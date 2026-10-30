"""The settings dialog: the saves root override and what a backup includes (CANDIDATE sources,
the private keys in tokens)."""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

from enginesave.fsutil import path_key
from enginesave.gui import i18n, state
from enginesave.gui.i18n import _
from enginesave.gui.views.dialogs.base import PAD, Dialog, log

if TYPE_CHECKING:
    from enginesave.gui.app import App


def language_label(choice: str) -> str:
    """A ``gui.json`` language as 「界面语言」 shows it: 「跟随系统」 in the window's language,
    each language by its own name (D68 3)."""
    return i18n.NAMES[choice] if choice in i18n.NAMES else _("跟随系统")


class SettingsDialog(Dialog):
    def __init__(self, app: App) -> None:
        super().__init__(app, _("设置"), min_size=(600, 280))
        settings = app.settings
        self.override = tk.StringVar(value=settings.saves_root_override or "")
        self.candidates = tk.BooleanVar(value=settings.include_candidates)
        self.tokens = tk.BooleanVar(value=settings.include_tokens)
        body = self.body
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text=_("Ren'Py 全局存档根目录（saves root override）：")).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Entry(body, textvariable=self.override, width=70).grid(
            row=1, column=0, sticky="ew", pady=(2, 2)
        )
        ttk.Button(body, text=_("浏览…"), command=self.browse).grid(row=1, column=1, padx=(PAD, 0))
        default = app.default_saves_root()
        ttk.Label(
            body,
            text=_(
                "留空则使用 {default}。扫描、备份与恢复中的全局存档都以此为根。", default=default
            ),
            wraplength=560,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, PAD))
        ttk.Checkbutton(
            body, text=_("备份时包含待确认（CANDIDATE）的存档来源"), variable=self.candidates
        ).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(
            body,
            text=_("备份时包含 tokens（Ren'Py 存档签名私钥；带私钥的备份库请妥善保管）"),
            variable=self.tokens,
        ).grid(row=5, column=0, columnspan=2, sticky="w")
        self.auto_mirror = tk.BooleanVar(value=settings.auto_mirror)
        last = app.last_mirror()
        if last is not None:  # D64: offered once this library was mirrored with success
            ttk.Checkbutton(
                body,
                text=_(state.AUTO_MIRROR).format(folder=last["to"]),
                variable=self.auto_mirror,
            ).grid(row=6, column=0, columnspan=2, sticky="w")
        # D63: folders the scan skips -- chosen (stored as the path) or typed as a pattern
        self.excludes = list(settings.scan_excludes)
        ttk.Label(body, text=_(state.EXCLUDES_LABEL), wraplength=560).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(PAD, 0)
        )
        self.exclude_list = tk.Listbox(body, height=4, activestyle="none", exportselection=False)
        self.exclude_list.grid(row=8, column=0, sticky="ew", pady=(2, 0))
        side = ttk.Frame(body)
        side.grid(row=8, column=1, sticky="n", padx=(PAD, 0), pady=(2, 0))
        ttk.Button(side, text=_("添加文件夹…"), command=self.add_exclude_folder).pack(fill="x")
        ttk.Button(side, text=_("删除"), command=self.remove_exclude).pack(fill="x", pady=(4, 0))
        self.pattern = tk.StringVar()
        pattern = ttk.Entry(body, textvariable=self.pattern)
        pattern.grid(row=9, column=0, sticky="ew", pady=(4, 0))
        pattern.bind("<Return>", self.add_pattern)  # Enter here adds the pattern, not 「确定」
        ttk.Button(body, text=_("添加模式"), command=self.add_pattern).grid(
            row=9, column=1, padx=(PAD, 0), pady=(4, 0)
        )
        self.refresh_excludes()
        # D68 3: 「跟随系统 / 中文 / English」; the window is built again after 「确定」
        self.language_labels = [language_label(c) for c in i18n.CHOICES]
        chosen = settings.language if settings.language in i18n.CHOICES else i18n.AUTO
        self.language = tk.StringVar(value=language_label(chosen))
        language_row = ttk.Frame(body)
        language_row.grid(row=10, column=0, columnspan=2, sticky="w", pady=(PAD, 0))
        ttk.Label(language_row, text=_("界面语言：")).pack(side="left")
        self.language_box = ttk.Combobox(
            language_row,
            textvariable=self.language,
            values=self.language_labels,
            state="readonly",
            width=16,
        )
        self.language_box.pack(side="left", padx=(4, 0))
        ttk.Label(body, text=state.config_location(app.settings_file.parent), wraplength=560).grid(
            row=11, column=0, columnspan=2, sticky="w", pady=(PAD, 0)
        )
        self.button(_("取消"), self.cancel, action=False)
        self.primary(_("确定"), self.accept)
        self.show()

    def browse(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self, title=_("选择 Ren'Py 全局存档根目录"), mustexist=True
        )
        if chosen:
            self.override.set(os.path.normpath(chosen))

    def refresh_excludes(self) -> None:
        self.exclude_list.delete(0, "end")
        for item in self.excludes:
            self.exclude_list.insert("end", item)

    def add_exclude(self, text: str) -> None:
        text = text.strip().strip('"').strip()  # Explorer's "Copy as path" adds quotes
        if text and not any(path_key(text) == path_key(e) for e in self.excludes):
            self.excludes.append(text)
            self.refresh_excludes()

    def add_exclude_folder(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self, title=_("选择扫描时跳过的文件夹"), mustexist=True
        )
        if chosen:
            self.add_exclude(os.path.normpath(chosen))

    def add_pattern(self, _event: tk.Event | None = None) -> str:
        self.add_exclude(self.pattern.get())
        self.pattern.set("")
        return "break"

    def remove_exclude(self) -> None:
        for index in sorted(self.exclude_list.curselection(), reverse=True):
            del self.excludes[index]
        self.refresh_excludes()

    def accept(self) -> None:
        settings = self.app.settings
        self.add_exclude(self.pattern.get())  # typed but not added yet: taken as well
        settings.scan_excludes = list(self.excludes)
        settings.saves_root_override = self.override.get().strip() or None
        settings.include_candidates = bool(self.candidates.get())
        settings.include_tokens = bool(self.tokens.get())
        settings.auto_mirror = bool(self.auto_mirror.get())
        label = self.language.get()
        if label in self.language_labels:
            settings.language = i18n.CHOICES[self.language_labels.index(label)]
        warnings = state.exclude_warnings(settings.scan_excludes, settings.scan_roots)
        if warnings:  # D66 8: kept, but said
            messagebox.showwarning(
                _("扫描时跳过的文件夹"),
                "\n".join(warnings) + _("\n\n设置已保存，可以再改。"),
                parent=self,
            )
        self.app.save_settings()
        log.info(
            _(
                "设置已保存：saves root override=%s，"
                "包含 CANDIDATE=%s，包含 tokens=%s，自动镜像=%s，"
                "扫描时跳过 %d 项"
            ),
            settings.saves_root_override or _("<默认>"),
            settings.include_candidates,
            settings.include_tokens,
            settings.auto_mirror and self.app.last_mirror() is not None,
            len(settings.scan_excludes),
        )
        self.close()
        self.app.settings_changed()
        self.app.apply_language()  # D68 3: last, after the dialog is gone
