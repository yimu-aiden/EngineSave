"""Godot (0.10.0, D65 third tier): a Godot game saves in
``%APPDATA%\\Godot\\app_userdata\\<project name>``, where the name is a project setting stored
inside the ``.pck`` (or a custom folder of the project's choosing). Since 0.11.0 (D69 1) the
``.pck``'s file table is read (formats 1-3, at most 20000 entries, paths of 1 KiB at most) and in
it ``project.binary`` (``res://project.binary`` before Godot 4.4; 1 MiB at most; strings and
booleans only): the project's name and its custom user folder give the save folder (CONFIRMED or
EXPECTED_ABSENT by the rules; the folder names made safe as Godot makes them) and the name is
the identity. A ``.pck`` that cannot be read leaves the 0.10.0 way: the folder whose name
is the exe's (``<N>.exe`` beside ``<N>.pck``, the ``.pck`` starting ``GDPC``) is only a
candidate, for 「确认 / 关联…」 to confirm. The ``logs`` and caches are left out.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Any

from enginesave import roots
from enginesave.engines import common
from enginesave.engines.base import ScanContext
from enginesave.fsutil import UnsafePathError, safe_relative_component
from enginesave.models import DetectedGame, Evidence

USERDATA = "Godot/app_userdata"
LEFT_OUT = ["logs", "shader_cache", "vulkan"]
MAX_ENTRIES = 20000  # D69 1: a pack's file table
MAX_PATH = 1024
MAX_PROJECT = 1024 * 1024  # project.binary
PROJECT = "project.binary"  # as Godot keeps it: without "res://"
NAME = "application/config/name"
USE_CUSTOM = "application/config/use_custom_user_dir"
CUSTOM = "application/config/custom_user_dir_name"


ENTRY_FIXED = 4 + 16 + 16  # path length, offset and size, md5 (format 2 and 3: + 4 flags)
PACK_DIR_ENCRYPTED = 1  # format 2 and 3 flags: the file table itself is encrypted
NOT_A_PACK = "the .pck is not a pack EngineSave reads (another format)"
ENCRYPTED = "the .pck's file table is encrypted"
NO_PROJECT = "the .pck holds no project.binary"
DAMAGED_TABLE = "the .pck's file table is damaged"
DAMAGED_PROJECT = "the project.binary in the .pck is damaged"


def find_project(path: Path) -> tuple[tuple[int, int] | None, str]:
    """``((offset in the file, size), "")`` of ``project.binary`` in a ``.pck``, or ``(None,
    why)``. 1.0.0 (D70 3): the file table is read entry by entry -- a path too long for this
    reader is skipped, the reading stops at ``project.binary`` -- and only a table that does
    not fit in the file (or an encrypted one) is given up on. Paths with and without
    ``res://`` (Godot 4.4+) are both read."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(4 + 4 * 4)
            if len(head) < 20 or head[:4] != b"GDPC":
                return None, NOT_A_PACK
            version = struct.unpack_from("<I", head, 4)[0]
            base = 0
            if version == 1:
                f.read(16 * 4)
            elif version in (2, 3):
                flags, base = struct.unpack("<IQ", f.read(12))
                if flags & PACK_DIR_ENCRYPTED:
                    return None, ENCRYPTED
                if version == 3:  # the file table at the end
                    (directory,) = struct.unpack("<Q", f.read(8))
                    if directory >= size:
                        return None, DAMAGED_TABLE
                    f.seek(directory)
                else:
                    f.read(16 * 4)
            else:
                return None, NOT_A_PACK
            (count,) = struct.unpack("<I", f.read(4))
            entry = ENTRY_FIXED + (4 if version >= 2 else 0)
            if count * entry > size - f.tell():  # R11: no table bigger than the file
                return None, DAMAGED_TABLE
            for _ in range(count):
                (length,) = struct.unpack("<I", f.read(4))
                if f.tell() + length + entry - 4 > size:
                    return None, DAMAGED_TABLE
                if length > MAX_PATH:  # not a path this looks for: skipped, not given up
                    f.seek(length + entry - 4, os.SEEK_CUR)
                    continue
                name = f.read(length).rstrip(b"\0").decode("utf-8", "replace")
                offset, item = struct.unpack("<QQ", f.read(16))
                f.seek(entry - 4 - 16, os.SEEK_CUR)  # md5 (and flags)
                if name.removeprefix("res://") == PROJECT:
                    if base + offset + item > size:
                        return None, DAMAGED_PROJECT
                    return (base + offset, item), ""
            return None, NO_PROJECT
    except (OSError, struct.error, ValueError):
        return None, DAMAGED_TABLE


