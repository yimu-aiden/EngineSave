"""The root tokens a save source is stored relative to (0.10.0, D65 1; S-035): one registry for
the scan, backup, restore, export, cleanup and the mirror's checks.

A source is kept as a token and a relative path, never as this machine's absolute path (except
``ABSOLUTE``), so that a backup goes back to the right place on another machine or for another
user. ``GAME_ROOT`` is the game's own folder (``.`` is the folder itself, for a source that
takes only some files of it by name, D65 2); the others are folders of the user profile, each
found in this order:

1. ``--root-override TOKEN=PATH`` on the command line (repeatable; it sets the variable below
   for the one command, so it wins over a variable set before);
2. the environment variable ``ENGINESAVE_ROOT_<TOKEN>``;
3. the system default.

``RENPY_SAVES_ROOT`` keeps its own options, which mean the same: ``--renpy-saves-root`` (first)
and ``ENGINESAVE_RENPY_SAVES_ROOT``; without any of them Ren'Py's own rules apply (its
``RENPY_PATH_TO_SAVES``, a portable ``Ren'Py Data`` folder, ``%APPDATA%\\RenPy``).

A manifest written by a later EngineSave may name a token this one does not know: such a source
is refused, with the reason, rather than restored to a guessed place.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path

from enginesave.errors import UsageError
from enginesave.fsutil import UnsafePathError, safe_relative_component, split_relative

GAME_ROOT = "GAME_ROOT"
ABSOLUTE = "ABSOLUTE"
RENPY_SAVES_ROOT = "RENPY_SAVES_ROOT"
UNITY_LOCALLOW = "UNITY_LOCALLOW"  # %USERPROFILE%\AppData\LocalLow (Unity)
LOCALAPPDATA = "LOCALAPPDATA"  # %LOCALAPPDATA% (Unreal)
APPDATA = "APPDATA"  # %APPDATA% (Electron, Godot)
# 0.11.0 (D69 3): the known folders 「文档」 and 「保存的游戏」 (read with SHGetKnownFolderPath, so
# a Documents moved by OneDrive is found too), and %LOCALAPPDATA%\VirtualStore
DOCUMENTS = "DOCUMENTS"
SAVED_GAMES = "SAVED_GAMES"
VIRTUALSTORE = "VIRTUALSTORE"
PROFILE_DEFAULTS = {"DOCUMENTS": "Documents", "SAVED_GAMES": "Saved Games"}
KNOWN_FOLDERS = {  # FOLDERID_Documents, FOLDERID_SavedGames
    DOCUMENTS: "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    SAVED_GAMES: "{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}",
}

ENGINE_PROFILE_TOKENS = (  # other engines (D65 1, D69 3)
    UNITY_LOCALLOW, LOCALAPPDATA, APPDATA, DOCUMENTS, SAVED_GAMES, VIRTUALSTORE,
)  # fmt: skip
PROFILE_TOKENS = (RENPY_SAVES_ROOT, *ENGINE_PROFILE_TOKENS)
TOKENS = (GAME_ROOT, *PROFILE_TOKENS, ABSOLUTE)
ENV_PREFIX = "ENGINESAVE_ROOT_"
ROOT_ITSELF = "."  # GAME_ROOT: the game folder itself (a source with patterns)


class UnknownToken(UnsafePathError):
    """A root token this EngineSave does not know (written by a later version)."""


def env_name(token: str) -> str:
    return f"{ENV_PREFIX}{token}"


def parse_overrides(values: Sequence[str]) -> dict[str, str]:
    """``--root-override`` values (``TOKEN=PATH``) by token; a bad one is a usage error."""
    found: dict[str, str] = {}
    for value in values:
        token, sep, path = value.partition("=")
        token, path = token.strip().upper(), path.strip()
        if not sep or not path:
            raise UsageError(f"--root-override wants TOKEN=PATH, not {value!r}")
        if token not in PROFILE_TOKENS:
            raise UsageError(
                f"--root-override: unknown root token {token!r} "
                f"(one of {', '.join(PROFILE_TOKENS)})"
            )
        if not Path(path).is_absolute():
            raise UsageError(f"--root-override {token} needs an absolute path, not {path!r}")
        found[token] = os.path.abspath(path)
    return found


@contextlib.contextmanager
def overriding(values: Sequence[str]) -> Iterator[None]:
    """``--root-override`` for one command: ``ENGINESAVE_ROOT_<TOKEN>`` set in this process
    while it runs, the earlier values put back afterwards."""
    found = parse_overrides(values)
    saved = {env_name(token): os.environ.get(env_name(token)) for token in found}
    try:
        for token, path in found.items():
            os.environ[env_name(token)] = path
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def profile_root(token: str, env: Mapping[str, str]) -> tuple[Path | None, str]:
    """``(folder, how it was found)`` of a profile token on this machine; ``(None, why)`` when
    it cannot be found (the variable it comes from is not set). ``RENPY_SAVES_ROOT`` is the
    machine-wide saves root here; a game's own (portable) one is ``paths.game_saves_root``."""
    if token == RENPY_SAVES_ROOT:
        from enginesave.engines.renpy.paths import default_saves_root, saves_root_override

        return default_saves_root(env, saves_root_override(None, env))
    if token not in PROFILE_TOKENS:
        raise UnknownToken(f"not a profile root token: {token!r}")
    override = env.get(env_name(token))
    if override:  # the same rule as --root-override: an absolute path, or nothing
        if not Path(override).is_absolute():
            return None, f"{env_name(token)} is not an absolute path ({override})"
        return Path(override), f"env:{env_name(token)}"
    if token == UNITY_LOCALLOW:
        profile = env.get("USERPROFILE")
        if not profile:
            return None, "USERPROFILE is not set"
        return Path(profile) / "AppData" / "LocalLow", "default:USERPROFILE"
    if token in KNOWN_FOLDERS:
        found = known_folder(KNOWN_FOLDERS[token])
        if found is not None:
            return found, "known folder"
        # D70 7: Windows did not say -- the profile's own folder, when it is there
        profile = env.get("USERPROFILE")
        fallback = Path(profile) / PROFILE_DEFAULTS[token] if profile else None
        if fallback is not None and fallback.is_dir():
            return fallback, "default:USERPROFILE"
        return None, f"no {token} folder"
    if token == VIRTUALSTORE:
        base, how = profile_root(LOCALAPPDATA, env)
        return (base / "VirtualStore", f"{how}\\VirtualStore") if base else (None, how)
    value = env.get(token)
    if not value:
        return None, f"{token} is not set"
    return Path(value), f"default:{token}"


