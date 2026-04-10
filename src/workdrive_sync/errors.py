"""Error list dialog."""

import logging
from typing import List

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

logger = logging.getLogger(__name__)


class ErrorDialog(Gtk.Dialog):
    """Shows a scrollable list of sync errors."""

    RESPONSE_IGNORE_ALL = 1

    def __init__(self, errors: List[str], parent=None):
        super().__init__(
            title=f"Sync Errors ({len(errors)})",
            transient_for=parent,
            modal=True,
        )
        self.set_default_size(650, 400)

        box = self.get_content_area()
        box.set_spacing(8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box.pack_start(scroll, True, True, 0)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        for err in errors:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=err)
            label.set_line_wrap(True)
            label.set_xalign(0.0)
            label.set_selectable(True)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            label.set_margin_end(8)
            row.add(label)
            list_box.add(row)
        scroll.add(list_box)

        self.add_button("Ignore All", self.RESPONSE_IGNORE_ALL)
        self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.set_default_response(Gtk.ResponseType.CLOSE)

        self.show_all()


def show_errors(errors: List[str]) -> bool:
    """Show the error dialog. Returns True if the user chose Ignore All.

    Must be called from the GTK main thread (or via GLib.idle_add).
    """
    if not errors:
        return False

    dialog = ErrorDialog(errors)
    response = dialog.run()
    dialog.destroy()

    return response == ErrorDialog.RESPONSE_IGNORE_ALL
