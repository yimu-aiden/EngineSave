"""The readable layout of a snapshot and of the library (0.11.0, D67 3-5).

A new snapshot (manifest format 3) keeps each source's files where they belong:
``data\\<root token>\\<relative root>\\...`` -- ``data\\GAME_ROOT\\game\\saves\\...``,
``data\\RENPY_SAVES_ROOT\\<save directory>\\...``, ``data\\UNITY_LOCALLOW\\<company>\\<product>``,
``data\\ABSOLUTE\\<drive>\\...``; ``GAME_ROOT``'s ``.`` puts its files right in
``data\\GAME_ROOT``. A second source with the same place (two installs, D9) or one inside
another's goes to ``data\\<token> (<source id>)\\...``. The source's ``data_dir`` in the manifest
says where; a snapshot of format 1 or 2 has its files in ``data\\<source id>``. Everything that
reads or writes a snapshot's files finds them through :func:`snapshot_file_path`.

Each snapshot has a ``README.txt`` (Chinese, then English) saying where each data folder goes
on this computer and listing every file with its size and original modification time, so the
saves can be put back by hand; the library's root has an ``INDEX.txt`` listing the records.
Both are rewritten whole (atomically); a library whose ``INDEX.txt`` cannot be written is only
logged about.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from enginesave import roots
from enginesave.errors import EngineSaveError
from enginesave.fsutil import (
    UnsafePathError,
    atomic_write_text,
    iso_utc,
    ns_from_iso,
    safe_join,
    safe_relative_component,
    split_relative,
    utc_now,
)

log = logging.getLogger("enginesave.layout")

DATA = "data"
README = "README.txt"
INDEX = "INDEX.txt"
READABLE_FORMAT = 3  # the manifest format of the readable layout
PROFILE_PLACES = {  # how a profile folder is written when this computer cannot say
    roots.RENPY_SAVES_ROOT: "%APPDATA%\\RenPy",
    roots.UNITY_LOCALLOW: "%USERPROFILE%\\AppData\\LocalLow",
    roots.LOCALAPPDATA: "%LOCALAPPDATA%",
    roots.APPDATA: "%APPDATA%",
    roots.DOCUMENTS: "%USERPROFILE%\\Documents",
    roots.SAVED_GAMES: "%USERPROFILE%\\Saved Games",
    roots.VIRTUALSTORE: "%LOCALAPPDATA%\\VirtualStore",
}
# auto: 1.2.0 (D73 3), a backup another program started (after the game was closed)
REASONS = {"manual": ("手动备份", "a backup"), "import": ("导入", "an import"),
           "pre_restore": ("恢复前的安全快照", "the safety snapshot before a restore"),
           "auto": ("自动", "an automatic backup")}  # fmt: skip
STATUSES = {"complete": ("完整", "complete"), "partial": ("不完整", "partial")}


# ------------------------------------------------------------------------------- folders


def _place_parts(token: str, relative: str) -> list[str]:
    """The folders below ``data\\<token>`` for a source's ``relative_root``."""
    if token == roots.ABSOLUTE:
        path = PureWindowsPath(relative)
        if not path.drive or ".." in path.parts:
            raise UnsafePathError(f"not a clean absolute path: {relative!r}")
        drive = path.drive
        head = (
            ["UNC", *[p for p in drive.strip("\\").split("\\") if p]]
            if drive.startswith("\\\\")
            else [drive.rstrip(":")]
        )
        parts = [*head, *path.parts[1:]]
        return [safe_relative_component(p) for p in parts]
    if relative in ("", roots.ROOT_ITSELF):
        return []
    return split_relative(relative)


def overlaps(a: str, b: str) -> bool:
    a, b = a.casefold(), b.casefold()
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


MAX_NUMBERED = 1000  # a snapshot holds a handful of sources: more is a damaged manifest


def checked_source_id(value: Any) -> str:
    """A source id as one folder name every reader accepts: a string, no separator, no
    character Windows forbids (``*``, ``?``...), no device name, no trailing dot or space
    (1.0.0, D72 R08 R10: an id from an archive is not trusted). Raises UnsafePathError."""
    if not isinstance(value, str):
        raise UnsafePathError(f"a source id is not a string: {value!r}")
    try:
        parts = split_relative(safe_relative_component(value))
    except UnsafePathError as exc:
        raise UnsafePathError(f"unsafe source id {value!r}: {exc}") from exc
    if len(parts) != 1:
        raise UnsafePathError(f"unsafe source id: {value!r}")
    return value


