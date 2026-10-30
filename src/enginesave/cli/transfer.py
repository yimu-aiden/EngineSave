"""``enginesave export`` and ``import``: one archive, or several at once."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from enginesave.cli.common import (
    STOPPED_NOTE,
    confirm,
    emit,
    override_from,
    print_progress,
    should_stop,
)
from enginesave.errors import STOPPED_EXIT_CODE, UsageError
from enginesave.export import (
    ALREADY_IMPORTED,
    BatchResult,
    default_export_name,
    execute_import,
    export_records,
    export_snapshot,
    import_archives,
    plan_import,
    preview_imports,
    zip_archives,
)
from enginesave.repo import Repo, destination_state
from enginesave.report import (
    render_export_batch,
    render_export_result,
    render_import_batch,
    render_import_plan,
    render_import_previews,
    render_import_result,
    render_import_skipped,
)


def cmd_export(args: argparse.Namespace) -> int:
    if args.all and args.game:
        raise UsageError("give --all or --game, not both")
    if not args.all and not args.game:
        raise UsageError("choose what to export: --game ID (repeatable) or --all")
    repo = Repo.open(Path(args.repo))
    if args.all or len(args.game) > 1:
        return _export_batch(args, repo)
    record = repo.resolve_game(repo.load_catalog(), args.game[0])
    game_id = record["game_id"]
    snapshot_id = repo.latest_snapshot(game_id) if args.snapshot == "latest" else args.snapshot
    if snapshot_id is None or snapshot_id not in repo.snapshot_ids(game_id):
        raise UsageError(f"no snapshot {args.snapshot} for {record.get('display_name')}")
    dest = Path(os.path.abspath(args.to))
    if dest.is_dir():
        dest = dest / default_export_name(str(record.get("display_name") or ""), snapshot_id)
    result = export_snapshot(
        repo,
        game_id,
        snapshot_id,
        dest,
        overwrite=args.overwrite,
        include_tokens=args.include_tokens,
        keep_paths=args.keep_paths,
        saves_root_override=override_from(args),
    )
    return _done(args, render_export_result(result), result.to_dict())


def _export_batch(args: argparse.Namespace, repo: Repo) -> int:
    """``--all`` or several ``--game``: the latest snapshot of each into the ``--to`` folder."""
    if args.snapshot != "latest":
        raise UsageError("--snapshot needs a single --game; several records export their latest")
    folder = Path(os.path.abspath(args.to))
    if not folder.exists() and folder.suffix.lower() == ".zip":
        raise UsageError(f"--to must be a folder when exporting several records: {folder}")
    if args.all:
        records = sorted(
            repo.load_catalog()["games"],
            key=lambda r: (str(r.get("display_name") or "").casefold(), r["game_id"]),
        )
        wanted = [r["game_id"] for r in records]
    else:
        wanted = list(args.game)
    result = export_records(
        repo,
        wanted,
        folder,
        overwrite=args.overwrite,
        include_tokens=args.include_tokens,
        keep_paths=args.keep_paths,
        saves_root_override=override_from(args),
        progress=print_progress,
        should_stop=should_stop,  # D58: Ctrl+C, between archives
    )
    return _batch_done(args, render_export_batch(result), result)


def cmd_import(args: argparse.Namespace) -> int:
    source = Path(args.from_path)
    if source.is_dir():
        return _import_folder(args, Path(os.path.abspath(source)))
    plan = plan_import(Path(args.repo), source)
    print(render_import_plan(plan, args.reimport), file=sys.stderr if args.json else sys.stdout)
    if plan.already_imported is not None and not args.reimport:  # D31: not a failure
        data = {"plan": plan.to_dict(), "result": None, "reason": ALREADY_IMPORTED}
        emit(args, render_import_skipped(plan), data)
        return 0
    if not confirm("Import this snapshot?", args.yes):
        print("Aborted: nothing was written.", file=sys.stderr)
        return 1
    if should_stop():  # D58: Ctrl+C before the import began; one archive is not split
        print(f"Stopped before importing: nothing was written.\n{STOPPED_NOTE}", file=sys.stderr)
        return STOPPED_EXIT_CODE
    result = execute_import(plan)
    data = {"plan": plan.to_dict(), "result": result.to_dict(), "reason": None}
    return _done(args, render_import_result(result, plan.repo_root), data)


def _done(args: argparse.Namespace, human: str, data: Any) -> int:
    """One archive is its own safe point (D62 1): a Ctrl+C while it was written or read lets
    it finish, and then says so and exits 130."""
    if should_stop():
        emit(args, f"{human}\n{STOPPED_NOTE}", data)
        return STOPPED_EXIT_CODE
    emit(args, human, data)
    return 0


def _import_folder(args: argparse.Namespace, folder: Path) -> int:
    """Every ``*.zip`` directly in ``folder``, by name; one failure does not stop the rest."""
    repo_root = Path(os.path.abspath(args.repo))
    destination_state(repo_root)  # refuses a folder that is not a library, before reading zips
    archives = zip_archives(folder)
    previews = preview_imports(repo_root, archives)
    human = render_import_previews(previews, repo_root, args.reimport)
    print(human, file=sys.stderr if args.json else sys.stdout)
    usable = sum(
        1
        for _archive, plan, _error in previews
        if plan is not None
        and (args.reimport or (plan.already_imported is None and plan.duplicate_of is None))
    )
    if usable and not confirm(f"Import {usable} archive(s)?", args.yes):
        print("Aborted: nothing was written.", file=sys.stderr)
        return 1
    result = import_archives(
        repo_root,
        archives,
        reimport=args.reimport,
        location=folder,
        progress=print_progress,
        should_stop=should_stop,  # D58: Ctrl+C, between archives
    )
    return _batch_done(args, render_import_batch(result, repo_root), result)


def _batch_done(args: argparse.Namespace, human: str, result: BatchResult) -> int:
    """A batch's output; stopped (D58), the archives done are listed and the code is 130."""
    emit(args, f"{human}\n{STOPPED_NOTE}" if result.cancelled else human, result.to_dict())
    return STOPPED_EXIT_CODE if result.cancelled else result.exit_code
