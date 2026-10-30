"""Guidance: the override notice, the first steps and what to do after a backup (D29, D32)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from enginesave.appdirs import portable
from enginesave.engines.renpy.paths import saves_root_override
from enginesave.gui.i18n import _
from enginesave.gui.settings import GuiSettings
from enginesave.gui.state.cleaning import cleanup_candidates
from enginesave.gui.state.common import checked_text, size_text
from enginesave.gui.state.scanning import plan_totals, plan_warnings
from enginesave.models import ScanReport

if TYPE_CHECKING:
    from enginesave.backup import BackupPlan, BackupResult

# ---------------------------------------------------------------------- notices (0.3.1)


@dataclass(frozen=True)
class OverrideNotice:
    text: str
    clearable: bool  # False when the override comes from ENGINESAVE_RENPY_SAVES_ROOT


def override_notice(settings: GuiSettings, env: Mapping[str, str]) -> OverrideNotice | None:
    """The yellow bar while a saves-root override is in effect, ``None`` otherwise (S-065):
    the window's own (clearable), the environment's, and other engines' root tokens with their
    conflicts (D66 7) -- all of them, whichever are set (M44 review)."""
    from enginesave import roots
    from enginesave.engines.renpy.paths import OVERRIDE_ENV, ROOT_OVERRIDE_ENV

    pieces = []
    if settings.override_path is not None:
        pieces.append(
            _(
                "全局存档根目录已改为：{override_path}（仅用于测试）",
                override_path=settings.override_path,
            )
        )
    parts = []
    from_env = saves_root_override(None, env) if settings.override_path is None else None
    if from_env is not None:
        variable = ROOT_OVERRIDE_ENV if env.get(ROOT_OVERRIDE_ENV) else OVERRIDE_ENV
        parts.append(
            _(
                "全局存档根目录已改为：{from_env}（来自环境变量 {variable}）",
                from_env=from_env,
                variable=variable,
            )
        )
    for token in roots.ENGINE_PROFILE_TOKENS:  # 0.10.0 (D65 1): other engines' folders
        if env.get(roots.env_name(token)):
            parts.append(
                _(
                    "{token} 已改为：{v}（来自 {v2}）",
                    token=token,
                    v=env[roots.env_name(token)],
                    v2=roots.env_name(token),
                )
            )
    if parts:
        pieces.append(_("；").join(parts) + _("（仅用于测试，要在系统环境变量里删除）"))
    if not pieces:
        return None
    text = _("；").join(pieces)
    # D66 7: a token moved onto a scan root or the library (the command line refuses it)
    libraries = [Path(settings.repo_path)] if settings.repo_path else []
    scanned = [Path(r) for r in settings.scan_roots]
    words = {  # D70 8: in the window's language
        "scan": _("{label} 与扫描根目录 {root} 重叠"),
        "library": _("{label} 与备份库 {root} 重叠"),
        "token": _("{label} 也是 {other} 的文件夹"),
    }
    conflicts = roots.override_conflicts(env, scanned, libraries, words)
    if conflicts:
        text += _("。冲突，扫描、恢复与清理前请先改掉：") + _("；").join(conflicts)
    return OverrideNotice(text, settings.override_path is not None)


def config_location(folder: Path, *, is_portable: bool | None = None) -> str:
    """The settings dialog's line on where settings and logs live (D28)."""
    if is_portable is None:
        is_portable = portable()
    where = _("设置与日志保存在：{folder}", folder=folder)
    if is_portable:
        return where + _("（便携版：程序旁边的 config 文件夹，随整个文件夹一起移动）")
    return where


FIRST_STEPS = (
    "选择备份库：在上方“备份库”一栏点“浏览…”，选择或新建一个文件夹"
    "（建议放在另一块 NTFS 硬盘上，不要放在游戏目录里）。",
    "添加扫描根目录：点“添加…”选择存放游戏的文件夹。",
    "点“扫描”（只读，也可以按 F5）。",
    "在“勾选”列勾上要备份的游戏（已确认的游戏默认已勾）。",
    "点「备份已勾选项」直接开始备份（想先看清会复制什么，点「预览…」）。",
    "到“备份库”页点“全选”再点“校验”，确认全部 OK。",
)
DONE_MARK, NEXT_MARK = "✔", "→"


def guidance(
    settings: GuiSettings, report: ScanReport | None = None, checked: tuple[int, int] = (0, 0)
) -> str | None:
    """The banner above the scan results (D29).

    Until a library and a scan root are chosen: the six first steps, the finished ones ticked
    and an arrow at the next one. After a scan: how many games are ticked (the orphaned folders
    have their own dialog since 0.7.0, D42) and where to go next. Otherwise (and after a scan
    that found nothing) no banner.
    """
    have = (bool(settings.repo_path), bool(settings.scan_roots))
    if not all(have):
        upcoming = have.index(False)
        lines = [_("首次使用，按这六步来：")]
        for index, text in enumerate(FIRST_STEPS):
            done = index < len(have) and have[index]
            mark = DONE_MARK if done else (NEXT_MARK if index == upcoming else "  ")
            lines.append(f"{mark} {index + 1}. {_(text)}")
        return "\n".join(lines)
    if report is None or not report.games:
        return None
    games, unlinked = checked  # orphaned folders ticked in their dialog go along (D42)
    if games or unlinked:
        what = checked_text(games, unlinked)
        return _("{what}，点击「备份已勾选项」直接开始，或「预览…」先看将复制的内容。", what=what)
    return _("还没有勾选：在“勾选”列勾上要备份的游戏，再点击「备份已勾选项」。")


def backup_plan_lines(plan: BackupPlan) -> list[str]:
    """What a one-click backup is about to copy, for the log panel (D32: no preview dialog)."""
    games, files, size = plan_totals(plan)
    lines = [
        _(
            "开始备份到 {dest}：{games} 项，{files} 个文件，{v}",
            dest=plan.dest,
            games=games,
            files=files,
            v=size_text(size),
        )
    ]
    for planned in plan.games:
        mark = _("（孤立目录）") if planned.kind == "unlinked" else ""
        for ps in planned.sources:
            lines.append(
                _(
                    "  {display_name}{mark} · {source_id}：{path}（{n} 个文件，{v}）",
                    display_name=planned.display_name,
                    mark=mark,
                    source_id=ps.source.source_id,
                    path=ps.source.path,
                    n=len(ps.files),
                    v=size_text(ps.total_bytes),
                )
            )
    lines += [_("  提示：{warning}", warning=warning) for warning in plan_warnings(plan)]
    return lines


def backup_status(plan_summary: tuple[int, int, int], value: tuple[int, int, str]) -> str:
    """The status bar while backing up: which item, and the totals of the whole backup."""
    games, files, size = plan_summary
    done, total, name = value
    return _(
        "正在备份 {name}（{v}/{total}）… 共 {games} 项、{files} 个文件、{v2}",
        name=name,
        v=done + 1,
        total=total,
        games=games,
        files=files,
        v2=size_text(size),
    )


def backup_next_steps(result: BackupResult) -> str | None:
    """What to do after a backup (D29): verify; then clean up or export."""
    if not any(r.status in ("ok", "partial") for r in result.results):
        return None
    text = _("下一步：到“备份库”页点“校验”，确认快照完好")
    if cleanup_candidates(result):
        text += _("；需要释放磁盘空间时，可点下方的“清理…”（游戏本体不在备份里）")
    return text + _("；要把存档带到别的电脑，在“备份库”页勾选记录后点“导出…”。")
