"""Backup (plan section 8): plan → copy + SHA-256 → read-back verification → atomic commit.

Sources are only ever read. A per-file read problem is recorded and makes the snapshot
``partial``; any other failure discards the temporary snapshot folder, so an incomplete snapshot
is never committed and never reported as a success."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import logging
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enginesave import __version__, layout, registry, store
from enginesave.discovery import DEFAULT_MAX_DEPTH
from enginesave.engines.base import Detector
from enginesave.engines.common import garbled, misread
from enginesave.engines.renpy.paths import TOKENS_DIR, default_saves_root
from enginesave.errors import EngineSaveError, StopCheck, Stopped, UsageError, stop_point
from enginesave.fsutil import (
    FileEntry,
    WalkError,
    describe_error,
    dir_stats,
    is_within,
    iso_from_ns,
    iso_utc,
    long_path,
    path_key,
    safe_join,
    sha256_file,
    source_files,
    utc_now,
)
from enginesave.identity import (
    game_roots,
    match_known_game,
    match_known_games,
    match_unlinked,
)
from enginesave.models import (
    BACKUP_READY,
    DetectedGame,
    GameStatus,
    RootToken,
    SaveSource,
    ScanReport,
    SourceKind,
    SourceStatus,
    UnlinkedSaveDir,
)
from enginesave.repo import (
    FORMAT_VERSION,
    MANIFEST_FORMAT,
    Repo,
    cleaned_at,
    destination_state,
    record_version,
)
from enginesave.roots import ENGINE_PROFILE_TOKENS
from enginesave.scan import ScanOptions, run_scan
from enginesave.store import SourceReadError, VerifyMismatch, storage_kind

if TYPE_CHECKING:
    from enginesave.scan_cache import ScanCache


log = logging.getLogger("enginesave.backup")


@dataclass
class PlannedSource:
    source: SaveSource
    files: list[FileEntry]
    walk_errors: list[WalkError] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class PlannedGame:
    kind: str  # "game" | "unlinked"
    display_name: str
    version: str | None
    sources: list[PlannedSource]
    game: DetectedGame | None = None
    unlinked: UnlinkedSaveDir | None = None
    game_id: str | None = None  # known catalog record; None → a new record is created
    # 1.2.0 (D73 14): {"game_root", "game_version"} of the install the snapshot comes from
    from_install: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "display_name": self.display_name,
            "version": self.version,
            "game_root": self.game.game_root if self.game else None,
            "game_id": self.game_id,
            "from_install": self.from_install,
            "sources": [
                {
                    "source_id": ps.source.source_id,
                    "kind": ps.source.kind.value,
                    "path": ps.source.path,
                    "status": ps.source.status.value,
                    "file_count": len(ps.files),
                    "total_bytes": ps.total_bytes,
                }
                for ps in self.sources
            ],
        }


@dataclass
class BackupPlan:
    dest: Path
    repo_state: str  # "new" | "existing"
    games: list[PlannedGame] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dest": str(self.dest),
            "repo_state": self.repo_state,
            "games": [g.to_dict() for g in self.games],
            "skipped": self.skipped,
        }


@dataclass
class GameResult:
    display_name: str
    kind: str
    # "ok" | "partial" | "failed"; 1.2.0 (D73 2): "unchanged" with --skip-unchanged
    status: str
    game_id: str | None = None
    snapshot_id: str | None = None
    file_count: int = 0
    total_bytes: int = 0
    errors: list[dict] = field(default_factory=list)
    changed_sources: list[str] = field(default_factory=list)
    message: str = ""
    storage: str | None = None  # manifest "storage" of the committed snapshot


SUCCESS = ("ok", "unchanged")  # 1.2.0 (D73 2): nothing new to keep is a success too


@dataclass
class BackupResult:
    results: list[GameResult] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    cancelled: bool = False  # D58: stopped between files; what is in ``results`` is done
    pruned: dict[str, Any] | None = None  # 1.2.0 (D73 3): what --keep-auto removed
    mirror: dict[str, Any] | None = None  # 1.2.0 (D73 16): the mirror after the backup

    @property
    def exit_code(self) -> int:
        return 1 if any(r.status not in SUCCESS for r in self.results) else 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"results": [vars(r) for r in self.results], "skipped": self.skipped}
        if self.cancelled:  # 0.9.0: only when it was stopped
            data["cancelled"] = True
        if self.pruned is not None:  # 1.2.0: only with --keep-auto
            data["pruned"] = self.pruned
        if self.mirror is not None:  # 1.2.0: only with --mirror or --auto-mirror
            data["mirror"] = self.mirror
        return data


# -------------------------------------------------------------------------------- planning


def check_destination(dest: Path, roots: Sequence[Path], saves_root: Path) -> None:
    """``--dest`` may not overlap a scan root or the Ren'Py saves root (exit code 2)."""
    for root in roots:
        if is_within(dest, root) or is_within(root, dest):
            raise UsageError(f"--dest must not be inside a scan root or contain one: {root}")
    if is_within(dest, saves_root) or is_within(saves_root, dest):
        raise UsageError(f"--dest must not overlap the Ren'Py saves root: {saves_root}")


