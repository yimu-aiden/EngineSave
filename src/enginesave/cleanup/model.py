"""What a cleanup is made of: the kinds of folders, the two methods, the recycle bin of a
drive (D35), the plan and the result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from enginesave.repo import Repo

GAME_ROOT = "game_root"
GLOBAL = "global"
UNLINKED = "unlinked"
KINDS = (GAME_ROOT, GLOBAL, UNLINKED)
RECYCLE = "recycle"
PERMANENT = "permanent"
DEFAULT_RECYCLE_LIMIT = 4 * 1024**3  # when the size of the drive's recycle bin cannot be read
RECYCLE_MARGIN_PERCENT = 5  # kept free in the recycle bin (D35)
DELETING_SUFFIX = ".enginesave-deleting-"
CLEANED_RESULTS = ("recycled", "deleted")  # a cleanup entry that removed the folder
GONE = "gone"  # an execution result: the folder was not there any more (D41)
SKIPPED_FULL_BIN = "skipped_full_bin"  # not recycled: no room left in that drive's bin (D41)
MAX_LISTED = 20
GAME_NOT_IN_BACKUP = (
    "the game itself (program and assets) is not in the backup; you need its installer to "
    "play it again"
)
RECYCLE_HINT = "use --permanent to delete it permanently instead"
ENLARGE_HINT = (
    "enlarge the recycle bin (right-click it > Properties > Custom size) and check again, or "
    + RECYCLE_HINT
)


# ------------------------------------------------------------------------------- data model


@dataclass
class CleanupTarget:
    kind: str
    path: str
    files: int = 0
    bytes: int = 0
    ok: bool = False
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    recycle_refused: bool = False  # the recycle bin cannot take it (off, too small; D35)
    # D41 (0.7.0): a folder that is not there is not refused. ``already_cleaned`` says how and
    # when a cleanup of this record removed it; ``gone`` means no cleanup is recorded.
    already_cleaned: dict[str, Any] | None = None
    gone: bool = False
    # D41: bigger than the room left in the recycle bin. Not refused: room is made first (or
    # the folder is left for later) when the cleanup runs.
    needs_room: bool = False

    @property
    def active(self) -> bool:
        """The folder is there to be checked (not already cleaned up, not gone)."""
        return self.already_cleaned is None and not self.gone

    def refuse(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def gigabytes(size: int) -> str:
    return f"{size / 1024**3:.1f} GB"


@dataclass
class RecycleBin:
    """A drive's recycle bin as Windows configures it (D35); ``None`` where it cannot be read."""

    drive: str
    capacity: int | None = None  # bytes (MaxCapacity)
    used: int | None = None  # bytes now in it
    disabled: bool = False  # NukeOnDelete: Windows deletes at once on this drive

    @property
    def room(self) -> int | None:
        """What it can still take with a 5 % margin kept free; ``None`` when unknown."""
        if self.capacity is None or self.used is None:
            return None
        margin = self.capacity * RECYCLE_MARGIN_PERCENT // 100
        return max(0, self.capacity - self.used - margin)

    def describe(self) -> str:
        if self.disabled:
            return f"recycle bin of {self.drive}: turned off (Windows deletes at once)"
        if self.room is None:
            return (
                f"recycle bin of {self.drive}: size unknown; folders up to "
                f"{gigabytes(DEFAULT_RECYCLE_LIMIT)} are recycled"
            )
        return (
            f"recycle bin of {self.drive}: capacity {gigabytes(self.capacity or 0)}, used "
            f"{gigabytes(self.used or 0)}, room for {gigabytes(self.room)} "
            f"({RECYCLE_MARGIN_PERCENT}% kept free)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive": self.drive,
            "capacity": self.capacity,
            "used": self.used,
            "room": self.room,
            "disabled": self.disabled,
        }


