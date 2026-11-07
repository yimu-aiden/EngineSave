"""The cleanup dialog: the folders that can go, the read-only check, the recycle bin or a
permanent delete (D26, D35, D40); and the dialog that makes room when the recycle bin is full
(D41)."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from enginesave.errors import EngineSaveError
from enginesave.fsutil import path_key
from enginesave.gui import state
from enginesave.gui.app import set_enabled
from enginesave.gui.i18n import _
from enginesave.gui.views.dialogs.base import (
    LINK_COLUMN,
    OPEN_LINK,
    PAD,
    WARNING_COLOR,
    Dialog,
    log,
    scrolled_tree,
)
from enginesave.repo import Repo

if TYPE_CHECKING:
    from enginesave.cleanup import CleanupPlan, CleanupResult, CleanupTarget, RecycleBin
    from enginesave.cleanup.batch import Waiting
    from enginesave.cleanup.recycle_bin import OurItem
    from enginesave.gui.app import App

RECYCLE, PERMANENT = state.RECYCLE, state.PERMANENT
Outcome = tuple["CleanupPlan", "CleanupResult | None"]


class CleanupDialog(Dialog):
    """Clean up after a verified backup (D26) with the fewest clicks (D32).

    The dialog opens with the check already running in the background (read only): every
    folder of the given records is checked by the library, the ones that pass are ticked,
    refused ones are greyed with the reason, folders already cleaned up say so (D41). The
    method is the one used last time. The main button does it in one click, for the recycle
    bin and for a permanent delete (D40, a red line says it cannot be undone). Right before
    removing anything the library checks each folder again. When the recycle bin has no room
    left the rest waits and :class:`FullBinDialog` offers to make room (D41).
    """

    def __init__(self, app: App, repo: Repo, game_ids: Sequence[str]) -> None:
        catalog = repo.load_catalog()
        records = [repo.resolve_game(catalog, gid) for gid in game_ids]  # before any window exists
        super().__init__(app, _("清理"), min_size=(1160, 560))
        self.repo = repo
        self.records = list({r["game_id"]: r for r in records}.values())
        method = app.settings.cleanup_method
        self.method = tk.StringVar(value=method if method in (RECYCLE, PERMANENT) else RECYCLE)
        self.rows: dict[str, tuple[dict[str, Any], tuple[str, str | None]]] = {}
        self.targets: dict[str, CleanupTarget] = {}
        self.plans: list[CleanupPlan] = []
        self.ticked: set[str] = set()
        self.checked = False  # the current rows went through the check
        self.generation = 0
        self.waiting_items: list[Waiting] = []  # left for later by a full recycle bin (D41)

        body = self.body
        top = ttk.Frame(body)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        names = _("、").join(state.record_label(r) for r in self.records)
        ttk.Label(top, text=_("记录：")).grid(row=0, column=0, sticky="nw")
        ttk.Label(top, text=names, wraplength=1040).grid(row=0, column=1, sticky="w")
        ttk.Label(top, text=_("方式：")).grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        methods = ttk.Frame(top)
        methods.grid(row=1, column=1, sticky="w", pady=(PAD, 0))
        self.method_buttons: dict[str, ttk.Radiobutton] = {}
        for value in (RECYCLE, PERMANENT):
            radio = ttk.Radiobutton(
                methods, text=_(state.METHOD_TEXTS[value]), value=value, variable=self.method
            )
            radio.pack(side="left", padx=(0, PAD * 2))
            self.actions.append(radio)
            self.method_buttons[value] = radio
        self.bins = tk.StringVar(value="")  # 回收站 E:\ 剩余 X / Y (D41 4)
        ttk.Label(methods, textvariable=self.bins).pack(side="left", padx=(PAD, 0))
        intro = ttk.Label(body, text=_(state.CLEANUP_INTRO), wraplength=1100)
        intro.pack(anchor="w", pady=(PAD, PAD))
        # D40: no name to type for a permanent delete; this stays in red while it is chosen.
        self.warning = tk.Label(
            body, text=_(state.PERMANENT_WARNING), foreground=WARNING_COLOR, wraplength=1100
        )
        self.intro = intro
        columns = [
            ("tick", _("勾选"), 46),
            ("record", _("记录"), 160),
            ("kind", _("类型"), 96),
            ("path", _("路径"), 330),
            LINK_COLUMN,
            ("files", _("文件数"), 60),
            ("size", _("大小"), 80),
            ("check", _("检查"), 150),
            ("note", _("说明"), 260),
        ]
        self.tree = scrolled_tree(body, columns, height=7)
        self.tree.column("tick", anchor="center")
        self.tree.tag_configure("refused", foreground="#8a8a8a")
        self.tree.tag_configure("failed", foreground="#b3261e")
        self.tree.bind("<ButtonRelease-1>", self.click)
        self.tree.bind("<space>", self.space)
        self.link_column(self.tree)
        self.result = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.result, wraplength=1100).pack(anchor="w", pady=(PAD, 0))
        self.watch(self.method, self.method_changed)
        self.button(_("关闭"), self.cancel, action=False)
        self.clean_button = self.primary(
            state.cleanup_primary_text(self.method.get(), []), self.press_main
        )
        self.button(_("重新检查"), self.check)
        self.show_warning()
        self.check()
        self.show()

    # -------------------------------------------------------------------------------- rows

    def load_rows(self) -> None:
        """Every folder the records could clean up, read again before each check."""
        self.tree.delete(*self.tree.get_children())
        self.rows, self.targets, self.ticked, self.plans = {}, {}, set(), []
        self.links = {}
        self.checked = False
        self.waiting_items = []
        index = 0
        for record in self.records:
            try:
                doc = self.repo.load_game(record["game_id"])
            except EngineSaveError as exc:
                self.app.show_error(exc, parent=self)
                continue
            for request in state.cleanup_requests(doc):
                iid = f"c{index}"
                index += 1
                self.rows[iid] = (record, request)
                kind, path = request
                if path:
                    self.links[iid] = Path(path)
                values = (
                    "",
                    str(record.get("display_name") or record["game_id"]),
                    _(state.CLEANUP_LABELS[kind]),
                    path or _("（检查后显示）"),
                    _(OPEN_LINK) if path else "",
                    "",
                    "",
                    _("检查中"),
                    "",
                )
                self.tree.insert("", "end", iid=iid, values=values)

    def groups(self, iids: Sequence[str]) -> list[tuple[dict[str, Any], list[str]]]:
        """Rows by record, in the order shown."""
        grouped: dict[str, tuple[dict[str, Any], list[str]]] = {}
        for iid in iids:
            record, _request = self.rows[iid]
            grouped.setdefault(record["game_id"], (record, []))[1].append(iid)
        return list(grouped.values())

    def plan_options(self) -> dict[str, Any]:
        """What every check and run of this dialog passes to the library (the method apart)."""
        return {
            "scan_roots": [Path(r) for r in self.app.settings.scan_roots],
            "report": self.app.report,
            "saves_root_override": self.app.saves_override(),
            "env": self.app.env,
        }

    # ------------------------------------------------------------------------------- check

    def method_changed(self) -> None:
        """The method is remembered (D32) and the check runs again: the recycle bin has its
        own preconditions."""
        self.app.settings.cleanup_method = self.method.get()
        self.app.save_settings()
        self.show_warning()
        self.check()

    def show_warning(self) -> None:
        """The red line under the introduction, while a permanent delete is chosen (D40)."""
        if self.method.get() == PERMANENT:
            if not self.warning.winfo_manager():
                self.warning.pack(anchor="w", pady=(0, PAD), after=self.intro)
        else:
            self.warning.pack_forget()

    @property
    def warning_shown(self) -> bool:
        return bool(self.warning.winfo_manager())

    def check(self) -> None:
        self.generation += 1
        self.load_rows()
        self.refresh_buttons()
        if not self.rows:
            self.result.set(_(state.NOTHING_TO_CLEAN))
            return
        repo, options, generation = self.repo, self.plan_options(), self.generation
        method = self.method.get()
        groups = [
            (record, [self.rows[iid][1] for iid in iids])
            for record, iids in self.groups(list(self.rows))
        ]

        def job(progress: Callable[[Any], None]) -> tuple[int, list[CleanupPlan]]:
            from enginesave.cleanup import RecycleLedger, plan_cleanup

            ledger = RecycleLedger()  # each bin read once for the whole check (D41)
            plans = []
            for index, (record, requests) in enumerate(groups):
                progress((index, len(groups), str(record.get("display_name") or "")))
                plans.append(
                    plan_cleanup(repo, record, requests, method=method, ledger=ledger, **options)
                )
            return generation, plans

        self.result.set(_(state.CHECKING))
        self.run_soon(
            _("清理检查"),
            job,
            self.show_plans,
            status=_(state.CHECKING),
            progress=self.check_progress,
            on_error=lambda exc: self.check_failed(generation, exc),
        )

    def check_progress(self, value: tuple[int, int, str]) -> None:
        done, total, name = value
        self.app.progress.configure(maximum=max(total, 1), value=done)
        self.app.status_var.set(
            _("正在检查 {name}（{v}/{total}，只读）…", name=name, v=done + 1, total=total)
        )

    def check_failed(self, generation: int, exc: BaseException) -> None:
        if generation == self.generation:
            self.result.set(_("检查没有完成：{exc}", exc=exc))
            log.warning(_("清理检查没有完成：%s"), exc)

    def show_plans(self, value: tuple[int, list[CleanupPlan]]) -> None:
        generation, plans = value
        if generation != self.generation:
            return  # the inputs changed meanwhile; a newer check follows
        for (_record, iids), plan in zip(self.groups(list(self.rows)), plans, strict=True):
            for iid, target in zip(iids, plan.targets, strict=True):
                self.show_target(iid, target)
        self.plans = plans
        self.checked = True
        self.ticked = {iid for iid, target in self.targets.items() if target.ok}  # D32 ②
        for iid in self.ticked:
            self.tree.set(iid, "tick", state.CHECKED)
        self.result.set(state.cleanup_check_summary(plans))
        bins: dict[str, RecycleBin] = {}
        for plan in plans:
            bins.update(plan.recycle_bins)
        self.bins.set(_("；").join(state.bin_line(b) for _d, b in sorted(bins.items())))
        self.refresh_buttons()
        log.info(state.cleanup_check_log(plans))  # one line per check (D41 3)

    def show_target(self, iid: str, target: CleanupTarget, outcome: str | None = None) -> None:
        self.targets[iid] = target
        record, _request = self.rows[iid]
        if target.path:
            self.links[iid] = Path(target.path)
        values = (
            state.UNCHECKED if target.ok and outcome is None else "",
            str(record.get("display_name") or record["game_id"]),
            _(state.CLEANUP_LABELS[target.kind]),
            target.path,
            _(OPEN_LINK) if target.path else "",
            target.files if target.active else "",
            state.size_text(target.bytes) if target.active else "",
            outcome or state.cleanup_verdict(target),
            state.cleanup_note(target),
        )
        if outcome is not None and outcome.startswith(_("失败")):
            tags: tuple[str, ...] = ("failed",)
        else:
            tags = () if target.ok else ("refused",)
        self.tree.item(iid, values=values, tags=tags)

    def refresh_buttons(self) -> None:
        if self.waiting_items:  # D41: the main button goes on with what waits for room
            self.clean_button.configure(text=state.continue_primary_text(len(self.waiting_items)))
            set_enabled(self.clean_button, not self.busy)
            return
        chosen = [self.targets[iid] for iid in self.rows if iid in self.ticked]
        self.clean_button.configure(text=state.cleanup_primary_text(self.method.get(), chosen))
        set_enabled(self.clean_button, self.checked and bool(chosen) and not self.busy)

    # ------------------------------------------------------------------------------- ticks

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
        """Only a folder that passed the current check can be ticked."""
        target = self.targets.get(iid)
        if self.busy or not self.checked or target is None or not target.ok:
            return
        self.ticked ^= {iid}
        self.tree.set(iid, "tick", state.CHECKED if iid in self.ticked else state.UNCHECKED)
        self.refresh_buttons()

    # ------------------------------------------------------------------------------- clean

    def press_main(self) -> None:
        if self.waiting_items:
            self.go_on()
        else:
            self.clean()

    def clean(self) -> None:
        if self.busy or not self.checked:
            return
        iids = [iid for iid in self.rows if iid in self.ticked]
        if not iids:
            return
        method = self.method.get()  # D40: the main button is the confirmation, either method
        snapshots = {plan.game_id: plan.snapshot_id for plan in self.plans}
        groups = [
            (record["game_id"], snapshots.get(record["game_id"]), [self.rows[i][1] for i in ids])
            for record, ids in self.groups(iids)
        ]
        repo, options = self.repo, self.plan_options()
        stop = self.app.stop_requested  # D58: 「取消」, between folders

        def job(progress: Callable[[Any], None]) -> tuple[list[Outcome], dict[str, Any]]:
            from enginesave.cleanup import RecycleLedger, batch

            ledger = RecycleLedger()  # one for the whole batch: the room adds up (D41)

            def step(done: int, total: int, path: str) -> None:
                progress((done, total, path))

            outcomes = batch.run_batch(
                repo,
                groups,
                method=method,
                options=options,
                ledger=ledger,
                progress=step,
                should_stop=stop,
            )
            return outcomes, room_to_make(repo, batch.waiting(outcomes), ledger)

        self.checked = False
        self.run(
            _("清理"),
            job,
            lambda value: self.cleaned(method, *value, chosen=iids),
            status=_("正在清理…"),
            progress=self.cleanup_progress,
            cancellable=True,
            lock=self.repo.root,  # D73 4
        )

    def cleanup_progress(self, value: tuple[int, int, str]) -> None:
        done, total, path = value
        text = _("正在清理 {path}（{v}/{total}）…", path=path, v=done + 1, total=total)
        self.app.progress.configure(maximum=max(total, 1), value=done)
        self.app.status_var.set(text)
        self.result.set(text)

    def show_outcomes(self, outcomes: Sequence[Outcome]) -> set[str]:
        """Each row's outcome in its check column; returns the rows that got one."""
        by_path = {(t.kind, path_key(t.path)): iid for iid, t in self.targets.items()}
        shown: set[str] = set()
        for plan, result in outcomes:
            if result is None:
                for target in plan.targets:
                    iid = by_path.get((target.kind, path_key(target.path)))
                    if iid is not None:
                        self.show_target(iid, target, _("失败：再次检查没有通过，未删除"))
                        shown.add(iid)
                continue
            for entry in result.targets:
                iid = by_path.get((entry["kind"], path_key(entry["path"])))
                if iid is not None:
                    self.show_target(iid, self.targets[iid], state.cleanup_outcome(entry))
                    shown.add(iid)
        return shown

    def cleaned(
        self,
        method: str,
        outcomes: list[Outcome],
        room: dict[str, Any],
        *,
        chosen: Sequence[str] = (),
    ) -> None:
        from enginesave.cleanup import batch

        self.ticked = set()
        for iid in self.rows:  # nothing can be ticked until the next check
            self.tree.set(iid, "tick", "")
        shown = self.show_outcomes(outcomes)
        if batch.cancelled(outcomes):  # D58: the folders it did not reach say so
            for iid in chosen:
                if iid not in shown and iid in self.targets:
                    self.show_target(iid, self.targets[iid], _(state.NOT_REACHED))
        self.result.set(state.cleanup_batch_summary(outcomes, method))
        log.info(state.cleanup_done_log(outcomes))  # one line per run (D41 3)
        self.waiting_items = list(room["waiting"])
        self.refresh_buttons()
        self.app.library_changed()
        if self.waiting_items:
            FullBinDialog(self, room)

    # ------------------------------------------------------------- what waits for room (D41)

    def go_on(self) -> None:
        """「继续清理」: read the bins again (the user may have emptied the recycle bin); if
        what waits fits now, go on at once, otherwise offer to make room."""
        if self.busy or not self.waiting_items:
            return
        repo, items = self.repo, list(self.waiting_items)

        def job(_progress: Callable[[Any], None]) -> dict[str, Any]:
            from enginesave.cleanup import RecycleLedger

            return room_to_make(repo, items, RecycleLedger())

        self.run(_("检查回收站"), job, self.room_known, status=_("正在读取回收站…"))

    def room_known(self, room: dict[str, Any]) -> None:
        needs = state.bin_needs(room["rooms"], waiting_rows(room["waiting"]), [])
        if any(n.missing for n in needs):
            FullBinDialog(self, room)
        else:
            self.resume([], [])

    def resume(self, purge: Sequence[OurItem], permanent: Sequence[str]) -> None:
        """Purge the chosen earlier items from the recycle bin, then clean up what waited:
        recycled, or deleted permanently where ticked (D41 6, in that order)."""
        repo, items, options = self.repo, list(self.waiting_items), self.plan_options()

        def job(
            progress: Callable[[Any], None],
        ) -> tuple[list[dict[str, Any]], list[Outcome], dict[str, Any]]:
            from enginesave.cleanup import RecycleLedger, batch, recycle_bin

            purged = recycle_bin.purge(repo, purge) if purge else []

            def step(done: int, total: int, path: str) -> None:
                progress((done, total, path))

            outcomes = batch.resume(
                repo,
                items,
                permanent=permanent,
                options=options,
                progress=step,
                should_stop=self.app.stop_requested,
            )
            left = batch.still_waiting(items, outcomes)  # a stop keeps what it did not reach
            return purged, outcomes, room_to_make(repo, left, RecycleLedger())

        self.run(
            _("继续清理"),
            job,
            lambda value: self.resumed(*value),
            status=_("正在继续清理…"),
            progress=self.cleanup_progress,
            cancellable=True,
            lock=self.repo.root,  # D73 4
        )

    def resumed(
        self, purged: list[dict[str, Any]], outcomes: list[Outcome], room: dict[str, Any]
    ) -> None:
        self.show_outcomes(outcomes)
        self.result.set(state.cleanup_batch_summary(outcomes, RECYCLE))
        log.info(state.cleanup_done_log(outcomes, purged))
        self.waiting_items = list(room["waiting"])
        self.refresh_buttons()
        self.app.library_changed()
        if self.waiting_items:
            FullBinDialog(self, room)


