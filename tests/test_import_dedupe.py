"""Imports recognise archives the library already holds (D31, plan section 20 M18).

An exported snapshot counts as imported when an ``import`` snapshot of the target library has
the same original record id and original snapshot id in its ``imported_from``; it is skipped
and reported (not a failure) unless ``--reimport``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from enginesave.backup import execute_backup, prepare_backup
from enginesave.cli import main
from enginesave.export import (
    ALREADY_IMPORTED,
    FAILED,
    IMPORTED,
    SKIPPED,
    export_records,
    export_snapshot,
    import_archives,
    imported_snapshots,
    plan_import,
    preview_imports,
    zip_archives,
)
from enginesave.repo import Repo
from fixtures.builder import build_f1, build_f8


def back_up(games_root: Path, saves_root: Path, dest: Path) -> Repo:
    plan = prepare_backup([games_root], dest, saves_root_override=saves_root, include_unlinked=[])
    repo = Repo.open_or_create(plan.dest)
    assert all(r.status == "ok" for r in execute_backup(plan, repo).results)
    return repo


@pytest.fixture
def exported(games_root: Path, saves_root: Path, tmp_path: Path) -> tuple[Repo, Path]:
    """A game and two orphaned folders, exported into one folder: three archives."""
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "repo")
    ids = [r["game_id"] for r in repo.load_catalog()["games"]]
    folder = tmp_path / "exports"
    batch = export_records(repo, ids, folder, saves_root_override=saves_root)
    assert batch.count("exported") == 3
    return repo, folder


def results(batch) -> list[str]:
    return [item["result"] for item in batch.items]


def snapshot_count(repo_root: Path) -> int:
    repo = Repo.open(repo_root)
    return sum(len(repo.snapshot_ids(r["game_id"])) for r in repo.load_catalog()["games"])


def test_a_folder_imported_twice_is_skipped_the_second_time(
    exported: tuple[Repo, Path], tmp_path: Path
) -> None:
    _repo, folder = exported
    target = tmp_path / "target"
    first = import_archives(target, zip_archives(folder))
    assert results(first) == [IMPORTED] * 3
    count = snapshot_count(target)
    previews = preview_imports(target, zip_archives(folder))
    assert all(plan is not None and plan.already_imported for _a, plan, _e in previews)

    second = import_archives(target, zip_archives(folder))
    assert results(second) == [SKIPPED] * 3
    assert {item["reason"] for item in second.items} == {ALREADY_IMPORTED}
    assert second.exit_code == 0 and second.to_dict()["skipped"] == 3
    assert snapshot_count(target) == count, "a skipped archive adds no snapshot"
    done = first.items[0]["import"]
    earlier = {"game_id": done["game_id"], "snapshot_id": done["snapshot_id"]}
    assert second.items[0]["plan"]["already_imported"] == earlier

    again = import_archives(target, zip_archives(folder), reimport=True)
    assert results(again) == [IMPORTED] * 3, "--reimport imports them again"
    assert snapshot_count(target) == count + 3


def test_a_newer_snapshot_of_the_same_record_imports_normally(
    exported: tuple[Repo, Path], games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    repo, folder = exported
    target = tmp_path / "target"
    import_archives(target, zip_archives(folder))
    back_up(games_root, saves_root, repo.root)  # a second snapshot of every record
    game = next(r for r in repo.load_catalog()["games"] if r["kind"] == "game")
    newer = repo.latest_snapshot(game["game_id"])
    done = export_snapshot(
        repo,
        game["game_id"],
        newer,
        tmp_path / "newer" / "newer.zip",
        saves_root_override=saves_root,
    )
    plan = plan_import(target, Path(done.path))
    assert plan.matched_by == "game_id" and plan.already_imported is None, "same record, new id"
    assert results(import_archives(target, [Path(done.path)])) == [IMPORTED]


def test_mixed_folders_report_each_kind(exported: tuple[Repo, Path], tmp_path: Path) -> None:
    """Two already imported, one new, one broken: skipped items do not fail the run."""
    _repo, folder = exported
    target = tmp_path / "target"
    import_archives(target, zip_archives(folder)[:2])
    (folder / "zz-broken.zip").write_bytes(b"not a zip")
    batch = import_archives(target, zip_archives(folder))
    assert results(batch) == [SKIPPED, SKIPPED, IMPORTED, FAILED]
    assert (batch.count(IMPORTED), batch.count(SKIPPED), batch.count(FAILED)) == (1, 2, 1)
    assert batch.exit_code == 1, "the broken archive fails, the skipped ones do not"
    rest = import_archives(target, zip_archives(folder)[:3])
    assert results(rest) == [SKIPPED] * 3 and rest.exit_code == 0


def test_a_second_copy_in_the_same_folder_is_skipped(
    exported: tuple[Repo, Path], tmp_path: Path
) -> None:
    _repo, folder = exported
    first = zip_archives(folder)[0]
    shutil.copy(first, folder / "zz-copy.zip")
    target = tmp_path / "target"
    previews = preview_imports(target, zip_archives(folder))
    assert all(plan.already_imported is None for _a, plan, _e in previews), "nothing imported yet"
    duplicates = [plan.duplicate_of for _a, plan, _e in previews]
    assert duplicates == [None, None, None, first.name], "S-098: the preview says so too"
    batch = import_archives(target, zip_archives(folder))
    assert results(batch) == [IMPORTED, IMPORTED, IMPORTED, SKIPPED]


def test_cli_preview_counts_a_second_copy_as_skipped(
    exported: tuple[Repo, Path], tmp_path: Path, capsys
) -> None:
    """S-098: "N of M will be imported" matches what the import does."""
    _repo, folder = exported
    first = zip_archives(folder)[0]
    shutil.copy(first, folder / "zz-copy.zip")
    target = tmp_path / "target"
    code, out, _ = run(["import", "--repo", str(target), "--from", str(folder), "--yes"], capsys)
    assert code == 0 and "3 of 4 archives will be imported" in out
    assert f"(same snapshot as {first.name} in this folder)" in out
    assert "1 archive(s) repeat a snapshot of an earlier archive" in out
    assert "Imported 3, skipped 1, failed 0." in out


def test_only_import_snapshots_count(exported: tuple[Repo, Path]) -> None:
    """Imported back into the library it came from: the manual snapshot is not an import."""
    repo, folder = exported
    archive = zip_archives(folder)[0]
    plan = plan_import(repo.root, archive)
    assert plan.matched_by == "game_id" and plan.already_imported is None
    assert results(import_archives(repo.root, [archive])) == [IMPORTED]
    assert results(import_archives(repo.root, [archive])) == [SKIPPED]
    assert len(imported_snapshots(repo)) == 1


def test_imports_without_imported_from_do_not_count(
    exported: tuple[Repo, Path], tmp_path: Path
) -> None:
    _repo, folder = exported
    target = tmp_path / "target"
    archive = zip_archives(folder)[0]
    done = import_archives(target, [archive]).items[0]["import"]
    repo = Repo.open(target)
    path = repo.snapshot_dir(done["game_id"], done["snapshot_id"]) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    del manifest["imported_from"]  # as an import from a tool that did not record its origin
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert imported_snapshots(repo) == {}
    assert plan_import(target, archive).already_imported is None


# ------------------------------------------------------------------------------------- CLI


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_skips_and_reimports(exported: tuple[Repo, Path], tmp_path: Path, capsys) -> None:
    _repo, folder = exported
    target = tmp_path / "target"
    archive = str(zip_archives(folder)[0])
    single = ["import", "--repo", str(target), "--from", archive]
    code, out, _ = run([*single, "--yes"], capsys)
    assert code == 0 and "Import summary" in out
    code, out, err = run([*single, "--json"], capsys)  # no --yes needed: nothing to write
    data = json.loads(out)
    assert code == 0 and data["result"] is None and data["reason"] == "already imported"
    assert data["plan"]["already_imported"]["snapshot_id"]
    assert "SKIPPED" in err and "--reimport" in err and "skipped unless --reimport" in err
    count = snapshot_count(target)
    code, out, _ = run([*single, "--yes", "--reimport", "--json"], capsys)
    assert code == 0 and json.loads(out)["result"]["snapshot_id"]
    assert snapshot_count(target) == count + 1

    whole = ["import", "--repo", str(target), "--from", str(folder)]
    code, out, _ = run([*whole, "--yes", "--json"], capsys)
    data = json.loads(out)
    assert code == 0 and (data["imported"], data["skipped"], data["failed"]) == (2, 1, 0)
    code, out, _ = run(whole, capsys)  # everything already imported: nothing to confirm
    assert code == 0 and "0 of 3 archives will be imported" in out
    assert "3 archive(s) are already in this repository" in out and "SKIPPED" in out
    code, _, err = run([*whole, "--reimport"], capsys)
    assert code == 2 and "--yes" in err, "importing again needs the confirmation"