@dataclass
class RecycleLedger:
    """What one cleanup run puts into each drive's recycle bin (D41 1).

    Each bin is read once, at the start; what the run moved in since is added up here, so the
    room for the next folder is ``capacity - used at the start - moved in - 5 %``, whatever
    Windows reports in between. Once a folder does not fit, the drive is ``full`` for the rest
    of the run: the folders after it on that drive wait too.
    """

    bins: dict[str, RecycleBin] = field(default_factory=dict)  # drive ("E:\\") -> its bin
    placed: dict[str, int] = field(default_factory=dict)  # bytes moved in by this run
    full: set[str] = field(default_factory=set)

    def room(self, drive: str) -> int | None:
        """Room left for this run on ``drive``; ``None`` when the bin's size is unknown."""
        bin_ = self.bins.get(drive)
        if bin_ is None or bin_.room is None:
            return None
        return bin_.room - self.placed.get(drive, 0)

    def fits(self, drive: str, size: int) -> bool:
        if drive in self.full:
            return False
        room = self.room(drive)
        return room is None or size <= room

    def place(self, drive: str, size: int) -> None:
        self.placed[drive] = self.placed.get(drive, 0) + size

    def forget(self, drive: str) -> None:
        """Read the drive's bin again next time (after room was made in it)."""
        self.bins.pop(drive, None)
        self.placed.pop(drive, None)
        self.full.discard(drive)


@dataclass
class CleanupPlan:
    repo: Repo
    game_id: str
    display_name: str
    record_kind: str
    snapshot_id: str | None
    snapshot_reason: str | None
    method: str
    recycle_limit: int | None  # --recycle-limit; None: the room in each drive's recycle bin
    targets: list[CleanupTarget]
    requests: list[tuple[str, str | None]]
    options: dict[str, Any]
    verified_files: int = 0
    recycle_bins: dict[str, RecycleBin] = field(default_factory=dict)  # drive → its bin
    ledger: RecycleLedger = field(default_factory=RecycleLedger)  # shared by a batch (D41)

    @property
    def active(self) -> list[CleanupTarget]:
        """The folders that are there; already cleaned and gone ones count neither way."""
        return [t for t in self.targets if t.active]

    @property
    def ok(self) -> bool:
        return bool(self.active) and all(t.ok for t in self.active)

    @property
    def passed(self) -> list[CleanupTarget]:
        return [t for t in self.targets if t.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": str(self.repo.root),
            "game_id": self.game_id,
            "display_name": self.display_name,
            "kind": self.record_kind,
            "snapshot_id": self.snapshot_id,
            "snapshot_reason": self.snapshot_reason,
            "verified_files": self.verified_files,
            "method": self.method,
            "recycle_limit": self.recycle_limit,
            "recycle_bins": [b.to_dict() for b in self.recycle_bins.values()],
            "ok": self.ok,
            "targets": [t.to_dict() for t in self.targets],
        }


@dataclass
class CleanupResult:
    at: str
    method: str
    targets: list[dict[str, Any]] = field(default_factory=list)
    recycle_bins: list[RecycleBin] = field(default_factory=list)  # read again afterwards

    full_bins: list[dict[str, Any]] = field(default_factory=list)  # per drive: what is missing
    cancelled: bool = False  # D58: stopped between folders; the targets listed are the ones done

    @property
    def freed_bytes(self) -> int:
        return sum(t["bytes"] for t in self.targets if t["result"] in ("recycled", "deleted"))

    @property
    def skipped(self) -> list[dict[str, Any]]:
        """Folders left for later because their drive's recycle bin is full (D41)."""
        return [t for t in self.targets if t["result"] == SKIPPED_FULL_BIN]

    @property
    def exit_code(self) -> int:
        """1 when a folder failed or was left for later (a full recycle bin, D41)."""
        return 1 if any(t["result"] in ("failed", SKIPPED_FULL_BIN) for t in self.targets) else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "method": self.method,
            "freed_bytes": self.freed_bytes,
            "targets": self.targets,
            "recycle_bins": [b.to_dict() for b in self.recycle_bins],
            "skipped_full_bin": [t["path"] for t in self.skipped],
            "full_bins": self.full_bins,
        } | ({"cancelled": True} if self.cancelled else {})  # 0.9.0: only when stopped
