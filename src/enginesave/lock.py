"""The library's write lock (1.2.0, D73 4): one writer at a time.

Every command and window operation that writes into a library first creates
``<library>\\enginesave.lock`` exclusively (``O_EXCL``) with who holds it -- process id,
command, start time, computer -- and removes it when it ends, an error or Ctrl+C included. A
second writer finds the file and stops at once (:class:`LibraryBusy`, exit code 4) instead of
waiting or writing beside the first. Reading (``list``, ``show``, ``verify``, a scan with
``--repo``) never looks at it.

A lock whose process is gone (killed, a power cut) is stale: it is removed, with a line in the
log, and the new writer goes on. A lock of another computer (a library on a network drive) is never
judged stale here, since its process cannot be looked at; it is removed by hand. The same
process may take a lock it holds again (a restore's safety snapshot, a backup's pruning).

The lock file is the only file EngineSave writes into a library's top folder besides
``catalog.json`` and ``INDEX.txt``; a folder that holds only a lock still counts as empty for a
new library or mirror.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from enginesave import __version__
from enginesave.errors import EngineSaveError, FatalError
from enginesave.fsutil import describe_error, iso_utc, long_path, ns_from_iso, path_key, utc_now

LOCK_NAME = "enginesave.lock"
BUSY_EXIT_CODE = 4
# a lock file without readable contents (its writer stopped between creating and writing it)
# is stale once it is this old
UNREADABLE_STALE_SECONDS = 60
log = logging.getLogger("enginesave.lock")

_held: dict[str, tuple[int, Path, str]] = {}  # path key -> (depth, lock file, our token)
_guard = threading.Lock()


class LibraryBusy(EngineSaveError):
    """Another program is writing the library (exit code 4)."""

    exit_code = BUSY_EXIT_CODE

    def __init__(self, root: Path, holder: dict[str, Any], message: str | None = None) -> None:
        self.root = root
        self.holder = holder
        super().__init__(
            message or f"the library is busy: {describe_holder(holder)} is writing it ({root})"
        )


def describe_holder(holder: dict[str, Any]) -> str:
    """``enginesave backup (process 1234, since 2026-11-07T08:00:00Z)``, as far as known."""
    command = holder.get("command") or "another program"
    facts = []
    if holder.get("pid") is not None:
        facts.append(f"process {holder['pid']}")
    if holder.get("host") and holder.get("host") != socket.gethostname():
        facts.append(f"on {holder['host']}")
    if holder.get("started_at"):
        facts.append(f"since {holder['started_at']}")
    return f"{command} ({', '.join(facts)})" if facts else str(command)


def lock_path(root: Path) -> Path:
    return root / LOCK_NAME


def only_lock(folder: Path) -> bool:
    """``folder`` holds nothing but (maybe) a lock file: an empty folder for a new library."""
    try:
        return all(entry.name == LOCK_NAME for entry in folder.iterdir())
    except OSError:
        return False


def read_holder(path: Path) -> dict[str, Any] | None:
    """What a lock file says, ``{}`` when it cannot be read as such, ``None`` when it is gone."""
    try:
        with open(long_path(path), encoding="utf-8") as handle:
            text = handle.read(64 * 1024)
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _process_started_ns(pid: int) -> int | None | bool:
    """When process ``pid`` started (ns since 1970); ``True`` when it runs but the time is not
    known, ``False`` when there is no such process."""
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    query = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel32.OpenProcess(query, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        return False if error == 87 else True  # ERROR_INVALID_PARAMETER: no such process
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:
            return False  # it has ended (STILL_ACTIVE is 259)
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return True
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime  # 100 ns since 1601
        return (ticks - 116444736000000000) * 100
    finally:
        kernel32.CloseHandle(handle)


def stale(holder: dict[str, Any], path: Path) -> bool:
    """The lock's process is gone (or its process id now belongs to a process that started
    after the lock was taken). Another computer's lock is never stale here."""
    if not holder:  # unreadable: stale once it is old enough
        try:
            age = time.time() - os.stat(long_path(path)).st_mtime
        except OSError:
            return False
        return age > UNREADABLE_STALE_SECONDS
    host = holder.get("host")
    if isinstance(host, str) and host and host.casefold() != socket.gethostname().casefold():
        return False
    pid = holder.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    started = _process_started_ns(pid)
    if started is False:
        return True
    if started is True:
        return False
    try:
        taken = ns_from_iso(str(holder.get("started_at")))
    except ValueError:
        return False
    return started > taken + 2_000_000_000  # the id was given to a newer process


