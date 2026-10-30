"""The "scan and back up" page: rows, notes, details, the skipped folders and the backup
plan's totals."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enginesave.engines.labels import engine_label
from enginesave.gui.i18n import _
from enginesave.gui.settings import GuiSettings
from enginesave.gui.state.changes import Change
from enginesave.gui.state.common import (
    CHECKED,
    LOCAL_ONLY_MARK,
    NO_VALUE,
    UNCHECKED,
    Selection,
    game_key,
    local_time,
    size_text,
    unlinked_key,
)
from enginesave.models import (
    DetectedGame,
    GameStatus,
    ScanReport,
    SourceKind,
    SourceStatus,
    UnlinkedSaveDir,
)

if TYPE_CHECKING:
    from enginesave.backup import BackupPlan, BackupResult


def backup_options(
    selection: Selection, report: ScanReport, settings: GuiSettings
) -> dict[str, Any]:
    """Keyword arguments for ``prepare_backup`` from the ticked games, the orphaned folders
    ticked in their dialog (with the games again since 0.8.0, D42) and the settings."""
    return {
        "select_roots": selection.checked_roots(report),
        "include_candidates": settings.include_candidates,
        "include_unlinked": selection.checked_unlinked(report) or None,  # [] would mean all
        "include_tokens": settings.include_tokens,
        "excludes": list(settings.scan_excludes),  # D63: the same folders as the scan skips
    }


def unlinked_dialog_count(games: int, unlinked: int) -> str:
    """Above the orphaned-folders list: what its 「备份已勾选项」 backs up (S-131)."""
    text = _("已勾选 {unlinked} 个孤立目录", unlinked=unlinked)
    return text + (_("，连同扫描页勾选的 {games} 款游戏一起备份", games=games) if games else "")


def unlinked_link_text(count: int) -> str | None:
    """The link under the games table that opens the orphaned folders (D42); none when 0."""
    return _("另有 {count} 个孤立存档目录…", count=count) if count else None


def game_search_fields(game: DetectedGame) -> list[str | None]:
    """What the scan page's search box looks at (D46): name, install folder names, save
    directory."""
    folders = [Path(game.game_root).name]
    folders += [Path(str(i["game_root"])).name for i in game.installs if i.get("game_root")]
    return [
        game.display_name,
        *folders,
        game.identity.renpy_save_directory or game.identity.engine_key,
    ]


@dataclass(frozen=True)
class GameRow:
    key: str
    check: str
    status: str
    name: str
    version: str
    engine: str
    local: str
    global_: str
    notes: str

    def values(self) -> tuple[str, ...]:
        return (
            self.check,
            self.status,
            self.name,
            self.version,
            self.engine,
            self.local,
            self.global_,
            self.notes,
        )


def source_cell(game: DetectedGame, kind: SourceKind) -> str:
    parts = []
    for source in (s for s in game.sources if s.kind is kind):
        text = (
            _(
                "{file_count} 个文件 · {v}",
                file_count=source.file_count,
                v=size_text(source.total_bytes),
            )
            if source.file_count
            else _("无文件")
        )
        if source.status is not SourceStatus.CONFIRMED:
            text += _("（{value}）", value=source.status.value)
        parts.append(text)
    return _("；").join(parts) or NO_VALUE


LIBRARY_SWITCHED = "换了备份库，请重新扫描"  # D61 ④
EXCLUDES_LABEL = (  # D63
    "扫描时跳过的文件夹（不扫描，也不进入；可以选文件夹，也可以写通配模式，如 *\\小说*）："
)


# folder names that have nothing in common: a pattern that skips them all skips everything
EXCLUDE_PROBES = ("a", "Game v1.0", "セーブ", "x_y-z", "1")
STAND_IN_ROOT = "C:\\Games"  # where there is no scan root yet


def exclude_warnings(patterns: Sequence[str], scan_roots: Sequence[str]) -> list[str]:
    """D66 8, M44 review: patterns of the folders to skip that skip every folder below a scan
    root (``*``, ``*\\*``, ``<root>\\*``: judged by unrelated names below it), or that name a
    scan root itself (which is still scanned, D63), said when the settings are kept."""
    from enginesave.discovery import is_excluded

    found = []
    roots = list(scan_roots) or [STAND_IN_ROOT]
    for pattern in patterns:
        every = [
            r for r in roots if all(is_excluded(Path(r) / p, [pattern]) for p in EXCLUDE_PROBES)
        ]
        if every and scan_roots:
            found.append(
                _("“{pattern}”会跳过扫描根目录 {v} 下的所有文件夹", pattern=pattern, v=every[0])
            )
        elif every:
            found.append(_("“{pattern}”会跳过扫描根目录下的所有文件夹", pattern=pattern))
        elif hit := [r for r in scan_roots if is_excluded(Path(r), [pattern])]:
            found.append(_("“{pattern}”：{v}", pattern=pattern, v=root_excluded_text(hit[0])))
    return found


def root_excluded_text(root: str) -> str:
    """D63: a scan root that is itself on the list of folders to skip."""
    return _(
        "{root} 在“扫描时跳过的文件夹”里：作为扫描根目录它仍会被扫描，"
        "跳过只对它下面的文件夹生效。不想扫描它，就把它从扫描根目录里移除。",
        root=root,
    )


def game_notes(game: DetectedGame, change: Change | None = None, stale: bool = False) -> str:
    """The 「备注」 column; a CONFIRMED game starts with how its saves compare with its latest
    snapshot (D54 ③), which also says whether it is in the library. ``stale``: another
    library was chosen after the scan (D61 ④)."""
    notes = [change.note()] if change is not None else []
    if stale and game.status is not GameStatus.UNSUPPORTED:
        notes.append(_(LIBRARY_SWITCHED))
    if game.status is GameStatus.UNSUPPORTED:
        notes.append(_("不支持：{v}", v=engine_label(game.engine)))
    if game.known_game_id and (change is None or (not change.backed_up and change.recorded)):
        notes.append(_("已在备份库"))
    if game.link_suggestions:
        notes.append(_("可关联 {n} 条记录", n=len(game.link_suggestions)))
    if any(LOCAL_ONLY_MARK in note for s in game.sources for note in s.notes):
        notes.append(_("只在本地存档"))
    if any(s.status is SourceStatus.CANDIDATE for s in game.sources):
        notes.append(_("有待确认的来源"))
    if any(s.status is SourceStatus.USER_CONFIRMED for s in game.sources):
        notes.append(_("含已确认的来源"))
    if game.shared_global_with:  # D48 1: both games back the folder up
        notes.append(_("与 {n} 款游戏共用全局存档目录", n=len(game.shared_global_with)))
    extra = sum(1 for s in game.sources if s.kind is SourceKind.EXTRA)
    if extra:
        notes.append(_("额外来源 {extra} 个", extra=extra))
    if game.warnings:
        notes.append(_("{n} 条警告", n=len(game.warnings)))
    return _("；").join(notes)


def game_row(
    game: DetectedGame,
    selection: Selection,
    change: Change | None = None,
    stale: bool = False,
) -> GameRow:
    key = game_key(game)
    check = "" if key in selection.locked else (CHECKED if selection.games.get(key) else UNCHECKED)
    return GameRow(
        key=key,
        check=check,
        status=game.status.value,
        name=game.display_name,
        version=game.identity.config_version or "",
        engine=f"{engine_label(game.engine)} {game.engine_version or ''}".strip(),
        local=source_cell(game, SourceKind.LOCAL),
        global_=source_cell(game, SourceKind.GLOBAL),
        notes=game_notes(game, change, stale),
    )


@dataclass(frozen=True)
class UnlinkedRow:
    key: str
    check: str
    name: str
    files: str
    size: str
    modified: str
    version: str

    def values(self) -> tuple[str, ...]:
        return (self.check, self.name, self.files, self.size, self.modified, self.version)


def unlinked_row(item: UnlinkedSaveDir, selection: Selection) -> UnlinkedRow:
    key = unlinked_key(item)
    return UnlinkedRow(
        key=key,
        check=CHECKED if selection.unlinked.get(key) else UNCHECKED,
        name=item.name,
        files=str(item.file_count),
        size=size_text(item.total_bytes),
        modified=local_time(item.last_modified),
        version=item.version_hint or "",
    )


def scan_summary(report: ScanReport) -> str:
    counts = report.summary()
    if not report.games and not report.unlinked:
        if report.skipped and all(s.get("reason") == "excluded" for s in report.skipped):
            return (  # D66 8
                _(
                    "扫描完成：没有找到游戏——找到的文件夹全部被“扫描时跳过的文件夹”跳过了，"
                    "请在“设置…”里检查那个列表。"
                )
            )
        return _("扫描完成：没有找到游戏，也没有孤立的全局存档目录。")
    parts = [f"{counts[s.value]} {s.value}" for s in GameStatus if s is not GameStatus.UNLINKED]
    return _(
        "{n} 款游戏（{v}）；跳过 {n2}，警告 {n3}",
        n=len(report.games),
        v=_("，").join(parts),
        n2=len(report.skipped),
        n3=len(report.warnings),
    )


def skip_summary(skipped: Sequence[Mapping[str, Any]], examples: int = 3) -> str | None:
    """The skipped folders in one line (D34): how many per reason and the first few; the full
    list is in 「查看跳过的目录」 and the scan report, not in the log."""
    if not skipped:
        return None
    counts = Counter(str(item["reason"]) for item in skipped)
    reasons = _("、").join(f"{reason} {count}" for reason, count in counts.most_common())
    shown = _("；").join(str(item["path"]) for item in skipped[:examples])
    first = _("（前 {examples} 个）", examples=examples) if len(skipped) > examples else ""
    return _(
        "跳过 {n} 个目录：{reasons}；示例：{shown}{first}",
        n=len(skipped),
        reasons=reasons,
        shown=shown,
        first=first,
    )


def scan_time_text(timing: Mapping[str, Any]) -> str | None:
    """``扫描用时 12.3 秒（D:\\Games 10.1 秒）`` (D34)."""
    if not timing:
        return None
    roots = _("；").join(
        _("{v} {v2:.1f} 秒", v=r["root"], v2=r["seconds"]) for r in timing.get("roots", [])
    )
    return _("扫描用时 {v:.1f} 秒", v=timing["total_seconds"]) + (
        _("（{roots}）", roots=roots) if roots else ""
    )


def skipped_detail_lines(report: ScanReport) -> list[tuple[str, str | None]]:
    """Every skipped folder by reason, each with 「打开」 (the details 「查看跳过的目录」)."""
    lines: list[tuple[str, str | None]] = []
    by_reason: dict[str, list[str]] = {}
    for item in report.skipped:
        by_reason.setdefault(str(item["reason"]), []).append(str(item["path"]))
    for reason, paths in sorted(by_reason.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        lines.append((_("{reason}（{n} 个）", reason=reason, n=len(paths)), None))
        lines.extend((f"  {path}", path) for path in paths)
        lines.append(("", None))
    slowest = report.timing.get("slowest", []) if report.timing else []
    if slowest:
        lines.append((_("最慢的目录（只算它自己的列目录与识别，不含子目录）："), None))
        lines.extend(
            (_("  {v:.1f} 秒  {v2}", v=s["seconds"], v2=s["path"]), s["path"]) for s in slowest
        )
    return lines


def game_details(game: DetectedGame, record_folder: Path | None = None) -> str:
    """Everything the scan knows about one game, for the details dialog."""
    return "\n".join(text for text, _path in game_detail_lines(game, record_folder))


def game_detail_lines(
    game: DetectedGame, record_folder: Path | None = None
) -> list[tuple[str, str | None]]:
    """The details line by line, each with the folder it names (D36: 「打开」 beside it);
    the library's record by its folder (D67 7) when ``record_folder`` is given."""
    identity = game.identity
    lines: list[tuple[str, str | None]] = []

    def add(text: str, folder: str | None = None) -> None:
        lines.append((text, folder or None))

    add(_("名称：{display_name}", display_name=game.display_name))
    add(
        _(
            "状态：{value}    元数据：{value2}",
            value=game.status.value,
            value2=game.metadata_state.value,
        )
    )
    add(_("游戏目录：{game_root}", game_root=game.game_root), game.game_root)
    add(_("引擎：{v} {v2}", v=engine_label(game.engine), v2=game.engine_version or "").rstrip())
    if game.engine == "renpy":
        add(_("save_directory：{v}", v=identity.renpy_save_directory or "<none>"))
        add(
            _(
                "config.name：{v}    build.name：{v2}",
                v=identity.config_name or "",
                v2=identity.build_name or "",
            )
        )
        add(_("config.version：{v}", v=identity.config_version or ""))
    else:  # 0.10.0 (D65 3): the fields the engine's identity comes from
        for label, value in (
            (_("标题"), identity.title),
            (_("公司"), identity.company),
            (_("产品"), identity.product),
            (_("项目"), identity.project),
            (_("身份键"), identity.engine_key),
        ):
            if value:
                add(_("{label}：{value}", label=label, value=value))
    add(
        _(
            "启动程序：{v}    游戏目录名：{root_dir_name}",
            v=identity.exe_basename or "",
            root_dir_name=identity.root_dir_name,
        )
    )
    if game.known_game_id and record_folder is not None:  # D67 7: its folder, not its id
        add(_("备份库记录：games\\{name}", name=record_folder.name), str(record_folder))
    else:
        add(_("备份库记录：{v}", v=game.known_game_id or _("无")))
    for install in game.installs:
        where = install.get("game_root")
        add(
            _("本次扫描的游戏目录：{where}  ({v})", where=where, v=install.get("config_version")),
            where,
        )
    add("")
    add(_("存档来源："))
    for source in game.sources:
        add(f"  {source.source_id}  [{source.status.value}]  {source.kind.value}")
        registry = source.kind is SourceKind.REGISTRY  # a registry key: nothing to open
        add(_("      路径：{path}", path=source.path), None if registry else source.path)
        add(
            _(
                "      {file_count} 个文件，{v}；规则：{method}；{value}: {relative_root}",
                file_count=source.file_count,
                v=size_text(source.total_bytes),
                method=source.method,
                value=source.root_token.value,
                relative_root=source.relative_root,
            )
        )
        if source.patterns:
            add(
                _(
                    "      只取：{v}（文件夹里直接放的、名字相符的文件）",
                    v=_("、").join(source.patterns),
                )
            )
        if source.excludes:
            add(_("      不取：{v}", v=_("、").join(source.excludes)))
        if source.mirror_of:
            add(_("      镜像：{v}", v=", ".join(source.mirror_of)))
        for note in source.notes:
            add(_("      备注：{note}", note=note))
        for e in source.evidence:
            add(
                _(
                    "      证据：{file} ({method}) {detail}",
                    file=e.file,
                    method=e.method,
                    detail=e.detail,
                )
            )
    add("")
    add(_("证据："))
    for evidence in game.evidence:
        where = f"{evidence.file}:{evidence.line}" if evidence.line else evidence.file
        add(f"  {where}  {evidence.method}  {evidence.detail}")
    add("")
    add(_("警告："))
    for warning in game.warnings or [_("无")]:
        add(f"  - {warning}")
    if game.link_suggestions:
        add("")
        add(_("关联建议（点「确认 / 关联…」选择）："))
        for suggestion in game.link_suggestions:
            add(
                _(
                    "  - {v}（{v2}）",
                    v=suggestion["game_id"],
                    v2=suggestion_reason(suggestion["reason"]),
                )
            )
    return lines


