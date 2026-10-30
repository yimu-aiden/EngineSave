"""``execute_cleanup``: every check again right before each folder, then the recycle bin or
a permanent delete, and the record in ``game.json`` and the catalog."""

from __future__ import annotations

import ctypes
import os
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from enginesave import layout
from enginesave.backup import update_catalog_entry
from enginesave.cleanup import windows
from enginesave.cleanup.model import (
    DELETING_SUFFIX,
    GAME_ROOT,
    GLOBAL,
    GONE,
    KINDS,
    MAX_LISTED,
    RECYCLE,
    SKIPPED_FULL_BIN,
    CleanupPlan,
    CleanupResult,
    CleanupTarget,
    RecycleLedger,
)
from enginesave.cleanup.plan import plan_cleanup, requested, same_path
from enginesave.cleanup.windows import FO_DELETE, RECYCLE_FLAGS, SHFILEOPSTRUCTW
from enginesave.errors import StopCheck, UsageError
from enginesave.fsutil import describe_error, iso_utc, long_path, utc_now

# ------------------------------------------------------------------------------- execution


def _recycle(path: Path) -> str | None:
    """Move ``path`` to the recycle bin; ``None`` on success, else the error (never deletes)."""
    buffer = ctypes.create_unicode_buffer(str(path) + "\0")  # the buffer adds the second NUL
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.addressof(buffer)
    operation.fFlags = RECYCLE_FLAGS
    try:
        code = windows.shell_file_operation(operation)
    except OSError as exc:
        return describe_error(exc)
    if code != 0:
        return f"the recycle bin refused the folder (error {code:#x}); nothing was deleted"
    if operation.fAnyOperationsAborted:
        return "the move to the recycle bin was aborted"
    if path.exists():
        return "the folder is still there after the move to the recycle bin"
    return None


def _delete(path: Path, stamp: str) -> tuple[str | None, list[str]]:
    """Rename, then remove: ``(error, leftovers)``. A failed rename stops before deleting."""
    doomed = path.with_name(f"{path.name}{DELETING_SUFFIX}{stamp}")
    try:
        windows.rename_dir(long_path(path), long_path(doomed))
    except OSError as exc:
        return f"could not rename the folder (still in use?): {describe_error(exc)}", []
    leftovers: list[str] = []

    def retry(function: Callable[..., Any], failed: str, _exc: BaseException) -> None:
        try:
            os.chmod(failed, stat.S_IWRITE)  # read-only files
            function(failed)
        except OSError:
            leftovers.append(failed)

    shutil.rmtree(long_path(doomed), onexc=retry)
    if os.path.exists(long_path(doomed)):
        return f"some files could not be deleted; they are left in {doomed}", leftovers
    return None, []


def _record(plan: CleanupPlan, result: CleanupResult) -> None:
    """What was attempted goes into ``cleanups``; a folder that was gone or left for later
    (a full recycle bin, D41) was not touched and is not recorded."""
    attempted = [t for t in result.targets if t["result"] not in (GONE, SKIPPED_FULL_BIN)]
    if not attempted:
        return
    repo = plan.repo
    doc = repo.load_game(plan.game_id)
    doc.setdefault("cleanups", []).append(
        {
            "at": result.at,
            "snapshot_id": plan.snapshot_id,
            "method": plan.method,
            "targets": [
                {k: t[k] for k in ("kind", "path", "bytes", "result", "at") if k in t}
                for t in attempted
            ],
        }
    )
    for item in result.targets:
        if item["kind"] == GAME_ROOT and item["result"] in ("recycled", "deleted"):
            for install in doc.get("installs", []):
                if install.get("game_root") and same_path(install["game_root"], item["path"]):
                    install["removed_at"] = result.at
    doc["updated_at"] = result.at
    repo.save_game(doc)
    update_catalog_entry(repo, repo.load_catalog(), doc)


def recheck_request(plan: CleanupPlan, target: CleanupTarget) -> tuple[str, str | None]:
    """The request that names exactly this folder again: an install by its path (the record's
    "only existing install" may be gone by now), the others as they were asked for."""
    if target.kind == GAME_ROOT:
        return GAME_ROOT, target.path
    return next(r for r in plan.requests if requested(r, target))


