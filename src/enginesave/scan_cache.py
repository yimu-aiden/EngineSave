"""The parse cache of the scan (0.9.0, D52 3): ``scan-cache.json`` beside ``gui.json``
(``%APPDATA%\\EngineSave``, or ``config\\`` beside a portable build).

A detector stores what a file (or a member of an archive) said under its absolute path; a
later scan finds it again by that path, the file's size and modification time and the
detector's parser version, and does not open the file. The values are plain JSON that the
detector writes and reads back itself. The cache is only an optimisation: a damaged file is
ignored and rebuilt, ``scan --no-cache`` neither reads nor writes it, and after each scan it
is rewritten atomically with the entries that scan used and the ones used in the last 30 days.

The command line and the window share the file. It is handed to the scan explicitly
(``ScanOptions.cache``); nothing derives it from the scan's environment, so a scan with the
real ``APPDATA`` in its environment (the real-sample tests) never touches the user's cache.
A cache inside a folder being scanned is not written: a scan stays read-only for what it scans.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enginesave.appdirs import config_dir
from enginesave.fsutil import atomic_write_text, is_within, path_key

log = logging.getLogger("enginesave.scan_cache")

CACHE_FILE = "scan-cache.json"
CACHE_FORMAT = 1
KEEP_SECONDS = 30 * 24 * 3600


def default_path(env: Mapping[str, str] | None = None) -> Path:
    """``<configuration folder>\\scan-cache.json``, the folder ``gui.json`` is in."""
    return config_dir(env) / CACHE_FILE


def _read(path: Path) -> str:  # the only read of the file (tests guard it)
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:  # the only write of the file (tests guard it)
    atomic_write_text(path, text)


@dataclass
class ScanCache:
    """Values by ``(path, member)``; ``hits`` and ``misses`` count this scan's lookups."""

    path: Path | None = None
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    used: set[str] = field(default_factory=set)
    hits: int = 0
    misses: int = 0

    @classmethod
    def load(cls, path: Path) -> ScanCache:
        """The cache at ``path``; empty when it is missing, damaged or of another format."""
        cache = cls(path)
        try:
            data = json.loads(_read(path))
        except FileNotFoundError:
            return cache
        except (OSError, RecursionError, ValueError) as exc:
            log.info("scan cache ignored and rebuilt (%s): %s", exc, path)
            return cache
        if (
            isinstance(data, dict)
            and data.get("format") == CACHE_FORMAT
            and isinstance(data.get("entries"), dict)
        ):
            cache.entries = {k: v for k, v in data["entries"].items() if isinstance(v, dict)}
        else:
            log.info("scan cache of another format ignored and rebuilt: %s", path)
        return cache

    @staticmethod
    def key(path: Path, member: str = "") -> str:
        return f"{path_key(path)}|{member}"

    def get(self, path: Path, size: int, mtime_ns: int, parser: str, member: str = "") -> Any:
        """The value stored for this file, or ``None`` when there is none or it changed."""
        key = self.key(path, member)
        entry = self.entries.get(key)
        if (
            entry is None
            or entry.get("size") != size
            or entry.get("mtime_ns") != mtime_ns
            or entry.get("parser") != parser
            or "value" not in entry
        ):
            self.misses += 1
            return None
        self.used.add(key)
        entry["used"] = int(time.time())
        self.hits += 1
        return entry["value"]

    def put(
        self, path: Path, size: int, mtime_ns: int, parser: str, value: Any, member: str = ""
    ) -> None:
        key = self.key(path, member)
        self.entries[key] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "parser": parser,
            "used": int(time.time()),
            "value": value,
        }
        self.used.add(key)

    def drop(self, path: Path, member: str = "") -> None:
        self.entries.pop(self.key(path, member), None)

    def save(self, keep_out: Iterable[Path] = ()) -> bool:
        """Rewrite the file atomically with this scan's entries and those used in 30 days.
        Nothing is written inside ``keep_out`` (the scanned folders); False when not written."""
        if self.path is None:
            return False
        inside = next((Path(p) for p in keep_out if is_within(self.path, Path(p))), None)
        if inside is not None:  # D60 3: said once per scan, in the log (the window's too)
            log.info(
                "scan cache not written: %s is inside the scanned folder %s", self.path, inside
            )
            return False
        cutoff = int(time.time()) - KEEP_SECONDS
        kept = {
            key: entry
            for key, entry in self.entries.items()
            if key in self.used or (isinstance(entry.get("used"), int) and entry["used"] >= cutoff)
        }
        try:  # the scan's result does not depend on the cache: it never fails a scan
            # ASCII escapes: a name with a lone surrogate (possible in pickled strings and
            # Windows file names) still gives valid UTF-8, so one entry never blocks the file
            text = json.dumps({"format": CACHE_FORMAT, "entries": kept}, separators=(",", ":"))
            _write(self.path, text)
        except (OSError, RecursionError, TypeError, ValueError) as exc:
            log.info("scan cache not written (%s): %s", exc, self.path)
            return False
        return True
