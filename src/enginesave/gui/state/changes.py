"""The scan page's four groups (D54): which CONFIRMED games have new saves since their latest
snapshot, compared file by file on the worker thread, and the groups' names, order and
defaults. The command line does not do this; it is how the window sorts what it shows."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enginesave import registry
from enginesave.errors import EngineSaveError, StopCheck, stop_point
from enginesave.fsutil import FileEntry, ns_from_iso, path_key, source_files
from enginesave.gui.i18n import _
from enginesave.gui.state.common import game_key, snapshot_time
from enginesave.models import (
    BACKUP_READY,
    DetectedGame,
    GameStatus,
    ScanReport,
    SourceKind,
    SourceStatus,
)
from enginesave.repo import Repo

if TYPE_CHECKING:
    from enginesave.backup import BackupPlan, BackupResult

GROUP_CHANGED = "changed"
GROUP_UNCHANGED = "unchanged"
GROUP_PENDING = "pending"
GROUP_UNSUPPORTED = "unsupported"
SCAN_GROUPS = (GROUP_CHANGED, GROUP_UNCHANGED, GROUP_PENDING, GROUP_UNSUPPORTED)  # top down
SCAN_GROUP_NAMES = {
    GROUP_CHANGED: "有新存档可备份",
    GROUP_UNCHANGED: "已备份、无新存档",
    GROUP_PENDING: "待确认",
    GROUP_UNSUPPORTED: "不支持",
}
GROUP_OPEN_BY_DEFAULT = {
    GROUP_CHANGED: True,
    GROUP_UNCHANGED: False,
    GROUP_PENDING: True,
    GROUP_UNSUPPORTED: False,
}


@dataclass(frozen=True)
class Change:
    """How a CONFIRMED game's save files differ from its latest snapshot (D54 ③)."""

    backed_up: bool  # the library holds a snapshot of it
    added: int = 0
    changed: int = 0
    removed: int = 0
    snapshot_id: str | None = None  # the snapshot compared with
    error: str | None = None  # it could not be compared: counts as new saves
    recorded: bool = True  # the record it was compared with is in the library

    @property
    def new_saves(self) -> bool:
        return (
            not self.backed_up
            or self.error is not None
            or bool(self.added or self.changed or self.removed)
        )

    def note(self) -> str:
        """``未备份过``, ``无新存档，上次备份 10-24`` or ``有新存档：新增 2、改动 1``."""
        if not self.backed_up:
            return _("未备份过")
        if self.error is not None:
            return _("无法与上次备份比对")
        if not self.new_saves:
            return _("无新存档，上次备份 {v}", v=snapshot_time(self.snapshot_id or "")[5:10])
        parts = [
            f"{label} {count}"
            for label, count in (
                (_("新增"), self.added),
                (_("改动"), self.changed),
                (_("删去"), self.removed),
            )
            if count
        ]
        return _("有新存档：") + _("、").join(parts)


def compared_statuses(include_candidates: bool) -> set[SourceStatus]:
    """The sources a backup would copy (``plan_backup``): what the comparison looks at."""
    return set(BACKUP_READY) | ({SourceStatus.CANDIDATE} if include_candidates else set())


