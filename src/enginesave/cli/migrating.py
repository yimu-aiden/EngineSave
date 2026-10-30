"""``enginesave migrate`` (0.11.0, D67 8): a library of SaveTrail 0.10.0 and earlier in place to
the readable layout -- record folders named after the games. ``--dry-run`` shows the names and
changes nothing; a library that cannot be migrated now is refused (exit 2) and says why."""

from __future__ import annotations

import argparse
from pathlib import Path

from enginesave.cli.common import STOPPED_NOTE, emit, should_stop
from enginesave.errors import STOPPED_EXIT_CODE
from enginesave.migrate import execute_migration, plan_migration
from enginesave.repo import Repo
from enginesave.report import render_migration, render_migration_plan


def cmd_migrate(args: argparse.Namespace) -> int:
    repo = Repo.open(Path(args.repo))
    plan = plan_migration(repo)
    if args.dry_run:
        data = plan.to_dict()
        emit(args, render_migration_plan(data), data)
        return 2 if plan.refusals else 0
    result = execute_migration(repo, plan, should_stop=should_stop)
    data = result.to_dict()
    human = render_migration(data)
    if result.cancelled:  # D58: between records; running it again goes on
        emit(args, f"{human}\n{STOPPED_NOTE}", data)
        return STOPPED_EXIT_CODE
    emit(args, human, data)
    return result.exit_code
