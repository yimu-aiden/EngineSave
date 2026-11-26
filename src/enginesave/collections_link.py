"""1.3.0 (D74): what Collections knows of the user's games, read to keep one game in one record.

Collections (the user's game library) keeps every install folder of a game as a *copy* of
one *work*: ``Hero-0.3`` and ``Hero-0.4`` are two copies of the work "Hero". EngineSave
groups folders by their save identity (D9, D48, plan 6.6): a new version whose Ren'Py save
directory or whose title changed would otherwise become a record of its own, and the saves
of one game would be split over two records.

Given a Collections address (``--collections`` or the window's setting), a scan reads the
list of copies once and, for every game it found:

* reports the work in ``game.collections`` (``work_id``, ``title``, ``version``);
* when the game matched no record by the 6.6 rules, and the other copies of its work belong
  to exactly one record (the copy's ``enginesave_id``, a record that lists the copy's folder
  as an install, a ``link_root`` override, or another game of this scan of the same work),
  associates it with that record (``collections.record_by`` = ``"collections"``); several
  records are offered as link suggestions (``collections_same_work``), never chosen;
* when the game matched a record but other copies of its work are in other records, lists
  those in ``collections.other_records`` (one game split over several records).

A backup of a game associated this way pins its folder to the record (``link_root``, as
「确认 / 关联…」 does), so later scans without Collections keep it there. Nothing in
Collections is ever written, and Collections is never required: a source that cannot be read
is a warning (``report.collections.error``), and the scan goes on without it.

The source is an ``http(s)://`` address of a Collections server (``GET
<address>/api/enginesave/copies``, or the address itself when it names a path) or a JSON file
of the same form::

    {"format_version": 1,
     "works": [{"work_id": "...", "title": "...",
                "copies": [{"path": "D:\\\\Games\\\\Hero-0.4", "version": "0.4",
                            "status": "present", "enginesave_id": null}]}]}

Unknown fields are ignored; a copy without an absolute ``path`` is left out.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from enginesave.fsutil import describe_error, path_key
from enginesave.models import DetectedGame

ENDPOINT = "/api/enginesave/copies"
FORMAT_VERSION = 1
TIMEOUT_SECONDS = 5.0
MAX_BYTES = 32 * 1024 * 1024
ENV_VAR = "ENGINESAVE_COLLECTIONS"
MATCH_REASON = "collections_same_work"  # a link suggestion's reason


class CollectionsUnavailable(Exception):
    """The source could not be read or is not a list of copies; the message says why."""


@dataclass(frozen=True)
class Copy:
    work_id: str
    title: str
    path: str
    version: str | None = None
    status: str | None = None
    enginesave_id: str | None = None


@dataclass
class Works:
    """The copies Collections listed, by folder and by work."""

    source: str
    copies: list[Copy] = field(default_factory=list)
    by_path: dict[str, Copy] = field(default_factory=dict)
    by_work: dict[str, list[Copy]] = field(default_factory=dict)

    @classmethod
    def build(cls, source: str, copies: Sequence[Copy]) -> Works:
        works = cls(source)
        for copy in copies:
            works.copies.append(copy)
            works.by_path.setdefault(path_key(copy.path), copy)
            works.by_work.setdefault(copy.work_id, []).append(copy)
        return works

    def summary(self, error: str | None = None) -> dict[str, Any]:
        """``report.collections``: where it was read, how much, or why not."""
        return {
            "source": self.source,
            "works": len(self.by_work),
            "copies": len(self.copies),
            "error": error,
        }

    def copy_at(self, folder: str | Path) -> Copy | None:
        return self.by_path.get(path_key(folder))


def unavailable(source: str, error: str) -> dict[str, Any]:
    return {"source": source, "works": 0, "copies": 0, "error": error}


# ----------------------------------------------------------------------------------- reading


def address(source: str) -> str:
    """The URL a ``http(s)://`` source is read from: ``<server>/api/enginesave/copies``
    unless the source names a path of its own."""
    parsed = urllib.parse.urlsplit(source)
    if parsed.path in ("", "/"):
        return urllib.parse.urlunsplit(parsed._replace(path=ENDPOINT))
    return source


def is_url(source: str) -> bool:
    return source.casefold().startswith(("http://", "https://"))


def _read_url(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise CollectionsUnavailable(
                f"{url}: this Collections does not list its copies for EngineSave yet (404)"
            ) from exc
        raise CollectionsUnavailable(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CollectionsUnavailable(f"{url}: not reachable ({reason})") from exc
    if len(data) > MAX_BYTES:
        raise CollectionsUnavailable(f"{url}: the answer is larger than {MAX_BYTES} bytes")
    return data


def _read_file(path: Path) -> bytes:
    try:
        if path.stat().st_size > MAX_BYTES:
            raise CollectionsUnavailable(f"{path}: larger than {MAX_BYTES} bytes")
        return path.read_bytes()
    except OSError as exc:
        raise CollectionsUnavailable(f"{path}: {describe_error(exc)}") from exc


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _absolute(path: str) -> bool:
    return PureWindowsPath(path).is_absolute() or os.path.isabs(path)


def parse(data: bytes | str, source: str) -> Works:
    """The copies of a Collections answer; :class:`CollectionsUnavailable` when it is not one."""
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CollectionsUnavailable(f"{source}: not JSON ({exc})") from exc
    if not isinstance(doc, Mapping) or not isinstance(doc.get("works"), list):
        raise CollectionsUnavailable(f"{source}: no list of works in the answer")
    version = doc.get("format_version", FORMAT_VERSION)
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise CollectionsUnavailable(
            f"{source}: format {version!r} is newer than this EngineSave reads "
            f"({FORMAT_VERSION}); update EngineSave"
        )
    copies: list[Copy] = []
    for work in doc["works"]:
        if not isinstance(work, Mapping):
            continue
        work_id = _text(work.get("work_id"))
        if work_id is None or not isinstance(work.get("copies"), list):
            continue
        title = _text(work.get("title")) or work_id
        for item in work["copies"]:
            if not isinstance(item, Mapping):
                continue
            path = _text(item.get("path"))
            if path is None or not _absolute(path):
                continue
            copies.append(
                Copy(
                    work_id,
                    title,
                    os.path.normpath(path),
                    _text(item.get("version")),
                    _text(item.get("status")),
                    _text(item.get("enginesave_id")),
                )
            )
    return Works.build(source, copies)


def load(source: str, *, timeout: float = TIMEOUT_SECONDS) -> Works:
    """Read the copies from ``source`` (a Collections address or a JSON file)."""
    if is_url(source):
        url = address(source)
        return parse(_read_url(url, timeout), url)
    path = Path(os.path.abspath(source))
    return parse(_read_file(path), str(path))


def source_from(value: str | None, env: Mapping[str, str] | None = None) -> str | None:
    """The source to read: ``value`` (``--collections``), else ``ENGINESAVE_COLLECTIONS``;
    ``None`` (or ``"off"``) for none."""
    env = os.environ if env is None else env
    chosen = (value if value is not None else env.get(ENV_VAR, "")).strip()
    return None if chosen.casefold() in ("", "off", "none") else chosen


# ---------------------------------------------------------------------------------- matching


_MAKER_PAIR = {"rpgmaker_mv": "electron", "rpgmaker_mz": "electron"}


def _engines_agree(a: str | None, b: str | None) -> bool:
    """The same engine, or an RPG Maker MV/MZ game and its Electron build (D70 7); a record
    that holds only attachments (no engine yet, D77) agrees with any."""
    if not a or not b:
        return True
    a, b = a or "renpy", b or "renpy"
    return a == b or _MAKER_PAIR.get(a) == b or _MAKER_PAIR.get(b) == a


def _install_paths(game: DetectedGame) -> list[str]:
    paths = [game.game_root]
    paths += [str(i["game_root"]) for i in game.installs if i.get("game_root")]
    return list(dict.fromkeys(paths))


def work_of(game: DetectedGame, works: Works) -> tuple[Copy | None, list[str]]:
    """The copy that names this game's work (its ``game_root`` first, then its other
    installs), and every distinct work its folders belong to."""
    found = [c for p in _install_paths(game) if (c := works.copy_at(p)) is not None]
    ids = list(dict.fromkeys(c.work_id for c in found))
    return (found[0] if found else None), ids


def _record_roots(record: Mapping[str, Any]) -> set[str]:
    return {
        path_key(i["game_root"])
        for i in record.get("installs") or []
        if isinstance(i, Mapping) and i.get("game_root")
    }


def records_of_work(
    work_id: str,
    works: Works,
    known: Sequence[Mapping[str, Any]],
    overrides: Sequence[Mapping[str, Any]],
    *,
    skip: Collection[str] = (),
) -> dict[str, list[str]]:
    """The records the copies of ``work_id`` are in, each with why: ``{record id: [how]}``.
    The folders in ``skip`` (the game's own) do not count as installs or links; the record
    Collections remembers for any copy (``enginesave_id``) does."""
    by_id = {str(r.get("game_id")): r for r in known if r.get("kind", "game") == "game"}
    out: dict[str, list[str]] = {}

    def add(record_id: str, how: str) -> None:
        if record_id in by_id and how not in out.setdefault(record_id, []):
            out[record_id].append(how)

    for copy in works.by_work.get(work_id, []):
        if copy.enginesave_id:
            add(copy.enginesave_id, "enginesave_id")
    for record_id, record in by_id.items():  # D77: the record keeps this work
        ids = record.get("collections_work_ids")
        if isinstance(ids, list) and work_id in ids:
            add(record_id, "work_id")
    copies = [c for c in works.by_work.get(work_id, []) if path_key(c.path) not in skip]
    paths = {path_key(c.path) for c in copies}
    for record_id, record in by_id.items():
        if paths & _record_roots(record):
            add(record_id, "install")
    for override in overrides:
        root = override.get("game_root")
        if override.get("type") == "link_root" and root and path_key(str(root)) in paths:
            add(str(override.get("game_id")), "link_root")
    return {k: v for k, v in out.items() if v}


def annotate(
    games: Sequence[DetectedGame],
    works: Works,
    known: Sequence[Mapping[str, Any]] = (),
    overrides: Sequence[Mapping[str, Any]] = (),
) -> int:
    """Fill ``game.collections`` and, for games no 6.6 rule matched, associate them with the
    one record their work's other copies are in (see the module). The number associated."""
    by_id = {str(r.get("game_id")): r for r in known}
    matched_in_scan: dict[str, set[str]] = {}
    pending: list[tuple[DetectedGame, Copy]] = []
    for game in games:
        copy, work_ids = work_of(game, works)
        game.collections = None
        if copy is None:
            continue
        info: dict[str, Any] = {
            "work_id": copy.work_id,
            "title": copy.title,
            "version": copy.version,
            "record_by": None,
            "other_records": [],
        }
        if len(work_ids) > 1:
            info["other_works"] = work_ids[1:]
        game.collections = info
        for install in game.installs:
            if install.get("game_root") and (c := works.copy_at(install["game_root"])):
                install["collections"] = {"work_id": c.work_id, "version": c.version}
        if game.known_game_id is not None:
            matched_in_scan.setdefault(copy.work_id, set()).add(game.known_game_id)
        pending.append((game, copy))
    associated = 0
    for game, copy in pending:
        info = game.collections
        assert info is not None
        own = {path_key(p) for p in _install_paths(game)}
        records = records_of_work(copy.work_id, works, known, overrides, skip=own)
        for record_id in matched_in_scan.get(copy.work_id, set()):
            if record_id != game.known_game_id:
                records.setdefault(record_id, []).append("scan")
        records = {
            rid: how
            for rid, how in records.items()
            if rid in by_id and _engines_agree(by_id[rid].get("engine"), game.engine)
        }
        if game.known_game_id is not None:
            info["other_records"] = sorted(r for r in records if r != game.known_game_id)
            continue
        if len(records) == 1:
            (record_id,) = records
            game.known_game_id = record_id
            game.link_suggestions = []
            info["record_by"] = "collections"
            matched_in_scan.setdefault(copy.work_id, set()).add(record_id)
            associated += 1
            continue
        offered = [{"game_id": rid, "reason": MATCH_REASON} for rid in sorted(records)]
        rest = [s for s in game.link_suggestions if s.get("game_id") not in records]
        game.link_suggestions = [*offered, *rest]
    return associated


def group_key(game: DetectedGame | None) -> str | None:
    """The work a game belongs to, for a backup that creates records: two new games of one
    work go into one new record."""
    info = getattr(game, "collections", None) if game is not None else None
    return info.get("work_id") if isinstance(info, Mapping) else None
