"""Deleting backups (0.8.0, D49): whole records, or some of their snapshots, and then the
objects no snapshot uses any more. ``enginesave remove`` and the library page's 「删除…」
both plan and run it through here.

Nothing goes to the recycle bin: a snapshot's files are hard links into the object store, so
recycling them would free nothing, and restoring them later could meet objects already
collected. A removal therefore deletes, and says so before. The order keeps an interruption
harmless: the catalog first, then the snapshot folders, then the record's folder, and only
then the object store, which is left alone when any manifest cannot be read (an interrupted
removal leaves a record folder the catalog no longer names; ``list`` and ``verify`` report it).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enginesave import layout
from enginesave.errors import EngineSaveError, FatalError, StopCheck, UsageError
from enginesave.fsutil import long_path
from enginesave.repo import (
    GAME_FILE,
    GAME_ID_RE,
    GAMES_DIR,
    LINKED_FORMATS,
    MANIFEST,
    SNAPSHOTS_DIR,
    Garbage,
    Repo,
    install_present,
    read_manifest,
)


@dataclass(frozen=True)
class SnapshotSize:
    """What one snapshot holds, for the estimate of what a removal frees."""

    files: int
    bytes: int
    objects: Mapping[str, int]  # SHA-256 -> size of the files stored as links to objects
    copies: int  # bytes stored as plain copies (format 1, or storage "copy")
    manifest: int  # bytes of manifest.json


@dataclass
class Inventory:
    """Every snapshot on disk, read once: ``(game_id, snapshot_id) -> SnapshotSize``."""

    snapshots: dict[tuple[str, str], SnapshotSize] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)  # "<game>/<snapshot>"
    stale: int = 0  # temporary folders of an interrupted backup

    @property
    def blocked(self) -> str | None:
        """Why the object store would not be collected (D49 2), or ``None``."""
        if self.stale:
            return f"{self.stale} temporary folder(s) of an interrupted backup"
        if self.unreadable:
            return f"{len(self.unreadable)} snapshot manifest(s) could not be read"
        return None

    def freed(self, removing: Iterable[tuple[str, str]]) -> int:
        """About how many bytes removing these snapshots frees: the objects only they use,
        their plain copies and their manifests (D49 1)."""
        doomed = {key for key in removing if key in self.snapshots}
        kept: set[str] = set()
        for key, size in self.snapshots.items():
            if key not in doomed:
                kept.update(size.objects)
        objects: dict[str, int] = {}
        total = 0
        for key in doomed:
            size = self.snapshots[key]
            total += size.copies + size.manifest
            objects.update((d, n) for d, n in size.objects.items() if d not in kept)
        return total + (0 if self.blocked else sum(objects.values()))


def inventory(repo: Repo) -> Inventory:
    """Read every manifest on disk (also in record folders the catalog does not name)."""
    found = Inventory(stale=len(repo.stale_tmp_dirs()))
    for owner, game in repo.record_folders():
        key = owner or game.name  # a folder of no record (a leftover) by its name, as in 0.10.0
        for snapshot_id in repo.folder_snapshot_ids(game):
            path = game / SNAPSHOTS_DIR / snapshot_id / MANIFEST
            try:
                found.snapshots[(key, snapshot_id)] = _size(read_manifest(path), path)
            except (FatalError, OSError, KeyError, TypeError, ValueError):
                found.unreadable.append(f"{game.name}/{snapshot_id}")
    return found


def _size(manifest: Mapping[str, Any], path: Path) -> SnapshotSize:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise FatalError(f"{MANIFEST} has no file list: {path}")
    linked = manifest.get("format_version") in LINKED_FORMATS and manifest.get("storage") != "copy"
    objects: dict[str, int] = {}
    copies = 0
    for entry in files:
        size = int(entry["size"])
        if linked:
            objects[str(entry["sha256"])] = size
        else:
            copies += size
    total = sum(int(f["size"]) for f in files)
    return SnapshotSize(len(files), total, objects, copies, path.stat().st_size)


# ------------------------------------------------------------------------ overview (D56)


@dataclass
class LibraryStats:
    """The library page's overview line and ``list --json`` ``stats`` (0.9.0, D56)."""

    records: int = 0
    installed: int = 0  # game records whose game is on disk (本体存在)
    gone: int = 0  # the other records (本体不在), the orphaned-folder ones included
    unlinked: int = 0  # orphaned-folder records
    snapshots: int = 0
    unreadable: int = 0  # snapshots whose manifest could not be read: counted, no bytes
    stored_bytes: int = 0  # the objects, plain copies, manifests and record files
    logical_bytes: int = 0  # every snapshot's files as if each were stored on its own
    last_backup: str | None = None  # the newest snapshot's time (ISO UTC)
    last_mirror: str | None = None

    @property
    def saved_percent(self) -> int:
        if not self.logical_bytes:
            return 0
        return max(0, round(100 * (1 - self.stored_bytes / self.logical_bytes)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "snapshots": self.snapshots,
            "stored_bytes": self.stored_bytes,
            "logical_bytes": self.logical_bytes,
            "last_backup": self.last_backup,
            "last_mirror": self.last_mirror,
        }


def library_stats(
    repo: Repo, found: Inventory | None = None, last_mirror: str | None = None
) -> LibraryStats:
    """The overview (D56), from the same reading of every manifest as a removal's estimate.
    "Stored" is what the library takes on disk: each object once, the plain copies (0.1.0
    snapshots), the manifests and the record files; "logical" is what the snapshots would
    take without sharing."""
    found = inventory(repo) if found is None else found
    stats = LibraryStats(last_mirror=last_mirror, unreadable=len(found.unreadable))
    newest: str | None = None  # UTC ISO
    for record in repo.load_catalog()["games"]:
        game_id = str(record.get("game_id"))
        stats.records += 1
        if record.get("kind", "game") == "unlinked":
            stats.unlinked += 1
            stats.gone += 1
        else:
            try:
                present = install_present(repo.load_game(game_id))
            except FatalError:
                present = False
            stats.installed += present
            stats.gone += not present
        # D59 5: the newest manual or imported snapshot, as the scan page compares with
        latest = repo.latest_snapshot(game_id) if GAME_ID_RE.match(game_id) else None
        created = repo.snapshot_created(game_id, latest) if latest is not None else None
        if created is not None and (newest is None or created > newest):
            newest = created
    # D59 5: counted from the same reading as the bytes (record folders the catalog does not
    # name included); the unreadable ones are counted without bytes
    stats.snapshots = len(found.snapshots) + len(found.unreadable)
    for size in found.snapshots.values():
        stats.logical_bytes += size.bytes
        stats.stored_bytes += size.copies + size.manifest
    stats.stored_bytes += _files_bytes(path for _name, path in repo.iter_objects())
    games = repo.root / GAMES_DIR
    records = [p / GAME_FILE for p in games.iterdir()] if games.is_dir() else []
    stats.stored_bytes += _files_bytes([repo.catalog_path, *records])
    stats.last_backup = newest  # D67 2: the manifest's created_at (0.10.0: the id's time)
    return stats


def _files_bytes(paths: Iterable[Path]) -> int:
    total = 0
    for path in paths:
        with contextlib.suppress(OSError):
            total += os.stat(long_path(path)).st_size
    return total


# ---------------------------------------------------------------------------------- plan


@dataclass
class RemoveItem:
    """One record in a removal: all of its snapshots (the record goes) or some of them."""

    game_id: str
    display_name: str
    kind: str
    snapshot_ids: list[str]
    snapshot_count: int  # all of the record's snapshots
    files: int
    bytes: int
    installed: bool  # one of its game folders is on disk now (D43)
    # 1.0.0 (D72 R06): every snapshot the record had when this was previewed
    existing: list[str] = field(default_factory=list)

    @property
    def whole(self) -> bool:
        return len(self.snapshot_ids) == self.snapshot_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "display_name": self.display_name,
            "kind": self.kind,
            "whole_record": self.whole,
            "snapshot_ids": list(self.snapshot_ids),
            "snapshot_count": self.snapshot_count,
            "files": self.files,
            "bytes": self.bytes,
            "install_present": self.installed,
        }


