"""Formatting shared by every part of the window, the check boxes and what an action applies
to (D33)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from datetime import UTC, datetime

from enginesave.fsutil import ns_from_iso, path_key
from enginesave.gui.i18n import _
from enginesave.models import DetectedGame, GameStatus, ScanReport, UnlinkedSaveDir
from enginesave.repo import parse_snapshot_id
from enginesave.report import human_size

CHECKED = "☑"
UNCHECKED = "☐"
DEFAULT_CHECKED = frozenset({GameStatus.CONFIRMED})
NO_VALUE = "—"
CANCEL = "取消"  # D58: the main window's and each dialog's button
CANCELLING = "正在取消…"
LOCAL_ONLY_MARK = "save_directory not defined"


# ---------------------------------------------------------------------------- formatting


def size_text(size: int) -> str:
    return human_size(size)


def local_time(iso: str | None) -> str:
    """``2026-10-24T01:27:55.123Z`` → local ``2026-10-24 09:27``; other text unchanged."""
    if not iso:
        return ""
    try:
        moment = datetime.fromtimestamp(ns_from_iso(iso) / 1_000_000_000)
    except (ValueError, OverflowError, OSError):
        return iso
    return moment.strftime("%Y-%m-%d %H:%M")


def snapshot_time(snapshot_id: str) -> str:
    """Local time of a snapshot id: an old one (``20261024T012755Z-1`` → ``2026-10-24 09:27:55
    (-1)``) and a new one, which is local time already (``2026-10-24 09-27-55 (2)`` →
    ``2026-10-24 09:27:55 (2)``, D67 2)."""
    parsed = parse_snapshot_id(snapshot_id)
    if parsed is None:
        return snapshot_id
    base, number, utc = parsed
    if not utc:
        day, clock = base.split(" ")
        text = f"{day} {clock.replace('-', ':')}"
        return f"{text} ({number})" if number > 1 else text
    moment = datetime.strptime(base, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    text = moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return f"{text} (-{number})" if number else text


# ---------------------------------------------------------------------------- scan results


def game_key(game: DetectedGame) -> str:
    return path_key(game.game_root)


def unlinked_key(item: UnlinkedSaveDir) -> str:
    return item.name.casefold()


def default_checked(game: DetectedGame) -> bool:
    """Only CONFIRMED games start checked (CANDIDATE, CONFLICT, NOT_FOUND... do not)."""
    return game.status in DEFAULT_CHECKED


def checkable(game: DetectedGame) -> bool:
    return game.status is not GameStatus.UNSUPPORTED


class Selection:
    """Check boxes of one scan: games keyed by install folder, orphaned folders by name."""

    def __init__(self) -> None:
        self.games: dict[str, bool] = {}
        self.unlinked: dict[str, bool] = {}
        self.locked: set[str] = set()

    def load(self, report: ScanReport, unchanged: Collection[str] = ()) -> None:
        """Default check boxes for a new scan result: CONFIRMED games, except those backed up
        before and without new saves since (``unchanged`` keys, D54 ④)."""
        self.games = {
            game_key(g): default_checked(g) and game_key(g) not in unchanged for g in report.games
        }
        self.unlinked = {unlinked_key(u): False for u in report.unlinked}
        self.locked = {game_key(g) for g in report.games if not checkable(g)}

    def toggle_game(self, key: str) -> bool:
        if key in self.games and key not in self.locked:
            self.games[key] = not self.games[key]
        return self.games.get(key, False)

    def replace(self, old: DetectedGame, new: DetectedGame, unchanged: bool = False) -> None:
        """A game scanned again after 「确认 / 关联…」 (D48 3): ticked if it was, or if it can
        be backed up now (CONFIRMED) and has new saves (D54)."""
        was = self.games.pop(game_key(old), False)
        self.locked.discard(game_key(old))
        key = game_key(new)
        if checkable(new):
            self.games[key] = was or (default_checked(new) and not unchanged)
        else:
            self.games[key] = False
            self.locked.add(key)

    def toggle_unlinked(self, key: str) -> bool:
        if key in self.unlinked:
            self.unlinked[key] = not self.unlinked[key]
        return self.unlinked.get(key, False)

    def tickable(self, shown: Collection[str] | None = None) -> list[str]:
        """The games the tick button works on: every one that can be ticked, or with
        ``shown`` those the search box shows (D46)."""
        return [k for k in self.games if k not in self.locked and (shown is None or k in shown)]

    def check_all(self, shown: Collection[str] | None = None) -> None:
        """「全选」 (D33 ③): every game that can be ticked (only the shown ones while the
        search box hides some; hidden ticks stay as they are). Since 0.7.0 (D42) the orphaned
        folders are not on the scan page and have their own button in their dialog."""
        self.games.update(dict.fromkeys(self.tickable(shown), True))

    def clear(self, shown: Collection[str] | None = None) -> None:
        self.games.update(dict.fromkeys(self.tickable(shown), False))

    def all_checked(self, shown: Collection[str] | None = None) -> bool:
        games = [self.games[key] for key in self.tickable(shown)]
        return bool(games) and all(games)

    def toggle_all(self, shown: Collection[str] | None = None) -> None:
        """The one tick button: 全不选 once every game (shown) is ticked, 全选 otherwise."""
        if self.all_checked(shown):
            self.clear(shown)
        else:
            self.check_all(shown)

    def all_unlinked_checked(self) -> bool:
        return bool(self.unlinked) and all(self.unlinked.values())

    def toggle_all_unlinked(self) -> None:
        """The orphaned-folders dialog's one tick button (D42)."""
        ticked = not self.all_unlinked_checked()
        self.unlinked = dict.fromkeys(self.unlinked, ticked)

    def checked_roots(self, report: ScanReport) -> list[str]:
        return [g.game_root for g in report.games if self.games.get(game_key(g))]

    def checked_unlinked(self, report: ScanReport) -> list[str]:
        return [u.name for u in report.unlinked if self.unlinked.get(unlinked_key(u))]

    def counts(self) -> tuple[int, int]:
        return sum(self.games.values()), sum(self.unlinked.values())


