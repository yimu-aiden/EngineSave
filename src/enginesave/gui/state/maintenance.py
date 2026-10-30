"""Library upkeep on the library page (0.9.0): the overview line (D56), 「清理临时文件夹」
(D55) and 「镜像到…」 (D57). Texts and rows only; the work is in ``enginesave.repo``,
``enginesave.removal`` and ``enginesave.mirror``."""

from __future__ import annotations

import os
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING

from enginesave.fsutil import iso_from_ns
from enginesave.gui.i18n import _
from enginesave.gui.state.common import CHECKED, NO_VALUE, UNCHECKED, local_time, size_text

if TYPE_CHECKING:
    from enginesave.backup import BackupResult
    from enginesave.migrate import MigrationPlan, MigrationResult
    from enginesave.mirror import MirrorResult
    from enginesave.removal import LibraryStats
    from enginesave.repo import TmpFolder, TmpRemoval

# ------------------------------------------------------------------------- overview (D56)


def overview_text(stats: LibraryStats) -> str:
    """``4 条记录（本体存在 2、本体不在 2，其中孤立目录 1）· 9 个快照 · 占用 1.2 GB · 去重前
    3.4 GB，省 65% · 上次备份 2026-10-25 · 上次镜像 2026-10-24``. 「本体不在」 counts the
    orphaned-folder records too, like the page's group of that name."""
    gone = _("本体不在 {gone}", gone=stats.gone) + (
        _("，其中孤立目录 {unlinked}", unlinked=stats.unlinked) if stats.unlinked else ""
    )
    parts = [
        _(
            "{records} 条记录（本体存在 {installed}、{gone}）",
            records=stats.records,
            installed=stats.installed,
            gone=gone,
        ),
        _("{snapshots} 个快照", snapshots=stats.snapshots),
        _("占用 {v}", v=size_text(stats.stored_bytes)),
        _(
            "去重前 {v}，省 {saved_percent}%",
            v=size_text(stats.logical_bytes),
            saved_percent=stats.saved_percent,
        ),
        _("上次备份 {v}", v=local_time(stats.last_backup)[:10] or NO_VALUE),
        _("上次镜像 {v}", v=local_time(stats.last_mirror)[:10])
        if stats.last_mirror
        else _("上次镜像 无"),  # S-232: its own text ("Last mirror None" does not read)
    ]
    text = " · ".join(parts)
    if stats.unreadable:
        text += _("（{unreadable} 个快照的清单读不出，未计入大小）", unreadable=stats.unreadable)
    return text


# ------------------------------------------------------------------- temporary folders (D55)

TMP_TITLE = "清理临时文件夹"
TMP_INTRO = (
    "这些是中断的备份、导入或镜像留下的临时文件夹，里面没有完整的快照。删除后立即回收对象库里"
    "没有快照用到的空间。10 分钟内有改动的可能正在使用，不能勾选。"
)
TMP_RECENT = "可能正在使用"


def tmp_link_text(count: int) -> str:
    """The library page's status line link (D55); empty when there is none."""
    return _("清理临时文件夹（{count}）…", count=count) if count else ""


def tmp_row_values(folder: TmpFolder, ticked: bool, now: float | None = None) -> tuple[str, ...]:
    recent = folder.recent(now)
    check = "" if recent else (CHECKED if ticked else UNCHECKED)
    return (
        check,
        str(folder.path),
        size_text(folder.bytes),
        str(folder.files),
        local_time(iso_from_ns(folder.modified_ns)),
        _(TMP_RECENT) if recent else "",
    )


def tmp_primary_text(count: int) -> str:
    return _("删除（{count} 个）并回收对象库空间", count=count)


