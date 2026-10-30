"""``enginesave cleanup``: check, confirm, recycle or delete; and when the recycle bin is full,
make room or leave the rest for later (``--on-full-bin``, D41)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from enginesave.cleanup import (
    GAME_ROOT,
    GLOBAL,
    PERMANENT,
    RECYCLE,
    UNLINKED,
    CleanupResult,
    execute_cleanup,
    parse_size,
    plan_cleanup,
)
from enginesave.cli.common import (
    STOPPED_NOTE,
    confirm,
    emit,
    interactive,
    override_from,
    should_stop,
    split_values,
)
from enginesave.errors import STOPPED_EXIT_CODE, UsageError
from enginesave.repo import Repo
from enginesave.report import human_size, render_cleanup_plan, render_cleanup_result


def cmd_cleanup(args: argparse.Namespace) -> int:
    targets: list[tuple[str, str | None]] = []
    if args.game_root is not None:
        targets.append((GAME_ROOT, os.path.abspath(args.game_root) if args.game_root else None))
    if args.global_folder:
        targets.append((GLOBAL, None))
    targets += [(UNLINKED, name) for name in split_values(args.unlinked)]
    if not targets:
        raise UsageError(
            "choose what to clean up: --game-root [PATH], --global and/or --unlinked NAME"
        )
    limit = parse_size(args.recycle_limit) if args.recycle_limit else None  # the bin's room
    repo = Repo.open(Path(args.repo))
    record = repo.resolve_game(repo.load_catalog(), args.game)
    plan = plan_cleanup(
        repo,
        record,
        targets,
        snapshot_id=args.snapshot,
        method=PERMANENT if args.permanent else RECYCLE,
        recycle_limit=limit,
        saves_root_override=override_from(args),
    )
    human = render_cleanup_plan(plan, skip_failed=args.skip_failed)
    if not plan.active:  # D41: already cleaned up or gone is not a failure, and nothing to do
        emit(args, human, {"dry_run": args.dry_run, "plan": plan.to_dict(), "result": None})
        return 0
    usable = plan.ok or (args.skip_failed and bool(plan.passed))
    if args.dry_run:
        emit(args, human, {"dry_run": True, "plan": plan.to_dict(), "result": None})
        return 0 if usable else 2
    print(human, file=sys.stderr if args.json else sys.stdout)
    if not usable:
        failed = len(plan.active) - len(plan.passed)
        raise UsageError(
            f"{failed} folder(s) failed the checks; nothing was deleted"
            + (" (--skip-failed cleans up only the folders that pass)" if plan.passed else "")
        )
    count = len(plan.passed)
    question = (  # D40: one 'yes' for the plan above, as for backup and restore
        f"Move {count} folder(s) to the recycle bin?"
        if plan.method == RECYCLE
        else f"Delete {count} folder(s) permanently? This cannot be undone."
    )
    if not confirm(question, args.yes):
        print("Aborted: nothing was deleted.", file=sys.stderr)
        return 1
    result = execute_cleanup(plan, skip_failed=args.skip_failed, should_stop=should_stop)
    data: dict[str, Any] = {"dry_run": False, "plan": plan.to_dict(), "result": result.to_dict()}
    text = render_cleanup_result(result)
    if result.cancelled:  # D58: Ctrl+C between folders; no making room afterwards
        emit(args, f"{text}\n{STOPPED_NOTE}", data)
        return STOPPED_EXIT_CODE
    if not result.skipped:
        emit(args, text, data)
        return result.exit_code
    follow_up, more = _full_bin(args, repo, plan, result)
    text += "\n" + "\n".join(follow_up.pop("lines"))
    data["full_bin"] = follow_up
    if follow_up.get("cancelled"):  # D58: Ctrl+C while making room or going on
        emit(args, f"{text}\n{STOPPED_NOTE}", data)
        return STOPPED_EXIT_CODE
    emit(args, text, data)
    failed = any(t["result"] == "failed" for r in [result, *more] for t in r.targets)
    return 1 if failed or follow_up["waiting"] else 0  # 1 while something is left for later


def _full_bin(
    args: argparse.Namespace, repo: Repo, plan: Any, result: CleanupResult
) -> tuple[dict[str, Any], list[CleanupResult]]:
    """D41 6: room for what the full recycle bin left for later. ``ask`` (a console without
    --yes) offers to purge EngineSave's earlier recycled folders, then to delete what still does
    not fit permanently; ``purge-ours`` and ``permanent`` do one of them; ``stop`` waits."""
    from enginesave.cleanup import batch, recycle_bin

    mode = args.on_full_bin or ("ask" if interactive(sys.stdin) and not args.yes else "stop")
    items = batch.waiting([(plan, result)])
    drives = sorted({i.drive for i in items})
    waiting_size = human_size(sum(i.bytes for i in items))
    lines = [
        f"The recycle bin is full: {len(items)} folder(s) ({waiting_size}) left for later on "
        f"{', '.join(drives)}."
    ]
    follow_up: dict[str, Any] = {"mode": mode, "purged": [], "results": []}
    results: list[CleanupResult] = []
    options = {"saves_root_override": override_from(args)}

    def run(permanent: list[str]) -> None:
        nonlocal items
        outcomes = batch.resume(
            repo, items, permanent=permanent, options=options, should_stop=should_stop
        )
        for again, done in outcomes:
            if done is None:
                lines.append(f"{again.display_name}: changed since the check; nothing removed")
                continue
            results.append(done)
            follow_up["results"].append(done.to_dict())
            lines.append(render_cleanup_result(done))
        items = batch.still_waiting(items, outcomes)  # a stop keeps what it did not reach
        if batch.cancelled(outcomes):  # D58: Ctrl+C; nothing more is asked or done
            follow_up["cancelled"] = True

    if mode == "permanent" or mode == "stop":
        if mode == "permanent":
            run([i.path for i in items])
    else:
        ours = recycle_bin.ours(repo, drives)
        freed = sum(o.item.size for o in ours)
        if ours:
            lines.append(
                f"EngineSave put {len(ours)} folder(s) ({human_size(freed)}) in the recycle bin "
                "earlier; they are backed up and verified:"
            )
            lines += [f"  {o.item.original}  {human_size(o.item.size)}" for o in ours]
        purge = mode == "purge-ours" or (
            mode == "ask"
            and bool(ours)
            and _ask(lines, f"Delete these {len(ours)} folder(s) from the recycle bin?")
        )
        if purge and ours:
            follow_up["purged"] = recycle_bin.purge(repo, ours)
            done = sum(1 for p in follow_up["purged"] if p["result"] == "purged")
            lines.append(f"Purged from the recycle bin: {done} of {len(ours)}.")
            run([])
        if items and mode == "ask" and not follow_up.get("cancelled"):
            size = human_size(sum(i.bytes for i in items))
            lines += [f"  waiting: {i.path}  {human_size(i.bytes)}" for i in items]
            if _ask(
                lines,
                f"Delete the {len(items)} folder(s) still waiting ({size}) permanently instead? "
                "This cannot be undone.",
            ):
                run([i.path for i in items])
    if items:
        lines.append(
            f"Left for later: {len(items)} folder(s); empty the recycle bin (or enlarge it) and "
            "run the cleanup again."
        )
    follow_up["waiting"] = [i.path for i in items]
    follow_up["lines"] = lines
    return follow_up, results


def _ask(lines: list[str], question: str) -> bool:
    """Print what was collected so far, then one interactive 'yes'."""
    print("\n".join(lines), file=sys.stderr)
    lines.clear()
    return confirm(question, False)
