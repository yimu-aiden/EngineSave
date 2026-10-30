"""Read RPA-3.0/2.0 archive indexes and small members without loading whole archives."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from enginesave.engines.renpy.rpyc_reader import decompress_limited, restricted_loads
from enginesave.fsutil import long_path

MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
HEADER_LIMIT = 256


class UnsupportedRpa(Exception):
    """Unsupported or malformed archive (RPA-1.0, unknown header, unreadable index...)."""


@dataclass(frozen=True)
class RpaEntry:
    name: str  # POSIX-style member name, e.g. "options.rpy" or "tl/RU/options.rpy"
    offset: int
    length: int
    prefix: bytes


RPA_MAGICS = (b"RPA-3.0 ", b"RPA-2.0 ")


def has_rpa_magic(path: Path) -> bool:
    """D53: the first 8 bytes say RPA-3.0 or RPA-2.0, whatever the file is called."""
    try:
        with open(long_path(path), "rb") as f:
            return f.read(8) in RPA_MAGICS
    except OSError:
        return False


def _parse_header(header: bytes) -> tuple[int, int]:
    """Return ``(index_offset, key)``; ``key`` is 0 for RPA-2.0."""
    parts = header.split()
    tag = parts[0] if parts else b""
    label = tag[:16].decode("ascii", "replace")
    try:
        if tag == b"RPA-3.0" and len(parts) >= 3:
            return int(parts[1], 16), int(parts[2], 16)
        if tag == b"RPA-2.0" and len(parts) >= 2:
            return int(parts[1], 16), 0
    except ValueError as exc:
        raise UnsupportedRpa(f"malformed {label} header") from exc
    raise UnsupportedRpa(f"unsupported archive format {label!r}")


def _entry(raw_name: Any, parts: Any, key: int) -> RpaEntry | None:
    name = raw_name.decode("utf-8", "replace") if isinstance(raw_name, bytes) else raw_name
    if not isinstance(name, str) or not isinstance(parts, list) or not parts:
        return None
    first = parts[0]
    if not isinstance(first, tuple | list) or len(first) not in (2, 3):
        return None
    offset, length = first[0], first[1]
    prefix = first[2] if len(first) == 3 else b""
    if isinstance(prefix, str):
        prefix = prefix.encode("latin-1", "replace")
    if not (isinstance(offset, int) and isinstance(length, int) and isinstance(prefix, bytes)):
        return None
    return RpaEntry(name.replace("\\", "/"), offset ^ key, length ^ key, prefix)


def read_index(path: Path) -> list[RpaEntry]:
    """Parse the header and index; only the index bytes at the end of the file are read."""
    with open(long_path(path), "rb") as f:
        header = f.readline(HEADER_LIMIT)
        offset, key = _parse_header(header)
        size = os.fstat(f.fileno()).st_size
        if not len(header) <= offset <= size:
            raise UnsupportedRpa("index offset out of range")
        if size - offset > MAX_INDEX_BYTES:
            raise UnsupportedRpa("index too large")
        f.seek(offset)
        blob = f.read()
    try:
        index = restricted_loads(decompress_limited(blob))
    except Exception as exc:  # malformed or refused pickle, bad zlib
        raise UnsupportedRpa(f"unreadable index: {type(exc).__name__}: {exc}") from exc
    if not isinstance(index, dict):
        raise UnsupportedRpa("index is not a mapping")
    entries = [e for e in (_entry(n, p, key) for n, p in index.items()) if e is not None]
    return sorted(entries, key=lambda e: e.name)


def read_member(
    path: Path,
    entry: RpaEntry,
    max_bytes: int = MAX_MEMBER_BYTES,
    *,
    stream: BinaryIO | None = None,
) -> bytes:
    """Return one member's bytes (``prefix + body``), refusing members above ``max_bytes``.

    ``stream`` is the archive already open for reading (several members, one ``open``: the
    scanner reads hundreds of members from one archive, D38); otherwise ``path`` is opened.
    """
    body = entry.length - len(entry.prefix)
    if entry.length > max_bytes:
        raise UnsupportedRpa(f"{entry.name}: member larger than {max_bytes} bytes")
    if entry.offset < 0 or body < 0:
        raise UnsupportedRpa(f"{entry.name}: invalid member bounds")
    if stream is None:
        with open(long_path(path), "rb") as f:
            return _read_body(f, entry, body)
    return _read_body(stream, entry, body)


def _read_body(f: BinaryIO, entry: RpaEntry, body: int) -> bytes:
    if entry.offset + body > os.fstat(f.fileno()).st_size:
        raise UnsupportedRpa(f"{entry.name}: member out of range")
    f.seek(entry.offset)
    return entry.prefix + f.read(body)
