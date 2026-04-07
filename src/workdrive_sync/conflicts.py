"""Batched conflict resolution GTK dialog."""

import logging
from typing import List

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from .sync import ConflictType, Resolution, SyncItem

logger = logging.getLogger(__name__)

RESOLUTION_OPTIONS = [Resolution.KEEP_LOCAL, Resolution.KEEP_REMOTE, Resolution.KEEP_BOTH, Resolution.MARK_SYNCED, Resolution.SKIP]


class ConflictDialog(Gtk.Dialog):
    """Shows a list of sync conflicts for batch resolution."""

    def __init__(self, conflicts: List[SyncItem], parent=None):
        super().__init__(
            title=f"Sync Conflicts ({len(conflicts)} files)",
            transient_for=parent,
            modal=True,
        )
        self.set_default_size(650, 400)
        self.conflicts = conflicts

        # Default all to SKIP
        for c in self.conflicts:
            c.resolution = Resolution.SKIP

        box = self.get_content_area()
        box.set_spacing(8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        # Scrolled list
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        box.pack_start(scroll, True, True, 0)

        # List store: file, conflict type, resolution index
        skip_idx = RESOLUTION_OPTIONS.index(Resolution.SKIP)
        self.store = Gtk.ListStore(str, str, int, int)  # file, conflict, res_idx, conflict_list_idx
        for i, c in enumerate(conflicts):
            self.store.append([c.rel_path, c.conflict_type.value if c.conflict_type else "", skip_idx, i])

        tree = Gtk.TreeView(model=self.store)
        tree.set_headers_visible(True)

        # File column
        col_file = Gtk.TreeViewColumn("File", Gtk.CellRendererText(), text=0)
        col_file.set_expand(True)
        col_file.set_resizable(True)
        tree.append_column(col_file)

        # Conflict type column
        col_type = Gtk.TreeViewColumn("Conflict", Gtk.CellRendererText(), text=1)
        col_type.set_min_width(160)
        tree.append_column(col_type)

        # Resolution combo column
        res_store = Gtk.ListStore(str)
        for r in RESOLUTION_OPTIONS:
            res_store.append([r.value])

        renderer_combo = Gtk.CellRendererCombo()
        renderer_combo.set_property("model", res_store)
        renderer_combo.set_property("text-column", 0)
        renderer_combo.set_property("editable", True)
        renderer_combo.set_property("has-entry", False)
        renderer_combo.connect("changed", self._on_resolution_changed)

        col_res = Gtk.TreeViewColumn("Action", renderer_combo, text=2)
        col_res.set_min_width(130)
        # Show the text from RESOLUTION_OPTIONS instead of the index
        col_res.set_cell_data_func(renderer_combo, self._render_resolution)
        tree.append_column(col_res)

        scroll.add(tree)

        # Bulk action buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        box.pack_start(btn_box, False, False, 0)

        for label, res in [("All Local", Resolution.KEEP_LOCAL), ("All Remote", Resolution.KEEP_REMOTE),
                           ("All Both", Resolution.KEEP_BOTH), ("All Mark Synced", Resolution.MARK_SYNCED)]:
            btn = Gtk.Button(label=label)
            btn.connect("clicked", self._on_bulk, res)
            btn_box.pack_start(btn, False, False, 0)

        # Apply / Skip buttons
        self.add_button("Skip All", Gtk.ResponseType.CANCEL)
        self.add_button("Apply", Gtk.ResponseType.OK)

        self.show_all()

    def _render_resolution(self, column, cell, model, iter_, data=None):
        idx = model[iter_][2]
        cell.set_property("text", RESOLUTION_OPTIONS[idx].value)

    def _on_resolution_changed(self, combo, path, new_iter):
        res_model = combo.get_property("model")
        text = res_model[new_iter][0]
        for i, r in enumerate(RESOLUTION_OPTIONS):
            if r.value == text:
                self.store[path][2] = i
                conflict_idx = self.store[path][3]
                self.conflicts[conflict_idx].resolution = r
                break

    def _on_bulk(self, button, resolution: Resolution):
        idx = RESOLUTION_OPTIONS.index(resolution)
        for row in self.store:
            row[2] = idx
            conflict_idx = row[3]
            self.conflicts[conflict_idx].resolution = resolution


def resolve_conflicts(conflicts: List[SyncItem]) -> List[SyncItem]:
    """Show the conflict dialog and return items with resolutions set.

    Must be called from the GTK main thread (or via GLib.idle_add).
    Returns only items that are not SKIP.
    """
    if not conflicts:
        return []

    dialog = ConflictDialog(conflicts)
    response = dialog.run()
    dialog.destroy()

    if response == Gtk.ResponseType.OK:
        return [c for c in conflicts if c.resolution != Resolution.SKIP]
    return []  # Skip all
