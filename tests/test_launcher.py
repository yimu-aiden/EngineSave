"""One-click start (D25, plan section 17 M11): icon, entry point, shortcut script, Tk check."""

from __future__ import annotations

import importlib.util
import shutil
import struct
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from enginesave.gui import app as gui_app

PROJECT = Path(__file__).resolve().parent.parent
ICON = PROJECT / "assets" / "enginesave.ico"
SCRIPT = PROJECT / "tools" / "install_shortcut.ps1"
LAUNCHER = PROJECT / ".venv" / "Scripts" / "enginesave-gui.exe"


def load_make_icon():
    spec = importlib.util.spec_from_file_location("make_icon", PROJECT / "tools" / "make_icon.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_icon_is_small_valid_and_reproducible() -> None:
    data = ICON.read_bytes()
    assert len(data) <= 20 * 1024, "the only binary in the repository stays under 20 KB"
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind, count) == (0, 1, 3)
    sizes = []
    for index in range(count):
        width, height, _, _, planes, bits, length, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * index
        )
        header = struct.unpack_from("<IiiHH", data, offset)
        assert (width, planes, bits) == (height, 1, 32)
        assert header == (40, width, width * 2, 1, 32)
        assert offset + length <= len(data)
        sizes.append(width)
    assert sizes == [48, 32, 16]
    make_icon = load_make_icon()
    generated = make_icon.icon([(s, make_icon.bitmap(make_icon.render(s))) for s in sizes])
    assert generated == data, "assets/enginesave.ico must be the output of tools/make_icon.py"


def test_gui_entry_point_is_declared() -> None:
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["gui-scripts"] == {"enginesave-gui": "enginesave.gui.app:main"}
    assert project["scripts"] == {"enginesave": "enginesave.cli:main"}


def test_main_explains_a_missing_tk(monkeypatch: pytest.MonkeyPatch) -> None:
    shown: list[str] = []
    monkeypatch.setattr(gui_app, "tk", None)
    monkeypatch.setattr(gui_app, "native_error", shown.append)
    assert gui_app.main() == 3
    assert len(shown) == 1 and "tcl/tk" in shown[0]


def powershell(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_install_shortcut_is_idempotent_and_uninstalls(tmp_path: Path) -> None:
    if shutil.which("powershell") is None:
        pytest.skip("powershell is not available")
    if not LAUNCHER.is_file():
        pytest.skip("run `pip install -e .` first so enginesave-gui.exe exists")
    desktop, menu = tmp_path / "Desktop", tmp_path / "Programs"
    desktop.mkdir()
    menu.mkdir()
    run = f"& '{SCRIPT}' -Folders '{desktop}','{menu}'"
    for _ in range(2):
        done = powershell(run)
        assert done.returncode == 0, done.stderr
        assert done.stdout.count("created ") == 2
    assert sorted(p.name for p in desktop.iterdir()) == ["EngineSave.lnk"]
    link = desktop / "EngineSave.lnk"
    read = powershell(
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('" + str(link) + "'); "
        "$s.TargetPath; $s.WorkingDirectory; $s.IconLocation"
    )
    target, workdir, icon = read.stdout.strip().splitlines()
    assert Path(target) == LAUNCHER and Path(workdir) == PROJECT
    assert icon == f"{ICON},0"
    removed = powershell(run + " -Uninstall")
    assert removed.returncode == 0 and removed.stdout.count("removed ") == 2
    assert not link.exists() and not (menu / "EngineSave.lnk").exists()
    again = powershell(run + " -Uninstall")
    assert again.returncode == 0 and again.stdout.count("not present") == 2


def test_a_missing_tk_is_said_in_the_systems_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """D70 8: before the window and its settings exist, the message follows Windows' display
    language (conftest makes the tests' default Chinese; here Windows says English)."""
    from enginesave.gui import i18n

    shown: list[str] = []
    monkeypatch.setattr(gui_app, "tk", None)
    monkeypatch.setattr(gui_app, "native_error", shown.append)
    monkeypatch.setattr(i18n, "default_language", lambda: i18n.EN)
    assert gui_app.main() == 3
    assert len(shown) == 1 and "Tk" in shown[0] and not i18n.CJK.search(shown[0])


def test_restore_does_not_load_the_console_renderer() -> None:
    """D72 R19: ``LOAD_NOTE`` lives in models; restore no longer imports ``report``."""
    code = "import sys, enginesave.restore; print('enginesave.report' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True
    )
    assert result.stdout.strip() == "False"


def test_the_portable_build_pins_its_tools() -> None:
    """D72 R18: every build tool at an exact version, the interpreter named, the script uses
    them and checks the interpreter before installing anything."""
    root = Path(__file__).resolve().parents[1]
    constraints = (root / "tools" / "build-constraints.txt").read_text(encoding="utf-8")
    pins = [line for line in constraints.splitlines() if line and not line.startswith("#")]
    assert pins and all("==" in line for line in pins)
    assert any(line.startswith("pyinstaller==") for line in pins)
    assert "python 3.14" in constraints and "64-bit" in constraints
    script = (root / "tools" / "build_portable.ps1").read_text(encoding="utf-8")
    assert "-c $constraints" in script
    assert script.index("AMD64") < script.index("pip install --disable-pip-version-check")