def known_folder(folder_id: str) -> Path | None:
    """A Windows known folder by its id (``SHGetKnownFolderPath``, read only), or ``None``."""
    import sys

    if sys.platform != "win32":
        return None
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [  # noqa: RUF012 - ctypes wants a list
            ("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD),
            ("data4", ctypes.c_ubyte * 8),
        ]  # fmt: skip

    raw = uuid.UUID(folder_id).bytes_le
    guid = GUID.from_buffer_copy(raw)
    path = ctypes.c_wchar_p()
    try:
        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) != 0:
            return None
        try:
            return Path(path.value) if path.value else None
        finally:
            ole32.CoTaskMemFree(path)
    except (AttributeError, OSError):
        return None


CONFLICT_WORDS = {  # the command line's; the window gives its own (1.0.0, D70 8)
    "scan": "{label} overlaps the scan folder {root}",
    "library": "{label} overlaps the library {root}",
    "token": "{label} is the folder of {other} too",
}


def override_conflicts(
    env: Mapping[str, str],
    scan_roots: Sequence[Path] = (),
    libraries: Sequence[Path] = (),
    words: Mapping[str, str] | None = None,
) -> list[str]:
    """D66 7: a token moved by ``--root-override`` or ``ENGINESAVE_ROOT_<TOKEN>`` onto a scan
    folder or a library (inside it or around it), or onto another token's own folder: each such
    conflict in words (a restore or a cleanup there would write into the wrong place) --
    ``words`` (templates with ``{label}``, ``{root}``, ``{other}``) or the English ones."""
    said = {**CONFLICT_WORDS, **(words or {})}
    from enginesave.fsutil import is_within, path_key

    def overlap(a: Path, b: Path) -> bool:
        return is_within(a, b) or is_within(b, a)

    moved = {  # M44 review: a relative value is never used (profile_root), so no conflict
        t: Path(env[env_name(t)])
        for t in PROFILE_TOKENS
        if env.get(env_name(t)) and Path(env[env_name(t)]).is_absolute()
    }
    problems = []
    for token, path in moved.items():
        label = f"{env_name(token)} ({path})"
        problems += [
            said["scan"].format(label=label, root=r) for r in scan_roots if overlap(path, r)
        ]
        problems += [
            said["library"].format(label=label, root=r) for r in libraries if overlap(path, r)
        ]
        for other in PROFILE_TOKENS:
            if other == token:
                continue
            theirs, _how = profile_root(other, env)  # %APPDATA%\RenPy is in %APPDATA%: equal only
            if theirs is not None and path_key(path) == path_key(theirs):
                problems.append(said["token"].format(label=label, other=other))
    return problems


