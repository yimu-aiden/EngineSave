"""Filesystem helpers: path safety, reparse points, streaming hashes, atomic writes, timestamps."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

CHUNK_SIZE = 1024 * 1024
LONG_PATH_THRESHOLD = 240
IS_WINDOWS = os.name == "nt"

_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_INVALID_CHARS = frozenset('<>:"|?*') | frozenset(chr(i) for i in range(32))
_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_HIDDEN_FLAG = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
NAME_SURROGATE_BIT = 0x20000000
_CLOUD_TAG = 0x9000001A
_CLOUD_TAG_MASK = 0xFFFF0FFF
_CLOUD_SEEN: ContextVar[list[str] | None] = ContextVar("enginesave_cloud_seen", default=None)


class UnsafePathError(ValueError):
    """A path component or relative path that could escape its base or is not portable."""


# ------------------------------------------------------------------------------ path safety


def safe_relative_component(name: str) -> str:
    """Validate a single directory name such as ``config.save_directory``; return it unchanged.

    Rejects empty names and anything containing ``..``, ``/``, ``\\``, ``:`` or NUL.
    """
    if not name or ".." in name or any(c in name for c in "/\\:\0"):
        raise UnsafePathError(f"unsafe path component: {name!r}")
    return name


def split_relative(rel: str) -> list[str]:
    """Split a manifest-style relative path (``/`` or ``\\`` separated) into safe parts."""
    if not rel or "\0" in rel:
        raise UnsafePathError(f"empty or NUL-containing relative path: {rel!r}")
    if rel[0] in "/\\" or re.match(r"^[A-Za-z]:", rel):
        raise UnsafePathError(f"absolute or drive-qualified path: {rel!r}")
    parts = re.split(r"[\\/]", rel)
    for part in parts:
        if part in ("", ".", ".."):
            raise UnsafePathError(f"path traversal or empty component in {rel!r}")
        if any(c in _INVALID_CHARS for c in part):
            raise UnsafePathError(f"invalid character in {rel!r}")
        if part.rstrip(" .") != part:
            raise UnsafePathError(f"trailing dot or space in {rel!r}")
        if part.split(".", 1)[0].upper() in _RESERVED_NAMES:
            raise UnsafePathError(f"reserved device name in {rel!r}")
    return parts


def safe_join(base: Path, rel: str) -> Path:
    """Join ``rel`` under ``base``, refusing traversal, absolute/drive paths and device names."""
    result = base.joinpath(*split_relative(rel))
    if not is_within(result, base):
        raise UnsafePathError(f"{rel!r} escapes {base}")
    return result


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(path))


def is_within(child: Path | str, parent: Path | str) -> bool:
    """True when ``child`` is ``parent`` or lies below it (case-insensitive on Windows)."""
    child_n, parent_n = _norm(child), _norm(parent)
    try:
        return os.path.commonpath([child_n, parent_n]) == parent_n
    except ValueError:  # different drives
        return False


def same_path(a: Path | str, b: Path | str) -> bool:
    return _norm(a) == _norm(b)


def path_key(path: Path | str) -> str:
    """A normalized key for comparing paths (absolute, case-folded on Windows)."""
    return _norm(path)


def long_path(path: Path | str) -> str:
    """Absolute path string, ``\\\\?\\``-prefixed on Windows once it reaches 240 characters."""
    text = str(path)
    if text.startswith("\\\\?\\"):
        return text
    text = os.path.abspath(text)
    if not IS_WINDOWS or len(text) < LONG_PATH_THRESHOLD:
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


# ----------------------------------------------------------------------- reparse points/walk


def reparse_tag(path: Path | os.DirEntry[str]) -> int | None:
    """``None`` when ``path`` is not a reparse point, otherwise its tag (0 when unknown)."""
    try:
        if isinstance(path, os.DirEntry):
            st = path.stat(follow_symlinks=False)
        else:
            st = os.lstat(long_path(path))
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return getattr(st, "st_reparse_tag", 0) or stat.IO_REPARSE_TAG_SYMLINK
    if not getattr(st, "st_file_attributes", 0) & _REPARSE_FLAG:
        return None
    return getattr(st, "st_reparse_tag", 0)


def is_cloud_tag(tag: int) -> bool:
    """``IO_REPARSE_TAG_CLOUD`` and ``CLOUD_1``..``CLOUD_F`` (OneDrive files on demand)."""
    return tag & _CLOUD_TAG_MASK == _CLOUD_TAG


def is_reparse_point(path: Path | os.DirEntry[str]) -> bool:
    """True for name-surrogate reparse points (symlinks, junctions, ...): never followed (D20).

    Other reparse points, such as cloud placeholders, are ordinary entries; cloud placeholders
    are recorded for the scan warning. A reparse point whose tag is unknown counts as a link.
    """
    tag = reparse_tag(path)
    if tag is None:
        return False
    if tag == 0 or tag & NAME_SURROGATE_BIT:
        return True
    if is_cloud_tag(tag):
        note_cloud_placeholder(path)
    return False


def note_cloud_placeholder(path: Path | os.DirEntry[str]) -> None:
    seen = _CLOUD_SEEN.get()
    if seen is not None and len(seen) < 100:
        seen.append(path.path if isinstance(path, os.DirEntry) else str(path))


@contextlib.contextmanager
def track_cloud_placeholders() -> Iterator[list[str]]:
    """Collect the cloud placeholders met while the block runs (per thread/context)."""
    seen: list[str] = []
    token = _CLOUD_SEEN.set(seen)
    try:
        yield seen
    finally:
        _CLOUD_SEEN.reset(token)


def is_hidden(entry: os.DirEntry[str]) -> bool:
    if entry.name.startswith("."):
        return True
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & _HIDDEN_FLAG)


@dataclass(frozen=True)
class FileEntry:
    rel: str  # POSIX-style path relative to the walked root
    path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class WalkError:
    rel: str
    error: str


def lies_in(path: Path | str, root: Path | str) -> bool:
    """``path`` is ``root`` or below it: by name, once links and junctions are followed, or
    because one of its existing parents is the same folder as ``root`` (another name for it:
    a junction, a ``\\\\?\\`` path, an administrative share). 1.0.0 (D72 R01)."""
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    if is_within(path, root) or is_within(os.path.realpath(path), os.path.realpath(root)):
        return True
    if not root.exists():
        return False
    for up in (path, *path.parents):
        try:
            if up.exists() and os.path.samefile(up, root):
                return True
        except OSError:
            continue
    return False


def describe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def iter_files(
    root: Path,
    *,
    skip_top: frozenset[str] = frozenset(),
    errors: list[WalkError] | None = None,
    exclude_top: Sequence[str] = (),
) -> Iterator[FileEntry]:
    """Yield regular files below ``root`` in a stable order, never following reparse points.

    ``skip_top`` names (lower-case) first-level directories to ignore; ``exclude_top`` are
    patterns for first-level files and folders that are left out, never entered (0.10.0: a
    log, a browser cache). Problems are appended to ``errors`` when given.
    """
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            with os.scandir(long_path(directory)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            if errors is not None:
                errors.append(WalkError(prefix.rstrip("/") or ".", describe_error(exc)))
            continue
        subdirs: list[tuple[Path, str]] = []
        for entry in entries:
            rel = prefix + entry.name
            if not prefix and exclude_top and matches_patterns(entry.name, exclude_top):
                continue
            try:
                if is_reparse_point(entry):
                    if errors is not None:
                        errors.append(WalkError(rel, "skipped reparse point (symlink or junction)"))
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not prefix and entry.name.lower() in skip_top:
                        continue
                    subdirs.append((directory / entry.name, rel + "/"))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    yield FileEntry(rel, directory / entry.name, st.st_size, st.st_mtime_ns)
            except OSError as exc:
                if errors is not None:
                    errors.append(WalkError(rel, describe_error(exc)))
        stack.extend(reversed(subdirs))


def walk_files(root: Path) -> tuple[list[FileEntry], list[WalkError]]:
    """All regular files below ``root`` (sorted by relative path) plus any walk errors."""
    errors: list[WalkError] = []
    files = sorted(iter_files(root, errors=errors), key=lambda f: f.rel)
    return files, errors


def matches_patterns(name: str, patterns: Sequence[str]) -> bool:
    """A file name matches one of a source's patterns (D65 2), case-insensitively."""
    lower = name.lower()
    return any(fnmatch.fnmatchcase(lower, pattern.lower()) for pattern in patterns)


