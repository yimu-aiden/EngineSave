# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec of the portable build (plan section 19 M15, D28); run it through
# tools/build_portable.ps1, which uses .venv-build:
#     pyinstaller tools/enginesave.spec --noconfirm --clean
#
# One folder, dist/EngineSave/, two programs sharing _internal/:
#     EngineSave.exe      the window (no console), from enginesave.gui.app:main
#     enginesave-cli.exe  the command line (console), from enginesave.cli:main
# The command line is not called enginesave.exe: Windows file names ignore case, so it would
# be the same file as EngineSave.exe (S-087).
#
# Tcl/Tk come with PyInstaller's tkinter hook. PyInstaller 6 puts data files into
# _internal/, so after COLLECT the portable marker, README, CHANGELOG and LICENSE are copied beside the
# programs, where users see them and where enginesave.appdirs looks for the marker.

import shutil
from pathlib import Path

ROOT = Path(SPECPATH).parent  # SPECPATH and DISTPATH are set by PyInstaller
TOOLS = ROOT / "tools"
ICON = str(ROOT / "assets" / "enginesave.ico")
FOLDER = "EngineSave"
GUI_NAME = "EngineSave"
CLI_NAME = "enginesave-cli"
MARKER = "EngineSave.portable"
BESIDE_PROGRAMS = [
    TOOLS / MARKER,
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "LICENSE",
]
DATAS = [(str(ROOT / "assets"), "assets")]  # the window icon

gui = Analysis([str(TOOLS / "portable_gui.py")], pathex=[str(ROOT / "src")], datas=DATAS)
cli = Analysis([str(TOOLS / "portable_cli.py")], pathex=[str(ROOT / "src")], datas=DATAS)

gui_exe = EXE(
    PYZ(gui.pure),
    gui.scripts,
    [],
    exclude_binaries=True,
    name=GUI_NAME,
    console=False,
    icon=ICON,
    upx=False,
)
cli_exe = EXE(
    PYZ(cli.pure),
    cli.scripts,
    [],
    exclude_binaries=True,
    name=CLI_NAME,
    console=True,
    icon=ICON,
    upx=False,
)
COLLECT(
    gui_exe,
    gui.binaries,
    gui.datas,
    cli_exe,
    cli.binaries,
    cli.datas,
    name=FOLDER,
    upx=False,
)

for source in BESIDE_PROGRAMS:
    shutil.copy2(source, Path(DISTPATH) / FOLDER / source.name)
