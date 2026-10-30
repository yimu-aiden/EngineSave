"""The search box above the scan page's games and the library page's records (D46).

Typing filters the table once typing pauses for 0.2 seconds; Esc clears it; Ctrl+F (bound by
the window) puts the cursor in it. While it is empty and has no focus it shows a grey hint.
Filtering only hides rows: ticks stay as they are.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from enginesave.gui import state
from enginesave.gui.i18n import _

FILTER_MS = 200  # a typed query is applied once typing pauses this long
HINT_COLOR = "#8a8a8a"


class FilterBox(ttk.Frame):
    def __init__(self, master: tk.Misc, on_change: Callable[[str], None]) -> None:
        super().__init__(master)
        self.on_change = on_change
        self.timer: str | None = None
        self.hinting = False  # the entry shows the grey hint, not a query
        self.quiet = False
        self.var = tk.StringVar(value="")
        self.entry = tk.Entry(self, textvariable=self.var, width=30, relief="solid", bd=1)
        self.entry.pack(side="left", ipady=2)
        self.normal_color = str(self.entry.cget("foreground"))
        self.entry.bind("<FocusIn>", lambda _e: self.hide_hint())
        self.entry.bind("<FocusOut>", lambda _e: self.show_hint())
        self.entry.bind("<Escape>", self.clear)
        self.trace = self.var.trace_add("write", lambda *_: self.typed())
        self.show_hint()

    def destroy(self) -> None:
        """The variable's trace and a pending timer go too: the trace is a Tcl command that
        would keep this box -- and through ``on_change`` its whole page -- alive after the
        window is built again (D68 3)."""
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        self.var.trace_remove("write", self.trace)
        super().destroy()

    @property
    def query(self) -> str:
        return "" if self.hinting else self.var.get()

    def show_hint(self) -> None:
        if not self.var.get() and not self.hinting:
            self.hinting = True
            self.var.set(_(state.SEARCH_PLACEHOLDER))
            self.entry.configure(foreground=HINT_COLOR)

    def hide_hint(self) -> None:
        if self.hinting:
            self.quiet = True  # taking the hint away is not typing
            self.hinting = False
            self.var.set("")
            self.quiet = False
            self.entry.configure(foreground=self.normal_color)

    def typed(self) -> None:
        if self.hinting or self.quiet:
            return
        if self.timer is not None:
            self.after_cancel(self.timer)
        self.timer = self.after(FILTER_MS, self.apply)

    def apply(self) -> None:
        self.timer = None
        self.on_change(self.query)

    def set(self, text: str) -> None:
        """Type ``text`` (and apply it at once)."""
        self.hide_hint()
        self.var.set(text)
        self.apply()

    def clear(self, _event: tk.Event | None = None) -> str:
        """Esc: no query, every row again."""
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        self.hide_hint()
        self.var.set("")
        self.apply()
        return "break"

    def focus(self) -> None:
        """Ctrl+F: the cursor into the box, the query (if any) selected."""
        self.entry.focus_set()
        self.hide_hint()
        self.entry.select_range(0, "end")
