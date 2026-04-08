"""Two-way sync engine with conflict detection."""

import logging
import os
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Tuple

from .api import WorkDriveAPI
from .state import FileRecord, StateDB, file_hash

logger = logging.getLogger(__name__)


class Action(Enum):
    SKIP = auto()
    UPLOAD = auto()
    DOWNLOAD = auto()
    LOCAL_DELETE = auto()
    REMOTE_DELETE = auto()
    CONFLICT = auto()
    REMOVE_STATE = auto()


class ConflictType(Enum):
    BOTH_MODIFIED = "Both modified"
    BOTH_ADDED = "Both added"
    LOCAL_MOD_REMOTE_DEL = "Modified locally, deleted remotely"
    LOCAL_DEL_REMOTE_MOD = "Deleted locally, modified remotely"


class Resolution(Enum):
    KEEP_LOCAL = "Keep local"
    KEEP_REMOTE = "Keep remote"
    KEEP_BOTH = "Keep both"
    MARK_SYNCED = "Mark synced"
    SKIP = "Skip"


@dataclass
class SyncItem:
    rel_path: str
    action: Action
    conflict_type: ConflictType | None = None
    resolution: Resolution | None = None
    # Populated during scanning
    local_path: Path | None = None
    remote_id: str = ""
    remote_etag: str = ""
    remote_modified: str = ""