def execute_cleanup(
    plan: CleanupPlan,
    *,
    skip_failed: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: StopCheck = None,
) -> CleanupResult:
    """Remove the folders that passed; each is checked again right before it is removed.
    ``should_stop`` is asked before each folder, never during one (D58): the folders done stay
    done and are recorded; the rest are not listed and ``cancelled`` says so.

    Recycling counts what the run has moved into each drive's bin (``plan.ledger``, shared by
    a batch, D41): a folder that does not fit is left for later (``skipped_full_bin``, not a
    failure) and so are the folders after it on that drive; ``result.full_bins`` says how
    much room each such drive is missing.
    """
    plan.repo.require_writable()  # D67 8: nothing is deleted that could not be recorded
    ledger = plan.ledger
    if not plan.passed or (not plan.ok and not skip_failed):
        raise UsageError("folders failed the checks; nothing was deleted")
    moment = utc_now()
    result = CleanupResult(iso_utc(moment), plan.method)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    # Install folders first: the shared check of a global folder then sees them gone.
    chosen = sorted(plan.passed, key=lambda t: KINDS.index(t.kind))
    for index, target in enumerate(chosen):
        if should_stop is not None and should_stop():
            result.cancelled = True
            break
        if progress is not None:
            progress(index, len(chosen), target.path)
        entry = {
            "kind": target.kind,
            "path": target.path,
            "bytes": target.bytes,
            "result": "failed",
            "error": None,
            "leftovers": [],
        }
        result.targets.append(entry)
        drive = Path(target.path).anchor.upper()
        waiting_install = target.kind == GLOBAL and any(
            t["kind"] == GAME_ROOT and t["result"] == SKIPPED_FULL_BIN for t in result.targets
        )
        if plan.method == RECYCLE and (drive in ledger.full or waiting_install):
            entry["result"] = SKIPPED_FULL_BIN  # its drive is full, or its game folder waits
            continue
        again = plan_cleanup(
            plan.repo,
            {"game_id": plan.game_id},
            [recheck_request(plan, target)],
            snapshot_id=plan.snapshot_id or "latest",
            method=plan.method,
            recycle_limit=plan.recycle_limit,
            ledger=ledger,
            **plan.options,
        ).targets[0]
        if not again.active:  # removed meanwhile: nothing to do, and no failure (D41)
            entry["result"] = GONE
            continue
        if not again.ok:
            entry["error"] = "changed since the check: " + "; ".join(again.reasons[:3])
            continue
        path = Path(target.path)
        if plan.method == RECYCLE and plan.recycle_limit is None:
            if not ledger.fits(drive, target.bytes):
                ledger.full.add(drive)  # the folders after it on this drive wait too
                entry["result"] = SKIPPED_FULL_BIN
                continue
        if plan.method == RECYCLE:
            error = _recycle(path)
            entry["result"], entry["error"] = ("failed", error) if error else ("recycled", None)
            if not error:
                ledger.place(drive, target.bytes)
        else:
            error, leftovers = _delete(path, stamp)
            entry["result"] = "failed" if error else "deleted"
            entry["error"], entry["leftovers"] = error, leftovers[:MAX_LISTED]
        entry["at"] = iso_utc(utc_now())  # each folder's own time (matching the recycle bin, D41)
    _record(plan, result)
    layout.write_index(plan.repo)  # D67 5: the game folder is gone
    if plan.method == RECYCLE:  # what the bins hold now (D35)
        drives = sorted({Path(t["path"]).anchor.upper() for t in result.targets} - {""})
        result.recycle_bins = [windows.recycle_bin_info(drive) for drive in drives]
        result.full_bins = full_bins(ledger, result.skipped)
    return result


def full_bins(ledger: RecycleLedger, skipped: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per drive with folders left for later: the room left, what they need, what is missing."""
    needed: dict[str, int] = {}
    for item in skipped:
        drive = Path(item["path"]).anchor.upper()
        needed[drive] = needed.get(drive, 0) + int(item["bytes"])
    report = []
    for drive, size in sorted(needed.items()):
        room = max(ledger.room(drive) or 0, 0)
        report.append(
            {"drive": drive, "room": room, "needed": size, "missing": max(size - room, 0)}
        )
    return report
