"""1.3.0 (D76): a record's attachments (附件): files the user keeps with a game's saves --
guides, patches, notes -- in ``games\\<record folder>\\附件\\``.

They are plain files: no versions, no snapshot, no object store. Verification, ``--keep-auto``,
removing snapshots and cleaning up game folders never touch them. Removing a whole record keeps
them (only ``--with-attachments`` or the window's own tick deletes them); a later record that
gets the same folder name takes them over. A mirror copies them (new and changed files; it never
deletes one there). Exports and imports leave them out.

``enginesave attachments list|add|open`` and the library page's 「附件」 work through here.
Adding copies (or with ``move`` moves) each file into a temporary file of the folder first and
gives it its final name only once it is complete, so a failure leaves no partial file and,
with ``move``, the original where it was.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enginesave.errors import FatalError, UsageError
from enginesave.fsutil import describe_error, is_reparse_point, long_path, own_temp_file

ATTACHMENTS_DIR = "附件"
WALK_LIMIT = 20000  # entries looked at before ``approximate``
MAX_PATH = 259  # Windows' MAX_PATH less the terminating NUL
TMP_SUFFIX = ".enginesave-tmp"
FALLBACK_NAME = "附件"
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])$", re.IGNORECASE)


def folder_of(record_folder: Path) -> Path:
    return record_folder / ATTACHMENTS_DIR


def only_attachments(record_folder: Path) -> bool:
    """A record folder that holds nothing but ``附件`` (kept when its record was removed)."""
    try:
        names = [p.name for p in record_folder.iterdir()]
    except OSError:
        return False
    return names == [ATTACHMENTS_DIR] and (record_folder / ATTACHMENTS_DIR).is_dir()


# ----------------------------------------------------------------------------------- listing


@dataclass
class Listing:
    game_id: str
    folder: Path
    exists: bool
    entries: list[dict[str, Any]] = field(default_factory=list)
    file_count: int = 0
    total_bytes: int = 0
    approximate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "folder": str(self.folder),
            "exists": self.exists,
            "entries": list(self.entries),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "approximate": self.approximate,
        }


def _walk(folder: Path, budget: list[int]) -> tuple[int, int, bool]:
    """``(files, bytes, cut short)`` below ``folder``; links and junctions are not followed."""
    files = size = 0
    stack = [folder]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(long_path(current)) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if budget[0] <= 0:
                return files, size, True
            budget[0] -= 1
            if is_reparse_point(entry):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files += 1
                    size += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return files, size, False


def listing(game_id: str, record_folder: Path, *, limit: int = WALK_LIMIT) -> Listing:
    """What a record's attachments folder holds: the entries directly in it (a folder with
    the size of everything below it), and the files and bytes below it all. A walk of more
    than ``limit`` entries stops there and says ``approximate``."""
    folder = folder_of(record_folder)
    found = Listing(game_id, folder, folder.is_dir() and not is_reparse_point(folder))
    if not found.exists:
        return found
    budget = [limit]
    try:
        with os.scandir(long_path(folder)) as it:
            top = sorted(it, key=lambda e: e.name.casefold())
    except OSError as exc:
        raise FatalError(f"cannot read {folder}: {describe_error(exc)}") from exc
    for entry in top:
        if is_reparse_point(entry) or entry.name.endswith(TMP_SUFFIX):
            continue
        budget[0] -= 1
        try:
            if entry.is_dir(follow_symlinks=False):
                files, size, cut = _walk(Path(entry.path), budget)
                found.entries.append({"name": entry.name, "kind": "dir", "bytes": size})
                found.file_count += files
                found.approximate |= cut
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                found.entries.append({"name": entry.name, "kind": "file", "bytes": size})
                found.file_count += 1
            else:
                continue
        except OSError:
            continue
        found.total_bytes += size
        if budget[0] <= 0:
            found.approximate = True
            break
    return found


# ------------------------------------------------------------------------------------ adding


def clean_name(name: str, folder: Path, *, room: int = len(" (999)")) -> str:
    """A file name Windows takes in ``folder``: no characters it forbids, not a device name,
    no dot or space at the end, and short enough that ``folder\\name`` -- with ``room`` for a
    `` (2)`` -- stays within MAX_PATH."""
    text = _ILLEGAL.sub("_", name).strip().rstrip(". ")
    stem, dot, ext = text.rpartition(".")
    if not dot or not stem:
        stem, ext = text, ""
    if _RESERVED.match(stem.strip()):
        stem = f"_{stem}"
    ext = ext[:16]
    allowed = MAX_PATH - len(str(folder)) - 1 - room - (len(ext) + 1 if ext else 0)
    if allowed < 8:
        raise UsageError(f"the attachments folder's path is too long for more files: {folder}")
    stem = stem[:allowed].rstrip(". ") or FALLBACK_NAME
    return f"{stem}.{ext}" if ext else stem


def numbered(name: str, taken: set[str]) -> str:
    """``name``, else ``name (2).ext``, ``name (3).ext``... -- the first not in ``taken``
    (case-folded names)."""
    if name.casefold() not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    n = 2
    while True:
        candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
        if candidate.casefold() not in taken:
            return candidate
        n += 1


def _taken(folder: Path) -> set[str]:
    try:
        return {p.name.casefold() for p in folder.iterdir()}
    except OSError:
        return set()


def _place(source: Path, folder: Path, name: str, move: bool) -> int:
    """Put ``source`` into ``folder`` as ``name`` (which must not exist): complete under a
    temporary name first, then renamed; with ``move`` the source goes only after that. The
    bytes placed."""
    final = folder / name
    size = os.stat(long_path(source)).st_size
    if move:
        with contextlib.suppress(OSError):  # the same drive: one rename, nothing copied
            os.rename(long_path(source), long_path(final))
            return size
    tmp, handle = own_temp_file(final, TMP_SUFFIX)
    try:
        with handle, open(long_path(source), "rb") as reader:
            shutil.copyfileobj(reader, handle, 1024 * 1024)
            handle.flush()
            os.fsync(handle.fileno())
        if os.stat(long_path(tmp)).st_size != size:
            raise OSError(f"{source} changed while it was copied")
        shutil.copystat(long_path(source), long_path(tmp))
        os.rename(long_path(tmp), long_path(final))  # never replaces a file of that name
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(long_path(tmp))
        raise
    if move:
        try:
            os.remove(long_path(source))
        except OSError as exc:
            raise OSError(
                f"copied as {final}, but the original could not be removed: {describe_error(exc)}"
            ) from exc
    return size


@dataclass
class Added:
    folder: Path
    saved: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def to_dict(self) -> dict[str, Any]:
        return {"folder": str(self.folder), "saved": self.saved, "failed": self.failed}


def check_sources(files: Sequence[Path], folder: Path) -> list[Path]:
    """The files to add, refused (exit code 2) when one is no file or is inside ``folder``."""
    sources = [Path(os.path.abspath(f)) for f in files]
    if not sources:
        raise UsageError("name at least one file to add")
    for source in sources:
        if not source.is_file() or is_reparse_point(source):
            raise UsageError(f"not a file (folders and links are not added): {source}")
        if Path(os.path.abspath(source.parent)) == Path(os.path.abspath(folder)):
            raise UsageError(f"already in the attachments folder: {source}")
    return sources


def add(record_folder: Path, files: Sequence[Path], *, move: bool = False) -> Added:
    """Copy (or move) ``files`` into the record's attachments folder, creating it, each under
    a cleaned, numbered name; one that fails is reported and the rest go on."""
    folder = folder_of(record_folder)
    sources = check_sources(files, folder)
    if is_reparse_point(folder):
        raise FatalError(f"the attachments folder is a link or junction; not written: {folder}")
    try:
        os.makedirs(long_path(folder), exist_ok=True)
    except OSError as exc:
        raise FatalError(f"cannot create {folder}: {describe_error(exc)}") from exc
    result = Added(folder)
    for source in sources:
        name = numbered(clean_name(source.name, folder), _taken(folder))
        try:
            size = _place(source, folder, name, move)
        except OSError as exc:
            result.failed.append({"source": str(source), "error": describe_error(exc)})
            continue
        result.saved.append({"source": str(source), "name": name, "bytes": size})
    return result


def ensure_folder(record_folder: Path) -> Path:
    """The attachments folder, created when it is not there yet."""
    folder = folder_of(record_folder)
    if is_reparse_point(folder):
        raise FatalError(f"the attachments folder is a link or junction: {folder}")
    try:
        os.makedirs(long_path(folder), exist_ok=True)
    except OSError as exc:
        raise FatalError(f"cannot create {folder}: {describe_error(exc)}") from exc
    return folder


def open_in_explorer(folder: Path) -> None:
    """Show ``folder`` in Explorer (Windows)."""
    starter = getattr(os, "startfile", None)
    if starter is None:
        raise UsageError(f"opening a folder needs Windows; the folder is {folder}")
    try:
        starter(str(folder))
    except OSError as exc:
        raise FatalError(f"cannot open {folder}: {describe_error(exc)}") from exc


# ---------------------------------------------------------------------------- the mirror


def mirror_files(source: Path, target: Path) -> tuple[int, list[str]]:
    """Copy the attachments folder ``source`` into ``target``: files the mirror lacks or holds
    with another size or time; nothing there is deleted. ``(files copied, problems)``."""
    copied, problems = 0, []
    if not source.is_dir() or is_reparse_point(source):
        return 0, []
    for current, dirs, names in os.walk(long_path(source)):
        here = Path(current)
        dirs[:] = [d for d in dirs if not is_reparse_point(here / d)]
        relative = here.relative_to(Path(long_path(source)))
        for name in names:
            if name.endswith(TMP_SUFFIX):
                continue
            src = here / name
            dst = target / relative / name
            try:
                if is_reparse_point(src):
                    continue
                info = os.stat(src)
                with contextlib.suppress(OSError):
                    there = os.stat(long_path(dst))
                    if there.st_size == info.st_size and int(there.st_mtime) == int(info.st_mtime):
                        continue
                os.makedirs(long_path(dst.parent), exist_ok=True)
                tmp, handle = own_temp_file(dst, TMP_SUFFIX)
                try:
                    with handle, open(src, "rb") as reader:
                        shutil.copyfileobj(reader, handle, 1024 * 1024)
                    shutil.copystat(src, long_path(tmp))
                    os.replace(long_path(tmp), long_path(dst))
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.remove(long_path(tmp))
                    raise
                copied += 1
            except OSError as exc:
                problems.append(f"{ATTACHMENTS_DIR}\\{relative / name}: {describe_error(exc)}")
    return copied, problems
