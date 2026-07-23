"""Regression tests for WorkDriveAPI folder-cache pruning.

These guard the duplicate-folder bug. WorkDrive folder listings are
eventually consistent, so a freshly created folder can be missing from its
parent's listing for a while even though it already exists. The cache must
not evict (and then re-create) such a folder -- WorkDrive permits duplicate
names, so a re-create yields a visible duplicate folder, with every file
under it duplicated too.

The fake below overrides only the three HTTP primitives that walk_remote and
ensure_remote_dirs depend on, so the real cache logic runs unchanged.
"""

import requests

from workdrive_sync.api import WorkDriveAPI
from workdrive_sync.state import StateDB
from workdrive_sync.sync import Action, SyncEngine, SyncItem


class FakeWorkDrive(WorkDriveAPI):
    """In-memory WorkDrive that models eventual-consistency of listings.

    A created folder exists immediately for get-by-id, but stays invisible
    in its parent's listing until ``make_visible`` is called -- mirroring
    WorkDrive, where the create response carries a usable id before the
    parent listing catches up.
    """

    def __init__(self, root_id="ROOT"):
        # Deliberately skip WorkDriveAPI.__init__: no auth/pacer needed.
        self.root_id = root_id
        self._next = 1
        self.nodes = {}          # id -> {id, name, parent, is_folder, trashed}
        self.invisible = set()   # ids hidden from parent listings (lag)
        self.error_ids = set()   # ids whose get_file_meta raises non-404
        self.create_calls = []   # (parent_id, name) per create_folder
        self.upload_calls = []   # (parent_id, name, override) per upload_file
        self.meta_calls = []     # ids passed to get_file_meta

    # --- helpers for arranging the fake tree ---
    def _add(self, parent, name, is_folder, visible=True):
        nid = f"id{self._next}"
        self._next += 1
        self.nodes[nid] = {"id": nid, "name": name, "parent": parent,
                           "is_folder": is_folder, "trashed": False}
        if not visible:
            self.invisible.add(nid)
        return nid

    def make_visible(self, nid):
        self.invisible.discard(nid)

    def _children(self, nid):
        return [n["id"] for n in self.nodes.values() if n["parent"] == nid]

    def trash(self, nid, cascade=True):
        """Trash a node. Real WorkDrive cascades to children (get-by-id of a
        child then 404s); pass cascade=False to model the orphan edge where
        the parent is gone but a child is still reachable by id."""
        self.nodes[nid]["trashed"] = True
        if cascade:
            for child in self._children(nid):
                self.trash(child, cascade=True)

    # --- overridden HTTP primitives ---
    def list_folder(self, folder_id):
        out = []
        for n in self.nodes.values():
            if (n["parent"] == folder_id and not n["trashed"]
                    and n["id"] not in self.invisible):
                out.append({"id": n["id"], "attributes": {
                    "name": n["name"], "is_folder": n["is_folder"]}})
        return out

    def create_folder(self, parent_id, name):
        self.create_calls.append((parent_id, name))
        # New folders exist immediately by id but are invisible in the
        # parent listing for a while (eventual consistency).
        nid = self._add(parent_id, name, is_folder=True, visible=False)
        return {"id": nid, "attributes": {"name": name, "is_folder": True}}

    def upload_file(self, parent_id, local_path, override=False, filename=None):
        name = filename or local_path.name
        self.upload_calls.append((parent_id, name, override))
        nid = self._add(parent_id, name, is_folder=False, visible=True)
        return {"id": nid, "attributes": {
            "name": name, "is_folder": False, "resource_id": nid}}

    def get_file_meta(self, file_id):
        # get-by-id is authoritative: it returns a just-created folder even
        # while the parent listing still lags, and 404s a trashed one.
        self.meta_calls.append(file_id)
        if file_id in self.error_ids:
            resp = requests.Response()
            resp.status_code = 500
            raise requests.HTTPError(response=resp)
        n = self.nodes.get(file_id)
        if n is None or n["trashed"]:
            resp = requests.Response()
            resp.status_code = 404
            raise requests.HTTPError(response=resp)
        return {"id": n["id"], "attributes": {
            "name": n["name"], "is_folder": n["is_folder"]}}


