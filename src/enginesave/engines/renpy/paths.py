"""Replica of Ren'Py's Windows ``path_to_saves`` plus EngineSave's saves-root override."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

OVERRIDE_ENV = "ENGINESAVE_RENPY_SAVES_ROOT"
PORTABLE_DIR = "Ren'Py Data"
TOKENS_DIR = "tokens"


def is_non_game_dir(name: str) -> bool:
    """Saves-root folders that never belong to a game: ``tokens``, ``persistent``, launcher*."""
    lower = name.lower()
    return lower in (TOKENS_DIR, "persistent") or lower.startswith("launcher")


ROOT_OVERRIDE_ENV = "ENGINESAVE_ROOT_RENPY_SAVES_ROOT"  # 0.10.0 (D65 1): what --root-override sets


def saves_root_override(cli_value: str | Path | None, env: Mapping[str, str]) -> Path | None:
    """``--renpy-saves-root`` wins over ``--root-override RENPY_SAVES_ROOT=…`` (which sets
    ``ENGINESAVE_ROOT_RENPY_SAVES_ROOT``), which wins over ``ENGINESAVE_RENPY_SAVES_ROOT``;
    none of them → ``None``."""
    if cli_value:
        return Path(cli_value)
    for name in (ROOT_OVERRIDE_ENV, OVERRIDE_ENV):
        if env.get(name):
            return Path(env[name])
    return None


def default_saves_root(env: Mapping[str, str], override: Path | None) -> tuple[Path, str]:
    """The machine-wide saves root (no per-game portable lookup)."""
    if override is not None:
        return override, "override"
    if env.get("RENPY_PATH_TO_SAVES"):
        return Path(env["RENPY_PATH_TO_SAVES"]), "env:RENPY_PATH_TO_SAVES"
    if env.get("APPDATA"):
        return Path(env["APPDATA"]) / "RenPy", "path_to_saves:APPDATA"
    return Path.home() / "RenPy", "path_to_saves:HOME"


def game_saves_root(
    game_root: Path | None, env: Mapping[str, str], override: Path | None
) -> tuple[Path, str]:
    """Saves root a game at ``game_root`` would use: override, env, ``Ren'Py Data``, APPDATA."""
    if override is None and not env.get("RENPY_PATH_TO_SAVES") and game_root is not None:
        for candidate in (game_root, *game_root.parents):
            if (candidate / PORTABLE_DIR).is_dir():
                return candidate / PORTABLE_DIR, "portable:RenPy Data"
    return default_saves_root(env, override)


def resolve_global_dir(
    game_root: Path,
    save_directory: str | None,
    *,
    env: Mapping[str, str],
    saves_root_override: Path | None,
) -> tuple[Path | None, str]:
    """``(path, method)`` of the global save directory; ``(None, ...)`` when it has none."""
    if not save_directory:
        return None, "save_directory_empty"
    root, method = game_saves_root(game_root, env, saves_root_override)
    return root / save_directory, method
