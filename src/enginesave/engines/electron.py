"""Electron (0.10.0, D65 third tier): an Electron app keeps its data -- a game's saves in Local
Storage or IndexedDB among it -- in ``%APPDATA%\\<name>``, where the name is ``productName``
(else ``name``) of the app's ``package.json``, trimmed.

``package.json`` is read from ``resources\\app`` or from inside ``resources\\app.asar`` (the
asar header: 4 bytes ``4``, the header's size, the pickle's size, the JSON's length, then the
JSON index of the files; a file's bytes start at ``8 + header size + offset``). Nothing is
run. The folder is taken whole except Chromium's caches; a name an app keeps unless its
author changes it (``Electron``, ``app``...) only matches by name: a candidate. So is an app
that shows nothing of a game (0.11.0, D66 6): a launcher, an updater, a tool -- its folder may
hold anything.

An RPG Maker MV or MZ game put into Electron (0.11.0, D69 2: ``js/rpg_core.js`` or
``js/rmmz_core.js``, in ``www`` or not, loose or in the archive) keeps its saves in Local
Storage there unless a plugin writes files: then in a ``save`` folder of the app (``www\\save``
or ``save`` in ``resources\\app``) or, for a game packed into the archive (which cannot be
written), beside it -- ``resources\\save`` or the game folder's ``save``. The folders that are
there are added to the APPDATA one; the title is ``data/System.json``'s ``gameTitle``. The
engine stays ``electron`` and the identity key the package's name, so a record 0.10.0 made of
such a game is the same record.
"""

from __future__ import annotations

import json
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from enginesave import roots
from enginesave.engines import common, rpgmaker_mv
from enginesave.engines.base import ScanContext
from enginesave.fsutil import UnsafePathError, is_reparse_point, long_path
from enginesave.models import DetectedGame, Evidence

CACHES = [
    "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "GrShaderCache", "ShaderCache", "Crashpad", "blob_storage", "Service Worker",
]  # fmt: skip
GENERIC_NAMES = frozenset({"electron", "app", "game", "electron-app", "my-app", "nw"})
HELPER_NAMES = frozenset(
    {"launcher", "updater", "patcher", "installer", "setup", "uninstall", "uninstaller"}
)
_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
# what a game made with a web engine carries (RPG Maker, TyranoBuilder...): D66 6
GAME_MARKERS = (
    "www", "js/rpg_core.js", "js/rmmz_core.js", "data/System.json", "tyrano", "data/scenario",
)  # fmt: skip
MAX_ASAR_HEADER = 32 * 1024 * 1024  # the index of a big app's files
MAX_PACKAGE_JSON = 1024 * 1024


@dataclass(frozen=True)
class Asar:
    """An asar archive's index: where its files' bytes start and the tree of their entries."""

    path: Path
    base: int  # 8 + header size: the first byte of the first file
    size: int
    files: dict[str, Any]


def open_asar(path: Path) -> Asar | None:
    """The index of an asar archive, or ``None`` (not one, damaged, too big)."""
    try:
        with open(long_path(path), "rb") as handle:
            head = handle.read(16)
            if len(head) < 16:
                return None
            four, header_size, _pickle, json_size = struct.unpack("<IIII", head)
            size = path.stat().st_size
            if four != 4 or not 0 < json_size <= min(header_size, MAX_ASAR_HEADER):
                return None
            if 8 + header_size > size:
                return None
            index = json.loads(handle.read(json_size).decode("utf-8"))
    except (OSError, ValueError, TypeError, struct.error, RecursionError):
        return None
    files = index.get("files") if isinstance(index, dict) else None
    return Asar(path, 8 + header_size, size, files) if isinstance(files, dict) else None


def asar_entry(asar: Asar, rel: str) -> dict[str, Any] | None:
    """The index entry of ``rel`` (``a/b``), file or folder, or ``None``."""
    entry: Any = {"files": asar.files}
    for part in rel.split("/"):
        children = entry.get("files") if isinstance(entry, dict) else None
        entry = children.get(part) if isinstance(children, dict) else None
        if not isinstance(entry, dict):
            return None
    return entry


def _whole(value: Any) -> int:
    """A non-negative whole number from an asar index (offsets are strings of digits, sizes
    numbers); anything else -- ``1e400``, a negative, a bool -- is a damaged index (1.0.0, D72
    R11: ValueError, never an OverflowError out of the scan)."""
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("not a whole number")
    elif not isinstance(value, int | str):
        raise ValueError("not a number")
    try:
        number = int(value)
    except OverflowError as exc:
        raise ValueError("out of range") from exc
    if number < 0:
        raise ValueError("negative")
    return number