def distinct_names(
    entries: Sequence[tuple[str, str, str | None] | tuple[str, str, str | None, str | None]],
) -> dict[str, str]:
    """``{game_id: name}`` for ``(game_id, name, version[, folder])``: records that share a
    name show ``名称 · 版本`` (D48 2, two seasons without ``config.name``), or, when the version
    does not tell them apart either, their folder in the library (D67 7: ``games\\<名称 (…)>``,
    unique) or ``名称 · <id 前 8 位>``."""
    rows = [(e[0], e[1], e[2], e[3] if len(e) > 3 else None) for e in entries]
    names = Counter(name for _id, name, _version, _folder in rows)
    labels = Counter((name, version) for _id, name, version, _folder in rows)
    shown = {}
    for game_id, name, version, folder in rows:
        if names[name] == 1:
            shown[game_id] = name
        elif version and labels[(name, version)] == 1:
            shown[game_id] = f"{name} · {version}"
        elif folder and folder != game_id:
            shown[game_id] = f"{name} · games\\{folder}"
        else:
            shown[game_id] = f"{name} · {game_id[:8]}"
    return shown


def checked_text(games: int, unlinked: int, hidden: int = 0, folded: int = 0) -> str:
    """``已勾选 2 款游戏、1 个孤立目录``: the orphaned folders ticked in their dialog are backed
    up with the games since 0.8.0 (D42); ``hidden`` games are ticked but filtered out (D46),
    ``folded`` ones ticked in a closed group (D61 ②)."""
    where = [
        _("{hidden} 款被筛选隐藏", hidden=hidden) if hidden else "",
        _("{folded} 款在收起的组里", folded=folded) if folded else "",
    ]
    where = [w for w in where if w]
    text = _("已勾选 {games} 款游戏", games=games) + (
        _("（其中 {v}）", v=_("、").join(where)) if where else ""
    )
    return text + (_("、{unlinked} 个孤立目录", unlinked=unlinked) if unlinked else "")


def matches(query: str, fields: Sequence[str | None]) -> bool:
    """The search box (D46): every field is searched case-insensitively for ``query`` as a
    substring; an empty query matches everything."""
    wanted = query.strip().casefold()
    return not wanted or any(wanted in str(f).casefold() for f in fields if f)


def toggle_all_text(everything_ticked: bool) -> str:
    """The one tick button of a table (D33 ③): 全不选 once everything is ticked, 全选
    otherwise, also when only some rows are."""
    return _("全不选") if everything_ticked else _("全选")


def action_targets(ticked: Sequence[str], selected: str | None) -> list[str]:
    """What a library action works on (D33 ②): the ticked records, else the selected one;
    nothing (the button is unavailable) when neither."""
    if ticked:
        return list(ticked)
    return [selected] if selected else []