def _remove_stale(path: Path, holder: dict[str, Any]) -> bool:
    """Take the stale lock away (renamed first, so that two writers that both found it stale
    do not remove the lock one of them took meanwhile); True when it is gone."""
    aside = path.with_name(f"{LOCK_NAME}.stale-{secrets.token_hex(4)}")
    try:
        os.rename(long_path(path), long_path(aside))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if read_holder(aside) != holder:  # another writer's fresh lock: put it back
        with contextlib.suppress(OSError):
            os.rename(long_path(aside), long_path(path))
        return False
    with contextlib.suppress(OSError):
        os.remove(long_path(aside))
    log.warning(
        "removed the stale lock of %s in %s: its process is gone", describe_holder(holder), path
    )
    return True


def _create(path: Path, command: str) -> str:
    """Create the lock file exclusively; our token. Raises FileExistsError when it is there."""
    token = secrets.token_hex(8)
    data = {
        "pid": os.getpid(),
        "command": command,
        "started_at": iso_utc(utc_now()),
        "host": socket.gethostname(),
        "tool_version": __version__,
        "token": token,
    }
    fd = os.open(long_path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    try:
        os.write(fd, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return token


def acquire(root: Path, command: str) -> tuple[Path, str]:
    """Create the lock of ``root`` or raise :class:`LibraryBusy`; ``(lock file, token)``."""
    path = lock_path(root)
    for _attempt in range(3):
        try:
            return path, _create(path, command)
        except FileExistsError:
            pass
        except OSError as exc:
            raise FatalError(f"cannot create the lock {path}: {describe_error(exc)}") from exc
        holder = read_holder(path)
        if holder is None:  # released meanwhile
            continue
        if stale(holder, path) and _remove_stale(path, holder):
            continue
        raise LibraryBusy(root, _public(holder))
    raise LibraryBusy(root, _public(read_holder(path) or {}))


def _public(holder: dict[str, Any]) -> dict[str, Any]:
    """What ``--json`` says of the holder (not the token that tells our own lock apart)."""
    return {k: holder.get(k) for k in ("pid", "command", "started_at", "host", "tool_version")}


def release(path: Path, token: str) -> None:
    """Remove the lock file when it is still ours."""
    holder = read_holder(path)
    if holder is not None and holder.get("token") == token:
        with contextlib.suppress(OSError):
            os.remove(long_path(path))


@contextlib.contextmanager
def library_lock(root: Path, command: str, *, create: bool = False) -> Iterator[None]:
    """Hold the write lock of the library at ``root`` while the block runs. ``create`` makes
    the folder first (a new library); a folder made here that holds nothing afterwards is
    removed again. The same process taking it again only counts up."""
    root = Path(os.path.abspath(root))
    key = path_key(root)
    with _guard:
        if key in _held:
            depth, path, token = _held[key]
            _held[key] = (depth + 1, path, token)
            nested = True
        else:
            nested = False
    if nested:
        try:
            yield
        finally:
            with _guard:
                depth, path, token = _held[key]
                _held[key] = (depth - 1, path, token)
        return
    if not (root / "catalog.json").is_file() and root.exists() and not only_lock(root):
        # neither a library nor empty: never written into, not even a lock -- the command
        # itself refuses such a folder (a file, or someone else's folder)
        yield
        return
    made = False
    if create and not root.exists():
        try:
            root.mkdir(parents=True)
            made = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise FatalError(
                f"cannot create the library folder {root}: {describe_error(exc)}"
            ) from exc
    try:
        path, token = acquire(root, command)
    except BaseException:
        if made:
            with contextlib.suppress(OSError):
                root.rmdir()
        raise
    with _guard:
        _held[key] = (1, path, token)
    try:
        yield
    finally:
        with _guard:
            _held.pop(key, None)
        release(path, token)
        if made:
            with contextlib.suppress(OSError):
                root.rmdir()  # only when nothing was written into it


def holds(root: Path) -> bool:
    """This process holds the lock of ``root`` (tests)."""
    return path_key(Path(os.path.abspath(root))) in _held
