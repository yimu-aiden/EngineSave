"""Orphaned global save folders under the Ren'Py saves root (``UNLINKED``, plan 6.5)."""

from __future__ import annotations

import json
import os
import zipfile
import zlib
from collections.abc import Sequence
from pathlib import Path

from enginesave.engines.renpy.paths import is_non_game_dir
from enginesave.fsutil import (
    FileEntry,
    is_hidden,
    is_reparse_point,
    iso_from_ns,
    long_path,
    path_key,
    walk_files,
)
from enginesave.models import DetectedGame, SourceKind, UnlinkedSaveDir

MAX_JSON_MEMBER = 1024 * 1024
MAX_SAVES_PROBED = 20


def version_hint(files: Sequence[FileEntry]) -> str | None:
    """``_version`` from the ``json`` member of the newest readable ``.save`` zip, if any."""
    saves = [f for f in files if f.rel.lower().endswith(".save")]
    saves.sort(key=lambda f: (-f.mtime_ns, f.rel))
    for entry in saves[:MAX_SAVES_PROBED]:
        try:
            with zipfile.ZipFile(long_path(entry.path)) as zf:
                info = zf.getinfo("json")
                if info.file_size > MAX_JSON_MEMBER:
                    continue
                meta = json.loads(zf.read(info))
        # 1.0.0 (D72 R11): a damaged stream (zlib.error) or a member that lies about its
        # size is that save's problem: the next one is tried
        except (OSError, KeyError, ValueError, zipfile.BadZipFile, RuntimeError, zlib.error,
                EOFError, NotImplementedError):  # fmt: skip
            continue
        value = meta.get("_version") if isinstance(meta, dict) else None
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            return str(value)
    return None


def _catalog_match(name: str, catalog_games: Sequence[dict]) -> str | None:
    wanted = name.casefold()
    for preferred_kind in ("unlinked", None):
        for record in catalog_games:
            sd = (record.get("identity") or {}).get("renpy_save_directory")
            if not isinstance(sd, str) or sd.casefold() != wanted:
                continue
            if preferred_kind is None or record.get("kind") == preferred_kind:
                return record.get("game_id")
    return None


def list_unlinked(
    saves_root: Path,
    games: Sequence[DetectedGame],
    catalog_games: Sequence[dict] = (),
) -> tuple[list[UnlinkedSaveDir], list[str]]:
    """First-level folders of ``saves_root`` that no detected game claims."""
    if not saves_root.is_dir():
        return [], [f"Ren'Py saves root not found: {saves_root}"]
    claimed_names = {
        g.identity.renpy_save_directory.casefold() for g in games if g.identity.renpy_save_directory
    }
    claimed_paths = {
        path_key(s.path) for g in games for s in g.sources if s.kind is SourceKind.GLOBAL
    }
    try:
        with os.scandir(long_path(saves_root)) as it:
            entries = sorted(it, key=lambda e: e.name.casefold())
    except OSError as exc:
        return [], [f"cannot list Ren'Py saves root {saves_root}: {exc}"]
    unlinked: list[UnlinkedSaveDir] = []
    for entry in entries:
        path = saves_root / entry.name
        if not entry.is_dir(follow_symlinks=False) or is_reparse_point(entry):
            continue
        if is_hidden(entry) or is_non_game_dir(entry.name):
            continue
        if entry.name.casefold() in claimed_names or path_key(path) in claimed_paths:
            continue
        files, _ = walk_files(path)
        latest = max((f.mtime_ns for f in files), default=entry.stat().st_mtime_ns)
        unlinked.append(
            UnlinkedSaveDir(
                name=entry.name,
                path=str(path),
                file_count=len(files),
                total_bytes=sum(f.size for f in files),
                last_modified=iso_from_ns(latest),
                version_hint=version_hint(files),
                known_game_id=_catalog_match(entry.name, catalog_games),
            )
        )
    return unlinked, []