@dataclass
class RemovePlan:
    repo: Repo
    items: list[RemoveItem]
    freed_bytes: int  # about; objects only when they will be collected
    gc_blocked: str | None  # why the object store will not be collected

    @property
    def records(self) -> int:
        return sum(1 for item in self.items if item.whole)

    @property
    def snapshots(self) -> int:
        return sum(len(item.snapshot_ids) for item in self.items)

    @property
    def gone(self) -> list[RemoveItem]:
        """Game records whose game is not on disk any more: their saves exist only here."""
        return [item for item in self.items if item.kind == "game" and not item.installed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": str(self.repo.root),
            "items": [item.to_dict() for item in self.items],
            "records": self.records,
            "snapshots": self.snapshots,
            "freed_bytes": self.freed_bytes,
            "gc_blocked": self.gc_blocked,
            "games_not_installed": len(self.gone),
        }


def plan_remove(
    repo: Repo,
    catalog: dict[str, Any],
    requests: Sequence[tuple[str, Sequence[str] | None]],
    found: Inventory | None = None,
) -> RemovePlan:
    """What removing ``(game reference, snapshot ids or None for all)`` would do. Nothing is
    written. Unknown records or snapshots are refused (UsageError)."""
    found = inventory(repo) if found is None else found
    items: dict[str, RemoveItem] = {}
    for ref, wanted in requests:
        record = repo.resolve_game(catalog, ref)
        game_id = record["game_id"]
        existing = repo.snapshot_ids(game_id)
        chosen = list(existing) if wanted is None else list(dict.fromkeys(wanted))
        unknown = [s for s in chosen if s not in existing]
        if unknown:
            name = record.get("display_name") or game_id
            raise UsageError(f"no snapshot {', '.join(unknown)} for {name}")
        if not chosen and existing:
            continue
        item = items.get(game_id)
        if item is not None:
            chosen = [s for s in existing if s in {*item.snapshot_ids, *chosen}]
        try:
            installed = install_present(repo.load_game(game_id))
        except FatalError:
            installed = False
        sizes = [found.snapshots.get((game_id, s)) for s in chosen]
        items[game_id] = RemoveItem(
            game_id,
            str(record.get("display_name") or game_id),
            str(record.get("kind", "game")),
            [s for s in existing if s in set(chosen)],
            len(existing),
            sum(size.files for size in sizes if size is not None),
            sum(size.bytes for size in sizes if size is not None),
            installed,
            list(existing),
        )
    doomed = [(item.game_id, s) for item in items.values() for s in item.snapshot_ids]
    return RemovePlan(repo, list(items.values()), found.freed(doomed), found.blocked)