def source_files(
    root: Path, patterns: Sequence[str] = (), excludes: Sequence[str] = ()
) -> tuple[list[FileEntry], list[WalkError]]:
    """A save source's files: everything below ``root`` (:func:`walk_files`), or with
    ``patterns`` (0.10.0, D65 2) only the regular files directly in it whose names match --
    the saves an engine keeps in the game folder itself, beside the game. ``excludes`` leaves
    out what is directly in it under those names (a log, a cache folder and all below it).
    Links are never followed."""
    if not patterns:
        if not excludes:
            return walk_files(root)
        walked: list[WalkError] = []
        found = iter_files(root, errors=walked, exclude_top=excludes)  # a cache is not entered
        return sorted(found, key=lambda f: f.rel), walked
    files: list[FileEntry] = []
    errors: list[WalkError] = []
    try:
        with os.scandir(long_path(root)) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as exc:  # gone, too: reported as walk_files reports it
        return files, [WalkError(".", describe_error(exc))]
    for entry in entries:
        if not matches_patterns(entry.name, patterns) or matches_patterns(entry.name, excludes):
            continue
        try:
            if is_reparse_point(entry):
                errors.append(WalkError(entry.name, "skipped reparse point (symlink or junction)"))
            elif entry.is_file(follow_symlinks=False):
                st = entry.stat(follow_symlinks=False)
                files.append(FileEntry(entry.name, root / entry.name, st.st_size, st.st_mtime_ns))
        except OSError as exc:
            errors.append(WalkError(entry.name, describe_error(exc)))
    return files, errors