def asar_read(asar: Asar, rel: str, limit: int) -> bytes | None:
    """The bytes of a file packed in the archive (not one left beside it, ``unpacked``)."""
    entry = asar_entry(asar, rel)
    try:
        if entry is None or entry.get("unpacked") or "files" in entry:
            return None
        offset, length = _whole(entry["offset"]), _whole(entry["size"])
        if not 0 < length <= limit or asar.base + offset + length > asar.size:
            return None
        with open(long_path(asar.path), "rb") as handle:
            handle.seek(asar.base + offset)
            return handle.read(length)
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None


def asar_head(asar: Asar, rel: str, limit: int) -> bytes | None:
    """The first ``limit`` bytes of a file packed in the archive, or ``None``."""
    entry = asar_entry(asar, rel)
    try:
        if entry is None or entry.get("unpacked") or "files" in entry:
            return None
        offset, length = _whole(entry["offset"]), _whole(entry["size"])
        if length <= 0 or asar.base + offset + length > asar.size:
            return None
        with open(long_path(asar.path), "rb") as handle:
            handle.seek(asar.base + offset)
            return handle.read(min(length, limit))
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None


def asar_package(path: Path) -> dict[str, Any] | None:
    """``package.json`` from inside an asar archive, or ``None`` (not one, packed elsewhere,
    too big)."""
    asar = open_asar(path)
    return package_of(asar) if asar is not None else None


def package_of(asar: Asar) -> dict[str, Any] | None:
    """``package.json`` from an archive whose index is read already."""
    data = asar_read(asar, "package.json", MAX_PACKAGE_JSON)
    if data is None:
        return None
    try:
        package = json.loads(common.decode_text(data))
    except (ValueError, RecursionError):
        return None
    return package if isinstance(package, dict) else None


class AppFiles:
    """The files of the app below ``resources``: the loose ``app`` folder, the
    ``app.asar.unpacked`` beside an archive, and the archive itself."""

    def __init__(self, resources: Path) -> None:
        # 1.0.0 (D72 R16): ``resources``, ``app``, ``app.asar.unpacked`` or the archive as a
        # link or junction would make another place's package the game's: not read
        linked = is_reparse_point(resources)
        self.folders = [] if linked else [resources / n for n in ("app", "app.asar.unpacked")]
        self.folders = [f for f in self.folders if f.is_dir() and not is_reparse_point(f)]
        archive = resources / "app.asar"
        usable = not linked and archive.is_file() and not is_reparse_point(archive)
        self.asar = open_asar(archive) if usable else None

    def loose(self, folder: Path, rel: str) -> Path | None:
        """``rel`` in a loose app folder, or ``None`` when it is not there or a link or
        junction is on the way (R16: every folder below the app's own)."""
        current = folder
        for part in rel.split("/"):
            current = current / part
            if not os.path.lexists(long_path(current)) or is_reparse_point(current):
                return None
        return current

    def has(self, rel: str) -> bool:
        if any(self.loose(folder, rel) is not None for folder in self.folders):
            return True
        return self.asar is not None and asar_entry(self.asar, rel) is not None

    def head(self, rel: str, limit: int = common.SMALL_FILE_BYTES) -> bytes | None:
        """The first bytes of an app file, loose or packed."""
        for folder in self.folders:
            path = self.loose(folder, rel)
            found = common.read_head(path, limit) if path is not None else None
            if found is not None:
                return found
        return asar_head(self.asar, rel, limit) if self.asar is not None else None

    def package(self) -> tuple[dict[str, Any] | None, str]:
        """``(package.json, where it was read)``."""
        for folder in self.folders:
            path = self.loose(folder, "package.json")
            found = common.read_json(path, MAX_PACKAGE_JSON) if path is not None else None
            if isinstance(found, dict):
                return found, f"resources/{folder.name}/package.json"
        package = package_of(self.asar) if self.asar is not None else None
        return package, "resources/app.asar!package.json"


def rpg_maker(files: AppFiles) -> tuple[str, str] | None:
    """D69 2: ``(engine, folder of the game's files: "www" or "")`` when the app is an RPG
    Maker MV or MZ game, else ``None``."""
    for prefix in ("www/", ""):
        for engine, core in rpgmaker_mv.CORES.items():
            if files.has(f"{prefix}js/{core}"):
                return engine, prefix.rstrip("/")
    return None


def rpg_maker_title(files: AppFiles, prefix: str) -> str | None:
    """``data/System.json``'s ``gameTitle`` (in its first 64 KiB, as for MV/MZ itself)."""
    rel = f"{prefix}/data/System.json" if prefix else "data/System.json"
    return rpgmaker_mv.title_in(files.head(rel) or b"")


def rpg_maker_saves(directory: Path, resources: str, files: AppFiles, prefix: str) -> list[str]:
    """The ``save`` folders of an MV/MZ game in Electron that are there, below the game folder:
    in a loose app (``resources/app/www/save``, ``resources/app/save``), beside the archive
    (``resources/save``) and the game folder's own ``save``."""
    candidates = []
    for folder in files.folders:
        where = f"{resources}/{folder.name}"
        if prefix:
            candidates.append(f"{where}/{prefix}/save")
        candidates.append(f"{where}/save")
    candidates += [f"{resources}/save", "save"]
    return [
        rel
        for rel in candidates
        if (found := _no_links(directory, rel)) is not None and found.is_dir()
    ]