def tmp_done_text(result: TmpRemoval) -> str:
    text = _(
        "已删除 {n} 个临时文件夹，释放 {v}", n=len(result.removed), v=size_text(result.freed_bytes)
    )
    garbage = result.garbage
    if result.cancelled:
        text += _("；已取消，没有回收对象库")
    elif garbage is not None and garbage.skipped:
        text += _("；对象库这次没有回收（{skipped}）", skipped=garbage.skipped)
    if result.recent:
        text += _("；{n} 个刚有改动，可能正在使用，保留", n=len(result.recent))
    if result.store_busy:  # D59 4
        text += _("；有文件正在写入对象库，临时快照文件夹一并保留")
    if result.leftovers:
        text += _("；{n} 项没能删除（见日志）", n=len(result.leftovers))
    return text + _("。")


def tmp_ticks(folders: Collection[TmpFolder], now: float | None = None) -> set[Path]:
    """Every leftover that may go starts ticked; the recent ones cannot be ticked."""
    return {f.path for f in folders if not f.recent(now)}


# --------------------------------------------------------------------------- mirror (D57)

MIRROR_TITLE = "镜像到…"
MIRROR_INTRO = (
    "把整个备份库复制到另一个文件夹，最好在另一块盘上：它本身就是一个能用的备份库，可以在上方"
    "“备份库”一栏选中它校验或恢复。以后再镜像到同一个文件夹只复制新的内容；镜像里的东西从不删除。"
)
MIRROR_VERIFY = "镜像后校验"
MIRROR_REPAIR = "修复镜像（逐个核对镜像里已有对象的内容，较慢）"  # D59 3: mirror --repair
MIRROR_NONE = "（还没选文件夹）"
SAME_DRIVE = "镜像与备份库在同一块盘上：这块盘坏了两份会一起丢，最好选另一块盘。"
MIRROR_NEW = "这是一个空文件夹：将建立新的镜像。"
MIRROR_AGAIN = "这是这个备份库之前的镜像：只复制新的内容。"


AUTO_MIRROR = "备份完成后自动镜像到上次镜像的文件夹（{folder}）"  # D64
STOP_MIRROR = "取消镜像"


def not_mirrored_reason(result: BackupResult) -> str:
    """Why a backup is not mirrored automatically (D66 9)."""
    if result.cancelled:
        return _("备份时按了“取消”")
    if any(r.status == "failed" for r in result.results):
        return _("备份有失败的项")
    return _("这次没有写出快照")


def backup_succeeded(result: BackupResult) -> bool:
    """D64: a backup worth mirroring right away -- not stopped, nothing failed, and at least
    one snapshot written (a partial one too: what it holds is worth a second copy)."""
    statuses = [r.status for r in result.results]
    return (
        not result.cancelled
        and "failed" not in statuses
        and any(r.snapshot_id for r in result.results)
    )


def auto_mirror_skipped(reason: str) -> str:
    """One line for the log and the status bar; no message box (D64)."""
    return _("未自动镜像：{reason}", reason=reason)


def auto_mirror_text(result: MirrorResult) -> str:
    """The line added at the bottom of the backup's result dialog (D64)."""
    where = str(result.target)
    if result.cancelled:
        return _("自动镜像已取消（{where}；再镜像一次会接着做）", where=where)
    if result.exit_code != 0:
        return _("自动镜像到 {where} 有问题：{v}", where=where, v=mirror_done_text(result))
    text = _(
        "已镜像到 {where}：复制 {objects_copied} 个对象、{snapshots_copied} 个快照",
        where=where,
        objects_copied=result.objects_copied,
        snapshots_copied=result.snapshots_copied,
    )
    if result.verified:
        text += _("，校验通过")
    elif result.verify_stopped:
        text += _("，校验已取消")
    return text + _("。")


def same_drive(a: Path, b: Path) -> bool:
    return os.path.splitdrive(os.path.abspath(a))[0].casefold() == (
        os.path.splitdrive(os.path.abspath(b))[0].casefold()
    )


def mirror_primary_text(target: Path | None) -> str:
    return _("开始镜像") if target is None else _("镜像到 {v}", v=target.name or target)


