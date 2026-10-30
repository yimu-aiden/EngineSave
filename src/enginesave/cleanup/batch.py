"""Several records at once, and what to do with the folders a full recycle bin left for later
(D41): the window's cleanup dialog and ``enginesave cleanup --on-full-bin`` share these.

:func:`run_batch` checks and cleans up record after record with one :class:`RecycleLedger`, so
the room in each bin adds up across the batch. :func:`waiting` lists what was left for later;
after room was made (purging EngineSave's own earlier items, :mod:`enginesave.cleanup.recycle_bin`)
:func:`resume` recycles them, or deletes the chosen ones permanently, each checked again.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from enginesave.cleanup.execute import execute_cleanup, recheck_request
from enginesave.cleanup.model import (
    PERMANENT,
    RECYCLE,
    CleanupPlan,
    CleanupResult,
    RecycleLedger,
)
from enginesave.cleanup.plan import plan_cleanup
from enginesave.errors import StopCheck
from enginesave.fsutil import path_key
from enginesave.repo import Repo

Outcome = tuple[CleanupPlan, CleanupResult | None]  # no result: the check before failed
Progress = Callable[[int, int, str], None]
Group = tuple[str, str | None, Sequence[tuple[str, str | None]]]  # game id, snapshot, requests


class Outcomes(list[Outcome]):
    """What :func:`run_batch` and :func:`resume` did, record by record; ``cancelled`` when
    they were told to stop (D58) before getting through, whether between records or between
    the folders of one."""

    cancelled: bool = False


def cancelled(outcomes: Sequence[Outcome]) -> bool:
    """The batch stopped before it got through (a plain list never did)."""
    return bool(getattr(outcomes, "cancelled", False)) or any(
        result is not None and result.cancelled for _plan, result in outcomes
    )


@dataclass(frozen=True)
class Waiting:
    """A folder left for later because its drive's recycle bin was full."""

    game_id: str
    display_name: str
    snapshot_id: str | None
    kind: str
    path: str
    bytes: int
    request: tuple[str, str | None]

    @property
    def drive(self) -> str:
        return Path(self.path).anchor.upper()


def run_batch(
    repo: Repo,
    groups: Sequence[Group],
    *,
    method: str,
    options: dict[str, Any],
    ledger: RecycleLedger | None = None,
    progress: Progress | None = None,
    should_stop: StopCheck = None,
) -> Outcomes:
    """Check each record again (the snapshot that was checked) and clean up the folders that
    pass, record by record; a record whose check fails is not touched (``None`` result). Told
    to stop (D58), it does not start the next folder or record, and says so (``cancelled``)."""
    ledger = ledger if ledger is not None else RecycleLedger()
    total = sum(len(requests) for _gid, _sid, requests in groups)
    outcomes = Outcomes()
    offset = 0
    for game_id, snapshot_id, requests in groups:
        if should_stop is not None and should_stop():
            outcomes.cancelled = True
            break
        again = plan_cleanup(
            repo,
            {"game_id": game_id},
            list(requests),
            snapshot_id=snapshot_id or "latest",
            method=method,
            ledger=ledger,
            **options,
        )
        if not again.ok:
            outcomes.append((again, None))
            continue

        def step(index: int, _count: int, path: str, base: int = offset) -> None:
            if progress is not None:
                progress(base + index, total, path)

        done = execute_cleanup(again, progress=step, should_stop=should_stop)
        outcomes.append((again, done))
        offset += len(again.targets)
        if done.cancelled:
            outcomes.cancelled = True
            break
    return outcomes


def waiting(outcomes: Sequence[Outcome]) -> list[Waiting]:
    """The folders the batch left for later, with the request that names each again."""
    found = []
    for plan, result in outcomes:
        for entry in result.skipped if result is not None else []:
            target = next(
                t
                for t in plan.targets
                if t.kind == entry["kind"] and path_key(t.path) == path_key(entry["path"])
            )
            found.append(
                Waiting(
                    plan.game_id,
                    plan.display_name,
                    plan.snapshot_id,
                    target.kind,
                    target.path,
                    target.bytes,
                    recheck_request(plan, target),
                )
            )
    return found


def resume(
    repo: Repo,
    items: Sequence[Waiting],
    *,
    permanent: Collection[str] = (),
    options: dict[str, Any],
    progress: Progress | None = None,
    should_stop: StopCheck = None,
) -> Outcomes:
    """Clean up what was left for later: the folders whose path is in ``permanent`` are
    deleted permanently, the others recycled. The bins are read again (room may have been
    made), every folder is checked again, and what still does not fit waits again. Told to
    stop (D58), it goes no further; :func:`still_waiting` keeps what it did not reach."""
    keys = {path_key(p) for p in permanent}
    groups: dict[tuple[str, str], list[Waiting]] = {}
    for item in items:
        method = PERMANENT if path_key(item.path) in keys else RECYCLE
        groups.setdefault((method, item.game_id), []).append(item)
    ledger = RecycleLedger()
    outcomes = Outcomes()
    for method in (RECYCLE, PERMANENT):  # recycling first, the way it was meant
        batch = [
            (gid, chosen[0].snapshot_id, [i.request for i in chosen])
            for (m, gid), chosen in groups.items()
            if m == method
        ]
        if batch:
            done = run_batch(
                repo,
                batch,
                method=method,
                options=options,
                ledger=ledger,
                progress=progress,
                should_stop=should_stop,
            )
            outcomes += done
            if done.cancelled:
                outcomes.cancelled = True
                break
    return outcomes


def still_waiting(items: Sequence[Waiting], outcomes: Sequence[Outcome]) -> list[Waiting]:
    """After :func:`resume`: what waits again for room, and what a stop (D58) left before it
    was reached; a folder that was handled (or whose record failed its check) is not."""
    handled = {
        path_key(t.path) if result is None else path_key(t["path"])
        for plan, result in outcomes
        for t in (plan.targets if result is None else result.targets)
    }
    return waiting(outcomes) + [i for i in items if path_key(i.path) not in handled]
