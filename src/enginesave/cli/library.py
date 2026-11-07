"""``enginesave list``, ``show``, ``verify``, ``link`` and ``confirm-source``: reading and
annotating a backup repository."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from enginesave.appdirs import config_dir
from enginesave.backup import register_game
from enginesave.cli.common import STOPPED_NOTE, emit, override_from, should_stop
from enginesave.errors import STOPPED_EXIT_CODE, EngineSaveError, Stopped, UsageError
from enginesave.fsutil import path_key
from enginesave.linking import confirm_source, game_at, link_root
from enginesave.mirror import last_mirror
from enginesave.removal import library_stats
from enginesave.repo import Repo, from_install_of, install_present
from enginesave.report import render_list, render_show_game, render_show_snapshot, render_verify
from enginesave.verify import verify_objects, verify_repo


def latest_facts(repo: Repo, game_id: str, snapshot_id: str | None) -> dict[str, Any]:
    """1.2.0 (D73 5, 14): when the latest snapshot (of any reason) was made, why, and the
    install it came from; all ``None`` without one or when its manifest cannot be read."""
    facts: dict[str, Any] = {
        "latest_snapshot_at": None,
        "latest_reason": None,
        "latest_from_install": None,
    }
    if snapshot_id is None:
        return facts
    facts["latest_snapshot_at"] = repo.snapshot_created(game_id, snapshot_id)
    try:
        manifest = repo.load_manifest(game_id, snapshot_id)
    except EngineSaveError:
        return facts
    reason = manifest.get("reason")
    facts["latest_reason"] = reason if isinstance(reason, str) else None
    facts["latest_from_install"] = from_install_of(manifest)
    return facts


def record_installs(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """D73 5: the install folders a record knows, newest seen first, with the game version
    read there and whether the folder is on disk now (an install cleaned up or deleted stays
    listed: the record remembers where the game was)."""
    found = []
    installs = [i for i in doc.get("installs") or [] if isinstance(i, dict) and i.get("game_root")]
    installs.sort(key=lambda i: str(i.get("last_seen") or ""), reverse=True)
    for install in installs:
        root = Path(str(install["game_root"]))
        version = install.get("game_version") or install.get("config_version")
        found.append(
            {
                "game_root": str(root),
                "game_version": version if isinstance(version, str) else None,
                "present": root.is_absolute() and root.is_dir(),
                "first_seen": install.get("first_seen"),
                "last_seen": install.get("last_seen"),
                "removed_at": install.get("removed_at"),
            }
        )
    return found


def cmd_list(args: argparse.Namespace) -> int:
    repo = Repo.open(Path(args.repo))
    catalog = repo.load_catalog()
    rows = []
    for record in catalog["games"]:
        ids = repo.snapshot_ids(record["game_id"])
        row = {
            "game_id": record["game_id"],
            "kind": record.get("kind", "game"),
            "display_name": record.get("display_name"),
            "engine": record.get("engine"),
            "renpy_save_directory": (record.get("identity") or {}).get("renpy_save_directory"),
            "snapshot_count": len(ids),
            "latest_snapshot": ids[-1] if ids else None,
        }
        if record.get("cleaned_at"):  # only after a cleanup (D26): older output is unchanged
            row["cleaned_at"] = record["cleaned_at"]
        doc = repo.load_game(record["game_id"])
        row["install_present"] = install_present(doc)  # D43
        row.update(latest_facts(repo, record["game_id"], ids[-1] if ids else None))  # D73 5
        row["installs"] = record_installs(doc)
        rows.append(row)
    mirrored = last_mirror(config_dir(), repo.root)
    stats = library_stats(repo, last_mirror=mirrored["at"] if mirrored else None)
    data = {
        "repository": str(repo.root),
        "games": rows,
        "stale_tmp_dirs": [str(p) for p in repo.stale_tmp_dirs()],
        "orphan_record_dirs": [str(p) for p in repo.orphan_game_dirs()],  # 0.8.0 (D49)
        # 0.9.0 (D56); 1.2.0 (D73 16): where it was last mirrored to
        "stats": stats.to_dict() | {"mirror_folder": mirrored["to"] if mirrored else None},
    }
    emit(args, render_list(data), data)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    repo = Repo.open(Path(args.repo))
    game_id = repo.resolve_game(repo.load_catalog(), args.game)["game_id"]
    doc = repo.load_game(game_id)
    if args.snapshot:
        snapshot_id = repo.latest_snapshot(game_id) if args.snapshot == "latest" else args.snapshot
        if snapshot_id is None or snapshot_id not in repo.snapshot_ids(game_id):
            raise UsageError(f"no snapshot {args.snapshot} for {doc['display_name']}")
        manifest = repo.load_manifest(game_id, snapshot_id)
        emit(args, render_show_snapshot(doc, manifest), {"game": doc, "manifest": manifest})
        return 0
    snapshots = [repo.snapshot_summary(game_id, sid) for sid in repo.snapshot_ids(game_id)]
    emit(args, render_show_game(doc, snapshots), {"game": doc, "snapshots": snapshots})
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    repo = Repo.open(Path(args.repo))
    catalog = repo.load_catalog()
    record = repo.resolve_game(catalog, args.game)
    root = Path(os.path.abspath(args.game_root))
    override, replaced = link_root(repo, catalog, record["game_id"], root)
    note = " (replaces an earlier link of this folder)" if replaced else ""
    emit(args, f"Linked {root} to {record['display_name']} ({record['game_id']}){note}", override)
    return 0


def cmd_confirm_source(args: argparse.Namespace) -> int:
    if bool(args.game) == bool(args.game_root):
        raise UsageError("give exactly one of --game or --game-root")
    repo = Repo.open(Path(args.repo))
    path = Path(os.path.abspath(args.path))
    if not path.is_dir():
        raise UsageError(f"--path is not an existing folder: {path}")
    notes: list[str] = []
    if args.game_root:
        game = game_at(
            Path(os.path.abspath(args.game_root)),
            repo=Path(args.repo),
            saves_root_override=override_from(args),
        )
        game_id, created = register_game(repo, repo.load_catalog(), game)
        if created:
            notes.append(f"Created a record for {game.display_name}; nothing was backed up.")
        if all(path_key(s.path) != path_key(path) for s in game.sources):
            notes.append(f"note: {path} is not among the save folders found for this game")
        catalog = repo.load_catalog()
        record = repo.resolve_game(catalog, game_id)
    else:
        catalog = repo.load_catalog()
        record = repo.resolve_game(catalog, args.game)
    override = confirm_source(repo, catalog, record["game_id"], args.source_id, path)
    human = (
        f"Confirmed {path} as source {override['source_id']} of {record['display_name']}. "
        f"Scans and backups that use this repository now treat it as USER_CONFIRMED."
    )
    emit(args, "\n".join([*notes, human]), override)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    repo = Repo.open(Path(args.repo))
    stopped = False
    try:
        checks = verify_repo(
            repo, repo.load_catalog(), args.game, args.snapshot, should_stop=should_stop
        )
    except Stopped as exc:  # D58: Ctrl+C between snapshots; the ones checked are reported
        checks, stopped = list(exc.partial or []), True
    objects = None
    if args.objects and not stopped:
        try:
            objects = verify_objects(repo, should_stop=should_stop)
        except Stopped:  # the objects are reported only when all were checked
            stopped = True
    stale = [str(p) for p in repo.stale_tmp_dirs()]
    orphans = [str(p) for p in repo.orphan_game_dirs()]  # 0.8.0 (D49)
    data: dict[str, Any] = {
        "repository": str(repo.root),
        "snapshots": [c.to_dict() for c in checks],
        "stale_tmp_dirs": stale,
        "orphan_record_dirs": orphans,
    }
    if objects is not None:
        data["objects"] = objects.to_dict()
    if stopped:
        data["cancelled"] = True  # 0.9.0: only when it was stopped
    human = render_verify(checks, stale, objects, orphans)
    emit(args, f"{human}\n{STOPPED_NOTE}" if stopped else human, data)
    if stopped:
        return STOPPED_EXIT_CODE
    clean = all(c.clean for c in checks) and (objects is None or objects.clean)
    return 0 if clean else 1
