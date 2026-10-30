"""RPG Maker XP, VX and VX Ace (0.10.0, D65 first tier): RGSS games keep their saves as files
directly in the game folder, beside everything else -- ``Save1.rxdata`` (XP),
``Save1.rvdata`` (VX), ``Save01.rvdata2`` (VX Ace). The source takes only those (D65 2):
``Save*.<extension>`` at the top of the game folder, never below it (an unencrypted game keeps
its database in ``Data\\`` with the same extension).

The game's ``.ini`` (``Game.ini``, or named after a renamed exe) has a ``[Game]`` section with
``Library=RGSS...`` and ``Scripts=Data\\Scripts.<extension>``; the extension tells the
generation, else the library's digit, else the archive (``.rgssad``, ``.rgss2a``,
``.rgss3a``). Its ``Title`` (Shift-JIS in Japanese games) is the game's name. RPG Maker
2000/2003 (``RPG_RT.ini``) is another engine and is not taken.

A game installed in Program Files cannot write there without being run as administrator, and
Windows puts its saves in ``%LOCALAPPDATA%\\VirtualStore\\Program Files…\\<game>`` instead:
since 0.11.0 (D69 3) that folder is a second source (VIRTUALSTORE), with the same patterns.

A game run by mkxp or mkxp-z (Pokémon Essentials and others) can say where it writes in
``mkxp.json`` (JSON with ``//`` and ``/* */`` comments): ``dataPathOrg`` and ``dataPathApp`` make
``%APPDATA%\\<org>\\<app>`` (SDL's folder for the app), ``customDataPath`` a folder of its own;
since 0.11.0 (D69 4) the one read from there is a source, taken whole. An absolute
``customDataPath`` is not followed (said); a game without ``mkxp.json`` is as before.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from enginesave import roots
from enginesave.engines import common
from enginesave.engines.base import ScanContext
from enginesave.fsutil import UnsafePathError
from enginesave.models import DetectedGame, Evidence, SaveSource

EXTENSIONS = {"rpgmaker_xp": "rxdata", "rpgmaker_vx": "rvdata", "rpgmaker_vxace": "rvdata2"}
LABELS = {"rpgmaker_xp": "XP", "rpgmaker_vx": "VX", "rpgmaker_vxace": "VX Ace"}
ARCHIVES = {".rgssad": "rpgmaker_xp", ".rgss2a": "rpgmaker_vx", ".rgss3a": "rpgmaker_vxace"}
LIBRARY_RE = re.compile(r"rgss(\d)", re.IGNORECASE)
MAX_MKXP_JSON = 256 * 1024


def strip_comments(text: str) -> str:
    """JSON without the ``//`` and ``/* */`` comments and the trailing commas mkxp-z's JSON5
    reader allows (never inside a string)."""
    out: list[str] = []
    index, length, quoted = 0, len(text), False
    while index < length:
        char = text[index]
        if quoted:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = length if end < 0 else end
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            out.append(" ")
            continue
        elif char == ",":
            ahead = index + 1
            while ahead < length and text[ahead] in " \t\r\n":
                ahead += 1
            if ahead >= length or text[ahead] not in "}]":
                out.append(char)  # a comma before } or ] is left out
        else:
            out.append(char)
        index += 1
    return "".join(out)


def mkxp_config(directory: Path, listing: common.Listing) -> dict[str, Any] | None:
    """``mkxp.json`` beside the game, read without running anything; ``None`` when there is
    none -- or it says nothing (empty, only comments, ``{}``: 1.0.0, D70 7) -- ``{}`` when
    there is one that cannot be read (said by the caller)."""
    name = listing.file("mkxp.json")
    if name is None:
        return None
    data = common.read_head(directory / name, MAX_MKXP_JSON + 1)
    if data is None or len(data) > MAX_MKXP_JSON:
        return {}
    text = strip_comments(common.decode_text(data)).strip()
    if not text:
        return None
    try:
        found = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    if found == {}:
        return None
    return found if isinstance(found, dict) else {}


def mkxp_source(
    directory: Path,
    listing: common.Listing,
    ctx: ScanContext,
    evidence: list[Evidence],
    warnings: list[str],
    title: str | None = None,
) -> SaveSource | None:
    """D69 4: the folder ``mkxp.json`` names for the game's data, or ``None``. An empty
    ``dataPathApp`` beside a ``dataPathOrg`` is the game's title, as mkxp-z makes it."""
    config = mkxp_config(directory, listing)
    if config is None:
        return None
    if not config:
        warnings.append(
            "mkxp.json could not be read (not JSON EngineSave understands): where the game "
            "writes its saves is not known; check its folder by hand"
        )
        return None
    custom = common.text_value(config.get("customDataPath"))
    org = common.text_value(config.get("dataPathOrg"))
    app = common.text_value(config.get("dataPathApp")) or (title if org else None)
    if custom:
        relative = custom.replace("\\", "/").strip("/")
        rooted = custom[:1] in "/\\"  # the root of a drive: absolute as well
        if rooted or Path(custom).is_absolute() or re.match(r"^[A-Za-z]:", custom) or not relative:
            warnings.append(
                f"mkxp.json customDataPath {custom!r} is not followed (not inside the game "
                "folder); copy the saves by hand"
            )
            return None
        found = Evidence(listing.file("mkxp.json") or "mkxp.json", "mkxp_config", "customDataPath")
        evidence.append(found)
        try:
            return common.game_source(directory, "mkxp_data", relative, [found])
        except UnsafePathError:
            warnings.append(f"mkxp.json customDataPath {custom!r} is not a safe folder name")
            return None
    if not app:
        return None
    relative = f"{org}/{app}" if org else app
    found = Evidence(listing.file("mkxp.json") or "mkxp.json", "mkxp_config", f"data {relative!r}")
    evidence.append(found)
    return common.profile_source("mkxp_data", roots.APPDATA, relative, ctx, [found], warnings)


