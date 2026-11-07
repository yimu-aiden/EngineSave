"""What a scan reports per install folder (1.2.0, D73 6, 7, 13).

Every game of a scan lists its install folders in ``installs`` -- one item for a game found in
one folder, several for a Ren'Py game whose versions were grouped (D9) -- and each item says:

* ``game_root``, ``config_version``, ``engine_version`` (as before);
* ``game_version`` and ``game_version_source``: the game's own version where a file states it
  plainly (Ren'Py's ``config.version``, ``package.json`` ``version`` of an Electron or NW.js
  game, a Unity game's ``bundleVersion`` where it can be read), else ``null``; never guessed
  from a folder name;
* ``launch_candidates``: the programs that start this install, most likely first, as paths
  relative to the install folder (``/`` between folders) with why each is listed. Helper
  programs (crash handlers, uninstallers, setups, redistributables) are never listed.

Local save sources (``kind`` ``local``) carry ``install_root``: the install folder they are in;
every other source has ``null``. Nothing here changes what a game is or which record it
matches: an install item is a report, the identity and the grouping stay as they were.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from enginesave.engines import common
from enginesave.fsutil import is_within, path_key
from enginesave.models import DetectedGame, SourceKind

# beyond common.HELPER_EXES / HELPER_NAMES: never a game's own program
EXTRA_HELPERS = ("*crash*", "dxsetup*", "vcredist*", "ue4prereqsetup*")

Candidate = tuple[str, str]  # (path relative to the install folder, reason)


def launchable(stem: str) -> bool:
    """An exe that may start a game: not a helper program."""
    lower = stem.casefold()
    return not common.is_helper_exe(stem) and not any(
        fnmatch.fnmatchcase(lower, pattern) for pattern in EXTRA_HELPERS
    )


# ------------------------------------------------------------------------ launch candidates


def _exes(listing: common.Listing) -> list[str]:
    return sorted((n for n in listing.exes() if launchable(Path(n).stem)), key=str.casefold)


def _stem(name: str) -> str:
    return Path(name).stem.casefold()


def _ranked(found: list[Candidate], rest: list[str], why: str) -> list[Candidate]:
    """``found`` first, then every other program of ``rest``; each path once, with the first
    reason given for it."""
    out: list[Candidate] = []
    seen: set[str] = set()
    for path, reason in [*found, *((name, why) for name in rest)]:
        if path.casefold() not in seen:
            seen.add(path.casefold())
            out.append((path, reason))
    return out


OTHER = "another program in the game folder"


def _renpy(root: Path, listing: common.Listing, game: DetectedGame) -> list[Candidate]:
    """The launcher named like the game's script (``exe_basename``), its ``-32`` build after
    it, then the other programs (a ``-32`` one last)."""
    from enginesave.engines.renpy import structure

    exes = _exes(listing)
    base = (structure.exe_basename(root) or "").casefold()
    found = [
        (e, "Ren'Py launcher (named like the game's script)") for e in exes if _stem(e) == base
    ]
    found += [(e, "32-bit Ren'Py launcher") for e in exes if base and _stem(e) == f"{base}-32"]
    rest = sorted((e for e in exes if _stem(e) != "renpy"), key=lambda e: _stem(e).endswith("-32"))
    return _ranked(found, rest, OTHER)


def _rpg_maker(root: Path, listing: common.Listing, game: DetectedGame) -> list[Candidate]:
    """``Game.exe`` with ``Game.ini`` first, then an exe with an ``.ini`` of its name (a
    renamed player), then a ``Game.exe`` alone (NW.js), then the other programs."""
    exes = _exes(listing)
    with_ini = {e for e in exes if listing.file(f"{Path(e).stem}.ini")}

    def rank(name: str) -> int:
        game_exe = _stem(name) == "game"
        return 0 if game_exe and name in with_ini else 1 if name in with_ini else 2

    found = [
        (e, f"RPG Maker player with {Path(e).stem}.ini" if e in with_ini else "Game.exe")
        for e in sorted((e for e in exes if e in with_ini or _stem(e) == "game"), key=rank)
    ]
    return _ranked(found, exes, OTHER)


def _unity(root: Path, listing: common.Listing, game: DetectedGame) -> list[Candidate]:
    exes = _exes(listing)
    found = [
        (e, f"Unity player beside {listing.dir(f'{Path(e).stem}_Data')}")
        for e in exes
        if listing.dir(f"{Path(e).stem}_Data")
    ]
    return _ranked(found, exes, OTHER)


def _unreal(root: Path, listing: common.Listing, game: DetectedGame) -> list[Candidate]:
    from enginesave.engines.unreal import project_folder

    exes = _exes(listing)
    found: list[Candidate] = [(e, "Unreal game launcher in the game folder") for e in exes]
    project = game.identity.project or project_folder(root, listing)
    if project:
        win64 = common.list_folder(root / project / "Binaries" / "Win64")
        inner = _exes(win64) if win64 is not None else []
        prefix = f"{project}/Binaries/Win64/"
        found += [
            (prefix + e, "Unreal shipping build") for e in inner if _stem(e).endswith("-shipping")
        ]
        found += [(prefix + e, "Unreal game program") for e in inner]
    return _ranked(found, [], OTHER)


def _electron(root: Path, listing: common.Listing, game: DetectedGame) -> list[Candidate]:
    exes = _exes(listing)
    names = {
        re.sub(r"[\s_-]+", "", str(n).casefold())
        for n in (game.identity.project, game.identity.title, game.display_name, root.name)
        if n
    }
    found = [
        (e, "the app's program (named like the app)")
        for e in exes
        if re.sub(r"[\s_-]+", "", _stem(e)) in names
    ]
    found += [(e, "program beside resources") for e in exes]
    return _ranked(found, [], OTHER)


def _godot(root: Path, listing: common.Listing, game: DetectedGame) -> list[Candidate]:
    exes = _exes(listing)
    found = [(e, f"Godot program beside {Path(e).stem}.pck") for e in exes if listing.file(
        f"{Path(e).stem}.pck"
    )]  # fmt: skip
    return _ranked(found, exes, OTHER)


def _emuera(root: Path, listing: common.Listing, game: DetectedGame) -> list[Candidate]:
    exes = _exes(listing)
    found = [(e, "Emuera") for e in exes if _stem(e).startswith("emuera")]
    return _ranked(found, exes, OTHER)


def _named_first(root: Path, listing: common.Listing, game: DetectedGame) -> list[Candidate]:
    """Wolf RPG Editor and KiriKiri: the game's own exe (not the engine's generic name)."""
    from enginesave.engines.kirikiri import GENERIC_EXES as KIRIKIRI_EXES
    from enginesave.engines.wolf import GENERIC_EXES as WOLF_EXES

    generic = KIRIKIRI_EXES | WOLF_EXES
    exes = _exes(listing)
    found = [(e, "the game's program") for e in exes if _stem(e) not in generic]
    found += [(e, "the engine's program") for e in exes if _stem(e) in generic]
    return _ranked(found, [], OTHER)


def _html(root: Path, listing: common.Listing, game: DetectedGame) -> list[Candidate]:
    from enginesave.engines.fingerprints import html_entries

    return [(name, reason) for name, reason in html_entries(root, listing)]


def _any(root: Path, listing: common.Listing, game: DetectedGame) -> list[Candidate]:
    return [(e, "program in the game folder") for e in _exes(listing)]


RULES: dict[str, Callable[[Path, common.Listing, DetectedGame], list[Candidate]]] = {
    "renpy": _renpy,
    "rpgmaker_mv": _rpg_maker,
    "rpgmaker_mz": _rpg_maker,
    "rpgmaker_mv_mz": _rpg_maker,
    "rpgmaker_xp_vx_vxace": _rpg_maker,
    "rpgmaker_xp": _rpg_maker,
    "rpgmaker_vx": _rpg_maker,
    "rpgmaker_vxace": _rpg_maker,
    "unity": _unity,
    "unreal": _unreal,
    "electron": _electron,
    "nwjs": _electron,
    "godot": _godot,
    "emuera": _emuera,
    "wolf_rpg": _named_first,
    "kirikiri": _named_first,
    "html": _html,
}


def launch_candidates(
    engine: str, root: Path, game: DetectedGame, listing: common.Listing | None = None
) -> list[dict[str, str]]:
    """``[{"path", "reason"}]`` of the programs that start the install at ``root``; the
    folder's ``listing`` when the scan has it already (one listing per folder)."""
    listing = common.list_folder(root) if listing is None else listing
    if listing is None:
        return []
    try:
        found = RULES.get(engine, _any)(root, listing, game)
    except OSError:
        found = []
    return [{"path": path, "reason": reason} for path, reason in found]


