"""The library page's 「删除…」 (0.8.0, D49): which snapshots start ticked, what the ticks
mean, the summary line under the list, the warning about records whose game is gone, the
primary button and the log line. The removal itself is ``enginesave.removal``.

A tick is ``(game_id, snapshot_id)``; a record without snapshots (written by 「确认 /
关联…」, nothing backed up) is ticked as ``(game_id, "")``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from enginesave.gui.i18n import _
from enginesave.gui.state.common import size_text

if TYPE_CHECKING:
    from enginesave.removal import Inventory, RemoveResult

REMOVE_TITLE = "删除备份"
REMOVE_INTRO = (
    "勾选要删除的快照；一条记录的快照全勾上就是删除整条记录。删除不经过回收站、无法还原："
    "快照里的文件是对象库的硬链接，放进回收站既不释放空间，以后还原时对象也可能已被回收。"
)
READING = "正在读取备份库里的全部快照清单（只读）…"
EMPTY_RECORD = ""  # the snapshot part of a tick on a record without snapshots

RemoveTick = tuple[str, str]


@dataclass(frozen=True)
class RemoveRecord:
    """One record in the dialog, with its snapshots (oldest first)."""

    game_id: str
    name: str
    kind: str
    installed: bool
    snapshots: tuple[str, ...]

    def ticks(self) -> set[RemoveTick]:
        return {(self.game_id, s) for s in self.snapshots} or {(self.game_id, EMPTY_RECORD)}


def initial_ticks(
    records: Sequence[RemoveRecord], only_snapshot: str | None = None
) -> set[RemoveTick]:
    """What starts ticked (D49 1): every snapshot of every record, or only the snapshot that
    was selected on the library page when nothing was ticked there."""
    if only_snapshot is not None and len(records) == 1:
        record = records[0]
        if only_snapshot in record.snapshots:
            return {(record.game_id, only_snapshot)}
    return {tick for record in records for tick in record.ticks()}


def whole_record(record: RemoveRecord, ticked: set[RemoveTick]) -> bool:
    return record.ticks() <= ticked


def remove_requests(
    records: Sequence[RemoveRecord], ticked: set[RemoveTick]
) -> list[tuple[str, list[str] | None]]:
    """``plan_remove`` requests: a whole record as ``None``, else its ticked snapshots."""
    out: list[tuple[str, list[str] | None]] = []
    for record in records:
        if whole_record(record, ticked):
            out.append((record.game_id, None))
            continue
        chosen = [s for s in record.snapshots if (record.game_id, s) in ticked]
        if chosen:
            out.append((record.game_id, chosen))
    return out


@dataclass(frozen=True)
class RemoveCounts:
    records: int  # removed entirely
    snapshots: int
    freed: int  # about
    gone: int  # ticked game records whose game is not on disk any more


def remove_counts(
    records: Sequence[RemoveRecord], ticked: set[RemoveTick], found: Inventory
) -> RemoveCounts:
    snapshots = {t for t in ticked if t[1] != EMPTY_RECORD}
    touched = [r for r in records if r.ticks() & ticked]
    return RemoveCounts(
        sum(1 for r in records if whole_record(r, ticked)),
        len(snapshots),
        found.freed(snapshots),
        sum(1 for r in touched if r.kind == "game" and not r.installed),
    )


def remove_summary(counts: RemoveCounts, blocked: str | None) -> str:
    """The line under the list: what goes and about how much it frees (D49 1)."""
    text = _(
        "将删除 {records} 条记录、{snapshots} 个快照，释放约 {v}",
        records=counts.records,
        snapshots=counts.snapshots,
        v=size_text(counts.freed),
    )
    if blocked:
        text += _("（对象库这次不回收：{blocked}）", blocked=blocked)
    return text + _("。")


def gone_warning(count: int) -> str | None:
    """Records whose game is not installed any more: their saves exist only here (D49 1)."""
    if not count:
        return None
    return _("其中 {count} 条记录的本体已不在，删除后这些存档再也找不回。", count=count)


def remove_primary_text(counts: RemoveCounts) -> str:
    if counts.snapshots:
        return _("永久删除（{snapshots} 个快照）", snapshots=counts.snapshots)
    return (
        _("永久删除（{records} 条记录）", records=counts.records)
        if counts.records
        else _("永久删除")
    )


def remove_done_text(result: RemoveResult) -> str:
    """After the removal, in the dialog and as the one log line."""
    text = _(
        "已删除 {records} 条记录、{snapshots} 个快照，释放 {v}",
        records=result.records,
        snapshots=result.snapshots,
        v=size_text(result.freed_bytes),
    )
    if result.garbage.skipped:
        text += _("；对象库未回收：{skipped}", skipped=result.garbage.skipped)
    if result.leftovers:
        text += _("；{n} 项没能删除（见日志）", n=len(result.leftovers))
    if result.cancelled:  # D58: the garbage note above says the store was not collected
        text = _("已取消：") + text
    return text


def record_values(
    record: RemoveRecord, found: Inventory, mark: str, kinds: Mapping[str, str]
) -> tuple[str, ...]:
    sizes = [found.snapshots.get((record.game_id, s)) for s in record.snapshots]
    files = sum(s.files for s in sizes if s is not None)
    total = sum(s.bytes for s in sizes if s is not None)
    kind = _(kinds.get(record.kind, record.kind))
    count = _("{n} 个快照", n=len(record.snapshots))
    return (mark, record.name, f"{kind} · {count}", str(files), size_text(total))


def snapshot_values(
    found: Inventory, game_id: str, snapshot_id: str, mark: str, when: str
) -> tuple[str, ...]:
    size = found.snapshots.get((game_id, snapshot_id))
    if size is None:
        return (mark, f"    {snapshot_id}", when, _("读不出"), "")
    return (mark, f"    {snapshot_id}", when, str(size.files), size_text(size.bytes))
