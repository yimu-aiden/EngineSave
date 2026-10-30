"""Tab 1 「扫描与备份」: the games found, with check boxes, a search box (D46), [备份已勾选项]
(one click, D32) and [预览…]. One button ticks every game or none (D33); a right click on a
row opens or copies its folders (D36). The orphaned folders are behind a link under the table
since 0.7.0 (D42).

Since 0.9.0 the games are in four groups that fold (D54): 有新存档可备份, 已备份、无新存档, 待确认
and 不支持. A click on a group's title row (or its triangle, or ←/→) opens or closes it; the
window remembers that until it closes, also across scans. While the search box has text every
group is open; the tick button works on the rows that can be seen."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from enginesave.gui import state
from enginesave.gui.i18n import _
from enginesave.gui.views.dialogs import (
    LINK_COLOR,
    PAD,
    TextDialog,
    UnlinkedDialog,
    game_details_dialog,
)
from enginesave.gui.views.filter_box import FilterBox
from enginesave.gui.views.menus import RowMenus, folder_menu
from enginesave.models import DetectedGame, GameStatus, ScanReport

if TYPE_CHECKING:
    from enginesave.gui.app import App

GAME_COLUMNS = [
    ("check", "勾选", 46),
    ("status", "状态", 100),
    ("name", "名称", 230),
    ("version", "版本", 90),
    ("engine", "引擎", 140),
    ("local", "本地", 170),
    ("global", "全局", 170),
    ("notes", "备注", 240),
]
GUIDE_BACKGROUND = "#E8F1FB"
GUIDE_FOREGROUND = "#0B3D91"
STATUS_COLORS = {
    "CONFLICT": "#b3261e",
    "CANDIDATE": "#8a5300",
    "NOT_FOUND": "#6b6b6b",
    "UNSUPPORTED": "#6b6b6b",
}


def make_tree(
    parent: tk.Misc, columns: Sequence[tuple[str, str, int]], height: int
) -> ttk.Treeview:
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height)
    for column, heading, width in columns:
        tree.heading(column, text=_(heading))  # D68: the headings are module constants
        anchor = "center" if column == "check" else "w"
        tree.column(column, width=width, minwidth=40, anchor=anchor, stretch=column != "check")
    ybar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    xbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ybar.grid(row=0, column=1, sticky="ns")
    xbar.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return tree


GROUP_IID = "group-"  # the iid prefix of the groups' title rows (D54)


class ScanView(ttk.Frame):
    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master, padding=PAD)
        self.app = app
        self.games_by_iid: dict[str, DetectedGame] = {}
        self.group_open = dict(state.GROUP_OPEN_BY_DEFAULT)  # D54 ②: kept for this window
        self.members: dict[str, list[str]] = {}  # each group's rows, as last shown
        self.guide_var = tk.StringVar(value="")
        self.guide = tk.Label(
            self,
            textvariable=self.guide_var,
            background=GUIDE_BACKGROUND,
            foreground=GUIDE_FOREGROUND,
            justify="left",
            anchor="w",
            wraplength=1100,
            padx=10,
            pady=8,
        )
        self.summary = tk.StringVar(value=_("尚未扫描：先在上方添加扫描根目录，再点“扫描”。"))
        self.summary_label = ttk.Frame(self)  # the summary line (the banner goes above it)
        self.summary_label.pack(anchor="w", fill="x")
        ttk.Label(self.summary_label, textvariable=self.summary).pack(side="left")
        # D34: the skipped folders are one line in the log; the whole list is behind this link.
        link_font = tkfont.nametofont("TkDefaultFont").copy()
        link_font.configure(underline=True)
        self.skipped_link = tk.Label(
            self.summary_label, text="", foreground=LINK_COLOR, cursor="hand2", font=link_font
        )
        self.skipped_link.bind("<Button-1>", lambda _e: self.show_skipped())

        # Bars are packed before the tables so a short window shrinks the tables, not the bars.
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", pady=(PAD, 0))
        self.counts = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.counts).pack(side="left")
        # D32: the main button backs up what is ticked in one click; 「预览…」 looks first.
        self.backup_button = ttk.Button(
            bottom, text=_("备份已勾选项"), command=app.backup_checked, default="active"
        )
        self.backup_button.pack(side="right")
        self.preview_button = ttk.Button(bottom, text=_("预览…"), command=app.preview_backup)
        self.preview_button.pack(side="right", padx=(0, PAD))
        app.register_action(self.backup_button)
        app.register_action(self.preview_button)

        games_frame = ttk.LabelFrame(self, text=_("游戏"), padding=4)
        games_frame.pack(fill="both", expand=True, pady=(4, 0))
        top = ttk.Frame(games_frame)
        top.pack(side="top", fill="x", pady=(0, 4))
        self.filter = FilterBox(top, lambda _query: self.show_rows())  # D46
        self.filter.pack(side="left")
        bar = ttk.Frame(games_frame)
        bar.pack(side="bottom", fill="x", pady=(4, 0))
        # D33 ③: one button, its text follows the ticks; every scan ticks CONFIRMED again.
        self.toggle_button = ttk.Button(bar, text=_("全选"), command=self.toggle_all)
        self.toggle_button.pack(side="left")
        ttk.Button(bar, text=_("查看详情"), command=self.details).pack(side="left", padx=PAD)
        ttk.Label(
            bar, text=_("点“勾选”列或按空格键切换勾选；双击一行查看详情；右键打开或复制文件夹。")
        ).pack(side="left")
        # D42: the orphaned folders are behind this link (hidden when there are none).
        self.unlinked_link = tk.Label(
            bar, text="", foreground=LINK_COLOR, cursor="hand2", font=link_font
        )
        self.unlinked_link.bind("<Button-1>", lambda _e: self.show_unlinked())
        self.games = make_tree(games_frame, GAME_COLUMNS, height=12)
        # D54: the tree column only carries the groups' triangles
        self.games.configure(show=("tree", "headings"))
        self.games.column("#0", width=28, minwidth=28, stretch=False)
        for status, color in STATUS_COLORS.items():
            self.games.tag_configure(status, foreground=color)
        group_font = tkfont.nametofont("TkDefaultFont").copy()
        group_font.configure(weight="bold")
        self.games.tag_configure("group", font=group_font)
        self.games.tag_configure("empty", foreground="#8a8a8a")

        self._bind(self.games, self.toggle_game)
        self.games.bind("<Double-1>", lambda event: self._double_click(event))
        # the triangle and ←/→ open and close a group by Tk's own bindings; keep up with them
        for sequence in ("<<TreeviewOpen>>", "<<TreeviewClose>>"):
            self.games.bind(sequence, lambda _e: self.after_idle(self.groups_toggled))
        self.game_menus = RowMenus(self.games, self.game_menu)
        self.refresh_toggle()

    def _bind(self, tree: ttk.Treeview, toggle: Callable[[str], None]) -> None:
        def click(event: tk.Event) -> None:
            iid = tree.identify_row(event.y)
            region = tree.identify_region(event.x, event.y)
            if not iid or region != "cell":  # the triangle ("tree") opens a group by itself
                return
            if iid.startswith(GROUP_IID):
                self.toggle_group(iid)
            elif tree.identify_column(event.x) == "#1":
                toggle(iid)

        tree.bind("<ButtonRelease-1>", click)
        tree.bind("<space>", lambda _event: self.space_toggle())

    def space_toggle(self) -> str:
        """Space ticks the selected games that can be seen: a selection made with Shift may
        reach into a folded group (D54)."""
        shown = set(self.visible())
        for iid in self.games.selection():
            if iid in shown:
                self.toggle_game(iid)
        return "break"

    def _double_click(self, event: tk.Event) -> str | None:
        iid = self.games.identify_row(event.y)
        if iid.startswith(GROUP_IID):
            return "break"  # each click already opened or closed it
        if iid in self.games_by_iid and self.games.identify_column(event.x) != "#1":
            self.details()
        return None  # nothing for 「无匹配」 or the empty space below the rows

    # ------------------------------------------------------------------------------ content

    def show_guidance(self, text: str | None) -> None:
        """The banner above the results: first steps, or what is ticked after a scan (D29)."""
        if not text:
            self.guide.pack_forget()
            return
        self.guide_var.set(text)
        if not self.guide.winfo_manager():
            self.guide.pack(anchor="w", fill="x", pady=(0, 6), before=self.summary_label)

    def load(self, report: ScanReport) -> None:
        self.games_by_iid = {f"g{index}": game for index, game in enumerate(report.games)}
        self.show_rows()
        self.summary.set(_("扫描结果：{v}", v=state.scan_summary(report)))
        if report.skipped or report.timing.get("slowest"):
            self.skipped_link.configure(text=_("查看跳过的目录（{n}）", n=len(report.skipped)))
            self.skipped_link.pack(side="left", padx=(PAD, 0))
        else:
            self.skipped_link.pack_forget()
        link = state.unlinked_link_text(len(report.unlinked))
        if link:
            self.unlinked_link.configure(text=link)
            self.unlinked_link.pack(side="right", padx=(PAD, 0))
        else:
            self.unlinked_link.pack_forget()
        self.update_counts()

    def show_rows(self) -> None:
        """The games in their groups (D54), those that match the search box (D46); a group
        with no game is left out, and every group is open while the search box has text.
        Ticks are not touched."""
        tree = self.games
        tree.delete(*tree.get_children())
        query = self.filter.query
        searching = bool(query.strip())
        changes = self.app.changes
        members: dict[str, list[str]] = {group: [] for group in state.SCAN_GROUPS}
        for iid, game in self.games_by_iid.items():
            members[state.game_group(game, changes)].append(iid)
        self.members = members
        for group in state.SCAN_GROUPS:
            if not members[group]:
                continue
            shown = [
                iid
                for iid in members[group]
                if state.matches(query, state.game_search_fields(self.games_by_iid[iid]))
            ]
            gid = f"{GROUP_IID}{group}"
            values = ("", "", self.title_of(group, len(shown)), "", "", "", "", "")
            is_open = searching or self.group_open[group]
            tree.insert("", "end", iid=gid, values=values, tags=("group",), open=is_open)
            for iid in shown:
                game = self.games_by_iid[iid]
                change = changes.get(state.game_key(game))
                row = state.game_row(game, self.app.selection, change, self.app.library_stale)
                tree.insert(gid, "end", iid=iid, values=row.values(), tags=(row.status,))
            if searching and not shown:
                values = ("", "", f"  {_(state.NO_MATCH)}", "", "", "", "", "")
                tree.insert(gid, "end", iid=f"empty-{group}", values=values, tags=("empty",))
        self.update_counts()

    def filtered(self) -> list[str]:
        """The games the search box shows, in an open group or a closed one."""
        tree = self.games
        return [
            iid
            for group in tree.get_children()
            for iid in tree.get_children(group)
            if iid in self.games_by_iid
        ]

    def visible(self) -> list[str]:
        """The games that can be seen: shown by the search box, in an open group (D54 ④)."""
        tree = self.games
        return [
            iid
            for group in tree.get_children()
            if tree.item(group, "open")
            for iid in tree.get_children(group)
            if iid in self.games_by_iid
        ]

    def group_iid(self, group: str) -> str:
        return f"{GROUP_IID}{group}"

    def is_open(self, group: str) -> bool:
        iid = self.group_iid(group)
        return self.games.exists(iid) and bool(self.games.item(iid, "open"))

    def set_group_open(self, group: str, is_open: bool) -> None:
        """Open or close a group; remembered for this window unless a search opened every
        group for the time being (D54 ②)."""
        iid = self.group_iid(group)
        if self.games.exists(iid):
            self.games.item(iid, open=is_open)
        if not self.filter.query.strip():
            self.group_open[group] = is_open
        self.update_counts()

    def toggle_group(self, iid: str) -> None:
        group = iid.removeprefix(GROUP_IID)
        if group in self.group_open:
            self.set_group_open(group, not self.is_open(group))

    def groups_toggled(self) -> None:
        """After Tk opened or closed a group itself (its triangle, ←/→)."""
        if not self.winfo_exists():
            return
        for group in state.SCAN_GROUPS:
            if self.games.exists(self.group_iid(group)) and not self.filter.query.strip():
                self.group_open[group] = self.is_open(group)
        self.update_counts()

    def refresh_checks(self) -> None:
        shown = set(self.filtered())
        for iid, game in self.games_by_iid.items():
            if iid in shown:
                self.games.set(iid, "check", state.game_row(game, self.app.selection).check)
        self.update_counts()

    def title_of(self, group: str, shown: int) -> str:
        """A group's title row: its count, its ticked games while it is closed (D61 ②), the
        engines of the unsupported group (D61 ⑤)."""
        iids = self.members.get(group, [])
        ticked = self.app.selection.games
        folded = 0
        if not self.is_open(group):
            folded = sum(1 for i in iids if ticked.get(state.game_key(self.games_by_iid[i])))
        engines = (
            [self.games_by_iid[i].engine for i in iids] if group == state.GROUP_UNSUPPORTED else []
        )
        return state.group_title(
            group, shown, len(iids), self.filter.query, folded_ticks=folded, engines=engines
        )

    def refresh_titles(self) -> None:
        tree = self.games
        for group in state.SCAN_GROUPS:
            gid = self.group_iid(group)
            if tree.exists(gid):
                shown = sum(1 for i in tree.get_children(gid) if i in self.games_by_iid)
                tree.set(gid, "name", self.title_of(group, shown))

    def update_counts(self) -> None:
        games, unlinked = self.app.selection.counts()
        shown = set(self.filtered())  # a folded group hides nothing from the count (D54 ④)
        visible = set(self.visible())
        hidden = folded = 0
        for iid, game in self.games_by_iid.items():
            if not self.app.selection.games.get(state.game_key(game)):
                continue
            if iid not in shown:
                hidden += 1
            elif iid not in visible:
                folded += 1  # D61 ②: ticked, in a closed group
        self.counts.set(state.checked_text(games, unlinked, hidden, folded))
        self.refresh_titles()
        self.refresh_toggle()
        self.app.refresh_guidance()  # the banner above the table says the same and what's next

    def shown_keys(self) -> set[str]:
        """The games the search box shows: what the tick button works on (D46)."""
        return {state.game_key(self.games_by_iid[iid]) for iid in self.visible()}

    def refresh_toggle(self) -> None:
        selection, shown = self.app.selection, self.shown_keys()
        self.toggle_button.configure(text=state.toggle_all_text(selection.all_checked(shown)))
        enabled = bool(selection.tickable(shown))
        self.toggle_button.state(["!disabled"] if enabled else ["disabled"])

    # ------------------------------------------------------------------------------ actions

    def toggle_game(self, iid: str) -> None:
        game = self.games_by_iid.get(iid)
        if game is not None:
            self.app.selection.toggle_game(state.game_key(game))
            self.refresh_checks()

    def toggle_all(self) -> None:
        """「全选」 ticks every game that can be ticked; once every game is ticked the same
        button reads 「全不选」 and clears them (D33 ③, D42). While the search box hides some
        games it works on the games shown; hidden ticks stay as they are (D46)."""
        self.app.selection.toggle_all(self.shown_keys())
        self.refresh_checks()

    def selected_game(self) -> DetectedGame | None:
        chosen = self.games.selection()
        return self.games_by_iid.get(chosen[0]) if chosen else None

    def details(self) -> None:
        game = self.selected_game()
        if game is None:
            messagebox.showinfo(_("查看详情"), _("先在游戏表中选中一行。"), parent=self.app.root)
            return
        game_details_dialog(self.app, game)

    def show_unlinked(self) -> UnlinkedDialog | None:
        """The orphaned global save folders of the last scan, in their own dialog (D42)."""
        report = self.app.report
        if report is None or not report.unlinked:
            return None
        return UnlinkedDialog(self.app, report)

    def show_skipped(self) -> TextDialog | None:
        """Every folder the last scan skipped, by reason, and the slowest ones (D34)."""
        report = self.app.report
        if report is None:
            return None
        return TextDialog(self.app, _("跳过的目录"), state.skipped_detail_lines(report))

    # -------------------------------------------------------------------------- menus (D36)

    def game_menu(self, iid: str) -> tk.Menu | None:
        """Its folders (D36), and 「确认 / 关联…」 first for a game EngineSave can back up."""
        game = self.games_by_iid.get(iid)
        if game is None:
            return None
        menu = folder_menu(self, self.app, state.game_folders(game))
        if game.status is not GameStatus.UNSUPPORTED:
            menu.insert_command(
                0, label=_(state.LINK_TITLE) + "…", command=lambda: self.app.confirm_link(game)
            )
            menu.insert_separator(1)
        return menu
