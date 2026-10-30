"""The orphaned global save folders, out of the scan page since 0.7.0 (D42): opened from the
link under the games table and ticked here with their own 「全选」. Since 0.8.0 the ticked
folders are backed up with the ticked games by the scan page's 「备份已勾选项」 and 「预览…」;
the dialog's button of the same name does the same, right here (D42, D47 7)."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from enginesave.gui import state
from enginesave.gui.app import set_enabled
from enginesave.gui.i18n import _
from enginesave.gui.views.dialogs.base import LINK_COLUMN, OPEN_LINK, PAD, Dialog, scrolled_tree
from enginesave.models import ScanReport, UnlinkedSaveDir

if TYPE_CHECKING:
    from enginesave.gui.app import App

INTRO = (
    "这些目录在全局存档根目录下，找不到对应的游戏本体（UNLINKED，默认不备份）。"
    "勾选的目录会和扫描页勾选的游戏一起，由「备份已勾选项」备份进当前备份库。"
)


class UnlinkedDialog(Dialog):
    def __init__(self, app: App, report: ScanReport) -> None:
        super().__init__(app, _("孤立存档目录列表"), min_size=(900, 420))
        self.report = report
        self.items: dict[str, UnlinkedSaveDir] = {}
        ttk.Label(self.body, text=_(INTRO), wraplength=860).pack(anchor="w")
        bar = ttk.Frame(self.body)
        bar.pack(fill="x", pady=(PAD, 4))
        self.toggle_button = ttk.Button(bar, text=_("全选"), command=self.toggle_all)
        self.toggle_button.pack(side="left")
        self.count = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.count).pack(side="left", padx=PAD)
        self.tree = scrolled_tree(
            self.body,
            [
                ("check", _("勾选"), 46),
                ("name", _("名称"), 300),
                ("files", _("文件数"), 70),
                ("size", _("大小"), 90),
                ("modified", _("最近修改"), 140),
                ("version", _("版本提示"), 140),
                LINK_COLUMN,
            ],
            height=10,
        )
        self.tree.column("check", anchor="center")
        for index, item in enumerate(report.unlinked):
            iid = f"u{index}"
            self.items[iid] = item
            folders = state.unlinked_folders(item)
            if folders:
                self.links[iid] = folders[0].path
            row = state.unlinked_row(item, app.selection)
            self.tree.insert("", "end", iid=iid, values=(*row.values(), _(OPEN_LINK)))
        self.tree.bind("<ButtonRelease-1>", self.click)
        self.tree.bind("<space>", self.space)
        self.link_column(self.tree)
        self.button(_("关闭"), self.cancel, action=False)
        self.backup_button = self.primary(_("备份已勾选项"), self.backup)
        self.refresh_buttons()
        self.show()

    def click(self, event: tk.Event) -> None:
        if (
            self.tree.identify_region(event.x, event.y) == "cell"
            and self.tree.identify_column(event.x) == "#1"
        ):
            iid = self.tree.identify_row(event.y)
            if iid:
                self.toggle(iid)

    def space(self, _event: tk.Event) -> str:
        for iid in self.tree.selection():
            self.toggle(iid)
        return "break"

    def toggle(self, iid: str) -> None:
        item = self.items.get(iid)
        if item is not None:
            self.app.selection.toggle_unlinked(state.unlinked_key(item))
            self.refresh_buttons()

    def toggle_all(self) -> None:
        self.app.selection.toggle_all_unlinked()
        self.refresh_buttons()

    def checked(self) -> list[str]:
        return self.app.selection.checked_unlinked(self.report)

    def refresh_buttons(self) -> None:
        selection = self.app.selection
        for iid, item in self.items.items():
            ticked = selection.unlinked.get(state.unlinked_key(item))
            self.tree.set(iid, "check", state.CHECKED if ticked else state.UNCHECKED)
        games, unlinked = selection.counts()
        self.count.set(state.unlinked_dialog_count(games, unlinked))
        self.app.scan_view.update_counts()  # the scan page counts them too (S-131)
        self.toggle_button.configure(text=state.toggle_all_text(selection.all_unlinked_checked()))
        set_enabled(self.backup_button, bool(games or unlinked) and not self.busy)

    def backup(self) -> None:
        """「备份已勾选项」 right here: the ticked folders and the games ticked on the scan
        page, the way the scan page's button does it (one click, the result dialog)."""
        games, unlinked = self.app.selection.counts()
        if not games and not unlinked:
            return
        self.close()
        self.app.backup_checked()
