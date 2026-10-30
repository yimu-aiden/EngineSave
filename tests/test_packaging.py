"""Portable build (D28, plan section 19 M15): configuration folders, the spec, packaging and
``--self-test``. PyInstaller itself never runs here (rule 17); the spec is checked statically.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

from enginesave import appdirs
from enginesave.gui.settings import log_dir, settings_path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"


def make_frozen(monkeypatch: pytest.MonkeyPatch, folder: Path, *, marker: bool) -> Path:
    """Pretend to run as ``folder/EngineSave.exe`` of a PyInstaller build."""
    folder.mkdir(parents=True, exist_ok=True)
    if marker:
        (folder / appdirs.PORTABLE_MARKER).write_text("marker", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(folder / "EngineSave.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(folder / "_internal"), raising=False)
    return folder


def test_config_dir_is_appdata_unless_a_portable_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"APPDATA": str(tmp_path / "Roaming")}
    roaming = tmp_path / "Roaming" / "EngineSave"
    assert not appdirs.frozen() and not appdirs.portable()
    assert appdirs.config_dir(env) == roaming
    assert appdirs.config_dir({}) == Path.home() / "AppData" / "Roaming" / "EngineSave"
    assert (appdirs.assets_dir() / "enginesave.ico").is_file(), "the checkout's assets"

    folder = make_frozen(monkeypatch, tmp_path / "dist" / "EngineSave", marker=False)
    assert appdirs.frozen() and not appdirs.portable()
    assert appdirs.config_dir(env) == roaming, "a frozen build without the marker"
    assert appdirs.assets_dir() == folder / "_internal" / "assets"

    (folder / appdirs.PORTABLE_MARKER).write_text("marker", encoding="utf-8")
    assert appdirs.portable()
    assert appdirs.config_dir(env) == folder / "config"
    assert settings_path(env) == folder / "config" / "gui.json"
    assert log_dir(env) == folder / "config" / "logs"


def test_the_marker_file_explains_itself() -> None:
    text = (TOOLS / appdirs.PORTABLE_MARKER).read_text(encoding="utf-8")
    assert "config" in text and "%APPDATA%\\EngineSave" in text


# ------------------------------------------------------------------------------------ spec


def spec_tree() -> tuple[dict[str, str], list[ast.Call]]:
    """Module-level string constants of the spec and every call in it."""
    tree = ast.parse((TOOLS / "enginesave.spec").read_text(encoding="utf-8"))
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    return constants, calls


def keywords(call: ast.Call, constants: dict[str, str]) -> dict[str, object]:
    values: dict[str, object] = {}
    for item in call.keywords:
        if isinstance(item.value, ast.Constant):
            values[str(item.arg)] = item.value.value
        elif isinstance(item.value, ast.Name):
            values[str(item.arg)] = constants.get(item.value.id, item.value.id)
    return values


def test_spec_builds_two_programs_in_one_folder() -> None:
    constants, calls = spec_tree()
    exes = [keywords(c, constants) for c in calls if c.func.id == "EXE"]
    assert {(e["name"], e["console"]) for e in exes} == {
        ("EngineSave", False),
        ("enginesave-cli", True),
    }
    names = [str(e["name"]).casefold() for e in exes]
    assert len(set(names)) == 2, "Windows file names ignore case"
    assert all(e["icon"] == "ICON" and e["upx"] is False for e in exes)
    assert all(e["exclude_binaries"] is True for e in exes), "onedir: one shared COLLECT"
    (collect,) = [keywords(c, constants) for c in calls if c.func.id == "COLLECT"]
    assert collect["name"] == "EngineSave"
    assert constants["MARKER"] == appdirs.PORTABLE_MARKER
    text = (TOOLS / "enginesave.spec").read_text(encoding="utf-8")
    assert '"assets" / "enginesave.ico"' in text and '(str(ROOT / "assets"), "assets")' in text
    assert '"README.md"' in text and '"CHANGELOG.md"' in text and "TOOLS / MARKER" in text
    assert '"LICENSE"' in text, "the license goes beside the programs"
    for script, module in (
        ("portable_gui.py", "enginesave.gui.app"),
        ("portable_cli.py", "enginesave.cli"),
    ):
        assert f'"{script}"' in text
        assert f"from {module} import main" in (TOOLS / script).read_text(encoding="utf-8")
    assert (ROOT / "assets" / "enginesave.ico").is_file()


def test_build_script_is_ascii_and_uses_the_build_venv() -> None:
    """PowerShell 5.1 reads a BOM-less script in the ANSI code page: keep it ASCII."""
    raw = (TOOLS / "build_portable.ps1").read_bytes()
    assert raw.isascii()
    text = raw.decode("ascii")
    for needle in (
        "py -3.14 -m venv",
        ".venv-build",
        "-e '.[build]'",
        "tools\\enginesave.spec",
        "--noconfirm --clean",
        "win64-portable.zip",
        "gh release upload",
        "init.tcl",
        "tk.tcl",
    ):
        assert needle in text, needle
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'build = ["pyinstaller>=6.11"]' in pyproject
    assert ".venv-build/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_package_portable_zips_the_folder_without_config(tmp_path: Path) -> None:
    sys.path.insert(0, str(TOOLS))
    try:
        import package_portable
    finally:
        sys.path.remove(str(TOOLS))
    folder = tmp_path / "dist" / "EngineSave"
    for name, data in {
        "EngineSave.exe": b"MZ gui",
        "enginesave-cli.exe": b"MZ cli",
        "EngineSave.portable": b"marker",
        "_internal/assets/enginesave.ico": b"icon",
        "config/gui.json": b"{}",
        "config/logs/gui.log": b"log",
    }.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
    archive = tmp_path / "dist" / "EngineSave-0.0.0-win64-portable.zip"
    assert package_portable.main([str(folder), str(archive)]) == 0
    with zipfile.ZipFile(archive) as zf:
        assert sorted(zf.namelist()) == [
            "EngineSave/EngineSave.exe",
            "EngineSave/EngineSave.portable",
            "EngineSave/_internal/assets/enginesave.ico",
            "EngineSave/enginesave-cli.exe",
        ]
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_name(archive.name + ".sha256")
    assert checksum.read_bytes() == f"{digest}  {archive.name}\n".encode("ascii")
    assert not list(archive.parent.glob("*.tmp"))


# ------------------------------------------------------------------------------ self-test


@pytest.fixture
def tk_available() -> None:
    tkinter = pytest.importorskip("tkinter")
    try:
        probe = tkinter.Tk()
    except tkinter.TclError as exc:
        pytest.skip(f"Tk cannot open a window here: {exc}")
    probe.destroy()


def test_self_test_builds_the_window_hidden_and_exits_0(tk_available: None) -> None:
    import os

    from enginesave.gui.app import main

    settings = Path(os.environ["APPDATA"]) / "EngineSave" / "gui.json"  # the test sandbox
    assert not settings.exists()
    assert main(["--self-test"]) == 0
    assert json.loads(settings.read_text(encoding="utf-8"))["format_version"] == 1
    log = (settings.parent / "logs" / "gui.log").read_text(encoding="utf-8")
    assert "自检通过" in log and "图形界面已关闭" in log


def test_portable_self_test_writes_config_beside_the_program(
    tk_available: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from enginesave.gui.app import main

    folder = make_frozen(monkeypatch, tmp_path / "dist" / "EngineSave", marker=True)
    roaming = Path(os.environ["APPDATA"]) / "EngineSave"
    assert main(["--self-test"]) == 0
    assert (folder / "config" / "gui.json").is_file()
    assert (folder / "config" / "logs" / "gui.log").is_file()
    assert not roaming.exists(), "%APPDATA%\\EngineSave is not used by a portable build"
