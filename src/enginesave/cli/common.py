"""What every subcommand shares: JSON or human output, the saves-root override, the ``yes``
confirmation and the output streams."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from enginesave.engines.renpy.paths import saves_root_override
from enginesave.errors import UsageError
from enginesave.fsutil import atomic_write_text, describe_error, lies_in

Handler = Callable[[argparse.Namespace], int]

# 0.9.0 (D58): Ctrl+C asks the running operation to stop at its next safe point
STOP = threading.Event()
STOPPED_NOTE = "stopped at a safe point: what was done is complete, nothing half-done was left"


def should_stop() -> bool:
    """Ctrl+C was pressed: asked by the running operation at its safe points (D58)."""
    return STOP.is_set()


@contextlib.contextmanager
def ctrl_c_stops() -> Iterator[None]:
    """While a long command runs, the first Ctrl+C asks it to stop at the next safe point
    (between folders, files, snapshots, archives or targets) and the command then exits with
    130; a second Ctrl+C interrupts at once, as before 0.9.0."""

    def handler(_signum: int, _frame: object) -> None:
        if STOP.is_set():
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        STOP.set()
        note = "stopping at the next safe point (Ctrl+C again to interrupt at once)"
        print(note, file=sys.stderr)

    STOP.clear()
    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:  # not the main thread: Ctrl+C stays as it is
        previous = None
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        STOP.clear()


def emit(args: argparse.Namespace, human: str, data: Any) -> None:
    """Human text to stdout, or with ``--json``: JSON to stdout and human text to stderr."""
    if args.json:
        if human:
            print(human, file=sys.stderr)
        print(json.dumps(data, indent=2, ensure_ascii=True))
    else:
        print(human)


def override_from(args: argparse.Namespace) -> Path | None:
    return saves_root_override(args.renpy_saves_root, os.environ)


def interactive(stream: Any) -> bool:
    """A console someone can type into.

    On Windows the NUL device also claims ``isatty()``; ``GetConsoleMode`` on the stream's
    handle tells a real console apart (S-060). A stream without an OS handle is taken at its
    ``isatty()``.
    """
    if stream is None or not stream.isatty():
        return False
    if sys.platform != "win32":
        return True
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return True
    mode = ctypes.c_uint32()
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    return bool(kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))


def confirm(question: str, assume_yes: bool) -> bool:
    """``--yes`` or an interactive ``yes``; a non-interactive stdin without ``--yes`` is refused."""
    if assume_yes:
        return True
    if not interactive(sys.stdin):
        raise UsageError("refusing to continue without --yes in a non-interactive session")
    print(f"{question} Type 'yes' to continue: ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() == "yes"


def split_values(values: list[str] | None) -> list[str]:
    return [part.strip() for value in values or [] for part in value.split(",") if part.strip()]


def checked_report_path(
    args: argparse.Namespace, roots: list[Path], libraries: Sequence[Path] = ()
) -> Path | None:
    """``--report``, refused (exit code 2) inside a scanned root, inside this command's library
    or inside any other EngineSave library -- by its real place too, not only by its name (1.0.0,
    D72 R01: a report written over ``catalog.json`` would replace the library's records)."""
    if not args.report:
        return None
    path = Path(os.path.abspath(args.report))
    if any(lies_in(path, root) for root in roots):
        raise UsageError("--report must not point inside a scanned root")
    if any(lies_in(path, library) for library in libraries):
        raise UsageError("--report must not point inside the backup library")
    for up in path.parents:
        if (up / "catalog.json").is_file():  # a library's catalog: the report is in it
            raise UsageError(f"--report must not point inside an EngineSave library ({up})")
    return path


def write_report(path: Path, data: Mapping[str, Any]) -> str | None:
    """Write the ``--report`` file; ``None``, or what went wrong (1.0.0, D72 R15: the caller
    says so and exits with code 3 instead of a traceback)."""
    from enginesave.report import to_json

    try:
        atomic_write_text(path, to_json(data))
    except OSError as exc:
        return f"the report could not be written to {path}: {describe_error(exc)}"
    return None


def print_progress(index: int, total: int, name: str) -> None:
    sys.stdout.flush()  # keep the order when stdout is a pipe (block-buffered)
    print(f"[{index + 1}/{total}] {name}", file=sys.stderr, flush=True)


def configure_output(verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
