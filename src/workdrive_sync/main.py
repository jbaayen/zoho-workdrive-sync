"""Entry point: CLI setup, tray icon, sync loop."""

import logging
import signal
import sys
import threading
import time
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from .api import WorkDriveAPI
from .auth import ZohoAuth, SCOPES
from .config import Config, load_config, save_config
from .conflicts import resolve_conflicts
from .errors import show_errors
from .state import StateDB
from .sync import SyncEngine
from .tray import SyncTray, TrayState

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("watchdog").setLevel(logging.WARNING)


def first_run_setup() -> Config:
    """Interactive first-run configuration via terminal."""
    print("\n=== WorkDrive Sync - First-Time Setup ===\n")

    client_id = input("Zoho client_id: ").strip()
    client_secret = input("Zoho client_secret: ").strip()

    print(f"\nGenerate a grant code at https://api-console.zoho.eu/")
    print(f"  Self Client -> Generate Code")
    print(f"  Scope: {SCOPES}\n")
    grant_code = input("Grant code: ").strip()

    auth = ZohoAuth(client_id, client_secret)
    auth.authorize(grant_code)

    api = WorkDriveAPI(auth)
    teams = api.list_teams()
    if not teams:
        print("Error: No WorkDrive teams found.")
        sys.exit(1)

    if len(teams) == 1:
        team = teams[0]
    else:
        print("\nAvailable teams:")
        for i, t in enumerate(teams):
            print(f"  {i + 1}. {t.get('attributes', {}).get('name', t['id'])}")
        try:
            choice = int(input("Select team number: ").strip()) - 1
        except ValueError:
            print("Error: Please enter a number.")
            sys.exit(1)
        if choice < 0 or choice >= len(teams):
            print(f"Error: Please enter a number between 1 and {len(teams)}.")
            sys.exit(1)
        team = teams[choice]

    team_id = team["id"]

    # Browse WorkDrive workspaces
    print("\nBrowsing WorkDrive workspaces...")
    workspaces = api.list_workspaces(team_id)

    if not workspaces:
        print("Error: No workspaces found in WorkDrive.")
        sys.exit(1)

    if len(workspaces) == 1:
        ws = workspaces[0]
        print(f"Using workspace: {ws.get('attributes', {}).get('name', ws['id'])}")
    else:
        print("\nAvailable workspaces:")
        for i, w in enumerate(workspaces):
            print(f"  {i + 1}. {w.get('attributes', {}).get('name', w['id'])}")
        try:
            choice = int(input("Select workspace number: ").strip()) - 1
        except ValueError:
            print("Error: Please enter a number.")
            sys.exit(1)
        if choice < 0 or choice >= len(workspaces):
            print(f"Error: Please enter a number between 1 and {len(workspaces)}.")
            sys.exit(1)
        ws = workspaces[choice]

    # Browse folders within the workspace
    print("\nBrowsing folders...")
    root_items = api.list_folder(ws["id"])
    folders = [f for f in root_items if f.get("attributes", {}).get("is_folder")]

    if not folders:
        # No subfolders — sync the workspace root directly
        remote_folder = ws
    else:
        print("\nAvailable folders:")
        print(f"  0. . (workspace root)")
        for i, f in enumerate(folders):
            print(f"  {i + 1}. {f.get('attributes', {}).get('name', f['id'])}")
        try:
            choice = int(input("Select folder to sync (0 for root): ").strip())
        except ValueError:
            print("Error: Please enter a number.")
            sys.exit(1)
        if choice < 0 or choice > len(folders):
            print(f"Error: Please enter a number between 0 and {len(folders)}.")
            sys.exit(1)
        remote_folder = ws if choice == 0 else folders[choice - 1]

    local_folder = input("\nLocal folder path: ").strip()
    local_folder = str(Path(local_folder).expanduser().resolve())
    Path(local_folder).mkdir(parents=True, exist_ok=True)

    cfg = Config(
        client_id=client_id,
        client_secret=client_secret,
        local_folder=local_folder,
        remote_folder_id=remote_folder["id"],
        remote_folder_name=remote_folder.get("attributes", {}).get("name", ""),
        team_id=team_id,
        workspace_id=ws["id"],
    )
    save_config(cfg)
    print(f"\nConfiguration saved. Syncing: {local_folder} <-> {cfg.remote_folder_name}")
    return cfg


