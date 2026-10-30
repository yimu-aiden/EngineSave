"""The restore dialog: the snapshot, the install to restore into (D39), the sources and
the preview."""

from __future__ import annotations

import contextlib
import os
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Any

from enginesave.gui import state
from enginesave.gui.app import set_enabled
from enginesave.gui.i18n import _
from enginesave.gui.views.dialogs.base import (
    LINK_COLUMN,
    OPEN_LINK,
    PAD,
    TOKENS,
    TYPING_MS,
    Dialog,
    log,
    scrolled_tree,
)
from enginesave.repo import Repo

if TYPE_CHECKING:
    from enginesave.gui.app import App
    from enginesave.restore import RestorePlan, RestoreResult


class RestoreDialog(Dialog):
    """Opens with the preview already running (D32): snapshot ``latest`` and the install the
    saves go back into. Every change of the inputs previews again; the main button says how
    many files it overwrites, which is the confirmation (a non-empty target always gets a
    safety snapshot first). Restoring Ren'Py's private keys still asks once more.

    The install is offered, not guessed (D39): the candidates are the record's installs that
    still exist and the installs the last scan recognised as this record. One candidate is
    filled in; with several nothing is, and the preview waits until one is chosen from the
    list or with 「浏览…」. The library checks a chosen folder (the game inside a folder that
    only wraps it is used instead) and the preview says which folder it uses and when the
    game version differs from the one the saves were made with."""

    def __init__(self, app: App, repo: Repo, game_id: str, snapshot_id: str | None = None) -> None:
        from enginesave.restore import install_candidates

        doc = repo.load_game(game_id)  # may raise: read before any window exists
        super().__init__(app, _("恢复"), min_size=(1060, 560))
        self.repo = repo
        self.game_id = game_id
        self.doc = doc
        self.candidates = install_candidates(doc, app.report)
        self.default_root = self.candidates[0][0] if len(self.candidates) == 1 else None
        self.plan: RestorePlan | None = None
        self.generation = 0  # previews of older inputs are ignored
        self.pending_preview: str | None = None
        ids = list(reversed(repo.snapshot_ids(game_id)))
        self.labels = {self._label(sid): sid for sid in ids}
        chosen = snapshot_id or repo.latest_snapshot(game_id) or (ids[0] if ids else "")
        self.snapshot = tk.StringVar(value=self._label(chosen) if chosen else "")
        self.game_root = tk.StringVar(value=str(self.default_root) if self.default_root else "")
        self.tokens = tk.BooleanVar(value=False)
        self.source_vars: dict[str, tk.BooleanVar] = {}
        self.has_tokens = False
        self.in_game_folder: set[str] = set()

        body = self.body
        top = ttk.Frame(body)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        ttk.Label(
            top, text=_("游戏：{v}  ({game_id})", v=self.doc.get("display_name"), game_id=game_id)
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(top, text=_("快照：")).grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        combo = ttk.Combobox(
            top, textvariable=self.snapshot, values=list(self.labels), state="readonly", width=70
        )
        combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(PAD, 0))
        combo.bind("<<ComboboxSelected>>", lambda _e: self.snapshot_changed())
        ttk.Label(top, text=_("游戏目录：")).grid(row=2, column=0, sticky="w", pady=(PAD, 0))
        self.root_choice = ttk.Combobox(
            top, textvariable=self.game_root, values=[str(path) for path, _ in self.candidates]
        )
        self.root_choice.grid(row=2, column=1, sticky="ew", pady=(PAD, 0))
        beside = ttk.Frame(top)
        beside.grid(row=2, column=2, padx=(PAD, 0), pady=(PAD, 0))
        browse = ttk.Button(beside, text=_("浏览…"), command=self.browse)
        browse.pack(side="left")
        self.actions.append(browse)
        self.open_button = ttk.Button(beside, text=_(OPEN_LINK), command=self.open_game_root)
        self.open_button.pack(side="left", padx=(4, 0))
        hint = state.restore_root_hint(self.candidates)
        ttk.Label(top, text=hint, wraplength=900).grid(row=3, column=1, columnspan=2, sticky="w")
        self.sources_frame = ttk.Frame(top)
        self.sources_frame.grid(row=4, column=0, columnspan=3, sticky="w", pady=(PAD, 0))
        ttk.Label(
            body,
            text=_(
                "打开时已自动预览（只读），改动任何选项都会重新预览；主按钮写明会覆盖多少个文件。"
                "非空的目标目录会先自动建安全快照（reason pre_restore），可以用它恢复回去；"
                "目标里多出来的文件不会被删除。"
            ),
            wraplength=940,
        ).pack(anchor="w", pady=(PAD, PAD))
        columns = [
            ("source", _("来源"), 110),
            ("target", _("目标路径"), 410),
            LINK_COLUMN,
            ("files", _("写入"), 60),
            ("over", _("覆盖"), 60),
            ("extra", _("保留的多余文件"), 110),
            ("create", _("需创建目录"), 90),
            ("how", _("依据"), 150),
        ]
        self.preview_tree = scrolled_tree(body, columns, height=8)
        self.link_column(self.preview_tree)
        self.result = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.result, wraplength=940).pack(anchor="w", pady=(PAD, 0))
        self.watch(self.game_root, lambda: self.schedule_preview(TYPING_MS))
        self.watch(self.tokens, self.schedule_preview)
        self.button(_("关闭"), self.cancel, action=False)
        self.restore_button = self.primary(_("恢复"), self.restore)
        self.snapshot_changed()
        self.show()

    def _label(self, snapshot_id: str) -> str:
        summary = self.repo.snapshot_summary(self.game_id, snapshot_id)
        return (
            f"{snapshot_id}   {state.snapshot_time(snapshot_id)}   {summary.get('reason')}   "
            f"{summary.get('status')}"
        )

    @property
    def snapshot_id(self) -> str:
        return self.labels.get(self.snapshot.get(), "")

    def snapshot_changed(self) -> None:
        for child in self.sources_frame.winfo_children():
            child.destroy()
        self.source_vars = {}
        sources: list[dict[str, Any]] = []
        if self.snapshot_id:
            try:
                sources = self.repo.load_manifest(self.game_id, self.snapshot_id).get("sources", [])
            except Exception as exc:  # noqa: BLE001 - shown to the user
                self.app.show_error(exc, parent=self)
        self.in_game_folder = {  # sources that need the game folder to be put back
            str(s["source_id"]) for s in sources if s.get("root_token") == "GAME_ROOT"
        }
        ttk.Label(self.sources_frame, text=_("来源：")).pack(side="left")
        for source in sources:
            if source.get("kind") == TOKENS:
                continue
            var = tk.BooleanVar(value=True)
            self.watch(var, self.schedule_preview)
            self.source_vars[str(source["source_id"])] = var
            text = _("{v}（{v2}）", v=source["source_id"], v2=source["kind"])
            ttk.Checkbutton(self.sources_frame, text=text, variable=var).pack(side="left")
        self.has_tokens = any(s.get("kind") == TOKENS for s in sources)
        self.tokens.set(False)
        if self.has_tokens:
            ttk.Checkbutton(
                self.sources_frame,
                text=_("同时恢复 machine_tokens（Ren'Py 存档签名私钥，会覆盖本机私钥）"),
                variable=self.tokens,
            ).pack(side="left", padx=(PAD, 0))
        self.schedule_preview()

    def browse(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title=_("选择游戏目录"), mustexist=True)
        if chosen:
            self.game_root.set(os.path.normpath(chosen))

    def open_game_root(self) -> None:
        text = self.game_root.get().strip()
        if not text:
            messagebox.showinfo(_("打开文件夹"), _("还没有选择游戏目录。"), parent=self)
            return
        self.app.open_folder(text, parent=self)

    def schedule_preview(self, delay_ms: int = 0) -> None:
        """Any change of the inputs discards the preview and previews again (D32)."""
        self.generation += 1
        self.plan = None
        self.links = {}
        self.preview_tree.delete(*self.preview_tree.get_children())
        self.restore_button.configure(text=_("恢复"))
        self.refresh_buttons()
        if self.pending_preview is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self.pending_preview)
        self.pending_preview = self.later(delay_ms, self.preview)

    def refresh_buttons(self) -> None:
        set_enabled(self.restore_button, self.plan is not None and not self.busy)

    def chosen_sources(self) -> list[str]:
        chosen = [sid for sid, var in self.source_vars.items() if var.get()]
        if self.has_tokens and self.tokens.get():
            chosen.append(TOKENS)
        return chosen

    def preview(self) -> None:
        self.pending_preview = None
        sources = self.chosen_sources()
        if not self.snapshot_id or not sources:
            self.result.set(_("请选择快照和至少一个来源。"))
            return
        if (
            not self.game_root.get().strip()
            and self.default_root is None
            and self.in_game_folder.intersection(sources)
        ):  # the case where the preview waits for the user (D32 ①, D39)
            self.result.set(state.restore_choose_root(len(self.candidates)))
            return
        options = state.restore_options(
            self.snapshot_id,
            self.game_root.get(),
            self.default_root,
            sources,
            self.app.saves_override(),
        )
        repo, game_id, generation = self.repo, self.game_id, self.generation
        report = self.app.report  # D39: installs the last scan found are candidates too

        def job(_progress: Callable[[Any], None]) -> tuple[int, RestorePlan]:
            from enginesave.restore import plan_restore

            plan = plan_restore(repo, repo.load_catalog(), game_id, report=report, **options)
            return generation, plan

        self.result.set(_("正在预览（只读）…"))
        self.run_soon(
            _("恢复预览"),
            job,
            self.show_plan,
            status=_("正在生成恢复计划（只读）…"),
            on_error=lambda exc: self.preview_failed(generation, exc),
        )

    def preview_failed(self, generation: int, exc: BaseException) -> None:
        if generation == self.generation:
            self.result.set(_("无法预览：{exc}", exc=exc))
            log.info(_("恢复预览没有完成：%s"), exc)

    def show_plan(self, value: tuple[int, RestorePlan]) -> None:
        generation, plan = value
        if generation != self.generation:
            return  # the inputs changed meanwhile; a newer preview follows
        self.preview_tree.delete(*self.preview_tree.get_children())
        self.links = {}
        for target in plan.targets:
            iid = self.preview_tree.insert(
                "",
                "end",
                values=(
                    target.source_id,
                    str(target.target_dir),
                    _(OPEN_LINK),
                    len(target.files),
                    target.overwrites,
                    target.extra_in_target,
                    _("是") if target.creates_dir else _("否"),
                    target.resolved_by,
                ),
            )
            self.links[iid] = Path(target.target_dir)
        self.plan = plan
        self.restore_button.configure(text=state.restore_primary_text(plan))
        notes = "".join(_("{line}。", line=line) for line in state.restore_notes(plan))
        self.result.set(_("预览完成：确认上面的目标路径无误后点主按钮。{notes}", notes=notes))
        log.info(
            _("恢复预览：%s 快照 %s，%d 个来源"),
            plan.display_name,
            plan.snapshot_id,
            len(plan.targets),
        )

    def restore(self) -> None:
        plan = self.plan
        if plan is None or self.busy:
            return
        if plan.includes_tokens and not messagebox.askyesno(
            _("确认恢复私钥"),
            _("这会覆盖本机 Ren'Py 的存档签名私钥（tokens\\security_keys.txt）。确定继续吗？"),
            icon="warning",
            default="no",
            parent=self,
        ):
            return

        def job(_progress: Callable[[Any], None]) -> RestoreResult:
            from enginesave.restore import execute_restore

            return execute_restore(plan)

        self.plan = None
        self.generation += 1  # a preview queued before this restore no longer applies
        self.run(
            _("恢复"),
            job,
            lambda result: self.restored(result, plan),
            status=_("正在恢复 {display_name}…", display_name=plan.display_name),
        )

    def restored(self, result: RestoreResult, plan: RestorePlan | None = None) -> None:
        safety = result.safety_snapshot or _("无需（目标为空或不存在）")
        text = _(
            "已恢复 {written} 个文件，失败 {n} 个；安全快照：{safety}。",
            written=result.written,
            n=len(result.failures),
            safety=safety,
        )
        if plan is not None:  # D39 (b): said again after restoring
            text += "".join(
                _("{line}。", line=line) for line in state.restore_notes(plan, version_only=True)
            )
        if result.failures:
            shown = _("；").join(
                f"{f['source_id']}/{f['path']}: {f['error']}" for f in result.failures[:5]
            )
            text += _(" 失败的文件保持原样：{shown}", shown=shown)
            log.warning(_("恢复有 %d 个文件失败：%s"), len(result.failures), result.failures)
        else:
            text += _(" 文件级恢复成功；游戏能否读取这些存档取决于版本兼容性。")
        self.result.set(text)
        self.restore_button.configure(text=_("恢复"))
        log.info(_("恢复完成：%s"), text)
        self.app.library_changed()
