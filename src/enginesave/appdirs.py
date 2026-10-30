"""Where EngineSave keeps its own files (D28): a portable folder or ``%APPDATA%\\EngineSave``.

A portable build (PyInstaller sets ``sys.frozen``) with a ``EngineSave.portable`` file next to
its executables keeps the window's settings and logs in ``config\\`` beside them, so the whole
folder can move to a USB stick or another computer. Everything else (a source checkout, an
installed package, a frozen build without the marker) uses ``%APPDATA%\\EngineSave``. The
command line keeps one file there, the scan's parse cache ``scan-cache.json`` (0.9.0, D52 3),
which it shares with the window.
"""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path

APP_DIR_NAME = "EngineSave"
PORTABLE_MARKER = "EngineSave.portable"
PORTABLE_CONFIG = "config"
FOLDERID_DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"
# EngineSave was called SaveTrail up to 1.0.0: its settings and mirror times are taken over once
LEGACY_APP_DIR_NAME = "SaveTrail"
CARRIED_OVER = ("gui.json", "mirrors.json")


def frozen() -> bool:
    """Running from a PyInstaller build."""
    return bool(getattr(sys, "frozen", False))


def executable_dir() -> Path:
    return Path(sys.executable).resolve().parent


def portable() -> bool:
    """A frozen build with the marker file beside its executable."""
    return frozen() and (executable_dir() / PORTABLE_MARKER).is_file()


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """``<exe folder>\\config`` for a portable build, otherwise ``%APPDATA%\\EngineSave``
    (``~\\AppData\\Roaming\\EngineSave`` without APPDATA)."""
    if portable():
        return executable_dir() / PORTABLE_CONFIG
    env = os.environ if env is None else env
    base = env.get("APPDATA")
    return (Path(base) if base else Path.home() / "AppData" / "Roaming") / APP_DIR_NAME


def carry_over_settings(env: Mapping[str, str] | None = None) -> list[str]:
    """Copy SaveTrail's ``gui.json`` and ``mirrors.json`` from ``%APPDATA%\\SaveTrail`` into
    ``%APPDATA%\\EngineSave`` when EngineSave has none of its own yet; the names copied.

    The old folder is only read. A file EngineSave already has is never replaced (the copy is
    created exclusively), and a portable build keeps its own ``config\\``. A file that cannot
    be read or written is left out: the window then starts with its defaults.
    """
    if portable():
        return []
    new = config_dir(env)
    old = new.with_name(LEGACY_APP_DIR_NAME)
    copied = []
    for name in CARRIED_OVER:
        source, target = old / name, new / name
        try:
            if target.exists() or not source.is_file():
                continue
            data = source.read_bytes()
            new.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as out:
                out.write(data)
        except OSError:
            continue
        copied.append(name)
    return copied


def assets_dir() -> Path:
    """``assets\\`` of the source checkout, or the copy inside a frozen build."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", executable_dir())) / "assets"
    return Path(__file__).resolve().parents[2] / "assets"


def documents_dir() -> Path:
    """The user's Documents folder where Windows keeps it (OneDrive included), else
    ``~\\Documents``. Only asks Windows for the path; nothing is created or read."""
    if sys.platform == "win32":
        try:
            shell32 = ctypes.WinDLL("shell32")  # type: ignore[attr-defined]
            ole32 = ctypes.WinDLL("ole32")  # type: ignore[attr-defined]
            shell32.SHGetKnownFolderPath.argtypes = [
                ctypes.c_char_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            shell32.SHGetKnownFolderPath.restype = ctypes.c_long
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            found = ctypes.c_wchar_p()
            guid = uuid.UUID(FOLDERID_DOCUMENTS).bytes_le
            if shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(found)) == 0:
                try:
                    if found.value:
                        return Path(found.value)
                finally:
                    ole32.CoTaskMemFree(ctypes.cast(found, ctypes.c_void_p))
        except (AttributeError, OSError):
            pass
    return Path.home() / "Documents"