class App:
    """Main application: ties together sync engine, tray, and watchdog."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.auth = ZohoAuth(cfg.client_id, cfg.client_secret)
        self.api = WorkDriveAPI(self.auth)
        self.db = StateDB()
        self.engine = SyncEngine(self.api, self.db, Path(cfg.local_folder), cfg.remote_folder_id)
        self._stop = threading.Event()
        self._pending_conflicts = []
        self._errors: list[str] = []
        self._sync_lock = threading.Lock()
        # Set while a full sync is in flight so a saved file isn't dropped;
        # the full sync drains this with one fast-upload pass before exiting.
        self._pending_fast_upload = False
        # Watchdog ignores events until this wall-clock time. Bumped after
        # syncs so downloads don't trigger an immediate fast-upload pass.
        self._suppress_watcher_until = 0.0

        self.tray = SyncTray(
            on_sync_now=self._trigger_sync,
            on_open_conflicts=self._show_conflicts,
            on_quit=self._quit,
            on_show_errors=self._show_errors,
            local_folder=cfg.local_folder,
            workdrive_url=self._build_workdrive_url(cfg),
        )

        # Start file watcher
        self._start_watcher()

        # Start sync loop in background thread
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._sync_thread.start()

    @staticmethod
    def _build_workdrive_url(cfg: Config) -> str:
        # Fall back to remote_folder_id for older configs that didn't
        # persist workspace_id — in that case the synced folder is the
        # top-level team folder, which doubles as the workspace slot.
        workspace_id = cfg.workspace_id or cfg.remote_folder_id
        base = f"https://workplace.zoho.eu/#workdrive_app/{cfg.team_id}/teams/{cfg.team_id}/ws/{workspace_id}"
        if cfg.remote_folder_id == workspace_id:
            return f"{base}/folders/files"
        return f"{base}/folders/{cfg.remote_folder_id}"

    def _start_watcher(self) -> None:
        """Start watchdog filesystem observer with debounced sync trigger."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            app = self

            class Handler(FileSystemEventHandler):
                def __init__(self, trigger):
                    self._trigger = trigger
                    self._timer = None

                def _debounce(self):
                    if self._timer:
                        self._timer.cancel()
                    self._timer = threading.Timer(5.0, self._trigger)
                    self._timer.daemon = True
                    self._timer.start()

                def on_any_event(self, event):
                    if event.is_directory:
                        return
                    if time.time() < app._suppress_watcher_until:
                        return
                    self._debounce()

            self._observer = Observer()
            self._observer.schedule(Handler(self._trigger_fast_upload), self.cfg.local_folder, recursive=True)
            self._observer.start()
            logger.info(f"Watching: {self.cfg.local_folder}")
        except ImportError:
            logger.warning("watchdog not installed, filesystem watching disabled")

    def _sync_loop(self) -> None:
        """Periodic sync loop."""
        # Initial sync after 2s (let GTK settle)
        self._stop.wait(2)
        while not self._stop.is_set():
            self._do_sync()
            self._stop.wait(self.cfg.interval_seconds)

    def _trigger_sync(self) -> None:
        """Trigger an immediate full sync (from tray "Sync Now")."""
        threading.Thread(target=self._do_sync, daemon=True).start()

    def _trigger_fast_upload(self) -> None:
        """Trigger a local-only fast-upload pass (from the watcher)."""
        threading.Thread(target=self._do_fast_upload, daemon=True).start()

    def _do_fast_upload(self) -> None:
        if not self._sync_lock.acquire(blocking=False):
            # A full sync is in flight; it will drain pending work when done.
            self._pending_fast_upload = True
            return
        try:
            start = time.time()
            items = self.engine.scan_local_changes()
            if not items:
                return
            self.tray.set_state(TrayState.SYNCING, f"Uploading {len(items)}...")
            errors = self.engine.quick_upload(items)
            logger.info("fast-upload: %d file(s) in %.1fs", len(items), time.time() - start)
            if errors:
                self._set_errors(errors)
            elif not self._errors:
                self.tray.set_state(TrayState.IDLE, "Synced")
        finally:
            # Give downloads/writes from this pass a window to settle so
            # the watcher doesn't immediately re-fire on our own changes.
            self._suppress_watcher_until = time.time() + 10
            self._sync_lock.release()

    def _do_sync(self) -> None:
        # Skip if another sync is already running. The watcher debounce can
        # fire during a long sync (e.g. because downloads create FS events),
        # and overlapping runs just multiply API pressure and trip 429s.
        if not self._sync_lock.acquire(blocking=False):
            logger.debug("Sync already in progress, skipping trigger")
            return
        try:
            self.tray.set_state(TrayState.SYNCING, "Syncing...")
            try:
                actions, conflicts = self.engine.scan()

                # Execute non-conflicting actions
                errors = self.engine.execute(actions)

                if conflicts:
                    self._pending_conflicts = conflicts
                    self.tray.set_state(TrayState.CONFLICT, f"{len(conflicts)} conflict(s)")
                    # Show conflict dialog on GTK thread
                    GLib.idle_add(self._show_conflicts)
                elif errors:
                    self._set_errors(errors)
                else:
                    self._errors = []
                    self.tray.set_state(TrayState.IDLE, "Synced")

            except Exception as e:
                logger.exception("Sync failed")
                self._set_errors([str(e)])

            # Drain any fast-upload requests that arrived while we were
            # scanning, so a save made mid-sync still lands promptly.
            if self._pending_fast_upload:
                self._pending_fast_upload = False
                try:
                    pending_items = self.engine.scan_local_changes()
                    if pending_items:
                        logger.info("drain: %d pending fast-upload item(s)", len(pending_items))
                        drain_errors = self.engine.quick_upload(pending_items)
                        if drain_errors:
                            self._set_errors(list(self._errors) + drain_errors)
                except Exception:
                    logger.exception("Pending fast-upload drain failed")
        finally:
            self._suppress_watcher_until = time.time() + 10
            self._sync_lock.release()

    def _show_conflicts(self) -> None:
        if not self._pending_conflicts:
            return
        resolved = resolve_conflicts(self._pending_conflicts)
        if resolved:
            threading.Thread(target=self._apply_resolutions, args=(resolved,), daemon=True).start()
        self._pending_conflicts = []

    def _apply_resolutions(self, items) -> None:
        self.tray.set_state(TrayState.SYNCING, "Resolving conflicts...")
        errors = self.engine.execute(items)
        if errors:
            self._set_errors(errors)
        else:
            self._errors = []
            self.tray.set_state(TrayState.IDLE, "Synced")

    def _set_errors(self, errors: list[str]) -> None:
        self._errors = list(errors)
        count = len(self._errors)
        label = f"{count} error" + ("s" if count != 1 else "")
        self.tray.set_state(TrayState.ERROR, label)

    def _show_errors(self) -> None:
        """Open the error dialog. Called from the GTK main thread."""
        if not self._errors:
            return
        ignore_all = show_errors(self._errors)
        if ignore_all:
            self._errors = []
            self.tray.set_state(TrayState.IDLE, "Idle")

    def _quit(self) -> None:
        self._stop.set()
        if hasattr(self, "_observer"):
            self._observer.stop()
        self.db.close()
        Gtk.main_quit()

    def run(self) -> None:
        Gtk.main()


def main() -> None:
    setup_logging()
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Allow Ctrl+C

    cfg = load_config()
    if not cfg.client_id:
        cfg = first_run_setup()

    app = App(cfg)
    app.run()


if __name__ == "__main__":
    main()
