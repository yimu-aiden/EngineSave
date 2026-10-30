"""The export and import dialogs: targets, rows, counts and summaries (D27, D31, D33 7)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from enginesave.appdirs import documents_dir
from enginesave.gui.i18n import _
from enginesave.gui.settings import GuiSettings
from enginesave.gui.state.common import NO_VALUE, size_text

if TYPE_CHECKING:
    from enginesave.export import BatchResult, ExportResult, ImportPlan


def export_folder(settings: GuiSettings) -> Path:
    """Where exports go by default: the last export folder, the first time the Documents."""
    return Path(settings.last_export_dir) if settings.last_export_dir.strip() else documents_dir()


def export_target(folder: str, display_name: str, snapshot_id: str) -> Path:
    """Suggested archive path: the given folder (Documents when empty) + the default name."""
    from enginesave.export import default_export_name

    base = Path(folder) if folder.strip() else documents_dir()
    return base / default_export_name(display_name, snapshot_id)


def export_forbidden_roots(settings: GuiSettings) -> list[Path]:
    """The window's scan roots, passed to ``export_snapshot`` as extra forbidden folders."""
    return [Path(root) for root in settings.scan_roots]


def export_summary(result: ExportResult) -> str:
    """The export dialog's result line: what was written, how paths look, what was left out."""
    from enginesave.export import EXCLUDED_ABSOLUTE, EXCLUDED_KEYS, PATHS_ANONYMIZED

    parts = [
        _(
            "已导出并逐个校验 {file_count} 个文件（{v}），归档 {v2}：{path}。",
            file_count=result.file_count,
            v=size_text(result.total_bytes),
            v2=size_text(result.archive_bytes),
            path=result.path,
        )
    ]
    if result.paths == PATHS_ANONYMIZED:
        parts.append(_("路径已匿名化（写成 GAME_ROOT/… 与 RENPY_SAVES_ROOT/…）。"))
    else:
        parts.append(_("保留了本机的原始路径，分享前请留意。"))
    reasons = result.excluded_reasons
    if EXCLUDED_KEYS in reasons.values():
        parts.append(_("未包含 tokens（私钥）。"))
    absolute = sorted(s for s, why in reasons.items() if why == EXCLUDED_ABSOLUTE)
    if absolute:
        parts.append(
            _(
                "未包含只能用绝对路径表示的存档目录 {v}（勾选“保留原始路径”才会包含）。",
                v=", ".join(absolute),
            )
        )
    if result.includes_tokens:
        parts.append(_("注意：归档里有私钥，请勿分享。"))
    return "".join(parts)


# ------------------------------------------------------------------ batch export/import (D27)

BATCH_LABELS = {"exported": "已导出", "skipped": "已跳过", "failed": "失败", "imported": "已导入"}
SKIP_REASONS = {
    "the file already exists": "同名文件已存在（勾选“覆盖同名文件”可替换）",
    "no manual or imported snapshot": "没有手动备份或导入的快照",
}
MATCHED = {
    None: "新记录",
    "game_id": "已有记录（同一记录 ID）",
    "identity": "已有记录（存档目录或游戏目录相同）",
}


def batch_export_rows(batch: BatchResult) -> list[tuple[str, ...]]:
    """结果 / 名称 / 快照 / 文件 / 说明, one row per record."""
    rows = []
    for item in batch.items:
        done = item["export"]
        if done:
            note = _(
                "{v} 个文件，归档 {v2}", v=done["file_count"], v2=size_text(done["archive_bytes"])
            )
        else:
            note = _(SKIP_REASONS.get(item["reason"], item["reason"] or ""))
        rows.append(
            (
                _(BATCH_LABELS[item["result"]]),
                item["display_name"],
                item["snapshot_id"] or NO_VALUE,
                Path(item["path"]).name if item["path"] else NO_VALUE,
                note,
            )
        )
    return rows


def batch_export_primary_text(folder: str) -> str:
    """The batch export button names the folder it writes to (D32)."""
    text = folder.strip()
    if not text:
        return _("导出")
    name = Path(text).name or text
    return _("导出到 {name}", name=name)


def import_primary_text(importable: int) -> str:
    return _("导入（{importable} 个）", importable=importable) if importable else _("导入")


def import_check_summary(importable: int, total: int, skipped: int = 0) -> str:
    text = _(
        "检查完成：{importable}/{total} 个导出文件可以导入。", importable=importable, total=total
    )
    if skipped:
        text += _(
            "{skipped} 个将跳过：已导入过，"
            "或与同一文件夹里前面的文件是同一快照（勾选“重新导入已导入过的归档”可再导入）。",
            skipped=skipped,
        )
    return text + (_("点“导入”开始。") if importable else "")


STOPPED_BATCH = "已取消：列出的已经完成，其余没有开始。"  # D58


