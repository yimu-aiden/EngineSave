"""Game identity: name normalization and association with backup catalog records (plan 6.6)."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from enginesave.engines.common import misread
from enginesave.fsutil import path_key
from enginesave.models import DetectedGame, Evidence, GameIdentity, SourceStatus
from enginesave.repo import game_removed

_DIR_SUFFIX_RE = re.compile(r"(?:-steam|-\d+)$", re.IGNORECASE)
# D66 5: a version at the end of a name, with the test stage or platform after it ("Hero
# v0.3", "PaperLantern-0.8.15r1-pc", "Game 1.2 beta", "魔王物語ver1.02", "魔王物語（ver1.0）");
# a demo or public build. An episode, a plain number and a word alone ("Project Alpha", "Big
# Win") are kept (M44 review)
_SEP = r"[\s._\-(\[（［【\u3000]"
_VERSION = r"(?:v(?:er(?:sion)?)?[\s.]*\d+(?:[._]\d+)*[a-z]?\d*|\d+(?:[._]\d+)+[a-z]?\d*)"
_STAGE = r"(?:(?:beta|alpha|rc)[\s._]*\d*|r\d+|pc|win(?:dows)?|win64|x64)"
_VERSION_TAIL_RE = re.compile(
    rf"(?:(?:{_SEP}+|^|(?<![a-z0-9])){_VERSION}(?:{_SEP}*{_STAGE})*|{_SEP}+(?:demo|public))"
    r"[)\]）］】]?\s*$",
    re.IGNORECASE,
)


def normalize_name(text: str | None) -> str:
    """Drop non-alphanumerics and case-fold: ``"Moonlit Harbor"`` → ``"moonlitharbor"``."""
    return "".join(ch for ch in (text or "").casefold() if ch.isalnum())


def suggestion_key(text: str | None) -> str:
    """A name as link suggestions compare it (D66 5): without the versions, test stages and
    platforms at its end, then as :func:`normalize_name`; the identity key is not changed."""
    name = text or ""
    previous = None
    while previous != name:
        previous, name = name, _VERSION_TAIL_RE.sub("", name)
    return normalize_name(name) or normalize_name(text)


def strip_dir_suffixes(name: str) -> str:
    """Remove trailing ``-<digits>`` and ``-steam`` (repeatedly), as Ren'Py ids often carry them."""
    previous = None
    while previous != name:
        previous, name = name, _DIR_SUFFIX_RE.sub("", name)
    return name


def normalized_dir_name(name: str) -> str:
    return normalize_name(strip_dir_suffixes(name))


def _fold(value: object) -> str:
    return value.casefold() if isinstance(value, str) else ""


def _save_directory(record: dict) -> str:
    return _fold((record.get("identity") or {}).get("renpy_save_directory"))


def match_key(engine: str | None, identity: Mapping[str, Any] | GameIdentity) -> str:
    """What 6.6 rule 1 compares: Ren'Py's save directory; for another engine (0.10.0, D65 3)
    its own key (a title, Unity's company and product, an Unreal project) with the engine,
    so that two engines never match. Empty when there is none."""
    values = identity if isinstance(identity, Mapping) else identity.to_dict()
    if (engine or "renpy") == "renpy":
        return _fold(values.get("renpy_save_directory"))
    key = " ".join(_fold(values.get("engine_key")).split())  # a title: spaces do not count
    return f"{engine}:{key}" if key else ""


def _record_key(record: Mapping[str, Any]) -> str:
    identity = record.get("identity") or {}
    if misread(str(identity.get("engine_key") or "")):  # 0.10.0 kept such keys: they say
        identity = {**identity, "engine_key": None}  # nothing (M44 review)
    return match_key(record.get("engine"), identity)


def legacy_match_key(game: DetectedGame) -> str:
    """The key 0.10.0 gave the game (``legacy_key``, M44 review), or empty."""
    legacy = game.identity.legacy_key
    return match_key(game.engine, {"engine_key": legacy}) if legacy else ""


MAKER_CORES = {"RPG Maker MV core": "rpgmaker_mv", "RPG Maker MZ core": "rpgmaker_mz"}
MAKER_ENGINES = frozenset(MAKER_CORES.values())


def maker_engine(game: DetectedGame) -> str | None:
    """1.0.0 (D70 7): the RPG Maker MV/MZ inside an Electron app (D69 2), from its evidence."""
    if game.engine != "electron":
        return None
    return next((MAKER_CORES[e.detail] for e in game.evidence if e.detail in MAKER_CORES), None)


