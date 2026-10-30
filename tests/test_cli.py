from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from enginesave import __version__
from enginesave.cli import main
from enginesave.report import abbreviate, human_size
from fixtures.builder import build_f1, build_f4b, build_f8, build_f19


def test_version_flag_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"enginesave {__version__}"


LOADED = (
    "import sys\n"
    "from enginesave.cli import main\n"
    "try:\n"
    "    main(sys.argv[1:])\n"
    "except SystemExit:\n"
    "    pass\n"
    "print(','.join(sorted(m for m in sys.modules if m.startswith('enginesave'))), "
    "file=sys.stderr)\n"
)
HEAVY = ("scan", "backup", "export", "cleanup", "restore", "report", "engines.renpy.detector")


@pytest.mark.parametrize(
    ("argv", "allowed"),
    [
        (["--version"], set()),
        (["list", "--repo", "MISSING"], {"backup", "scan", "report"}),
    ],
)
def test_a_subcommand_imports_only_what_it_needs(argv: list[str], allowed: set[str]) -> None:
    """D38: the entry point loads the module of the subcommand that runs and nothing else, so
    ``--version`` loads no library code and ``list`` no exporter, cleanup or restore code."""
    proc = subprocess.run(
        [sys.executable, "-c", LOADED, *argv], capture_output=True, text=True, check=True
    )
    loaded = set(proc.stderr.strip().splitlines()[-1].split(","))
    unexpected = {name for name in HEAVY if f"enginesave.{name}" in loaded} - allowed
    assert not unexpected, f"{argv} loaded {sorted(unexpected)}"


def test_missing_subcommand_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_scan_human_output(
    games_root: Path, saves_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    code = main(["scan", str(games_root), "--renpy-saves-root", str(saves_root)])
    out = capsys.readouterr().out
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == f"EngineSave {__version__} - scan"
    assert lines[2] == f"Ren'Py saves root: {saves_root}"
    assert "[CONFIRMED]  Readable Game (1.0)   renpy 8.4.1.25072401" in lines
    identity = next(line for line in lines if line.startswith("  identity:"))
    assert "save_directory=ReadableGame-1600000001" in identity
    assert identity.endswith("(game/options.rpyc)"), "D52: the readable .rpyc alone"
    local = next(line for line in lines if line.startswith("  local "))
    assert "CONFIRMED" in local and f"game{os.sep}saves" in local and "4 files" in local
    global_ = next(line for line in lines if line.startswith("  global "))
    assert "mirror of: local (incl. sync" in global_
    assert "Unlinked global save directories (2):" in lines
    assert any("Orphan-1500000000" in line and "version hint: 0.7.2" in line for line in lines)
    assert lines[-1] == (
        "Summary: 1 confirmed, 0 candidate, 0 not found, 0 conflict, 0 unsupported, 2 unlinked"
    )


def test_scan_json_and_global_options_before_command(
    games_root: Path, saves_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_f1(games_root, saves_root)
    code = main(["--json", "--renpy-saves-root", str(saves_root), "scan", str(games_root)])
    captured = capsys.readouterr()
    assert code == 0
    data = json.loads(captured.out)
    assert data["format_version"] == 1
    assert data["tool_version"] == __version__
    assert data["games"][0]["status"] == "CONFIRMED"
    assert data["games"][0]["sources"][1]["root_token"] == "RENPY_SAVES_ROOT"
    assert "EngineSave" in captured.err


def test_scan_report_file(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_f1(games_root, saves_root)
    report = tmp_path / "out" / "scan.json"
    args = ["scan", str(games_root), "--renpy-saves-root", str(saves_root), "--hash"]
    assert main([*args, "--report", str(report)]) == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["games"][0]["display_name"] == "Readable Game"
    assert any(n.startswith("identical by hash") for n in data["games"][0]["sources"][1]["notes"])
    inside = games_root / "report.json"
    assert main([*args, "--report", str(inside)]) == 2
    assert not inside.exists()
    assert "must not point inside a scanned root" in capsys.readouterr().err


def test_scan_conflict_exits_1(
    games_root: Path, saves_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_f19(games_root, saves_root)
    assert main(["scan", str(games_root), "--renpy-saves-root", str(saves_root)]) == 1
    assert "[CONFLICT]" in capsys.readouterr().out


def test_two_games_sharing_a_folder_are_not_a_conflict(
    games_root: Path, saves_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D48 1: since 0.8.0 the scan of two games that share a global folder exits 0."""
    build_f4b(games_root, saves_root)
    assert main(["scan", str(games_root), "--renpy-saves-root", str(saves_root)]) == 0
    out = capsys.readouterr().out
    assert "[CONFLICT]" not in out and "shared with a different game" in out


def test_scan_respects_env_override_and_no_unlinked(
    games_root: Path, saves_root: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    monkeypatch.setenv("ENGINESAVE_RENPY_SAVES_ROOT", str(saves_root))
    assert main(["scan", str(games_root), "--no-unlinked", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["renpy_saves_root"] == str(saves_root)
    assert data["unlinked"] == []
    assert data["games"][0]["status"] == "CONFIRMED"


def test_formatting_helpers() -> None:
    assert human_size(512) == "512 B"
    assert human_size(5_767_168) == "5.5 MB"
    path = "C:\\Users\\alice\\AppData\\Roaming\\RenPy\\MoonlitHarbor"
    assert abbreviate(path) == "C:\\...\\RenPy\\MoonlitHarbor"
    assert abbreviate("C:\\short") == "C:\\short"
