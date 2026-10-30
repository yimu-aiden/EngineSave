"""Ren'Py distribution layout: root/SDK detection, ``path_to_gamedir`` replica, engine version."""

from __future__ import annotations

import os
import re
from pathlib import Path

from enginesave.engines.renpy.rpa_reader import has_rpa_magic
from enginesave.fsutil import is_reparse_point, iter_files, long_path
from enginesave.models import Evidence

SCRIPT_SUFFIXES = (".rpy", ".rpyc", ".rpa")
MAX_SMALL_FILE = 64 * 1024
_VC_VERSION_RE = re.compile(r"""^\s*version\s*=\s*[uU]?['"]([^'"]+)['"]""", re.M)
_LOG_VERSION_RE = re.compile(r"Ren'Py\s+(\d+(?:\.\d+)+)")


def has_renpy_package(directory: Path) -> bool:
    renpy = directory / "renpy"
    return renpy.is_dir() and ((renpy / "__init__.py").is_file() or (renpy / "common").is_dir())


def is_renpy_sdk(directory: Path) -> bool:
    return has_renpy_package(directory) and (directory / "launcher" / "game").is_dir()


def launcher_stems(directory: Path) -> tuple[list[str], list[str], list[str]]:
    """Stems of top-level ``*.exe`` (minus ``-32``), ``*.py`` and ``*.sh``; ``renpy`` excluded."""
    exes: list[str] = []
    pys: list[str] = []
    shs: list[str] = []
    try:
        with os.scandir(directory) as it:
            names = sorted(e.name for e in it if e.is_file(follow_symlinks=False))
    except OSError:
        return exes, pys, shs
    for name in names:
        stem, dot, suffix = name.rpartition(".")
        if not dot or stem.lower() == "renpy":
            continue
        suffix = suffix.lower()
        if suffix == "exe":
            exes.append(stem.removesuffix("-32"))
        elif suffix == "py":
            pys.append(stem)
        elif suffix == "sh":
            shs.append(stem)
    return exes, pys, shs


def is_renpy_root(directory: Path) -> bool:
    """``renpy/`` package plus ``lib/`` or at least one non-``renpy`` launcher script."""
    if not has_renpy_package(directory):
        return False
    return (directory / "lib").is_dir() or any(launcher_stems(directory))


def exe_basename(directory: Path) -> str | None:
    """Launcher base name; prefers an ``.exe`` that has a same-named ``.py``."""
    exes, pys, shs = launcher_stems(directory)
    py_by_lower = {p.lower(): p for p in pys}
    paired = sorted(py_by_lower[e.lower()] for e in exes if e.lower() in py_by_lower)
    for group in (paired, pys, exes, shs):
        if group:
            return sorted(group)[0]
    return None


def path_to_gamedir(basedir: Path, name: str | None) -> Path:
    """Replica of the launcher script's ``path_to_gamedir(basedir, name)``.

    Candidates in order: ``name``; ``name`` with each prefix ending at a space or underscore
    removed; ``game``; ``data``; ``launcher/game``. ``renpy`` is skipped. The first existing
    directory wins, otherwise ``basedir`` itself.
    """
    candidates: list[str] = []
    if name:
        candidates.append(name)
        rest = name
        while rest:
            prefix, rest = rest[0], rest[1:]
            if prefix in (" ", "_"):
                candidates.append(rest)
    candidates.extend(["game", "data", "launcher/game"])
    for candidate in candidates:
        if candidate == "renpy":
            continue
        path = basedir / candidate
        if path.is_dir():
            return path
    return basedir


def gamedir_has_scripts(gamedir: Path, skip_top: frozenset[str]) -> bool:
    return any(
        entry.rel.lower().endswith(SCRIPT_SUFFIXES)
        for entry in iter_files(gamedir, skip_top=skip_top)
    )


def renamed_archives(gamedir: Path) -> list[Path]:
    """D53 1: files at the top of ``game/`` that are RPA archives under another name (such as
    ``archive.liu``), found by their first 8 bytes."""
    try:
        with os.scandir(long_path(gamedir)) as it:
            entries = sorted(it, key=lambda e: e.name.casefold())
    except OSError:
        return []
    found = []
    for entry in entries:
        if is_reparse_point(entry) or not entry.is_file(follow_symlinks=False):
            continue
        path = gamedir / entry.name
        if not entry.name.lower().endswith(SCRIPT_SUFFIXES) and has_rpa_magic(path):
            found.append(path)
    return found


def read_small_text(path: Path, limit: int = MAX_SMALL_FILE) -> str | None:
    try:
        with open(long_path(path), "rb") as f:
            return f.read(limit).decode("utf-8-sig", "replace")
    except OSError:
        return None


def relative_label(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:
        return str(path)


def engine_version(root: Path, gamedir: Path) -> tuple[str | None, Evidence | None]:
    """``renpy/vc_version.py`` → ``<gamedir>/script_version.txt`` → ``log.txt`` (first lines)."""
    text = read_small_text(root / "renpy" / "vc_version.py")
    if text and (match := _VC_VERSION_RE.search(text)):
        version = match.group(1)
        return version, Evidence("renpy/vc_version.py", "vc_version", f"version = '{version}'")
    script_version = gamedir / "script_version.txt"
    text = read_small_text(script_version)
    if text and (numbers := re.findall(r"\d+", text)):
        label = relative_label(script_version, root)
        return ".".join(numbers), Evidence(label, "script_version", text.strip()[:60])
    text = read_small_text(root / "log.txt")
    for line in (text or "").splitlines()[:10]:
        if match := _LOG_VERSION_RE.search(line):
            return match.group(1), Evidence("log.txt", "log_txt", line.strip()[:120])
    return None, None