def room_to_make(repo: Repo, items: Sequence[Waiting], ledger: Any) -> dict[str, Any]:
    """For the folders that wait: the room left in each of their bins (as the ledger counts
    it) and EngineSave's earlier items in those bins (read only; on the worker thread)."""
    from enginesave.cleanup import recycle_bin, recycle_bin_of

    drives = sorted({i.drive for i in items})
    rooms = {}
    for drive in drives:
        recycle_bin_of(ledger, drive)
        rooms[drive] = ledger.room(drive) or 0
    ours = recycle_bin.ours(repo, drives) if drives else []
    return {"waiting": list(items), "rooms": rooms, "ours": ours}


def waiting_rows(
    items: Sequence[Waiting], permanent: frozenset[str] | set[str] = frozenset()
) -> list[tuple[str, int, bool]]:
    return [(i.drive, i.bytes, path_key(i.path) in permanent) for i in items]


class FullBinDialog(Dialog):
    """The recycle bin has no room for the rest (D41 6). Two lists: EngineSave's earlier items
    still in that bin (backed up and verified), ticked to be purged from it; and the folders
    that wait, ticked to be deleted permanently instead (not ticked: recycled). The line under
    them says what the ticks make room for; 「继续清理」 runs when it is enough, 「稍后再说」
    leaves the rest for later (the cleanup dialog's main button goes on with it)."""

    def __init__(self, parent: CleanupDialog, room: dict[str, Any]) -> None:
        super().__init__(parent.app, _("回收站放不下"), min_size=(1000, 520))
        self.parent = parent
        self.rooms: dict[str, int] = room["rooms"]
        self.waiting_items: list[Waiting] = room["waiting"]
        self.ours: list[OurItem] = room["ours"]
        self.purge_ticked = set(range(len(self.ours)))  # A: all ticked (D41 6)
        self.permanent_ticked: set[int] = set()  # B: none ticked
        body = self.body
        self.heading = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.heading, wraplength=960).pack(anchor="w")
        ttk.Label(
            body,
            text=_(state.FULL_BIN_OURS),
        ).pack(anchor="w", pady=(PAD, 0))
        self.ours_tree = scrolled_tree(
            body,
            [
                ("tick", _("勾选"), 46),
                ("record", _("记录"), 180),
                ("path", _("原路径"), 420),
                ("size", _("大小"), 80),
                ("when", _("放入回收站"), 130),
            ],
            height=5,
        )
        for index, ours in enumerate(self.ours):
            self.ours_tree.insert(
                "",
                "end",
                iid=f"a{index}",
                values=(
                    state.CHECKED,
                    ours.display_name,
                    ours.item.original,
                    state.size_text(ours.item.size),
                    state.local_time(ours.item.deleted_at),
                ),
            )
        ttk.Label(body, text=_(state.FULL_BIN_WAITING)).pack(anchor="w", pady=(PAD, 0))
        self.waiting_tree = scrolled_tree(
            body,
            [
                ("tick", _("勾选"), 46),
                ("record", _("记录"), 180),
                ("path", _("路径"), 420),
                ("size", _("大小"), 80),
            ],
            height=5,
        )
        for index, item in enumerate(self.waiting_items):
            self.waiting_tree.insert(
                "",
                "end",
                iid=f"b{index}",
                values=(state.UNCHECKED, item.display_name, item.path, state.size_text(item.bytes)),
            )
        for tree in (self.ours_tree, self.waiting_tree):
            tree.column("tick", anchor="center")
            tree.bind("<ButtonRelease-1>", lambda event, t=tree: self.click(t, event))
        self.status = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.status).pack(anchor="w", pady=(PAD, 0))
        self.button(_("稍后再说"), self.cancel, action=False)
        self.go_button = self.primary(_("继续清理"), self.go_on)
        self.refresh_buttons()
        self.show()

    def needs(self) -> list[state.BinNeed]:
        permanent = {path_key(self.waiting_items[i].path) for i in self.permanent_ticked}
        ours = [
            (Path(o.item.original).anchor.upper(), o.item.size, i in self.purge_ticked)
            for i, o in enumerate(self.ours)
        ]
        return state.bin_needs(self.rooms, waiting_rows(self.waiting_items, permanent), ours)

    def refresh_buttons(self) -> None:
        needs = self.needs()
        heading = state.full_bin_heading(
            state.bin_needs(self.rooms, waiting_rows(self.waiting_items), [])
        )
        self.heading.set("\n".join(heading))
        self.status.set(state.full_bin_status(needs))
        set_enabled(self.go_button, not self.busy and not any(n.missing for n in needs))

    def click(self, tree: ttk.Treeview, event: tk.Event) -> None:
        if (
            tree.identify_region(event.x, event.y) != "cell"
            or tree.identify_column(event.x) != "#1"
        ):
            return
        iid = tree.identify_row(event.y)
        if iid:
            self.toggle(iid)

    def toggle(self, iid: str) -> None:
        """``a<n>``: purge EngineSave's earlier item n; ``b<n>``: delete waiting folder n
        permanently."""
        if self.busy:
            return
        ticked = self.purge_ticked if iid.startswith("a") else self.permanent_ticked
        tree = self.ours_tree if iid.startswith("a") else self.waiting_tree
        ticked ^= {int(iid[1:])}
        tree.set(iid, "tick", state.CHECKED if int(iid[1:]) in ticked else state.UNCHECKED)
        self.refresh_buttons()

    def go_on(self) -> None:
        if self.busy or any(n.missing for n in self.needs()):
            return
        purge = [self.ours[i] for i in sorted(self.purge_ticked)]
        permanent = [self.waiting_items[i].path for i in sorted(self.permanent_ticked)]
        parent = self.parent
        self.close()
        parent.resume(purge, permanent)

    def close(self) -> None:
        """Back to the cleanup dialog, which is modal again."""
        super().close()
        if self.parent.winfo_exists():
            self.app.last_dialog = self.parent
            try:
                self.parent.grab_set()
            except tk.TclError:  # not viewable (hidden main window in tests)
                pass