def _plan_source(source: SaveSource) -> PlannedSource:
    if source.kind is SourceKind.REGISTRY and registry.is_key_path(source.path):  # D65: .reg
        data = registry.export_key(registry.subkey_of(source.path))
        files = (
            [FileEntry(registry.REG_FILE, Path(registry.REG_FILE), len(data), 0)] if data else []
        )
        return PlannedSource(source, files, [])
    files, errors = source_files(Path(source.path), source.patterns, source.excludes)
    return PlannedSource(source, files, errors)


def _registry_files(
    repo: Repo, tmp: Path, src: SaveSource, data_dir: Path, now: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """``(files, errors, stable)`` of a registry source (D65, Unity's PlayerPrefs): the key
    read (never written) and kept as ``PlayerPrefs.reg``, read twice to see it did not change
    meanwhile. The export is written in the snapshot's temporary folder, stored like any
    file, then removed."""
    subkey = registry.subkey_of(src.path)
    data = registry.export_key(subkey)
    if data is None:
        return [], [{"source_id": src.source_id, "path": registry.REG_FILE,
                     "error": f"the registry key {src.path} is not there"}], True  # fmt: skip
    staged = tmp / f"registry-{src.source_id}.reg"
    try:  # writing into the library: an error there fails the backup, as for any file
        with open(long_path(staged), "wb") as handle:
            handle.write(data)
        digest, size, storage = store.copy_and_verify(
            repo, staged, safe_join(data_dir, registry.REG_FILE)
        )
    except (SourceReadError, VerifyMismatch) as exc:
        return (
            [],
            [{"source_id": src.source_id, "path": registry.REG_FILE, "error": str(exc)}],
            True,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(long_path(staged))
    entry = {
        "source_id": src.source_id,
        "path": registry.REG_FILE,
        "size": size,
        "sha256": digest,
        "mtime": iso_utc(now),
        "_storage": storage,
    }
    return [entry], [], registry.export_key(subkey) == data


def tokens_source(saves_root: Path) -> SaveSource | None:
    path = saves_root / TOKENS_DIR
    exists, count, size = dir_stats(path)
    if not count:
        return None
    source = SaveSource(
        "machine_tokens",
        SourceKind.MACHINE_TOKENS,
        str(path),
        RootToken.RENPY_SAVES_ROOT,
        TOKENS_DIR,
        SourceStatus.USER_CONFIRMED,
        "include_tokens",
        notes=["contains Ren'Py save-signing private keys (security_keys.txt)"],
    )
    source.exists, source.file_count, source.total_bytes = exists, count, size
    return source


def _skip_reason(game: DetectedGame, allowed: set[SourceStatus]) -> str:
    if game.status is GameStatus.UNSUPPORTED:
        return "unsupported engine or structure"
    states = ", ".join(f"{s.source_id}: {s.status.value}" for s in game.sources) or "none"
    wanted = "/".join(sorted(s.value for s in allowed))
    return f"no {wanted} source with files ({states})"


def _select(games: list[DetectedGame], select: Sequence[str]) -> list[DetectedGame]:
    wanted = {token.casefold() for token in select}
    chosen, hit = [], set()
    for game in games:
        keys = {
            k.casefold()
            for k in (
                game.known_game_id,
                game.identity.renpy_save_directory,
                Path(game.game_root).name,
            )
            if k
        }
        if keys & wanted:
            chosen.append(game)
            hit |= keys & wanted
    if missing := wanted - hit:
        raise UsageError(f"--select matched no scanned game: {', '.join(sorted(missing))}")
    return chosen


def _select_roots(games: list[DetectedGame], roots: Sequence[str | Path]) -> list[DetectedGame]:
    """Games whose install folder (``game_root`` or a grouped install) is one of ``roots``."""
    wanted = {path_key(root): str(root) for root in roots}
    chosen, hit = [], set()
    for game in games:
        keys = {path_key(game.game_root)}
        keys.update(path_key(i["game_root"]) for i in game.installs if i.get("game_root"))
        if keys & wanted.keys():
            chosen.append(game)
            hit |= keys & wanted.keys()
    if missing := [wanted[k] for k in wanted.keys() - hit]:
        raise UsageError(
            "selected game folders were not found by the scan (not the folder of a game "
            f"EngineSave recognises): {', '.join(missing)}"
        )
    return chosen


def from_install(
    game: DetectedGame,
    sources: Sequence[SaveSource],
    select_roots: Sequence[str | Path] | None = None,
) -> dict[str, Any] | None:
    """1.2.0 (D73 14): the install a snapshot of ``game`` comes from, ``{"game_root",
    "game_version"}``: the one chosen (``--game-root``, the window's ticks), else the game's
    only install, else the one install all of its local sources are in; ``None`` when several
    installs take part (a Ren'Py game whose versions were grouped, D9)."""
    installs = [i for i in game.installs if i.get("game_root")]
    chosen: list[dict[str, Any]] = []
    if select_roots:
        wanted = {path_key(root) for root in select_roots}
        chosen = [i for i in installs if path_key(i["game_root"]) in wanted]
    if not chosen and len(installs) == 1:
        chosen = installs
    if not chosen:
        roots = {path_key(s.install_root) for s in sources if s.install_root}
        chosen = [i for i in installs if path_key(i["game_root"]) in roots]
    if len(chosen) != 1:
        return None
    return {"game_root": str(chosen[0]["game_root"]), "game_version": chosen[0].get("game_version")}


def plan_backup(
    report: ScanReport,
    dest: Path,
    *,
    repo_state: str,
    known: Sequence[dict] = (),
    overrides: Sequence[dict] = (),
    select: Sequence[str] | None = None,
    select_roots: Sequence[str | Path] | None = None,
    include_candidates: bool = False,
    include_unlinked: Sequence[str] | None = None,
    include_tokens: bool = False,
) -> BackupPlan:
    """Decide what would be copied. ``include_unlinked=[]`` means every unlinked folder.

    ``select`` takes CLI tokens (game id, save directory, folder name); ``select_roots`` takes
    exact install folders (the GUI's check boxes), and ``[]`` selects no game at all.
    """
    allowed = set(BACKUP_READY) | ({SourceStatus.CANDIDATE} if include_candidates else set())
    plan = BackupPlan(dest, repo_state)
    tokens = tokens_source(Path(report.renpy_saves_root)) if include_tokens else None
    games = _select(report.games, select) if select else report.games
    if select_roots is not None:
        games = _select_roots(games, select_roots)
    # D48 2: every game of the scan takes part, so that one game with the folder the library
    # knows keeps its record, also when only the other one is backed up now
    matched = match_known_games(report.games, known, overrides) if known else {}
    for game in games:
        eligible = [s for s in game.sources if s.status in allowed and s.file_count > 0]
        if game.status is GameStatus.UNSUPPORTED or not eligible:
            reason = _skip_reason(game, allowed)
            plan.skipped.append(
                {"name": game.display_name, "path": game.game_root, "reason": reason}
            )
            continue
        sources = [_plan_source(s) for s in eligible]
        if tokens is not None:
            sources.append(_plan_source(tokens))
        game_id = game.known_game_id or matched.get(id(game))
        plan.games.append(
            PlannedGame(
                "game",
                game.display_name,
                game.identity.config_version,
                sources,
                game,
                None,
                game_id,
                from_install(game, eligible, select_roots),
            )
        )
    if include_unlinked is not None:
        available = {u.name.casefold(): u for u in report.unlinked}
        if include_unlinked:
            missing = [n for n in include_unlinked if n.casefold() not in available]
            if missing:
                raise UsageError(f"--include-unlinked: no such folder: {', '.join(missing)}")
            chosen = [available[n.casefold()] for n in include_unlinked]
        else:
            chosen = list(report.unlinked)
        for item in chosen:
            if not item.file_count:
                plan.skipped.append(
                    {"name": item.name, "path": item.path, "reason": "empty folder"}
                )
                continue
            source = SaveSource(
                "global",
                SourceKind.GLOBAL,
                item.path,
                RootToken.RENPY_SAVES_ROOT,
                item.name,
                SourceStatus.USER_CONFIRMED,
                "unlinked_selected",
            )
            source.exists = True
            source.file_count, source.total_bytes = item.file_count, item.total_bytes
            sources = [_plan_source(source)]
            if tokens is not None:
                sources.append(_plan_source(tokens))
            game_id = item.known_game_id or match_unlinked(item.name, known)
            plan.games.append(
                PlannedGame("unlinked", item.name, item.version_hint, sources, None, item, game_id)
            )
    for planned in plan.games:
        for ps in planned.sources:
            if ps.source.kind is SourceKind.REGISTRY and registry.is_key_path(ps.source.path):
                continue  # a registry key, not a folder
            if is_within(dest, ps.source.path) or is_within(ps.source.path, dest):
                raise UsageError(f"--dest overlaps a save source: {ps.source.path}")
    return plan


def _state(files: Sequence[FileEntry]) -> dict[str, tuple[int, int]]:
    return {f.rel: (f.size, f.mtime_ns) for f in files}


def build_snapshot(
    repo: Repo,
    tmp: Path,
    planned: PlannedGame,
    game_id: str,
    snapshot_id: str,
    now: datetime,
    reason: str,
    should_stop: StopCheck = None,
) -> dict[str, Any]:
    """Store every planned source where it belongs in the snapshot (``tmp/data/<root
    token>/<relative root>``, D67 3) and return the manifest. ``should_stop`` is asked before
    each file (D58): :class:`Stopped` leaves the half-built snapshot for the caller to
    discard."""
    files: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    storages: set[str] = set()
    taken: list[str] = []
    for ps in planned.sources:
        src = ps.source
        origin = Path(src.path)
        data_rel = layout.data_dir_for(
            {
                "source_id": src.source_id,
                "root_token": src.root_token.value,
                "relative_root": src.relative_root,
            },
            taken,
        )
        data_dir = safe_join(tmp, data_rel)
        os.makedirs(long_path(data_dir), exist_ok=True)
        if src.kind is SourceKind.REGISTRY and registry.is_key_path(src.path):
            stop_point(should_stop)
            stored, problems, stable = _registry_files(repo, tmp, src, data_dir, now)
            storages.update(entry.pop("_storage") for entry in stored)
            files.extend(stored)
            errors.extend(problems)
            sources.append(
                _manifest_source(src, data_rel, len(stored), sum(f["size"] for f in stored), stable)
            )
            continue
        # fresh listing right before copying (D14); only the matching files of a source with
        # patterns (D65 2)
        before, walk_errors = source_files(origin, src.patterns, src.excludes)
        copied = copied_bytes = 0
        for entry in before:
            stop_point(should_stop)
            try:
                digest, size, storage = store.copy_and_verify(
                    repo, entry.path, safe_join(data_dir, entry.rel)
                )
            except (SourceReadError, VerifyMismatch) as exc:
                errors.append({"source_id": src.source_id, "path": entry.rel, "error": str(exc)})
                continue
            storages.add(storage)
            files.append(
                {
                    "source_id": src.source_id,
                    "path": entry.rel,
                    "size": size,
                    "sha256": digest,
                    "mtime": iso_from_ns(entry.mtime_ns),
                }
            )
            copied += 1
            copied_bytes += size
        errors.extend(
            {"source_id": src.source_id, "path": e.rel, "error": e.error} for e in walk_errors
        )
        after, _ = source_files(origin, src.patterns, src.excludes)
        stable = _state(before) == _state(after)
        sources.append(_manifest_source(src, data_rel, copied, copied_bytes, stable))
    complete = not errors and all(s["consistency"] == "stable" for s in sources)
    manifest = {
        "format_version": MANIFEST_FORMAT,
        "snapshot_id": snapshot_id,
        "created_at": iso_utc(now),
        "tool_version": __version__,
        "game_id": game_id,
        "reason": reason,
        "status": "complete" if complete else "partial",
        "storage": storage_kind(storages),
        "sources": sources,
        "files": files,
        "errors": errors,
    }
    if planned.kind == "game" and reason != "pre_restore":  # 1.2.0 (D73 14)
        manifest["from_install"] = planned.from_install
    return manifest


def _new_game_doc(planned: PlannedGame, game_id: str, now: str) -> dict[str, Any]:
    if planned.game is not None:
        game = planned.game
        identity = game.identity.stored()
        engine, engine_version, name = game.engine, game.engine_version, game.display_name
    else:
        assert planned.unlinked is not None
        identity = {
            "renpy_save_directory": planned.unlinked.name,
            "config_name": None,
            "build_name": None,
            "config_version": None,
            "exe_basename": None,
            "root_dir_name": None,
        }
        engine, engine_version, name = "renpy", None, planned.unlinked.name
    return {
        "format_version": FORMAT_VERSION,
        "game_id": game_id,
        "kind": planned.kind,
        "engine": engine,
        "engine_version": engine_version,
        "display_name": name,
        "identity": identity,
        "installs": [],
        "source_templates": [],
        "created_at": now,
        "updated_at": now,
    }


def touch_install(installs: list[dict], install: dict, now: str) -> None:
    """Add the install folder to a record's ``installs`` or update it there; none is ever
    dropped (a backup of one install keeps the others, D73 14). 1.2.0 (D73 7): its
    ``game_version`` too, when the scan read one."""
    for item in installs:
        if path_key(item["game_root"]) == path_key(install["game_root"]):
            item["last_seen"] = now
            item.pop("removed_at", None)  # installed again after a cleanup (D26)
            if install.get("config_version"):
                item["config_version"] = install["config_version"]
            if install.get("game_version"):
                item["game_version"] = install["game_version"]
                item["game_version_source"] = install.get("game_version_source")
            return
    added = {
        "game_root": install["game_root"],
        "config_version": install.get("config_version"),
        "first_seen": now,
        "last_seen": now,
    }
    if install.get("game_version"):
        added["game_version"] = install["game_version"]
        added["game_version_source"] = install.get("game_version_source")
    installs.append(added)


def record_snapshot(
    repo: Repo, catalog: dict, planned: PlannedGame, game_id: str, manifest: dict, now: str
) -> dict:
    """Create/update ``game.json`` (identity only fills blanks) and the catalog entry."""
    doc = (
        repo.load_game(game_id) if repo.has_game(game_id) else _new_game_doc(planned, game_id, now)
    )
    rekeyed = False
    if planned.game is not None:
        rekeyed = _rekey(doc, planned.game)
        for key, value in planned.game.identity.stored().items():
            if value and not doc["identity"].get(key):
                doc["identity"][key] = value
        if not doc.get("engine_version"):
            doc["engine_version"] = planned.game.engine_version
        for install in planned.game.installs:
            touch_install(doc["installs"], install, now)
    known = {t["source_id"]: i for i, t in enumerate(doc["source_templates"])}
    for source in manifest["sources"]:
        template = source_template(source)
        index = known.get(source["source_id"])
        if index is None:
            known[source["source_id"]] = len(doc["source_templates"])
            doc["source_templates"].append(template)
        elif _moved(doc["source_templates"][index], template):
            doc["source_templates"][index] = template
    doc["updated_at"] = now
    repo.save_game(doc)
    update_catalog_entry(repo, catalog, doc)
    if rekeyed:
        _rename_garbled_folder(repo, catalog, game_id)
    return doc


def _rename_garbled_folder(repo: Repo, catalog: dict, game_id: str) -> None:
    """1.0.0 (D70 9): the record's folder was named after the garbled title (S-210): it gets
    the name of the title read right now, as 「按当前名字重命名文件夹…」 would (a mirror
    follows by the record id); a folder of another name is the user's and stays."""
    folder = repo.folder_name(game_id)
    if not garbled(folder):
        return
    try:
        old, new = repo.rename_folder(game_id)
    except EngineSaveError as exc:
        log.warning("the record folder %s keeps its garbled name: %s", folder, exc)
        return
    if old != new:
        log.info("record folder renamed after the title read right: %s -> %s", old, new)
        for entry in catalog.get("games", []):  # the caller's catalog says it too
            if isinstance(entry, dict) and entry.get("game_id") == game_id:
                entry["folder"] = new


def _rekey(doc: dict[str, Any], game: DetectedGame) -> bool:
    """A record keyed by 0.10.0's garbled reading of the title (its ``legacy_key``, or a key
    with replacement characters, M44 review) takes this reading: the key, the title, and the
    name when it was that title. One 0.10.0 read right (a Japanese title without kana, which
    a Chinese system now reads as GBK) is kept as it is: it still matches by ``legacy_key``."""
    identity = doc["identity"]
    stored, new = identity.get("engine_key"), game.identity.engine_key
    if not stored or stored == new or not garbled(str(stored)):
        return False
    if stored != game.identity.legacy_key and not misread(str(stored)):
        return False  # another key: the record's own, kept
    old_title = identity.get("title")
    identity["engine_key"] = new
    if game.identity.title:
        identity["title"] = game.identity.title
    if doc.get("display_name") in {old_title, stored} and game.display_name:
        doc["display_name"] = game.display_name
    return True


def _manifest_source(
    src: SaveSource, data_rel: str, copied: int, copied_bytes: int, stable: bool
) -> dict[str, Any]:
    extra = {  # 0.10.0, optional: which files of the folder (D65 2)
        key: list(value)
        for key, value in (("patterns", src.patterns), ("excludes", src.excludes))
        if value
    }
    return {
        **extra,
        "source_id": src.source_id,
        "kind": src.kind.value,
        "origin_path": src.path,
        "root_token": src.root_token.value,
        "relative_root": src.relative_root,
        "source_status": src.status.value,
        "method": src.method,
        "file_count": copied,
        "total_bytes": copied_bytes,
        "data_dir": data_rel,
        "consistency": "stable" if stable else "changed_during_backup",
    }


def _moved(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """0.10.0: another engine's source now in another folder of the user profile, or taking
    other files: the record follows its latest backup (the cleanup's checks read it)."""
    tokens = {old.get("root_token"), new.get("root_token")}
    newer = bool(tokens & set(ENGINE_PROFILE_TOKENS)) or any(
        old.get(k) or new.get(k) for k in ("patterns", "excludes")
    )
    return newer and dict(old) != dict(new)


def source_template(source: Mapping[str, Any]) -> dict[str, Any]:
    """What a record remembers of a manifest source (``game.json`` ``source_templates``); a
    source's ``patterns`` (0.10.0, D65 2) only when it has some."""
    template = {k: source[k] for k in ("source_id", "kind", "root_token", "relative_root")}
    for key in ("patterns", "excludes"):
        if source.get(key):
            template[key] = list(source[key])
    return template


def update_catalog_entry(repo: Repo, catalog: dict, doc: dict) -> None:
    ids = repo.snapshot_ids(doc["game_id"])
    entry = {
        "game_id": doc["game_id"],
        "display_name": doc["display_name"],
        "engine": doc["engine"],
        "identity": {
            k: doc["identity"].get(k) for k in ("renpy_save_directory", "config_name", "build_name")
        }
        | ({"engine_key": key} if (key := doc["identity"].get("engine_key")) else {}),
        "latest_snapshot": ids[-1] if ids else None,
        "snapshot_count": len(ids),
        "kind": doc["kind"],
    }
    if not repo.old_format:  # D67 1: the record's folder under games/
        entry["folder"] = repo.folder_name(doc["game_id"])
    if removed := cleaned_at(doc):  # the game was cleaned up (D26); absent otherwise
        entry["cleaned_at"] = removed
    games = catalog["games"]
    for i, existing in enumerate(games):
        if existing.get("game_id") == doc["game_id"]:
            games[i] = entry
            break
    else:
        games.append(entry)
    repo.save_catalog(catalog)


def register_game(
    repo: Repo, catalog: dict, game: DetectedGame, *, new: bool = False
) -> tuple[str, bool]:
    """Catalog record for ``game`` without backing anything up: ``(game_id, created)``.

    Used by ``confirm-source --game-root`` (D21): an existing record found by the 6.6 rules is
    reused; otherwise a new one is written with the game's installs and no snapshots. ``new``
    always writes a new one (「确认 / 关联…」's 新建记录, D48 3).
    """
    if not new:
        known = repo.known_games(catalog)
        game_id = game.known_game_id or match_known_game(game, known, catalog["overrides"])
        if game_id is not None:
            return game_id, False
    repo.require_writable()
    now = iso_utc(utc_now())
    game_id = str(uuid.uuid4())
    doc = _new_game_doc(PlannedGame("game", game.display_name, None, [], game), game_id, now)
    for install in game.installs:
        touch_install(doc["installs"], install, now)
    repo.assign_folder(game_id, doc["display_name"], record_version(doc))
    repo.save_game(doc)
    update_catalog_entry(repo, catalog, doc)
    return game_id, True


def backup_game(
    repo: Repo, catalog: dict, planned: PlannedGame, *, reason: str, should_stop: StopCheck = None
) -> GameResult:
    """One snapshot for one planned game; never raises for copy problems. Stopped (D58), the
    snapshot being assembled is discarded and :class:`Stopped` raised again."""
    now = utc_now()
    game_id = planned.game_id or str(uuid.uuid4())
    result = GameResult(planned.display_name, planned.kind, "failed", game_id=game_id)
    is_new = not repo.has_game(game_id)
    tmp: Path | None = None
    try:
        if is_new:  # D67 1: named after the game, once
            repo.assign_folder(game_id, planned.display_name, _planned_version(planned))
        snapshot_id = repo.next_snapshot_id(game_id, now)
        tmp = repo.new_tmp_dir(game_id, snapshot_id)
        manifest = build_snapshot(
            repo, tmp, planned, game_id, snapshot_id, now, reason, should_stop
        )
        game = _readme_game(repo, planned, game_id)
        result.snapshot_id = repo.commit_snapshot(
            tmp, game_id, manifest, readme=lambda final: layout.snapshot_readme(game, final)
        )
        tmp = None
    except BaseException as exc:
        if tmp is not None:
            repo.discard_tmp(tmp)
        if is_new:
            repo.remove_if_empty(game_id)
        if not isinstance(exc, Exception):
            raise
        result.message = f"snapshot discarded: {describe_error(exc)}"
        return result
    record_snapshot(repo, catalog, planned, game_id, manifest, iso_utc(now))
    result.status = "ok" if manifest["status"] == "complete" else "partial"
    result.file_count = len(manifest["files"])
    result.total_bytes = sum(f["size"] for f in manifest["files"])
    result.errors = manifest["errors"]
    result.storage = manifest["storage"]
    result.changed_sources = [
        s["source_id"] for s in manifest["sources"] if s["consistency"] != "stable"
    ]
    if result.changed_sources:
        result.message = "files changed while copying; close the game and run backup again"
    return result


def _readme_game(repo: Repo, planned: PlannedGame, game_id: str) -> dict[str, Any]:
    """What a snapshot's README says of its game (D67 4): the record's, or the scan's."""
    if repo.has_game(game_id):
        with contextlib.suppress(EngineSaveError):
            return repo.load_game(game_id)
    if planned.game is not None:
        game = planned.game
        return {
            "game_id": game_id,
            "display_name": game.display_name,
            "engine": game.engine,
            "engine_version": game.engine_version,
            "installs": game.installs,
        }
    return {"game_id": game_id, "display_name": planned.display_name, "engine": "renpy"}


def _planned_version(planned: PlannedGame) -> str | None:
    """The version a new record's folder name may carry (D67 1)."""
    if planned.game is None:
        return None
    doc = {"identity": planned.game.identity.to_dict(), "installs": planned.game.installs}
    return record_version(doc)


def execute_backup(
    plan: BackupPlan,
    repo: Repo,
    *,
    reason: str = "manual",
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: StopCheck = None,
    skip_unchanged: bool = False,
    keep_auto: int | None = None,
) -> BackupResult:
    """Back up every planned game; ``progress(done, total, next_name)`` precedes each one.

    ``should_stop`` (D58) is asked between files: the game being copied gets no snapshot
    (its temporary folder is removed), the games done keep theirs, the rest are not started;
    ``cancelled`` says so.

    1.2.0 (D73 2): ``skip_unchanged`` makes no snapshot of a game whose saves are exactly
    its record's latest snapshot (``unchanged``). D73 3: ``keep_auto`` then keeps only the
    newest so many ``auto`` snapshots of each game backed up (``pruned``).
    """
    repo.require_writable()  # D67 8: a library of format 1 is migrated first
    if keep_auto is not None and keep_auto < 1:
        raise UsageError("--keep-auto must be 1 or more")
    catalog = repo.load_catalog()
    result = BackupResult(skipped=list(plan.skipped))
    try:
        for index, planned in enumerate(plan.games):
            stop_point(should_stop)
            if progress is not None:
                progress(index, len(plan.games), planned.display_name)
            same = unchanged_snapshot(repo, planned) if skip_unchanged else None
            if same is not None:
                result.results.append(note_unchanged(repo, catalog, planned, same))
                continue
            done = backup_game(repo, catalog, planned, reason=reason, should_stop=should_stop)
            result.results.append(done)
    except Stopped:
        result.cancelled = True
    pin_shared_folders(repo, plan, result)
    if keep_auto is not None and not result.cancelled:
        ids = [r.game_id for r in result.results if r.game_id and r.status != "failed"]
        result.pruned = prune_auto(repo, ids, keep_auto, should_stop=should_stop)
    if result.results:
        layout.write_index(repo)  # D67 5
    return result


# ------------------------------------------------- unchanged saves, automatic ones (D73 2, 3)


def unchanged_snapshot(repo: Repo, planned: PlannedGame) -> str | None:
    """The record's latest snapshot (of any reason) when the saves ``planned`` would back up
    now are exactly what it holds -- the same sources, the same files, byte for byte (SHA-256)
    -- else ``None``. A partial latest snapshot, a file that cannot be read or listed, or a
    record without snapshots never counts as unchanged."""
    game_id = planned.game_id
    if game_id is None or not repo.has_game(game_id):
        return None
    ids = repo.snapshot_ids(game_id)
    if not ids:
        return None
    try:
        manifest = repo.load_manifest(game_id, ids[-1])
    except EngineSaveError:
        return None
    if manifest.get("status") != "complete" or manifest.get("errors"):
        return None

    def where(entry: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            entry.get("root_token"),
            entry.get("relative_root"),
            tuple(entry.get("patterns") or ()),
            tuple(entry.get("excludes") or ()),
        )

    stored_sources = {s.get("source_id"): where(s) for s in manifest.get("sources") or []}
    planned_sources = {
        ps.source.source_id: where(
            {
                "root_token": ps.source.root_token.value,
                "relative_root": ps.source.relative_root,
                "patterns": ps.source.patterns,
                "excludes": ps.source.excludes,
            }
        )
        for ps in planned.sources
    }
    if stored_sources != planned_sources:
        return None
    stored = {
        (f.get("source_id"), os.path.normcase(str(f.get("path")))): (f.get("size"), f.get("sha256"))
        for f in manifest.get("files") or []
    }
    now: dict[tuple[str, str], tuple[int, Path | bytes]] = {}
    for ps in planned.sources:
        src = ps.source
        if src.kind is SourceKind.REGISTRY and registry.is_key_path(src.path):
            data = registry.export_key(registry.subkey_of(src.path))
            if data is None:
                return None
            now[(src.source_id, os.path.normcase(registry.REG_FILE))] = (len(data), data)
            continue
        files, errors = source_files(Path(src.path), src.patterns, src.excludes)
        if errors:
            return None
        for entry in files:
            now[(src.source_id, os.path.normcase(entry.rel))] = (entry.size, entry.path)
    if now.keys() != stored.keys():
        return None
    if any(now[key][0] != stored[key][0] for key in now):  # sizes first: no reading needed
        return None
    for key, (_size, content) in now.items():
        try:
            digest = (
                hashlib.sha256(content).hexdigest()
                if isinstance(content, bytes)
                else sha256_file(content)[0]
            )
        except OSError:
            return None
        if digest != stored[key][1]:
            return None
    return ids[-1]


def note_unchanged(repo: Repo, catalog: dict, planned: PlannedGame, same: str) -> GameResult:
    """No snapshot for a game whose saves are its latest snapshot; the record still learns
    the install folders this scan saw (D73 14), as a backup would have told it."""
    assert planned.game_id is not None
    if planned.game is not None:
        now = iso_utc(utc_now())
        doc = repo.load_game(planned.game_id)
        before = copy.deepcopy(doc.get("installs") or [])
        for install in planned.game.installs:
            touch_install(doc.setdefault("installs", []), install, now)
        if [i.get("game_root") for i in doc["installs"]] != [i.get("game_root") for i in before]:
            doc["updated_at"] = now
            repo.save_game(doc)
            update_catalog_entry(repo, catalog, doc)
        elif doc["installs"] != before:
            repo.save_game(doc)  # versions and times of known installs; the catalog is as it was
    total = sum(ps.total_bytes for ps in planned.sources)
    return GameResult(
        planned.display_name,
        planned.kind,
        "unchanged",
        game_id=planned.game_id,
        file_count=sum(len(ps.files) for ps in planned.sources),
        total_bytes=total,
        message=f"saves unchanged since snapshot {same}; no new snapshot",
    )


def auto_snapshots(repo: Repo, game_id: str) -> list[str]:
    """The record's ``auto`` snapshots, oldest first (a manifest that cannot be read is never
    one)."""
    found = []
    for snapshot_id in repo.snapshot_ids(game_id):
        try:
            if repo.load_manifest(game_id, snapshot_id).get("reason") == "auto":
                found.append(snapshot_id)
        except EngineSaveError:
            continue
    return found


def prune_auto(
    repo: Repo, game_ids: Sequence[str], keep: int, *, should_stop: StopCheck = None
) -> dict[str, Any]:
    """D73 3: keep only the newest ``keep`` ``auto`` snapshots of each record; ``manual``,
    ``import`` and ``pre_restore`` snapshots are never removed here. The removal is the one of
    ``enginesave remove`` (catalog first, then the folders, then the object store)."""
    from enginesave.removal import execute_remove, plan_remove

    requests = []
    for game_id in dict.fromkeys(game_ids):
        doomed = auto_snapshots(repo, game_id)[:-keep]
        if doomed:
            requests.append((game_id, doomed))
    data: dict[str, Any] = {
        "keep": keep,
        "removed": [{"game_id": g, "snapshot_ids": list(s)} for g, s in requests],
        "freed_bytes": 0,
        "leftovers": [],
        "gc_skipped": None,
    }
    if not requests:
        return data
    plan = plan_remove(repo, repo.load_catalog(), requests)
    done = execute_remove(plan, should_stop=should_stop)
    data.update(
        freed_bytes=done.freed_bytes,
        leftovers=list(done.leftovers),
        gc_skipped=done.garbage.skipped,
    )
    if done.cancelled:
        data["cancelled"] = True
    return data


def pin_shared_folders(repo: Repo, plan: BackupPlan, result: BackupResult) -> list[dict]:
    """D48 2: once several records use one save directory (two games that share it), every
    folder just backed up is linked to its record (``link_root``, 6.6 rule 6), so later scans
    tell the games apart without guessing. A folder the user linked already is left alone."""
    catalog = repo.load_catalog()
    users: dict[str, int] = {}
    for record in catalog["games"]:
        name = (record.get("identity") or {}).get("renpy_save_directory")
        if record.get("kind", "game") == "game" and isinstance(name, str) and name:
            users[name.casefold()] = users.get(name.casefold(), 0) + 1
    linked = {
        path_key(o["game_root"])
        for o in catalog["overrides"]
        if o.get("type") == "link_root" and o.get("game_root")
    }
    now = iso_utc(utc_now())
    written = []
    for planned, outcome in zip(plan.games, result.results, strict=False):  # D58: maybe fewer
        game = planned.game
        if game is None or outcome.status == "failed" or not outcome.game_id:
            continue
        name = game.identity.renpy_save_directory
        if not name or users.get(name.casefold(), 0) < 2:
            continue
        for root in sorted(game_roots(game)):
            if root in linked:
                continue
            where = next(
                (i["game_root"] for i in game.installs if path_key(i["game_root"]) == root),
                game.game_root,
            )
            override = {
                "game_id": outcome.game_id,
                "type": "link_root",
                "game_root": str(where),
                "linked_at": now,
            }
            catalog["overrides"].append(override)
            linked.add(root)
            written.append(override)
    if written:
        repo.save_catalog(catalog)
    return written


def prepare_repository(
    dest: Path,
    roots: Sequence[Path],
    *,
    saves_root_override: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Repo:
    """Open the library at ``dest``, creating it when absent or empty (section 8 checks first).

    ``dest`` may not overlap a scan root or the Ren'Py saves root, and a non-empty folder that
    is not a library is refused (UsageError).
    """
    env = dict(os.environ) if env is None else env
    dest = Path(os.path.abspath(dest))
    saves_root, _ = default_saves_root(env, saves_root_override)
    check_destination(dest, [Path(os.path.abspath(r)) for r in roots], saves_root)
    return Repo.open_or_create(dest)


def prepare_backup(
    roots: Sequence[Path],
    dest: Path,
    *,
    saves_root_override: Path | None = None,
    env: Mapping[str, str] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    excludes: Sequence[str] = (),
    detectors: list[Detector] | None = None,
    cache: ScanCache | None = None,
    should_stop: StopCheck = None,
    scan_progress: Callable[[int, str], None] | None = None,
    scan_unlinked: bool = True,
    **plan_options: Any,
) -> BackupPlan:
    """Safety checks, scan and plan. Nothing is written to the library or the scanned folders;
    a ``cache`` given (D52 3) is rewritten after the scan, like after any scan. A scan told to
    stop (D58) raises :class:`Stopped`: there is no plan.

    ``detectors`` defaults to the registry; backup itself never looks at engines, only at
    the ``SaveSource`` list each detected game carries.
    """
    env = dict(os.environ) if env is None else env
    roots = [Path(os.path.abspath(r)) for r in roots]
    dest = Path(os.path.abspath(dest))
    saves_root, _ = default_saves_root(env, saves_root_override)
    check_destination(dest, roots, saves_root)
    state = destination_state(dest)
    existing = state == "existing"
    report = run_scan(
        ScanOptions(
            roots=list(roots),
            max_depth=max_depth,
            excludes=list(excludes),
            saves_root_override=saves_root_override,
            env=env,
            repo=dest if existing else None,
            detectors=detectors,
            cache=cache,
            should_stop=should_stop,
            progress=scan_progress,  # 1.2.0 (D73 8)
            # D73 1: one game's folders only: the orphaned save folders need not be listed
            include_unlinked=scan_unlinked or plan_options.get("include_unlinked") is not None,
        )
    )
    known: list[dict] = []
    overrides: list[dict] = []
    if existing:
        repo = Repo.open(dest)
        catalog = repo.load_catalog()
        known, overrides = repo.known_games(catalog), catalog["overrides"]
    return plan_backup(
        report, dest, repo_state=state, known=known, overrides=overrides, **plan_options
    )