# ----------------------------------------------------------------------------- the report


def complete_installs(
    game: DetectedGame, listings: Mapping[str, common.Listing] | None = None
) -> None:
    """Give ``game`` one install item per folder (at least its ``game_root``), each with every
    field of 1.2.0, and its local sources their ``install_root``. ``listings`` are the folder
    listings the scan made already, by :func:`path_key`."""
    if not any(
        path_key(i.get("game_root") or "") == path_key(game.game_root) for i in game.installs
    ):
        game.installs.insert(
            0,
            {
                "game_root": game.game_root,
                "config_version": game.identity.config_version,
                "engine_version": game.engine_version,
            },
        )
    for install in game.installs:
        install.setdefault("config_version", None)
        install.setdefault("engine_version", None)
        if "game_version" not in install:  # a detector that said nothing: Ren'Py's own
            version = None
            if game.engine == "renpy":
                version = plain_renpy_version(install["config_version"])
            install["game_version"] = version
            install["game_version_source"] = "config.version" if version else None
        if install["game_version"] is None:
            install["game_version_source"] = None
        install.setdefault("game_version_source", None)
        if "launch_candidates" not in install:
            root = Path(install["game_root"])
            listing = (listings or {}).get(path_key(root))
            install["launch_candidates"] = launch_candidates(game.engine, root, game, listing)
    tag_install_roots(game)


def plain_renpy_version(value: Any) -> str | None:
    """Ren'Py's ``config.version`` as it is (a game sets it on purpose; no defaults to drop)."""
    text = common.text_value(value)
    return text if text is not None and len(text) <= common.MAX_VERSION_LENGTH else None


def tag_install_roots(game: DetectedGame) -> None:
    """``install_root`` of every local source: the install folder it is in (the deepest one,
    when one install is inside another), else ``null``."""
    roots = sorted(
        {str(i["game_root"]) for i in game.installs if i.get("game_root")},
        key=lambda r: len(path_key(r)),
        reverse=True,
    )
    for source in game.sources:
        if source.kind is not SourceKind.LOCAL:
            source.install_root = None
            continue
        source.install_root = next((r for r in roots if is_within(source.path, r)), None)


def install_of(game: DetectedGame, root: Path | str) -> dict[str, Any] | None:
    """The install item of ``game`` at ``root``."""
    return next(
        (i for i in game.installs if path_key(i.get("game_root") or "") == path_key(root)), None
    )