def secondary_match_key(game: DetectedGame) -> tuple[str, str] | None:
    """``(engine, key)`` an Electron app carrying RPG Maker MV/MZ also tries for rule 1: the
    key the NW.js version of the same game has -- ``rpgmaker_mv:<title>`` -- when its title
    came from ``System.json``."""
    engine = maker_engine(game)
    titled = any(e.method == "engine_title" for e in game.evidence)
    if engine is None or not titled or not game.identity.title:
        return None
    key = match_key(engine, {"engine_key": game.identity.title})
    return (engine, key) if key else None


def game_match_key(game: DetectedGame) -> str:
    return match_key(game.engine, game.identity)


def install_roots(record: dict) -> set[str]:
    return {
        path_key(item["game_root"])
        for item in record.get("installs", [])
        if isinstance(item, dict) and item.get("game_root")
    }


def game_roots(game: DetectedGame) -> set[str]:
    roots = {path_key(game.game_root)}
    roots.update(path_key(i["game_root"]) for i in game.installs if i.get("game_root"))
    return roots


def match_known_game(
    game: DetectedGame,
    known: Sequence[dict],
    overrides: Sequence[dict] = (),
    claimed: Collection[str] = (),
) -> str | None:
    """Catalog game id a scanned game is automatically associated with, or ``None``."""
    found = match_record(
        game_match_key(game),
        game_roots(game),
        known,
        overrides,
        claimed,
        engine=game.engine,
        legacy=legacy_match_key(game),
    )
    secondary = secondary_match_key(game) if found is None else None
    if secondary is not None:  # D70 7: the NW.js version's record, by its own rule 1
        engine, key = secondary
        found = match_record(key, set(), known, overrides, claimed, engine=engine)
    return found


def _linked(roots: set[str], known_ids: set[object], overrides: Sequence[dict]) -> str | None:
    for override in overrides:
        if (
            override.get("type") == "link_root"
            and override.get("game_id") in known_ids
            and override.get("game_root")
            and path_key(override["game_root"]) in roots
        ):
            return str(override["game_id"])
    return None


def match_record(
    key: str | None,
    roots: set[str],
    known: Sequence[dict],
    overrides: Sequence[dict] = (),
    claimed: Collection[str] = (),
    engine: str | None = None,
    legacy: str | None = None,
) -> str | None:
    """6.6 automatic association for a :func:`match_key` and install roots (``path_key``
    form); with ``engine`` (0.10.0) only records of that engine are matched by rules 1-2.
    ``legacy`` (0.11.0) is the key 0.10.0 gave the game: rule 1 tries it when no record has
    the key, and rule 2 takes a record with it as not contradicting.

    Order: an explicit ``link_root`` override for one of the roots (rule 6); an equal key --
    the ``renpy_save_directory``, or for another engine the same engine and ``engine_key``
    (rule 1, D65 3); a record that already lists one of the roots and does not contradict the
    key (rule 2). ``unlinked`` records are never auto-merged. Since 0.8.0 (D48 2) rule 1
    needs the only record with that key, and one that no other game of the same scan has
    ``claimed``: two games can share a save directory.
    """
    linked = _linked(roots, {r.get("game_id") for r in known}, overrides)
    if linked is not None:
        return linked
    records = [
        r
        for r in known
        if r.get("kind", "game") == "game"
        and (engine is None or (r.get("engine") or "renpy") == engine)
    ]
    wanted = key or ""
    for candidate in dict.fromkeys(k for k in (wanted, legacy or "") if k):
        same = [r for r in records if _record_key(r) == candidate]
        if len(same) == 1 and same[0]["game_id"] not in claimed:
            return str(same[0]["game_id"])
        if same:  # more than one, or taken: not by a key at all
            break
    agrees = {"", wanted, legacy or ""}
    for record in records:
        theirs = _record_key(record)
        if roots & install_roots(record) and (not wanted or theirs in agrees):
            return record["game_id"]
    return None


def _identity(record: dict, key: str) -> str:
    return normalize_name((record.get("identity") or {}).get(key))


def _suggestion(record: dict, key: str) -> str:
    return suggestion_key((record.get("identity") or {}).get(key))


# exe names many games share (RPG Maker's and Wolf's Game.exe...): no reason to suggest
GENERIC_EXE_NAMES = frozenset({"game", "gamepro", "nw", "start", "launcher", "play"})