def _variant(data: bytes, at: int, end: int) -> str | bool | None:
    """A string or a boolean of Godot's binary variant format at ``data[at:end]``; ``None``
    for anything else. A length beyond ``end`` is damage (D72 R12): ValueError."""
    if at + 4 > end:
        raise ValueError("variant beyond its size")
    (kind,) = struct.unpack_from("<I", data, at)
    kind &= 0xFFFF
    if kind == 1:
        if at + 8 > end:
            raise ValueError("boolean beyond its size")
        return bool(struct.unpack_from("<I", data, at + 4)[0])
    if kind == 4:
        if at + 8 > end:
            raise ValueError("string beyond its size")
        (length,) = struct.unpack_from("<I", data, at + 4)
        if at + 8 + length > end:
            raise ValueError("string beyond its size")
        return data[at + 8 : at + 8 + length].decode("utf-8")
    return None


def read_project(pck: Path) -> tuple[dict[str, str | bool] | None, str]:
    """``(the strings and booleans of the pack's project.binary, "")``, or ``(None, why)``.
    Every key and value is checked against the size the file says it has (D72 R12): a name
    cut short is damage, not the project's name."""
    entry, why = find_project(pck)
    if entry is None:
        return None, why
    if entry[1] > MAX_PROJECT:
        return None, DAMAGED_PROJECT
    try:
        with open(pck, "rb") as f:
            f.seek(entry[0])
            data = f.read(entry[1])
        if len(data) != entry[1] or data[:4] != b"ECFG":
            return None, DAMAGED_PROJECT
        (count,) = struct.unpack_from("<I", data, 4)
        at, found = 8, {}
        for _ in range(min(count, MAX_ENTRIES)):
            (length,) = struct.unpack_from("<I", data, at)
            if at + 4 + length > len(data):
                raise ValueError("key beyond the file")
            key = data[at + 4 : at + 4 + length].decode("utf-8")
            at += 4 + length
            (size,) = struct.unpack_from("<I", data, at)
            if at + 4 + size > len(data):
                raise ValueError("value beyond the file")
            value = _variant(data, at + 4, at + 4 + size)
            at += 4 + size
            if value is not None:
                found[key] = value
        return found, ""
    except (OSError, struct.error, UnicodeDecodeError, ValueError):
        return None, DAMAGED_PROJECT


def project_settings(pck: Path) -> dict[str, str | bool] | None:
    """The strings and booleans of the pack's ``project.binary`` (``ECFG``), or ``None``."""
    return read_project(pck)[0]


INVALID = (":", "*", "?", '"', "<", ">", "|")


def safe_dir_name(text: str, allow_paths: bool = False) -> str:
    """Godot's ``OS::get_safe_dir_name``: the folder name Godot itself makes of a setting."""
    if allow_paths:  # separators kept, ".." not
        text = text.replace("\\", "/").replace("//", "/").strip()
        invalid: tuple[str, ...] = (*INVALID, "..")
    else:
        text = text.strip()
        text = {".": "dot", "..": "twodots"}.get(text, text)
        invalid = (*INVALID, "/", "\\")
    for char in invalid:
        text = text.replace(char, "-")
    return text.rstrip(".")


