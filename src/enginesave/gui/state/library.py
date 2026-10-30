"""The "library" page: records in two groups, 「本体存在」 and 「本体不在」 (D43, named so since
0.9.0, D51), the search box (D46), and the snapshots of the selected record."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from enginesave.errors import EngineSaveError
from enginesave.gui.i18n import _
from enginesave.gui.state.common import (
    NO_VALUE,
    distinct_names,
    matches,
    size_text,
    snapshot_time,
)
from enginesave.repo import Repo, install_present

INSTALLED_GROUP = "本体存在"
GONE_GROUP = "本体不在"  # D51: whether EngineSave cleaned it up or not changes nothing
STOPPED = "已取消"  # D58: the verify table's last row when it was stopped
NOT_VERIFIED = "其余的快照没有校验"
NO_MATCH = "无匹配"
SEARCH_PLACEHOLDER = "搜索游戏…"
# D47, D51: the 「类型」 column in Chinese, the kind of record and nothing else
KIND_TEXTS = {"game": "游戏", "unlinked": "孤立目录"}
WIDEST_KIND = KIND_TEXTS["unlinked"]  # the 「类型」 column fits this

# ---------------------------------------------------------------------------- library


@dataclass(frozen=True)
class RecordRow:
    game_id: str
    name: str
    kind: str
    snapshots: int
    latest: str
    installed: bool = False  # D43: one of its install folders is on disk now
    search: tuple[str, ...] = ()  # name, save directory, install folder names, id (D46)

    def values(self) -> tuple[str, ...]:
        kind = _(KIND_TEXTS.get(self.kind, self.kind))
        return (self.name, kind, str(self.snapshots), self.latest)


@dataclass(frozen=True)
class SnapshotRow:
    snapshot_id: str
    time: str
    reason: str
    status: str
    files: str
    size: str
    storage: str

    def values(self) -> tuple[str, ...]:
        return (
            self.snapshot_id,
            self.time,
            self.reason,
            self.status,
            self.files,
            self.size,
            self.storage,
        )


def record_rows(repo: Repo, catalog: dict[str, Any]) -> list[RecordRow]:
    """Every record, with whether one of its install folders is on disk now (each record's
    ``game.json`` is read; on the worker thread). Records that share a name show their
    version too (D48 2)."""
    rows = []
    versions: list[tuple[str, str, str | None]] = []
    for record in catalog["games"]:
        ids = repo.snapshot_ids(record["game_id"])
        try:
            doc = repo.load_game(record["game_id"])
        except EngineSaveError:
            doc = {}
        installs = [str(i["game_root"]) for i in doc.get("installs") or [] if i.get("game_root")]
        installed = install_present(doc)
        identity = record.get("identity") or {}
        save_directory = identity.get("renpy_save_directory") or identity.get("engine_key")
        name = str(record.get("display_name") or record["game_id"])
        versions.append(
            (record["game_id"], name, record_version(doc), repo.folder_name(record["game_id"]))
        )
        rows.append(
            RecordRow(
                game_id=record["game_id"],
                name=name,
                kind=str(record.get("kind", "game")),
                snapshots=len(ids),
                latest=snapshot_time(ids[-1]) if ids else NO_VALUE,
                installed=installed,
                search=(name, str(save_directory or ""), *(Path(p).name for p in installs))
                + (record["game_id"][:8], repo.folder_name(record["game_id"])),  # D67 7
            )
        )
    shown = distinct_names(versions)
    rows = [replace(row, name=shown[row.game_id]) for row in rows]
    return sorted(rows, key=lambda r: r.name.casefold())


def record_version(doc: dict[str, Any]) -> str | None:
    """The game version a record stands for: its identity's, else its newest install's."""
    version = (doc.get("identity") or {}).get("config_version")
    installs = sorted(doc.get("installs") or [], key=lambda i: str(i.get("last_seen") or ""))
    newest = next(
        (i.get("config_version") for i in reversed(installs) if i.get("config_version")), None
    )
    return str(version or newest) if (version or newest) else None


def record_groups(rows: Sequence[RecordRow], query: str = "") -> list[tuple[str, list[RecordRow]]]:
    """The two groups of the library page (D43, D51), each filtered by the search box (D46).
    Whether a missing game directory was cleaned up shows in the cleanup dialog and ``list``."""
    shown = [r for r in rows if matches(query, r.search)]
    return [
        (_(INSTALLED_GROUP), [r for r in shown if r.installed]),
        (_(GONE_GROUP), [r for r in shown if not r.installed]),
    ]


def group_label(name: str, shown: int, total: int, query: str = "") -> str:
    """``本体存在（3）``, or ``本体存在（1/3）`` while the search box hides some."""
    return (
        _("{name}（{shown}/{total}）", name=name, shown=shown, total=total)
        if query.strip()
        else _("{name}（{total}）", name=name, total=total)
    )


def check_count_text(checked: int, hidden: int) -> str:
    """The ticks count all records, also those the search hides (D46)."""
    if hidden:
        return _(
            "已勾选 {checked} 条（其中 {hidden} 项被筛选隐藏）", checked=checked, hidden=hidden
        )
    return _("已勾选 {checked} 条", checked=checked)


def snapshot_rows(repo: Repo, game_id: str) -> list[SnapshotRow]:
    """Newest first; damaged manifests show as ``unreadable``."""
    rows = []
    for snapshot_id in reversed(repo.snapshot_ids(game_id)):
        summary = repo.snapshot_summary(game_id, snapshot_id)
        unreadable = summary["status"] == "unreadable"
        rows.append(
            SnapshotRow(
                snapshot_id=snapshot_id,
                time=snapshot_time(snapshot_id),
                reason="" if unreadable else str(summary.get("reason")),
                status=str(summary["status"]),
                files="" if unreadable else str(summary["file_count"]),
                size="" if unreadable else size_text(summary["total_bytes"]),
                storage="" if unreadable else str(summary.get("storage", "copy (v1)")),
            )
        )
    return rows
