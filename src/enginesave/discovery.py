"""Walk the scan roots and ask every detector about each directory (plan 6.1)."""

from __future__ import annotations

import fnmatch
import heapq
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from enginesave.engines.base import Detector, ScanContext
from enginesave.engines.common import legacy_decoding, noting_decoding
from enginesave.errors import StopCheck, stop_point
from enginesave.fsutil import describe_error, is_reparse_point, long_path, path_key
from enginesave.models import DetectedGame

log = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 6
SYSTEM_DIRS = frozenset({"$recycle.bin", "system volume information"})
SLOWEST_KEPT = 10


@dataclass
class DiscoveryResult:
    games: list[DetectedGame] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    root_seconds: list[tuple[str, float]] = field(default_factory=list)  # (root, seconds)
    # The folders that took longest on their own (detection and listing, not their children),
    # slowest first: what to look at when a scan takes minutes (D34).
    slowest: list[tuple[float, str]] = field(default_factory=list)


def is_backup_repo(directory: Path) -> bool:
    """An EngineSave backup repository (``catalog.json`` + ``games/``) is never scanned."""
    return (directory / "catalog.json").is_file() and (directory / "games").is_dir()


def is_excluded(path: Path, patterns: Sequence[str]) -> bool:
    """Case-insensitive glob match against the folder name or its full path; a pattern that
    is the folder's own path matches it as it is (0.10.0, D63: a folder chosen in the window
    may have ``[`` in its name, which a glob would read as a set). A full path without ``*``
    or ``?`` is that folder only: its brackets are part of the name, not a set."""
    name = path.name.lower()
    full = str(path).replace("\\", "/").rstrip("/").lower()
    for pattern in patterns:
        pat = pattern.replace("\\", "/").lower()
        if full == pat.rstrip("/"):
            return True
        if PureWindowsPath(pattern).is_absolute() and not any(c in pat for c in "*?"):
            continue
        if fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(full, pat):
            return True
    return False


def _note_legacy_key(
    directory: Path, detectors: Sequence[Detector], ctx: ScanContext, game: DetectedGame
) -> None:
    """A game whose files 0.10.0 read otherwise (M44 review): the key it gave the game then,
    so that a record of 0.10.0 still matches (``legacy_key``)."""
    try:
        with legacy_decoding():
            old = next((d for d in (det.detect(directory, ctx) for det in detectors) if d), None)
    except OSError:
        return
    key = old.game.identity.engine_key if old is not None and old.game is not None else None
    if key and old is not None and old.game is not None and old.game.engine == game.engine:
        if key != game.identity.engine_key:
            game.identity.legacy_key = key


def discover(
    roots: Sequence[Path],
    detectors: Sequence[Detector],
    ctx: ScanContext,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    excludes: Sequence[str] = (),
    should_stop: StopCheck = None,
    progress: Callable[[int, str], None] | None = None,
) -> DiscoveryResult:
    """Depth-first walk; a directory recognised by a detector is not descended into.
    ``should_stop`` is asked before each folder (D58) and raises :class:`Stopped`;
    ``progress(folders so far, folder)`` is told before each folder is looked at (D73 8)."""
    result = DiscoveryResult()
    visited: set[str] = set()
    slow: list[tuple[float, str]] = []  # a min-heap of the slowest folders

    def skip(path: Path, reason: str) -> None:
        result.skipped.append({"path": str(path), "reason": reason})

    def visit(directory: Path, depth: int) -> list[Path]:
        """Detect or list one folder; the children still to walk."""
        if is_backup_repo(directory):
            skip(directory, "backup_repo")
            return []
        try:
            with noting_decoding() as differed:
                detection = next(
                    (d for d in (det.detect(directory, ctx) for det in detectors) if d), None
                )
            if differed and detection is not None and detection.game is not None:
                _note_legacy_key(directory, detectors, ctx, detection.game)
        except PermissionError:
            skip(directory, "permission_denied")
            return []
        if detection is not None:
            log.debug("detected %s: %s", directory, detection)
            if detection.skipped_reason:
                skip(directory, detection.skipped_reason)
            if detection.game is not None:
                result.games.append(detection.game)
                # 1.2.0 (D73 6): the game folder's listing, for its launch candidates later
                memo = ctx.memo.get("folder")
                if memo is not None and memo[0] == str(directory) and memo[1] is not None:
                    ctx.memo.setdefault("listings", {})[path_key(directory)] = memo[1]
            return []
        try:
            with os.scandir(long_path(directory)) as it:
                entries = sorted(it, key=lambda e: e.name.casefold())
        except PermissionError:
            skip(directory, "permission_denied")
            return []
        except OSError as exc:
            result.warnings.append(f"cannot list {directory}: {describe_error(exc)}")
            return []
        children: list[Path] = []
        for entry in entries:
            child = directory / entry.name
            try:
                if is_reparse_point(entry):
                    if entry.is_dir():
                        skip(child, "symlink")
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name.lower() in SYSTEM_DIRS:
                continue
            if is_excluded(child, excludes):
                skip(child, "excluded")
                continue
            children.append(child)
        if children and depth >= max_depth:
            skip(directory, "depth_limit")
            return []
        return children

    for root in roots:
        if not root.is_dir():
            result.warnings.append(f"scan root is not a directory: {root}")
            continue
        started = time.perf_counter()
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            stop_point(should_stop)
            directory, depth = stack.pop()
            key = path_key(directory)
            if key in visited:
                continue
            visited.add(key)
            if progress is not None:
                progress(len(visited), str(directory))
            began = time.perf_counter()
            children = visit(directory, depth)
            took = time.perf_counter() - began
            if len(slow) < SLOWEST_KEPT:
                heapq.heappush(slow, (took, str(directory)))
            elif took > slow[0][0]:
                heapq.heapreplace(slow, (took, str(directory)))
            stack.extend((child, depth + 1) for child in reversed(children))
        result.root_seconds.append((str(root), time.perf_counter() - started))
    result.slowest = sorted(slow, reverse=True)
    result.games.sort(key=lambda g: path_key(g.game_root))
    return result
