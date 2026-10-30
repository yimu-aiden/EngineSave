"""Right-click menus that open or copy the folders of a row (D36).

The rows decide which folders there are (``state.game_folders``, ``state.unlinked_folders``,
``state.record_folders``); a folder that is not there is greyed, cleaned up or not (D51). Opening
goes through ``App.open_folder`` (Explorer, or a notice when the folder has gone since) and
copying through ``App.copy_path``; nothing here reads or writes the folders themselves.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk
from typing import TYPE_CHECKING

from enginesave.gui import state
from enginesave.gui.i18n import _

if TYPE_CHECKING:
    from enginesave.gui.app import App

COPY_PATH = "复制路径"


def folder_menu(master: tk.Misc, app: App, folders: Sequence[state.Folder]) -> tk.Menu:
    """「打开…」 for every folder, then 「复制路径」 with one entry per folder."""
    menu = tk.Menu(master, tearoff=False)
    for entry in folders:
        menu.add_command(
            label=entry.open_label,
            command=lambda path=entry.path: app.open_folder(path),
            state="normal" if entry.exists else "disabled",
        )
    copy = tk.Menu(menu, tearoff=False)
    for entry in folders:
        copy.add_command(label=entry.label, command=lambda path=entry.path: app.copy_path(path))
    menu.add_separator()
    menu.add_cascade(label=_(COPY_PATH), menu=copy)
    return menu


class RowMenus:
    """Right click on a row selects it and shows the menu ``build`` makes for it; the
    previous menu is destroyed, so menus do not pile up."""

    def __init__(self, tree: ttk.Treeview, build: Callable[[str], tk.Menu | None]) -> None:
        self.tree = tree
        self.build = build
        self.menu: tk.Menu | None = None
        tree.bind("<Button-3>", self.show)

    def menu_for(self, iid: str) -> tk.Menu | None:
        if self.menu is not None:
            self.menu.destroy()
        self.menu = self.build(iid)
        return self.menu

    def show(self, event: tk.Event) -> str:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            menu = self.menu_for(iid)
            if menu is not None:
                try:
                    menu.tk_popup(event.x_root, event.y_root)
                finally:
                    menu.grab_release()
        return "break"