def _db(tmp_path):
    return StateDB(path=tmp_path / "state.db")


def test_walk_does_not_evict_folder_invisible_due_to_consistency(tmp_path):
    """The core regression. A folder that exists but is absent from the
    parent listing (lag) must survive the walk's prune so the next
    ensure_remote_dirs reuses it instead of creating a duplicate.

    Fails on the old absence-based prune (it evicts "Proj", and the next
    resolve re-creates it -> two create_folder calls); passes with the
    404-confirmed prune."""
    api = FakeWorkDrive()
    db = _db(tmp_path)

    # First upload under "Proj" creates the folder and caches it.
    proj_id = api.ensure_remote_dirs(api.root_id, "Proj/deck.md", db=db)
    assert api.create_calls == [(api.root_id, "Proj")]
    assert db.get_folder("Proj") == (proj_id, api.root_id)

    # "Proj" is still invisible in ROOT's listing, so a full walk can't see
    # it. The old code prunes it here; the new code keeps it (exists by id).
    walked = api.walk_remote(api.root_id, db=db)
    assert all(item["rel_path"] != "Proj" for item in walked)
    assert db.get_folder("Proj") is not None, \
        "folder confirmed-present by id was wrongly evicted from the cache"

    # The next upload under the same folder must reuse the cached id, not
    # create a second "Proj".
    parent2 = api.ensure_remote_dirs(api.root_id, "Proj/notes.md", db=db)
    assert parent2 == proj_id
    assert api.create_calls == [(api.root_id, "Proj")], \
        f"duplicate folder created: {api.create_calls}"


def test_nested_invisible_folder_not_duplicated(tmp_path):
    """The log's actual shape: a subfolder ("assets") created under a
    freshly-made parent that is itself still invisible. Neither level may be
    re-created across a full walk."""
    api = FakeWorkDrive()
    db = _db(tmp_path)

    leaf = api.ensure_remote_dirs(api.root_id, "Proj/assets/img.png", db=db)
    assert [c[1] for c in api.create_calls] == ["Proj", "assets"]

    api.walk_remote(api.root_id, db=db)  # both still invisible -> unseen

    again = api.ensure_remote_dirs(api.root_id, "Proj/assets/img2.png", db=db)
    assert again == leaf
    assert [c[1] for c in api.create_calls] == ["Proj", "assets"], \
        f"duplicate folder(s) created: {api.create_calls}"


def test_prune_removes_folder_confirmed_deleted(tmp_path):
    """The original commit's intent must be preserved: a folder genuinely
    trashed remotely (404 by id) is still pruned from the cache."""
    api = FakeWorkDrive()
    db = _db(tmp_path)

    fid = api._add(api.root_id, "Old", is_folder=True, visible=True)
    api.walk_remote(api.root_id, db=db)
    assert db.get_folder("Old") is not None

    api.trash(fid)                       # gone remotely; listing drops it
    api.walk_remote(api.root_id, db=db)  # unseen AND 404 by id -> prune
    assert db.get_folder("Old") is None, "deleted folder not pruned from cache"


def test_seen_folders_are_not_existence_checked(tmp_path):
    """Steady state stays cheap: folders present in the walk incur no
    get_file_meta calls."""
    api = FakeWorkDrive()
    db = _db(tmp_path)
    api._add(api.root_id, "A", is_folder=True, visible=True)
    api._add(api.root_id, "B", is_folder=True, visible=True)
    api.walk_remote(api.root_id, db=db)

    api.meta_calls.clear()
    api.walk_remote(api.root_id, db=db)
    assert api.meta_calls == [], "seen folders should not be existence-checked"


