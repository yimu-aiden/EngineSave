"""Detector protocol shared by all engines, plus the engine-agnostic game status rules (6.4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enginesave.models import (
    DetectedGame,
    GameStatus,
    MetadataState,
    SourceStatus,
)

if TYPE_CHECKING:
    from enginesave.scan_cache import ScanCache


@dataclass
class ScanContext:
    env: Mapping[str, str]
    saves_root_override: Path | None = None
    cache: ScanCache | None = None  # 0.9.0 (D52 3): the parse cache, when the caller gives one
    # 0.10.0: what one detector worked out about the folder for the next (engines.common)
    memo: dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectionResult:
    """What a detector concluded about one directory. Either field may be set.

    Any non-``None`` result stops discovery from descending into that directory.
    """

    game: DetectedGame | None = None
    skipped_reason: str | None = None  # reported under ``skipped`` (e.g. ``renpy_sdk``)


class Detector:
    """Engine detector protocol.

    ``detect`` is asked about every visited directory and returns ``None`` when the directory
    is not a root of its engine. ``finalize`` receives every detected game (all engines) once
    discovery is complete and may merge or annotate games of its own engine.

    Backup and restore copy what a detector returned without engine code of their own (a
    restore scans the game folder it is given to recognise the game: detectors again, never
    an engine's rules for the copying). A new engine therefore only has to produce a
    ``DetectedGame`` whose:

    * ``sources`` carry ``source_id`` (safe folder name), ``kind``, absolute ``path``,
      ``root_token`` + ``relative_root`` (``GAME_ROOT`` → relative to the game root,
      ``ABSOLUTE`` → the absolute path itself, ``RENPY_SAVES_ROOT`` → one folder name below
      the Ren'Py saves root) and a ``status`` (only CONFIRMED/USER_CONFIRMED are backed up
      by default);
    * ``installs`` lists ``{"game_root", "config_version", "engine_version"}`` per install;
    * ``status`` comes from :func:`game_status`.

    Register it in ``enginesave.engines.default_detectors`` (or pass ``detectors=`` to
    ``run_scan``/``prepare_backup``); nothing in ``backup.py``/``restore.py`` changes for it. Since
    0.10.0 a source may also be relative to a folder of the user profile (``enginesave.roots``)
    and take only some files of its folder (``patterns``, ``excludes``); the rule engines are
    built on ``enginesave.engines.common``.
    """

    engine = "unknown"

    def detect(self, directory: Path, ctx: ScanContext) -> DetectionResult | None:
        raise NotImplementedError

    def finalize(self, games: list[DetectedGame], ctx: ScanContext) -> list[DetectedGame]:
        return games


def game_status(game: DetectedGame) -> GameStatus:
    """Game-level status, first matching rule wins (plan 6.4).

    ``UNSUPPORTED`` and ``CONFLICT`` are sticky once assigned (cross-game conflicts are only
    known to ``finalize``).
    """
    if game.status in (GameStatus.UNSUPPORTED, GameStatus.CONFLICT):
        return game.status
    if game.metadata_state is MetadataState.CONFLICT:
        return GameStatus.CONFLICT
    statuses = {source.status for source in game.sources}
    # 0.8.0 (D48 3): once the user confirmed every candidate folder, metadata that could not
    # be read or is set by code no longer keeps the game a candidate
    settled = SourceStatus.USER_CONFIRMED in statuses and SourceStatus.CANDIDATE not in statuses
    unsure = game.metadata_state in (MetadataState.DYNAMIC, MetadataState.MISSING)
    if SourceStatus.CANDIDATE in statuses or (unsure and not settled):
        return GameStatus.CANDIDATE
    if statuses & {SourceStatus.CONFIRMED, SourceStatus.USER_CONFIRMED}:
        return GameStatus.CONFIRMED
    return GameStatus.NOT_FOUND