# ------------------------------------------------------------------------------- execute


@dataclass
class RemoveResult:
    records: int = 0
    snapshots: int = 0
    freed_bytes: int = 0  # the snapshots' copies and manifests, and the objects collected
    garbage: Garbage = field(default_factory=Garbage)
    leftovers: list[str] = field(default_factory=list)  # what could not be deleted
    cancelled: bool = False  # D58: stopped between snapshots; the object store not collected

    @property
    def exit_code(self) -> int:
        return 1 if self.garbage.skipped or self.garbage.failed or self.leftovers else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "snapshots": self.snapshots,
            "freed_bytes": self.freed_bytes,
            "objects_removed": self.garbage.removed,
            "objects_kept_linked": self.garbage.kept_linked,
            "gc_skipped": self.garbage.skipped,
            "leftovers": list(self.leftovers),
        } | ({"cancelled": True} if self.cancelled else {})  # 0.9.0: only when stopped


def _unchanged(repo: Repo, item: RemoveItem) -> bool:
    """The record still has exactly the snapshots it had when the removal was previewed
    (D72 R06: a whole-record removal planned for snapshot A must not take B made since)."""
    try:
        return repo.snapshot_ids(item.game_id) == item.existing
    except EngineSaveError:
        return False


def execute_remove(
    plan: RemovePlan, found: Inventory | None = None, should_stop: StopCheck = None
) -> RemoveResult:
    """Delete what ``plan`` lists, record by record, then collect the object store.

    ``should_stop`` (D58) is asked before each record and, when only some snapshots of a
    record go, before each of them (each one leaves the catalog right): what is deleted stays
    deleted, nothing more is, and the object store is not collected.
    """
    repo = plan.repo
    changed = [item.display_name for item in plan.items if not _unchanged(repo, item)]
    if changed:  # D72 R06: before anything is deleted
        raise UsageError(
            f"the library changed since the preview ({', '.join(changed[:3])} gained or lost "
            "snapshots); preview the removal again -- nothing was deleted"
        )
    found = inventory(repo) if found is None else found
    result = RemoveResult()

    def freed(game_id: str, snapshot_id: str) -> int:
        size = found.snapshots.get((game_id, snapshot_id))
        return 0 if size is None else size.copies + size.manifest

    for item in plan.items:
        if should_stop is not None and should_stop():
            result.cancelled = True
            break
        if not _unchanged(repo, item):  # a backup meanwhile: this record is left alone
            result.leftovers.append(
                f"{item.display_name}: changed while the removal ran (a new snapshot?); "
                "not deleted -- preview it again"
            )
            continue
        if item.whole:
            result.freed_bytes += sum(freed(item.game_id, s) for s in item.snapshot_ids)
            result.leftovers += repo.remove_game(item.game_id)
            result.records += 1
            result.snapshots += len(item.snapshot_ids)
            continue
        for snapshot_id in item.snapshot_ids:
            if should_stop is not None and should_stop():
                result.cancelled = True
                break
            result.freed_bytes += freed(item.game_id, snapshot_id)
            result.leftovers += repo.remove_snapshots(item.game_id, [snapshot_id])
            result.snapshots += 1
        if result.cancelled:
            break
    layout.write_index(repo)  # D67 5
    if result.cancelled:
        result.garbage = Garbage(skipped="stopped before collecting the object store")
        return result
    result.garbage = repo.collect_garbage()
    result.freed_bytes += result.garbage.freed_bytes
    return result