def file_state(entries: Iterable[FileEntry]) -> dict[str, tuple[int, int | None]]:
    """``{relative path: (size, mtime in milliseconds)}``: the manifest's precision."""
    return {e.rel: (e.size, e.mtime_ns // 1_000_000) for e in entries}


def _milliseconds(stamp: object) -> int | None:
    """A manifest's ``mtime`` in milliseconds; ``None`` (always a change) when unreadable."""
    try:
        return ns_from_iso(str(stamp)) // 1_000_000
    except (OverflowError, ValueError):
        return None


def save_change(
    game: DetectedGame,
    repo: Repo | None,
    *,
    include_candidates: bool = False,
    game_id: str | None = None,
    records: Collection[str] | None = None,
) -> Change:
    """Compare ``game``'s sources with its record's latest snapshot (``game_id``, or the
    record the scan recognised; ``records`` are the ids in the library, when known): a file
    is new, changed (size or modification time) or removed. Each source is matched to the
    snapshot's source with the same folder (``origin_path``), else to the one source of the
    same kind and relative root (an anonymised import); a source the snapshot does not have
    counts every file as new. Reads the snapshot's manifest and lists the save folders
    (worker thread). Whatever cannot be read counts as new saves; nothing raises."""
    game_id = game_id or game.known_game_id
    if repo is None or not game_id:
        return Change(backed_up=False, recorded=False)
    if records is not None and game_id not in records:  # deleted since the scan (D49)
        return Change(backed_up=False, recorded=False)
    try:
        snapshot_id = repo.latest_snapshot(game_id)
        if snapshot_id is None:
            return Change(backed_up=False)
        manifest = repo.load_manifest(game_id, snapshot_id)
        stored, match = _snapshot_files(manifest)
        statuses = compared_statuses(include_candidates)
        added = changed = removed = 0
        for source in game.sources:
            if source.status not in statuses:
                continue
            if source.kind is SourceKind.REGISTRY:  # D65: the .reg export, by its content
                source_id = match(source.path, source.kind.value, source.relative_root)
                counts = _registry_change(source.path, manifest, source_id)
                added, changed, removed = (
                    added + counts[0],
                    changed + counts[1],
                    removed + counts[2],
                )
                continue
            entries, _errors = (
                source_files(Path(source.path), source.patterns, source.excludes)
                if source.exists
                else ([], [])
            )
            now = file_state(entries)
            source_id = match(source.path, source.kind.value, source.relative_root)
            before = stored.get(source_id, {}) if source_id is not None else {}
            added += sum(1 for rel in now if rel not in before)
            removed += sum(1 for rel in before if rel not in now)
            changed += sum(
                1 for rel, value in now.items() if rel in before and before[rel] != value
            )
    except (
        EngineSaveError,
        OSError,
        OverflowError,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        return Change(backed_up=True, error=str(exc) or type(exc).__name__)
    return Change(True, added, changed, removed, snapshot_id)


def _registry_change(
    path: str, manifest: Mapping[str, Any], source_id: str | None
) -> tuple[int, int, int]:
    """``(added, changed, removed)`` of a registry source: its export now against the one
    the snapshot keeps (by SHA-256; an export has no time of its own)."""
    data = registry.export_key(registry.subkey_of(path))
    kept = [
        str(item.get("sha256"))
        for item in manifest.get("files") or []
        if source_id is not None and str(item["source_id"]) == source_id
    ]
    if data is None:
        return 0, 0, len(kept)
    if not kept:
        return 1, 0, 0
    return 0, int(hashlib.sha256(data).hexdigest() not in kept), 0


def _snapshot_files(
    manifest: Mapping[str, Any],
) -> tuple[dict[str, dict[str, tuple[int, int | None]]], Callable[[str, str, str], str | None]]:
    """The snapshot's files by source and a function that finds the snapshot's source for a
    folder; raises TypeError/ValueError/... on a manifest of the wrong shape."""
    stored: dict[str, dict[str, tuple[int, int | None]]] = {}
    for item in manifest.get("files") or []:
        stored.setdefault(str(item["source_id"]), {})[str(item["path"])] = (
            int(item.get("size") or 0),
            _milliseconds(item.get("mtime")),
        )
    sources = [s for s in manifest.get("sources") or [] if isinstance(s, dict)]
    by_origin = {
        path_key(str(s["origin_path"])): str(s.get("source_id"))
        for s in sources
        if s.get("origin_path")
    }
    by_place = Counter(
        (str(s.get("kind")), str(s.get("relative_root")).casefold()) for s in sources
    )

    def match(path: str, kind: str, relative_root: str) -> str | None:
        if (found := by_origin.get(path_key(path))) is not None:
            return found
        place = (kind, relative_root.casefold())
        if by_place[place] != 1:
            return None
        return next(
            str(s.get("source_id"))
            for s in sources
            if (str(s.get("kind")), str(s.get("relative_root")).casefold()) == place
        )

    return stored, match


def save_changes(
    report: ScanReport,
    repo_path: Path | None,
    *,
    include_candidates: bool = False,
    game_ids: Mapping[str, str] | None = None,
    should_stop: StopCheck = None,
) -> dict[str, Change]:
    """``{game key: Change}`` for every CONFIRMED game of a scan (D54 ③). ``game_ids`` names
    the records of games backed up since the scan (by game key). The library is opened
    read-only; one that cannot be opened leaves every game "not backed up", and a record no
    longer in it (deleted, D49) is "not backed up" too. ``should_stop`` is asked between
    games (D61 ③) and raises :class:`Stopped`."""
    repo: Repo | None = None
    records: set[str] | None = None
    if repo_path is not None:
        try:
            repo = Repo.open(repo_path)
            records = {str(r["game_id"]) for r in repo.load_catalog()["games"]}
        except (EngineSaveError, KeyError, TypeError):
            repo = None
    ids = game_ids or {}
    changes: dict[str, Change] = {}
    for game in report.games:
        if game.status is not GameStatus.CONFIRMED:
            continue
        stop_point(should_stop)
        changes[game_key(game)] = save_change(
            game,
            repo,
            include_candidates=include_candidates,
            game_id=ids.get(game_key(game)),
            records=records,
        )
    return changes


def backed_up_ids(plan: BackupPlan, result: BackupResult) -> dict[str, str]:
    """``{game key: record}`` of the games a backup just stored a snapshot for."""
    return {
        game_key(planned.game): outcome.game_id
        for planned, outcome in zip(plan.games, result.results, strict=False)
        if planned.game is not None and outcome.game_id and outcome.snapshot_id
    }


def unchanged_keys(changes: Mapping[str, Change]) -> set[str]:
    """The games backed up before and without new saves: not ticked by default (D54 ④)."""
    return {key for key, change in changes.items() if not change.new_saves}


def newly_changed(before: Mapping[str, Change], after: Mapping[str, Change]) -> set[str]:
    """Games that had no new saves and have some now (their snapshot was deleted, or another
    library is in use): ticked again, so a one-click backup does not leave them out."""
    return unchanged_keys(before) - unchanged_keys(after)


def game_group(game: DetectedGame, changes: Mapping[str, Change]) -> str:
    """Which of the four groups a game is in (D54 ①). A CONFIRMED game not compared (yet)
    counts as having new saves."""
    if game.status is GameStatus.UNSUPPORTED:
        return GROUP_UNSUPPORTED
    if game.status is not GameStatus.CONFIRMED:
        return GROUP_PENDING  # CANDIDATE, CONFLICT, NOT_FOUND
    change = changes.get(game_key(game))
    return GROUP_CHANGED if change is None or change.new_saves else GROUP_UNCHANGED


def group_title(
    group: str,
    shown: int,
    total: int,
    query: str = "",
    *,
    folded_ticks: int = 0,
    engines: Collection[str] = (),
) -> str:
    """``有新存档可备份（3）``; ``（1/3）`` while the search box hides some. A closed group with
    ticked games says so (``（已勾选 2）``, D61 ②); the unsupported group names its engines
    (``不支持（314：Emuera 133、RPG Maker MV/MZ 108、Unity 45…）``, D61 ⑤)."""
    if query.strip():
        count = f"{shown}/{total}"
    else:
        count = str(total) + engine_counts(engines)
    title = _("{v}（{count}）", v=_(SCAN_GROUP_NAMES[group]), count=count)
    return title + (
        _("（已勾选 {folded_ticks}）", folded_ticks=folded_ticks) if folded_ticks else ""
    )


# families under one short name, so that the title fits its column
SHORT_ENGINE_LABELS = {
    "rpgmaker_mv_mz": "RPG Maker",
    "rpgmaker_xp_vx_vxace": "RPG Maker",
    **dict.fromkeys(
        ("rpgmaker_mv", "rpgmaker_mz", "rpgmaker_xp", "rpgmaker_vx", "rpgmaker_vxace"), "RPG Maker"
    ),
    "wolf_rpg": "Wolf RPG",
    "unreal": "Unreal",
}


def engine_counts(engines: Collection[str], most: int = 3) -> str:
    """``：Emuera 133、Unity 45…``: the engines of the unsupported group, the most common
    first, at most ``most`` of them; empty when there are none."""
    from enginesave.engines.labels import ENGINE_LABELS

    if not engines:
        return ""
    labels = (SHORT_ENGINE_LABELS.get(e) or ENGINE_LABELS.get(e, e) for e in engines)
    counts = Counter(labels).most_common()
    text = _("、").join(f"{label} {n}" for label, n in counts[:most])
    return _("：") + text + ("…" if len(counts) > most else "")


def groups_log(games: Collection[DetectedGame], changes: Mapping[str, Change]) -> str:
    """One log line after a scan: ``分组：有新存档可备份 3；已备份、无新存档 5；…``."""
    counts = Counter(game_group(game, changes) for game in games)
    return _("分组：") + _("；").join(f"{_(SCAN_GROUP_NAMES[g])} {counts[g]}" for g in SCAN_GROUPS)
