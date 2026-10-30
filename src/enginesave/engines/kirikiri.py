"""KiriKiri (krkr2, KiriKiri Z; 0.10.0, D65 first tier): the engine keeps a game's saves in
``savedata`` beside its exe, unless the exe's ``<name>.cf`` moves them (``datapath=``).

``krkr.console.log`` there is a log and is left out; ``<exe>.cfu`` is the engine's own
settings and is kept with the saves, but alone it does not mean the game saves there. The
``.cf`` macros are followed through the root tokens: ``$(exepath)`` (the game folder),
``$(appdatapath)`` and ``$(vistapath)`` (APPDATA), and since 0.11.0 (D69 3) ``$(personalpath)``
(DOCUMENTS, 「文档」) and ``$(savedgamespath)`` (SAVED_GAMES, 「保存的游戏」). A KAG
``Config.tjs`` that moves the saves is inside ``data.xp3`` (often encrypted) and is not read.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from enginesave import roots
from enginesave.engines import common
from enginesave.engines.base import ScanContext
from enginesave.fsutil import UnsafePathError, source_files
from enginesave.models import DetectedGame, Evidence, SaveSource, SourceStatus

LOG = "krkr.console.log"
GENERIC_EXES = frozenset({"krkr", "krkrz", "tvpwin32", "tvpwin64", "game", "start", "config"})
MACRO_RE = re.compile(r"^\$\((\w+)\)[\\/]?(.*)$")
TOKENS = {
    "exepath": roots.GAME_ROOT,
    "appdatapath": roots.APPDATA,
    "vistapath": roots.APPDATA,
    "personalpath": roots.DOCUMENTS,  # 0.11.0 (D69 3)
    "savedgamespath": roots.SAVED_GAMES,
}


def datapath(directory: Path, exe: str) -> str | None:
    """The ``datapath=`` of ``<exe stem>.cf`` beside the exe (comments start with ``;``)."""
    data = common.read_small(directory / f"{Path(exe).stem}.cf")
    if data is None:
        return None
    for line in common.decode_text(data).splitlines():
        line = line.strip()
        if line.startswith(";") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().lstrip("-").lower() == "datapath":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            return value.strip() or None
    return None


def named(stem: str) -> bool:
    """An exe whose name can name the game: not the engine's own, not a helper."""
    return stem.casefold() not in GENERIC_EXES and not common.is_helper_exe(stem)


def only_settings(source: SaveSource) -> bool:
    """The folder holds nothing but the engine's ``.cfu`` settings, at any depth (the log is
    left out)."""
    files, _ = source_files(Path(source.path), (), [LOG])
    return bool(files) and all(f.rel.lower().endswith(".cfu") for f in files)


def in_savedata(source: SaveSource | None) -> bool:
    """The game folder's ``savedata`` itself, or a folder in it (M44 review: then the second
    source would hold the same files again)."""
    if source is None or source.root_token.value != roots.GAME_ROOT:
        return False
    relative = source.relative_root.replace("\\", "/").lower()
    return relative == "savedata" or relative.startswith("savedata/")


def clean_relative(rest: str) -> str:
    """``rest`` of a datapath without ``.`` parts (``.\\save`` is ``save``; ``.`` is ``""``)."""
    return "/".join(p for p in rest.replace("\\", "/").split("/") if p not in ("", "."))


class KiriKiriDetector(common.EngineDetector):
    engine = "kirikiri"
    engines = ("kirikiri",)

    def recognise(
        self, directory: Path, listing: common.Listing, found: Any, ctx: ScanContext
    ) -> DetectedGame | None:
        exes = listing.exes()
        if not exes:
            return None
        evidence = [Evidence(found.marker, "engine_fingerprint", "KiriKiri archive")]
        warnings: list[str] = []
        moved = [(exe, value) for exe in exes if (value := datapath(directory, exe))]
        names = [Path(e).stem for e in exes if named(Path(e).stem)]
        if moved and named(Path(moved[0][0]).stem):
            name: str | None = Path(moved[0][0]).stem
        else:
            name = names[0] if len(names) == 1 else None
        default = None
        if moved:
            exe, value = moved[0]
            evidence.append(Evidence(f"{Path(exe).stem}.cf", "engine_config", f"datapath={value}"))
            source = self._moved(directory, value, ctx, evidence, warnings)
            if not in_savedata(source):
                # D66 4: the engine's own savedata too, as MV's two save folders
                default = common.game_source(
                    directory, "savedata_default", "savedata", evidence[:1], excludes=[LOG]
                )
        else:
            source = common.game_source(
                directory, "savedata", "savedata", evidence[:1], excludes=[LOG]
            )
        sources = [s for s in (source, default) if s is not None]
        settled = [s for s in sources if s.file_count and only_settings(s)]
        for folder in settled:
            folder.status = SourceStatus.EXPECTED_ABSENT
            folder.notes.append("only the engine's settings (.cfu); the saves are elsewhere")
        saving = [s for s in sources if s.file_count and s not in settled]
        if len(saving) == 2:
            warnings.append("saves both where the .cf says and in savedata; both are backed up")
        if source is not None and source in settled and not saving:
            where = "the folder the .cf names" if moved else "savedata"
            warnings.append(
                f"{where} holds only the engine's settings: this game probably saves somewhere "
                "else (a Config.tjs inside the archive can move it), which EngineSave cannot find"
            )
        elif source is not None and source in settled:
            warnings.append(
                "the folder the .cf names holds only the engine's settings (.cfu); the saves "
                "were found in savedata"
            )
        elif source is not None and not source.file_count and not moved:
            warnings.append("no savedata folder yet (not played, or the game saves somewhere else)")
        return common.rule_game(
            directory, "kirikiri", title=name, engine_key=name, sources=sources,
            evidence=evidence, warnings=warnings,
        )  # fmt: skip

    def _moved(
        self,
        directory: Path,
        value: str,
        ctx: ScanContext,
        evidence: list[Evidence],
        warnings: list[str],
    ) -> SaveSource | None:
        """The folder a ``.cf`` ``datapath`` names, when a root token covers it."""
        macro = MACRO_RE.match(value)
        rest = clean_relative(macro.group(2) if macro else value)
        token = TOKENS.get(macro.group(1).lower()) if macro else None
        if (macro is not None and token is None) or Path(value).is_absolute() or not rest:
            warnings.append(
                f"datapath {value!r} of the .cf is not followed (not a folder EngineSave can "
                "place); copy the saves by hand"
            )
            return None
        if token is None or token == roots.GAME_ROOT:
            try:
                return common.game_source(directory, "savedata", rest, evidence, excludes=[LOG])
            except UnsafePathError:
                warnings.append(f"datapath {value!r} of the .cf is not a safe folder name")
                return None
        return common.profile_source(
            "savedata", token, rest, ctx, evidence, warnings, excludes=[LOG]
        )
