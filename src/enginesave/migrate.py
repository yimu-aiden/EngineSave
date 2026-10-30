"""Migrating a library of format 1 (0.1.0-0.10.0) to the readable layout of 0.11.0 (D67 8).

``enginesave migrate`` and the window's migration dialog both run through here; only the user
starts it, once per library (a mirror of a migrated library migrates its target first). The
steps keep an interruption harmless and a second run able to go on:

1. ``catalog.json`` is copied to ``catalog.json.bak-<local time>``, and the catalog gets
   ``format_version`` 2 at once (1.0.0, D70 1): from then on SaveTrail 0.10.0 and earlier
   refuse the library, so none of them can give a half-renamed library new ``games/<id>``
   folders; an entry without its ``folder`` is read at ``games/<id>`` as before;
2. record by record, ``games/<id>`` is renamed to ``games/<name>`` (D67 1) -- the id in the
   folder's ``game.json`` checked before and after -- and the catalog, with that entry's
   ``folder``, written again at once. A record renamed by an interrupted run whose catalog
   was not written yet is found by the id in its folder's ``game.json`` (whatever the folder
   is called, D72 R14). A failed rename leaves that one record at ``games/<id>``; running
   the migration again goes on with it;
3. each ``game.json`` gets ``format_version`` 2;
4. every snapshot without a ``README.txt`` gets one (its files stay in ``data\\<source id>``,
   which the README explains, D67 4), and the library its ``INDEX.txt`` (D67 5). A library
   of format 2 whose snapshots lack READMEs (stopped in this step) gets the rest.

The object store, the manifests, the overrides, the cleanup records and ``mirrors.json`` stay
as they are (D67 9). Nothing is migrated while ``games/`` holds a link or junction (a rename
or a written file would land elsewhere) or while a backup's temporary snapshot folder is there
(``enginesave remove --tmp`` cleans those up) or while one record is in two folders (1.0.0,
D70 1: which one is the record is not known); the refusals are a :class:`UsageError`.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from enginesave import layout
from enginesave.errors import EngineSaveError, FatalError, StopCheck, UsageError
from enginesave.fsutil import atomic_write_text, describe_error, is_reparse_point, long_path
from enginesave.repo import (
    CATALOG,
    FORMAT_VERSION,
    GAME_FILE,
    GAME_ID_RE,
    GAMES_DIR,
    OLD_FORMAT,
    SNAPSHOTS_DIR,
    TMP_PREFIX,
    Repo,
    dump_json,
    folder_name_for,
    record_version,
    valid_folder,
)

BACKUP_PREFIX = f"{CATALOG}.bak-"
RENAME, DONE, ADOPT, MISSING = "rename", "done", "adopt", "missing"


@dataclass(frozen=True)
class FolderMove:
    """One record's folder: ``rename`` (``games/<id>`` → ``games/<new>``), ``done`` (an
    earlier run did it), ``adopt`` (renamed by an interrupted run, not in the catalog yet) or
    ``missing`` (the record has no folder: ``new`` is the name its next backup will use)."""

    game_id: str
    display_name: str
    old: str
    new: str
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "display_name": self.display_name,
            "from": self.old,
            "to": self.new,
            "state": self.state,
        }


@dataclass
class MigrationPlan:
    root: Path
    format_version: int
    moves: list[FolderMove] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    readmes_missing: int = 0  # snapshots without a README.txt (D67 4)

    @property
    def folders_needed(self) -> bool:
        """A library of format 1, or a record without its folder."""
        return self.format_version == OLD_FORMAT or any(m.state != DONE for m in self.moves)

    @property
    def needed(self) -> bool:
        """Anything left to do: folders, or READMEs."""
        return self.folders_needed or self.readmes_missing > 0

    @property
    def renames(self) -> list[FolderMove]:
        return [m for m in self.moves if m.state == RENAME]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": str(self.root),
            "format_version": self.format_version,
            "needed": self.needed,
            "readmes_missing": self.readmes_missing,  # D67 4
            "refusals": list(self.refusals),
            "records": [m.to_dict() for m in self.moves],
        }


@dataclass
class MigrationResult:
    root: Path
    already: bool = False  # nothing was left to do
    backup: str | None = None  # the copy of catalog.json made first
    renamed: int = 0
    recorded: int = 0  # catalog entries given their folder
    readmes: int = 0  # README.txt written into old snapshots (D67 4)
    index: bool = False  # INDEX.txt written (D67 5)
    missing: list[str] = field(default_factory=list)  # records without a folder
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    format_version: int = OLD_FORMAT
    cancelled: bool = False

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "repo": str(self.root),
            "already_migrated": self.already,
            "catalog_backup": self.backup,
            "renamed": self.renamed,
            "recorded": self.recorded,
            "readmes": self.readmes,
            "index": self.index,
            "missing": list(self.missing),
            "failed": list(self.failed),
            "notes": list(self.notes),
            "format_version": self.format_version,
        }
        if self.cancelled:  # D58: only when it was stopped
            data["cancelled"] = True
        return data


# ----------------------------------------------------------------------------------- plan


def refusals(repo: Repo) -> list[str]:
    """Why ``repo`` cannot be migrated now (D67 8): links or junctions in ``games/`` (also a
    record's ``snapshots`` folder or a snapshot), temporary snapshot folders."""
    found: list[str] = []
    games = repo.root / GAMES_DIR
    links: list[str] = []
    if os.path.lexists(games) and is_reparse_point(games):
        links.append(GAMES_DIR)
    elif games.is_dir():
        for record in sorted(games.iterdir()):
            if is_reparse_point(record):
                links.append(f"{GAMES_DIR}\\{record.name}")
                continue
            snapshots = record / SNAPSHOTS_DIR
            if os.path.lexists(snapshots) and is_reparse_point(snapshots):
                links.append(f"{GAMES_DIR}\\{record.name}\\{SNAPSHOTS_DIR}")
            elif snapshots.is_dir():
                links += [
                    f"{GAMES_DIR}\\{record.name}\\{SNAPSHOTS_DIR}\\{s.name}"
                    for s in sorted(snapshots.iterdir())
                    if is_reparse_point(s)
                ]
    if links:
        shown = ", ".join(links[:3]) + (", ..." if len(links) > 3 else "")
        found.append(
            f"{len(links)} link(s) or junction(s) where the library has its folders ({shown}): "
            "renaming or writing there could reach another place; replace them with real "
            "folders first"
        )
    for game_id, names in sorted(_folders_by_id(games).items()):  # D70 1
        if len(names) > 1:
            shown = " and ".join(f"{GAMES_DIR}\\{n}" for n in names[:3])
            found.append(
                f"record {game_id} is in two folders ({shown}); keep the right one and move "
                "the other out of the library first"
            )
    tmp = [p for p in repo.stale_tmp_dirs() if p.name.startswith(TMP_PREFIX)]
    tmp = [p for p in tmp if p.parent.name == SNAPSHOTS_DIR]
    if tmp:
        found.append(
            f"{len(tmp)} temporary snapshot folder(s) of an interrupted or running backup "
            f'(such as {tmp[0]}); clean them up first: enginesave remove --repo "{repo.root}" '
            '--tmp --yes (or the window\'s "Clean up temporary folders…" on the library page)'
        )
    return found


def plan_migration(repo: Repo) -> MigrationPlan:
    """What :func:`execute_migration` would do: each record's folder name (D67 1), in the
    catalog's order; the records that already have their folder keep it."""
    catalog = repo.load_catalog()
    plan = MigrationPlan(repo.root, int(catalog["format_version"]))
    plan.refusals = refusals(repo)
    games = repo.root / GAMES_DIR
    entries = [
        e
        for e in catalog["games"]
        if isinstance(e, dict)
        and isinstance(e.get("game_id"), str)
        and GAME_ID_RE.match(e["game_id"])
    ]
    strays = _strays(games, {e["game_id"] for e in entries})
    taken: set[str] = set()
    with contextlib.suppress(OSError):
        taken = {p.name.casefold() for p in games.iterdir()} if games.is_dir() else set()
    settled: dict[str, FolderMove] = {}
    for entry in entries:  # first the folders that are settled: their names are taken
        game_id, name = entry["game_id"], str(entry.get("display_name") or "")
        folder = entry.get("folder")
        if valid_folder(folder) and (games / str(folder)).is_dir():
            settled[game_id] = FolderMove(game_id, name, str(folder), str(folder), DONE)
        elif game_id in strays and not (games / game_id).is_dir():
            settled[game_id] = FolderMove(game_id, name, strays[game_id], strays[game_id], ADOPT)
        elif valid_folder(folder) and plan.format_version != OLD_FORMAT:
            # M45 review: migrated already, the record has no folder yet (its next backup
            # makes it): nothing to do
            settled[game_id] = FolderMove(game_id, name, "", str(folder), DONE)
    for entry in entries:
        game_id, name = entry["game_id"], str(entry.get("display_name") or "")
        if game_id in settled:
            plan.moves.append(settled[game_id])
            continue
        version = None
        with contextlib.suppress(EngineSaveError, OSError, ValueError):
            version = record_version(_read(games / game_id / GAME_FILE))
        new = folder_name_for(name, version, game_id, taken - {game_id.casefold()})
        taken.add(new.casefold())
        state = RENAME if (games / game_id).is_dir() else MISSING
        plan.moves.append(FolderMove(game_id, name, game_id if state == RENAME else "", new, state))
    plan.readmes_missing = sum(1 for _ in layout.missing_readmes(repo))
    return plan


def _read(path: Path) -> dict[str, Any]:
    with open(long_path(path), encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def folder_id(folder: Path) -> str | None:
    """The record id in a folder's ``game.json``, or ``None`` (none, unreadable)."""
    with contextlib.suppress(OSError, ValueError):
        game_id = _read(folder / GAME_FILE).get("game_id")
        if isinstance(game_id, str) and GAME_ID_RE.match(game_id):
            return game_id
    return None


def _folders_by_id(games: Path) -> dict[str, list[str]]:
    """Every record folder under ``games/`` by the id it belongs to: its ``game.json``'s, or
    for a folder named ``<id>`` without a readable one, its name."""
    found: dict[str, list[str]] = {}
    with contextlib.suppress(OSError):
        for path in sorted(games.iterdir()) if games.is_dir() else []:
            if not path.is_dir() or is_reparse_point(path):
                continue
            game_id = folder_id(path) or (path.name if GAME_ID_RE.match(path.name) else None)
            if game_id is not None:
                found.setdefault(game_id.casefold(), []).append(path.name)
    return found


def _strays(games: Path, ids: set[str]) -> dict[str, str]:
    """Folders under ``games/`` not named by their own id, by the record id in their
    ``game.json``: renamed by an interrupted run (also to a name shaped like an id, R14)."""
    found: dict[str, str] = {}
    with contextlib.suppress(OSError):
        for path in sorted(games.iterdir()) if games.is_dir() else []:
            if not path.is_dir():
                continue
            game_id = folder_id(path)
            if game_id in ids and game_id.casefold() != path.name.casefold():
                found.setdefault(str(game_id), path.name)
    return found


# -------------------------------------------------------------------------------- execute


def backup_catalog(repo: Repo, when: datetime | None = None) -> Path:
    """``catalog.json.bak-<local time>``, a byte-for-byte copy; never over an earlier one."""
    stamp = (when or datetime.now()).astimezone().strftime("%Y%m%d-%H%M%S")
    target = repo.root / f"{BACKUP_PREFIX}{stamp}"
    n = 1
    while target.exists():
        n += 1
        target = repo.root / f"{BACKUP_PREFIX}{stamp}-{n}"
    shutil.copy2(long_path(repo.catalog_path), long_path(target))
    return target


def execute_migration(
    repo: Repo,
    plan: MigrationPlan | None = None,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: StopCheck = None,
) -> MigrationResult:
    """Migrate ``repo`` (see the module docstring); refusals raise :class:`UsageError` before
    anything is written. Stopped (D58) between records: what was renamed is recorded, the
    format stays 1 and running it again goes on."""
    plan = plan_migration(repo) if plan is None else plan
    result = MigrationResult(repo.root, format_version=plan.format_version)
    if plan.refusals:
        raise UsageError("the library is not migrated: " + "; ".join(plan.refusals))
    if not plan.needed:
        result.already = True
        return result
    if plan.folders_needed:
        _migrate_folders(repo, plan, result, progress, should_stop)
        if result.failed or result.cancelled:  # running it again goes on with the rest
            return result
    _write_readmes(repo, result, should_stop)
    result.index = layout.write_index(repo)
    return result


def _migrate_folders(
    repo: Repo,
    plan: MigrationPlan,
    result: MigrationResult,
    progress: Callable[[int, int, str], None] | None,
    should_stop: StopCheck,
) -> None:
    """Steps 1-3: the catalog copied, the folders renamed record by record, format 2."""
    games = repo.root / GAMES_DIR
    repo.allow_old_format = True
    try:
        try:
            result.backup = str(backup_catalog(repo))
        except OSError as exc:
            raise FatalError(
                f"cannot copy {CATALOG} before migrating ({describe_error(exc)}); nothing changed"
            ) from exc
        catalog = repo.load_catalog()
        if catalog.get("format_version") == OLD_FORMAT:  # D70 1: before the first rename
            catalog["format_version"] = FORMAT_VERSION
            repo.save_catalog(catalog)
            result.format_version = FORMAT_VERSION
        entries = {e.get("game_id"): e for e in catalog["games"] if isinstance(e, dict)}
        moves = [m for m in plan.moves if m.state != DONE]
        for index, move in enumerate(moves):
            if should_stop is not None and should_stop():
                result.cancelled = True
                return
            if progress is not None:
                progress(index, len(moves), move.display_name or move.game_id)
            if move.state == RENAME:
                problem = _rename(games, move)
                if problem is not None:
                    result.failed.append(f"{move.display_name or move.game_id}: {problem}")
                    continue
                result.renamed += 1
            elif move.state == MISSING:
                result.missing.append(move.display_name or move.game_id)
            entry = entries.get(move.game_id)
            if entry is not None:
                entry["folder"] = move.new
                repo.set_folder(move.game_id, move.new)
                repo.save_catalog(catalog)
                result.recorded += 1
        _raise_format(repo, catalog, result)  # every game.json; a failed one keeps <id>
    finally:
        repo.allow_old_format = False


def _rename(games: Path, move: FolderMove) -> str | None:
    """``games/<old>`` to ``games/<new>``, the record's id in ``game.json`` checked before and
    after (D70 1); what went wrong, or ``None``."""
    old, new = games / move.old, games / move.new
    found = folder_id(old)
    if found is not None and found.casefold() != move.game_id.casefold():
        return f"{GAMES_DIR}\\{move.old} holds the record {found}, not {move.game_id}; not renamed"
    try:
        os.rename(long_path(old), long_path(new))
    except OSError as exc:
        return f"cannot rename {GAMES_DIR}\\{move.old} to {move.new} ({describe_error(exc)})"
    if found is not None and folder_id(new) != found:
        with contextlib.suppress(OSError):
            os.rename(long_path(new), long_path(old))
        return f"{GAMES_DIR}\\{move.new} does not read back as {move.game_id}; put back"
    return None


def _write_readmes(repo: Repo, result: MigrationResult, should_stop: StopCheck) -> None:
    """Step 4: a ``README.txt`` for every snapshot without one (D67 4)."""
    for game_id, snapshot_id in list(layout.missing_readmes(repo)):
        if should_stop is not None and should_stop():
            result.cancelled = True
            return
        try:
            doc: dict[str, Any] = repo.load_game(game_id)
        except EngineSaveError:
            doc = {"game_id": game_id}
        try:
            manifest = repo.load_manifest(game_id, snapshot_id)
            folder = repo.snapshot_dir(game_id, snapshot_id)
            layout.write_readme(folder, layout.snapshot_readme(doc, manifest))
            result.readmes += 1
        except (EngineSaveError, OSError) as exc:  # the snapshot is as it was; said
            text = describe_error(exc) if isinstance(exc, OSError) else str(exc)
            name = doc.get("display_name") or game_id
            result.notes.append(f"{name} {snapshot_id}: no README.txt ({text})")


def write_missing_readmes(repo: Repo, should_stop: StopCheck = None) -> MigrationResult:
    """D70 1: a library of format 2 whose snapshots lack their ``README.txt`` (a migration
    stopped in its last step) gets them -- the window does this in the background when it
    opens such a library; also the ``INDEX.txt``."""
    result = MigrationResult(repo.root, format_version=repo.format_version)
    _write_readmes(repo, result, should_stop)
    if not result.cancelled:
        result.index = layout.write_index(repo)
    return result


def _raise_format(repo: Repo, catalog: dict[str, Any], result: MigrationResult) -> None:
    """Every ``game.json``, then the catalog, to ``format_version`` 2 (D67 8)."""
    for entry in catalog["games"]:
        game_id = entry.get("game_id") if isinstance(entry, dict) else None
        if not isinstance(game_id, str) or not GAME_ID_RE.match(game_id):
            continue
        path = repo.game_dir(game_id) / GAME_FILE
        if not os.path.isfile(long_path(path)):
            continue
        try:
            doc = _read(path)
            if doc.get("format_version") == OLD_FORMAT:
                atomic_write_text(path, dump_json({**doc, "format_version": FORMAT_VERSION}))
        except (OSError, ValueError) as exc:  # read as it is: both formats are readable
            text = describe_error(exc) if isinstance(exc, OSError) else str(exc)
            result.notes.append(f"{entry.get('display_name') or game_id} {GAME_FILE}: {text}")
    catalog["format_version"] = FORMAT_VERSION
    repo.save_catalog(catalog)
    result.format_version = FORMAT_VERSION


def migrate_repo(
    repo: Repo,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: StopCheck = None,
) -> MigrationResult:
    """Plan and migrate in one go (the mirror's target, D67 8)."""
    return execute_migration(repo, progress=progress, should_stop=should_stop)
