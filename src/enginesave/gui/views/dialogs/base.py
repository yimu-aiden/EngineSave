"""What every dialog shares: the modal window with its primary button, background jobs and
"open" links (D36), a scrolled table, and the plain text dialog."""

from __future__ import annotations

import contextlib
import logging
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from enginesave.gui import state
from enginesave.gui.i18n import _
from enginesave.models import DetectedGame, GameStatus, SourceKind

if TYPE_CHECKING:
    from enginesave.gui.app import App

log = logging.getLogger("enginesave.gui")
PAD = 8
TOKENS = SourceKind.MACHINE_TOKENS.value
RETRY_MS = 100  # how often a queued automatic job looks whether the runner is free
TYPING_MS = 400  # a typed path is previewed once typing pauses this long
QueuedJob = tuple[str, Callable[..., Any], Callable[[Any], None], dict[str, Any]]
OPEN_LINK = "打开"  # beside every path a dialog shows (D36)
LINK_COLOR = "#0B57D0"
WARNING_COLOR = "#b3261e"
LINK_COLUMN = ("open", "", 50)


def scrolled_tree(
    parent: tk.Misc, columns: Sequence[tuple[str, str, int]], *, height: int, tree_heading: str = ""
) -> ttk.Treeview:
    """A Treeview with a vertical scroll bar; ``columns`` are ``(id, heading, width)``."""
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    show = "tree headings" if tree_heading else "headings"
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show=show, height=height)
    if tree_heading:
        tree.heading("#0", text=tree_heading)
        tree.column("#0", width=560, stretch=True)
    for column, heading, width in columns:
        tree.heading(column, text=heading)
        tree.column(column, width=width, stretch=width >= 200)
    bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    return tree


