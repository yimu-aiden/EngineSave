"""``enginesave remove`` (0.8.0, D49): delete backups, a whole record or some of its snapshots,
and the objects no snapshot uses any more. It shows what it would delete first; only ``--yes``
deletes (D40: no names to type). ``--tmp`` (0.9.0, D55) deletes the temporary folders that
interrupted runs left instead, those changed in the last ten minutes excepted, and then
reclaims the object store's space."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from enginesave.cli.common import STOPPED_NOTE, emit, should_stop
from enginesave.errors import STOPPED_EXIT_CODE, UsageError
from enginesave.removal import execute_remove, inventory, plan_remove
from enginesave.repo import Repo
from enginesave.report import (
    render_remove_plan,
    render_remove_result,
    render_tmp_folders,
    render_tmp_result,
)


def cmd_remove(args: argparse.Namespace) -> int:
    if args.tmp:
        if args.game or args.snapshot:
            raise UsageError("--tmp deletes temporary folders only; drop --game and --snapshot")
        return remove_tmp(args)
    if not args.game:
        raise UsageError("give --game (or --tmp for the temporary folders)")
    repo = Repo.open(Path(args.repo))
    found = inventory(repo)
    plan = plan_remove(repo, repo.load_catalog(), [(args.game, args.snapshot or None)], found)
    preview = render_remove_plan(plan.to_dict())
    if args.dry_run or not args.yes:
        human = f"{preview}\n\nNothing was deleted. Add --yes to delete (this cannot be undone)."
        emit(args, human, {"dry_run": True, "plan": plan.to_dict(), "result": None})
        return 0
    result = execute_remove(plan, found, should_stop=should_stop)
    data = {"dry_run": False, "plan": plan.to_dict(), "result": result.to_dict()}
    return _done(args, f"{preview}\n\n{render_remove_result(result.to_dict())}", data, result)


def remove_tmp(args: argparse.Namespace) -> int:
    repo = Repo.open(Path(args.repo))
    now = time.time()
    found = {"repository": str(repo.root), "tmp": [f.to_dict(now) for f in repo.tmp_folders()]}
    preview = render_tmp_folders(found)
    if args.dry_run or not args.yes:
        note = "Nothing was deleted. Add --yes to delete (this cannot be undone)."
        emit(args, f"{preview}\n\n{note}", {"dry_run": True, **found, "result": None})
        return 0
    result = repo.remove_tmp_dirs(should_stop=should_stop)  # none left: still reclaims space
    data = {"dry_run": False, **found, "result": result.to_dict()}
    return _done(args, f"{preview}\n\n{render_tmp_result(result.to_dict())}", data, result)


def _done(args: argparse.Namespace, human: str, data: dict, result: Any) -> int:
    """The output; stopped by Ctrl+C (D58), what was deleted is listed and the code is 130."""
    emit(args, f"{human}\n{STOPPED_NOTE}" if result.cancelled else human, data)
    return STOPPED_EXIT_CODE if result.cancelled else result.exit_code
