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
from .state import StateDB
from .sync import SyncEngine
from .tray import SyncTray, TrayState

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


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
        choice = int(input("Select team number: ").strip()) - 1
        team = teams[choice]

    team_id = team["id"]

    # Browse WorkDrive folders
    print("\nBrowsing WorkDrive root folders...")
    root_items = api.list_folder(team_id)
    folders = [f for f in root_items if f.get("attributes", {}).get("is_folder")]

    if not folders:
        print("Error: No folders found in WorkDrive.")
        sys.exit(1)

    print("\nAvailable folders:")
    for i, f in enumerate(folders):
        print(f"  {i + 1}. {f.get('attributes', {}).get('name', f['id'])}")
    choice = int(input("Select folder to sync: ").strip()) - 1
    remote_folder = folders[choice]

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

        self.tray = SyncTray(
            on_sync_now=self._trigger_sync,
            on_open_conflicts=self._show_conflicts,
            on_quit=self._quit,
            local_folder=cfg.local_folder,
        )

        # Start file watcher
        self._start_watcher()

        # Start sync loop in background thread
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._sync_thread.start()

    def _start_watcher(self) -> None:
        """Start watchdog filesystem observer with debounced sync trigger."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

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
                    if not event.is_directory:
                        self._debounce()

            self._observer = Observer()
            self._observer.schedule(Handler(self._trigger_sync), self.cfg.local_folder, recursive=True)
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
        """Trigger an immediate sync (from tray or watchdog)."""
        threading.Thread(target=self._do_sync, daemon=True).start()

    def _do_sync(self) -> None:
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
                self.tray.set_state(TrayState.ERROR, f"{errors} error(s)")
            else:
                self.tray.set_state(TrayState.IDLE, "Synced")

        except Exception as e:
            logger.exception("Sync failed")
            self.tray.set_state(TrayState.ERROR, str(e)[:50])

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
            self.tray.set_state(TrayState.ERROR, f"{errors} error(s)")
        else:
            self.tray.set_state(TrayState.IDLE, "Synced")

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
