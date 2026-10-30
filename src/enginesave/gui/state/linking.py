"""「确认 / 关联…」 on the scan page (0.8.0, D48 3): which library record a game folder belongs
to, and which of its CANDIDATE save folders to confirm, in one dialog instead of the command
line (``enginesave link`` and ``confirm-source`` do the same)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from enginesave.gui.i18n import _
from enginesave.gui.state.common import size_text
from enginesave.gui.state.library import record_version
from enginesave.gui.state.scanning import suggestion_reason
from enginesave.models import DetectedGame, SaveSource, SourceKind, SourceStatus

if TYPE_CHECKING:
    from enginesave.linking import LinkDecision

LINK_TITLE = "确认 / 关联"
LINK_INTRO = (
    "选择这个游戏目录属于哪条备份库记录，并确认勾选的存档来源。"
    "只写备份库的记录，不复制任何文件；之后照常「备份已勾选项」。"
)
NEW_RECORD = "新建记录"
NO_CANDIDATES = "没有待确认的存档来源。"


@dataclass(frozen=True)
class LinkChoice:
    """One radio button: a record, or ``None`` for a new one."""

    game_id: str | None
    label: str


def record_choice_name(record: Mapping[str, Any], doc: Mapping[str, Any] | None) -> str:
    """``名称 · 版本``, so that two seasons with the same name can be told apart (D48 2)."""
    name = str(record.get("display_name") or record.get("game_id"))
    version = record_version(dict(doc)) if doc else None
    return f"{name} · {version}" if version else name


def link_choices(game: DetectedGame, names: Mapping[str, str]) -> tuple[list[LinkChoice], int]:
    """The radio buttons and the one chosen at first: the record the game is linked to now
    (chosen), the suggested records with their reasons, and a new record, chosen when the game
    is not linked yet; a record is never picked by guessing."""
    choices = []
    if game.known_game_id:
        current = names.get(game.known_game_id, game.known_game_id)
        choices.append(
            LinkChoice(game.known_game_id, _("{current}（当前关联的记录）", current=current))
        )
    for suggestion in game.link_suggestions:
        game_id = suggestion["game_id"]
        if game_id != game.known_game_id and all(c.game_id != game_id for c in choices):
            reason = suggestion_reason(suggestion["reason"])
            choices.append(
                LinkChoice(
                    game_id, _("{v}（{reason}）", v=names.get(game_id, game_id), reason=reason)
                )
            )
    choices.append(LinkChoice(None, _(NEW_RECORD)))
    return choices, 0 if game.known_game_id else len(choices) - 1


def candidate_sources(game: DetectedGame) -> list[SaveSource]:
    """The save folders waiting to be confirmed; all of them are ticked at first."""
    return [s for s in game.sources if s.status is SourceStatus.CANDIDATE]


def candidate_reason(source: SaveSource) -> str:
    if source.method == "dir_name_match":
        return _("目录名与游戏名相近")
    if source.kind is SourceKind.EXTRA:
        return _("额外存档目录，里面没有 .save 文件")
    if source.method == "engine_rule":  # 0.10.0: another engine's folder found by name only
        return _("按名字找到的文件夹，引擎的规则定不下来")
    return _("脚本里的存档目录无法确定")


def candidate_values(source: SaveSource, mark: str) -> tuple[str, ...]:
    return (
        mark,
        source.path,
        str(source.file_count),
        size_text(source.total_bytes),
        candidate_reason(source),
    )


def confirm_primary_text(count: int) -> str:
    return _("确认（{count} 个来源）", count=count) if count else _("确认关联")


def link_done_log(game: DetectedGame, decision: LinkDecision, record: str) -> str:
    """The one log line after 「确认 / 关联…」."""
    made = _("新建记录 ") if decision.created else ""
    text = _(
        "已关联：{game_root} → {made}{record}", game_root=game.game_root, made=made, record=record
    )
    if decision.confirmed:
        text += _("；确认了 {n} 个存档来源", n=len(decision.confirmed))
    return text