def batch_export_summary(batch: BatchResult) -> str:
    from enginesave.export import PATHS_ANONYMIZED

    counts = {outcome: batch.count(outcome) for outcome in ("exported", "skipped", "failed")}
    text = _(
        "已导出 {v} 个、跳过 {v2} 个、失败 {v3} 个（每个导出文件都已逐个校验）：{location}。",
        v=counts["exported"],
        v2=counts["skipped"],
        v3=counts["failed"],
        location=batch.location,
    )
    exports = [item["export"] for item in batch.items if item["export"]]
    if any(e["paths"] == PATHS_ANONYMIZED for e in exports):
        text += _("路径已匿名化。")
    if any(e["includes_tokens"] for e in exports):
        text += _("注意：有导出文件含私钥，请勿分享。")
    if batch.cancelled:  # D58
        text += _(STOPPED_BATCH)
    return text


def import_preview_rows(
    previews: Sequence[tuple[Path, ImportPlan | None, str | None]], reimport: bool = False
) -> list[tuple[str, ...]]:
    """导出文件 / 游戏 / 快照 / 文件数 / 大小 / 检查, one row per archive (nothing written).

    An archive the library already holds as an import is skipped unless ``reimport`` (D31).
    """
    rows = []
    for archive, plan, error in previews:
        if plan is None:
            rows.append(
                (archive.name, NO_VALUE, NO_VALUE, "", "", _("无法导入：{error}", error=error))
            )
            continue
        files = plan.manifest["files"]
        if plan.already_imported is not None:
            snapshot = plan.already_imported["snapshot_id"]
            check = (
                _("重新导入：已导入过（快照 {snapshot}）", snapshot=snapshot)
                if reimport
                else _("已导入过（将跳过）：快照 {snapshot}", snapshot=snapshot)
            )
        elif plan.duplicate_of is not None:  # S-098: a second copy in the same folder
            check = (
                _("重新导入：与 {duplicate_of} 是同一快照", duplicate_of=plan.duplicate_of)
                if reimport
                else _(
                    "批内重复（将跳过）：与 {duplicate_of} 是同一快照",
                    duplicate_of=plan.duplicate_of,
                )
            )
        else:
            check = _("可以导入：{v}", v=_(MATCHED[plan.matched_by]))
        rows.append(
            (
                archive.name,
                plan.display_name,
                str(plan.header["snapshot_id"]),
                str(len(files)),
                size_text(sum(f["size"] for f in files)),
                check,
            )
        )
    return rows


def import_counts(
    previews: Sequence[tuple[Path, ImportPlan | None, str | None]], reimport: bool = False
) -> tuple[int, int, int]:
    """(to import, skipped as already imported or repeated in the batch, cannot be imported)."""
    plans = [plan for _archive, plan, _error in previews if plan is not None]
    skipped = (
        0
        if reimport
        else sum(1 for p in plans if p.already_imported is not None or p.duplicate_of is not None)
    )
    return len(plans) - skipped, skipped, len(previews) - len(plans)


def import_rows(batch: BatchResult) -> list[tuple[str, ...]]:
    """The same columns after importing: the new snapshot and what happened."""
    rows = []
    for item in batch.items:
        name = Path(item["archive"]).name
        done = item["import"]
        plan = item.get("plan") or {}
        if item["result"] == "skipped":
            earlier = plan.get("already_imported") or {}
            rows.append(
                (
                    name,
                    str(plan.get("display_name", NO_VALUE)),
                    str(plan.get("source_snapshot_id", NO_VALUE)),
                    str(plan.get("file_count", "")),
                    size_text(int(plan.get("total_bytes", 0))),
                    _("已跳过：已导入过（快照 {v}）", v=earlier.get("snapshot_id", NO_VALUE)),
                )
            )
            continue
        if done is None:
            rows.append((name, NO_VALUE, NO_VALUE, "", "", _("失败：{v}", v=item["error"])))
            continue
        record = _("新记录") if done["created_record"] else _("已有记录")
        rows.append(
            (
                name,
                done["display_name"],
                done["snapshot_id"],
                str(done["file_count"]),
                size_text(done["total_bytes"]),
                _("已导入（{record}）", record=record),
            )
        )
    return rows


def import_summary(batch: BatchResult) -> str:
    text = _("已导入 {v} 个", v=batch.count("imported"))
    if batch.count("skipped"):
        text += _("、跳过 {v} 个（已导入过）", v=batch.count("skipped"))
    text += _("、失败 {v} 个。", v=batch.count("failed"))
    if batch.count("imported"):
        text += _("要放回游戏，在备份库页选中记录后点“恢复…”。")
    if batch.cancelled:  # D58
        text += _(STOPPED_BATCH)
    return text
