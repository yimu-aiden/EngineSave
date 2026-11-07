"""The restore dialog: options, the install to restore into (D39) and the primary button."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enginesave.fsutil import path_key
from enginesave.gui.i18n import _

if TYPE_CHECKING:
    from enginesave.restore import RestorePlan


def from_install_text(summary: dict[str, Any]) -> str:
    """1.2.0 (D73 14): 「来自 0.4.0」 beside a snapshot, the version of the install it was
    backed up from (its folder's name when no version was read); empty when it does not say."""
    found = summary.get("from_install")
    if not isinstance(found, dict) or not found.get("game_root"):
        return ""
    return _("来自 {v}", v=found.get("game_version") or Path(str(found["game_root"])).name)


def restore_options(
    snapshot_id: str,
    game_root_text: str,
    default_root: Path | None,
    sources: Sequence[str],
    saves_root_override: Path | None,
) -> dict[str, Any]:
    """Keyword arguments for ``plan_restore``.

    The pre-filled install folder is the library's own default; when the user leaves it as is,
    ``game_root`` stays ``None`` so the library applies its rule (that folder for one local
    source, the original installs for several).
    """
    text = game_root_text.strip()
    explicit = Path(text) if text else None
    if (
        explicit is not None
        and default_root is not None
        and path_key(explicit) == path_key(default_root)
    ):
        explicit = None
    return {
        "snapshot": snapshot_id,
        "game_root": explicit,
        "sources": list(sources),
        "saves_root_override": saves_root_override,
    }


def restore_root_hint(candidates: Sequence[tuple[Path, str]]) -> str:
    """The line under the restore dialog's game folder: what is filled in, and why (D39)."""
    from enginesave.restore import SCANNED_INSTALL

    if len(candidates) == 1:
        path, how = candidates[0]
        found = (
            _("扫描找到、还没有备份过的游戏目录")
            if how == SCANNED_INSTALL
            else _("备份库里记录的游戏目录")
        )
        return _("默认是唯一的游戏目录：{path}（{found}）。", path=path, found=found)
    if candidates:
        return _(
            "这款游戏有 {n} 个游戏目录：请在下拉框里选择要放回的那一个，或点“浏览…”。",
            n=len(candidates),
        )
    return _("备份库里记录的游戏目录都不存在，请选择游戏目录（全局存档不需要）。")


def restore_choose_root(candidates: int) -> str:
    """Why the restore preview waits for the user (D32 ①, D39)."""
    if candidates > 1:
        return _(
            "请选择游戏目录：这款游戏有 {candidates} 个游戏目录，"
            "放回哪一个由你选（只恢复全局存档时不需要）。",
            candidates=candidates,
        )
    return _("请选择游戏目录：备份库里记录的游戏目录都不存在（只恢复全局存档时不需要）。")


def restore_notes(plan: RestorePlan, *, version_only: bool = False) -> list[str]:
    """Lines below the restore preview (D39): the game folder used, a wrapper folder that was
    replaced by the game inside it, a game version that differs from the saves', and the
    other games whose saves share the global save directory (D48 1)."""
    notes = []
    if not version_only and plan.game_root is not None:
        notes.append(_("游戏目录：{game_root}", game_root=plan.game_root))
        if plan.given_root is not None:
            notes.append(
                _(
                    "“{given_root}”里面才是游戏目录，存档放进 {game_root}",
                    given_root=plan.given_root,
                    game_root=plan.game_root,
                )
            )
    saved, target = plan.saved_version, plan.target_version
    if saved and target and saved != target:
        notes.append(
            _(
                "注意：存档来自版本 {saved}，目标是版本 {target}，能否读取取决于游戏",
                saved=saved,
                target=target,
            )
        )
    if plan.shared_with:
        others = _("、").join(plan.shared_with)
        notes.append(
            _(
                "注意：全局存档目录与 {others} 共用，恢复会一并覆盖其中 {others2} 的存档",
                others=others,
                others2=others,
            )
        )
    return notes


def restore_primary_text(plan: RestorePlan) -> str:
    """The restore dialog's main button names what pressing it does (D32): the preview's
    overwrite count is the confirmation, and a non-empty target is snapshotted first."""
    files = sum(len(t.files) for t in plan.targets)
    overwrites = sum(t.overwrites for t in plan.targets)
    if overwrites:
        return _("恢复（覆盖 {overwrites} 个文件，先建安全快照）", overwrites=overwrites)
    if any(t.extra_in_target for t in plan.targets):
        return _("恢复（写入 {files} 个文件，先建安全快照）", files=files)
    return _("恢复（写入 {files} 个文件）", files=files)
