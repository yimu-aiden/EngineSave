"""1.2.0 (D73): the command line run by another program -- in the background, without a
console, always ``--json --yes``: one game's install folders (``--game-root``), unchanged
saves, automatic backups and their pruning, progress lines, a stop file, the snapshot's
install, restoring into another folder, the mirror afterwards and ``about``."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from enginesave.cli import main
from enginesave.repo import Repo
from fixtures.builder import BuiltGame, build_f1, build_f4, reinstall_copy


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(args: list[str], capsys) -> tuple[int, dict]:
    code, out, _ = run([*args, "--json"], capsys)
    return code, json.loads(out)


@pytest.fixture
def two_versions(games_root: Path, saves_root: Path, tmp_path: Path):
    """Game A in two version folders (one save directory, D9) and a library to fill."""
    v1, v2 = build_f4(games_root, saves_root)
    return v1, v2, tmp_path / "library", ["--renpy-saves-root", str(saves_root)]


def only_record(dest: Path) -> tuple[Repo, str]:
    repo = Repo.open(dest)
    (record,) = repo.load_catalog()["games"]
    return repo, record["game_id"]


def backup(root: BuiltGame, dest: Path, common: list[str], capsys, *extra: str):
    args = ["backup", "--game-root", str(root.root), *common, "--dest", str(dest), "--yes"]
    return run_json([*args, *extra], capsys)


# ------------------------------------------------------------------- --game-root (D73 1, 14)


def test_one_install_is_backed_up_into_the_games_record(two_versions, capsys) -> None:
    v1, v2, dest, common = two_versions
    code, data = backup(v1, dest, common, capsys)
    assert code == 0
    (planned,) = data["plan"]["games"]
    assert planned["game_root"] == str(v1.root)
    assert planned["from_install"] == {"game_root": str(v1.root), "game_version": "1.0"}
    assert [s["source_id"] for s in planned["sources"]] == ["local", "global"]
    repo, game_id = only_record(dest)
    first = repo.load_manifest(game_id, repo.snapshot_ids(game_id)[-1])
    assert first["from_install"] == {"game_root": str(v1.root), "game_version": "1.0"}
    assert first["sources"][0]["origin_path"] == str(v1.local_dir)

    code, data = backup(v2, dest, common, capsys)  # the other version: the same record
    assert code == 0 and data["result"]["results"][0]["game_id"] == game_id
    repo, same = only_record(dest)
    assert same == game_id and len(repo.snapshot_ids(game_id)) == 2
    installs = {i["game_root"]: i for i in repo.load_game(game_id)["installs"]}
    assert set(installs) == {str(v1.root), str(v2.root)}, "no install is lost"
    assert installs[str(v2.root)]["game_version"] == "2.0"
    second = repo.load_manifest(game_id, repo.snapshot_ids(game_id)[-1])
    assert second["from_install"]["game_root"] == str(v2.root)


def test_a_folder_that_is_no_game_is_refused(two_versions, games_root: Path, capsys) -> None:
    v1, _v2, dest, common = two_versions
    base = ["backup", *common, "--dest", str(dest), "--yes"]
    code, _, err = run([*base, "--game-root", str(games_root)], capsys)
    assert code == 2 and "not the folder of a game EngineSave recognises" in err
    code, _, err = run([*base, "--game-root", str(games_root / "missing")], capsys)
    assert code == 2 and "not an existing folder" in err
    code, _, err = run(base, capsys)
    assert code == 2 and "--game-root" in err
    assert not dest.exists(), "nothing was written, not even the library folder"


def test_only_the_named_folder_is_scanned(two_versions, games_root: Path, capsys) -> None:
    """The ROOT folders are not walked with --game-root; they only keep the library out."""
    v1, _v2, dest, common = two_versions
    code, data = backup(v1, dest, common, capsys, str(games_root), "--dry-run")
    assert code == 0 and [g["game_root"] for g in data["plan"]["games"]] == [str(v1.root)]
    inside = games_root / "library"
    code, _, err = run(
        ["backup", str(games_root), "--game-root", str(v1.root), *common, "--dest", str(inside)],
        capsys,
    )
    assert code == 2 and "--dest must not be inside a scan root" in err


def test_a_full_backup_of_grouped_versions_names_no_single_install(
    two_versions, games_root: Path, capsys
) -> None:
    _v1, _v2, dest, common = two_versions
    code, data = run_json(
        ["backup", str(games_root), *common, "--dest", str(dest), "--yes"], capsys
    )
    assert code == 0 and data["plan"]["games"][0]["from_install"] is None
    repo, game_id = only_record(dest)
    manifest = repo.load_manifest(game_id, repo.snapshot_ids(game_id)[-1])
    assert manifest["from_install"] is None
    assert {s["source_id"] for s in manifest["sources"]} == {"local", "local-2", "global"}


# ------------------------------------------------- unchanged, auto, keep-auto (D73 2, 3)


def test_unchanged_saves_make_no_snapshot(two_versions, capsys) -> None:
    v1, _v2, dest, common = two_versions
    auto = ["--skip-unchanged", "--reason", "auto"]
    code, data = backup(v1, dest, common, capsys, *auto)
    assert code == 0 and data["result"]["results"][0]["status"] == "ok"
    repo, game_id = only_record(dest)
    (first,) = repo.snapshot_ids(game_id)
    code, data = backup(v1, dest, common, capsys, *auto)
    (result,) = data["result"]["results"]
    assert code == 0 and result["status"] == "unchanged" and result["snapshot_id"] is None
    assert first in result["message"] and repo.snapshot_ids(game_id) == [first]
    (v1.local_dir / "1-1-LT1.save").write_bytes(b"a new save")
    code, data = backup(v1, dest, common, capsys, *auto)
    assert code == 0 and data["result"]["results"][0]["status"] == "ok"
    assert len(repo.snapshot_ids(game_id)) == 2
    from test_schemas import conforms

    conforms("backup", data)


def test_same_size_other_bytes_is_a_change(two_versions, capsys) -> None:
    v1, _v2, dest, common = two_versions
    backup(v1, dest, common, capsys)
    path = v1.local_dir / sorted(v1.local_files)[0]
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    code, out = backup(v1, dest, common, capsys, "--skip-unchanged")
    assert code == 0 and out["result"]["results"][0]["status"] == "ok"


def test_auto_snapshots_are_marked_picked_and_pruned(two_versions, capsys) -> None:
    from enginesave.repo import Repo
    from enginesave.restore import plan_restore

    v1, _v2, dest, common = two_versions
    backup(v1, dest, common, capsys)  # manual: never pruned
    for n in range(4):
        (v1.local_dir / "1-1-LT1.save").write_bytes(b"save %d" % n)
        code, data = backup(v1, dest, common, capsys, "--reason", "auto", "--keep-auto", "2")
        assert code == 0
    pruned = data["result"]["pruned"]
    assert pruned["keep"] == 2 and len(pruned["removed"][0]["snapshot_ids"]) == 1
    repo, game_id = only_record(dest)
    reasons = [repo.load_manifest(game_id, s)["reason"] for s in repo.snapshot_ids(game_id)]
    assert reasons == ["manual", "auto", "auto"]
    newest = repo.snapshot_ids(game_id)[-1]
    readme = (repo.snapshot_dir(game_id, newest) / "README.txt").read_text(encoding="utf-8")
    assert "（自动，完整）" in readme and "(an automatic backup, complete)" in readme
    plan = plan_restore(repo, repo.load_catalog(), game_id, game_root=v1.root)
    assert plan.snapshot_id == newest and plan.manifest["reason"] == "auto"
    code, _, err = run(
        ["backup", "--game-root", str(v1.root), *common, "--dest", str(dest), "--keep-auto", "0"],
        capsys,
    )
    assert code == 2 and "--keep-auto must be 1 or more" in err
    assert Repo.open(dest).snapshot_ids(game_id)[-1] == newest


# ----------------------------------------------------------- list, show, about (D73 5, 11)


def test_list_and_show_say_where_the_latest_snapshot_came_from(two_versions, capsys) -> None:
    from test_schemas import conforms

    v1, v2, dest, common = two_versions
    backup(v1, dest, common, capsys)
    backup(v2, dest, common, capsys, "--reason", "auto")
    code, data = run_json(["list", "--repo", str(dest)], capsys)
    assert code == 0
    (row,) = data["games"]
    assert row["latest_reason"] == "auto" and row["latest_snapshot_at"].endswith("Z")
    assert row["latest_from_install"] == {"game_root": str(v2.root), "game_version": "2.0"}
    assert {i["game_root"] for i in row["installs"]} == {str(v1.root), str(v2.root)}
    assert all(i["present"] for i in row["installs"])
    assert data["stats"]["mirror_folder"] is None
    conforms("list", data)
    code, shown = run_json(["show", "--repo", str(dest), "--game", row["game_id"]], capsys)
    assert [s["from_install"]["game_root"] for s in shown["snapshots"]] == [
        str(v1.root),
        str(v2.root),
    ]


def test_an_anonymized_export_does_not_name_the_install(two_versions, tmp_path, capsys) -> None:
    import zipfile

    v1, _v2, dest, common = two_versions
    backup(v1, dest, common, capsys)
    out = tmp_path / "export.zip"
    assert (
        run(["export", "--repo", str(dest), "--game", "game a", "--to", str(out)], capsys)[0] == 0
    )
    with zipfile.ZipFile(out) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["from_install"] is None
    assert str(v1.root) not in json.dumps(manifest)


def test_about(capsys) -> None:
    from enginesave import __version__

    code, data = run_json(["about"], capsys)
    assert code == 0 and data["tool_version"] == __version__
    assert data["formats"]["scan"] == 1 and data["formats"]["list"] == 1
    assert data["formats"]["backup"] == 1 and data["library"]["manifest"] == 3


# -------------------------------------------------------- progress, stop file (D73 8, 9)


def progress_lines(err: str) -> list[dict]:
    return [json.loads(line) for line in err.splitlines() if line.startswith("{")]


def test_progress_lines_on_stderr(two_versions, games_root: Path, capsys) -> None:
    v1, _v2, dest, common = two_versions
    code, out, err = run(
        ["scan", str(games_root), *common, "--json", "--progress", "jsonl"], capsys
    )
    assert code == 0 and json.loads(out)["games"]
    lines = progress_lines(err)
    assert lines and all(line["event"] == "progress" for line in lines)
    assert lines[0]["phase"] == "scan" and lines[0]["folders"] == 1
    assert lines[0]["current"] == str(games_root)
    assert all(line.startswith("{") for line in err.splitlines()), "only JSON lines"
    args = ["--progress", "jsonl", "--json"]
    code, out, err = run(
        ["backup", "--game-root", str(v1.root), *common, "--dest", str(dest), "--yes", *args],
        capsys,
    )
    assert code == 0
    phases = [(x["phase"], x.get("done"), x.get("total")) for x in progress_lines(err)]
    assert ("backup", 0, 1) in phases and phases[-1] == ("backup", 1, 1)
    assert progress_lines(err)[-1]["name"] == "Game A"
    code, out, err = run(["scan", str(games_root), *common, "--json"], capsys)
    assert not progress_lines(err), "without --progress nothing changes"


def test_a_stop_file_stops_at_a_safe_point(two_versions, games_root: Path, tmp_path, capsys):
    v1, _v2, dest, common = two_versions
    stop = tmp_path / "stop.flag"
    stop.write_text("stop")
    code, _, err = run(["scan", str(games_root), *common, "--stop-file", str(stop)], capsys)
    assert code == 130 and "stopped at a safe point" in err
    args = ["backup", "--game-root", str(v1.root), *common, "--dest", str(dest), "--yes"]
    code, _, _ = run([*args, "--stop-file", str(stop)], capsys)
    assert code == 130
    stop.unlink()
    assert run([*args, "--stop-file", str(stop)], capsys)[0] == 0


def test_a_stop_file_between_games_keeps_what_was_done(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys, monkeypatch
) -> None:
    from enginesave import backup as backing_up
    from enginesave.cli import common

    monkeypatch.setattr(common, "STOP_FILE_INTERVAL", 0.0)  # looked for at every safe point
    build_f1(games_root, saves_root)
    build_f4(games_root, saves_root)
    dest, stop = tmp_path / "library", tmp_path / "stop.flag"
    real = backing_up.backup_game

    def then_stop(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        done = real(*args, **kwargs)
        stop.write_text("stop")
        return done

    monkeypatch.setattr(backing_up, "backup_game", then_stop)
    args = ["backup", str(games_root), "--renpy-saves-root", str(saves_root), "--dest", str(dest)]
    code, out, _ = run([*args, "--yes", "--json", "--stop-file", str(stop)], capsys)
    data = json.loads(out)
    assert code == 130 and data["result"]["cancelled"] is True
    assert len(data["result"]["results"]) == 1 and data["result"]["results"][0]["status"] == "ok"


# ------------------------------------------------------------- restore elsewhere (D73 15)


def grouped_snapshot(v1: BuiltGame, games_root: Path, dest: Path, saves_root: Path) -> None:
    """A snapshot of both versions' local saves that says it comes from ``v1`` (the window's
    tick of one install of a grouped game)."""
    from enginesave.backup import execute_backup, prepare_backup

    plan = prepare_backup(
        [games_root], dest, saves_root_override=saves_root, select_roots=[v1.root]
    )
    assert plan.games[0].from_install["game_root"] == str(v1.root)
    assert len([s for s in plan.games[0].sources if s.source.kind.value == "local"]) == 2
    execute_backup(plan, Repo.open_or_create(dest))


def test_restore_into_a_new_folder_takes_the_snapshots_install(
    two_versions, games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    from test_schemas import conforms

    v1, v2, dest, common = two_versions
    grouped_snapshot(v1, games_root, dest, saves_root)
    again = reinstall_copy(v1.root, tmp_path / "downloaded again" / v1.root.name)
    restore = ["restore", "--repo", str(dest), "--game", "game a", *common]
    code, data = run_json([*restore, "--game-root", str(again), "--dry-run"], capsys)
    assert code == 0
    plan = data["plan"]
    assert plan["game_root"] == str(again) and plan["from_install"] == str(v1.root)
    assert [t["source_id"] for t in plan["targets"]] == ["local", "global"]
    assert plan["targets"][0]["target_dir"] == str(again / "game" / "saves")
    conforms("restore", data)
    code, data = run_json(
        [*restore, "--game-root", str(again), "--from-install", str(v2.root), "--dry-run"], capsys
    )
    assert code == 0 and [t["source_id"] for t in data["plan"]["targets"]] == ["local-2", "global"]
    code, _, err = run(
        [*restore, "--game-root", str(again), "--from-install", str(tmp_path), "--dry-run"], capsys
    )
    assert code == 2 and "no local saves of that install" in err
    code, _, err = run([*restore, "--from-install", str(v1.root), "--dry-run"], capsys)
    assert code == 2 and "--from-install goes with --game-root" in err

    code, data = run_json([*restore, "--game-root", str(again), "--yes"], capsys)  # no console
    assert code == 0 and data["result"]["failures"] == []
    assert (again / "game" / "saves" / "persistent").read_bytes() == v1.local_files["persistent"]
    repo, game_id = only_record(dest)
    roots = {i["game_root"] for i in repo.load_game(game_id)["installs"]}
    assert str(again) in roots, "the new folder is an install of the record now (D43)"


def test_several_installs_without_a_choice_are_still_refused(
    two_versions, games_root: Path, tmp_path: Path, capsys
) -> None:
    v1, _v2, dest, common = two_versions
    run_json(["backup", str(games_root), *common, "--dest", str(dest), "--yes"], capsys)
    again = reinstall_copy(v1.root, tmp_path / "again")
    restore = ["restore", "--repo", str(dest), "--game", "game a", *common, "--dry-run"]
    code, _, err = run([*restore, "--game-root", str(again)], capsys)
    assert code == 2 and "several game-root sources" in err and "--from-install" in err
    code, data = run_json(
        [*restore, "--game-root", str(again), "--from-install", str(v1.root)], capsys
    )
    assert code == 0 and data["plan"]["from_install"] == str(v1.root)


def test_restore_into_a_folder_of_the_game_deleted_and_downloaded_again(
    two_versions, tmp_path: Path, capsys
) -> None:
    """The game folder was deleted after the backup and the same version comes back
    elsewhere: the identity checks pass, the saves go there."""
    v1, _v2, dest, common = two_versions
    backup(v1, dest, common, capsys)
    again = reinstall_copy(v1.root, tmp_path / "D" / "Games" / "GameA")
    shutil.rmtree(v1.root)
    restore = ["restore", "--repo", str(dest), "--game", "game a", *common]
    code, data = run_json([*restore, "--game-root", str(again), "--yes"], capsys)
    assert code == 0 and data["plan"]["from_install"] == str(v1.root)
    assert (again / "game" / "saves" / "persistent").is_file()


# ---------------------------------------------------------------- the mirror (D73 16)


def test_backup_then_mirror(two_versions, tmp_path: Path, capsys) -> None:
    from enginesave.appdirs import config_dir
    from test_schemas import conforms

    v1, _v2, dest, common = two_versions
    code, data = backup(v1, dest, common, capsys, "--mirror")
    assert code == 0 and data["result"]["mirror"]["status"] == "skipped"
    assert "not been mirrored" in data["result"]["mirror"]["message"]
    mirror = tmp_path / "NAS" / "mirror"
    assert run(["mirror", "--repo", str(dest), "--to", str(mirror)], capsys)[0] == 0
    (v1.local_dir / "1-1-LT1.save").write_bytes(b"newer")
    code, data = backup(v1, dest, common, capsys, "--mirror", "--reason", "auto")
    outcome = data["result"]["mirror"]
    assert code == 0 and outcome["status"] == "ok" and outcome["folder"] == str(mirror)
    assert outcome["finished_at"].endswith("Z")
    conforms("backup", data)
    repo, game_id = only_record(dest)
    copy = Repo.open(mirror)
    assert copy.snapshot_ids(game_id) == repo.snapshot_ids(game_id)
    code, listed = run_json(["list", "--repo", str(dest)], capsys)
    assert listed["stats"]["mirror_folder"] == str(mirror)

    code, data = backup(v1, dest, common, capsys, "--mirror", "--skip-unchanged")
    assert data["result"]["mirror"]["status"] == "skipped"  # nothing new to mirror
    (config_dir() / "gui.json").write_text(json.dumps({"auto_mirror": False}), encoding="utf-8")
    (v1.local_dir / "1-1-LT1.save").write_bytes(b"newest")
    code, data = backup(v1, dest, common, capsys, "--auto-mirror")
    assert data["result"]["mirror"]["status"] == "skipped"
    assert "turned off" in data["result"]["mirror"]["message"]
    shutil.rmtree(mirror.parent)  # the NAS is not there
    (v1.local_dir / "1-1-LT1.save").write_bytes(b"newest still")
    code, data = backup(v1, dest, common, capsys, "--mirror")
    assert code == 0, "a mirror that fails leaves the backup's exit code alone"
    assert data["result"]["mirror"]["status"] == "failed"
    assert "not there" in data["result"]["mirror"]["message"]
