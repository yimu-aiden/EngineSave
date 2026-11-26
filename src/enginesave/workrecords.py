"""1.3.0 (D77): records Collections works point to.

EngineSave keeps every save and attachment file; Collections keeps metadata and calls the
command line. So a record remembers the Collections works it keeps (``collections_work_ids``
in ``game.json`` and in its catalog entry), ``--game collections:<work id>`` finds it, and:

* a game Collections knows that has no record yet -- not installed here, or never backed up --
  can still keep attachments: ``attachments add|open --game collections:W --title T`` makes an
  *attachment-only* record (no snapshots, no engine yet) named after ``T``;
* a later scan or backup with ``--collections`` that finds a copy of ``W`` puts the game into
  that record (it is the record of the work); the first backup gives it its engine and
  identity, its name stays the one Collections gave;
* a backup of a game that already had a record of its own, while an attachment-only record
  keeps the same work, moves that record's attachments and work into the game's record and
  removes the empty one (``absorb``);
* ``attachments merge --from A --into B`` (Collections merged two works) moves A's attachments
  into B, gives B A's works and removes A when it has no snapshots; a record with snapshots is
  kept (its saves are not moved) and only loses its works.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from typing import Any

from enginesave import layout
from enginesave.attachments import ATTACHMENTS_DIR, TMP_SUFFIX, ensure_folder, numbered
from enginesave.errors import FatalError, UsageError
from enginesave.fsutil import describe_error, is_reparse_point, iso_utc, long_path, utc_now
from enginesave.repo import FORMAT_VERSION, Repo, add_work_id, work_ref

MAX_TITLE = 200


def create_attachment_record(repo: Repo, work_id: str, title: str) -> str:
    """A new record with no snapshots that keeps the Collections work ``work_id``, its folder
    named after ``title``; its id."""
    from enginesave.backup import update_catalog_entry

    title = " ".join((title or "").split())
    if not title:
        raise UsageError(
            f"no record keeps the Collections work {work_id!r}; give --title to make one"
        )
    if len(title) > MAX_TITLE:
        raise UsageError(f"--title is longer than {MAX_TITLE} characters")
    repo.require_writable()
    now = iso_utc(utc_now())
    game_id = str(uuid.uuid4())
    doc: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "game_id": game_id,
        "kind": "game",
        "engine": None,  # given by the first backup of a copy of the work
        "engine_version": None,
        "display_name": title,
        "identity": {
            "renpy_save_directory": None,
            "config_name": None,
            "build_name": None,
            "config_version": None,
            "exe_basename": None,
            "root_dir_name": "",
        },
        "installs": [],
        "source_templates": [],
        "created_at": now,
        "updated_at": now,
        "collections_work_ids": [work_id],
    }
    repo.assign_folder(game_id, title, None)
    repo.save_game(doc)
    update_catalog_entry(repo, repo.load_catalog(), doc)
    layout.write_index(repo)
    return game_id


def resolve_or_create(repo: Repo, ref: str, title: str | None) -> tuple[dict, bool]:
    """``(catalog record, created)`` for ``--game``: a ``collections:<work id>`` no record
    keeps gets an attachment-only record when ``title`` is given."""
    catalog = repo.load_catalog()
    work = work_ref(ref)
    if work is not None and not repo.records_of_work(catalog, work):
        if title is None:
            raise UsageError(
                f"no record keeps the Collections work {work!r}; give --title to make one"
            )
        game_id = create_attachment_record(repo, work, title)
        return repo.resolve_game(repo.load_catalog(), game_id), True
    return repo.resolve_game(catalog, ref), False


def _save(repo: Repo, doc: dict) -> None:
    from enginesave.backup import update_catalog_entry

    doc["updated_at"] = iso_utc(utc_now())
    repo.save_game(doc)
    catalog = repo.load_catalog()
    if not doc.get("collections_work_ids"):
        for entry in catalog["games"]:
            if isinstance(entry, dict) and entry.get("game_id") == doc["game_id"]:
                entry.pop("collections_work_ids", None)
    update_catalog_entry(repo, catalog, doc)


def link_work(repo: Repo, ref: str, work_id: str) -> dict:
    """``link --collections-work``: the record keeps ``work_id`` from now on; refused when
    another record keeps it (merge them instead)."""
    catalog = repo.load_catalog()
    record = repo.resolve_game(catalog, ref)
    others = [
        r for r in repo.records_of_work(catalog, work_id) if r["game_id"] != record["game_id"]
    ]
    if others:
        names = ", ".join(f"{r['game_id']} ({r.get('display_name') or '?'})" for r in others)
        raise UsageError(
            f"the Collections work {work_id!r} is kept by {names}; move it with "
            "`enginesave attachments merge`"
        )
    doc = repo.load_game(record["game_id"])
    if add_work_id(doc, work_id):
        _save(repo, doc)
    return {"game_id": record["game_id"], "collections_work_ids": doc["collections_work_ids"]}


def _move_entries(source: Any, target: Any) -> tuple[list[dict], list[dict]]:
    """Every entry directly in the attachments folder ``source`` renamed into ``target`` (one
    library, one drive: nothing is copied), numbered when its name is taken there."""
    moved: list[dict] = []
    failed: list[dict] = []
    if not source.is_dir() or is_reparse_point(source):
        return moved, failed
    taken = {p.name.casefold() for p in target.iterdir()}
    for entry in sorted(source.iterdir(), key=lambda p: p.name.casefold()):
        if entry.name.endswith(TMP_SUFFIX) or is_reparse_point(entry):
            continue
        name = numbered(entry.name, taken)
        try:
            os.rename(long_path(entry), long_path(target / name))
        except OSError as exc:
            failed.append({"name": entry.name, "error": describe_error(exc)})
            continue
        taken.add(name.casefold())
        moved.append({"from": entry.name, "name": name})
    return moved, failed


def merge(repo: Repo, source_ref: str, target_ref: str) -> dict[str, Any]:
    """``attachments merge --from A --into B``: A's attachments into B, A's works to B; A goes
    when it has no snapshots, else it stays without works (its saves are not moved)."""
    repo.require_writable()
    catalog = repo.load_catalog()
    source = repo.resolve_game(catalog, source_ref)
    target = repo.resolve_game(catalog, target_ref)
    if source["game_id"] == target["game_id"]:
        raise UsageError("--from and --into are the same record")
    source_doc = repo.load_game(source["game_id"])
    target_doc = repo.load_game(target["game_id"])
    into = ensure_folder(repo.game_dir(target["game_id"]))
    moved, failed = _move_entries(repo.game_dir(source["game_id"]) / ATTACHMENTS_DIR, into)
    works = [w for w in source_doc.get("collections_work_ids") or [] if isinstance(w, str)]
    for work in works:
        add_work_id(target_doc, work)
    _save(repo, target_doc)
    snapshots = len(repo.snapshot_ids(source["game_id"]))
    removed = False
    if not failed:
        source_doc["collections_work_ids"] = []
        if snapshots == 0:
            leftovers = repo.remove_game(source["game_id"], with_attachments=True)
            if leftovers:
                failed += [{"name": path, "error": "could not be removed"} for path in leftovers]
            removed = True
        else:
            _save(repo, source_doc)
    layout.write_index(repo)
    return {
        "from": {"game_id": source["game_id"], "removed": removed, "snapshots": snapshots},
        "into": {
            "game_id": target["game_id"],
            "folder": str(into),
            "collections_work_ids": list(target_doc.get("collections_work_ids") or []),
        },
        "moved": moved,
        "failed": failed,
    }


def absorb(repo: Repo, games: Sequence[Any], record_ids: Sequence[str | None]) -> list[dict]:
    """After a backup: an attachment-only record that keeps the work of a game just backed up
    into another record is merged into that record (D77). The merges done."""
    done = []
    for game, record_id in zip(games, record_ids, strict=False):
        info = getattr(game, "collections", None) or {}
        work = info.get("work_id") if isinstance(info, dict) else None
        if not work or not record_id:
            continue
        catalog = repo.load_catalog()
        for other in repo.records_of_work(catalog, work):
            other_id = other["game_id"]
            if other_id == record_id or repo.snapshot_ids(other_id):
                continue
            try:
                done.append(merge(repo, other_id, record_id))
            except (UsageError, FatalError) as exc:
                done.append({"from": {"game_id": other_id}, "error": str(exc)})
    return done
