"""The cleanup dialog: candidates, requests, the check's verdicts, the recycle bins (D35), the
primary button (D40), and what a full recycle bin left for later (D41). Since 0.7.0 the texts
are short (D41 4) and each check and each run logs one line."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enginesave.gui.i18n import _
from enginesave.gui.state.common import local_time, size_text

if TYPE_CHECKING:
    from enginesave.backup import BackupResult
    from enginesave.cleanup import CleanupPlan, CleanupResult, CleanupTarget, RecycleBin

# ---------------------------------------------------------------------- cleanup (0.4.0)

# The folder kinds and the two methods as ``plan_cleanup`` names them (a test checks that they
# agree with ``enginesave.cleanup``), written out so that the window does not load the cleanup
# code until a cleanup dialog opens (D38).
GAME_ROOT, GLOBAL, UNLINKED = "game_root", "global", "unlinked"
RECYCLE, PERMANENT = "recycle", "permanent"
CLEANUP_LABELS = {GAME_ROOT: "游戏目录", GLOBAL: "全局存档目录", UNLINKED: "孤立存档目录"}
CLEANUP_OUTCOMES = {"recycled": "已移到回收站", "deleted": "已永久删除"}
METHOD_TEXTS = {RECYCLE: "移到回收站", PERMANENT: "永久删除（无法还原）"}
CLEANUP_INTRO = "已自动检查（只读），通过的项已勾选；删除前每一项都会再查一遍。"
NOTHING_TO_CLEAN = "没有可以清理的文件夹（游戏目录都不存在，也没有全局存档目录）。"
PERMANENT_WARNING = "永久删除无法还原：不经过回收站。游戏本体不在备份里，删掉要重新下载。"
CHECKING = "正在检查（只读：校验快照、比对文件）…"
NOTE_GAME = "本体不在备份，需重新下载"
NOTE_SAVES = "已备份并校验"
NOTE_NEEDS_ROOM = "回收站放不下，执行时先腾空间"
LEFT_FOR_LATER = "未处理：回收站已满"  # D41: not a failure
NOT_REACHED = "未处理：已取消"  # D58: the run stopped before this folder
GONE = "不存在"  # D47, D51: a folder that is not there, whether EngineSave removed it or not
RECYCLE_WAYS_OUT = "放不进回收站的项可以改选“永久删除”。"
FULL_BIN_OURS = (
    "EngineSave 之前放进回收站的项（都已备份并校验）；勾选的从回收站永久删除，腾出空间："
)
FULL_BIN_WAITING = "本次还没放进去的项；勾选的改为直接永久删除（无法还原）："


def record_label(record: Mapping[str, Any]) -> str:
    """A record in the cleanup dialog's list: name, orphan mark and its folder in the library
    (D67 7; the id for a record of a library not migrated yet)."""
    name = str(record.get("display_name") or record["game_id"])
    mark = _("  [孤立目录]") if record.get("kind") == "unlinked" else ""
    folder = record.get("folder")
    return f"{name}{mark}  ({'games' + chr(92) + str(folder) if folder else record['game_id']})"


def cleanup_candidates(result: BackupResult) -> list[str]:
    """Records the backup result offers to clean up: none unless every backup is complete."""
    if not result.results or any(r.status != "ok" for r in result.results):
        return []
    return [r.game_id for r in result.results if r.game_id]


def cleanup_requests(doc: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """Every folder a record could clean up, as ``plan_cleanup`` requests (D26): the installs
    that are there, and the cleaned-up ones so that the check lists them, 「不存在」 with when
    a cleanup removed them (D41, D51)."""
    identity = doc.get("identity") or {}
    if doc.get("kind", "game") == "unlinked":
        name = identity.get("renpy_save_directory")
        return [(UNLINKED, str(name))] if name else []
    requests: list[tuple[str, str | None]] = [
        (GAME_ROOT, str(i["game_root"]))
        for i in doc.get("installs", [])
        if i.get("game_root") and (i.get("removed_at") or Path(i["game_root"]).is_dir())
    ]
    from enginesave.cleanup import has_global_folder

    if has_global_folder(doc):
        requests.append((GLOBAL, None))
    return requests


def cleanup_verdict(target: CleanupTarget) -> str:
    """D51: a folder that is not there is 「不存在」, cleaned up by EngineSave or not (the 「说明」
    column says when a cleanup removed it); neither passed nor refused (D41)."""
    if target.already_cleaned is not None or target.gone:
        return _(GONE)
    return _("通过") if target.ok else _("拒绝")


def cleaned_note(cleaned: Mapping[str, Any]) -> str:
    """``已移到回收站 · 2026-10-24 20:58`` for a folder a cleanup removed earlier (D41)."""
    how = _("已移到回收站") if cleaned.get("method") == RECYCLE else _("已永久删除")
    text = f"{how} · {local_time(str(cleaned['at']))}"
    if cleaned.get("purged_at"):
        text += _("（已从回收站删除）")
    return text


def cleanup_note(target: CleanupTarget) -> str:
    """Why a folder is refused, or a short phrase about it (D41 4)."""
    if target.already_cleaned is not None:
        return cleaned_note(target.already_cleaned)
    if target.gone:
        return _("没有清理记录")
    if not target.ok:
        return _("；").join(target.reasons)
    note = _(NOTE_GAME) if target.kind == GAME_ROOT else _(NOTE_SAVES)
    return (
        _("{note}；{NOTE_NEEDS_ROOM}", note=note, NOTE_NEEDS_ROOM=_(NOTE_NEEDS_ROOM))
        if target.needs_room
        else note
    )


def cleanup_counts(plans: Sequence[CleanupPlan]) -> tuple[int, int, int, int]:
    """``(records, can be cleaned up, not there, refused)``; "not there" is a folder a cleanup
    removed and one that is simply gone alike (D51)."""
    targets = [t for plan in plans for t in plan.targets]
    passed = sum(1 for t in targets if t.active and t.ok)
    missing = sum(1 for t in targets if not t.active)
    refused = sum(1 for t in targets if t.active and not t.ok)
    return len(plans), passed, missing, refused


def cleanup_check_summary(plans: Sequence[CleanupPlan]) -> str:
    """After the automatic check, in one line (D41 4)."""
    _records, passed, missing, refused = cleanup_counts(plans)
    parts = [_("{passed} 项可清理", passed=passed) + (_("（已勾选）") if passed else "")]
    if missing:
        parts.append(_("{missing} 项{GONE}", missing=missing, GONE=_(GONE)))
    if refused:
        parts.append(_("{refused} 项未通过", refused=refused))
    text = _("检查完成：") + _("，").join(parts) + _("。")
    if any(t.recycle_refused for plan in plans for t in plan.targets):
        text += _(RECYCLE_WAYS_OUT)
    return text


def cleanup_check_log(plans: Sequence[CleanupPlan]) -> str:
    """The one log line of a check (D41 3)."""
    records, passed, missing, refused = cleanup_counts(plans)
    return _(
        "清理检查：{records} 条记录，{passed} 项可清理，{missing} 项{GONE}，{refused} 项未通过",
        records=records,
        passed=passed,
        missing=missing,
        GONE=_(GONE),
        refused=refused,
    )


def bin_line(bin_: RecycleBin, room: int | None = None) -> str:
    """``回收站 E:\\ 剩余 33.8 GB / 72.4 GB`` (D41 4); ``room`` is what is left for this run."""
    from enginesave.cleanup import DEFAULT_RECYCLE_LIMIT

    if bin_.disabled:
        return _("回收站 {drive} 已关闭（会直接删除）", drive=bin_.drive)
    if bin_.room is None:
        return _(
            "回收站 {drive} 大小未知（每项不超过 {v}）",
            drive=bin_.drive,
            v=size_text(DEFAULT_RECYCLE_LIMIT),
        )
    left = bin_.room if room is None else room
    return _(
        "回收站 {drive} 剩余 {v} / {v2}",
        drive=bin_.drive,
        v=size_text(max(left, 0)),
        v2=size_text(bin_.capacity or 0),
    )


def recycle_bin_lines(bins: Sequence[RecycleBin]) -> list[str]:
    return [bin_line(bin_) for bin_ in bins]


def cleanup_primary_text(method: str, targets: Sequence[CleanupTarget]) -> str:
    """The cleanup dialog's main button: what pressing it does, with count and size (D32);
    a permanent delete also says it cannot be undone, as it asks nothing more (D40)."""
    size = size_text(sum(t.bytes for t in targets))
    if method == PERMANENT:
        return (
            _("永久删除 {n} 项（{size}，无法还原）", n=len(targets), size=size)
            if targets
            else _("永久删除")
        )
    return (
        _("移到回收站（{n} 项，{size}）", n=len(targets), size=size) if targets else _("移到回收站")
    )


def continue_primary_text(count: int) -> str:
    """The main button while folders wait for room in the recycle bin (D41 6)."""
    return _("继续清理（{count} 项）", count=count)


def _entries(outcomes: Sequence[tuple[CleanupPlan, CleanupResult | None]]) -> list[dict]:
    return [e for _plan, result in outcomes if result is not None for e in result.targets]


def cleanup_batch_summary(
    outcomes: Sequence[tuple[CleanupPlan, CleanupResult | None]], method: str
) -> str:
    """One run, in one line: what went where, what failed, what waits (D41 4); a record whose
    check before removal failed is named."""
    entries = _entries(outcomes)
    recycled = [e for e in entries if e["result"] == "recycled"]
    deleted = [e for e in entries if e["result"] == "deleted"]
    parts = []
    if recycled:
        parts.append(_("已移到回收站 {n} 项 · {v}", n=len(recycled), v=size_text(_size(recycled))))
    if deleted:
        parts.append(_("已永久删除 {n} 项 · {v}", n=len(deleted), v=size_text(_size(deleted))))
    failed = sum(1 for e in entries if e["result"] == "failed")
    waiting = sum(1 for e in entries if e["result"] == "skipped_full_bin")
    if failed:
        parts.append(_("失败 {failed} 项", failed=failed))
    if waiting:
        parts.append(_("未处理 {waiting} 项（回收站已满）", waiting=waiting))
    stopped = [plan.display_name for plan, result in outcomes if result is None]
    if stopped:
        parts.append(_("再次检查没有通过、未删除：") + _("、").join(stopped))
    text = _("；").join(parts) or _("什么都没有删除")
    if recycled:
        text += _("。回收站里的内容清空前不释放空间")
    if _stopped(outcomes):
        text = _("已取消：") + text + _("；其余的没有处理")  # D58: between folders or records
    return text + _("。")


def _stopped(outcomes: Sequence[tuple[CleanupPlan, CleanupResult | None]]) -> bool:
    """Told to stop (D58) between records (``cleanup.batch.Outcomes.cancelled``) or between
    the folders of one."""
    return bool(getattr(outcomes, "cancelled", False)) or any(
        result is not None and result.cancelled for _plan, result in outcomes
    )


def _size(entries: Sequence[Mapping[str, Any]]) -> int:
    return sum(int(e["bytes"]) for e in entries)


def cleanup_done_log(
    outcomes: Sequence[tuple[CleanupPlan, CleanupResult | None]],
    purged: Sequence[Mapping[str, Any]] = (),
) -> str:
    """The one log line of a run (D41 3); 「已取消：…」 when it was stopped (D58)."""
    entries = _entries(outcomes)
    count = {r: sum(1 for e in entries if e["result"] == r) for r in CLEANUP_OUTCOMES}
    removed = [e for e in entries if e["result"] in CLEANUP_OUTCOMES]
    failed = sum(1 for e in entries if e["result"] == "failed")
    waiting = sum(1 for e in entries if e["result"] == "skipped_full_bin")
    stopped = sum(len(plan.targets) for plan, result in outcomes if result is None)
    head = _("已取消：其余的没有处理；") if _stopped(outcomes) else _("清理完成：")
    text = _(
        "{head}{v} 项进回收站 / {v2} 项永久删除，释放 {v3}，失败 {v4}，未处理 {waiting}",
        head=head,
        v=count["recycled"],
        v2=count["deleted"],
        v3=size_text(_size(removed)),
        v4=failed + stopped,
        waiting=waiting,
    )
    done = [p for p in purged if p.get("result") == "purged"]
    if done:
        text += _(
            "；从回收站删除了 EngineSave 之前放入的 {n} 项（{v}）",
            n=len(done),
            v=size_text(_size(done)),
        )
    return text


def cleanup_outcome(entry: Mapping[str, Any]) -> str:
    """The check column after a run: ``已移到回收站 · 12.3 GB`` (D41 4)."""
    if entry["result"] == "skipped_full_bin":
        return _(LEFT_FOR_LATER)
    if entry["result"] == "gone":
        return _(GONE)
    if entry["result"] in CLEANUP_OUTCOMES:
        return f"{_(CLEANUP_OUTCOMES[entry['result']])} · {size_text(int(entry['bytes']))}"
    return _("失败：{v}", v=entry["error"])


# ------------------------------------------------------------ a full recycle bin (0.7.0, D41 6)


@dataclass(frozen=True)
class BinNeed:
    """One drive in the "recycle bin is full" dialog."""

    drive: str
    room: int  # left in its bin for this run
    waiting: int  # bytes of the folders still meant for the bin (not ticked for a permanent delete)
    count: int  # how many such folders
    purge: int  # bytes of EngineSave's earlier items ticked to be purged from the bin

    @property
    def missing(self) -> int:
        return max(self.waiting - self.room - self.purge, 0)


def bin_needs(
    rooms: Mapping[str, int],
    waiting: Sequence[tuple[str, int, bool]],
    ours: Sequence[tuple[str, int, bool]],
) -> list[BinNeed]:
    """Per drive: ``waiting`` are ``(drive, bytes, delete permanently)``, ``ours`` are
    ``(drive, bytes, ticked to purge)``."""
    drives = sorted({d for d, _s, _t in waiting})
    needs = []
    for drive in drives:
        meant = [s for d, s, permanent in waiting if d == drive and not permanent]
        needs.append(
            BinNeed(
                drive,
                max(rooms.get(drive, 0), 0),
                sum(meant),
                len(meant),
                sum(s for d, s, ticked in ours if d == drive and ticked),
            )
        )
    return needs


def full_bin_heading(needs: Sequence[BinNeed]) -> list[str]:
    """``回收站 E:\\ 剩余 0.3 GB，待放入 10 项共 45.6 GB，还差 45.3 GB`` per drive."""
    return [
        _(
            "回收站 {drive} 剩余 {v}，待放入 {count} 项共 {v2}，还差 {v3}",
            drive=n.drive,
            v=size_text(n.room),
            count=n.count,
            v2=size_text(n.waiting),
            v3=size_text(max(n.waiting - n.room, 0)),
        )
        for n in needs
    ]


def full_bin_status(needs: Sequence[BinNeed]) -> str:
    """The live line under the two lists: what the ticks make room for, what is still missing."""
    freed = sum(n.purge for n in needs)
    missing = sum(n.missing for n in needs)
    if missing:
        return _("腾出 {v} · 还差 {v2}", v=size_text(freed), v2=size_text(missing))
    return _("腾出 {v} · 够了，可以继续清理", v=size_text(freed))