class SyncEngine:
    """Detects changes and executes sync actions."""

    def __init__(self, api: WorkDriveAPI, db: StateDB, local_root: Path, remote_folder_id: str):
        self.api = api
        self.db = db
        self.local_root = local_root
        self.remote_folder_id = remote_folder_id

    def scan(self) -> Tuple[List[SyncItem], List[SyncItem]]:
        """Scan local and remote, return (actions, conflicts)."""
        known = self.db.all()

        # Scan local filesystem
        local_files: Dict[str, Tuple[float, str]] = {}  # rel_path -> (mtime, hash)
        for root, _dirs, files in os.walk(self.local_root):
            for name in files:
                full = Path(root) / name
                rel = str(full.relative_to(self.local_root))
                try:
                    mtime = full.stat().st_mtime
                    local_files[rel] = (mtime, "")  # hash computed lazily
                except OSError:
                    pass

        # Scan remote
        remote_files: Dict[str, Dict] = {}  # rel_path -> item dict
        for item in self.api.walk_remote(self.remote_folder_id):
            rel = item.get("rel_path", "")
            if rel:
                remote_files[rel] = item

        # Collect all known paths
        all_paths = set(known.keys()) | set(local_files.keys()) | set(remote_files.keys())

        actions: List[SyncItem] = []
        conflicts: List[SyncItem] = []

        for rel in sorted(all_paths):
            rec = known.get(rel)
            in_local = rel in local_files
            in_remote = rel in remote_files

            local_changed = False
            remote_changed = False

            # Determine local state
            if in_local and rec:
                mtime, _ = local_files[rel]
                if mtime != rec.local_mtime:
                    h = file_hash(self.local_root / rel)
                    local_changed = h != rec.local_hash
                    local_files[rel] = (mtime, h)
            local_added = in_local and not rec
            local_deleted = not in_local and rec is not None

            # Determine remote state
            if in_remote and rec:
                r_attrs = remote_files[rel].get("attributes", {})
                r_etag = r_attrs.get("resource_etag", "")
                r_mod = r_attrs.get("modified_time", "")
                remote_changed = (r_etag != rec.remote_etag) or (r_mod != rec.remote_modified)

            remote_added = in_remote and not rec
            remote_deleted = not in_remote and rec is not None and rec.remote_id

            # Build SyncItem
            item = SyncItem(
                rel_path=rel,
                action=Action.SKIP,
                local_path=self.local_root / rel if in_local else None,
                remote_id=remote_files[rel]["id"] if in_remote else (rec.remote_id if rec else ""),
                remote_etag=remote_files[rel].get("attributes", {}).get("resource_etag", "") if in_remote else "",
                remote_modified=remote_files[rel].get("attributes", {}).get("modified_time", "") if in_remote else "",
            )

            # Classify action
            if local_added and not in_remote:
                item.action = Action.UPLOAD
            elif local_changed and not remote_changed:
                item.action = Action.UPLOAD
            elif local_deleted and not remote_changed and not remote_deleted:
                item.action = Action.REMOTE_DELETE
            elif remote_added and not in_local:
                item.action = Action.DOWNLOAD
            elif remote_changed and not local_changed:
                item.action = Action.DOWNLOAD
            elif remote_deleted and not local_changed and not local_deleted:
                item.action = Action.LOCAL_DELETE
            elif local_deleted and remote_deleted:
                item.action = Action.REMOVE_STATE
            elif (local_added and remote_added):
                item.action = Action.CONFLICT
                item.conflict_type = ConflictType.BOTH_ADDED
            elif (local_changed and remote_changed):
                item.action = Action.CONFLICT
                item.conflict_type = ConflictType.BOTH_MODIFIED
            elif local_changed and remote_deleted:
                item.action = Action.CONFLICT
                item.conflict_type = ConflictType.LOCAL_MOD_REMOTE_DEL
            elif local_deleted and remote_changed:
                item.action = Action.CONFLICT
                item.conflict_type = ConflictType.LOCAL_DEL_REMOTE_MOD
            # else: both unchanged -> SKIP

            if item.action == Action.CONFLICT:
                conflicts.append(item)
            elif item.action != Action.SKIP:
                actions.append(item)

        return actions, conflicts

    def execute(self, items: List[SyncItem]) -> List[str]:
        """Execute a list of sync actions. Returns list of error messages."""
        errors: List[str] = []
        for item in items:
            try:
                self._execute_one(item)
            except Exception as e:
                msg = f"{item.rel_path}: {e}"
                logger.error(f"Sync failed for {msg}")
                errors.append(msg)
        return errors

    def _execute_one(self, item: SyncItem) -> None:
        rel = item.rel_path
        local = self.local_root / rel

        if item.action == Action.UPLOAD:
            logger.info(f"Uploading: {rel}")
            parent_id = self.api.ensure_remote_dirs(self.remote_folder_id, rel)
            if item.remote_id:
                result = self.api.update_file(item.remote_id, local)
            else:
                result = self.api.upload_file(parent_id, local)
            # Upload response lacks etag/modified_time; fetch full metadata
            file_id = (result.get("id")
                       or result.get("attributes", {}).get("resource_id")
                       or item.remote_id)
            meta = self.api.get_file_meta(file_id)
            attrs = meta.get("attributes", {})
            self.db.upsert(FileRecord(
                rel_path=rel,
                local_mtime=local.stat().st_mtime,
                local_hash=file_hash(local),
                remote_etag=attrs.get("resource_etag", ""),
                remote_modified=attrs.get("modified_time", ""),
                remote_id=meta.get("id", file_id),
            ))

        elif item.action == Action.DOWNLOAD:
            logger.info(f"Downloading: {rel}")
            self.api.download_file(item.remote_id, local)
            self.db.upsert(FileRecord(
                rel_path=rel,
                local_mtime=local.stat().st_mtime,
                local_hash=file_hash(local),
                remote_etag=item.remote_etag,
                remote_modified=item.remote_modified,
                remote_id=item.remote_id,
            ))

        elif item.action == Action.LOCAL_DELETE:
            logger.info(f"Deleting local: {rel}")
            if local.exists():
                local.unlink()
                # Remove empty parent dirs up to sync root
                parent = local.parent
                while parent != self.local_root:
                    try:
                        parent.rmdir()
                        parent = parent.parent
                    except OSError:
                        break
            self.db.remove(rel)

        elif item.action == Action.REMOTE_DELETE:
            logger.info(f"Deleting remote: {rel}")
            if item.remote_id:
                self.api.delete_file(item.remote_id)
            self.db.remove(rel)

        elif item.action == Action.REMOVE_STATE:
            self.db.remove(rel)

        elif item.action == Action.CONFLICT:
            self._resolve_conflict(item)

    def _resolve_conflict(self, item: SyncItem) -> None:
        """Execute a resolved conflict."""
        if item.resolution == Resolution.KEEP_LOCAL:
            if item.local_path and item.local_path.exists():
                item.action = Action.UPLOAD
                self._execute_one(item)
            else:
                # Local was deleted, confirm remote delete
                item.action = Action.REMOTE_DELETE
                self._execute_one(item)

        elif item.resolution == Resolution.KEEP_REMOTE:
            if item.remote_id:
                item.action = Action.DOWNLOAD
                self._execute_one(item)
            else:
                # Remote was deleted, confirm local delete
                item.action = Action.LOCAL_DELETE
                self._execute_one(item)

        elif item.resolution == Resolution.KEEP_BOTH:
            # Rename local file with conflict suffix, then download remote
            local = self.local_root / item.rel_path
            if local.exists():
                stem = local.stem
                suffix = local.suffix
                conflict_name = f"{stem} (conflict){suffix}"
                conflict_path = local.with_name(conflict_name)
                local.rename(conflict_path)
                logger.info(f"Renamed local to: {conflict_path.name}")
            if item.remote_id:
                item.action = Action.DOWNLOAD
                self._execute_one(item)

        elif item.resolution == Resolution.MARK_SYNCED:
            # Accept current state as baseline without transferring files
            local = self.local_root / item.rel_path
            logger.info(f"Marking as synced: {item.rel_path}")
            self.db.upsert(FileRecord(
                rel_path=item.rel_path,
                local_mtime=local.stat().st_mtime if local.exists() else 0,
                local_hash=file_hash(local) if local.exists() else "",
                remote_etag=item.remote_etag,
                remote_modified=item.remote_modified,
                remote_id=item.remote_id,
            ))

        # Resolution.SKIP -> do nothing
