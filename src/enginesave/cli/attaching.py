"""``enginesave attachments list|add|open|merge`` (1.3.0, D76, D77): a record's 附件 folder."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from enginesave import layout
from enginesave.attachments import add, ensure_folder, listing, open_in_explorer
from enginesave.cli.common import emit
from enginesave.errors import UsageError
from enginesave.report import human_size


def _empty(work: str) -> dict:
    """``list`` of a Collections work no record keeps yet: nothing, and no folder."""
    return {
        "game_id": None,
        "folder": None,
        "exists": False,
        "entries": [],
        "file_count": 0,
        "total_bytes": 0,
        "approximate": False,
        "collections_work_ids": [],
    }


def cmd_attachments(args: argparse.Namespace) -> int:
    from enginesave.repo import Repo, work_ref
    from enginesave.workrecords import merge, resolve_or_create

    repo = Repo.open(Path(args.repo))
    action = args.attachments_command
    if action == "merge":
        data = merge(repo, args.from_ref, args.into)
        lines = [f"moved into {data['into']['folder']}:"]
        lines += [f"  {m['from']} -> {m['name']}" for m in data["moved"]]
        lines += [f"  FAILED {f['name']}: {f['error']}" for f in data["failed"]]
        fate = "removed" if data["from"]["removed"] else "kept (it has snapshots)"
        lines.append(f"the record merged from: {fate}")
        emit(args, "\n".join(lines), data)
        return 1 if data["failed"] else 0
    if action == "list":
        catalog = repo.load_catalog()
        work = work_ref(args.game)
        if work is not None and not repo.records_of_work(catalog, work):
            emit(args, f"no record keeps the Collections work {work!r} yet", _empty(work))
            return 0
        record, created = repo.resolve_game(catalog, args.game), False
    else:
        record, created = resolve_or_create(repo, args.game, args.title)
    game_id = str(record["game_id"])
    name = record.get("display_name") or game_id
    record_folder = repo.game_dir(game_id)
    works = list(repo.load_game(game_id).get("collections_work_ids") or [])
    made = {"created_record": created, "collections_work_ids": works}
    if action == "list":
        found = listing(game_id, record_folder)
        lines = [f"附件 of {name}: {found.folder}" + ("" if found.exists else " (not created yet)")]
        for entry in found.entries:
            mark = "\\" if entry["kind"] == "dir" else ""
            lines.append(f"  {entry['name']}{mark}  {human_size(entry['bytes'])}")
        about = "about " if found.approximate else ""
        lines.append(f"{about}{found.file_count} file(s), {human_size(found.total_bytes)}")
        data = {**found.to_dict(), "collections_work_ids": works}
        emit(args, "\n".join(lines), data)
        return 0
    if action == "add":
        result = add(record_folder, [Path(f) for f in args.files], move=args.move)
        if result.saved:
            layout.write_index(repo)  # INDEX.txt counts each record's attachments
        verb = "moved" if args.move else "copied"
        lines = [f"{verb} into {result.folder}:"]
        lines += [f"  {s['name']}  ({human_size(s['bytes'])})" for s in result.saved]
        lines += [f"  FAILED {f['source']}: {f['error']}" for f in result.failed]
        emit(args, "\n".join(lines), {"game_id": game_id, **made, **result.to_dict()})
        if result.failed:
            print(f"error: {len(result.failed)} file(s) not added", file=sys.stderr)
        return result.exit_code
    if action == "open":
        folder = ensure_folder(record_folder)
        if not args.create_only:
            open_in_explorer(folder)
        emit(args, str(folder), {"game_id": game_id, "folder": str(folder), **made})
        return 0
    raise UsageError(f"unknown attachments action {action!r}")
