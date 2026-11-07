"""``enginesave backup``: plan, confirm, copy.

1.2.0 (D73): ``--game-root`` backs up the games of those install folders only (nothing else
is scanned); ``--skip-unchanged``, ``--reason auto`` and ``--keep-auto`` for backups another
program starts; ``--mirror``/``--auto-mirror`` mirror afterwards as the window does;
``--progress jsonl`` and ``--stop-file`` for a caller without a console.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from enginesave.backup import BackupResult, execute_backup, prepare_backup
from enginesave.cli.common import (
    STOPPED_NOTE,
    Progress,
    checked_report_path,
    confirm,
    emit,
    jsonl,
    override_from,
    split_values,
    stopper,
    write_report,
)
from enginesave.errors import STOPPED_EXIT_CODE, UsageError
from enginesave.repo import Repo
from enginesave.report import render_backup_plan, render_backup_result
from enginesave.scan_cache import ScanCache, default_path


def scan_roots(args: argparse.Namespace) -> tuple[list[Path], list[Path] | None]:
    """``(folders to scan, install folders chosen)``: with ``--game-root`` only those folders
    are scanned (each must be a game's own folder), else the ``ROOT`` folders."""
    game_roots = [Path(os.path.abspath(p)) for p in args.game_root or []]
    if game_roots:
        for root in game_roots:
            if not root.is_dir():
                raise UsageError(f"--game-root is not an existing folder: {root}")
        return game_roots, game_roots
    if not args.roots:
        raise UsageError("give the folders to scan (ROOT), or --game-root PATH")
    return [Path(r) for r in args.roots], None


def mirror_wanted(args: argparse.Namespace) -> str | None:
    if args.mirror:
        return "mirror"
    return "auto" if args.auto_mirror else None


def not_worth_mirroring(result: BackupResult) -> str | None:
    """Why a backup is not mirrored now (the window's rule, D64 and D66 9), else ``None``."""
    if result.cancelled:
        return "the backup was stopped"
    if any(r.status == "failed" for r in result.results):
        return "the backup has failures"
    if not any(r.snapshot_id for r in result.results):
        return "no new snapshot was written"
    return None


def cmd_backup(args: argparse.Namespace) -> int:
    roots, chosen = scan_roots(args)
    named = [Path(r) for r in args.roots or []]
    report_path = checked_report_path(args, [*roots, *named], [Path(args.dest)])  # D72 R01
    names = split_values(args.unlinked)
    if args.include_unlinked and names:
        raise UsageError(
            "use either --include-unlinked (every orphaned folder) or --unlinked NAME, not both"
        )
    if args.keep_auto is not None and args.keep_auto < 1:
        raise UsageError("--keep-auto must be 1 or more (the newest automatic snapshot stays)")
    unlinked = [] if args.include_unlinked else (names or None)
    progress = Progress(jsonl(args))
    stop = stopper(args)
    narrow: dict[str, Any] = {}
    if chosen is not None:  # D73 1: each folder itself, nothing below it, no orphaned folders
        narrow = {"max_depth": 0, "scan_unlinked": False}
        if named:  # ROOT given too: not scanned, but the library must still keep out of it
            from enginesave.backup import check_destination
            from enginesave.engines.renpy.paths import default_saves_root

            saves_root, _how = default_saves_root(dict(os.environ), override_from(args))
            dest = Path(os.path.abspath(args.dest))
            check_destination(dest, [Path(os.path.abspath(r)) for r in named], saves_root)
    plan = prepare_backup(
        roots,
        Path(args.dest),
        saves_root_override=override_from(args),
        select=split_values(args.select) or None,
        select_roots=chosen,
        include_candidates=args.include_candidates,
        include_unlinked=unlinked,
        include_tokens=args.include_tokens,
        # D52 3: the rescan before a backup is cheap; D62 3: --no-cache as for scan
        cache=None if args.no_cache else ScanCache.load(default_path()),
        should_stop=stop,  # D58: Ctrl+C; D73 9: the stop file
        scan_progress=progress.scan_callback(),
        **narrow,
    )
    data: dict[str, Any] = {"dry_run": args.dry_run, "plan": plan.to_dict(), "result": None}
    human_plan = render_backup_plan(plan)
    if args.dry_run or not plan.games:
        problem = write_report(report_path, data) if report_path else None
        emit(args, human_plan + ("" if plan.games else "\nNothing to back up."), data)
        if problem:  # D72 R15
            print(f"error: {problem}", file=sys.stderr)
            return 3
        return 0
    if not (args.json and jsonl(args)):
        print(human_plan, file=sys.stderr if args.json else sys.stdout)
    if not confirm("Proceed with the backup?", args.yes):
        print("Aborted: nothing was written.", file=sys.stderr)
        return 1
    repo = Repo.open_or_create(plan.dest)
    result = execute_backup(
        plan,
        repo,
        reason=args.reason,
        progress=progress.backup,
        should_stop=stop,
        skip_unchanged=args.skip_unchanged,
        keep_auto=args.keep_auto,
    )
    if plan.games:
        last = plan.games[len(result.results) - 1].display_name if result.results else ""
        progress.backup(len(result.results), len(plan.games), last)
    wanted = mirror_wanted(args)
    if wanted is not None:
        result.mirror = follow_up_mirror(args, repo, result, wanted, roots, progress, stop)
    data["result"] = result.to_dict()
    problem = write_report(report_path, data) if report_path else None
    human = render_backup_result(result, repo.root)
    if result.mirror is not None:
        human += f"\nMirror: {result.mirror['status']}"
        if result.mirror["message"]:
            human += f" ({result.mirror['message']})"
    emit(args, f"{human}\n{STOPPED_NOTE}" if result.cancelled else human, data)
    if problem:  # D72 R15: what the backup committed stays; only the report is missing
        print(f"error: the backup finished and its snapshots are kept; {problem}", file=sys.stderr)
        return 3
    return STOPPED_EXIT_CODE if result.cancelled else result.exit_code


def follow_up_mirror(
    args: argparse.Namespace,
    repo: Repo,
    result: BackupResult,
    wanted: str,
    roots: list[Path],
    progress: Progress,
    stop: Any,
) -> dict[str, Any]:
    """D73 16: the mirror after the backup; its failure is said in ``mirror``, never in the
    exit code."""
    from enginesave.appdirs import config_dir
    from enginesave.mirror import forbidden_places, mirror_after_backup

    try:
        forbidden = forbidden_places(repo, saves_root=override_from(args), also=roots)
    except Exception as exc:  # noqa: BLE001 - the mirror's problem, never the backup's
        return {"folder": None, "status": "failed", "finished_at": None, "message": str(exc)}
    return mirror_after_backup(
        repo,
        config_dir(),
        wanted=wanted,
        backed_up=not_worth_mirroring(result),
        forbidden=forbidden,
        progress=progress.mirror if progress.enabled else None,
        should_stop=stop,
        command=f"enginesave backup --{'mirror' if wanted == 'mirror' else 'auto-mirror'}",
    )
