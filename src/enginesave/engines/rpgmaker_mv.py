"""RPG Maker MV and MZ (0.10.0, D65 first tier): NW.js games whose saves are files in a
``save`` folder of the game (never the NW.js profile, which every such game shares).

* The game's files are below the folder of the page ``package.json`` starts (``"main"``):
  ``www`` for a deployed MV game, the game folder itself for MZ and for an MV project.
  ``js/rpg_core.js`` there is MV, ``js/rmmz_core.js`` MZ (the fingerprint decides).
* The saves: the whole ``save`` folder beside that page (``file1.rpgsave``, its ``.bak``,
  ``global``, ``config``; MZ's ``file0`` autosave and a ``.rmmzsave_`` left by a write that
  was cut off). Older MV versions and games packed into one exe write ``save`` in the game
  folder instead, so for ``www`` both are looked at; with saves in both, both are backed up.
* The title: ``data/System.json`` ``gameTitle`` (what the game shows), else the window title
  in ``package.json``, else the folder name. ``package.json`` ``name`` is never a title: it is
  NW.js's id of the app (``KADOKAWA/RPGMV``, ``rmmz-game``).
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

from enginesave.engines import common
from enginesave.engines.base import ScanContext
from enginesave.models import DetectedGame, Evidence

CORES = {"rpgmaker_mv": "rpg_core.js", "rpgmaker_mz": "rmmz_core.js"}
VERSION_RE = re.compile(rb"RPGMAKER_VERSION\s*=\s*[\"']([0-9][0-9.]*)[\"']")
# System.json can be large (every switch and variable is named in it): the title is taken from
# its first 64 KiB, where the editor writes it (keys in order, the long lists after it)
TITLE_RE = re.compile(rb'"gameTitle"\s*:\s*("(?:[^"\\]|\\.)*")')


def base_folder(directory: Path, listing: common.Listing) -> tuple[str, Path]:
    """``(relative, folder)`` of the folder ``package.json`` ``"main"`` names, ``www`` when there
    is no ``package.json`` but a ``www`` folder, else the game folder (``.``)."""
    name = listing.file("package.json")
    manifest = common.read_json(directory / name) if name else None
    main = manifest.get("main") if isinstance(manifest, dict) else None
    if isinstance(main, str) and main.strip():
        parts = [p for p in main.strip().replace("\\", "/").split("/")[:-1] if p not in ("", ".")]
        if parts and ".." not in parts and ":" not in parts[0]:
            return "/".join(parts), directory.joinpath(*parts)
        if not parts:
            return ".", directory
    www = listing.dir("www")
    if www and (directory / www / "js").is_dir():
        return www, directory / www
    return ".", directory


def core_version(path: Path) -> str | None:
    head = common.read_head(path)
    found = VERSION_RE.search(head or b"")
    return found.group(1).decode("ascii") if found else None


def title_in(head: bytes) -> str | None:
    """``gameTitle`` from the first bytes of a ``System.json``, or ``None``."""
    if (match := TITLE_RE.search(head)) is not None:
        with contextlib.suppress(ValueError):
            return common.text_value(json.loads(common.decode_text(match.group(1))))
    return None


def title_of(directory: Path, base: Path, listing: common.Listing) -> tuple[str | None, str]:
    """``(title, where it came from)``; ``None`` when only the folder name is left."""
    title = title_in(common.read_head(base / "data" / "System.json") or b"")
    if title:
        return title, "data/System.json gameTitle"
    name = listing.file("package.json")
    manifest: Any = common.read_json(directory / name) if name else None
    window = manifest.get("window") if isinstance(manifest, dict) else None
    if isinstance(window, dict) and (title := common.text_value(window.get("title"))):
        return title, "package.json window.title"
    return None, "folder name"


class RpgMakerMvDetector(common.EngineDetector):
    engine = "rpgmaker_mv_mz"
    engines = tuple(CORES)

    def recognise(
        self, directory: Path, listing: common.Listing, found: Any, ctx: ScanContext
    ) -> DetectedGame | None:
        relative, base = base_folder(directory, listing)
        core = base / "js" / CORES[found.engine]
        label = "MV" if found.engine == "rpgmaker_mv" else "MZ"
        evidence = [Evidence(found.marker, "engine_fingerprint", f"RPG Maker {label} core")]
        warnings: list[str] = []
        title, source = title_of(directory, base, listing)
        if title is not None:
            evidence.append(Evidence(source, "engine_title", f"title {title!r}"))
        save = "save" if relative == "." else f"{relative}/save"
        sources = [common.game_source(directory, "save", save, evidence[:1])]
        if relative != ".":  # older MV, or packed into one exe: the game folder's own save
            sources.append(common.game_source(directory, "save_root", "save", evidence[:1]))
            if all(s.file_count for s in sources):
                warnings.append(
                    f"saves in both {save.replace('/', chr(92))} and save; both are backed up"
                )
        package = listing.file("package.json")  # NW.js's; a version only when the author set one
        version = (
            common.package_version(common.read_json(directory / package), package)
            if package
            else None
        )
        return common.rule_game(
            directory,
            found.engine,
            title=title,
            engine_key=title,
            sources=sources,
            evidence=evidence,
            warnings=warnings,
            engine_version=core_version(core),
            game_version=version,  # 1.2.0 (D73 7)
        )