def test_prune_drops_subtree_of_vanished_parent(tmp_path):
    """If a parent vanishes but a child is somehow still reachable by id
    (orphan edge, no cascade), the child's cache row must be dropped with
    the parent so the next resolve rebuilds the path cleanly instead of
    creating a duplicate child under a freshly-recreated parent."""
    api = FakeWorkDrive()
    db = _db(tmp_path)
    a = api._add(api.root_id, "A", is_folder=True, visible=True)
    api._add(a, "B", is_folder=True, visible=True)
    api.walk_remote(api.root_id, db=db)
    assert db.get_folder("A") is not None and db.get_folder("A/B") is not None

    # Parent gone (404), but child still answers get-by-id (orphan).
    api.trash(a, cascade=False)
    api.walk_remote(api.root_id, db=db)

    assert db.get_folder("A") is None
    assert db.get_folder("A/B") is None, "subtree of vanished parent not pruned"

    # No descendant API call once the ancestor is known gone.
    assert "id2" not in api.meta_calls, "child id should not be existence-checked"


def test_prune_only_removes_genuinely_deleted_among_invisible(tmp_path):
    """Many cached folders with mixed states in one walk: only the ones
    confirmed gone (404) are pruned; invisible-but-existing ones survive."""
    api = FakeWorkDrive()
    db = _db(tmp_path)
    a = api._add(api.root_id, "A", is_folder=True, visible=True)  # exists, lags
    b = api._add(api.root_id, "B", is_folder=True, visible=True)  # deleted
    c = api._add(api.root_id, "C", is_folder=True, visible=True)  # exists, lags
    d = api._add(api.root_id, "D", is_folder=True, visible=True)  # deleted
    api.walk_remote(api.root_id, db=db)

    api.invisible.update({a, c})    # A, C exist but are absent from listing
    api.trash(b)
    api.trash(d)
    api.walk_remote(api.root_id, db=db)

    assert db.get_folder("A") is not None
    assert db.get_folder("C") is not None
    assert db.get_folder("B") is None
    assert db.get_folder("D") is None


def test_prune_keeps_folder_when_existence_check_errors(tmp_path):
    """Transient non-404 errors must not evict a row -- "when unsure, never
    evict". An evicted-then-recreated folder is exactly the duplicate bug."""
    api = FakeWorkDrive()
    db = _db(tmp_path)
    e = api._add(api.root_id, "E", is_folder=True, visible=True)
    api.walk_remote(api.root_id, db=db)

    api.invisible.add(e)        # unseen by the next walk
    api.error_ids.add(e)        # ...and get_file_meta 500s for it
    api.walk_remote(api.root_id, db=db)

    assert db.get_folder("E") is not None, \
        "folder evicted on a transient error -- risks a duplicate on re-create"


# --- full-reconcile upload path: same collision guard as quick_upload ---

def test_execute_defers_new_file_when_remote_name_exists(tmp_path):
    """A new (untracked) file whose name already exists remotely must be
    deferred, not uploaded with override-name-exist=false (which forks a
    duplicate). The full-reconcile execute() path must apply the same guard
    quick_upload does. Fails on the pre-fix execute() (no guard -> upload);
    passes with the guard."""
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "report.pdf").write_bytes(b"local-bytes")

    api = FakeWorkDrive()
    db = _db(tmp_path)
    # Remote already has 'report.pdf' at the root (e.g. made in the web UI),
    # untracked in the local state DB.
    api._add(api.root_id, "report.pdf", is_folder=False, visible=True)

    engine = SyncEngine(api, db, local_root, api.root_id)
    item = SyncItem(rel_path="report.pdf", action=Action.UPLOAD,
                    local_path=local_root / "report.pdf")
    errors = engine.execute([item])

    assert errors == []
    assert api.upload_calls == [], \
        "new file uploaded despite a same-named remote sibling -> duplicate"


def test_execute_uploads_new_file_when_no_remote_collision(tmp_path):
    """The guard must not block the normal case: a genuinely new file with
    no remote sibling still uploads (with override=false, as before)."""
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "fresh.txt").write_text("x")

    api = FakeWorkDrive()
    db = _db(tmp_path)

    engine = SyncEngine(api, db, local_root, api.root_id)
    item = SyncItem(rel_path="fresh.txt", action=Action.UPLOAD,
                    local_path=local_root / "fresh.txt")
    errors = engine.execute([item])

    assert errors == []
    assert len(api.upload_calls) == 1
    assert api.upload_calls[0][1] == "fresh.txt"
    assert api.upload_calls[0][2] is False  # new file -> override stays false
