"""Tab 2 「备份库」: records, their snapshots, verify, restore, export, import (a zip or a
folder) and cleanup.

One action, one button (D33): verify, export and cleanup work on the ticked records, or on the
selected record when none is ticked, and are unavailable when there is neither; restore works
on the selected record. One button ticks every record or none. A right click on a record opens
or copies its folders (D36); a double click opens its folder in the library.

Since 0.7.0 the records are in two groups, 「本体存在」 (an install folder is on disk) and
「本体不在」 (D43, D51), and a search box filters both (D46); ticks are kept for hidden records too.
"""

from __future__ import annotations

import contextlib
import logging
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Any

from enginesave.errors import EngineSaveError
from enginesave.fsutil import path_key
from enginesave.gui import state
from enginesave.gui.i18n import _
from enginesave.gui.state import CHECKED, UNCHECKED
from enginesave.gui.views.dialogs import (
    LINK_COLOR,
    PAD,
    CleanupDialog,
    ExportDialog,
    ImportDialog,
    MirrorDialog,
    RemoveDialog,
    RestoreDialog,
    TmpDialog,
)
from enginesave.gui.views.filter_box import FilterBox
from enginesave.gui.views.menus import RowMenus, folder_menu
from enginesave.gui.views.scan_view import make_tree
from enginesave.repo import Repo

if TYPE_CHECKING:
    from enginesave.gui.app import App
    from enginesave.migrate import MigrationPlan
    from enginesave.removal import LibraryStats
    from enginesave.verify import SnapshotCheck

log = logging.getLogger("enginesave.gui")
# repo, rows, leftovers of interrupted runs, orphaned record folders, overview (D56)
Loaded = tuple[
    Repo, list[state.RecordRow], list[str], list[str], "LibraryStats", "MigrationPlan | None"
]

RECORD_COLUMNS = [
    ("check", "勾选", 46),
    ("name", "名称", 210),
    ("kind", "类型", 100),
    ("count", "快照数", 60),
    ("latest", "最近快照", 150),
]
SNAPSHOT_COLUMNS = [
    ("id", "快照 ID", 150),
    ("time", "时间（本地）", 140),
    ("reason", "原因", 80),
    ("status", "状态", 80),
    ("files", "文件数", 60),
    ("size", "大小", 80),
    ("storage", "存储", 80),
]
VERIFY_COLUMNS = [
    ("result", "结果", 80),
    ("name", "名称", 220),
    ("snapshot", "快照", 170),
    ("ok", "一致", 60),
    ("mismatched", "不一致", 70),
    ("missing", "缺失", 60),
    ("extra", "多余", 60),
    ("detail", "说明", 320),
]
NO_LIBRARY = "未选择备份库：在上方“备份库”一栏选择已有的备份库，或新建一个。"
NO_TARGET = "先勾选记录，或在左侧选中一条记录。"
HINT = (
    "校验、导出、清理、删除作用于勾选的记录，没有勾选时作用于选中的记录（校验时选中了快照则只校验它）；"
    "恢复作用于选中的记录。右键记录可打开或复制它的文件夹，双击打开它在备份库里的文件夹。"
)