def data_dir_for(source: Mapping[str, Any], taken: list[str]) -> str:
    """D67 3: where a source's files go in a new snapshot, relative to it with ``/``; the
    folders ``taken`` by the snapshot's earlier sources are left alone (and this one added).
    A source this EngineSave cannot place keeps the 0.10.0 folder ``data/<source id>``."""
    token = str(source.get("root_token"))
    source_id = checked_source_id(source["source_id"])  # D72 R08: a bad name fails here
    try:
        parts = _place_parts(token, str(source.get("relative_root") or roots.ROOT_ITSELF))
        known = token in roots.TOKENS
    except (UnsafePathError, ValueError):
        parts, known = [], False
    candidates = (
        ["/".join([DATA, token, *parts]), "/".join([DATA, f"{token} ({source_id})", *parts])]
        if known
        else []
    )
    candidates.append(f"{DATA}/{source_id}")

    def usable(candidate: str) -> bool:
        """The rules every reader applies (M45 review: a folder such as ``aux.d`` in an
        absolute path is not one a snapshot can hold), and free."""
        try:
            checked_data_rel(candidate)
        except UnsafePathError:
            return False
        return not any(overlaps(candidate, t) for t in taken)

    chosen = next((c for c in candidates if usable(c)), None)
    n = 1
    while chosen is None:  # every name taken (ids that fold alike): numbered
        n += 1
        if n > MAX_NUMBERED:  # D72 R08: a name that is taken, not one that never fits
            raise UnsafePathError(f"no free folder for source {source_id!r} in the snapshot")
        if usable(candidate := f"{DATA}/{source_id} ({n})"):
            chosen = candidate
    taken.append(chosen)
    return chosen


