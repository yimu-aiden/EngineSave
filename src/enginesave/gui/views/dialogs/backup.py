"""The backup preview and the backup result dialogs."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING

from enginesave.errors import EngineSaveError
from enginesave.gui import state
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
from enginesave.gui.views.dialogs.cleanup import CleanupDialog
from enginesave.models import SourceKind
from enginesave.repo import Repo

if TYPE_CHECKING:
    from enginesave.backup import BackupPlan, BackupResult
    from enginesave.gui.app import App


class BackupPreviewDialog(Dialog):
    """What a backup would copy (the 「预览…」 button); [开始备份] is the CLI's ``--yes``."""

    def __init__(self, app: App, plan: BackupPlan, on_start: Callable[[BackupPlan], None]) -> None:
        super().__init__(app, _("备份预览"), min_size=(1000, 520))
        self.plan = plan
        self.on_start = on_start
        state_text = _("新建") if plan.repo_state == "new" else _("已有")
        ttk.Label(
            self.body,
            text=_("备份库：{dest}（{state_text}）", dest=plan.dest, state_text=state_text),
        ).pack(anchor="w")
        columns = [
            LINK_COLUMN,
            ("files", _("文件数"), 80),
            ("size", _("大小"), 90),
            ("status", _("状态"), 150),
        ]
        self.tree = scrolled_tree(self.body, columns, height=14, tree_heading=_("游戏 / 来源"))
        for planned in plan.games:
            files = sum(len(ps.files) for ps in planned.sources)
            size = sum(ps.total_bytes for ps in planned.sources)
            label = planned.display_name + (
                _("（{version}）", version=planned.version) if planned.version else ""
            )
            if planned.kind == "unlinked":
                label += _("  [孤立目录]")
            record = _("已有记录") if planned.game_id else _("新记录")
            parent = self.tree.insert(
                "", "end", text=label, values=("", files, state.size_text(size), record), open=True
            )
            for ps in planned.sources:
                iid = self.tree.insert(
                    parent,
                    "end",
                    text=_(
                        "{source_id}：{path}", source_id=ps.source.source_id, path=ps.source.path
                    ),
                    values=(
                        _(OPEN_LINK),
                        len(ps.files),
                        state.size_text(ps.total_bytes),
                        ps.source.status.value,
                    ),
                )
                if ps.source.kind is not SourceKind.REGISTRY:  # a registry key: nothing to open
                    self.links[iid] = Path(ps.source.path)
        self.link_column(self.tree)
        games, files, size = state.plan_totals(plan)
        ttk.Label(
            self.body,
            text=_(
                "合计：{games} 项，{files} 个文件，{v}",
                games=games,
                files=files,
                v=state.size_text(size),
            ),
        ).pack(anchor="w", pady=(PAD, 0))
        warnings = state.plan_warnings(plan)
        ttk.Label(self.body, text=_("提示与警告（{n}）：", n=len(warnings))).pack(
            anchor="w", pady=(PAD, 0)
        )
        box = tk.Listbox(self.body, height=min(max(len(warnings), 1), 6))
        for line in warnings or [_("无")]:
            box.insert("end", line)
        box.pack(fill="x")
        self.button(_("取消"), self.cancel, action=False)
        self.start_button = self.primary(_("开始备份"), self.start)
        self.show()

    def start(self) -> None:
        self.close()
        self.on_start(self.plan)