def _no_links(directory: Path, rel: str) -> Path | None:
    """``rel`` below the game folder when no folder on the way is a link (R16)."""
    current = directory
    for part in rel.split("/"):
        current = current / part
        if not os.path.lexists(long_path(current)) or is_reparse_point(current):
            return None
    return current


def app_package(resources: Path) -> tuple[dict[str, Any] | None, str]:
    """``(package.json, where it was read)`` of the app below ``resources``."""
    return AppFiles(resources).package()


def looks_like_a_game(files: AppFiles, package: dict[str, Any], name: str) -> bool:
    """D66 6: the app carries a game engine's files, or its package says it is a game; never
    one named like a launcher or an updater -- a word of its name (``Game Launcher``,
    ``my-updater``, ``GameSetup``), not a part of one (``Space Dispatcher``)."""
    if helper_name(name):
        return False
    if any(files.has(marker) for marker in GAME_MARKERS):
        return True
    if "game" in package or "save" in package:
        return True
    keywords = package.get("keywords")
    if isinstance(keywords, list) and any("game" in str(k).casefold() for k in keywords):
        return True
    return "game" in str(package.get("description") or "").casefold()


def helper_name(name: str) -> bool:
    """A word of ``name`` (split at spaces, punctuation and capitals) is a helper's."""
    return any(word.casefold() in HELPER_NAMES for word in _WORDS.findall(name))


def data_folder(name: str) -> str | None:
    """The folder below APPDATA Electron uses for ``name``, as Windows makes it: a scoped npm
    name (``@studio/game``) is two folders, trailing dots and spaces go; ``None`` when it is
    still not a safe folder name."""
    parts = [part.rstrip(" .") for part in name.replace("\\", "/").split("/")]
    relative = "/".join(parts)
    try:
        roots.check_relative(roots.APPDATA, relative)
    except UnsafePathError:
        return None
    return relative


class ElectronDetector(common.EngineDetector):
    engine = "electron"
    engines = ("electron",)

    def recognise(
        self, directory: Path, listing: common.Listing, found: Any, ctx: ScanContext
    ) -> DetectedGame | None:
        resources = listing.dir("resources")
        if resources is None:
            return None
        files = AppFiles(directory / resources)
        package, where = files.package()
        evidence = [Evidence(found.marker, "engine_fingerprint", "Electron app")]
        name = None
        if package is not None:
            name = common.text_value(package.get("productName")) or common.text_value(
                package.get("name")
            )
        warnings: list[str] = []
        if package is None or name is None:  # nothing to read, or no name: left unsupported
            return None
        evidence.append(Evidence(where, "electron_package", f"name {name!r}"))
        relative = data_folder(name)
        if relative is None:  # not a folder EngineSave can name: left unsupported
            return None
        generic = name.casefold() in GENERIC_NAMES or name.casefold().startswith("electron-")
        game = looks_like_a_game(files, package, name)
        maker = rpg_maker(files)
        title = name
        if maker is not None:  # D69 2: an RPG Maker MV/MZ game, put into Electron
            label = "MV" if maker[0] == "rpgmaker_mv" else "MZ"
            evidence.append(
                Evidence(f"{maker[1] or '.'}/js", "engine_fingerprint", f"RPG Maker {label} core")
            )
            title = rpg_maker_title(files, maker[1]) or name
            if title != name:
                evidence.append(Evidence("data/System.json", "engine_title", f"title {title!r}"))
        # a name many apps keep stays a candidate even for an RPG Maker game (M48 review): the
        # folder may be another app's; the game's own save folders are not in doubt
        weak = generic or (not game and maker is None)
        if generic:
            warnings.append(
                f"{name!r} is a name many apps keep: %APPDATA%\\{name} is only a candidate"
            )
        elif weak:
            warnings.append(
                f"{name!r} shows nothing of a game (no game engine's files, no game in its "
                f"package.json): %APPDATA%\\{name} is only a candidate"
            )
        found_source = common.profile_source(
            "userdata", roots.APPDATA, relative, ctx, evidence[1:2], warnings,
            excludes=CACHES, candidate=weak,
        )  # fmt: skip
        sources = [found_source] if found_source is not None else []
        if maker is not None:
            for index, rel in enumerate(rpg_maker_saves(directory, resources, files, maker[1])):
                source_id = "save" if index == 0 else f"save_{index + 1}"
                sources.append(common.game_source(directory, source_id, rel, evidence[2:3]))
        return common.rule_game(
            directory,
            "electron",
            title=title,
            engine_key=None if weak else name,
            sources=sources,
            evidence=evidence,
            warnings=warnings,
            project=name,
            game_version=common.package_version(package, where),  # 1.2.0 (D73 7)
        )
