"""EngineSave's own earlier items in the recycle bin, purged to make room when it is full (D41 6).

Windows keeps every recycled item as a pair in ``<drive>\\$RECYCLE.BIN\\<user SID>\\``:
``$I<id>`` holds the version, size, deletion time and original path, ``$R<id>`` is the folder
itself. :func:`ours` offers only the pairs whose original path is a folder a cleanup of a record
moved there (its ``cleanups[]`` entry with method ``recycle``), deleted within two minutes of
the time recorded for it; :func:`purge` deletes such a pair (``$R`` then ``$I``) and notes it on
that entry (``purged_from_recycle_bin``). Everything else in the bin is never touched: no
other ``$I`` is acted on, nothing is restored or moved, and the bin is never emptied
(``SHEmptyRecycleBin`` is not used). An ``$I`` that cannot be parsed is left alone.

``$I`` format, version 2 (Windows 10/11): 8 bytes version, 8 bytes size, 8 bytes FILETIME of
the deletion, 4 bytes length of the path in characters, the path in UTF-16LE. Version 1
(Windows Vista-8.1) has a fixed 520-byte path field instead of the length.
"""

from __future__ import annotations

import os
import shutil
import stat
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from enginesave.backup import update_catalog_entry
from enginesave.cleanup import windows
from enginesave.cleanup.model import RECYCLE
from enginesave.fsutil import (
    describe_error,
    is_reparse_point,
    iso_from_ns,
    iso_utc,
    long_path,
    ns_from_iso,
    path_key,
    utc_now,
)
from enginesave.repo import Repo

MATCH_SECONDS = 120  # the deletion time in $I against the time recorded for the cleanup
MAX_INFO_BYTES = 64 * 1024
FILETIME_EPOCH = 116_444_736_000_000_000  # 1601-01-01 to 1970-01-01, in 100 ns


@dataclass(frozen=True)
class RecycledItem:
    """One pair in a recycle bin folder, as its ``$I`` describes it."""

    info: Path
    data: Path
    original: str
    size: int
    deleted_ns: int  # since 1970, UTC

    @property
    def deleted_at(self) -> str:
        return iso_from_ns(self.deleted_ns)


@dataclass(frozen=True)
class OurItem:
    """A recycled item that a cleanup of ``game_id`` put there (entry ``cleanup``, target
    ``target`` of its ``cleanups[]``)."""

    item: RecycledItem
    game_id: str
    display_name: str
    cleanup: int
    target: int
    kind: str
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.item.original,
            "bytes": self.item.size,
            "recycled_at": self.item.deleted_at,
            "game_id": self.game_id,
            "display_name": self.display_name,
            "kind": self.kind,
        }


def bin_folder(drive: str) -> Path | None:
    """This user's folder in the drive's recycle bin; ``None`` when the SID is unknown.
    (A module attribute: tests point it at a folder of their own.)"""
    sid = windows.user_sid()
    return Path(drive) / "$RECYCLE.BIN" / sid if sid else None


def parse_info(data: bytes) -> tuple[str, int, int] | None:
    """``(original path, size, deletion time in ns since 1970)`` from an ``$I``; ``None`` if
    it is not one this module understands."""
    if len(data) < 24:
        return None
    version, size, filetime = struct.unpack_from("<qqq", data, 0)
    if version == 2:
        if len(data) < 28:
            return None
        (chars,) = struct.unpack_from("<i", data, 24)
        raw = data[28 : 28 + 2 * chars]
        if chars <= 0 or len(raw) != 2 * chars:
            return None
    elif version == 1:
        raw = data[24 : 24 + 520]
        if len(raw) != 520:
            return None
    else:
        return None
    try:
        original = raw.decode("utf-16-le").split("\0", 1)[0]
    except UnicodeDecodeError:
        return None
    if not original or size < 0 or filetime <= FILETIME_EPOCH:
        return None
    return original, size, (filetime - FILETIME_EPOCH) * 100


def list_items(drive: str) -> list[RecycledItem]:
    """The pairs in this user's folder of the drive's recycle bin (only ``$I`` headers read)."""
    folder = bin_folder(drive)
    if folder is None or not folder.is_dir():
        return []
    items = []
    with os.scandir(long_path(folder)) as entries:
        for entry in entries:
            if not entry.name.startswith("$I") or not entry.is_file(follow_symlinks=False):
                continue
            data_path = folder / ("$R" + entry.name[2:])
            if not os.path.lexists(long_path(data_path)):
                continue
            try:
                with open(entry.path, "rb") as fh:
                    parsed = parse_info(fh.read(MAX_INFO_BYTES))
            except OSError:
                continue
            if parsed is None:
                continue
            original, size, deleted = parsed
            items.append(RecycledItem(folder / entry.name, data_path, original, size, deleted))
    return sorted(items, key=lambda i: (i.deleted_ns, i.info.name))


