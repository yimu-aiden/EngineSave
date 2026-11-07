"""「删除备份」 (0.8.0, D49), from the library page's 「删除…」: the target records with their
snapshots, ticked as the page asked; a line says what goes and about how much it frees, a red
line which records' games are gone. One click deletes permanently through
``enginesave.removal``; the library page is read again and its ticks are cleared."""

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable, Mapping, Sequence
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from enginesave.errors import EngineSaveError
from enginesave.gui import state
from enginesave.gui.app import set_enabled
from enginesave.gui.i18n import _
from enginesave.gui.views.dialogs.base import PAD, WARNING_COLOR, Dialog, scrolled_tree
from enginesave.repo import Repo, install_present

if TYPE_CHECKING:
    from enginesave.gui.app import App
    from enginesave.removal import Inventory, RemoveResult

log = logging.getLogger("enginesave.gui")


class RemoveDialog(Dialog):
    def __init__(
        self,
        app: App,
        repo: Repo,
        game_ids: Sequence[str],
        names: Mapping[str, str],
        only_snapshot: str | None = None,
    ) -> None:
        super().__init__(app, _(state.REMOVE_TITLE), min_size=(880, 480))
        self.repo = repo
        self.game_ids = list(game_ids)
        self.names = dict(names)
        self.only_snapshot = only_snapshot
        self.records: list[state.RemoveRecord] = []
        self.found: Inventory | None = None
        self.ticked: set[state.RemoveTick] = set()
        self.rows: dict[str, state.RemoveTick | str] = {}  # tree row -> a tick, or a record's id
        self.done = False

        ttk.Label(self.body, text=_(state.REMOVE_INTRO), wraplength=840).pack(anchor="w")
        self.tree = scrolled_tree(
            self.body,
            [
                ("check", _("勾选"), 46),
                ("name", _("名称 / 快照"), 320),
                ("what", _("类型 / 时间"), 230),
                ("files", _("文件数"), 70),
                ("size", _("大小"), 90),
            ],
            height=12,
        )
        self.tree.column("check", anchor="center")
        self.tree.bind("<ButtonRelease-1>", self.click)
        self.tree.bind("<space>", self.space)
        self.summary = tk.StringVar(value=_(state.READING))
        ttk.Label(self.body, textvariable=self.summary, wraplength=840).pack(
            anchor="w", pady=(PAD, 0)
        )
        self.warning = tk.StringVar(value="")
        self.warning_label = ttk.Label(
            self.body, textvariable=self.warning, foreground=WARNING_COLOR, wraplength=840
        )
        self.button(_("关闭"), self.cancel, action=False)
        self.remove_button = self.primary(_("永久删除"), self.remove)
        self.refresh_buttons()
        self.show()
        self.run_soon(_("读取备份库"), self.read, self.loaded, status=_(state.READING))

    # ---------------------------------------------------------------------------- reading

    def read(
        self, _progress: Callable[[Any], None]
    ) -> tuple[list[state.RemoveRecord], Inventory, dict[str, str]]:
        """Read-only, on the worker thread: the records, every manifest, the snapshot times."""
        from enginesave.removal import inventory

        repo = self.repo
        catalog = repo.load_catalog()
        records, times = [], {}
        for game_id in self.game_ids:
            record = repo.resolve_game(catalog, game_id)
            try:
                installed = install_present(repo.load_game(game_id))
            except EngineSaveError:
                installed = False
            snapshots = tuple(repo.snapshot_ids(game_id))
            for snapshot_id in snapshots:
                summary = repo.snapshot_summary(game_id, snapshot_id)
                reason = summary.get("reason") or summary.get("status")
                times[f"{game_id}/{snapshot_id}"] = f"{state.snapshot_time(snapshot_id)} · {reason}"
            name = self.names.get(game_id) or str(record.get("display_name") or game_id)
            kind = str(record.get("kind", "game"))
            records.append(state.RemoveRecord(game_id, name, kind, installed, snapshots))
        return records, inventory(repo), times

    def loaded(self, value: tuple[list[state.RemoveRecord], Inventory, dict[str, str]]) -> None:
        self.records, self.found, times = value
        self.ticked = state.initial_ticks(self.records, self.only_snapshot)
        for index, record in enumerate(self.records):
            parent = f"r{index}"
            self.rows[parent] = record.game_id
            values = state.record_values(record, self.found, "", state.KIND_TEXTS)
            self.tree.insert("", "end", iid=parent, values=values, open=True)
            for number, snapshot_id in enumerate(reversed(record.snapshots)):
                iid = f"{parent}s{number}"
                self.rows[iid] = (record.game_id, snapshot_id)
                when = times.get(f"{record.game_id}/{snapshot_id}", "")
                values = state.snapshot_values(self.found, record.game_id, snapshot_id, "", when)
                self.tree.insert(parent, "end", iid=iid, values=values)
        self.refresh_buttons()

    # ------------------------------------------------------------------------------ ticks

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

    def record(self, game_id: str) -> state.RemoveRecord:
        return next(r for r in self.records if r.game_id == game_id)

    def toggle(self, iid: str) -> None:
        """A record row ticks or clears all its snapshots; a snapshot row only itself."""
        target = self.rows.get(iid)
        if target is None or self.done or self.busy:
            return
        if isinstance(target, str):
            ticks = self.record(target).ticks()
            self.ticked = self.ticked - ticks if ticks <= self.ticked else self.ticked | ticks
        else:
            self.ticked ^= {target}
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        if not self.winfo_exists():
            return
        found = self.found
        if found is None or self.done:
            set_enabled(self.remove_button, False)
            return
        for iid, target in self.rows.items():
            if not self.tree.exists(iid):
                continue
            if isinstance(target, str):
                ticks = self.record(target).ticks()
                mark = state.CHECKED if ticks <= self.ticked else state.UNCHECKED
            else:
                mark = state.CHECKED if target in self.ticked else state.UNCHECKED
            self.tree.set(iid, "check", mark)
        counts = state.remove_counts(self.records, self.ticked, found)
        self.summary.set(state.remove_summary(counts, found.blocked))
        warning = state.gone_warning(counts.gone)
        self.warning.set(warning or "")
        if warning:
            self.warning_label.pack(anchor="w", pady=(4, 0))
        else:
            self.warning_label.pack_forget()
        self.remove_button.configure(text=state.remove_primary_text(counts))
        set_enabled(self.remove_button, bool(self.ticked) and not self.busy)

    # ----------------------------------------------------------------------------- remove

    def remove(self) -> None:
        if self.busy or self.done or not self.ticked:
            return
        requests = state.remove_requests(self.records, self.ticked)
        shown = {r.game_id: list(r.snapshots) for r in self.records}
        root = self.repo.root
        stop = self.app.stop_requested  # D58: 「取消」, between snapshots

        def job(_progress: Callable[[Any], None]) -> RemoveResult:
            from enginesave.errors import UsageError
            from enginesave.removal import execute_remove, inventory, plan_remove

            repo = Repo.open(root)
            found = inventory(repo)
            plan = plan_remove(repo, repo.load_catalog(), requests, found)
            # 1.0.0 (D72 R06): the snapshots this dialog showed, not those there are now
            changed = [i.display_name for i in plan.items if i.existing != shown.get(i.game_id)]
            if changed:
                raise UsageError(
                    _(
                        "{v} 在打开这个对话框之后有了新快照（或少了快照），什么都没有删除；"
                        "请关闭对话框重新打开",
                        v=_("、").join(changed[:3]),
                    )
                )
            return execute_remove(plan, found, should_stop=stop)

        self.run(
            _("删除备份"),
            job,
            self.removed,
            status=_("正在删除备份…"),
            on_error=self.failed,
            cancellable=True,
            lock=root,  # D73 4
        )

    def failed(self, exc: BaseException) -> None:
        """Part of it may be deleted already: say so, and read the library again (with the
        scan page's comparison, D54) as after a removal that finished."""
        self.app.show_error(exc, parent=self)
        self.done = True
        set_enabled(self.remove_button, False)
        self.app.library.removed()

    def deleted_row(self, target: str | tuple[str, str]) -> bool:
        """Whether a row's record or snapshot is gone from disk now: after 「取消」 (D58) or a
        leftover, a ticked one may still be there and keeps its row."""
        if isinstance(target, str):
            ticked = self.record(target).ticks() <= self.ticked
            return ticked and not self.repo.game_dir(target).exists()
        game_id, snapshot_id = target
        if target not in self.ticked:
            return False
        if snapshot_id == state.EMPTY_RECORD:
            return not self.repo.game_dir(game_id).exists()
        return not self.repo.snapshot_dir(game_id, snapshot_id).exists()

    def removed(self, result: RemoveResult) -> None:
        text = state.remove_done_text(result)
        log.info(text)
        for path in result.leftovers:
            log.warning(_("没能删除：%s"), path)
        self.done = True
        for iid, target in list(self.rows.items()):
            if self.tree.exists(iid) and self.deleted_row(target):
                self.tree.delete(iid)
        self.summary.set(text)
        self.warning_label.pack_forget()
        self.remove_button.configure(text=_("永久删除"))
        set_enabled(self.remove_button, False)
        self.app.library.removed()