def mirror_done_text(result: MirrorResult) -> str:
    """One line for the dialog, the log and the status bar."""
    if result.cancelled:
        head = _("镜像已取消（已复制的部分保留，再镜像一次会接着做）")
    elif result.verify_stopped:  # D59 2: copied completely, only the check was stopped
        head = _("镜像完成，校验已取消")
    else:
        head = _("镜像完成")
    text = _(
        "{head}：复制 {objects_copied} 个对象、{snapshots_copied} 个快照，"
        "共 {v}；已有 {snapshots_present} 个快照",
        head=head,
        objects_copied=result.objects_copied,
        snapshots_copied=result.snapshots_copied,
        v=size_text(result.bytes_copied),
        snapshots_present=result.snapshots_present,
    )
    if result.failed:
        text += _("；{n} 项没能复制（见日志）", n=len(result.failed))
    if result.unreadable:
        text += _("；{n} 个快照的清单读不出，没有镜像", n=len(result.unreadable))
    if result.kept_records:
        text += _("；镜像里保留了 {n} 条备份库已删除的记录", n=len(result.kept_records))
    if result.objects_recopied:
        text += _("；重新复制 {objects_recopied} 个对象", objects_recopied=result.objects_recopied)
    if result.files_replaced:
        text += _("；重新放置 {files_replaced} 个快照文件", files_replaced=result.files_replaced)
    if result.verified is True:
        text += _("；校验通过")
    elif result.verified is False:
        text += _("；校验发现问题（见日志）")
    return text + _("。")


# ----------------------------------------------------------------- migration (0.11.0, D67 8)

MIGRATE_TITLE = "迁移备份库"
MIGRATE = "迁移"
LATER = "稍后"
READ_ONLY = (
    "只读：这个备份库是 0.10.0 及更早的旧布局，迁移后才能备份、恢复、导入、清理、删除、"
    "确认 / 关联与重命名文件夹"
)
RENAME_TITLE = "按当前名字重命名文件夹"


def migrate_intro(plan: MigrationPlan) -> str:
    """The dialog's question (D67 8)."""
    count = len(plan.renames) or sum(m.state != "done" for m in plan.moves)
    if plan.format_version != 1:  # D70 1: a migration that stopped before it finished
        return _(
            "这个备份库上次没有迁移完：还有 {count} 个记录文件夹要改成游戏名（之前的已经改好）",
            count=count,
        )
    return _(
        "这个备份库是旧布局：把 {count} 个记录文件夹改成游戏名、"
        "补写说明文件（一次性；之后 0.10 及更早版本打不开它，迁移完成前也不要用它们打开）",
        count=count,
    )


def migrate_details(plan: MigrationPlan, limit: int = 8) -> str:
    """A few of the new names, and why it cannot be migrated now."""
    lines = [f"games\\{m.old} → games\\{m.new}" for m in plan.renames[:limit]]
    if len(plan.renames) > limit:
        lines.append(_("……另外 {v} 个", v=len(plan.renames) - limit))
    lines += [_("现在不能迁移：{reason}", reason=reason) for reason in plan.refusals]
    return "\n".join(lines)


def migration_done_text(result: MigrationResult) -> str:
    """The log line after a migration; also the message when it did not finish."""
    if result.already:
        return _("这个备份库已经迁移过。")
    text = _("已把 {renamed} 个记录文件夹改成游戏名", renamed=result.renamed)
    if result.missing:
        text += _("，{n} 条记录还没有文件夹（下次备份时建）", n=len(result.missing))
    if result.cancelled:
        return text + _("；已取消，还没迁移完（再迁移一次会接着做）")
    if result.failed:
        return text + _("；没有迁移完（再迁移一次会接着做）：") + _("；").join(result.failed)
    return text + _(
        "，迁移完成；原来的 catalog.json 另存为 {name}", name=Path(result.backup or "").name
    )


def rename_question(old: str, new: str) -> str:
    return _(
        "把备份库里这条记录的文件夹\n  games\\{old}\n改成\n  games\\{new}\n\n记录 ID 与快照不变；"
        "镜像下次会跟着改名。",
        old=old,
        new=new,
    )
