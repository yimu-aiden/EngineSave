"""The import dialog: one archive or every archive in a folder, checked before anything
is written (D31)."""

from __future__ import annotations

import contextlib
import os
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, ttk
from typing import TYPE_CHECKING, Any

from enginesave.gui import state
from enginesave.gui.app import set_enabled
from enginesave.gui.i18n import _
from enginesave.gui.views.dialogs.base import PAD, TYPING_MS, Dialog, log, scrolled_tree

if TYPE_CHECKING:
    from enginesave.export import BatchResult, ImportPlan
    from enginesave.gui.app import App


class ImportDialog(Dialog):
    """One exported zip or every zip of a folder into the library (D27). Choosing the path
    checks it at once (read only, D32) and the main button imports what can be imported; each
    archive is planned again by the library right before it is imported, one that fails does
    not stop the others, and nothing outside the library is written."""

    def __init__(self, app: App, repo_root: Path) -> None:
        super().__init__(app, _("导入"), min_size=(1040, 520))
        self.repo_root = repo_root
        self.source = tk.StringVar(value="")
        self.reimport = tk.BooleanVar(value=False)  # D31: never remembered
        self.archives: list[Path] = []
        self.previews: list[tuple[Path, ImportPlan | None, str | None]] = []
        self.importable = 0
        self.generation = 0
        self.pending_check: str | None = None

        body = self.body
        top = ttk.Frame(body)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text=_("导入到备份库：{repo_root}", repo_root=repo_root)).grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        ttk.Label(top, text=_("导出文件或文件夹：")).grid(
            row=1, column=0, sticky="w", pady=(PAD, 0)
        )
        ttk.Entry(top, textvariable=self.source).grid(row=1, column=1, sticky="ew", pady=(PAD, 0))
        for column, (text, command) in enumerate(
            ((_("选择 zip…"), self.choose_zip), (_("选择文件夹…"), self.choose_folder)), start=2
        ):
            widget = ttk.Button(top, text=text, command=command)
            widget.grid(row=1, column=column, padx=(PAD, 0), pady=(PAD, 0))
            self.actions.append(widget)
        ttk.Checkbutton(
            top,
            text=_("重新导入已导入过的归档（默认跳过：备份库里已有同一记录、同一快照的导入）"),
            variable=self.reimport,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(PAD, 0))
        ttk.Label(
            body,
            text=_(
                "选择一个导出的 zip，或一个文件夹（导入其中全部 .zip，不含子文件夹，"
                "按文件名顺序）。选好后自动检查（只读）并列出结果；点“导入”后每个 zip 成为备份库里"
                "的一个新快照（reason import），不会改动备份库以外的任何文件；之后用“恢复…”"
                "放回游戏。备份库里已经导入过的（同一记录、同一快照）默认跳过。"
            ),
            wraplength=1000,
        ).pack(anchor="w", pady=(PAD, PAD))
        columns = [
            ("archive", _("导出文件"), 280),
            ("name", _("游戏"), 200),
            ("snapshot", _("快照"), 150),
            ("files", _("文件数"), 60),
            ("size", _("大小"), 80),
            ("check", _("结果"), 260),
        ]
        self.tree = scrolled_tree(body, columns, height=8)
        self.result = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.result, wraplength=1000).pack(anchor="w", pady=(PAD, 0))
        self.watch(self.source, lambda: self.schedule_check(TYPING_MS))
        self.watch(self.reimport, self.show_previews)
        self.button(_("关闭"), self.cancel, action=False)
        self.import_button = self.primary(_("导入"), self.import_all)
        self.refresh_buttons()
        self.show()

    def start_dir(self) -> str | None:
        return self.app.settings.last_import_dir or None

    def choose_zip(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self,
            title=_("选择导出的 zip"),
            filetypes=[(_("zip 压缩包"), "*.zip")],
            initialdir=self.start_dir(),
        )
        if chosen:
            self.choose(Path(os.path.normpath(chosen)), Path(os.path.normpath(chosen)).parent)

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self,
            title=_("选择放有导出文件的文件夹"),
            initialdir=self.start_dir(),
            mustexist=True,
        )
        if chosen:
            self.choose(Path(os.path.normpath(chosen)), Path(os.path.normpath(chosen)))

    def choose(self, source: Path, remember: Path) -> None:
        """A chosen path is checked at once; its folder is remembered for next time."""
        self.app.settings.last_import_dir = str(remember)
        self.app.save_settings()
        self.source.set(str(source))  # the watch starts the check
        self.schedule_check()

    def schedule_check(self, delay_ms: int = 0) -> None:
        """A new source discards the check and checks again."""
        self.generation += 1
        self.archives, self.previews, self.importable = [], [], 0
        self.tree.delete(*self.tree.get_children())
        self.import_button.configure(text=_("导入"))
        self.refresh_buttons()
        if self.pending_check is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self.pending_check)
        self.pending_check = self.later(delay_ms, self.check)

    def refresh_buttons(self) -> None:
        set_enabled(self.import_button, self.importable > 0 and not self.busy)

    def check(self) -> None:
        self.pending_check = None
        text = self.source.get().strip()
        if not text:
            self.result.set("")
            return
        source, repo_root, generation = Path(text), self.repo_root, self.generation

        def job(
            _progress: Callable[[Any], None],
        ) -> tuple[int, list[Path], list[tuple[Path, ImportPlan | None, str | None]]]:
            from enginesave.export import preview_imports, zip_archives

            archives = zip_archives(source) if source.is_dir() else [source]
            return generation, archives, preview_imports(repo_root, archives)

        self.result.set(_("正在检查导出文件（只读）…"))
        self.run_soon(
            _("导入检查"),
            job,
            self.checked,
            status=_("正在检查导出文件（只读）…"),
            on_error=lambda exc: self.check_failed(generation, exc),
        )

    def check_failed(self, generation: int, exc: BaseException) -> None:
        if generation == self.generation:
            self.result.set(_("无法导入：{exc}", exc=exc))

    def checked(
        self, value: tuple[int, list[Path], list[tuple[Path, ImportPlan | None, str | None]]]
    ) -> None:
        generation, archives, previews = value
        if generation != self.generation:
            return  # the source changed meanwhile; a newer check follows
        self.archives, self.previews = archives, previews
        self.show_previews()

    def show_previews(self) -> None:
        """The check's rows and the main button; the re-import tick only changes these."""
        if not self.previews:
            return
        reimport = bool(self.reimport.get())
        self.tree.delete(*self.tree.get_children())
        for row in state.import_preview_rows(self.previews, reimport):
            self.tree.insert("", "end", values=row)
        self.importable, skipped, _broken = state.import_counts(self.previews, reimport)
        self.import_button.configure(text=state.import_primary_text(self.importable))
        self.result.set(state.import_check_summary(self.importable, len(self.previews), skipped))
        self.refresh_buttons()

    def import_all(self) -> None:
        if not self.archives or not self.importable or self.busy:
            return
        repo_root, archives = self.repo_root, list(self.archives)
        location = Path(self.source.get().strip())
        reimport = bool(self.reimport.get())
        stop = self.app.stop_requested  # D58: 「取消」, between archives

        def job(progress: Callable[[Any], None]) -> BatchResult:
            from enginesave.export import import_archives

            def step(index: int, total: int, name: str) -> None:
                progress((index, total, name))

            return import_archives(
                repo_root,
                archives,
                reimport=reimport,
                location=location,
                progress=step,
                should_stop=stop,
            )

        self.importable, self.previews = 0, []
        self.import_button.configure(text=_("导入"))
        self.run(
            _("导入"),
            job,
            self.imported,
            status=_("正在导入…"),
            progress=self.step_progress,
            cancellable=True,
            lock=Path(repo_root),  # D73 4
        )

    def step_progress(self, value: tuple[int, int, str]) -> None:
        done, total, name = value
        text = _("正在导入 {name}（{v}/{total}）…", name=name, v=done + 1, total=total)
        self.app.progress.configure(maximum=max(total, 1), value=done)
        self.app.status_var.set(text)
        self.result.set(text)

    def imported(self, batch: BatchResult) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in state.import_rows(batch):
            self.tree.insert("", "end", values=row)
        text = state.import_summary(batch)
        self.result.set(text)
        log.info(_("导入：%s"), text)
        for item in batch.items:
            if item["result"] == "failed":
                log.warning(_("导入失败：%s：%s"), item["archive"], item["error"])
        self.app.library_changed(compare=True)  # D54: imported snapshots count too
