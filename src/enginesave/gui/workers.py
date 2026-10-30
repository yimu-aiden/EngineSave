"""Background jobs for the window: one at a time, results delivered on the Tk thread.

A job runs on a daemon thread and receives a ``progress(value)`` function. Completion, failure
and progress are queued and handed to the callbacks by :meth:`TaskRunner.poll`, which the
window calls every 100 ms; widgets are therefore only ever touched from the Tk thread.

The worker thread holds nothing but the job and the queue. The callbacks (which reach the
window) stay with the runner on the Tk thread, so the last reference to a Tk object is never
dropped on the worker thread: Tk objects must be destroyed by the thread that created them.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any

Progress = Callable[[Any], None]
Job = Callable[[Progress], Any]
PROGRESS, DONE, ERROR = "progress", "done", "error"


class TaskRunner:
    def __init__(self, on_busy: Callable[[bool, str], None] | None = None) -> None:
        self._events: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self._callbacks: dict[str, Callable[[Any], None] | None] = {}
        self._on_busy = on_busy
        self.busy = False
        self.name = ""

    def run_in_thread(
        self,
        name: str,
        job: Job,
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
        on_progress: Callable[[Any], None] | None = None,
    ) -> bool:
        """Start ``job``; returns False (and does nothing) while another job is running."""
        if self.busy:
            return False
        self._set_busy(True, name)
        self._callbacks = {DONE: on_done, ERROR: on_error, PROGRESS: on_progress}
        events = self._events

        def progress(value: Any) -> None:
            events.put((PROGRESS, value))

        def target() -> None:
            try:
                result = job(progress)
            except BaseException as exc:  # noqa: BLE001 - reported on the Tk thread
                events.put((ERROR, exc))
            else:
                events.put((DONE, result))

        threading.Thread(target=target, name=f"enginesave-{name}", daemon=True).start()
        return True

    def poll(self) -> None:
        """Deliver queued events; call from the Tk thread only."""
        while True:
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                return
            callback = self._callbacks.get(kind)
            if kind != PROGRESS:
                self._callbacks = {}
                self._set_busy(False, "")
            if callback is not None:
                callback(value)

    def _set_busy(self, busy: bool, name: str) -> None:
        self.busy, self.name = busy, name
        if self._on_busy is not None:
            self._on_busy(busy, name)
