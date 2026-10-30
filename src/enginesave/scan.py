"""Scan orchestration shared by ``scan`` and ``backup``: discover → finalize → report."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from enginesave import __version__
from enginesave.discovery import DEFAULT_MAX_DEPTH, DiscoveryResult, discover
from enginesave.engines import default_detectors
from enginesave.engines.base import Detector, ScanContext, game_status
from enginesave.engines.renpy.paths import default_saves_root
from enginesave.errors import StopCheck, Stopped
from enginesave.fsutil import (
    iso_utc,
    path_key,
    sha256_file,
    track_cloud_placeholders,
    utc_now,
    walk_files,
)
from enginesave.identity import annotate_known_games
from enginesave.models import (
    FORMAT_VERSION,
    DetectedGame,
    ScanReport,
    SourceKind,
)
from enginesave.repo import Repo
from enginesave.saves_root import list_unlinked
from enginesave.scan_cache import ScanCache

CLOUD_WARNING = "cloud placeholder files were read; reading them may trigger download"
SLOW_SCAN_SECONDS = 60  # beyond this the report names the slowest folders (D34)


@dataclass
class ScanOptions:
    roots: list[Path]
    max_depth: int = DEFAULT_MAX_DEPTH
    excludes: list[str] = field(default_factory=list)
    compute_hashes: bool = False
    include_unlinked: bool = True
    saves_root_override: Path | None = None
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    repo: Path | None = None  # backup repository used to recognise known games (read only)
    # 0.9.0 (D52 3): the parse cache; only the command line and the window pass one
    cache: ScanCache | None = None
    detectors: list[Detector] | None = None
    should_stop: StopCheck = None  # 0.9.0 (D58): asked between folders


def compare_by_hash(local_dir: Path, mirror_dir: Path) -> tuple[int, int]:
    """``(identical, compared)``: files of ``local_dir`` byte-identical at the same path."""
    files, _ = walk_files(local_dir)
    identical = 0
    for entry in files:
        twin = mirror_dir.joinpath(*entry.rel.split("/"))
        try:
            if twin.is_file() and sha256_file(twin)[0] == sha256_file(entry.path)[0]:
                identical += 1
        except OSError:
            continue
    return identical, len(files)


def annotate_mirror_hashes(games: list[DetectedGame]) -> None:
    for game in games:
        by_id = {s.source_id: s for s in game.sources}
        for source in game.sources:
            if source.kind is not SourceKind.GLOBAL:
                continue
            for other_id in source.mirror_of:
                other = by_id.get(other_id)
                if other is None or other.kind is not SourceKind.LOCAL:
                    continue
                identical, compared = compare_by_hash(Path(other.path), Path(source.path))
                source.notes.append(f"identical by hash: {identical}/{compared} ({other_id})")


def timing(found: DiscoveryResult, total: float) -> dict:
    """How long the scan took, per scan root; the slowest folders when it took minutes."""
    slow = total > SLOW_SCAN_SECONDS
    return {
        "total_seconds": round(total, 3),
        "roots": [{"root": root, "seconds": round(s, 3)} for root, s in found.root_seconds],
        "slowest": [{"path": path, "seconds": round(s, 3)} for s, path in found.slowest]
        if slow
        else [],
    }


def run_scan(options: ScanOptions) -> ScanReport:
    """Read-only scan of ``options.roots``: nothing in them is written. The parse cache, when
    one is given, is rewritten afterwards (never inside the scanned folders). Told to stop
    (D58), it raises :class:`Stopped` between folders: there is no report then, but what was
    parsed stays in the cache."""
    started = time.perf_counter()
    roots = [Path(os.path.abspath(r)) for r in options.roots]
    ctx = ScanContext(
        env=options.env, saves_root_override=options.saves_root_override, cache=options.cache
    )
    detectors = options.detectors if options.detectors is not None else default_detectors()
    with track_cloud_placeholders() as cloud_placeholders:
        try:
            found = discover(
                roots,
                detectors,
                ctx,
                max_depth=options.max_depth,
                excludes=options.excludes,
                should_stop=options.should_stop,
            )
        except Stopped:
            if options.cache is not None:
                options.cache.save(keep_out=roots)
            raise
        games = found.games
        for detector in detectors:
            games = detector.finalize(games, ctx)
        games.sort(key=lambda g: path_key(g.game_root))
        known: list[dict] = []
        if options.repo is not None:
            repo = Repo.open(options.repo)
            catalog = repo.load_catalog()
            known = repo.known_games(catalog)
            annotate_known_games(games, known, catalog["overrides"])
        for game in games:
            game.status = game_status(game)

        saves_root, _ = default_saves_root(options.env, options.saves_root_override)
        warnings = list(found.warnings)
        unlinked = []
        if options.include_unlinked:
            unlinked, root_warnings = list_unlinked(saves_root, games, known)
            warnings.extend(root_warnings)
        if options.compute_hashes:
            annotate_mirror_hashes(games)
    if cloud_placeholders:
        warnings.append(CLOUD_WARNING)
    if options.cache is not None:
        options.cache.save(keep_out=roots)
    return ScanReport(
        format_version=FORMAT_VERSION,
        tool_version=__version__,
        generated_at=iso_utc(utc_now()),
        roots=[str(r) for r in roots],
        renpy_saves_root=str(saves_root),
        games=games,
        unlinked=unlinked,
        skipped=found.skipped,
        warnings=warnings,
        timing=timing(found, time.perf_counter() - started),
    )