SUGGESTION_REASONS = {  # 6.6: why a record is offered to a game that was not linked
    "shared_save_directory": "存档目录相同，可能是共用存档目录的另一款游戏",  # D48 2
    "config_name_match": "config.name 相同",
    "dir_name_match": "目录名或启动程序名相同",
    "unlinked_save_directory_match": "存档目录与这条孤立目录记录相同",
}


def suggestion_reason(reason: str) -> str:
    return _(SUGGESTION_REASONS.get(reason, reason))


# ---------------------------------------------------------------------------- backup


def plan_totals(plan: BackupPlan) -> tuple[int, int, int]:
    """(games, files, bytes) of a backup plan."""
    files = sum(len(ps.files) for g in plan.games for ps in g.sources)
    size = sum(ps.total_bytes for g in plan.games for ps in g.sources)
    return len(plan.games), files, size


def plan_warnings(plan: BackupPlan) -> list[str]:
    warnings = []
    if plan.repo_state == "new":
        warnings.append(_("将新建备份库：{dest}", dest=plan.dest))
    for planned in plan.games:
        if planned.game is not None:
            warnings.extend(
                _("{display_name}：{w}", display_name=planned.display_name, w=w)
                for w in planned.game.warnings
            )
        statuses = {ps.source.status for ps in planned.sources}
        if SourceStatus.CANDIDATE in statuses:
            warnings.append(
                _(
                    "{display_name}：包含待确认（CANDIDATE）的来源",
                    display_name=planned.display_name,
                )
            )
        if any(ps.source.kind is SourceKind.MACHINE_TOKENS for ps in planned.sources):
            warnings.append(
                _(
                    "{display_name}：包含 tokens（Ren'Py 存档签名私钥）",
                    display_name=planned.display_name,
                )
            )
        errors = sum(len(ps.walk_errors) for ps in planned.sources)
        if errors:
            warnings.append(
                _(
                    "{display_name}：{errors} 个文件或目录读不出",
                    display_name=planned.display_name,
                    errors=errors,
                )
            )
    warnings.extend(_("跳过 {v}：{v2}", v=s["name"], v2=s["reason"]) for s in plan.skipped)
    return warnings


CANCELLED_BACKUP = "已取消：列出的项已经完整备份；正在复制的那一项没有留下快照，后面的没有开始。"


def cancelled_backup_text(done: int, planned: int | None) -> str:
    """D58, the log line: ``已取消备份：完成 2 项，未做 3 项（…）``."""
    rest = "" if planned is None else _("，未做 {v} 项", v=max(planned - done, 0))
    return _(
        "已取消备份：完成 {done} 项{rest}（正在复制的那一项没有留下快照）", done=done, rest=rest
    )


def storage_hint(result: BackupResult) -> str | None:
    if any(r.storage in ("copy", "mixed") for r in result.results):
        return _(
            "备份库所在磁盘不支持硬链接（或单个文件的链接数已到上限），部分或全部文件改为普通复制，"
            "会占用更多空间。"
        )
    return None


def result_counts(result: BackupResult) -> dict[str, int]:
    counts = {k: sum(1 for r in result.results if r.status == k) for k in ("ok", "partial")}
    counts["failed"] = sum(1 for r in result.results if r.status == "failed")
    counts["skipped"] = len(result.skipped)
    return counts