def check_relative(token: str, relative: str) -> None:
    """A manifest's ``(root_token, relative_root)`` is one this EngineSave can place safely;
    raises :class:`UnsafePathError` (:class:`UnknownToken` for a token it does not know)."""
    if token == RENPY_SAVES_ROOT:
        safe_relative_component(relative)
    elif token == GAME_ROOT:
        if relative != ROOT_ITSELF:
            split_relative(relative)
    elif token in PROFILE_TOKENS:
        split_relative(relative)
    elif token == ABSOLUTE:
        path = Path(relative)
        if not path.is_absolute() or ".." in path.parts:
            raise UnsafePathError(f"not a clean absolute path: {relative!r}")
    else:
        raise UnknownToken(
            f"unknown root token {token!r}: this snapshot was made by a later EngineSave; "
            "update EngineSave to restore it"
        )


def join_relative(base: Path, relative: str) -> Path:
    """``base`` with a checked relative path below it; ``.`` is ``base`` itself."""
    if relative == ROOT_ITSELF:
        return base
    return base.joinpath(*split_relative(relative))


def profile_folders(templates: Iterable[object], env: Mapping[str, str]) -> list[Path]:
    """The folders outside the game folder that a record's ``source_templates`` name (another
    engine's, D65 4); one that cannot be found or placed safely is left out."""
    found = []
    for template in templates:
        if not isinstance(template, Mapping):
            continue
        token = template.get("root_token")
        if token not in ENGINE_PROFILE_TOKENS:
            continue
        with contextlib.suppress(UnsafePathError):
            path, _ = resolve(str(token), str(template.get("relative_root") or ""), env)
            if path is not None:
                found.append(path)
    return found


def resolve(
    token: str, relative: str, env: Mapping[str, str], game_root: Path | None = None
) -> tuple[Path | None, str]:
    """Where a source stored as ``(token, relative)`` lives here: below the game folder, a
    profile folder, or the absolute path itself. ``(None, why)`` when the folder it is
    relative to cannot be found."""
    check_relative(token, relative)
    if token == ABSOLUTE:
        return Path(relative), "original absolute path"
    if token == GAME_ROOT:
        if game_root is None:
            return None, "the game folder is not known"
        return join_relative(game_root, relative), "game folder"
    base, how = profile_root(token, env)
    if base is None:
        return None, how
    return join_relative(base, relative), how