def dir_stats(
    path: Path, patterns: Sequence[str] = (), excludes: Sequence[str] = ()
) -> tuple[bool, int, int]:
    """``(exists, file_count, total_bytes)`` for a directory, counting files recursively (or
    as :func:`source_files` takes them, with ``patterns`` or ``excludes``)."""
    if not path.is_dir() or is_reparse_point(path):
        return False, 0, 0
    files, _ = source_files(path, patterns, excludes)
    return True, len(files), sum(f.size for f in files)


# ----------------------------------------------------------------------------- hashing/writes


def sha256_file(path: Path) -> tuple[str, int]:
    """Streaming SHA-256 (1 MiB chunks); returns ``(hexdigest, size)``."""
    digest = hashlib.sha256()
    size = 0
    with open(long_path(path), "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def own_temp_file(path: Path, suffix: str) -> tuple[Path, BinaryIO]:
    """A new, empty file beside ``path`` that nobody else has: a random name created with
    ``O_EXCL`` (never an existing file, hard link or link), opened for writing (1.0.0, D72 R02).
    Only the caller removes it."""
    for _attempt in range(16):
        tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}{suffix}")
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(long_path(tmp), flags, 0o666)
        except FileExistsError:
            continue
        return tmp, os.fdopen(fd, "w+b")
    raise FileExistsError(f"no free temporary name beside {path}")


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to a sibling temp file, fsync it, then ``os.replace`` it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
    try:
        with open(long_path(tmp), "w", encoding=encoding, newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(long_path(tmp), long_path(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(long_path(tmp))
        raise


# ------------------------------------------------------------------------------- timestamps


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def iso_from_ns(ns: int) -> str:
    """ISO-8601 UTC with millisecond precision, e.g. ``2026-04-05T10:12:28.889Z``. Computed,
    not asked of the OS: Windows refuses times before 1970 (a file copied from an old disc
    or an archive can carry one); years outside 1-9999 raise OverflowError."""
    seconds, rem = divmod(ns, 1_000_000_000)
    m = EPOCH + timedelta(seconds=seconds)
    return (
        f"{m.year:04d}-{m.month:02d}-{m.day:02d}T{m.hour:02d}:{m.minute:02d}:{m.second:02d}"
        f".{rem // 1_000_000:03d}Z"
    )


def ns_from_iso(text: str) -> int:
    """Inverse of :func:`iso_from_ns` (also accepts timestamps without fractions)."""
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z", text)
    if not match:
        raise ValueError(f"not an ISO-8601 UTC timestamp: {text!r}")
    moment = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    fraction = (match.group(2) or "").ljust(9, "0")
    return int(moment.timestamp()) * 1_000_000_000 + int(fraction or 0)
