"""EngineSave desktop window and the ``enginesave-gui`` entry point (D22, plan section 17 M10).

The window only calls the library: ``run_scan``, ``prepare_backup``/``execute_backup``,
``plan_restore``/``execute_restore``, ``verify_repo``, ``export_snapshot`` and
``plan_cleanup``/``execute_cleanup``; the scan and backup actions live in ``scan_backup.py``
(mixed into ``App``). Slow calls run on a worker thread whose results are polled every 100 ms;
while one runs, the action buttons are disabled. Every error is shown in a dialog and written
to ``logs\\gui.log`` in the configuration folder (``%APPDATA%\\EngineSave``, or ``config\\``
beside a portable build).
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

from enginesave import __version__
from enginesave.appdirs import assets_dir, carry_over_settings
from enginesave.engines.renpy.paths import default_saves_root, saves_root_override
from enginesave.errors import EngineSaveError
from enginesave.fsutil import path_key
from enginesave.gui import i18n, state
from enginesave.gui.i18n import _, language, set_language
from enginesave.gui.logging_setup import close_logging, setup_logging
from enginesave.gui.scan_backup import ScanAndBackup
from enginesave.gui.settings import load_settings, log_dir, save_settings, settings_path
from enginesave.gui.workers import TaskRunner
from enginesave.models import ScanReport
from enginesave.repo import Repo, destination_state

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont
except ImportError:  # Python installed without Tcl/Tk; main() explains and exits with 3
    tk = None  # type: ignore[assignment]

log = logging.getLogger("enginesave.gui")

TITLE = "EngineSave"
FAILED = "操作失败"  # the title of an error box (looked up where it is shown)
MIN_SIZE = (1000, 650)
DEFAULT_GEOMETRY = "1280x820"
CANCEL = state.CANCEL  # D58
CANCELLING = state.CANCELLING
POLL_MS = 100
PAD = 8
LOG_PANEL_LINES = 2000
NOTICE_BACKGROUND = "#FFF4CE"
NOTICE_BORDER = "#E0B000"
NOTICE_FOREGROUND = "#3B2E00"
TK_MISSING = (
    "找不到 Tk（tkinter），图形界面无法启动。\n\n"
    "请用 python.org 的安装程序“修改”或重新安装 Python，勾选“tcl/tk and IDLE”，"
    "然后按 README 的说明重建 .venv。命令行 enginesave 不受影响。"
)


def native_error(message: str) -> None:
    """Show ``message`` without Tk: a Windows message box, otherwise stderr."""
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)
            return
        except (AttributeError, OSError):
            pass
    print(message, file=sys.stderr)


def enable_dpi_awareness() -> None:
    """Declare system DPI awareness before Tk starts so Windows does not blur the text."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def apply_scaling(root: tk.Tk) -> float:
    """Tk points follow the real DPI (96 dpi → 1.33)."""
    scaling = root.winfo_fpixels("1i") / 72.0
    root.tk.call("tk", "scaling", scaling)
    return scaling


def apply_theme(root: tk.Tk) -> str:
    style = ttk.Style(root)
    theme = "vista" if "vista" in style.theme_names() else "clam"
    style.theme_use(theme)
    return theme


def apply_icon(root: tk.Tk) -> bool:
    """Use ``assets/enginesave.ico`` (source checkout or portable build) for window and taskbar."""
    icon = assets_dir() / "enginesave.ico"
    if not icon.is_file():
        return False
    try:
        root.iconbitmap(default=str(icon))
    except tk.TclError:
        return False
    return True


def set_enabled(widget: Any, enabled: bool) -> None:
    if isinstance(widget, ttk.Widget):
        widget.state(["!disabled"] if enabled else ["disabled"])
    else:
        widget.configure(state="normal" if enabled else "disabled")


