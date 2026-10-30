"""Exceptions that map to the CLI exit codes of plan section 7."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

STOPPED_EXIT_CODE = 130  # 0.9.0 (D58): Ctrl+C, after the operation stopped at a safe point

StopCheck = Callable[[], bool] | None


class EngineSaveError(Exception):
    exit_code = 3


class UsageError(EngineSaveError):
    """Bad arguments or an unmet safety precondition (exit code 2)."""

    exit_code = 2


class VerificationError(EngineSaveError):
    """The command ran but its result failed verification and was discarded (exit code 1)."""

    exit_code = 1


class FatalError(EngineSaveError):
    """Repository damaged, directories cannot be created, safety snapshot failed (exit 3)."""

    exit_code = 3


class Stopped(BaseException):
    """A long operation was asked to stop and did, at a safe point (0.9.0, D58): between
    folders, files, snapshots, archives or targets, never in the middle of one. A
    ``BaseException``, so that the ``except Exception`` around one item's work does not count
    it as that item's failure; whoever runs the operation catches it. ``partial`` carries what
    was done when the operation hands it back this way (verification)."""

    def __init__(self, partial: Any = None) -> None:
        super().__init__("stopped")
        self.partial = partial


def stop_point(should_stop: StopCheck) -> None:
    """A safe point: raise :class:`Stopped` when ``should_stop`` says so."""
    if should_stop is not None and should_stop():
        raise Stopped