class LibraryView(ttk.Frame):
    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master, padding=PAD)
        self.app = app
        self.repo: Repo | None = None
        self.records: dict[str, state.RecordRow] = {}  # every record, shown or not
        self.checked: set[str] = set()  # ticked game ids: what the actions work on (D33 ②)
        # 1.0.0 (D70 5): the last verification's results, shown again after a language switch
        self.last_verified: tuple[list[SnapshotCheck], bool] | None = None
        self.snapshots: dict[str, str] = {}
        self.status = tk.StringVar(value=_(NO_LIBRARY))
        top = ttk.Frame(self)
        top.pack(anchor="w", fill="x")
        ttk.Label(top, textvariable=self.status).pack(side="left")
        # D55: the leftovers of interrupted runs, one click away (hidden when there are none)
        link_font = tkfont.nametofont("TkDefaultFont").copy()
        link_font.configure(underline=True)
        self.tmp_link = tk.Label(
            top, text="", foreground=LINK_COLOR, cursor="hand2", font=link_font
        )
        self.tmp_link.bind("<Button-1>", lambda _e: self.clean_tmp())
        self.overview = tk.StringVar(value="")  # D56
        ttk.Label(self, textvariable=self.overview).pack(anchor="w")

        # Bottom parts first, so a short window shrinks the tables instead of hiding them.
        results = ttk.LabelFrame(self, text=_("校验结果"), padding=4)
        results.pack(side="bottom", fill="x", pady=(PAD, 0))
        self.verify_tree = make_tree(results, VERIFY_COLUMNS, height=4)
        self.verify_tree.tag_configure("problem", foreground="#b3261e")
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", pady=(PAD, 0))

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(4, 0))
        left = ttk.LabelFrame(panes, text=_("记录"), padding=4)
        ticks = ttk.Frame(left)
        ticks.pack(fill="x", pady=(0, 4))
        self.toggle_button = ttk.Button(ticks, text=_("全选"), command=self.toggle_all)
        self.toggle_button.pack(side="left")
        self.check_count = tk.StringVar(value="")
        ttk.Label(ticks, textvariable=self.check_count).pack(side="left", padx=PAD)
        self.filter = FilterBox(left, lambda _query: self.show_rows())  # D46
        self.filter.pack(anchor="w", pady=(0, 4))
        self.record_tree = make_tree(left, RECORD_COLUMNS, height=8)
        font = tkfont.nametofont("TkDefaultFont")
        fit = max(font.measure(_(kind)) for kind in state.KIND_TEXTS.values()) + 16
        self.record_tree.column("kind", width=max(100, fit), minwidth=fit)  # D47: never cut off
        group_font = tkfont.nametofont("TkDefaultFont").copy()
        group_font.configure(weight="bold")
        self.record_tree.tag_configure("group", font=group_font)
        self.record_tree.tag_configure("empty", foreground="#8a8a8a")
        self.record_tree.configure(selectmode="browse")
        self.record_tree.bind("<ButtonRelease-1>", self.click_check)
        self.record_tree.bind("<space>", self.space_check)
        self.record_tree.bind("<Double-1>", self.double_click)
        self.record_menus = RowMenus(self.record_tree, self.record_menu)
        panes.add(left, weight=2)
        right = ttk.LabelFrame(panes, text=_("快照（最新在上）"), padding=4)
        self.snapshot_tree = make_tree(right, SNAPSHOT_COLUMNS, height=8)
        self.snapshot_tree.configure(selectmode="browse")
        panes.add(right, weight=3)
        self.record_tree.bind("<<TreeviewSelect>>", lambda _e: self.record_selected())

        writes = self.writable  # D67 8: not in a library of format 1 (read-only)
        for text, command, when in (
            (_("刷新"), self.reload, self.has_library),
            (_("校验"), self.verify, self.has_targets),
            (_("恢复…"), self.restore, lambda: self.selected_record() is not None and writes()),
            (_("导出…"), self.export, self.has_targets),
            (_("导入…"), self.import_archives, lambda: self.has_library() and writes()),
            (_("清理…"), self.cleanup, lambda: self.has_targets() and writes()),
            (_("删除…"), self.remove, lambda: self.has_targets() and writes()),  # 0.8.0 (D49)
            (_("镜像到…"), self.mirror, self.has_library),  # 0.9.0 (D57)
        ):
            button = ttk.Button(bar, text=text, command=command)
            button.pack(side="left", padx=(0, PAD))
            app.register_action(button, when)
        ttk.Label(self, text=_(HINT), wraplength=1100).pack(side="bottom", anchor="w", before=bar)
        self.refresh_checks()

    # ------------------------------------------------------------------------------ loading

    def clear(self, message: str) -> None:
        """No library shown; the search box keeps its text (D46)."""
        self.repo = None
        self.records = {}
        self.snapshots = {}
        self.record_tree.delete(*self.record_tree.get_children())
        self.snapshot_tree.delete(*self.snapshot_tree.get_children())
        self.refresh_checks()
        self.status.set(message)
        self.overview.set("")
        self.tmp_link.pack_forget()

    def reload(self, then: Callable[[], None] | None = None) -> None:
        """Read the library again; ``then`` runs once it has loaded (not when it failed)."""
        path = self.app.repo_path()
        if path is None:
            self.clear(_(NO_LIBRARY))
            return

        config = self.app.settings_file.parent

        def job(_progress: Callable[[Any], None]) -> Loaded:
            from enginesave.mirror import last_mirror
            from enginesave.removal import library_stats

            repo = Repo.open(path)
            rows = state.record_rows(repo, repo.load_catalog())
            stale = [str(p) for p in repo.stale_tmp_dirs()]
            mirrored = last_mirror(config, repo.root)
            stats = library_stats(repo, last_mirror=mirrored["at"] if mirrored else None)
            left = None
            if not repo.old_format:  # D70 1: a migration that did not finish
                from enginesave.migrate import plan_migration

                with contextlib.suppress(EngineSaveError, OSError):
                    left = plan_migration(repo)
            orphans = [str(p) for p in repo.orphan_game_dirs()]
            return repo, rows, stale, orphans, stats, left

        def done(value: Loaded) -> None:
            self.loaded(value)
            if then is not None:
                then()

        self.app.run(
            _("读取备份库"), job, done, on_error=self.load_failed, status=_("正在读取备份库…")
        )

    def loaded(self, value: Loaded) -> None:
        repo, rows, stale, orphans, stats, left = value
        same = self.repo is not None and path_key(self.repo.root) == path_key(repo.root)
        previous = self.selected_record() if same else None
        self.clear("")
        self.repo = repo
        # Ticks survive a reload of the same library; another library starts with none (its
        # records may share ids with these, e.g. after an import).
        self.checked = {row.game_id for row in rows} & self.checked if same else set()
        self.records = {f"r{index}": row for index, row in enumerate(rows)}
        self.show_rows(previous.game_id if previous is not None else None)
        total = sum(r.snapshots for r in rows)
        text = _(
            "备份库：{root}    {n} 条记录，{total} 个快照", root=repo.root, n=len(rows), total=total
        )
        if not rows:
            text += _("（空的备份库：在“扫描与备份”页备份后会出现在这里）")
        if stale:  # D55: the link opens 「清理临时文件夹」
            self.tmp_link.configure(text=state.tmp_link_text(len(stale)))
            self.tmp_link.pack(side="left", padx=(PAD, 0))
            for path in stale:
                log.warning(_("备份库里有中断留下的临时文件夹：%s"), path)
        self.overview.set(state.overview_text(stats))
        if stats.unreadable:
            log.warning(_("概览：%d 个快照的清单读不出，大小未计入"), stats.unreadable)
        if orphans:  # D49: an interrupted removal
            text += _("    注意：有 {n} 个不在目录里的记录文件夹（见日志）", n=len(orphans))
            for path in orphans:
                log.warning(_("备份库里有不在目录（catalog.json）里的记录文件夹：%s"), path)
        if repo.old_format:  # D67 8: read-only until it is migrated
            text += f"    {_(state.READ_ONLY)}"
        self.status.set(text)
        self.app.refresh_actions()
        if repo.old_format:
            self.app.offer_migration(repo, again=False)
        elif left is not None and left.folders_needed and not left.refusals:
            self.app.offer_migration(repo, again=False)  # D70 1: renames left to do
        elif left is not None and left.readmes_missing:
            self.root_after(lambda: self.write_readmes(repo.root))

    def root_after(self, action: Callable[[], None]) -> None:
        """``action`` once the job that read the library has ended."""
        self.after_idle(action)

    def write_readmes(self, root: Path) -> None:
        """D70 1: a migrated library whose snapshots lack their README.txt (a migration that
        stopped in its last step) gets them in the background; one log line says so."""
        if self.app.runner.busy:
            return  # the next reading of the library tries again

        def job(_progress: Callable[[Any], None]) -> Any:
            from enginesave.migrate import write_missing_readmes

            return write_missing_readmes(Repo.open(root), should_stop=self.app.stop_requested)

        def done(result: Any) -> None:
            if result.readmes:
                log.info(_("已为 %d 个快照补写说明文件 README.txt"), result.readmes)
            for note in result.notes[:5]:
                log.warning(note)

        self.app.run(_("补写说明文件"), job, done, status=_("正在补写说明文件…"))

    def show_rows(self, select: str | None = None) -> None:
        """The records in their two groups, those matching the search box (D43, D46); the
        selection is kept when its record is still shown."""
        tree = self.record_tree
        if select is None and (current := self.selected_record()) is not None:
            select = current.game_id
        tree.delete(*tree.get_children())
        query = self.filter.query
        by_id = {row.game_id: iid for iid, row in self.records.items()}
        groups = state.record_groups(list(self.records.values()), query)
        totals = state.record_groups(list(self.records.values()))
        for index, ((name, rows), (_name, every)) in enumerate(zip(groups, totals, strict=True)):
            group = f"group{index}"
            label = state.group_label(name, len(rows), len(every), query)
            tree.insert("", "end", iid=group, values=("", label, "", "", ""), tags=("group",))
            tree.item(group, open=True)
            for row in rows:
                iid = by_id[row.game_id]
                tree.insert(group, "end", iid=iid, values=(UNCHECKED, *row.values()))
                if row.game_id == select:
                    tree.selection_set(iid)  # so does the selection: no click to redo
                    tree.see(iid)
            if not rows and query.strip():
                values = ("", f"  {_(state.NO_MATCH)}", "", "", "")
                tree.insert(group, "end", iid=f"empty{index}", values=values, tags=("empty",))
        self.refresh_checks()

    def visible(self) -> set[str]:
        """The record rows shown now (the search box may hide some)."""
        tree = self.record_tree
        return {c for group in tree.get_children() for c in tree.get_children(group)}

    def load_failed(self, exc: BaseException) -> None:
        self.clear(_("备份库无法打开：{exc}", exc=exc))
        self.app.show_error(exc, title=_("备份库无法打开"))

    def record_selected(self) -> None:
        self.snapshot_tree.delete(*self.snapshot_tree.get_children())
        self.snapshots = {}
        self.app.refresh_actions()
        row = self.selected_record()
        if row is None or self.repo is None:
            return
        try:
            snapshot_rows = state.snapshot_rows(self.repo, row.game_id)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.app.show_error(exc)
            return
        for index, snapshot in enumerate(snapshot_rows):
            iid = f"s{index}"
            self.snapshots[iid] = snapshot.snapshot_id
            self.snapshot_tree.insert("", "end", iid=iid, values=snapshot.values())

    # ------------------------------------------------------------------------------ ticks

    def click_check(self, event: tk.Event) -> None:
        tree = self.record_tree
        if (
            tree.identify_region(event.x, event.y) == "cell"
            and tree.identify_column(event.x) == "#1"
        ):
            iid = tree.identify_row(event.y)
            if iid:
                self.toggle_check(iid)

    def space_check(self, _event: tk.Event) -> str:
        for iid in self.record_tree.selection():
            self.toggle_check(iid)
        return "break"

    def toggle_check(self, iid: str) -> None:
        row = self.records.get(iid)
        if row is not None:
            self.checked ^= {row.game_id}
            self.refresh_checks()

    def shown_ids(self) -> set[str]:
        """The records the search box shows: what the tick button works on (D46)."""
        return {self.records[iid].game_id for iid in self.visible() if iid in self.records}

    def all_checked(self) -> bool:
        shown = self.shown_ids()
        return bool(shown) and shown <= self.checked

    def toggle_all(self) -> None:
        """「全选」 ticks every record; once all are ticked the same button reads 「全不选」
        and clears them (D33 ③). While the search box hides some records it works on the
        records shown; hidden ticks stay as they are (D46)."""
        if self.all_checked():
            self.checked -= self.shown_ids()
        else:
            self.checked |= self.shown_ids()
        self.refresh_checks()

    def refresh_checks(self) -> None:
        shown = self.visible()
        for iid, row in self.records.items():
            if iid in shown:
                mark = CHECKED if row.game_id in self.checked else UNCHECKED
                self.record_tree.set(iid, "check", mark)
        hidden = sum(
            1 for iid, r in self.records.items() if iid not in shown and r.game_id in self.checked
        )
        self.check_count.set(
            state.check_count_text(len(self.checked), hidden) if self.records else ""
        )
        self.toggle_button.configure(text=state.toggle_all_text(self.all_checked()))
        self.toggle_button.state(["!disabled"] if self.shown_ids() else ["disabled"])
        self.app.refresh_actions()

    def checked_ids(self) -> list[str]:
        """Ticked records in the order they are listed."""
        return [row.game_id for row in self.records.values() if row.game_id in self.checked]

    def targets(self) -> list[str]:
        """What verify, export and cleanup work on: the ticked records, else the selected one."""
        if self.repo is None:
            return []
        row = self.selected_record()
        return state.action_targets(self.checked_ids(), row.game_id if row else None)

    def has_targets(self) -> bool:
        return bool(self.targets())

    def has_library(self) -> bool:
        return self.app.repo_path() is not None

    def writable(self) -> bool:
        """D67 8: the library shown is not of format 1 (one not read yet counts as writable;
        the library layer refuses a write into an old one anyway)."""
        return self.repo is None or not self.repo.old_format

    def selected_record(self) -> state.RecordRow | None:
        chosen = self.record_tree.selection()
        return self.records.get(chosen[0]) if chosen else None

    def selected_snapshot(self) -> str | None:
        chosen = self.snapshot_tree.selection()
        return self.snapshots.get(chosen[0]) if chosen else None

    def snapshot_for(self, targets: list[str]) -> str | None:
        """The snapshot picked on the right counts when the action is about that one record."""
        row = self.selected_record()
        return self.selected_snapshot() if row is not None and targets == [row.game_id] else None

    def names(self, game_ids: list[str]) -> str:
        by_id = {row.game_id: row.name for row in self.records.values()}
        return _("、").join(by_id.get(gid, gid) for gid in game_ids)

    # ------------------------------------------------------------------------------ actions

    def verify(self) -> None:
        targets = self.targets()
        if self.repo is None or not targets:
            self.explain(_("校验"))
            return
        repo = self.repo
        snapshot_id = self.snapshot_for(targets)
        if snapshot_id:
            scope = _("{v} 的快照 {snapshot_id}", v=self.names(targets), snapshot_id=snapshot_id)
        elif len(targets) == 1:
            scope = _("{v} 的全部快照", v=self.names(targets))
        else:
            scope = _("{n} 条记录的全部快照", n=len(targets))

        stop = self.app.stop_requested  # D58: 「取消」, between snapshots

        def job(_progress: Callable[[Any], None]) -> tuple[list[SnapshotCheck], bool]:
            from enginesave.errors import Stopped
            from enginesave.verify import verify_repo

            catalog = repo.load_catalog()
            checks: list[SnapshotCheck] = []
            for game_id in targets:
                try:
                    checks += verify_repo(repo, catalog, game_id, snapshot_id, should_stop=stop)
                except Stopped as exc:
                    return checks + list(exc.partial or []), True
            return checks, False

        self.app.run(
            _("校验"),
            job,
            lambda value: self.verified(scope, *value),
            status=_("正在校验{scope}…", scope=scope),
            cancellable=True,
        )

    def verified(self, scope: str, checks: list[SnapshotCheck], cancelled: bool = False) -> None:
        self.show_verified(checks, cancelled)
        bad = [c for c in checks if not c.clean]
        if cancelled:
            log.info(_("已取消校验%s：校验了 %d 个快照，%d 个有问题"), scope, len(checks), len(bad))
        else:
            log.info(_("校验%s：%d 个快照，%d 个有问题"), scope, len(checks), len(bad))
        self.log_problems(bad)

    def show_verified(self, checks: list[SnapshotCheck], cancelled: bool = False) -> None:
        """The verification results table (in the window's language)."""
        self.last_verified = (list(checks), cancelled)
        self.verify_tree.delete(*self.verify_tree.get_children())
        for check in checks:
            details = [*check.mismatched, *check.missing, *check.extra, *check.problems]
            if check.snapshot_status == "partial":
                details.insert(0, _("部分完成（partial）的快照：备份时有文件读不出或被改动"))
            self.verify_tree.insert(
                "",
                "end",
                values=(
                    "OK" if check.clean else "PROBLEM",
                    check.display_name,
                    check.snapshot_id,
                    check.ok,
                    len(check.mismatched),
                    len(check.missing),
                    len(check.extra),
                    _("；").join(details[:10]),
                ),
                tags=() if check.clean else ("problem",),
            )
        if cancelled:  # D58: the table says it is not the whole library
            self.verify_tree.insert(
                "", "end", values=(_(state.STOPPED), *[""] * 6, _(state.NOT_VERIFIED))
            )

    def log_problems(self, bad: list[SnapshotCheck]) -> None:
        for check in bad:
            log.warning(
                _("校验发现问题：%s %s mismatched=%s missing=%s extra=%s %s"),
                check.display_name,
                check.snapshot_id,
                check.mismatched,
                check.missing,
                check.extra,
                check.problems,
            )
        if bad:
            messagebox.showwarning(
                _("校验发现问题"),
                _("{n} 个快照与清单不一致，详见校验结果与日志。", n=len(bad)),
                parent=self.app.root,
            )

    def restore(self) -> None:
        row = self.selected_record()
        if self.repo is None or row is None:
            messagebox.showinfo(_("恢复"), _("先在左侧选中一条记录。"), parent=self.app.root)
            return
        try:
            RestoreDialog(self.app, self.repo, row.game_id, self.selected_snapshot())
        except EngineSaveError as exc:
            self.app.show_error(exc)

    def export(self) -> None:
        """「导出…」 (D33 ⑦): one record, its snapshot to a file; several, each latest snapshot
        into one folder."""
        targets = self.targets()
        if self.repo is None or not targets:
            self.explain(_("导出"))
            return
        try:
            ExportDialog(self.app, self.repo, targets, self.snapshot_for(targets))
        except EngineSaveError as exc:
            self.app.show_error(exc)

    def import_archives(self) -> None:
        path = self.app.repo_path()
        if path is None:
            messagebox.showinfo(_("导入"), _(NO_LIBRARY), parent=self.app.root)
            return
        ImportDialog(self.app, path)

    def cleanup(self) -> None:
        targets = self.targets()
        if self.repo is None or not targets:
            self.explain(_("清理"))
            return
        try:
            CleanupDialog(self.app, self.repo, targets)
        except EngineSaveError as exc:
            self.app.show_error(exc)

    def remove(self) -> None:
        """「删除…」 (D49): the ticked records, else the selected one; with no tick and a
        snapshot selected, only that snapshot starts ticked."""
        targets = self.targets()
        if self.repo is None or not targets:
            self.explain(_("删除"))
            return
        only = self.selected_snapshot() if not self.checked_ids() else None
        names = {row.game_id: row.name for row in self.records.values()}
        try:
            RemoveDialog(self.app, self.repo, targets, names, only)
        except EngineSaveError as exc:
            self.app.show_error(exc)

    def clean_tmp(self) -> None:
        """「清理临时文件夹（N）…」 (D55): not while this window is backing up or restoring."""
        if self.repo is None:
            return
        if self.app.runner.busy:
            messagebox.showinfo(
                _(state.TMP_TITLE),
                _(
                    "“{name}”还在进行，它可能正在写临时文件夹；结束后再清理。",
                    name=self.app.runner.name,
                ),
                parent=self.app.root,
            )
            return
        TmpDialog(self.app, self.repo)

    def mirror(self) -> None:
        """「镜像到…」 (D57): the last mirror's folder is offered first."""
        if self.repo is None:
            self.explain(_("镜像"))
            return
        last = self.app.settings.mirror_folder
        MirrorDialog(self.app, self.repo, Path(last) if last else None)

    def removed(self) -> None:
        """After a removal: no ticks (the records may be gone), the library read again and
        the scan page's games compared with what is left (D54)."""
        self.checked = set()
        self.app.library_changed(compare=True)

    def explain(self, action: str) -> None:
        """Why an action has nothing to work on (its button is greyed for the same reason)."""
        text = _(NO_LIBRARY) if self.repo is None else _(NO_TARGET)
        messagebox.showinfo(action, text, parent=self.app.root)

    # -------------------------------------------------------------------------- menus (D36)

    def folders(self, iid: str) -> list[state.Folder]:
        row = self.records.get(iid)
        if row is None or self.repo is None:
            return []
        try:
            doc = self.repo.load_game(row.game_id)
        except EngineSaveError as exc:
            log.warning(_("读不出记录 %s：%s"), row.game_id, exc)
            return [state.folder(_("备份记录文件夹"), self.repo.game_dir(row.game_id))]
        return state.record_folders(
            self.repo, doc, saves_root_override=self.app.saves_override(), env=self.app.env
        )

    def record_menu(self, iid: str) -> tk.Menu | None:
        folders = self.folders(iid)
        if not folders:
            return None
        menu = folder_menu(self, self.app, folders)
        menu.add_separator()  # D67 1: the folder follows the name only when asked
        menu.add_command(
            label=f"{_(state.RENAME_TITLE)}…",
            command=lambda: self.rename_folder(iid),
            state="normal" if self.writable() and not self.app.runner.busy else "disabled",
        )
        return menu

    def rename_folder(self, iid: str) -> None:
        """「按当前名字重命名文件夹…」 (D67 1): the record's folder after its name now; asked
        once with the old and the new name."""
        row, repo = self.records.get(iid), self.repo
        if row is None or repo is None:
            return
        try:
            old, new = repo.folder_rename(row.game_id)
        except EngineSaveError as exc:
            self.app.show_error(exc)
            return
        if old == new:
            messagebox.showinfo(
                _(state.RENAME_TITLE),
                _("文件夹已经是 games\\{old}。", old=old),
                parent=self.app.root,
            )
            return
        if not messagebox.askyesno(
            _(state.RENAME_TITLE), state.rename_question(old, new), parent=self.app.root
        ):
            return

        def done(names: tuple[str, str]) -> None:
            log.info(_("记录文件夹已改名：games\\%s → games\\%s"), *names)
            self.reload()

        self.app.run(
            _("重命名记录文件夹"),
            lambda _p: repo.rename_folder(row.game_id),
            done,
            status=_("正在重命名记录文件夹…"),
        )

    def double_click(self, event: tk.Event) -> None:
        """Open the record's folder in the library: the menu's first entry (D36)."""
        tree = self.record_tree
        iid = tree.identify_row(event.y)
        if iid and tree.identify_column(event.x) != "#1":
            self.open_record_folder(iid)

    def open_record_folder(self, iid: str) -> None:
        row = self.records.get(iid)
        if row is not None and self.repo is not None:
            self.app.open_folder(self.repo.game_dir(row.game_id))