def readable_sources(sources: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """``sources`` with the ``data_dir`` of the readable layout, in their order."""
    taken: list[str] = []
    return [{**source, "data_dir": data_dir_for(source, taken)} for source in sources]


def checked_data_rel(rel: str) -> str:
    """A manifest's ``data_dir``: ``data/...`` with safe parts only."""
    parts = rel.split("/")
    if len(parts) < 2 or parts[0] != DATA:
        raise UnsafePathError(f"not a data folder of a snapshot: {rel!r}")
    split_relative("/".join(parts[1:]))  # the rules safe_join applies (device names too)
    return rel


def data_dirs(manifest: Mapping[str, Any]) -> dict[str, str]:
    """Each source's folder in the snapshot (``data/...``): its ``data_dir`` in format 3,
    ``data/<source id>`` before. A source whose folder is not safe is left out (its files
    then cannot be found, which a check reports)."""
    readable = manifest.get("format_version") == READABLE_FORMAT
    found: dict[str, str] = {}
    for source in manifest.get("sources") or []:
        if not isinstance(source, Mapping) or not isinstance(source.get("source_id"), str):
            continue
        try:
            source_id = safe_relative_component(source["source_id"])
            rel = source.get("data_dir") if readable else None
            found[source_id] = (
                checked_data_rel(rel) if isinstance(rel, str) else f"{DATA}/{source_id}"
            )
        except UnsafePathError:
            continue
    return found


def source_data_rel(manifest: Mapping[str, Any], source_id: str) -> str:
    """One source's folder in the snapshot; raises :class:`UnsafePathError` when it has
    none (a format-3 manifest that does not list the source)."""
    found = data_dirs(manifest).get(source_id)
    if found is not None:
        return found
    if manifest.get("format_version") == READABLE_FORMAT:
        raise UnsafePathError(f"the manifest gives no data folder for {source_id!r}")
    return f"{DATA}/{safe_relative_component(source_id)}"


def source_data_dir(snapshot_dir: Path, manifest: Mapping[str, Any], source_id: str) -> Path:
    return snapshot_dir.joinpath(*source_data_rel(manifest, source_id).split("/"))


def snapshot_file_path(
    snapshot_dir: Path, manifest: Mapping[str, Any], entry: Mapping[str, Any]
) -> Path:
    """Where a manifest's file lies (D67 3): the one way every reader finds it."""
    folder = source_data_dir(snapshot_dir, manifest, str(entry["source_id"]))
    return safe_join(folder, str(entry["path"]))


def windows(rel: str) -> str:
    return rel.replace("/", "\\")


# -------------------------------------------------------------------------------- README


def _local(iso: object) -> str:
    try:
        moment = datetime.fromtimestamp(ns_from_iso(str(iso)) / 1_000_000_000)
    except (ValueError, OverflowError, OSError):
        return str(iso or "?")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def place(
    source: Mapping[str, Any],
    env: Mapping[str, str] | None,
    *,
    resolve: bool = True,
) -> tuple[str, str]:
    """Where a source's folder goes, ``(Chinese, English)``: ``GAME_ROOT`` as
    ``<游戏目录>\\...``, a profile token as the folder it was backed up from on this computer
    (``resolve``; else as this computer has the token) or as its variable, ``ABSOLUTE`` as the
    path itself."""
    token = str(source.get("root_token"))
    relative = str(source.get("relative_root") or roots.ROOT_ITSELF)
    below = "" if relative == roots.ROOT_ITSELF else "\\" + windows(relative)
    if source.get("kind") == "registry":
        from enginesave import registry

        key = str(source.get("origin_path") or "")
        if not registry.is_key_path(key):  # an anonymized export says only where it came from
            key = "HKEY_CURRENT_USER\\Software\\<公司>\\<产品>"
        return (
            f"注册表 {key}：双击其中的 .reg 文件即可导入（Unity 的设置与存档）",
            f"the registry key {key.replace('<公司>', '<company>').replace('<产品>', '<product>')}"
            ": double-click the .reg file in it to import it",
        )
    if token == roots.GAME_ROOT:
        return f"<游戏目录>{below}", f"<game folder>{below}"
    if token == roots.ABSOLUTE:
        return windows(relative), windows(relative)
    if token in PROFILE_PLACES:
        origin = str(source.get("origin_path") or "")
        if resolve and PureWindowsPath(origin).is_absolute():  # where it was backed up from
            return origin, origin
        base: Path | None = None
        if resolve and env is not None:
            try:
                base, _how = roots.profile_root(token, env)
            except UnsafePathError:
                base = None
        text = f"{base}{below}" if base is not None else f"{PROFILE_PLACES[token]}{below}"
        return text, text
    return (
        f"（这个版本的 EngineSave 不认识的位置 {token}：{relative}）",
        f"(a place this EngineSave does not know: {token} {relative})",
    )


def engine_name(engine: str) -> str:
    from enginesave.engines.labels import engine_label

    return "Ren'Py" if engine == "renpy" else engine_label(engine)


def snapshot_readme(
    game: Mapping[str, Any],
    manifest: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    *,
    resolve: bool = True,
) -> str:
    """D67 4: a snapshot's ``README.txt``, Chinese first, then English: which game, when,
    where each data folder goes on this computer, and every file with its size and original
    modification time. ``resolve`` False writes profile folders as their variables (an
    anonymized export)."""
    env = dict(os.environ) if env is None and resolve else env
    name = str(game.get("display_name") or manifest.get("game_id") or "?")
    engine = engine_name(str(game.get("engine") or "renpy"))
    if game.get("engine_version"):
        engine += f" {game['engine_version']}"
    game_id = str(game.get("game_id") or manifest.get("game_id") or "")
    snapshot_id = str(manifest.get("snapshot_id") or "")
    created = str(manifest.get("created_at") or "")
    reason = REASONS.get(str(manifest.get("reason")), (str(manifest.get("reason")),) * 2)
    status = STATUSES.get(str(manifest.get("status")), (str(manifest.get("status")),) * 2)
    folders = data_dirs(manifest)
    files = [f for f in manifest.get("files") or [] if isinstance(f, Mapping)]
    sources = [s for s in manifest.get("sources") or [] if isinstance(s, Mapping)]
    installs = [
        str(i["game_root"])
        for i in game.get("installs") or []
        if isinstance(i, Mapping) and i.get("game_root") and not i.get("removed_at")
    ]
    rows = []
    for source in sources:
        sid = str(source.get("source_id"))
        count = sum(1 for f in files if f.get("source_id") == sid)
        rows.append(
            (
                windows(folders.get(sid, f"{DATA}/{sid}")),
                *place(source, env, resolve=resolve),
                count,
            )
        )

    def folder_of(entry: Mapping[str, Any]) -> str:
        sid = str(entry.get("source_id"))
        return folders.get(sid, f"{DATA}/{sid}")

    listing = [
        (
            windows(f"{folder_of(f)}/{f.get('path')}"),
            int(f.get("size") or 0),
            _local(f.get("mtime")),
        )
        for f in files
    ]
    zh = [
        "EngineSave 快照说明",
        "==================",
        "",
        f"游戏：{name}",
        f"引擎：{engine}",
        f"记录 ID：{game_id}",
        f"快照：{snapshot_id}（{reason[0]}，{status[0]}）",
        f"备份时间：{_local(created)}（本地时间），{created}（UTC）",
    ]
    if installs:
        zh.append("记录的游戏目录：" + "；".join(installs))
    zh += [
        "",
        "这个文件夹里 data 下的文件是真实的存档文件，直接复制即可。复制后文件的修改时间可能变成"
        "复制的时间；原来的修改时间以本文件为准。放回之前先退出游戏，并把那里现有的存档另存一份。",
        "",
        "各文件夹放回哪里",
        "----------------",
    ]
    zh += [f"{folder}\\  →  {where}（{count} 个文件）" for folder, where, _en, count in rows]
    zh += ["", f"文件（{len(listing)} 个：大小、原来的修改时间）", "------------------------------"]
    zh += [f"{path}    {size:,} 字节    {mtime}" for path, size, mtime in listing]
    en = [
        "EngineSave snapshot",
        "==================",
        "",
        f"Game:      {name}",
        f"Engine:    {engine}",
        f"Record ID: {game_id}",
        f"Snapshot:  {snapshot_id} ({reason[1]}, {status[1]})",
        f"Made:      {_local(created)} (local time), {created} (UTC)",
    ]
    if installs:
        en.append("Game folder(s) on record: " + "; ".join(installs))
    en += [
        "",
        "The files below data are the real save files: copy them back as they are. A copy may",
        "get the time of copying; the original modification times are listed here. Quit the",
        "game first, and keep a copy of the saves that are there now.",
        "",
        "Where each folder goes",
        "----------------------",
    ]
    en += [f"{folder}\\  ->  {where} ({count} files)" for folder, _zh, where, count in rows]
    en += ["", f"Files ({len(listing)}: size, original modification time)", "-" * 42]
    en += [f"{path}    {size:,} bytes    {mtime}" for path, size, mtime in listing]
    return "\r\n".join([*zh, "", "", *en]) + "\r\n"


def write_readme(snapshot_dir: Path, text: str) -> None:
    """The README of a snapshot (UTF-8 with a BOM, so that every editor reads it right)."""
    atomic_write_text(snapshot_dir / README, "﻿" + text)


# --------------------------------------------------------------------------------- INDEX


def index_text(repo: Any, env: Mapping[str, str] | None = None) -> str:
    """D67 5: the library's ``INDEX.txt``, Chinese first, then English: every record by name
    -- engine, folder under ``games\\``, snapshots, latest backup (local time), game folder(s)
    and save folders."""

    env = dict(os.environ) if env is None else env
    catalog = repo.load_catalog()
    entries = []
    for record in catalog["games"]:
        try:
            entries.append(_index_entry(repo, record, env))
        except Exception as exc:  # noqa: BLE001 - D70 6: one damaged record, not the index
            log.warning("INDEX.txt: record %s skipped: %s", record.get("game_id"), exc)
    entries.sort(key=lambda e: (e["name"].casefold(), e["folder"].casefold()))

    now = _local(iso_utc(utc_now()))
    zh = [
        "EngineSave 备份库索引",
        "====================",
        "",
        f"备份库：{repo.root}",
        f"更新于：{now}（本地时间），共 {len(entries)} 条记录，按名称排序",
        "每条记录的文件夹在 games 下；每个快照文件夹里的 README.txt 说明怎样把存档放回去。",
        "",
    ]
    en = [
        "EngineSave library index",
        "=======================",
        "",
        f"Library: {repo.root}",
        f"Updated: {now} (local time), {len(entries)} records by name",
        "Each record's folder is below games; the README.txt in each snapshot folder says how",
        "to put the saves back.",
        "",
    ]
    for e in entries:
        kind_zh = "（孤立存档目录）" if e["kind"] == "unlinked" else ""
        kind_en = " (orphaned save folder)" if e["kind"] == "unlinked" else ""
        zh += [
            f"{e['name']}{kind_zh}",
            f"  引擎：{e['engine']}    文件夹：games\\{e['folder']}",
            f"  快照：{e['snapshots']} 个" + (f"，最近一次 {e['latest']}" if e["latest"] else ""),
        ]
        zh += [f"  游戏目录：{p}{'' if here else '（不在）'}" for p, here in e["installs"]]
        zh += [f"  存档目录：{p}" for p in e["saves"]]
        zh.append("")
        en += [
            f"{e['name']}{kind_en}",
            f"  Engine: {e['engine']}    Folder: games\\{e['folder']}",
            f"  Snapshots: {e['snapshots']}" + (f", latest {e['latest']}" if e["latest"] else ""),
        ]
        en += [f"  Game folder: {p}{'' if here else ' (not there)'}" for p, here in e["installs"]]
        en += [f"  Save folder: {p}" for p in e["saves"]]
        en.append("")
    return "\r\n".join([*zh, "", *en]) + "\r\n"


def _save_places(
    repo: Any,
    game_id: str,
    latest: str | None,
    doc: Mapping[str, Any],
    env: Mapping[str, str],
    installed: bool,
) -> list[str]:
    """Where a record's saves are outside its game folder (M46 review): the folders its latest
    snapshot came from, else its templates as this computer has the tokens; a source in the
    game folder only when there is no game folder to say it; the registry by its key."""
    sources: list[Mapping[str, Any]] = []
    if latest is not None:
        try:
            sources = [s for s in repo.load_manifest(game_id, latest).get("sources") or []]
        except EngineSaveError:
            sources = []
    sources = sources or [t for t in doc.get("source_templates") or [] if isinstance(t, Mapping)]
    found = []
    for source in sources:
        token = str(source.get("root_token"))
        relative = str(source.get("relative_root") or roots.ROOT_ITSELF)
        origin = str(source.get("origin_path") or "")
        if source.get("kind") == "registry":
            found.append(origin if origin.upper().startswith("HKEY_") else "PlayerPrefs (registry)")
            continue
        if token == roots.GAME_ROOT:
            if not installed:
                below = "" if relative == roots.ROOT_ITSELF else "\\" + windows(relative)
                found.append(f"<game folder>{below}")
            continue
        if PureWindowsPath(origin).is_absolute():
            found.append(origin)
            continue
        try:
            path, _how = roots.resolve(token, relative, env)
        except UnsafePathError:
            path = None
        found.append(str(path) if path is not None else f"{token}\\{windows(relative)}")
    return list(dict.fromkeys(found))


def _index_entry(repo: Any, record: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """One record's lines of ``INDEX.txt``; raises when the record cannot be read."""
    from enginesave.repo import present_installs

    game_id = str(record.get("game_id"))
    try:
        doc = repo.load_game(game_id)
    except EngineSaveError:
        doc = {}
    ids = repo.snapshot_ids(game_id)
    latest = repo.snapshot_created(game_id, ids[-1]) if ids else None
    installs = [str(i["game_root"]) for i in doc.get("installs") or [] if i.get("game_root")]
    present = {str(p) for p in present_installs(doc)}
    saves = _save_places(repo, game_id, ids[-1] if ids else None, doc, env, bool(installs))
    return {
        "name": str(record.get("display_name") or game_id),
        "engine": engine_name(str(record.get("engine") or "renpy")),
        "kind": record.get("kind", "game"),
        "folder": repo.folder_name(game_id),
        "snapshots": len(ids),
        "latest": _local(latest) if latest else "",
        "installs": [(p, p in present) for p in installs],
        "saves": list(dict.fromkeys(saves)),
    }


def write_index(repo: Any, env: Mapping[str, str] | None = None) -> bool:
    """Rewrite the library's ``INDEX.txt`` (D67 5); a failure is only logged. A library of
    format 1 gets none (0.10.0 would not keep it current)."""
    try:
        if repo.old_format:
            return False
        atomic_write_text(repo.root / INDEX, "﻿" + index_text(repo, env))
    except Exception as exc:  # noqa: BLE001 - D70 6: the operation it follows succeeded
        log.warning("INDEX.txt of %s not written: %s", repo.root, exc)
        return False
    return True


def missing_readmes(repo: Any) -> Iterable[tuple[str, str]]:
    """``(record id, snapshot id)`` of every snapshot without a ``README.txt``."""
    for record in repo.load_catalog()["games"]:
        game_id = record.get("game_id")
        if not isinstance(game_id, str):
            continue
        try:
            ids = repo.snapshot_ids(game_id)
        except EngineSaveError:
            continue
        for snapshot_id in ids:
            if not (repo.snapshot_dir(game_id, snapshot_id) / README).is_file():
                yield game_id, snapshot_id
