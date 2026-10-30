"""CLI behaviour of restore / link / confirm-source / scan --repo (plan sections 7 and 9.2)."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from enginesave.cli import main
from fixtures.builder import BuiltGame, build_f1, build_f5, build_f8, reinstall_copy


class FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def backed_up(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[BuiltGame, Path, list[str]]:
    built = build_f1(games_root, saves_root)
    build_f8(saves_root)
    repo = tmp_path / "repo"
    common = ["--renpy-saves-root", str(saves_root)]
    code, _, _ = run(
        ["backup", str(games_root), *common, "--dest", str(repo), "--yes", "--include-tokens"],
        capsys,
    )
    assert code == 0
    return built, repo, common


def test_restore_round_trip(backed_up, tmp_path: Path, capsys) -> None:
    built, repo, common = backed_up
    wrapper = tmp_path / "Reinstall"  # as an archive unpacks: the game is one level down
    target = reinstall_copy(built.root, wrapper / built.root.name)
    shutil.rmtree(built.root)
    shutil.rmtree(built.global_dir)
    args = ["restore", "--repo", str(repo), "--game", "ReadableGame-1600000001", *common]
    code, out, _ = run([*args, "--game-root", str(wrapper), "--yes"], capsys)
    assert code == 0
    assert "restore plan" in out and "folder will be created" in out
    assert f"Game folder: {target}" in out and "holds the game in" in out
    assert "restored 12 files, 0 failed" in out
    assert "whether the game loads these saves depends on game version" in out
    assert (target / "game" / "saves" / "persistent").read_bytes() == built.local_files[
        "persistent"
    ]
    assert (built.global_dir / "sync" / "persistent").is_file()


def test_restore_dry_run_and_json(backed_up, tmp_path: Path, capsys) -> None:
    built, repo, common = backed_up
    target = reinstall_copy(built.root, tmp_path / "Reinstall")
    args = ["restore", "--repo", str(repo), "--game", "readable game", *common]
    code, out, _ = run([*args, "--game-root", str(target), "--dry-run", "--json"], capsys)
    assert code == 0
    data = json.loads(out)
    assert data["dry_run"] is True and data["plan"]["game_root"] == str(target)
    assert [t["source_id"] for t in data["plan"]["targets"]] == ["local", "global"]
    assert not (target / "game" / "saves").exists()
    empty = tmp_path / "Empty"
    empty.mkdir()
    code, _, err = run([*args, "--game-root", str(empty), "--dry-run"], capsys)
    assert code == 2 and "is not the folder of a game" in err


def test_restore_requires_confirmation(backed_up, monkeypatch, capsys) -> None:
    built, repo, common = backed_up
    (built.local_dir / "1-1-LT1.save").write_bytes(b"newer")
    args = ["restore", "--repo", str(repo), "--game", "readable game", *common]
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code, _, err = run(args, capsys)
    assert code == 2 and "without --yes" in err
    monkeypatch.setattr("sys.stdin", FakeTty("no\n"))
    code, _, err = run(args, capsys)
    assert code == 1 and "Aborted" in err
    assert (built.local_dir / "1-1-LT1.save").read_bytes() == b"newer"
    monkeypatch.setattr("sys.stdin", FakeTty("yes\n"))
    code, out, _ = run(args, capsys)
    assert code == 0 and "safety snapshot: " in out and "pre_restore" in out
    assert (built.local_dir / "1-1-LT1.save").read_bytes() == built.local_files["1-1-LT1.save"]


def test_safety_snapshot_opt_out_needs_both_flags(backed_up, capsys) -> None:
    _, repo, common = backed_up
    args = ["restore", "--repo", str(repo), "--game", "readable game", *common, "--yes"]
    code, _, err = run([*args, "--no-safety-snapshot"], capsys)
    assert code == 2 and "--i-know-what-i-am-doing" in err
    code, out, _ = run([*args, "--no-safety-snapshot", "--i-know-what-i-am-doing"], capsys)
    assert code == 0 and "none needed" in out


def test_tokens_need_interactive_second_confirmation(backed_up, monkeypatch, capsys) -> None:
    _, repo, common = backed_up
    args = ["restore", "--repo", str(repo), "--game", "readable game", *common]
    args += ["--sources", "machine_tokens", "--yes"]
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code, out, err = run(args, capsys)
    assert code == 2 and "second, interactive confirmation" in err
    assert "WARNING: this restores tokens/" in out
    monkeypatch.setattr("sys.stdin", FakeTty("no\n"))
    assert run(args, capsys)[0] == 1


def test_path_injection_exit_3(backed_up, capsys) -> None:
    _, repo, common = backed_up
    manifest_path = next(repo.glob("games/*/snapshots/*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../evil.save"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    args = ["restore", "--repo", str(repo), "--game", "readable game", *common, "--yes"]
    code, _, err = run(args, capsys)
    assert code == 3 and "invalid or unsafe" in err


def test_link_and_scan_repo(
    backed_up, games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    built, repo, common = backed_up
    code, out, _ = run(["scan", str(games_root), *common, "--repo", str(repo)], capsys)
    assert code == 0 and "  known as:  " in out
    game_id = json.loads(run(["list", "--repo", str(repo), "--json"], capsys)[1])["games"][0][
        "game_id"
    ]
    elsewhere = tmp_path / "Somewhere"
    elsewhere.mkdir()
    link = ["link", "--repo", str(repo), "--game", game_id]
    code, out, _ = run([*link, "--game-root", str(elsewhere)], capsys)
    assert code == 0 and f"Linked {elsewhere}" in out
    code, out, _ = run([*link, "--game-root", str(elsewhere)], capsys)
    assert "replaces an earlier link" in out
    overrides = json.loads((repo / "catalog.json").read_text(encoding="utf-8"))["overrides"]
    assert [o["type"] for o in overrides] == ["link_root"]
    code, _, err = run([*link, "--game-root", str(tmp_path / "missing")], capsys)
    assert code == 2 and "not an existing folder" in err


def test_confirm_source_cli(games_root: Path, saves_root: Path, tmp_path: Path, capsys) -> None:
    built = build_f5(games_root, saves_root)
    repo = tmp_path / "repo"
    common = ["--renpy-saves-root", str(saves_root)]
    run(["backup", str(games_root), *common, "--dest", str(repo), "--yes"], capsys)
    confirm = ["confirm-source", "--repo", str(repo), "--game", "Dynamic Game", "--source-id"]
    code, out, _ = run([*confirm, "global", "--path", str(built.global_dir)], capsys)
    assert code == 0 and "USER_CONFIRMED" in out
    code, out, _ = run(["scan", str(games_root), *common, "--repo", str(repo), "--json"], capsys)
    source = json.loads(out)["games"][0]["sources"][1]
    assert (source["source_id"], source["status"]) == ("global", "USER_CONFIRMED")
    code, _, err = run([*confirm, "../x", "--path", str(built.global_dir)], capsys)
    assert code == 2 and "--source-id" in err
