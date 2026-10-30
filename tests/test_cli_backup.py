"""CLI behaviour of backup / list / show / verify (exit codes per plan section 7)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from enginesave.cli import interactive, main
from fixtures.builder import build_f1, build_f8


class FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def setup(games_root: Path, saves_root: Path, tmp_path: Path) -> tuple[list[str], Path]:
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    dest = tmp_path / "backup"
    return [str(games_root), "--renpy-saves-root", str(saves_root), "--dest", str(dest)], dest


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_backup_list_show_verify_round_trip(setup, capsys) -> None:
    args, dest = setup
    code, out, _ = run(["backup", *args, "--yes"], capsys)
    assert code == 0
    assert "backup plan" in out and "Readable Game" in out
    assert "OK       Readable Game  snapshot" in out
    assert out.strip().endswith("Result: 1 ok, 0 partial, 0 failed, 0 skipped")

    code, out, _ = run(["list", "--repo", str(dest)], capsys)
    assert code == 0 and "Readable Game" in out and "1 snapshot " in out

    code, out, _ = run(["show", "--repo", str(dest), "--game", "ReadableGame-1600000001"], capsys)
    assert code == 0 and "Snapshots (1):" in out and "manual" in out

    code, out, _ = run(
        ["show", "--repo", str(dest), "--game", "readable game", "--snapshot", "latest"], capsys
    )
    assert code == 0 and "status:   complete" in out and "Errors (0):" in out

    code, out, _ = run(["verify", "--repo", str(dest)], capsys)
    assert code == 0 and "Result: 1 clean, 0 with problems" in out


def test_backup_json_and_report(setup, tmp_path: Path, capsys) -> None:
    args, dest = setup
    report = tmp_path / "backup-report.json"
    code, out, err = run(["backup", *args, "--yes", "--json", "--report", str(report)], capsys)
    assert code == 0
    data = json.loads(out)
    assert data["result"]["results"][0]["status"] == "ok"
    assert json.loads(report.read_text(encoding="utf-8")) == data
    assert "backup plan" in err
    code, out, _ = run(["--json", "list", "--repo", str(dest)], capsys)
    assert json.loads(out)["games"][0]["snapshot_count"] == 1
    code, out, _ = run(["verify", "--repo", str(dest), "--json"], capsys)
    assert json.loads(out)["snapshots"][0]["clean"] is True


def test_dry_run_writes_nothing(setup, capsys) -> None:
    args, dest = setup
    code, out, _ = run(["backup", *args, "--dry-run", "--include-unlinked"], capsys)
    assert code == 0
    assert "Orphan-1500000000" in out and "Total: 3 to back up" in out
    assert not dest.exists()


def test_non_interactive_without_yes_is_refused(setup, monkeypatch, capsys) -> None:
    args, dest = setup
    monkeypatch.setattr("sys.stdin", io.StringIO("yes\n"))
    code, _, err = run(["backup", *args], capsys)
    assert code == 2
    assert "without --yes" in err
    assert not dest.exists()


def test_nul_stdin_counts_as_non_interactive(setup, monkeypatch, capsys) -> None:
    """On Windows NUL claims isatty(); GetConsoleMode tells it apart (S-060)."""
    args, dest = setup
    with open(os.devnull, encoding="ascii") as nul:
        monkeypatch.setattr("sys.stdin", nul)
        assert interactive(nul) is False
        code, _, err = run(["backup", *args], capsys)
    assert code == 2 and "without --yes" in err
    assert not dest.exists()
    assert interactive(FakeTty("yes\n")) is True, "streams without a handle keep isatty()"
    assert interactive(None) is False


@pytest.mark.parametrize(("answer", "expected"), [("yes\n", 0), ("no\n", 1)])
def test_interactive_confirmation(setup, monkeypatch, capsys, answer: str, expected: int) -> None:
    args, dest = setup
    monkeypatch.setattr("sys.stdin", FakeTty(answer))
    code, _, err = run(["backup", *args], capsys)
    assert code == expected
    assert "Type 'yes' to continue" in err
    assert dest.exists() is (expected == 0)


def test_unsafe_destination_exit_2(games_root: Path, saves_root: Path, capsys) -> None:
    build_f1(games_root, saves_root)
    dest = games_root / "inside"
    args = ["backup", str(games_root), "--renpy-saves-root", str(saves_root), "--dest", str(dest)]
    code, _, err = run([*args, "--yes"], capsys)
    assert code == 2 and "--dest must not be inside a scan root" in err
    assert not dest.exists()


def test_verify_detects_tampering_exit_1(setup, capsys) -> None:
    args, dest = setup
    assert run(["backup", *args, "--yes"], capsys)[0] == 0
    stored = next(dest.glob("games/*/snapshots/*/data/GAME_ROOT/game/saves/persistent"))
    stored.write_bytes(b"tampered")
    code, out, _ = run(["verify", "--repo", str(dest)], capsys)
    assert code == 1
    assert "mismatched: local/persistent" in out


def test_repository_errors(tmp_path: Path, capsys) -> None:
    code, _, err = run(["list", "--repo", str(tmp_path / "missing")], capsys)
    assert code == 2 and "not an EngineSave repository" in err
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "catalog.json").write_text("{not json")
    code, _, err = run(["list", "--repo", str(broken)], capsys)
    assert code == 3 and "unreadable" in err


def test_show_unknown_snapshot_and_game(setup, capsys) -> None:
    args, dest = setup
    run(["backup", *args, "--yes"], capsys)
    code, _, err = run(
        ["show", "--repo", str(dest), "--game", "readable game", "--snapshot", "20200101T000000Z"],
        capsys,
    )
    assert code == 2 and "no snapshot" in err
    code, _, err = run(["show", "--repo", str(dest), "--game", "nope"], capsys)
    assert code == 2 and "no game matches" in err
