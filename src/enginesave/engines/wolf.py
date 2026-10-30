"""Wolf RPG Editor (0.10.0, D65 first tier): the saves are the ``Save`` folder of the game, every
file in it (``SaveData01.sav``, ``System.sav`` and whatever names a game chose).

A Wolf game is an exe beside ``Data.wolf``, ``Data\\*.wolf`` or an unpacked
``Data\\BasicData`` -- told by what is in ``Data``, not by its name, which KiriKiri uses too.
Its name is the exe's, unless that is ``Game``/``GamePro``, else the folder's; ``Game.ini``
holds settings, not a title (it is written on the first start).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from enginesave.engines import common
from enginesave.engines.base import ScanContext
from enginesave.models import DetectedGame, Evidence

GENERIC_EXES = frozenset({"game", "gamepro", "config"})


def game_exe(listing: common.Listing) -> str | None:
    exes = [Path(name).stem for name in listing.exes()]
    named = [s for s in exes if s.casefold() not in GENERIC_EXES and not common.is_helper_exe(s)]
    return named[0] if len(named) == 1 else None


class WolfDetector(common.EngineDetector):
    engine = "wolf_rpg"
    engines = ("wolf_rpg",)

    def recognise(
        self, directory: Path, listing: common.Listing, found: Any, ctx: ScanContext
    ) -> DetectedGame | None:
        if not listing.exes():
            return None
        evidence = [Evidence(found.marker, "engine_fingerprint", "Wolf RPG Editor data")]
        name = game_exe(listing)
        save = listing.dir("save") or "Save"
        sources = [common.game_source(directory, "save", save, evidence)]
        return common.rule_game(
            directory, "wolf_rpg", title=name, engine_key=name, sources=sources, evidence=evidence
        )