class Dialog(tk.Toplevel):
    """Modal window centred on the main window. Escape and the close box cancel; while an
    operation that can stop runs they stop it at its next safe point, like the 「取消」 the
    dialog then shows in its own footer (the main window's is out of reach while the dialog
    is modal, D58), and while any other runs they wait. The primary button (D32) is the
    recommended action: it has the focus and Enter presses it."""

    def __init__(self, app: App, title: str, *, min_size: tuple[int, int] = (520, 260)) -> None:
        super().__init__(app.root)
        if app.root.state() == "withdrawn":
            self.withdraw()  # a hidden main window (tests) never shows dialogs or takes focus
        self.app = app
        self.busy = False
        self.title(title)
        self.transient(app.root)
        self.minsize(*min_size)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _event: self.cancel())
        self.bind("<Return>", self.press_primary)
        self.body = ttk.Frame(self, padding=PAD)
        self.footer = ttk.Frame(self, padding=(PAD, 0, PAD, PAD))
        self.footer.pack(side="bottom", fill="x")
        self.body.pack(side="top", fill="both", expand=True)
        self.actions: list[tk.Misc] = []
        self.traces: list[tuple[tk.Variable, str]] = []
        self.primary_button: ttk.Button | None = None
        self.stop_button: ttk.Button | None = None  # 「取消」 while a job that can stop runs
        self.stoppable = False
        self.timers: list[str] = []  # ``after`` callbacks cancelled on close
        self.queued: QueuedJob | None = None  # the automatic job waiting for the runner
        self.waiting = False
        self.links: dict[str, Path] = {}  # a row or a line → the folder its 「打开」 opens

    def watch(self, variable: tk.Variable, callback: Callable[[], None]) -> None:
        """Call ``callback`` whenever ``variable`` is written, until the dialog closes.

        A trace lives in the Tcl interpreter, not in a widget, so destroying the widgets does
        not remove it; ``close`` does, otherwise the dialog could never be freed.
        """
        name = variable.trace_add("write", lambda *_: callback())
        self.traces.append((variable, name))

    def later(self, delay_ms: int, callback: Callable[[], None]) -> str:
        """``after`` that is cancelled when the dialog closes."""
        timer = self.after(delay_ms, callback)
        self.timers.append(timer)
        return timer

    def button(self, text: str, command: Callable[[], None], *, action: bool = True) -> ttk.Button:
        widget = ttk.Button(self.footer, text=text, command=command)
        widget.pack(side="right", padx=(PAD, 0))
        if action:
            self.actions.append(widget)
        return widget

    def primary(self, text: str, command: Callable[[], None], *, action: bool = True) -> ttk.Button:
        """The recommended action: drawn as the default button, focused, pressed by Enter."""
        widget = self.button(text, command, action=action)
        widget.configure(default="active")
        self.primary_button = widget
        return widget

    def press_primary(self, _event: tk.Event | None = None) -> str:
        """Enter presses the primary button when it can be pressed."""
        button = self.primary_button
        if button is not None and not self.busy and button.instate(["!disabled"]):
            button.invoke()
        return "break"

    def link_column(self, tree: ttk.Treeview, column: str = LINK_COLUMN[0]) -> None:
        """A click on ``column`` of a row opens the folder ``links`` holds for it (D36)."""
        index = f"#{list(tree['columns']).index(column) + 1}"

        def click(event: tk.Event) -> None:
            if (
                tree.identify_region(event.x, event.y) == "cell"
                and tree.identify_column(event.x) == index
            ):
                iid = tree.identify_row(event.y)
                if iid in self.links:
                    self.open_link(iid)

        tree.bind("<ButtonRelease-1>", click, add="+")

    def open_link(self, key: str) -> bool:
        """Open the folder of a row or line in Explorer; one that is gone is a notice."""
        path = self.links.get(key)
        return path is not None and self.app.open_folder(path, parent=self)

    def show(self) -> None:
        """Centre over the main window and make the dialog modal."""
        self.update_idletasks()
        parent = self.app.root
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 3)
        self.geometry(f"+{x}+{y}")
        try:
            self.grab_set()
        except tk.TclError:  # not viewable, e.g. while the main window is hidden in tests
            log.debug("dialog %s shown without a grab", self.title())
        (self.primary_button or self).focus_set()
        self.app.last_dialog = self

    def run(
        self,
        name: str,
        job: Callable[[Callable[[Any], None]], Any],
        on_done: Callable[[Any], None],
        *,
        status: str,
        progress: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        cancellable: bool = False,
        lock: Path | None = None,
    ) -> bool:
        """Run ``job`` through the window's task runner with this dialog's buttons disabled.
        ``lock``: the library the job writes into (1.2.0, D73 4, :meth:`App.run`).

        Errors are shown in a message box unless ``on_error`` handles them (the automatic,
        read-only checks put them into the dialog instead). Returns whether the job started.
        A ``cancellable`` job stops at its safe points when 「取消」 is pressed (D58).
        """

        def done(value: Any) -> None:
            self.busy = False
            self.hide_stop()
            on_done(value)
            self.refresh_if_open()

        def failed(exc: BaseException) -> None:
            self.busy = False
            self.hide_stop()
            if on_error is None:
                self.app.show_error(exc, parent=self)
            else:
                on_error(exc)
            self.refresh_if_open()

        self.busy = True
        if not self.app.run(
            name,
            job,
            done,
            on_error=failed,
            widgets=self.actions,
            progress=progress,
            status=status,
            determinate=progress is not None,
            cancellable=cancellable,
            lock=lock,
        ):
            self.busy = False
            return False
        if cancellable:
            self.show_stop()
        return True

    def refresh_if_open(self) -> None:
        """1.0.0 (D70 2): the buttons after a job -- unless its callback closed the dialog
        (a finished migration does): a destroyed dialog's buttons are gone."""
        try:
            alive = bool(self.winfo_exists())
        except tk.TclError:
            alive = False
        if alive:
            self.refresh_buttons()

    def show_stop(self) -> None:
        """「取消」 at the left of the footer while the job runs; not one of ``actions``, so
        it stays usable."""
        if self.stop_button is None:
            self.stop_button = ttk.Button(self.footer, text=_(state.CANCEL), command=self.stop)
        self.stop_button.configure(text=_(state.CANCEL))
        self.stop_button.state(["!disabled"])
        self.stop_button.pack(side="left")
        self.stoppable = True

    def hide_stop(self) -> None:
        self.stoppable = False
        if self.stop_button is not None:
            with contextlib.suppress(tk.TclError):
                self.stop_button.pack_forget()

    def stop(self) -> None:
        """The running job stops at its next safe point (D58); the button says so until it
        has."""
        if not (self.busy and self.stoppable):
            return
        self.app.cancel_task()
        if self.stop_button is not None:
            self.stop_button.configure(text=_(state.CANCELLING))
            self.stop_button.state(["disabled"])

    def run_soon(
        self,
        name: str,
        job: Callable[[Callable[[Any], None]], Any],
        on_done: Callable[[Any], None],
        **options: Any,
    ) -> None:
        """Start an automatic job as soon as the window's runner is free (D32).

        Only the latest request waits: asking again before it started replaces it.
        """
        self.queued = (name, job, on_done, options)
        if not self.waiting:
            self.waiting = True
            self._start_queued()

    def _start_queued(self) -> None:
        if self.app.runner.busy or self.busy:
            self.later(RETRY_MS, self._start_queued)
            return
        self.waiting = False
        if self.queued is not None:
            name, job, on_done, options = self.queued
            self.queued = None
            self.run(name, job, on_done, **options)

    def refresh_buttons(self) -> None:
        """Re-apply button states that depend on the dialog (overridden where needed)."""

    def cancel(self) -> None:
        if self.busy:
            self.stop()  # a job that can stop does (D58); any other is waited for
            return
        self.close()

    def close(self) -> None:
        if self.app.last_dialog is self:
            self.app.last_dialog = None
        for timer in self.timers:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(timer)
        self.timers.clear()
        self.queued = None
        for variable, name in self.traces:
            with contextlib.suppress(tk.TclError):
                variable.trace_remove("write", name)
        self.traces.clear()
        self.grab_release()
        self.destroy()


