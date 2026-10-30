"""D21 (0.2.0): --include-unlinked / --unlinked and confirm-source --game-root."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from enginesave.cli import main
from fixtures.builder import build_f1, build_f5, build_f8, build_game, simple_spec


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def backup_args(games_root: Path, saves_root: Path, tmp_path: Path) -> list[str]:
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    dest = tmp_path / "repo"
    common = ["--renpy-saves-root", str(saves_root), "--dest", str(dest)]
    return ["backup", str(games_root), *common, "--dry-run", "--json"]


def planned_unlinked(out: str) -> list[str]:
    games = json.loads(out)["plan"]["games"]
    return sorted(g["display_name"] for g in games if g["kind"] == "unlinked")


def test_include_unlinked_is_a_switch_for_all(backup_args: list[str], capsys) -> None:
    code, out, _ = run([*backup_args, "--include-unlinked"], capsys)
    assert code == 0
    assert planned_unlinked(out) == ["Orphan-1500000000", "OrphanTwo"]
    code, out, _ = run(backup_args, capsys)
    assert planned_unlinked(out) == []


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (["--unlinked", "orphan-1500000000"], ["Orphan-1500000000"]),
        (["--unlinked", "Orphan-1500000000,OrphanTwo"], ["Orphan-1500000000", "OrphanTwo"]),
        (
            ["--unlinked", "OrphanTwo", "--unlinked", "Orphan-1500000000"],
            ["Orphan-1500000000", "OrphanTwo"],
        ),
    ],
)
def test_unlinked_names(
    backup_args: list[str], capsys, extra: list[str], expected: list[str]
) -> None:
    code, out, _ = run([*backup_args, *extra], capsys)
    assert code == 0
    assert planned_unlinked(out) == expected


def test_unknown_unlinked_name_exit_2(backup_args: list[str], capsys) -> None:
    code, _, err = run([*backup_args, "--unlinked", "Nope"], capsys)
    assert code == 2 and "no such folder: Nope" in err


def test_old_valued_form_is_rejected(backup_args: list[str], capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main([*backup_args, "--include-unlinked=OrphanTwo"])
    assert exc.value.code == 2
    assert "ignored explicit argument" in capsys.readouterr().err


def test_switch_and_names_together_exit_2(backup_args: list[str], capsys) -> None:
    code, _, err = run([*backup_args, "--include-unlinked", "--unlinked", "OrphanTwo"], capsys)
    assert code == 2 and "not both" in err


# ----------------------------------------------------------------- confirm-source --game-root


def confirm_args(repo: Path, saves_root: Path) -> list[str]:
    return ["confirm-source", "--repo", str(repo), "--renpy-saves-root", str(saves_root)]


@pytest.fixture
def empty_repo(games_root: Path, saves_root: Path, tmp_path: Path, capsys) -> Path:
    """A repository created by backing up an unrelated game, so the target has no record."""
    other = tmp_path / "other"
    build_game(other, simple_spec("Unrelated", "Unrelated", "Unrelated-1"), saves_root)
    repo = tmp_path / "repo"
    args = ["backup", str(other), "--renpy-saves-root", str(saves_root), "--dest", str(repo)]
    assert run([*args, "--yes"], capsys)[0] == 0
    return repo


def test_game_root_creates_a_record_without_backing_up(
    games_root: Path, saves_root: Path, empty_repo: Path, capsys
) -> None:
    built = build_f5(games_root, saves_root)
    args = confirm_args(empty_repo, saves_root)
    args += ["--game-root", str(built.root), "--source-id", "global"]
    code, out, _ = run([*args, "--path", str(built.global_dir)], capsys)
    assert code == 0
    assert "Created a record for Dynamic Game; nothing was backed up." in out
    catalog = json.loads((empty_repo / "catalog.json").read_text(encoding="utf-8"))
    record = next(g for g in catalog["games"] if g["display_name"] == "Dynamic Game")
    assert record["snapshot_count"] == 0
    assert catalog["overrides"][-1]["game_id"] == record["game_id"]
    assert record["folder"] == "Dynamic Game"  # D67 1
    game_json = empty_repo / "games" / "Dynamic Game" / "game.json"
    installs = json.loads(game_json.read_text(encoding="utf-8"))["installs"]
    assert [i["game_root"] for i in installs] == [str(built.root)]
    assert not (empty_repo / "games" / "Dynamic Game" / "snapshots").exists()

    scan = ["scan", str(games_root), "--renpy-saves-root", str(saves_root), "--json"]
    code, out, _ = run([*scan, "--repo", str(empty_repo)], capsys)
    game = json.loads(out)["games"][0]
    assert game["known_game_id"] == record["game_id"]
    assert game["sources"][1]["status"] == "USER_CONFIRMED"

    code, out, _ = run([*args, "--path", str(built.global_dir)], capsys)
    assert code == 0 and "Created a record" not in out, "second run reuses the record"


def test_game_root_notes_unknown_path(
    games_root: Path, saves_root: Path, empty_repo: Path, tmp_path: Path, capsys
) -> None:
    built = build_f5(games_root, saves_root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    args = confirm_args(empty_repo, saves_root) + ["--game-root", str(built.root)]
    code, out, _ = run([*args, "--source-id", "x", "--path", str(elsewhere)], capsys)
    assert code == 0 and "is not among the save folders found for this game" in out


def test_game_or_game_root_is_required_exactly_once(
    games_root: Path, saves_root: Path, empty_repo: Path, capsys
) -> None:
    built = build_f5(games_root, saves_root)
    base = confirm_args(empty_repo, saves_root) + ["--source-id", "global"]
    base += ["--path", str(built.global_dir)]
    assert run(base, capsys)[0] == 2
    both = [*base, "--game", "Unrelated", "--game-root", str(built.root)]
    code, _, err = run(both, capsys)
    assert code == 2 and "exactly one of --game or --game-root" in err


def test_game_root_must_be_a_game_install(
    games_root: Path, saves_root: Path, empty_repo: Path, capsys
) -> None:
    build_f5(games_root, saves_root)
    args = confirm_args(empty_repo, saves_root) + ["--game-root", str(games_root)]
    code, _, err = run([*args, "--source-id", "global", "--path", str(saves_root)], capsys)
    assert code == 2 and "no game is installed at" in err and "Dynamic Game" in err