class BackupResultDialog(Dialog):
    def __init__(self, app: App, result: BackupResult, repo_root: Path) -> None:
        super().__init__(app, _("备份结果"), min_size=(900, 420))
        self.repo_root = repo_root
        counts = state.result_counts(result)
        ttk.Label(
            self.body,
            text=_(
                "备份库：{repo_root}    成功 {v}，部分完成 {v2}，失败 {v3}，跳过 {v4}",
                repo_root=repo_root,
                v=counts["ok"],
                v2=counts["partial"],
                v3=counts["failed"],
                v4=counts["skipped"],
            ),
        ).pack(anchor="w")
        if result.cancelled:  # D58
            ttk.Label(self.body, text=_(state.CANCELLED_BACKUP), foreground=WARNING_COLOR).pack(
                anchor="w"
            )
        columns = [
            ("result", _("结果"), 80),
            ("name", _("名称"), 230),
            ("snapshot", _("快照"), 170),
            ("files", _("文件数"), 70),
            ("size", _("大小"), 90),
            ("note", _("说明"), 300),
        ]
        self.tree = scrolled_tree(self.body, columns, height=12)
        for r in result.results:
            notes = [r.message] if r.message else []
            notes += [f"{e['source_id']}/{e['path']}: {e['error']}" for e in r.errors]
            if r.changed_sources:
                notes.append(_("备份期间有改动：{v}", v=", ".join(r.changed_sources)))
            if r.storage:
                notes.append(f"storage {r.storage}")
            self.tree.insert(
                "",
                "end",
                values=(
                    r.status.upper(),
                    r.display_name,
                    r.snapshot_id or state.NO_VALUE,
                    r.file_count,
                    state.size_text(r.total_bytes),
                    _("；").join(notes),
                ),
            )
        for skipped in result.skipped:
            self.tree.insert(
                "",
                "end",
                values=("SKIPPED", skipped["name"], state.NO_VALUE, "", "", skipped["reason"]),
            )
        hints = []
        if counts["partial"]:
            hints.append(
                _(
                    "有部分完成（partial）的快照：备份时存档被改动或有文件读不出，请关闭游戏后再备份一次。"
                )
            )
        if hint := state.storage_hint(result):
            hints.append(hint)
        if steps := state.backup_next_steps(result):
            hints.append(steps)
        for hint in hints:
            ttk.Label(self.body, text=hint, wraplength=860).pack(anchor="w", pady=(PAD, 0))
        self.mirror_var = tk.StringVar(value="")  # D64: the automatic mirror, when there is one
        self.mirror_label = ttk.Label(self.body, textvariable=self.mirror_var, wraplength=860)
        self.mirror_stop: ttk.Button | None = None
        self.mirror_stopping = False
        self.primary(_("关闭"), self.cancel, action=False)
        self.button(_("打开备份库文件夹"), lambda: app.open_folder(repo_root), action=False)
        cleanable = state.cleanup_candidates(result)
        if cleanable:  # only when every backup is complete (D26)
            self.button(_("清理…"), lambda: self.cleanup(cleanable), action=False)
        self.show()

    def mirroring(self, folder: Path) -> None:
        """D64: the automatic mirror started; it runs in the background, 「取消镜像」 stops it
        (the main window's 「取消」 is out of reach while this dialog is open)."""
        self.mirror_var.set(_("正在自动镜像到 {folder}…", folder=folder))
        self.mirror_label.pack(anchor="w", pady=(PAD, 0))
        if self.mirror_stop is None:
            self.mirror_stop = self.button(_(state.STOP_MIRROR), self.stop_mirror, action=False)

    def stop_mirror(self) -> None:
        self.mirror_stopping = True
        self.app.cancel_task()
        if self.mirror_stop is not None:
            self.mirror_stop.configure(text=_(state.CANCELLING))
            self.mirror_stop.state(["disabled"])

    def mirrored(self, text: str) -> None:
        """The automatic mirror ended: its line at the bottom."""
        self.mirror_var.set(text)
        self.mirror_label.pack(anchor="w", pady=(PAD, 0))
        if self.mirror_stop is not None:
            self.mirror_stop.destroy()
            self.mirror_stop = None

    def close(self) -> None:
        """D66 9: a running automatic mirror stops at its next safe point first (mirroring
        again goes on from there); it would otherwise run on with nothing to show it."""
        if self.mirror_stop is not None and not self.mirror_stopping:
            log.info(_("关闭结果窗口，先停下自动镜像（再镜像一次会接着做）"))
            self.stop_mirror()
        super().close()

    def cleanup(self, game_ids: list[str]) -> None:
        if self.mirror_stop is not None:  # D64: the automatic mirror stops at a safe point
            log.info(_("清理前先停下自动镜像（再镜像一次会接着做）"))
            self.stop_mirror()
        self.close()
        try:
            CleanupDialog(self.app, Repo.open(self.repo_root), game_ids)
        except EngineSaveError as exc:
            self.app.show_error(exc)