class TextDialog(Dialog):
    """Read-only text with a scroll bar (game details); a line naming a folder ends in an
    「打开」 link (D36)."""

    def __init__(self, app: App, title: str, lines: Sequence[tuple[str, str | None]]) -> None:
        super().__init__(app, title, min_size=(760, 520))
        frame = ttk.Frame(self.body)
        frame.pack(fill="both", expand=True)
        widget = tk.Text(frame, wrap="none", width=110, height=32)
        bar = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
        hbar = ttk.Scrollbar(frame, orient="horizontal", command=widget.xview)
        widget.configure(yscrollcommand=bar.set, xscrollcommand=hbar.set)
        widget.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        widget.tag_configure("link", foreground=LINK_COLOR, underline=True)
        widget.tag_bind("link", "<Enter>", lambda _e: widget.configure(cursor="hand2"))
        widget.tag_bind("link", "<Leave>", lambda _e: widget.configure(cursor=""))
        for index, (line, path) in enumerate(lines):
            widget.insert("end", line)
            if path:
                key = f"line{index}"
                self.links[key] = Path(path)
                widget.insert("end", "  ")
                widget.insert("end", _(OPEN_LINK), ("link", key))
                widget.tag_bind(key, "<Button-1>", lambda _e, k=key: self.open_link(k))
            widget.insert("end", "\n")
        widget.configure(state="disabled")
        self.text = widget
        self.primary(_("关闭"), self.cancel, action=False)
        self.show()


def game_details_dialog(app: App, game: DetectedGame) -> TextDialog:
    """The scan's evidence for one game; 「确认 / 关联…」 at the bottom (D48 3)."""
    folder = None
    path = app.repo_path()
    if game.known_game_id and path is not None and (path / "catalog.json").is_file():
        from enginesave.errors import EngineSaveError
        from enginesave.repo import Repo

        with contextlib.suppress(EngineSaveError, OSError):
            folder = Repo.open(path).game_dir(game.known_game_id)  # D67 7
    lines = state.game_detail_lines(game, folder)
    dialog = TextDialog(app, _("详情 - {display_name}", display_name=game.display_name), lines)
    if game.status is not GameStatus.UNSUPPORTED:

        def confirm_link() -> None:
            dialog.close()
            app.confirm_link(game)

        dialog.button(_(state.LINK_TITLE) + "…", confirm_link, action=False)
    return dialog