def user_folder(settings: dict[str, str | bool]) -> tuple[str, str] | None:
    """``(project name, folder below APPDATA)`` the project saves in, as Godot's
    ``OS::get_user_data_dir`` makes it: ``Godot/app_userdata/<name>``, or with a custom user
    folder ``<custom name>`` -- the project's own name when the custom one is empty."""
    name = settings.get(NAME)
    if not isinstance(name, str) or not name.strip():
        return None
    folder = safe_dir_name(name)
    if not folder:
        return None
    if settings.get(USE_CUSTOM) is True:
        custom = settings.get(CUSTOM)
        custom = safe_dir_name(custom, allow_paths=True) if isinstance(custom, str) else ""
        return name, custom.strip("/") or folder
    return name, f"{USERDATA}/{folder}"


def game_pck(directory: Path, listing: common.Listing) -> tuple[str, str] | None:
    """``(exe stem, pck name)`` of an ``<N>.exe`` beside an ``<N>.pck`` that starts ``GDPC``."""
    for exe in listing.exes():
        stem = Path(exe).stem
        pck = listing.file(f"{stem}.pck")
        if pck is not None and (common.read_head(directory / pck, 4) or b"") == b"GDPC":
            return stem, pck
    return None


def userdata_folder(stem: str, ctx: ScanContext) -> str | None:
    """The name of the ``app_userdata`` folder that is the exe's name, as it is on disk."""
    base, _how = roots.profile_root(roots.APPDATA, ctx.env)
    folder = common.list_folder(base.joinpath(*USERDATA.split("/"))) if base else None
    if folder is None:
        return None
    return folder.dir(stem)


class GodotDetector(common.EngineDetector):
    engine = "godot"
    engines = ("godot",)

    def recognise(
        self, directory: Path, listing: common.Listing, found: Any, ctx: ScanContext
    ) -> DetectedGame | None:
        pck = game_pck(directory, listing)
        if pck is None:
            return None
        stem, pck_name = pck
        evidence = [Evidence(pck_name, "engine_fingerprint", "Godot pack (GDPC)")]
        warnings: list[str] = []
        sources = []
        settings, why = read_project(directory / pck_name)
        found = user_folder(settings) if settings else None
        if settings is not None and found is None:
            why = "the project.binary in the .pck names no project"
        if found is not None:  # D69 1: the project says where it saves
            name, relative = found
            evidence.append(Evidence(pck_name, "project_setting", f"{NAME}={name}"))
            try:
                roots.check_relative(roots.APPDATA, relative)
                source = common.profile_source(
                    "userdata", roots.APPDATA, relative, ctx, evidence, warnings,
                    excludes=LEFT_OUT,
                )  # fmt: skip
            except UnsafePathError:
                source = None
            if source is not None:
                return common.rule_game(
                    directory, "godot", title=name, engine_key=common.identity_key(name),
                    sources=[source], evidence=evidence, warnings=warnings,
                )  # fmt: skip
        name = userdata_folder(stem, ctx)
        if name is not None:
            try:
                safe_relative_component(name)
            except UnsafePathError:
                name = None
        if name is None:
            warnings.append(  # D70 3: why the project's own folder is not known
                f"no %APPDATA%\\Godot\\app_userdata\\{stem}, and the save folder is not known "
                f"({why}): not backed up"
            )
        else:
            found_source = common.profile_source(
                "userdata", roots.APPDATA, f"{USERDATA}/{name}", ctx, evidence, warnings,
                excludes=LEFT_OUT, candidate=True,
            )  # fmt: skip
            sources += [found_source] if found_source is not None else []
        return common.rule_game(
            directory,
            "godot",
            title=stem,
            engine_key=None,  # an exe's name is not the project's: no automatic match
            sources=sources,
            evidence=evidence,
            warnings=warnings,
        )