def game_ini(directory: Path, listing: common.Listing) -> tuple[str, dict[str, str]] | None:
    """``(file name, [Game] values with lower-case keys)`` of the RGSS ``.ini``: ``Game.ini``
    first, then any other ``.ini`` with a ``[Game]`` section naming an RGSS library or
    scripts."""
    names = [n for lower, n in sorted(listing.files.items()) if lower.endswith(".ini")]
    names.sort(key=lambda n: n.lower() != "game.ini")
    for name in names:
        values = read_game_section(directory / name)
        if values is not None and (
            "rgss" in values.get("library", "").lower() or "scripts" in values
        ):
            return name, values
    return None


def read_game_section(path: Path) -> dict[str, str] | None:
    """The ``[Game]`` section's ``key=value`` lines, keys in lower case, values as written (a
    ``%`` or ``;`` in a title is the title's); lines of another form are passed over, as the
    engine does. ``None`` without such a section."""
    data = common.read_small(path)
    if data is None:
        return None
    values: dict[str, str] | None = None
    section = None
    for line in common.decode_text(data).splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section == "game" and values is None:
                values = {}
            continue
        if section != "game" or values is None or line.startswith(";") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip().lower(), value.strip())
    return values


def generation(values: dict[str, str], listing: common.Listing) -> str | None:
    scripts = values.get("scripts", "").strip().lower()
    for engine, extension in EXTENSIONS.items():
        if scripts.endswith(f".{extension}"):
            return engine
    digit = LIBRARY_RE.search(values.get("library", ""))
    if digit is not None and digit.group(1) in "123":
        return ("rpgmaker_xp", "rpgmaker_vx", "rpgmaker_vxace")[int(digit.group(1)) - 1]
    for lower in sorted(listing.files):
        for suffix, engine in ARCHIVES.items():
            if lower.endswith(suffix):
                return engine
    return None


class RgssDetector(common.EngineDetector):
    engine = "rpgmaker_rgss"
    engines = ("rpgmaker_xp_vx_vxace",)

    def recognise(
        self, directory: Path, listing: common.Listing, found: Any, ctx: ScanContext
    ) -> DetectedGame | None:
        ini = game_ini(directory, listing)
        values = ini[1] if ini is not None else {}
        engine = generation(values, listing)
        if engine is None:
            return None  # the fingerprint labels it, unsupported
        evidence = [Evidence(found.marker, "engine_fingerprint", f"RPG Maker {LABELS[engine]}")]
        if ini is not None:
            evidence.append(Evidence(ini[0], "engine_ini", "[Game] section"))
        title = common.text_value(values.get("title"))
        pattern = f"Save*.{EXTENSIONS[engine]}"
        sources = [
            common.game_source(
                directory, "saves", roots.ROOT_ITSELF, evidence[:1], patterns=[pattern]
            )
        ]
        warnings: list[str] = []
        virtual = common.virtualstore_relative(directory, ctx.env)
        if virtual is not None:
            moved = common.profile_source(
                "virtualstore",
                roots.VIRTUALSTORE,
                virtual,
                ctx,
                evidence[:1],
                warnings,
                patterns=[pattern],
            )
            if moved is not None:
                sources.append(moved)
                if sources[0].file_count and moved.file_count:  # D70 7: as MV and KiriKiri
                    warnings.append(
                        "saves in both the game folder and VirtualStore "
                        f"({moved.path}); both are backed up"
                    )
        mkxp = mkxp_source(directory, listing, ctx, evidence, warnings, title)  # D69 4
        if mkxp is not None:
            sources.append(mkxp)
        return common.rule_game(
            directory,
            engine,
            title=title,
            engine_key=title,
            sources=sources,
            evidence=evidence,
            warnings=warnings,
        )