def ours(repo: Repo, drives: Sequence[str]) -> list[OurItem]:
    """EngineSave's earlier recycled folders still in the bins of ``drives``, oldest first.

    A folder matches when a ``cleanups[]`` target of a record in ``repo`` recycled exactly that
    path (case-insensitive) and the ``$I`` deletion time is within ``MATCH_SECONDS`` of the
    time recorded for it; of several candidates the closest in time wins, and every recycled
    item answers for one target at most. Targets already purged are skipped.
    """
    wanted = {d.upper() for d in drives}
    available: dict[str, list[RecycledItem]] = {}
    for drive in wanted:
        for item in list_items(drive):
            available.setdefault(path_key(item.original), []).append(item)
    found: list[OurItem] = []
    taken: set[Path] = set()
    for record in repo.load_catalog()["games"]:
        doc = repo.load_game(record["game_id"])
        for ci, entry in enumerate(doc.get("cleanups") or []):
            if entry.get("method") != RECYCLE:
                continue
            for ti, target in enumerate(entry.get("targets") or []):
                if target.get("result") != "recycled" or target.get("purged_from_recycle_bin"):
                    continue
                if Path(target["path"]).anchor.upper() not in wanted:
                    continue
                at = str(target.get("at") or entry["at"])
                try:
                    recorded = ns_from_iso(at)
                except ValueError:
                    continue
                candidates = [
                    i
                    for i in available.get(path_key(target["path"]), [])
                    if i.info not in taken
                    and abs(i.deleted_ns - recorded) <= MATCH_SECONDS * 1_000_000_000
                ]
                if not candidates:
                    continue
                best = min(candidates, key=lambda i: abs(i.deleted_ns - recorded))
                taken.add(best.info)
                found.append(
                    OurItem(
                        best,
                        doc["game_id"],
                        str(doc.get("display_name") or doc["game_id"]),
                        ci,
                        ti,
                        str(target.get("kind")),
                        at,
                    )
                )
    return sorted(found, key=lambda o: (o.item.deleted_ns, o.item.info.name))


def _remove(path: Path) -> str | None:
    """Delete one ``$R`` (a folder or a file); ``None`` on success."""
    leftovers: list[str] = []

    def retry(function: Callable[..., Any], failed: str, _exc: BaseException) -> None:
        try:
            os.chmod(failed, stat.S_IWRITE)
            function(failed)
        except OSError:
            leftovers.append(failed)

    try:
        if path.is_dir() and not is_reparse_point(path):
            shutil.rmtree(long_path(path), onexc=retry)
        else:
            os.chmod(long_path(path), stat.S_IWRITE)
            os.remove(long_path(path))
    except OSError as exc:
        return describe_error(exc)
    if os.path.lexists(long_path(path)):
        return f"{len(leftovers) or 'some'} file(s) could not be deleted"
    return None


def purge(
    repo: Repo,
    chosen: Sequence[OurItem],
    *,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """Delete these items from the recycle bin and note it on their cleanup entries.

    Each ``$I`` is read again first: an item whose header no longer names the same folder, or
    whose ``$R`` is a link, is left alone (``not_found``). ``$R`` goes first, then ``$I``; if
    ``$R`` cannot be deleted completely, ``$I`` stays and the item is ``failed``.
    """
    results: list[dict[str, Any]] = []
    noted: dict[str, list[tuple[OurItem, str]]] = {}
    for index, ours_ in enumerate(chosen):
        item = ours_.item
        if progress is not None:
            progress(index, len(chosen), item.original)
        outcome = {**ours_.to_dict(), "result": "failed", "error": None}
        results.append(outcome)
        try:
            with open(long_path(item.info), "rb") as fh:
                again = parse_info(fh.read(MAX_INFO_BYTES))
        except OSError:
            again = None
        if (
            again is None
            or path_key(again[0]) != path_key(item.original)
            or again[2] != item.deleted_ns
            or not os.path.lexists(long_path(item.data))
            or is_reparse_point(item.data)
        ):
            outcome["result"] = "not_found"
            outcome["error"] = "not in the recycle bin any more (or changed); left alone"
            continue
        error = _remove(item.data)
        if error is None:
            try:
                os.remove(long_path(item.info))
            except OSError as exc:
                error = f"the folder is gone but its entry stays: {describe_error(exc)}"
        if error:
            outcome["error"] = error
            continue
        outcome["result"] = "purged"
        at = iso_utc(utc_now())
        noted.setdefault(ours_.game_id, []).append((ours_, at))
    for game_id, done in noted.items():
        doc = repo.load_game(game_id)
        for ours_, at in done:
            target = doc["cleanups"][ours_.cleanup]["targets"][ours_.target]
            target["purged_from_recycle_bin"] = {
                "at": at,
                "recycled_at": ours_.item.deleted_at,
                "bytes": ours_.item.size,
            }
        doc["updated_at"] = iso_utc(utc_now())
        repo.save_game(doc)
        update_catalog_entry(repo, repo.load_catalog(), doc)
    return results
