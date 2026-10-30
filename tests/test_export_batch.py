"""Batch export into a folder and import of a whole folder (D27, plan section 19 M14)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enginesave import backup
from enginesave.backup import execute_backup, prepare_backup
from enginesave.cli import main
from enginesave.errors import UsageError
from enginesave.export import (
    EXPORTED,
    FAILED,
    IMPORTED,
    SKIPPED,
    default_export_name,
    export_records,
    export_snapshot,
    import_archives,
    preview_imports,
    read_export,
    zip_archives,
)
from enginesave.repo import Repo, base_snapshot_id
from enginesave.verify import verify_objects, verify_repo
from fixtures.builder import build_f1, build_f8, build_game, simple_spec


def back_up(games_root: Path, saves_root: Path, dest: Path) -> Repo:
    plan = prepare_backup([games_root], dest, saves_root_override=saves_root, include_unlinked=[])
    repo = Repo.open_or_create(plan.dest)
    assert all(r.status == "ok" for r in execute_backup(plan, repo).results)
    return repo


@pytest.fixture
def library(games_root: Path, saves_root: Path, tmp_path: Path) -> Repo:
    """One game and two orphaned folders: three records."""
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "repo")
    assert len(repo.load_catalog()["games"]) == 3
    return repo


def record_ids(repo: Repo) -> list[str]:
    return [r["game_id"] for r in repo.load_catalog()["games"]]


def results(batch) -> list[str]:
    return [item["result"] for item in batch.items]


def test_export_writes_one_verified_zip_per_record(
    library: Repo, saves_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "exports"
    ticks: list[tuple[int, int]] = []
    batch = export_records(
        library,
        record_ids(library),
        folder,
        saves_root_override=saves_root,
        progress=lambda index, total, _name: ticks.append((index, total)),
    )
    assert results(batch) == [EXPORTED] * 3 and batch.exit_code == 0
    assert ticks == [(0, 3), (1, 3), (2, 3)]
    expected = sorted(default_export_name(i["display_name"], i["snapshot_id"]) for i in batch.items)
    assert sorted(p.name for p in folder.iterdir()) == expected
    for item in batch.items:
        header, _ = read_export(Path(item["path"]))
        assert header["game"]["game_id"] == item["game_id"]
        assert item["export"]["paths"] == "anonymized" and not item["export"]["includes_tokens"]

    stamps = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
    again = export_records(library, record_ids(library), folder, saves_root_override=saves_root)
    assert results(again) == [SKIPPED] * 3 and again.exit_code == 0
    assert {i["reason"] for i in again.items} == {"the file already exists"}
    assert {p.name: p.stat().st_mtime_ns for p in folder.iterdir()} == stamps, "kept as they were"
    replaced = export_records(
        library, record_ids(library), folder, overwrite=True, saves_root_override=saves_root
    )
    assert results(replaced) == [EXPORTED] * 3
    assert sorted(p.name for p in folder.iterdir()) == expected, "no temporary files are left"
    assert replaced.to_dict()["exported"] == 3 and replaced.to_dict()["kind"] == "export"


def test_one_failed_export_does_not_stop_the_others(
    library: Repo, saves_root: Path, tmp_path: Path
) -> None:
    game = next(r for r in library.load_catalog()["games"] if r["kind"] == "game")
    data = library.snapshot_dir(game["game_id"], library.latest_snapshot(game["game_id"]))
    next(p for p in (data / "data").rglob("*") if p.is_file()).unlink()  # a damaged snapshot
    folder = tmp_path / "exports"
    batch = export_records(library, record_ids(library), folder, saves_root_override=saves_root)
    outcome = {item["game_id"]: item for item in batch.items}
    broken = outcome[game["game_id"]]
    assert broken["result"] == FAILED and "unreadable" in broken["reason"]
    assert results(batch).count(EXPORTED) == 2 and batch.exit_code == 1
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        Path(i["path"]).name for i in batch.items if i["result"] == EXPORTED
    ), "the failed archive and its temporary file are gone"


def test_a_record_without_a_manual_snapshot_is_skipped(
    library: Repo, saves_root: Path, tmp_path: Path
) -> None:
    game = next(r for r in library.load_catalog()["games"] if r["kind"] == "game")
    for snapshot_id in library.snapshot_ids(game["game_id"]):
        shutil.rmtree(library.snapshot_dir(game["game_id"], snapshot_id))
    batch = export_records(
        library, record_ids(library), tmp_path / "out", saves_root_override=saves_root
    )
    skipped = next(i for i in batch.items if i["game_id"] == game["game_id"])
    assert (skipped["result"], skipped["path"]) == (SKIPPED, None)
    assert skipped["reason"] == "no manual or imported snapshot" and batch.exit_code == 0


def test_two_records_with_one_name_get_two_archives(
    games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for folder, exe, save_directory in (
        ("Twin A", "TwinA", "TwinA-1"),
        ("Twin B", "TwinB", "TwinB-2"),
    ):
        build_game(games_root, simple_spec(folder, exe, save_directory, name="Twin"), saves_root)
    moment = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(backup, "utc_now", lambda: moment)  # one snapshot id for both
    plan = prepare_backup([games_root], tmp_path / "repo", saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    same = base_snapshot_id(moment)
    assert [r.snapshot_id for r in execute_backup(plan, repo).results] == [same] * 2
    batch = export_records(repo, record_ids(repo), tmp_path / "out", saves_root_override=saves_root)
    names = [Path(i["path"]).name for i in batch.items]
    assert results(batch) == [EXPORTED, EXPORTED]
    assert names == [
        f"EngineSave-twin-{same}.zip",
        f"EngineSave-twin-{same}-{batch.items[1]['game_id'][:8]}.zip",
    ]


def test_export_folders_that_are_refused(library: Repo, saves_root: Path, tmp_path: Path) -> None:
    ids = record_ids(library)
    with pytest.raises(UsageError, match="inside the backup library"):
        export_records(library, ids, library.root / "exports", saves_root_override=saves_root)
    with pytest.raises(UsageError, match="must not be inside"):
        export_records(library, ids, saves_root / "exports", saves_root_override=saves_root)
    with pytest.raises(UsageError, match="must not be inside"):
        export_records(library, ids, tmp_path / "roots" / "x", forbidden_roots=[tmp_path / "roots"])
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    with pytest.raises(UsageError, match="is a file"):
        export_records(library, ids, tmp_path / "a-file", saves_root_override=saves_root)
    with pytest.raises(UsageError):
        export_records(library, [*ids, "no such game"], tmp_path / "out")
    assert not (library.root / "exports").exists() and not (tmp_path / "out").exists()


def test_folder_import_reports_every_archive(
    library: Repo, saves_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "exports"
    export_records(library, record_ids(library), folder, saves_root_override=saves_root)
    (folder / "0-broken.zip").write_bytes(b"not a zip at all")
    (folder / "notes.txt").write_text("not an archive", encoding="utf-8")
    (folder / "sub").mkdir()
    shutil.copy(next(folder.glob("EngineSave-*.zip")), folder / "sub" / "nested.zip")
    archives = zip_archives(folder)
    assert len(archives) == 4 and archives[0].name == "0-broken.zip", "by name, not recursive"

    target = tmp_path / "imported"
    previews = preview_imports(target, archives)
    assert [plan is not None for _archive, plan, _error in previews] == [False, True, True, True]
    assert not target.exists(), "previews read only"
    ticks: list[str] = []
    batch = import_archives(
        target, archives, location=folder, progress=lambda _i, _n, name: ticks.append(name)
    )
    assert results(batch) == [FAILED, IMPORTED, IMPORTED, IMPORTED] and batch.exit_code == 1
    assert "0-broken.zip" in batch.items[0]["error"] and ticks == [a.name for a in archives]
    assert batch.to_dict()["imported"] == 3 and batch.location == str(folder)
    imported = Repo.open(target)
    assert len(imported.load_catalog()["games"]) == 3
    assert all(check.clean for check in verify_repo(imported, imported.load_catalog()))
    assert verify_objects(imported).clean


def test_each_archive_is_planned_again_before_its_import(
    library: Repo, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    back_up(games_root, saves_root, library.root)  # a second snapshot of every record
    game = next(r for r in library.load_catalog()["games"] if r["kind"] == "game")
    folder = tmp_path / "two"
    for snapshot_id in library.snapshot_ids(game["game_id"]):
        export_snapshot(
            library,
            game["game_id"],
            snapshot_id,
            folder / f"{snapshot_id}.zip",
            saves_root_override=saves_root,
        )
    fresh = tmp_path / "fresh"
    previews = preview_imports(fresh, zip_archives(folder))
    assert [plan.matched_by for _archive, plan, _error in previews] == [None, None]
    batch = import_archives(fresh, zip_archives(folder))
    assert [i["plan"]["matched_by"] for i in batch.items] == [None, "game_id"]
    (record,) = Repo.open(fresh).load_catalog()["games"]
    assert len(Repo.open(fresh).snapshot_ids(record["game_id"])) == 2


def test_zip_archives_needs_a_folder_with_archives(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="not an existing folder"):
        zip_archives(tmp_path / "missing")
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    with pytest.raises(UsageError, match="no .zip files"):
        zip_archives(tmp_path)


# ------------------------------------------------------------------------------------- CLI


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_batch_export(library: Repo, saves_root: Path, tmp_path: Path, capsys) -> None:
    common = ["--repo", str(library.root), "--renpy-saves-root", str(saves_root)]
    folder = tmp_path / "batch"
    code, out, err = run(["export", *common, "--all", "--to", str(folder), "--json"], capsys)
    assert code == 0 and json.loads(out)["exported"] == 3
    assert "[1/3]" in err and "Export summary" in err
    code, out, _ = run(["export", *common, "--all", "--to", str(folder)], capsys)
    assert code == 0 and out.count("SKIPPED") == 3 and "--overwrite" in out
    ids = record_ids(library)
    two = tmp_path / "two"
    code, out, _ = run(
        ["export", *common, "--game", ids[0], "--game", ids[1], "--to", str(two)], capsys
    )
    assert code == 0 and len(list(two.glob("*.zip"))) == 2 and "Exported 2" in out
    one = tmp_path / "one.zip"
    code, _, _ = run(["export", *common, "--game", ids[0], "--to", str(one)], capsys)
    assert code == 0 and one.is_file(), "a single --game still writes one file"
    for wrong in (
        ["--all", "--game", ids[0], "--to", str(tmp_path / "x")],
        ["--to", str(tmp_path / "x")],
        ["--all", "--snapshot", "20261024T000000Z", "--to", str(tmp_path / "x")],
        ["--all", "--to", str(tmp_path / "several.zip")],
    ):
        code, _, err = run(["export", *common, *wrong], capsys)
        assert code == 2 and err.startswith("error:"), wrong
    assert not (tmp_path / "x").exists() and not (tmp_path / "several.zip").exists()


def test_cli_folder_import(library: Repo, saves_root: Path, tmp_path: Path, capsys) -> None:
    folder = tmp_path / "batch"
    export_records(library, record_ids(library), folder, saves_root_override=saves_root)
    (folder / "zz-broken.zip").write_bytes(b"broken")
    target = tmp_path / "imported"
    command = ["import", "--repo", str(target), "--from", str(folder)]
    code, out, _ = run(command, capsys)
    assert code == 2 and not target.exists(), "no --yes in a non-interactive session"
    assert "3 of 4 archives will be imported" in out and "CANNOT   zz-broken.zip" in out
    code, out, err = run([*command, "--yes", "--json"], capsys)
    data = json.loads(out)
    assert code == 1 and (data["imported"], data["failed"]) == (3, 1)
    assert data["items"][-1]["archive"].endswith("zz-broken.zip") and "Imported 3" in err
    code, _, _ = run(["verify", "--repo", str(target), "--objects"], capsys)
    assert code == 0
    (tmp_path / "empty").mkdir()
    code, _, err = run(["import", "--repo", str(target), "--from", str(tmp_path / "empty")], capsys)
    assert code == 2 and "no .zip files" in err
    not_a_library = tmp_path / "busy"
    (not_a_library / "something").mkdir(parents=True)
    code, _, err = run(["import", "--repo", str(not_a_library), "--from", str(folder)], capsys)
    assert code == 2 and "refusing" in err
