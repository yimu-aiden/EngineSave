"""``enginesave restore``: plan, confirm, restore (with the safety snapshot)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from enginesave.cli.common import confirm, emit, interactive, override_from, split_values
from enginesave.errors import UsageError
from enginesave.repo import Repo
from enginesave.report import render_restore_plan, render_restore_result
from enginesave.restore import execute_restore, plan_restore


def cmd_restore(args: argparse.Namespace) -> int:
    if args.no_safety_snapshot and not args.i_know_what_i_am_doing:
        raise UsageError(
            "--no-safety-snapshot is meant for tests and requires --i-know-what-i-am-doing"
        )
    repo = Repo.open(Path(args.repo))
    plan = plan_restore(
        repo,
        repo.load_catalog(),
        args.game,
        snapshot=args.snapshot,
        game_root=Path(args.game_root) if args.game_root else None,
        sources=split_values([args.sources] if args.sources else None) or None,
        saves_root_override=override_from(args),
    )
    human_plan = render_restore_plan(plan)
    if args.dry_run:
        emit(args, human_plan, {"dry_run": True, "plan": plan.to_dict(), "result": None})
        return 0
    print(human_plan, file=sys.stderr if args.json else sys.stdout)
    if not confirm("Restore these files?", args.yes):
        print("Aborted: nothing was written.", file=sys.stderr)
        return 1
    if plan.includes_tokens:
        if not interactive(sys.stdin):
            raise UsageError("restoring machine_tokens needs a second, interactive confirmation")
        if not confirm("This overwrites Ren'Py's save-signing private keys. Again:", False):
            print("Aborted: nothing was written.", file=sys.stderr)
            return 1
    result = execute_restore(plan, safety_snapshot=not args.no_safety_snapshot)
    data = {"dry_run": False, "plan": plan.to_dict(), "result": result.to_dict()}
    emit(args, render_restore_result(result), data)
    return result.exit_code
