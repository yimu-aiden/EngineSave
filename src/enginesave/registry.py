"""Read-only access to one registry key and its export as a ``.reg`` file (0.10.0, D65 second
tier: Unity's PlayerPrefs). EngineSave never writes the registry (1.3): a backup keeps the
``.reg`` file; a restore puts the file back and the user imports it by double-clicking.

The key is opened with ``KEY_READ`` and its values are read as raw bytes. ``winreg`` is not
used for the values: it reads a ``REG_DWORD`` as 4 bytes, and Unity writes a float preference
as an 8-byte double under that type, so a ``winreg`` export would lose every float.

:data:`reader` is what the scan and the backup call; tests put a fake in its place (the tests
never write the registry either).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

REG_FILE = "PlayerPrefs.reg"
HEADER = "Windows Registry Editor Version 5.00"
HKCU = "HKEY_CURRENT_USER"

REG_NONE, REG_SZ, REG_EXPAND_SZ, REG_BINARY, REG_DWORD = 0, 1, 2, 3, 4
REG_MULTI_SZ, REG_QWORD = 7, 11
WRAP = 80  # regedit's line width for hex values


@dataclass(frozen=True)
class Value:
    name: str
    kind: int  # REG_* type
    data: bytes


def read_values(subkey: str) -> list[Value] | None:
    """The values of ``HKEY_CURRENT_USER\\<subkey>`` (not its subkeys), raw; ``None`` when the
    key is not there (or this is not Windows)."""
    if sys.platform != "win32":
        return None
    import ctypes
    import winreg
    from ctypes import wintypes

    try:  # opened read-only by winreg; its values read raw through the same handle
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ)
    except (OSError, ValueError):  # not there, or not a key name (a NUL in it, D66 3)
        return None
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    with key:
        handle = wintypes.HKEY(int(key))
        count, name_max, data_max = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        status = advapi.RegQueryInfoKeyW(
            handle, None, None, None, None, None, None,
            ctypes.byref(count), ctypes.byref(name_max), ctypes.byref(data_max), None, None,
        )  # fmt: skip
        if status != 0:
            return None
        values = []
        for index in range(count.value):
            name = ctypes.create_unicode_buffer(name_max.value + 1)
            name_len = wintypes.DWORD(name_max.value + 1)
            data = (ctypes.c_ubyte * max(data_max.value, 1))()
            data_len = wintypes.DWORD(max(data_max.value, 1))
            kind = wintypes.DWORD()
            status = advapi.RegEnumValueW(
                handle, index, name, ctypes.byref(name_len), None,
                ctypes.byref(kind), data, ctypes.byref(data_len),
            )  # fmt: skip
            if status != 0:
                return None  # changed while being read: read nothing rather than half
            values.append(Value(name.value, kind.value, bytes(data[: data_len.value])))
        return values


reader: Callable[[str], list[Value] | None] = read_values


def _quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _hex(prefix: str, data: bytes) -> str:
    """``prefix`` + comma-separated bytes, wrapped as regedit does (a ``\\`` at the end of a
    line, two spaces before the next)."""
    parts = [f"{b:02x}" for b in data]
    lines: list[str] = []
    line = prefix
    for i, part in enumerate(parts):
        piece = part + ("," if i < len(parts) - 1 else "")
        if len(line) + len(piece) > WRAP - 2 and line.strip():
            lines.append(line + "\\")
            line = "  "
        line += piece
    lines.append(line)
    return "\r\n".join(lines)


def _string(data: bytes) -> str | None:
    """A ``REG_SZ`` that regedit can write as text: UTF-16 ending in one NUL, none inside, no
    line break and no lone surrogate (anything else is written as bytes)."""
    if len(data) % 2 or not data.endswith(b"\x00\x00"):
        return None
    try:
        text = data[:-2].decode("utf-16-le", errors="strict")
    except UnicodeDecodeError:
        return None
    return None if any(c in text for c in "\x00\r\n") else text


def value_line(value: Value) -> str:
    name = "@" if value.name == "" else _quoted(value.name)
    if value.kind == REG_SZ and (text := _string(value.data)) is not None:
        return f"{name}={_quoted(text)}"
    if value.kind == REG_DWORD and len(value.data) == 4:
        return f"{name}=dword:{int.from_bytes(value.data, 'little'):08x}"
    if value.kind == REG_BINARY:
        return _hex(f"{name}=hex:", value.data)
    return _hex(f"{name}=hex({value.kind:x}):", value.data)  # as it is, whatever its type


def export(subkey: str, values: Sequence[Value]) -> bytes:
    """A ``.reg`` file of the values, as regedit writes one: UTF-16LE with a BOM, CRLF."""
    lines = [HEADER, "", f"[{HKCU}\\{subkey}]"]
    for value in sorted(values, key=lambda v: v.name.casefold()):
        if "\r" in value.name or "\n" in value.name:  # no .reg form: regedit could not import it
            lines.append("; left out: a value whose name has a line break")
        else:
            lines.append(value_line(value))
    text = "\r\n".join([*lines, "", ""])
    return b"\xff\xfe" + text.encode("utf-16-le", errors="surrogatepass")


def is_key_path(path: str) -> bool:
    """``path`` names a registry key (a registry source's own path; the safety snapshot before
    a restore keeps the source's kind but names the game folder)."""
    return path.upper().startswith(f"{HKCU}\\")


def subkey_of(path: str) -> str:
    """``Software\\Co\\Game`` of a source path ``HKEY_CURRENT_USER\\Software\\Co\\Game``."""
    prefix = f"{HKCU}\\"
    return path[len(prefix) :] if path.upper().startswith(prefix) else path


def export_key(subkey: str) -> bytes | None:
    """The ``.reg`` export of a key, ``None`` when it is not there."""
    values = reader(subkey)
    return export(subkey, values) if values is not None else None
