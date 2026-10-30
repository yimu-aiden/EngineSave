"""The folders a right-click can open or copy (D36)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from enginesave.gui import i18n
from enginesave.gui.i18n import _
from enginesave.models import DetectedGame, SourceKind, UnlinkedSaveDir
from enginesave.repo import Repo

# ------------------------------------------------------------------ quick folder access (D36)


@dataclass(frozen=True)
class Folder:
    """A folder a row's right-click menu or a dialog's 「打开」 offers (D36)."""

    label: str
    path: Path
    exists: bool
    note: str = ""  # why it cannot be opened: 不存在

    @property
    def open_label(self) -> str:
        label = (
            self.label[:1].lower() + self.label[1:] if i18n.language() == i18n.EN else self.label
        )
        return _("打开{label}", label=label) + (
            _("（{note}）", note=self.note) if self.note else ""
        )


def folder(label: str, path: str | Path, note: str = "") -> Folder:
    where = Path(path)
    exists = where.is_dir()
    return Folder(label, where, exists, note or ("" if exists else _("不存在")))


def game_folders(game: DetectedGame) -> list[Folder]:
    """A scanned game: its folder, its global and its local save folders."""
    entries = [folder(_("游戏目录"), game.game_root)]
    for kind, label in (
        (SourceKind.GLOBAL, _("全局存档目录")),
        (SourceKind.LOCAL, _("本地存档目录")),
    ):
        sources = [s for s in game.sources if s.kind is kind]
        for source in sources:
            name = (
                _("{label}（{name}）", label=label, name=Path(source.path).name)
                if len(sources) > 1
                else label
            )
            entries.append(folder(name, source.path))
    return entries


def unlinked_folders(item: UnlinkedSaveDir) -> list[Folder]:
    return [folder(_("孤立存档目录"), item.path)]


def record_folders(
    repo: Repo,
    doc: Mapping[str, Any],
    *,
    saves_root_override: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[Folder]:
    """A library record: its folder in the library first (the default, what a double click
    opens), every install (greyed 「（不存在）」 when it is not there, cleaned up or not: D51)
    and its global or orphaned save folder."""
    entries = [folder(_("备份记录文件夹"), repo.game_dir(str(doc["game_id"])))]
    installs = [i for i in doc.get("installs", []) if i.get("game_root")]
    for install in installs:
        where = Path(install["game_root"])
        label = _("游戏目录（{name}）", name=where.name) if len(installs) > 1 else _("游戏目录")
        entries.append(folder(label, where))
    from enginesave.cleanup import save_folder

    saves = save_folder(doc, saves_root_override=saves_root_override, env=env)
    if saves is not None:
        label = _("孤立存档目录") if doc.get("kind") == "unlinked" else _("全局存档目录")
        entries.append(folder(label, saves))
    return entries
