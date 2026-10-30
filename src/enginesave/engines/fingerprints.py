"""Fingerprints for non-Ren'Py engines. Labelled ``UNSUPPORTED``; their saves are not located."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from enginesave.engines.base import DetectionResult, Detector, ScanContext
from enginesave.engines.common import Listing, list_folder, looked_at
from enginesave.engines.labels import ENGINE_LABELS, engine_label  # noqa: F401 - re-exported
from enginesave.engines.renpy.structure import read_small_text
from enginesave.engines.rpgmaker_mv import CORES, base_folder
from enginesave.models import (
    DetectedGame,
    Evidence,
    GameIdentity,
    GameStatus,
    MetadataState,
)

_RGSS_ARCHIVES = ("game.rgssad", "game.rgss2a", "game.rgss3a")
_RGSS_SUFFIXES = (".rgssad", ".rgss2a", ".rgss3a")
# Files a Chromium-based runtime (Electron) ships beside its .exe (plan section 21 M21).
_CHROMIUM_FILES = ("ffmpeg.dll", "chrome_100_percent.pak", "v8_context_snapshot.bin")


@dataclass(frozen=True)
class Fingerprint:
    engine: str
    marker: str  # relative path of the file or folder that matched


def fingerprint(directory: Path, listing: Listing | None = None) -> Fingerprint | None:
    """The engine a folder's own files point at, the first rule that matches winning; the
    ``listing`` when the caller has it (one listing of a folder per scan, 0.10.0)."""
    listing = list_folder(directory) if listing is None else listing
    if listing is None:
        return None
    files, dirs = listing.files, listing.dirs

    if "unityplayer.dll" in files:
        return Fingerprint("unity", files["unityplayer.dll"])
    for lower, name in sorted(dirs.items()):
        if lower.endswith("_data") and (directory / name / "globalgamemanagers").is_file():
            return Fingerprint("unity", f"{name}/globalgamemanagers")
    if "nw.dll" in files or "nw.pak" in files:  # NW.js (0.12, of MV up to 1.5: nw.pak only)
        # 0.10.0 (D65): MV or MZ by the core script below the page package.json starts
        relative, base = base_folder(directory, listing)
        for engine, core in CORES.items():
            if (base / "js" / core).is_file():
                prefix = "" if relative == "." else f"{relative}/"
                return Fingerprint(engine, f"{prefix}js/{core}")
        for marker in ("www", "js/rpg_core.js", "js/rmmz_core.js"):
            if (directory / marker).exists():
                return Fingerprint("rpgmaker_mv_mz", marker)
    # Electron and NW.js games carry a whole web app (node_modules and all): recognising the
    # folder keeps the scan from walking that deep tree (D34).
    if "resources" in dirs and any(name.endswith(".exe") for name in files):
        resources = directory / dirs["resources"]
        app = next(
            (m for m in ("app.asar", "app") if (resources / m).exists()),
            None,
        )
        chromium = next((files[name] for name in _CHROMIUM_FILES if name in files), None)
        if app is not None and chromium is not None:
            return Fingerprint("electron", f"{dirs['resources']}/{app}")
    if "nw.dll" in files and "package.json" in files:
        return Fingerprint("nwjs", files["package.json"])
    emuera = next(
        (files[n] for n in sorted(files) if n.startswith("emuera") and n.endswith(".exe")), None
    )
    if emuera is not None:
        return Fingerprint("emuera", emuera)
    if "erb" in dirs and "csv" in dirs:
        return Fingerprint("emuera", dirs["erb"])
    for archive in _RGSS_ARCHIVES:
        if archive in files:
            return Fingerprint("rpgmaker_xp_vx_vxace", files[archive])
    exes = [lower[:-4] for lower in sorted(files) if lower.endswith(".exe")]
    for lower in sorted(files):  # 0.10.0: an archive named after a renamed exe (Foo.rgss3a)
        if lower.endswith(_RGSS_SUFFIXES) and lower.rsplit(".", 1)[0] in exes:
            return Fingerprint("rpgmaker_xp_vx_vxace", files[lower])
    for ini in dict.fromkeys(["game.ini", *(f"{stem}.ini" for stem in exes)]):
        if ini not in files:
            continue
        text = (read_small_text(directory / files[ini]) or "").lower()
        if "rgss" in text or ("[game]" in text and "scripts=" in text):
            return Fingerprint("rpgmaker_xp_vx_vxace", files[ini])
    for lower in sorted(files):
        if lower.endswith(".exe") and f"{lower[:-4]}.pck" in files:
            return Fingerprint("godot", files[f"{lower[:-4]}.pck"])
    if (directory / "Engine" / "Binaries").is_dir():
        for name in sorted(dirs.values()):
            if (directory / name / "Content" / "Paks").is_dir():
                return Fingerprint("unreal", f"{name}/Content/Paks")
    wolf = _wolf_data(directory, files, dirs)
    if wolf is not None:
        return Fingerprint("wolf_rpg", wolf)
    for lower in sorted(files):
        if lower.endswith(".xp3"):
            return Fingerprint("kirikiri", files[lower])
    return None


def _wolf_data(directory: Path, files: dict[str, str], dirs: dict[str, str]) -> str | None:
    """0.10.0: a Wolf RPG Editor game is an exe beside ``Data.wolf``, ``Data\\*.wolf`` or an
    unpacked ``Data\\BasicData`` -- told by what ``Data`` holds, since KiriKiri games may have
    a ``data`` folder and a ``Game.exe`` too."""
    if not any(name.endswith(".exe") for name in files):
        return None
    if "data.wolf" in files:
        return files["data.wolf"]
    if "data" not in dirs:
        return None
    data = list_folder(directory / dirs["data"])
    if data is None:
        return None
    packed = next(
        (name for lower, name in sorted(data.files.items()) if lower.endswith(".wolf")), None
    )
    if packed is not None:
        return f"{dirs['data']}/{packed}"
    basic = data.dir("basicdata")
    if basic is not None:
        inner = list_folder(directory / dirs["data"] / basic)
        if inner is not None and (
            inner.file("game.dat") or any(lower.endswith(".project") for lower in inner.files)
        ):
            return f"{dirs['data']}/{basic}"
    return None


class FingerprintDetector(Detector):
    engine = "fingerprint"

    def detect(self, directory: Path, ctx: ScanContext) -> DetectionResult | None:
        _listing, found = looked_at(directory, ctx)
        if found is None:
            return None
        label = ENGINE_LABELS[found.engine]
        exe = next((p.stem for p in sorted(directory.glob("*.exe"))), None)
        game = DetectedGame(
            game_root=str(directory),
            engine=found.engine,
            engine_version=None,
            display_name=directory.name,
            identity=GameIdentity(None, None, None, None, exe, directory.name),
            metadata_state=MetadataState.MISSING,
            status=GameStatus.UNSUPPORTED,
            evidence=[Evidence(found.marker, "engine_fingerprint", f"{label} marker")],
            warnings=[f"{label} is not supported: engine identified only, saves not located"],
        )
        return DetectionResult(game=game)
