from __future__ import annotations

from pathlib import Path

import pytest

from enginesave.engines.renpy import paths, structure
from enginesave.identity import normalize_name, normalized_dir_name, strip_dir_suffixes
from fixtures.builder import build_engine_skeleton, write_text


def touch(path: Path, text: str = "") -> Path:
    write_text(path, text)
    return path


def test_renpy_root_requires_package_and_lib_or_launcher(tmp_path: Path) -> None:
    root = tmp_path / "g"
    touch(root / "renpy" / "__init__.py")
    assert structure.has_renpy_package(root)
    assert not structure.is_renpy_root(root)
    touch(root / "renpy.exe")
    assert not structure.is_renpy_root(root), "a renpy.* launcher alone is not a game launcher"
    touch(root / "Game.sh")
    assert structure.is_renpy_root(root)
    other = tmp_path / "h"
    (other / "renpy" / "common").mkdir(parents=True)
    (other / "lib").mkdir()
    assert structure.is_renpy_root(other)


def test_sdk_detection(tmp_path: Path) -> None:
    sdk = tmp_path / "renpy-sdk"
    build_engine_skeleton(sdk, "renpy", "8.1.3.23091805")
    (sdk / "launcher" / "game").mkdir(parents=True)
    assert structure.is_renpy_sdk(sdk)
    assert not structure.is_renpy_sdk(tmp_path)


def test_exe_basename_prefers_exe_with_matching_py(tmp_path: Path) -> None:
    for name in ["Zeta.exe", "Alpha-32.exe", "Alpha.py", "Beta.py", "renpy.py", "Gamma.sh"]:
        touch(tmp_path / name)
    assert structure.exe_basename(tmp_path) == "Alpha"
    (tmp_path / "Alpha.py").unlink()
    assert structure.exe_basename(tmp_path) == "Beta"
    for name in ["Beta.py", "Zeta.exe", "Alpha-32.exe"]:
        (tmp_path / name).unlink()
    assert structure.exe_basename(tmp_path) == "Gamma"


@pytest.mark.parametrize(
    ("name", "dirs", "expected"),
    [
        ("MoonlitHarbor", ["game", "MoonlitHarbor.app"], "game"),
        ("MyGame", ["MyGame", "game"], "MyGame"),
        ("Big Bad_Game", ["Game", "game_x"], "Game"),
        ("Big Bad_Game", ["Bad_Game", "Game"], "Bad_Game"),
        ("renpy", ["renpy", "data"], "data"),
        ("X", ["launcher/game"], "launcher/game"),
        ("X", [], "."),
        (None, ["data"], "data"),
    ],
)
def test_path_to_gamedir_candidate_order(
    tmp_path: Path, name: str | None, dirs: list[str], expected: str
) -> None:
    for d in dirs:
        (tmp_path / d).mkdir(parents=True)
    result = structure.path_to_gamedir(tmp_path, name)
    assert structure.relative_label(result, tmp_path) == expected


def test_engine_version_fallbacks(tmp_path: Path) -> None:
    root = tmp_path / "g"
    gamedir = root / "game"
    gamedir.mkdir(parents=True)
    assert structure.engine_version(root, gamedir) == (None, None)
    touch(root / "log.txt", "Mon\nPlatform\nRen'Py 7.4.11.2266\n")
    assert structure.engine_version(root, gamedir)[0] == "7.4.11.2266"
    touch(gamedir / "script_version.txt", "(8, 1, 3)")
    version, evidence = structure.engine_version(root, gamedir)
    assert (version, evidence.method, evidence.file) == (
        "8.1.3",
        "script_version",
        "game/script_version.txt",
    )
    touch(root / "renpy" / "vc_version.py", "nightly = False\nversion = '8.1.3.23091805'\n")
    version, evidence = structure.engine_version(root, gamedir)
    assert (version, evidence.method) == ("8.1.3.23091805", "vc_version")


def test_resolve_global_dir_branches(tmp_path: Path) -> None:
    game = tmp_path / "Library" / "Games" / "Portable"
    game.mkdir(parents=True)
    env = {"APPDATA": str(tmp_path / "AppData")}
    assert paths.resolve_global_dir(game, None, env=env, saves_root_override=None) == (
        None,
        "save_directory_empty",
    )
    assert paths.resolve_global_dir(game, "X", env=env, saves_root_override=None) == (
        tmp_path / "AppData" / "RenPy" / "X",
        "path_to_saves:APPDATA",
    )
    portable = tmp_path / "Library" / "Ren'Py Data"
    portable.mkdir()
    assert paths.resolve_global_dir(game, "X", env=env, saves_root_override=None) == (
        portable / "X",
        "portable:RenPy Data",
    )
    env_saves = {**env, "RENPY_PATH_TO_SAVES": str(tmp_path / "EnvSaves")}
    assert paths.resolve_global_dir(game, "X", env=env_saves, saves_root_override=None) == (
        tmp_path / "EnvSaves" / "X",
        "env:RENPY_PATH_TO_SAVES",
    )
    override = tmp_path / "Override"
    assert paths.resolve_global_dir(game, "X", env=env_saves, saves_root_override=override) == (
        override / "X",
        "override",
    )
    path, method = paths.resolve_global_dir(game, "X", env={}, saves_root_override=None)
    assert method == "portable:RenPy Data"
    portable.rmdir()
    path, method = paths.resolve_global_dir(game, "X", env={}, saves_root_override=None)
    assert (path, method) == (Path.home() / "RenPy" / "X", "path_to_saves:HOME")


def test_saves_root_override_precedence(tmp_path: Path) -> None:
    env = {paths.OVERRIDE_ENV: str(tmp_path / "from-env")}
    assert paths.saves_root_override(None, env) == tmp_path / "from-env"
    assert paths.saves_root_override(str(tmp_path / "cli"), env) == tmp_path / "cli"
    assert paths.saves_root_override(None, {}) is None


def test_name_normalization() -> None:
    assert normalize_name("Moonlit Harbor") == "moonlitharbor"
    assert normalize_name("Willow's Promise Beta") == "willowspromisebeta"
    assert normalize_name(None) == ""
    assert strip_dir_suffixes("Lanternfall-1574721269-steam") == "Lanternfall"
    assert strip_dir_suffixes("MorM-savefiles") == "MorM-savefiles"
    assert normalized_dir_name("Willowpromise-1637695238") == "willowpromise"
