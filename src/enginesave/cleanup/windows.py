"""The Windows calls a cleanup needs, each small enough to be replaced in tests: the shell's
recycle operation, the drive type, the recycle bin's size (read only, D35), the running
processes and the rename before a permanent delete.

Tests replace these functions *here* (``enginesave.cleanup.windows``); the test suite's
``conftest.py`` makes ``shell_file_operation`` refuse, so no test reaches the real recycle bin
(rule 16)."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from enginesave.cleanup.model import RecycleBin

BITBUCKET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"
DRIVE_FIXED = 3
FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
RECYCLE_FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT


# --------------------------------------------------------------------------- Windows helpers


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_void_p),
    ]


def shell_file_operation(operation: SHFILEOPSTRUCTW) -> int:
    """``SHFileOperationW``; a module attribute so tests replace it (rule 16)."""
    return int(ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation)))  # type: ignore[attr-defined]


def has_recycle_bin(anchor: str) -> bool:
    """The volume keeps a ``$RECYCLE.BIN`` folder (module attribute, replaced in tests)."""
    return (Path(anchor) / "$RECYCLE.BIN").is_dir()


def rename_dir(source: str, target: str) -> None:
    """``os.rename`` for the permanent delete (module attribute, replaced in tests)."""
    os.rename(source, target)


def drive_type(anchor: str) -> int:
    """``GetDriveTypeW`` of a volume root such as ``E:\\`` (0 when unknown)."""
    if sys.platform != "win32":
        return 0
    return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(anchor)))  # type: ignore[attr-defined]


def volume_guid(anchor: str) -> str | None:
    """``{GUID}`` of the volume mounted at ``anchor`` (``GetVolumeNameForVolumeMountPointW``)."""
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        call = kernel32.GetVolumeNameForVolumeMountPointW
        call.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        call.restype = ctypes.c_int
        buffer = ctypes.create_unicode_buffer(64)
        if not call(anchor, buffer, len(buffer)):
            return None
    except (AttributeError, OSError):
        return None
    name = buffer.value  # \\?\Volume{...}\
    start, end = name.find("{"), name.find("}")
    return name[start : end + 1] if 0 <= start < end else None


def read_bitbucket(guid: str) -> tuple[int | None, bool]:
    """``(MaxCapacity in bytes, NukeOnDelete)`` of a volume's recycle bin, read from
    ``HKCU\\...\\BitBucket\\Volume\\{GUID}``; ``(None, False)`` when it cannot be read."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{BITBUCKET_KEY}\\{guid}") as key:
            values = {}
            for name in ("MaxCapacity", "NukeOnDelete"):
                try:
                    values[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    values[name] = None
    except (ImportError, OSError):
        return None, False
    capacity = values["MaxCapacity"]
    megabytes = capacity if isinstance(capacity, int) and capacity > 0 else None
    return (megabytes * 1024**2 if megabytes else None), values["NukeOnDelete"] == 1


class SHQUERYRBINFO(ctypes.Structure):
    _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8  # shellapi.h packs it on x86
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("i64Size", ctypes.c_int64),
        ("i64NumItems", ctypes.c_int64),
    ]


def recycle_bin_usage(anchor: str) -> int | None:
    """Bytes now in the recycle bin of ``anchor`` (``SHQueryRecycleBinW``; only asks)."""
    if sys.platform != "win32":
        return None
    try:
        shell32 = ctypes.WinDLL("shell32")  # type: ignore[attr-defined]
        query = shell32.SHQueryRecycleBinW
        query.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(SHQUERYRBINFO)]
        query.restype = ctypes.c_long
        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(info)
        if query(anchor, ctypes.byref(info)) != 0:
            return None
    except (AttributeError, OSError):
        return None
    return int(info.i64Size)


def read_recycle_bin(anchor: str) -> RecycleBin:
    """The recycle bin of the drive ``anchor`` (``E:\\``): its capacity, what it holds and
    whether it is on. Only reads (the registry and ``SHQueryRecycleBinW``)."""
    info = RecycleBin(anchor)
    guid = volume_guid(anchor)
    if guid is not None:
        info.capacity, info.disabled = read_bitbucket(guid)
    if info.capacity is not None:
        info.used = recycle_bin_usage(anchor)
    return info


recycle_bin_info = read_recycle_bin  # what the checks call; a module attribute tests replace


def list_process_images() -> list[str] | None:
    """Full image paths of the running processes we may query; ``None`` when unknown.

    Uses private ``WinDLL`` instances so the prototypes set here affect no other code.
    """
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
        dword = ctypes.c_uint32
        psapi.EnumProcesses.argtypes = [ctypes.POINTER(dword), dword, ctypes.POINTER(dword)]
        psapi.EnumProcesses.restype = ctypes.c_int
        kernel32.OpenProcess.argtypes = [dword, ctypes.c_int, dword]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            dword,
            ctypes.c_wchar_p,
            ctypes.POINTER(dword),
        ]
        kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        ids = (dword * 8192)()
        needed = dword()
        if not psapi.EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(needed)):
            return None
        images = []
        for pid in ids[: needed.value // ctypes.sizeof(dword)]:
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                continue
            try:
                buffer = ctypes.create_unicode_buffer(32768)
                size = dword(len(buffer))
                if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    images.append(buffer.value)
            finally:
                kernel32.CloseHandle(handle)
        return images
    except (AttributeError, OSError):
        return None


def user_sid() -> str | None:
    """The current user's SID (``S-1-5-21-…``), whose folder in ``$RECYCLE.BIN`` holds what
    this user recycled (D41); ``None`` when it cannot be read. Only the process token is read."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return None  # TOKEN_QUERY refused
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                return None
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
            text = ctypes.c_void_p()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                return None
            try:
                return ctypes.wstring_at(text.value)
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError):
        return None
