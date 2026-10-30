"""Minimal synthetic games of the engines 0.10.0 supports (D65): only the files their rules
look at, with dummy bytes (never a real program, never a copy of a real game). Each builder
returns the game folder and its save files (path -> bytes) so that a test can delete and
compare them."""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DUMMY_EXE = b"MZ\x90\x00 dummy, never run"


@dataclass
class Built:
    root: Path
    saves: dict[Path, bytes] = field(default_factory=dict)  # save file -> its bytes
    others: dict[Path, bytes] = field(default_factory=dict)  # files that are not saves

    def write(self, path: Path, data: bytes, *, save: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        (self.saves if save else self.others)[path] = data


# ----------------------------------------------------------------------- RPG Maker MV/MZ


def build_mv(
    parent: Path,
    folder: str = "Fixture MV",
    *,
    title: str | None = "Fixture MV",
    window_title: str = "Fixture MV",
    saves: bool = True,
    root_saves: bool = False,
    version: str = "1.6.2",
) -> Built:
    """A deployed MV game: ``Game.exe``, ``nw.dll``, ``package.json`` (``main``
    ``www/index.html``, NW.js's ``name``), ``www/js/rpg_core.js``, ``www/data/System.json``
    and saves in ``www/save`` (``root_saves``: also in ``save``, as older MV writes)."""
    root = parent / folder
    built = Built(root)
    built.write(root / "Game.exe", DUMMY_EXE, save=False)
    built.write(root / "nw.dll", b"dll", save=False)
    manifest = {
        "name": "KADOKAWA/RPGMV",
        "main": "www/index.html",
        "window": {"title": window_title},
    }
    built.write(root / "package.json", json.dumps(manifest).encode(), save=False)
    www = root / "www"
    built.write(www / "index.html", b"<title></title>", save=False)
    core = f'var Utils = {{}};\nUtils.RPGMAKER_VERSION = "{version}";\n'.encode()
    built.write(www / "js" / "rpg_core.js", core, save=False)
    built.write(www / "js" / "libs" / "lz-string.js", b"//", save=False)
    system = {"airship": {}, "gameTitle": title, "locale": "en_US", "switches": [""] * 10}
    if title is None:
        system.pop("gameTitle")
    built.write(www / "data" / "System.json", json.dumps(system).encode(), save=False)
    built.write(www / "data" / "Map001.json", b"{}", save=False)
    if saves:
        for name in ("config.rpgsave", "global.rpgsave", "file1.rpgsave", "file1.rpgsave.bak"):
            built.write(www / "save" / name, f"N4Ig{name}".encode(), save=True)
    if root_saves:
        built.write(root / "save" / "file2.rpgsave", b"N4Igolder", save=True)
    return built


def build_mz(
    parent: Path, folder: str = "Fixture MZ", *, title: str = "Fixture MZ", saves: bool = True
) -> Built:
    """A deployed MZ game: everything at the top (``main`` ``index.html``), ``js/rmmz_core.js``,
    saves in ``save`` with the autosave ``file0`` and a ``.rmmzsave_`` left by a cut write."""
    root = parent / folder
    built = Built(root)
    built.write(root / "Game.exe", DUMMY_EXE, save=False)
    built.write(root / "nw.dll", b"dll", save=False)
    manifest = {"name": "rmmz-game", "main": "index.html", "window": {"title": title}}
    built.write(root / "package.json", json.dumps(manifest).encode(), save=False)
    built.write(root / "index.html", b"<title></title>", save=False)
    core = b'Utils.RPGMAKER_NAME = "MZ";\nUtils.RPGMAKER_VERSION = "1.8.0";\n'
    built.write(root / "js" / "rmmz_core.js", core, save=False)
    built.write(root / "js" / "libs" / "pako.min.js", b"//", save=False)
    built.write(
        root / "data" / "System.json", json.dumps({"gameTitle": title}).encode(), save=False
    )
    if saves:
        names = ("config.rmmzsave", "global.rmmzsave", "file0.rmmzsave", "file1.rmmzsave")
        for name in (*names, "file1.rmmzsave_"):
            built.write(root / "save" / name, b"x\x01" + name.encode(), save=True)
    return built


# ------------------------------------------------------------------ RPG Maker XP/VX/VX Ace

RGSS = {
    "xp": ("RGSS104E.dll", "rxdata", "Game.rgssad", b"\x01", ("Save1", "Save2")),
    "vx": ("RGSS202E.dll", "rvdata", "Game.rgss2a", b"\x01", ("Save1", "Save2")),
    "vxace": ("System\\RGSS301.dll", "rvdata2", "Game.rgss3a", b"\x03", ("Save01", "Save16")),
}


def build_rgss(
    parent: Path,
    generation: str,
    folder: str | None = None,
    *,
    title: str | bytes = "",
    exe: str = "Game",
    saves: bool = True,
    encrypted: bool = True,
) -> Built:
    """An RGSS game: ``<exe>.exe`` and ``<exe>.ini`` (``[Game]`` with ``Library``, ``Scripts``,
    ``Title``; CRLF, the title as given -- ``bytes`` for a Shift-JIS one), the archive or (not
    ``encrypted``) a ``Data`` folder whose database files share the saves' extension, and the
    saves at the top. The title defaults to ``Fixture <generation>``."""
    library, extension, archive, version, slots = RGSS[generation]
    root = parent / (folder or f"Fixture {generation.upper()}")
    built = Built(root)
    built.write(root / f"{exe}.exe", DUMMY_EXE, save=False)
    text = (
        title if isinstance(title, bytes) else (title or f"Fixture {generation.upper()}").encode()
    )
    lines = [
        b"[Game]",
        b"Library=" + library.encode(),
        b"Scripts=Data\\Scripts." + extension.encode(),
    ]
    ini = b"\r\n".join([*lines, b"Title=" + text, b"RTP=Standard", b""])
    built.write(root / f"{exe}.ini", ini, save=False)
    if encrypted:
        built.write(
            root / archive.replace("Game", exe), b"RGSSAD\x00" + version + b"filler", save=False
        )
    else:
        for name in ("Scripts", "System", "Map001"):
            built.write(root / "Data" / f"{name}.{extension}", b"\x04\x08database", save=False)
        built.write(root / "Data" / f"Save9.{extension}", b"\x04\x08not a save", save=False)
    if saves:
        for slot in slots:
            built.write(root / f"{slot}.{extension}", b"\x04\x08" + slot.encode(), save=True)
    return built


def build_rpg2000(parent: Path, folder: str = "Fixture 2000") -> Built:
    """RPG Maker 2000/2003: ``RPG_RT.exe``, ``RPG_RT.ini`` with ``[RPG_RT]`` -- another engine."""
    root = parent / folder
    built = Built(root)
    built.write(root / "RPG_RT.exe", DUMMY_EXE, save=False)
    built.write(root / "RPG_RT.ini", b"[RPG_RT]\r\nGameTitle=Old\r\n", save=False)
    built.write(root / "RPG_RT.ldb", b"LcfDataBase", save=False)
    built.write(root / "Save01.lsd", b"LcfSaveData", save=False)
    return built


# ----------------------------------------------------------------------------- Wolf RPG


def build_wolf(
    parent: Path,
    folder: str = "Fixture Wolf",
    *,
    exe: str = "Game",
    layout: str = "data_wolf",
    saves: bool = True,
) -> Built:
    """A Wolf RPG Editor game: ``<exe>.exe``, ``GuruguruSMF4.dll`` and the data as
    ``Data.wolf`` (``data_wolf``), ``Data\\BasicData.wolf`` (``packed``) or an unpacked
    ``Data\\BasicData\\Game.dat`` (``unpacked``); saves in ``Save`` (any names)."""
    root = parent / folder
    built = Built(root)
    built.write(root / f"{exe}.exe", DUMMY_EXE, save=False)
    built.write(root / "GuruguruSMF4.dll", b"dll", save=False)
    if layout == "data_wolf":
        built.write(root / "Data.wolf", b"wolf archive", save=False)
    elif layout == "packed":
        built.write(root / "Data" / "BasicData.wolf", b"wolf archive", save=False)
        built.write(root / "Data" / "MapData.wolf", b"wolf archive", save=False)
    else:
        built.write(
            root / "Data" / "BasicData" / "Game.dat", b"\x00W\x00\x00OL\x00FM\x00", save=False
        )
    built.write(root / "Game.ini", b"Fullscreen=0\r\n", save=False)
    if saves:
        for name in ("SaveData01.sav", "System.sav", "Chapter2.dat"):
            built.write(root / "Save" / name, b"wolf " + name.encode(), save=True)
    return built


# ----------------------------------------------------------------------------- KiriKiri

XP3_MAGIC = b"XP3\r\n \n\x1a\x8bg\x01"


def build_kirikiri(
    parent: Path,
    folder: str = "Fixture KiriKiri",
    *,
    exe: str = "fixture",
    saves: bool = True,
    cf: str | None = None,
    only_settings: bool = False,
) -> Built:
    """A KiriKiri game: ``<exe>.exe`` and ``data.xp3`` (with the XP3 mark); ``savedata`` with
    saves, the engine's ``<exe>.cfu`` and ``krkr.console.log``; ``cf``: a ``<exe>.cf`` whose
    ``datapath=`` is that (the saves are then written where it says, not in ``savedata``);
    ``only_settings``: ``savedata`` holds just the ``.cfu`` and the log."""
    root = parent / folder
    built = Built(root)
    built.write(root / f"{exe}.exe", DUMMY_EXE, save=False)
    built.write(root / "data.xp3", XP3_MAGIC + b"filler", save=False)
    if cf is not None:
        built.write(root / f"{exe}.cf", f";comment\r\ndatapath={cf}\r\n".encode(), save=False)
    savedata = root / "savedata"
    if cf is None and (saves or only_settings):
        built.write(savedata / f"{exe}.cfu", b"settings", save=not only_settings)
        built.write(savedata / "krkr.console.log", b"a log", save=False)
    if saves and not only_settings and cf is None:
        for name in ("data0.kdt", "data1.bmp", "datasc.ksd", "datasu.ksd"):
            built.write(savedata / name, b"kag " + name.encode(), save=True)
    return built


# -------------------------------------------------------------------------------- Unity


def profile_folder(token: str) -> Path:
    """Where a root token points in the tests (conftest: a sandbox folder)."""
    return Path(os.environ[f"ENGINESAVE_ROOT_{token}"])


def build_unity(
    parent: Path,
    folder: str = "Fixture Unity",
    *,
    exe: str = "Fixture",
    company: str = "Fixture Co",
    product: str = "Fixture Game",
    app_info: bool = True,
    saves: bool = True,
) -> Built:
    """A Unity player: ``<exe>.exe``, ``UnityPlayer.dll``, ``<exe>_Data`` with
    ``globalgamemanagers`` and ``app.info`` (company, product); saves in
    ``UNITY_LOCALLOW\\<company>\\<product>`` beside ``Player.log`` and ``Player-prev.log``."""
    root = parent / folder
    built = Built(root)
    built.write(root / f"{exe}.exe", DUMMY_EXE, save=False)
    built.write(root / "UnityPlayer.dll", b"dll", save=False)
    data = root / f"{exe}_Data"
    built.write(data / "globalgamemanagers", b"unity", save=False)
    if app_info:
        built.write(data / "app.info", f"{company}\n{product}".encode(), save=False)
    if saves:
        locallow = profile_folder("UNITY_LOCALLOW") / company / product
        built.write(locallow / "save1.dat", b"unity save one", save=True)
        built.write(locallow / "Saves" / "slot2.json", b'{"level": 2}', save=True)
        built.write(locallow / "Player.log", b"a log", save=False)
        built.write(locallow / "Player-prev.log", b"an older log", save=False)
    return built


# ----------------------------------------------------------------------------- Electron


def asar_bytes(files: dict[str, bytes]) -> bytes:
    """An asar archive as Electron writes one: a Chromium pickle holding the JSON index, then
    the files' bytes (``offset`` counted from the end of the header)."""
    index: dict[str, dict] = {"files": {}}
    offset = 0
    for name, data in files.items():  # "a/b.js" is the file b.js in the folder a
        *folders, leaf = name.split("/")
        node = index
        for folder in folders:
            node = node["files"].setdefault(folder, {"files": {}})
        node["files"][leaf] = {"size": len(data), "offset": str(offset)}
        offset += len(data)
    text = json.dumps(index).encode()
    pad = (4 - len(text) % 4) % 4
    payload = 4 + len(text) + pad
    head = struct.pack("<IIII", 4, 4 + payload, payload, len(text))
    return head + text + b"\x00" * pad + b"".join(files.values())


def build_electron(
    parent: Path,
    folder: str = "Fixture Electron",
    *,
    product: str | None = "Fixture Electron",
    name: str = "fixture-electron",
    packed: bool = True,
    saves: bool = True,
    game: bool = True,
    extra: dict[str, bytes] | None = None,
    package: dict[str, Any] | None = None,
) -> Built:
    """An Electron app: ``<folder>.exe``, ``ffmpeg.dll``, ``resources\\app.asar`` (or the loose
    ``resources\\app``) with ``package.json`` (``game``: its keywords say so, D66 6; with the
    fields of ``package``) and the ``extra`` files; its data in ``APPDATA\\<productName or
    name>``: Local Storage and IndexedDB (saves), ``Preferences``, and caches that are not
    saves."""
    root = parent / folder
    built = Built(root)
    built.write(root / f"{folder}.exe", DUMMY_EXE, save=False)
    built.write(root / "ffmpeg.dll", b"dll", save=False)
    fields = {"name": name, "main": "main.js", **({"productName": product} if product else {})}
    if game:
        fields["keywords"] = ["game", "visual novel"]
    fields |= package or {}
    files = {"package.json": json.dumps(fields).encode(), "main.js": b"// app", **(extra or {})}
    if packed:
        built.write(root / "resources" / "app.asar", asar_bytes(files), save=False)
    else:
        for file, data in files.items():
            built.write(root / "resources" / "app" / file, data, save=False)
    if saves:
        data_dir = profile_folder("APPDATA") / (product.strip() if product else name)
        built.write(data_dir / "Local Storage" / "leveldb" / "000003.log", b"save data", save=True)
        built.write(
            data_dir / "IndexedDB" / "file__0.indexeddb.leveldb" / "CURRENT", b"x", save=True
        )
        built.write(data_dir / "Preferences", b"{}", save=True)
        built.write(data_dir / "Cache" / "Cache_Data" / "data_0", b"cache", save=False)
        built.write(data_dir / "GPUCache" / "index", b"cache", save=False)
    return built


# ------------------------------------------------------------------------------- Unreal


def build_unreal(
    parent: Path,
    folder: str = "Fixture Unreal",
    *,
    project: str = "FixtureGame",
    saves: str | None = "installed",
) -> Built:
    """A packaged UE4/UE5 game: ``Engine\\Binaries``, ``<project>\\Binaries\\Win64\\<project>-
    Win64-Shipping.exe``, ``<project>\\Content\\Paks\\*.pak``; saves in
    ``<project>\\Saved\\SaveGames`` below LOCALAPPDATA (``installed``) or the game folder
    (``local``), with logs and settings beside them that are not saves."""
    root = parent / folder
    built = Built(root)
    built.write(root / f"{project}.exe", DUMMY_EXE, save=False)
    built.write(root / "Engine" / "Binaries" / "ThirdParty" / "x.dll", b"dll", save=False)
    win64 = root / project / "Binaries" / "Win64"
    built.write(win64 / f"{project}-Win64-Shipping.exe", DUMMY_EXE, save=False)
    built.write(root / project / "Content" / "Paks" / "pakchunk0-Windows.pak", b"pak", save=False)
    if saves:
        base = profile_folder("LOCALAPPDATA") if saves == "installed" else root
        saved = base / project / "Saved"
        built.write(saved / "SaveGames" / "Slot1.sav", b"GVAS one", save=True)
        built.write(saved / "SaveGames" / "76561190000000000" / "Slot2.sav", b"GVAS two", save=True)
        built.write(saved / "Logs" / f"{project}.log", b"a log", save=False)
        built.write(saved / "Config" / "Windows" / "GameUserSettings.ini", b"[x]", save=False)
    return built


# -------------------------------------------------------------------------------- Godot


def project_binary(settings: dict[str, str | bool | int]) -> bytes:
    """A minimal ``project.binary`` (``ECFG``): strings, booleans and (skipped) integers."""
    out = [b"ECFG", struct.pack("<I", len(settings))]
    for key, value in settings.items():
        raw = key.encode("utf-8")
        out += [struct.pack("<I", len(raw)), raw]
        if isinstance(value, bool):
            variant = struct.pack("<II", 1, int(value))
        elif isinstance(value, int):
            variant = struct.pack("<Iq", 2 | (1 << 16), value)
        else:
            text = value.encode("utf-8")
            variant = struct.pack("<II", 4, len(text)) + text + b"\0" * (-len(text) % 4)
        out += [struct.pack("<I", len(variant)), variant]
    return b"".join(out)


def pck_bytes(files: dict[str, bytes], version: int = 2) -> bytes:
    """A minimal Godot pack of format 1, 2 or 3 holding ``files`` (offsets from the start)."""
    names = [n.encode("utf-8") for n in files]
    names = [n + b"\0" * (-len(n) % 4) for n in names]
    blobs = list(files.values())
    extra = b"\0" * 4 if version >= 2 else b""

    def table(offsets: list[int]) -> bytes:
        out = [struct.pack("<I", len(names))]
        for name, offset, blob in zip(names, offsets, blobs, strict=True):
            out += [struct.pack("<I", len(name)), name, struct.pack("<QQ", offset, len(blob))]
            out += [b"\0" * 16, extra]
        return b"".join(out)

    def offsets(start: int) -> list[int]:
        found, at = [], start
        for blob in blobs:
            found.append(at)
            at += len(blob)
        return found

    fixed = b"GDPC" + struct.pack("<IIII", version, 4, 2, 0)
    if version == 1:
        head_len = len(fixed) + 64
        data_at = head_len + len(table([0] * len(blobs)))
        return fixed + b"\0" * 64 + table(offsets(data_at)) + b"".join(blobs)
    if version == 2:
        head_len = len(fixed) + 12 + 64
        data_at = head_len + len(table([0] * len(blobs)))
        head = fixed + struct.pack("<IQ", 0, 0) + b"\0" * 64
        return head + table(offsets(data_at)) + b"".join(blobs)
    head_len = len(fixed) + 12 + 8 + 64
    directory = head_len + sum(len(b) for b in blobs)
    head = fixed + struct.pack("<IQQ", 0, 0, directory) + b"\0" * 64
    return head + b"".join(blobs) + table(offsets(head_len))


def build_godot(
    parent: Path,
    folder: str = "Fixture Godot",
    *,
    exe: str = "fixture_godot",
    saves: bool = True,
    project: str | None = None,
    custom_dir: str | None = None,
    pck_version: int = 2,
) -> Built:
    """A Godot export: ``<exe>.exe`` beside ``<exe>.pck`` (``GDPC``); saves in
    ``APPDATA\\Godot\\app_userdata\\<exe>`` beside its ``logs``. With ``project`` the pack is
    a real one (format ``pck_version``) whose ``project.binary`` names the project (and a
    custom user folder), and the saves are where that says."""
    root = parent / folder
    built = Built(root)
    built.write(root / f"{exe}.exe", DUMMY_EXE, save=False)
    if project is None:
        built.write(root / f"{exe}.pck", b"GDPC\x02\x00\x00\x00", save=False)
    else:
        settings: dict[str, str | bool | int] = {"application/config/name": project}
        settings["display/window/size/width"] = 1280
        if custom_dir is not None:
            settings["application/config/use_custom_user_dir"] = True
            settings["application/config/custom_user_dir_name"] = custom_dir
        prefix = "" if pck_version >= 3 else "res://"  # Godot 4.4+ (format 3) drops it
        files = {f"{prefix}icon.png": b"png", f"{prefix}project.binary": project_binary(settings)}
        built.write(root / f"{exe}.pck", pck_bytes(files, pck_version), save=False)
    if saves:
        userdata = profile_folder("APPDATA") / "Godot" / "app_userdata" / exe
        if project is not None:
            userdata = profile_folder("APPDATA") / (
                custom_dir if custom_dir is not None else f"Godot/app_userdata/{project}"
            )
        built.write(userdata / "save_game.dat", b"godot save", save=True)
        built.write(userdata / "settings.cfg", b"[audio]", save=True)
        built.write(userdata / "logs" / "godot.log", b"a log", save=False)
    return built