class App(ScanAndBackup):
    """The main window: roots and library at the top, two tabs, the log panel and a status bar."""

    def __init__(
        self,
        root: tk.Tk,
        *,
        settings_file: Path | None = None,
        logs: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:

        self.root = root
        self.env = dict(os.environ) if env is None else dict(env)
        self.settings_file = settings_file or settings_path(self.env)
        # D52 3: the parse cache beside gui.json, shared with the command line
        self.cache_file = self.settings_file.parent / "scan-cache.json"
        set_language(i18n.default_language())  # what a damaged gui.json is said in
        self.settings, settings_note = load_settings(self.settings_file)
        set_language(i18n.resolve(self.settings.language))  # D68 2: auto, zh or en
        self.panel_handler, self.file_handler = setup_logging(logs or log_dir(self.env))
        self.log_file = Path(self.file_handler.baseFilename)
        self.runner = TaskRunner(on_busy=self._busy_changed)
        self.stop_event = threading.Event()  # D58: set by 「取消」, read by the running job
        self.report: ScanReport | None = None
        self.changes: dict[str, state.Change] = {}  # D54: new saves since the last snapshot
        self.library_stale = False  # D61 4: another library was chosen after the scan
        self.migration_asked: set[str] = set()  # D67 8: libraries of format 1 asked about
        self.selection = state.Selection()
        self.actions: list[tuple[Any, Callable[[], bool] | None]] = []
        self.last_dialog: Any = None
        self.last_scan = ""
        self.closed = False
        self.closing = False  # D58: closing, waiting for the running job to stop
        self.poll_job: str | None = None
        self.backup_totals: tuple[int, int, int] = (0, 0, 0)

        self._build_window()

        root.title(f"{TITLE} {__version__}")
        root.minsize(*MIN_SIZE)
        try:
            root.geometry(self.settings.window_geometry or DEFAULT_GEOMETRY)
        except tk.TclError:  # D72 R17: a size Tk refuses never keeps the window from opening
            log.warning(_("窗口位置与大小无效，改用默认值：%s"), self.settings.window_geometry)
            self.settings.window_geometry = ""
            root.geometry(DEFAULT_GEOMETRY)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        root.report_callback_exception = self.callback_exception  # type: ignore[method-assign]
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<F5>", lambda _event: None if self.runner.busy else self.scan())
        for key in ("<Control-f>", "<Control-F>"):  # D46: the search box of the page shown
            root.bind(key, lambda _event: self.focus_filter())
        if settings_note:
            log.warning(settings_note)
        log.info(
            _("EngineSave %s 图形界面已启动。设置：%s；日志：%s"),
            __version__,
            self.settings_file,
            self.log_file,
        )
        self.refresh_roots()
        self.refresh_status()
        self.refresh_notice()
        self.refresh_guidance()
        self._poll()
        if self.settings.repo_path:  # read in the background once the window is up; no scan
            root.after_idle(self.library.reload)

    # -------------------------------------------------------------------------------- layout

    def _build_window(self) -> None:
        """Everything in the main window, in the language now (D68)."""
        self.shown_language = language()
        from enginesave.gui.views.library_view import LibraryView
        from enginesave.gui.views.scan_view import ScanView

        self._build_notice()
        self._build_top()
        panes = ttk.Panedwindow(self.root, orient="vertical")
        panes.grid(row=2, column=0, sticky="nsew", padx=PAD, pady=(PAD, 0))
        self.notebook = ttk.Notebook(panes)
        self.scan_view = ScanView(self.notebook, self)
        self.library = LibraryView(self.notebook, self)
        self.notebook.add(self.scan_view, text=_("扫描与备份"))
        self.notebook.add(self.library, text=_("备份库"))
        panes.add(self.notebook, weight=5)
        panes.add(self._build_log_panel(panes), weight=1)
        self._build_status_bar()

    def apply_language(self) -> None:
        """D68 3: the language chosen in the settings dialog, at once -- the main window is
        built again when it is not the one shown."""
        if set_language(i18n.resolve(self.settings.language)) == self.shown_language:
            return
        self.rebuild()
        log.info(_("界面语言：%s"), i18n.NAMES[language()])

    def rebuild(self) -> None:
        """D68 3: the whole main window built again (tabs, tables, buttons, status bar); the
        scan's results, the ticks, the groups opened, the searches, the library and the log
        panel's lines stay."""
        log_lines = self.log_text.get("1.0", "end-1c")
        tab = self.notebook.index(self.notebook.select())
        group_open = dict(self.scan_view.group_open)
        searches = (self.scan_view.filter.query, self.library.filter.query)
        # D70 5: the library page's ticks, selected record and last verification too
        checked = set(self.library.checked)
        current = self.library.selected_record()
        selected = current.game_id if current is not None else None
        verified = self.library.last_verified
        for child in self.root.winfo_children():
            child.destroy()
        self.actions = []
        self._build_window()
        self.log_text.configure(state="normal")
        self.log_text.insert("1.0", log_lines)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")
        self.scan_view.group_open.update(group_open)
        for page, query in zip((self.scan_view, self.library), searches, strict=True):
            if query:
                page.filter.set(query)
        if self.report is not None:
            self.scan_view.load(self.report)
        self.notebook.select(tab)
        self.refresh_roots()
        self.refresh_status()
        self.refresh_notice()
        self.refresh_guidance()
        self.refresh_actions()
        if verified is not None:
            self.library.show_verified(*verified)
        if self.settings.repo_path:

            def restore() -> None:
                self.library.checked = checked & {r.game_id for r in self.library.records.values()}
                self.library.show_rows(selected)

            self.library.reload(then=restore)

    def _build_notice(self) -> None:
        """Yellow bar above everything while a saves-root override is in effect (S-065)."""
        self.notice = tk.Frame(
            self.root,
            background=NOTICE_BACKGROUND,
            highlightbackground=NOTICE_BORDER,
            highlightthickness=1,
        )
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self.notice_var = tk.StringVar()
        tk.Label(
            self.notice,
            textvariable=self.notice_var,
            background=NOTICE_BACKGROUND,
            foreground=NOTICE_FOREGROUND,
            font=bold,
            anchor="w",
            justify="left",
        ).pack(side="left", fill="x", expand=True, padx=PAD, pady=6)
        self.notice_clear = ttk.Button(self.notice, text=_("清空"), command=self.clear_override)
        self.notice_clear.pack(side="right", padx=PAD, pady=4)
        self.notice.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(PAD, 0))
        self.register_action(self.notice_clear)

    def _build_top(self) -> None:
        top = ttk.Frame(self.root, padding=(PAD, PAD, PAD, 0))
        top.grid(row=1, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text=_("扫描根目录：")).grid(row=0, column=0, sticky="nw", pady=(2, 0))
        roots = ttk.Frame(top)
        roots.grid(row=0, column=1, sticky="ew")
        self.roots_list = tk.Listbox(roots, height=3, activestyle="none", selectmode="extended")
        bar = ttk.Scrollbar(roots, orient="vertical", command=self.roots_list.yview)
        self.roots_list.configure(yscrollcommand=bar.set)
        self.roots_list.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        buttons = ttk.Frame(top)
        buttons.grid(row=0, column=2, sticky="n", padx=(PAD, 0))
        add = ttk.Button(buttons, text=_("添加…"), command=self.add_root)
        add.pack(fill="x")
        remove = ttk.Button(buttons, text=_("移除"), command=self.remove_root)
        remove.pack(fill="x", pady=(4, 0))

        ttk.Label(top, text=_("备份库：")).grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.repo_var = tk.StringVar(value=self.settings.repo_path)
        ttk.Entry(top, textvariable=self.repo_var, state="readonly").grid(
            row=1, column=1, sticky="ew", pady=(PAD, 0)
        )
        browse = ttk.Button(top, text=_("浏览…"), command=self.choose_repo)
        browse.grid(row=1, column=2, sticky="ew", padx=(PAD, 0), pady=(PAD, 0))

        actions = ttk.Frame(top)
        actions.grid(row=0, column=3, rowspan=2, sticky="ns", padx=(PAD * 2, 0))
        self.scan_button = ttk.Button(actions, text=_("扫描 (F5)"), command=self.scan)
        self.scan_button.pack(fill="x")
        settings_button = ttk.Button(actions, text=_("设置…"), command=self.open_settings)
        settings_button.pack(fill="x", pady=(4, 0))
        for widget in (add, remove, browse, self.scan_button, settings_button):
            self.register_action(widget)

    def _build_log_panel(self, master: tk.Misc) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(master, text=_("日志"), padding=4)
        self.log_text = tk.Text(frame, height=7, wrap="none", state="disabled")
        ybar = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return frame

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=(PAD, 4, PAD, PAD))
        bar.grid(row=3, column=0, sticky="ew")
        self.status_var = tk.StringVar(value=_("就绪"))
        self.scan_var = tk.StringVar()
        self.repo_status_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        # D58: shown while a job that can stop runs; it stops at the job's next safe point
        self.cancel_button = ttk.Button(bar, text=_(CANCEL), command=self.cancel_task)
        self.progress = ttk.Progressbar(bar, length=220, mode="indeterminate")
        self.progress.pack(side="right")
        ttk.Label(bar, textvariable=self.repo_status_var).pack(side="right", padx=(PAD * 2, PAD))
        ttk.Label(bar, textvariable=self.scan_var).pack(side="right", padx=PAD)

    def focus_filter(self) -> str:
        """Ctrl+F: the cursor into the search box of the page shown (D46)."""
        page = self.library if self.notebook.select() == str(self.library) else self.scan_view
        page.filter.focus()
        return "break"

    def register_action(self, widget: Any, when: Callable[[], bool] | None = None) -> None:
        """Buttons that start work; they are disabled while a job runs, and also while
        ``when`` says there is nothing for them to act on (D33 ②)."""
        self.actions.append((widget, when))
        if when is not None:
            set_enabled(widget, not self.runner.busy and when())

    def refresh_actions(self) -> None:
        """Re-apply the ``when`` rules after the ticks or the selection changed."""
        busy = self.runner.busy
        for widget, when in self.actions:
            set_enabled(widget, not busy and (when is None or when()))

    # ------------------------------------------------------------------------ infrastructure

    def run(
        self,
        name: str,
        job: Callable[[Callable[[Any], None]], Any],
        on_done: Callable[[Any], None],
        *,
        on_error: Callable[[BaseException], None] | None = None,
        widgets: Iterable[Any] = (),
        progress: Callable[[Any], None] | None = None,
        status: str | None = None,
        determinate: bool = False,
        cancellable: bool = False,
    ) -> bool:
        """Run ``job`` on the worker thread; ``widgets`` are disabled until it ends. A
        ``cancellable`` job checks :meth:`stop_requested` at its safe points (D58): 「取消」
        shows beside the progress bar while it runs."""
        if self.closing:  # D58: the window waits for the running job to stop, nothing more
            return False
        if self.runner.busy:
            messagebox.showinfo(
                TITLE, _("“{name}”还在进行，请稍候。", name=self.runner.name), parent=self.root
            )
            return False
        extra = list(widgets)
        for widget in extra:
            set_enabled(widget, False)

        def finish() -> None:
            for widget in extra:
                with contextlib.suppress(tk.TclError):  # the dialog may be gone
                    set_enabled(widget, True)

        def done(value: Any) -> None:
            finish()
            on_done(value)

        def failed(exc: BaseException) -> None:
            finish()
            (on_error or self.show_error)(exc)

        self.stop_event.clear()
        self.status_var.set(status or _("正在{name}…", name=name))
        self.progress.configure(mode="determinate" if determinate else "indeterminate", value=0)
        if not determinate:
            self.progress.start(15)
        started = self.runner.run_in_thread(name, job, done, failed, progress)
        if started and cancellable:
            self.cancel_button.configure(text=_(CANCEL))
            self.cancel_button.state(["!disabled"])
            self.cancel_button.pack(side="right", padx=(PAD, 0), before=self.progress)
        return started

    def cancel_task(self) -> None:
        """「取消」 (D58): the running job stops at its next safe point; the button says so
        until it has."""
        if not self.runner.busy or self.stop_event.is_set():
            return
        self.stop_event.set()
        self.cancel_button.configure(text=_(CANCELLING))
        self.cancel_button.state(["disabled"])
        self.status_var.set(_("正在取消“{name}”，等它停在安全点…", name=self.runner.name))
        log.info(_("取消“%s”：等它停在安全点"), self.runner.name)

    def stop_requested(self) -> bool:
        """D58: whether the running job was asked to stop (checked at its safe points, on the
        worker thread)."""
        return self.stop_event.is_set()

    def mirror_progress(self, value: tuple[int, int, str]) -> None:
        """「镜像到…」 (D57): files done of all, in the status bar."""
        done, total, name = value
        self.progress.configure(maximum=max(total, 1), value=done)
        self.status_var.set(
            _("正在镜像 {name}（{done}/{total}）…", name=name, done=done, total=total)
        )

    def _busy_changed(self, busy: bool, _name: str) -> None:
        self.refresh_actions()
        if not busy:
            self.cancel_button.pack_forget()
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.status_var.set(_("就绪"))

    def _poll(self) -> None:
        if self.closed:
            return
        try:
            self.runner.poll()  # a job's callbacks run here, message boxes included
            if self.closed:
                return
            self._drain_log()
            if self.closing and not self.runner.busy:  # D58: the job has stopped; close now,
                self.on_close()  # here and never from inside a message box's own loop
        finally:
            if not self.closed:
                self.poll_job = self.root.after(POLL_MS, self._poll)

    def stop_polling(self) -> None:
        """Cancel the next poll, so that nothing fires once the window is gone."""
        if self.poll_job is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self.poll_job)
            self.poll_job = None

    def _drain_log(self) -> None:
        lines = self.panel_handler.drain()
        if not lines:
            return
        self.log_text.configure(state="normal")
        for line in lines:
            self.log_text.insert("end", line + "\n")
        excess = int(self.log_text.index("end-1c").split(".")[0]) - LOG_PANEL_LINES
        if excess > 0:
            self.log_text.delete("1.0", f"{excess + 1}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def show_error(
        self, exc: BaseException | str, *, parent: Any = None, title: str = FAILED
    ) -> None:
        """Every failure is shown and logged; library errors keep their English text."""
        title = _(title)  # D68: a default is evaluated once, in Chinese
        if isinstance(exc, EngineSaveError):
            message = str(exc)
            log.error(_("%s：%s"), title, message)
        elif isinstance(exc, BaseException):
            message = f"{type(exc).__name__}: {exc}"
            log.error(_("%s：%s"), title, message, exc_info=exc)
        else:
            message = exc
            log.error(_("%s：%s"), title, message)
        messagebox.showerror(title, message, parent=parent or self.root)

    def info(self, message: str, *, title: str = TITLE) -> None:
        messagebox.showinfo(title, message, parent=self.root)

    def callback_exception(
        self, kind: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        if self.closed and issubclass(kind, tk.TclError):  # a late callback of a closed window
            log.debug("关闭后的回调：%s: %s", kind.__name__, exc)
            return
        log.error(_("界面内部错误：%s: %s"), kind.__name__, exc, exc_info=(kind, exc, tb))
        messagebox.showerror(
            _("内部错误"),
            _(
                "{name__}: {exc}\n\n详情已写入日志：{log_file}",
                name__=kind.__name__,
                exc=exc,
                log_file=self.log_file,
            ),
            parent=self.root,
        )

    def save_settings(self) -> None:
        try:
            save_settings(self.settings, self.settings_file)
        except OSError as exc:
            self.show_error(exc, title=_("设置无法保存"))

    def open_folder(self, path: Path | str, *, parent: Any = None) -> bool:
        """Open a folder in Explorer (D36); a folder that is not there is a notice, not an
        error. Returns whether it was opened."""
        where = Path(path)
        if not where.is_dir():
            log.info(_("要打开的文件夹不存在：%s"), where)
            messagebox.showinfo(
                _("打开文件夹"),
                _("这个文件夹不存在：\n{where}", where=where),
                parent=parent or self.root,
            )
            return False
        try:
            os.startfile(where)  # type: ignore[attr-defined]  # Windows only
        except (AttributeError, OSError) as exc:
            self.show_error(exc, parent=parent, title=_("无法打开文件夹"))
            return False
        return True

    def copy_path(self, path: Path | str) -> None:
        """Put a path on the clipboard (D36 「复制路径」)."""
        self.root.clipboard_clear()
        self.root.clipboard_append(str(path))
        self.status_var.set(_("已复制路径：{path}", path=path))

    def on_close(self) -> None:
        """Closing while a job runs (D58): it is asked to stop at its next safe point and the
        window closes once it has (the poll does it, after the job's own callbacks), so
        nothing is left half-done (a job without safe points, always a short one, is waited
        for)."""
        if self.runner.busy:
            if not self.closing:
                self.closing = True
                self.stop_event.set()
                self.cancel_button.configure(text=_(CANCELLING))
                self.cancel_button.state(["disabled"])
                self.status_var.set(_("正在关闭：等“{name}”停在安全点…", name=self.runner.name))
                log.info(_("正在关闭：先等“%s”停下"), self.runner.name)
            return
        if self.root.state() == "normal":
            self.settings.window_geometry = self.root.geometry()
        self.save_settings()
        log.info(_("图形界面已关闭。"))
        self.closed = True
        self.stop_polling()
        close_logging(self.panel_handler, self.file_handler)
        self.root.destroy()

    # ---------------------------------------------------------------------------- settings

    def saves_override(self) -> Path | None:
        """The window's override, else ``ENGINESAVE_RENPY_SAVES_ROOT`` (as for the CLI)."""
        return self.settings.override_path or saves_root_override(None, self.env)

    def default_saves_root(self) -> Path:
        return default_saves_root(self.env, saves_root_override(None, self.env))[0]

    def repo_path(self) -> Path | None:
        return Path(self.settings.repo_path) if self.settings.repo_path else None

    def last_mirror(self) -> dict[str, str] | None:
        """D64: this library's last mirror that worked (``mirrors.json``), or ``None``."""
        from enginesave.mirror import last_mirror

        path = self.repo_path()
        return last_mirror(self.settings_file.parent, path) if path is not None else None

    def refresh_roots(self) -> None:
        self.roots_list.delete(0, "end")
        for root in self.settings.scan_roots:
            self.roots_list.insert("end", root)

    def refresh_status(self) -> None:
        repo = self.settings.repo_path
        self.repo_status_var.set(_("备份库：{repo}", repo=repo) if repo else _("未选择备份库"))
        override = self.saves_override()
        saves = _("    全局存档根目录：{override}", override=override) if override else ""
        self.scan_var.set(
            (
                _("上次扫描：{last_scan}", last_scan=self.last_scan)
                if self.last_scan
                else _("尚未扫描")
            )
            + saves
        )

    @property
    def notice_visible(self) -> bool:
        return bool(self.notice.winfo_manager())

    def refresh_notice(self) -> None:
        """Show the override bar while an override is in effect, hide it otherwise."""
        notice = state.override_notice(self.settings, self.env)
        if notice is None:
            self.notice.grid_remove()
            return
        self.notice_var.set(notice.text)
        if notice.clearable:
            self.notice_clear.pack(side="right", padx=PAD, pady=4)
        else:
            self.notice_clear.pack_forget()
        self.notice.grid()

    def clear_override(self) -> None:
        if self.settings.override_path is None:
            return
        log.info(_("已恢复使用真实的全局存档根目录（之前改为 %s）"), self.settings.override_path)
        self.settings.saves_root_override = None
        self.save_settings()
        self.settings_changed()

    def library_old(self) -> bool:
        """D67 8: the chosen library is of format 1 (not migrated yet)."""
        path = self.repo_path()
        if path is None or not (path / "catalog.json").is_file():
            return False
        try:
            return Repo(path).old_format
        except EngineSaveError:
            return False

    def writable_library(self) -> bool:
        """Before a write into the library: one of format 1 offers its migration again and
        the write does not start (D67 8)."""
        if not self.library_old():
            return True
        path = self.repo_path()
        assert path is not None
        try:
            self.offer_migration(Repo.open(path), again=True)
        except EngineSaveError as exc:
            self.show_error(exc)
        return False

    def offer_migration(self, repo: Repo, *, again: bool) -> None:
        """The migration dialog for a library of format 1: once when it is opened, and again
        (``again``) when a write is asked for."""
        key = path_key(repo.root)
        if (key in self.migration_asked and not again) or self.runner.busy:
            return
        self.migration_asked.add(key)
        from enginesave.gui.views.dialogs import MigrateDialog
        from enginesave.migrate import plan_migration

        try:
            plan = plan_migration(repo)
        except EngineSaveError as exc:
            self.show_error(exc)
            return
        if plan.needed:
            MigrateDialog(self, repo, plan)

    def refresh_guidance(self) -> None:
        """The banner: the first steps, or after a scan what is ticked and what comes next."""
        counts = self.selection.counts()
        self.scan_view.show_guidance(state.guidance(self.settings, self.report, counts))

    def open_settings(self) -> None:
        from enginesave.gui.views.dialogs import SettingsDialog

        SettingsDialog(self)

    def settings_changed(self) -> None:
        self.refresh_status()
        self.refresh_notice()
        if self.report is not None:
            log.info(_("设置已改变；重新扫描后预览备份才会使用新设置。"))

    def add_root(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root, title=_("添加扫描根目录"), mustexist=True
        )
        if not chosen:
            return
        path = os.path.normpath(chosen)
        if any(path_key(path) == path_key(r) for r in self.settings.scan_roots):
            return
        self.settings.scan_roots.append(path)
        self.save_settings()
        self.refresh_roots()
        self.refresh_guidance()
        self.refresh_notice()  # M44 review: a root token on this folder is a conflict
        log.info(_("添加扫描根目录：%s"), path)
        from enginesave.discovery import is_excluded

        if is_excluded(Path(path), self.settings.scan_excludes):  # D63
            self.info(state.root_excluded_text(path))

    def remove_root(self) -> None:
        chosen = self.roots_list.curselection()
        if not chosen:
            self.info(_("先在扫描根目录列表中选中要移除的目录。"))
            return
        for index in sorted(chosen, reverse=True):
            removed = self.settings.scan_roots.pop(index)
            log.info(_("移除扫描根目录：%s"), removed)
        self.save_settings()
        self.refresh_roots()
        self.refresh_guidance()
        self.refresh_notice()

    def choose_repo(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root, title=_("选择备份库文件夹（可新建一个空文件夹）"), mustexist=False
        )
        if chosen:
            self.set_repo(Path(os.path.normpath(chosen)))

    def set_repo(self, path: Path) -> bool:
        """Use ``path`` as the library; an absent or empty folder is created after asking."""
        try:
            existing = destination_state(path) == "existing"
        except EngineSaveError as exc:
            self.show_error(exc, title=_("不能用作备份库"))
            return False
        if not existing:
            if not messagebox.askyesno(
                _("新建备份库"),
                _("{path}\n\n还不是 EngineSave 备份库。要在这里新建吗？", path=path),
                parent=self.root,
            ):
                return False
            from enginesave.backup import prepare_repository

            try:
                prepare_repository(
                    path,
                    [Path(r) for r in self.settings.scan_roots],
                    saves_root_override=self.saves_override(),
                    env=self.env,
                )
            except EngineSaveError as exc:
                self.show_error(exc, title=_("不能在这里新建备份库"))
                return False
            log.info(_("已新建备份库：%s"), path)
        previous = self.settings.repo_path
        self.settings.repo_path = str(path)
        self.repo_var.set(str(path))
        self.save_settings()
        self.refresh_status()
        self.refresh_guidance()
        self.refresh_notice()
        log.info(_("备份库：%s"), path)
        self.library.reload()
        if not previous or path_key(previous) != path_key(path):
            self.library_switched()  # D54: the scan page compared with the old one
        return True


def self_test(app: App, timeout: float = 60.0) -> int:
    """Close a window built hidden: wait for start-up work, save the settings, report 0."""
    deadline = time.monotonic() + timeout
    app.root.update()  # runs the idle work queued at start-up (reading the library)
    while app.runner.busy and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.02)
    if app.runner.busy:
        log.error(_("自检失败：“%s”在 %d 秒内没有结束。"), app.runner.name, timeout)
        app.closed = True
        close_logging(app.panel_handler, app.file_handler)
        app.root.destroy()
        return 1
    log.info(_("自检通过（--self-test）：窗口在隐藏状态下建立并关闭。设置：%s"), app.settings_file)
    app.on_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """``enginesave-gui`` / ``EngineSave.exe``: open the window; exit code 3 when Tk is unavailable.

    ``--self-test`` builds the window without ever showing it, closes it again (which writes
    the settings file) and exits with 0, so a portable build can be checked without a window.
    Arguments are read by hand: a windowed program has no console for argparse's messages.
    """
    args = sys.argv[1:] if argv is None else argv
    testing = "--self-test" in args
    report = (lambda _text: None) if testing else native_error
    # D70 8: what is said before the window (and its settings) exist is in Windows' language
    set_language(i18n.default_language())
    if tk is None:
        report(_(TK_MISSING))
        return 3
    enable_dpi_awareness()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        report(_("{TK_MISSING}\n\n（{exc}）", TK_MISSING=_(TK_MISSING), exc=exc))
        return 3
    if testing:
        root.withdraw()  # before anything is drawn: the self-test never shows a window
    apply_scaling(root)
    apply_theme(root)
    apply_icon(root)
    carried = carry_over_settings()  # renamed from SaveTrail: its settings, once
    try:
        app = App(root)
    except Exception as exc:  # noqa: BLE001 - the window could not be built at all
        logging.getLogger("enginesave.gui").error(_("图形界面启动失败"), exc_info=exc)
        report(_("图形界面启动失败：{name__}: {exc}", name__=type(exc).__name__, exc=exc))
        root.destroy()
        return 3
    if carried:
        logging.getLogger("enginesave.gui").info(
            _("已沿用 SaveTrail 的设置：{names}", names=_("、").join(carried))
        )
    if testing:
        return self_test(app)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
