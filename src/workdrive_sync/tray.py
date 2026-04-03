"""System tray icon and menu."""

import logging
import subprocess
import webbrowser
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Optional

import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3
    HAS_APPINDICATOR = True
except (ValueError, ImportError):
    HAS_APPINDICATOR = False

from gi.repository import Gtk, GLib

logger = logging.getLogger(__name__)


class TrayState(Enum):
    IDLE = auto()
    SYNCING = auto()
    CONFLICT = auto()
    ERROR = auto()


# Icon names from the system icon theme
ICON_MAP = {
    TrayState.IDLE: "folder-sync",        # fallback: folder
    TrayState.SYNCING: "emblem-synchronizing",  # fallback: view-refresh
    TrayState.CONFLICT: "dialog-warning",
    TrayState.ERROR: "dialog-error",
}

FALLBACK_ICONS = {
    TrayState.IDLE: "folder",
    TrayState.SYNCING: "view-refresh",
    TrayState.CONFLICT: "dialog-warning",
    TrayState.ERROR: "dialog-error",
}


class SyncTray:
    """System tray icon with status and context menu."""

    def __init__(
        self,
        on_sync_now: Callable,
        on_open_conflicts: Callable,
        on_quit: Callable,
        local_folder: str = "",
        workdrive_url: str = "https://workdrive.zoho.eu",
    ):
        self.on_sync_now = on_sync_now
        self.on_open_conflicts = on_open_conflicts
        self.on_quit = on_quit
        self.local_folder = local_folder
        self.workdrive_url = workdrive_url
        self._state = TrayState.IDLE
        self._status_text = "Idle"

        self._build_menu()

        if HAS_APPINDICATOR:
            self.indicator = AppIndicator3.Indicator.new(
                "workdrive-sync",
                self._icon_name(TrayState.IDLE),
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.indicator.set_menu(self.menu)
        else:
            self.status_icon = Gtk.StatusIcon()
            self.status_icon.set_from_icon_name(self._icon_name(TrayState.IDLE))
            self.status_icon.set_tooltip_text("WorkDrive Sync")
            self.status_icon.connect("popup-menu", self._on_popup)
            self.status_icon.set_visible(True)

    def _icon_name(self, state: TrayState) -> str:
        name = ICON_MAP[state]
        theme = Gtk.IconTheme.get_default()
        if theme.has_icon(name):
            return name
        return FALLBACK_ICONS.get(state, "dialog-information")

    def _build_menu(self) -> None:
        self.menu = Gtk.Menu()

        self._status_item = Gtk.MenuItem(label="Status: Idle")
        self._status_item.set_sensitive(False)
        self.menu.append(self._status_item)
        self.menu.append(Gtk.SeparatorMenuItem())

        item_sync = Gtk.MenuItem(label="Sync Now")
        item_sync.connect("activate", lambda _: self.on_sync_now())
        self.menu.append(item_sync)

        item_folder = Gtk.MenuItem(label="Open Sync Folder")
        item_folder.connect("activate", lambda _: self._open_folder())
        self.menu.append(item_folder)

        item_web = Gtk.MenuItem(label="Open WorkDrive")
        item_web.connect("activate", lambda _: webbrowser.open(self.workdrive_url))
        self.menu.append(item_web)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda _: self.on_quit())
        self.menu.append(item_quit)

        self.menu.show_all()

    def _on_popup(self, icon, button, time):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, time)

    def _open_folder(self) -> None:
        if self.local_folder and Path(self.local_folder).is_dir():
            subprocess.Popen(["xdg-open", self.local_folder])

    def set_state(self, state: TrayState, text: str = "") -> None:
        self._state = state
        self._status_text = text or state.name.capitalize()
        GLib.idle_add(self._update_ui)

    def _update_ui(self) -> None:
        icon = self._icon_name(self._state)
        self._status_item.set_label(f"Status: {self._status_text}")

        if HAS_APPINDICATOR:
            self.indicator.set_icon(icon)
        else:
            self.status_icon.set_from_icon_name(icon)
            self.status_icon.set_tooltip_text(f"WorkDrive Sync - {self._status_text}")
