"""``enginesave backup``: plan, confirm, copy."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from enginesave.backup import execute_backup, prepare_backup
from enginesave.cli.common import (
    STOPPED_NOTE,
    checked_report_path,
    confirm,
    emit,
    override_from,
    should_stop,
    split_values,
    write_report,
)
from enginesave.errors import STOPPED_EXIT_CODE, UsageError
from enginesave.repo import Repo
from enginesave.report import render_backup_plan, render_backup_result
from enginesave.scan_cache import ScanCache, default_path


def cmd_backup(args: argparse.Namespace) -> int:
    roots = [Path(r) for r in args.roots]
    report_path = checked_report_path(args, roots, [Path(args.dest)])  # D72 R01
    names = split_values(args.unlinked)
    if args.include_unlinked and names:
        raise UsageError(
            "use either --include-unlinked (every orphaned folder) or --unlinked NAME, not both"
        )
    unlinked = [] if args.include_unlinked else (names or None)
    plan = prepare_backup(
        roots,
        Path(args.dest),
        saves_root_override=override_from(args),
        select=split_values(args.select) or None,
        include_candidates=args.include_candidates,
        include_unlinked=unlinked,
        include_tokens=args.include_tokens,
        # D52 3: the rescan before a backup is cheap; D62 3: --no-cache as for scan
        cache=None if args.no_cache else ScanCache.load(default_path()),
        should_stop=should_stop,  # D58: Ctrl+C
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
    print(human_plan, file=sys.stderr if args.json else sys.stdout)
    if not confirm("Proceed with the backup?", args.yes):
        print("Aborted: nothing was written.", file=sys.stderr)
        return 1
    repo = Repo.open_or_create(plan.dest)
    result = execute_backup(plan, repo, should_stop=should_stop)
    data["result"] = result.to_dict()
    problem = write_report(report_path, data) if report_path else None
    human = render_backup_result(result, repo.root)
    emit(args, f"{human}\n{STOPPED_NOTE}" if result.cancelled else human, data)
    if problem:  # D72 R15: what the backup committed stays; only the report is missing
        print(f"error: the backup finished and its snapshots are kept; {problem}", file=sys.stderr)
        return 3
    return STOPPED_EXIT_CODE if result.cancelled else result.exit_code
