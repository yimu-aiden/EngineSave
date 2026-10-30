"""The export dialog: one snapshot to a file, or the latest snapshot of several records
to a folder (D33 7)."""

from __future__ import annotations

import os
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Any

from enginesave.errors import UsageError
from enginesave.gui import state
from enginesave.gui.app import set_enabled
from enginesave.gui.i18n import _
from enginesave.gui.views.dialogs.base import PAD, TOKENS, Dialog, log, scrolled_tree
from enginesave.repo import Repo

if TYPE_CHECKING:
    from enginesave.export import BatchResult, ExportResult
    from enginesave.gui.app import App


class ExportDialog(Dialog):
    """「导出…」 (D33 ⑦): one dialog whether one record or several are exported.

    One record: a snapshot (``latest``, or the one picked in the library) to a zip with the
    default name in the last export folder (the first time: Documents); 「另存为…」 changes
    the file. Several records: the latest snapshot of each into one folder, one zip per record;
    「更换文件夹…」 changes the folder. The options are the same either way: "覆盖同名文件" is
    remembered, private keys and original paths start unticked every time (S-092). The main
    button exports at once (D32) and the library verifies every archive. An existing file is
    replaced only when "覆盖" is ticked; for a single file the dialog asks once otherwise
    (S-093), several are skipped and reported.
    """

    def __init__(
        self, app: App, repo: Repo, game_ids: Sequence[str], snapshot_id: str | None = None
    ) -> None:
        catalog = repo.load_catalog()
        found = [repo.resolve_game(catalog, gid) for gid in game_ids]  # before any window exists
        records = list({r["game_id"]: r for r in found}.values())
        if not records:
            raise UsageError("choose at least one record to export")
        single = len(records) == 1
        super().__init__(app, _("导出"), min_size=(900, 380) if single else (1000, 560))
        self.repo = repo
        self.single = single
        self.game_ids = [str(r["game_id"]) for r in records]
        self.display_name = str(records[0].get("display_name") or self.game_ids[0])
        self.overwrite = tk.BooleanVar(value=app.settings.export_overwrite)
        self.keep_paths = tk.BooleanVar(value=False)
        self.tokens = tk.BooleanVar(value=False)
        self.target = tk.StringVar()  # one record: the file
        self.folder = tk.StringVar(value=str(state.export_folder(app.settings)))  # several
        self.snapshot = tk.StringVar(value="")
        self.labels: dict[str, str] = {}
        self.result_path: Path | None = None
        self.result_folder: Path | None = None
        self.tree: ttk.Treeview | None = None

        body = self.body
        top = ttk.Frame(body)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        if single:
            game_id = self.game_ids[0]
            ids = list(reversed(repo.snapshot_ids(game_id)))
            self.labels = {self._label(sid): sid for sid in ids}
            chosen = snapshot_id or repo.latest_snapshot(game_id) or (ids[0] if ids else "")
            self.snapshot.set(self._label(chosen) if chosen else "")
            ttk.Label(
                top,
                text=_(
                    "记录：{display_name}  ({game_id})",
                    display_name=self.display_name,
                    game_id=game_id,
                ),
            ).grid(row=0, column=0, columnspan=3, sticky="w")
            ttk.Label(top, text=_("快照：")).grid(row=1, column=0, sticky="w", pady=(PAD, 0))
            combo = ttk.Combobox(
                top, textvariable=self.snapshot, values=list(self.labels), state="readonly"
            )
            combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(PAD, 0))
            combo.bind("<<ComboboxSelected>>", lambda _e: self.snapshot_changed())
            where, value, change = _("保存为："), self.target, _("另存为…")
            overwrite = _("覆盖同名文件（不勾选时，文件已存在会先问一声）")
        else:
            names = _("、").join(str(r.get("display_name") or r["game_id"]) for r in records[:6])
            more = _(" 等 {n} 条", n=len(records)) if len(records) > 6 else ""
            ttk.Label(
                top,
                text=_(
                    "导出 {n} 条记录的最新快照，每条一个 zip：{names}{more}",
                    n=len(records),
                    names=names,
                    more=more,
                ),
                wraplength=940,
            ).grid(row=0, column=0, columnspan=3, sticky="w")
            where, value, change = _("导出到文件夹："), self.folder, _("更换文件夹…")
            overwrite = _("覆盖同名文件（不勾选时，文件夹里已有的同名导出文件会被跳过）")
        ttk.Label(top, text=where).grid(row=2, column=0, sticky="w", pady=(PAD, 0))
        ttk.Entry(top, textvariable=value).grid(row=2, column=1, sticky="ew", pady=(PAD, 0))
        change_button = ttk.Button(top, text=change, command=self.browse)
        change_button.grid(row=2, column=2, padx=(PAD, 0), pady=(PAD, 0))
        self.actions.append(change_button)
        options = (
            (overwrite, self.overwrite),
            (
                _("保留原始路径（导出文件会含有本机的文件夹路径，可能包括 Windows 用户名）"),
                self.keep_paths,
            ),
            (_("包含 tokens（Ren'Py 存档签名私钥；含私钥的导出文件请勿分享）"), self.tokens),
        )
        checks = []
        for row, (text, variable) in enumerate(options, start=3):
            check = ttk.Checkbutton(top, text=text, variable=variable)
            check.grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(PAD if row == 3 else 0, 0)
            )
            checks.append(check)
        self.tokens_check = checks[-1]
        ttk.Label(
            body,
            text=_(
                "不勾选“保留原始路径”时，路径改写为 GAME_ROOT/… 与 RENPY_SAVES_ROOT/…，只能用"
                "绝对路径表示的额外存档目录不导出。"
                "文件名为 EngineSave-<名称>-<快照>.zip；每个导出文件"
                "先写成临时文件、逐个校验 SHA-256 后才改成正式文件名"
            )
            + ("" if single else _("，某一条失败不影响其他条"))
            + _("。不能放在备份库、游戏目录、存档目录或扫描根目录里。"),
            wraplength=960,
        ).pack(anchor="w", pady=(PAD, PAD))
        if not single:
            columns = [
                ("result", _("结果"), 70),
                ("name", _("名称"), 200),
                ("snapshot", _("快照"), 150),
                ("file", _("文件"), 320),
                ("note", _("说明"), 260),
            ]
            self.tree = scrolled_tree(body, columns, height=8)
        self.result = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.result, wraplength=960).pack(anchor="w", pady=(PAD, 0))
        self.button(_("关闭"), self.cancel, action=False)
        self.open_button = self.button(_("打开文件夹"), self.open_folder, action=False)
        self.export_button = self.primary(_("导出"), self.export)
        if single:
            self.watch(self.target, self.refresh_buttons)
            self.snapshot_changed()
        else:
            self.watch(self.folder, self.folder_changed)
            self.folder_changed()
        self.show()

    def _label(self, snapshot_id: str) -> str:
        summary = self.repo.snapshot_summary(self.game_ids[0], snapshot_id)
        return _(
            "{snapshot_id}   {v}   {v2}   {v3} 个文件",
            snapshot_id=snapshot_id,
            v=state.snapshot_time(snapshot_id),
            v2=summary.get("reason"),
            v3=summary.get("file_count"),
        )

    @property
    def snapshot_id(self) -> str:
        return self.labels.get(self.snapshot.get(), "")

    def snapshot_changed(self) -> None:
        """The default file name follows the snapshot; a folder chosen with 另存为 stays."""
        current = self.target.get().strip()
        folder = (
            str(Path(current).parent) if current else str(state.export_folder(self.app.settings))
        )
        if self.snapshot_id:
            self.target.set(str(state.export_target(folder, self.display_name, self.snapshot_id)))
        has_tokens = False
        if self.snapshot_id:
            try:
                manifest = self.repo.load_manifest(self.game_ids[0], self.snapshot_id)
                has_tokens = any(s.get("kind") == TOKENS for s in manifest.get("sources", []))
            except Exception as exc:  # noqa: BLE001 - shown to the user
                self.app.show_error(exc, parent=self)
        self.tokens.set(False)
        set_enabled(self.tokens_check, has_tokens)
        self.refresh_buttons()

    def folder_changed(self) -> None:
        self.export_button.configure(text=state.batch_export_primary_text(self.folder.get()))
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        set_enabled(self.open_button, self.result_folder is not None)
        ready = (
            bool(self.snapshot_id and self.target.get().strip())
            if self.single
            else bool(self.folder.get().strip())
        )
        set_enabled(self.export_button, ready and not self.busy)

    def browse(self) -> None:
        """「另存为…」 for one record, 「更换文件夹…」 for several."""
        if self.single:
            current = Path(self.target.get()) if self.target.get().strip() else None
            chosen = filedialog.asksaveasfilename(
                parent=self,
                title=_("导出为"),
                defaultextension=".zip",
                filetypes=[(_("zip 压缩包"), "*.zip")],
                initialdir=str(current.parent) if current else None,
                initialfile=current.name if current else None,
                confirmoverwrite=False,  # asked once, when exporting
            )
            if chosen:
                self.target.set(os.path.normpath(chosen))
            return
        current_folder = self.folder.get().strip()
        chosen = filedialog.askdirectory(
            parent=self,
            title=_("选择导出到的文件夹"),
            initialdir=current_folder or None,
            mustexist=False,
        )
        if chosen:
            self.folder.set(os.path.normpath(chosen))

    def options(self) -> dict[str, Any]:
        """Library arguments common to both cases; "覆盖" is remembered (D32 ⑥)."""
        self.app.settings.export_overwrite = bool(self.overwrite.get())
        return {
            "include_tokens": bool(self.tokens.get()),
            "keep_paths": bool(self.keep_paths.get()),
            "forbidden_roots": state.export_forbidden_roots(self.app.settings),
            "saves_root_override": self.app.saves_override(),
        }

    def export(self) -> None:
        if self.busy:
            return
        if self.single:
            self.export_one()
        else:
            self.export_many()

    def export_one(self) -> None:
        text = self.target.get().strip()
        if not self.snapshot_id or not text:
            messagebox.showinfo(_("导出"), _("请选择快照和保存位置。"), parent=self)
            return
        dest = Path(text)
        if dest.is_dir():
            messagebox.showinfo(_("导出"), _("请给导出文件一个文件名，而不是文件夹。"), parent=self)
            return
        overwrite = bool(self.overwrite.get())
        if dest.exists() and not overwrite:  # S-093: replacing a single file asks first
            if not messagebox.askyesno(
                _("文件已存在"), _("{dest} 已存在，要覆盖它吗？", dest=dest), parent=self
            ):
                return
            overwrite = True
        repo, game_id, snapshot_id = self.repo, self.game_ids[0], self.snapshot_id
        options = self.options()

        def job(_progress: Callable[[Any], None]) -> ExportResult:
            from enginesave.export import export_snapshot

            return export_snapshot(repo, game_id, snapshot_id, dest, overwrite=overwrite, **options)

        self.result_path = self.result_folder = None
        self.result.set("")
        self.run(
            _("导出"),
            job,
            self.exported_one,
            status=_("正在导出 {display_name}…", display_name=self.display_name),
        )

    def exported_one(self, result: ExportResult) -> None:
        self.result_path = Path(result.path)
        self.result_folder = self.result_path.parent
        text = state.export_summary(result)
        self.result.set(text)
        log.info(_("导出完成：%s"), text)
        self.app.settings.last_export_dir = str(self.result_folder)
        self.app.save_settings()

    def export_many(self) -> None:
        text = self.folder.get().strip()
        if not text:
            return
        folder = Path(text)
        repo, ids = self.repo, list(self.game_ids)
        options = {
            **self.options(),
            "overwrite": bool(self.overwrite.get()),
            "env": self.app.env,
        }
        stop = self.app.stop_requested  # D58: 「取消」, between archives

        def job(progress: Callable[[Any], None]) -> BatchResult:
            from enginesave.export import export_records

            def step(index: int, total: int, name: str) -> None:
                progress((index, total, name))

            return export_records(repo, ids, folder, progress=step, should_stop=stop, **options)

        self.result_folder = None
        if self.tree is not None:
            self.tree.delete(*self.tree.get_children())
        self.result.set("")
        self.run(
            _("批量导出"),
            job,
            self.exported_many,
            status=_("正在批量导出…"),
            progress=self.step_progress,
            cancellable=True,
        )

    def step_progress(self, value: tuple[int, int, str]) -> None:
        done, total, name = value
        text = _("正在导出 {name}（{v}/{total}）…", name=name, v=done + 1, total=total)
        self.app.progress.configure(maximum=max(total, 1), value=done)
        self.app.status_var.set(text)
        self.result.set(text)

    def exported_many(self, batch: BatchResult) -> None:
        if self.tree is not None:
            for row in state.batch_export_rows(batch):
                self.tree.insert("", "end", values=row)
        text = state.batch_export_summary(batch)
        self.result.set(text)
        log.info(_("批量导出：%s"), text)
        for item in batch.items:
            if item["result"] == "failed":
                log.warning(_("导出失败：%s：%s"), item["display_name"], item["reason"])
        if any(item["path"] for item in batch.items):
            self.result_folder = Path(batch.location)
            self.app.settings.last_export_dir = batch.location
        self.app.save_settings()

    def open_folder(self) -> None:
        if self.result_folder is not None:
            self.app.open_folder(self.result_folder, parent=self)