def link_suggestions(game: DetectedGame, known: Sequence[dict]) -> list[dict]:
    """6.6 rules 2-3 for a game that was not linked automatically; never links by itself."""
    suggestions: list[dict] = []

    def add(record: dict, reason: str) -> None:
        if all(s["game_id"] != record["game_id"] for s in suggestions):
            suggestions.append({"game_id": record["game_id"], "reason": reason})

    # D48 2: a game whose save directory other records use too (or that another game of this
    # scan has taken) is not linked by that; every such record is offered, cleaned up or not.
    wanted = game_match_key(game)
    for record in known:
        if record.get("kind", "game") == "game" and wanted and _record_key(record) == wanted:
            add(record, "shared_save_directory")
    # Records whose game was cleaned up get no further suggestions (D26); 0.10.0: only the
    # game's engine's records are offered by name
    engine = game.engine or "renpy"
    # D70 7: the NW.js and the Electron versions of an RPG Maker game are offered to each other
    engines = {engine}
    if (maker := maker_engine(game)) is not None:
        engines.add(maker)
    elif engine in MAKER_ENGINES:
        engines.add("electron")
    records = [
        r
        for r in known
        if r.get("kind", "game") == "game"
        and not game_removed(r)
        and (r.get("engine") or "renpy") in engines
    ]
    name = suggestion_key(game.identity.config_name or game.identity.title)  # D65 3, D66 5
    if name:
        same_name = [
            r for r in records if name in {_suggestion(r, "config_name"), _suggestion(r, "title")}
        ]
        if len(same_name) == 1:
            add(same_name[0], "config_name_match")
    ours = {
        suggestion_key(game.identity.root_dir_name),
        suggestion_key(game.identity.exe_basename),
    } - {"", *GENERIC_EXE_NAMES}
    for record in records:
        theirs = {_suggestion(record, "root_dir_name"), _suggestion(record, "exe_basename")} - {""}
        if ours & theirs:
            add(record, "dir_name_match")
    renpy_dir = _fold(game.identity.renpy_save_directory)
    for record in known:
        if record.get("kind") == "unlinked" and renpy_dir and _save_directory(record) == renpy_dir:
            add(record, "unlinked_save_directory_match")
    return suggestions


def apply_confirmed_sources(game: DetectedGame, overrides: Sequence[dict]) -> None:
    """Promote ``CANDIDATE`` sources the user confirmed (``confirm_source``) to USER_CONFIRMED."""
    for override in overrides:
        if (
            override.get("type") != "confirm_source"
            or override.get("game_id") != game.known_game_id
        ):
            continue
        if not override.get("path"):
            continue
        wanted = path_key(override["path"])
        source = next((s for s in game.sources if path_key(s.path) == wanted), None)
        if source is None:
            game.warnings.append(
                f"user-confirmed source {override['path']} was not found by this scan"
            )
            continue
        if source.status is SourceStatus.CANDIDATE:
            source.status = SourceStatus.USER_CONFIRMED
            detail = f"confirmed as {override.get('source_id')} at {override.get('confirmed_at')}"
            source.evidence.append(Evidence(override["path"], "user_override", detail))


def known_root(game: DetectedGame, known: Sequence[dict], overrides: Sequence[dict]) -> bool:
    """The library already knows this game folder: a ``link_root`` or a record's install."""
    roots = game_roots(game)
    linked = _linked(roots, {r.get("game_id") for r in known}, overrides)
    return linked is not None or any(roots & install_roots(r) for r in known)


def match_known_games(
    games: Sequence[DetectedGame], known: Sequence[dict], overrides: Sequence[dict]
) -> dict[int, str | None]:
    """Each game's record by ``id(game)`` (D48 2): games whose folder the library knows match
    first and claim their record; a later game with the same save directory then does not
    take that record by rule 1."""
    ordered = sorted(games, key=lambda g: not known_root(g, known, overrides))
    claimed: set[str] = set()
    matched: dict[int, str | None] = {}
    for game in ordered:
        matched[id(game)] = match_known_game(game, known, overrides, claimed)
        if matched[id(game)] is not None:
            claimed.add(str(matched[id(game)]))
    return matched


def annotate_known_games(
    games: Sequence[DetectedGame], known: Sequence[dict], overrides: Sequence[dict]
) -> None:
    """Fill ``known_game_id``/``link_suggestions`` and apply ``confirm_source`` overrides."""
    matched = match_known_games(games, known, overrides)
    for game in games:
        game.known_game_id = matched[id(game)]
        if game.known_game_id is None:
            game.link_suggestions = link_suggestions(game, known)
        else:
            apply_confirmed_sources(game, overrides)


def match_unlinked(name: str, known: Sequence[dict]) -> str | None:
    """Record for an orphaned global folder: an ``unlinked`` record first, then a game record."""
    wanted = name.casefold()
    for kind in ("unlinked", "game"):
        for record in known:
            if record.get("kind", "game") == kind and _save_directory(record) == wanted:
                return record["game_id"]
    return None
