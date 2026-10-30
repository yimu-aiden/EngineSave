"""Deleting backups (0.8.0, D49): whole records, some snapshots, and the object store; only
ever on libraries made in ``tmp_path`` (rule 16)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from enginesave import repo as repo_module
from enginesave.backup import execute_backup, prepare_backup
from enginesave.cli import main
from enginesave.removal import execute_remove, inventory, plan_remove
from enginesave.repo import Repo
from enginesave.verify import verify_objects, verify_repo
from fixtures.builder import build_f1, build_f18
from fixtures.fsprobe import needs_hard_links


def back_up(roots: list[Path], dest: Path, saves_root: Path, **options: object) -> Repo:
    plan = prepare_backup(roots, dest, saves_root_override=saves_root, **options)
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    return repo


def objects(repo: Repo) -> set[str]:
    return {name for name, _path in repo.iter_objects()}


def digests(repo: Repo, game_id: str) -> set[str]:
    return {
        f["sha256"]
        for snapshot_id in repo.snapshot_ids(game_id)
        for f in repo.load_manifest(game_id, snapshot_id)["files"]
    }


def all_clean(repo: Repo) -> bool:
    checks = verify_repo(repo, repo.load_catalog())
    return all(c.clean for c in checks) and verify_objects(repo).clean


@pytest.fixture
def seasons(games_root: Path, saves_root: Path, tmp_path: Path) -> tuple[Repo, str, str]:
    """F18 backed up: two records that share the global folder's objects (D48)."""
    one, _two = build_f18(games_root, saves_root)
    repo = back_up([games_root], tmp_path / "repo", saves_root)
    ids = [r["game_id"] for r in repo.load_catalog()["games"]]
    first = next(i for i in ids if repo.load_game(i)["installs"][0]["game_root"] == str(one.root))
    second = next(i for i in ids if i != first)
    return repo, first, second


def two_snapshots(games_root: Path, saves_root: Path, tmp_path: Path) -> tuple[Repo, str, set[str]]:
    """F1 backed up twice, every save changed in between: ``(repo, game id, the digests only
    the first snapshot holds)``."""
    built = build_f1(games_root, saves_root)
    repo = back_up([games_root], tmp_path / "repo", saves_root)
    assert built.global_dir is not None
    for folder in (built.local_dir, built.global_dir):
        for path in folder.rglob("*"):
            if path.is_file():
                path.write_bytes(path.read_bytes() + b" changed")
    back_up([games_root], tmp_path / "repo", saves_root)
    (record,) = repo.load_catalog()["games"]
    old, new = repo.snapshot_ids(record["game_id"])
    only_old = {f["sha256"] for f in repo.load_manifest(record["game_id"], old)["files"]}
    only_old -= {f["sha256"] for f in repo.load_manifest(record["game_id"], new)["files"]}
    assert only_old, "the first snapshot holds versions the second does not"
    return repo, record["game_id"], only_old


def test_removing_a_record_keeps_the_objects_another_record_uses(
    seasons: tuple[Repo, str, str],
) -> None:
    """D49 2: the catalog entry, every override of the record and its folder go; the objects it
    shares with the other season stay, the other record still verifies."""
    repo, first, second = seasons
    shared = digests(repo, first) & digests(repo, second)
    assert shared, "the seasons share the global folder"
    assert any(o["game_id"] == first for o in repo.load_catalog()["overrides"])
    plan = plan_remove(repo, repo.load_catalog(), [(first, None)])
    (item,) = plan.items
    assert item.whole and (plan.records, plan.snapshots) == (1, 1) and plan.gc_blocked is None
    manifest = repo.snapshot_dir(first, item.snapshot_ids[0]) / "manifest.json"
    files = repo.load_manifest(first, item.snapshot_ids[0])["files"]
    sizes = {f["sha256"]: f["size"] for f in files}
    unique = digests(repo, first) - digests(repo, second)
    expected = manifest.stat().st_size + sum(sizes[d] for d in unique)
    assert plan.freed_bytes == expected
    result = execute_remove(plan)
    assert (result.records, result.snapshots, result.leftovers) == (1, 1, [])
    catalog = repo.load_catalog()
    assert [r["game_id"] for r in catalog["games"]] == [second]
    assert all(o["game_id"] != first for o in catalog["overrides"])
    assert not repo.game_dir(first).exists()
    assert objects(repo) == digests(repo, second), "what the other record uses, nothing else"
    assert all_clean(repo) and result.exit_code == 0
    assert result.freed_bytes == expected


def test_removing_some_snapshots_collects_only_their_own_objects(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    repo, game_id, only_old = two_snapshots(games_root, saves_root, tmp_path)
    old, new = repo.snapshot_ids(game_id)
    result = execute_remove(plan_remove(repo, repo.load_catalog(), [(game_id, [old])]))
    assert (result.records, result.snapshots, result.garbage.removed) == (0, 1, len(only_old))
    assert repo.snapshot_ids(game_id) == [new]
    (entry,) = repo.load_catalog()["games"]
    assert (entry["snapshot_count"], entry["latest_snapshot"]) == (1, new)
    assert not objects(repo) & only_old and all_clean(repo)


@needs_hard_links
def test_an_unused_object_something_still_links_to_is_kept(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """An object no manifest names any more but a file still links to is left alone."""
    repo, game_id, only_old = two_snapshots(games_root, saves_root, tmp_path)
    old, _new = repo.snapshot_ids(game_id)
    victim = sorted(only_old)[0]
    os.link(repo.object_path(victim), tmp_path / "elsewhere.bin")
    result = execute_remove(plan_remove(repo, repo.load_catalog(), [(game_id, [old])]))
    assert result.garbage.kept_linked == 1 and result.garbage.removed == len(only_old) - 1
    assert victim in objects(repo)


def test_an_unreadable_manifest_keeps_the_object_store(seasons: tuple[Repo, str, str]) -> None:
    """D49 2: the record and its snapshots still go; the objects are not collected, and why."""
    repo, first, second = seasons
    damaged = repo.snapshot_dir(second, repo.snapshot_ids(second)[0]) / "manifest.json"
    damaged.write_text("{ not json", encoding="utf-8")
    before = objects(repo)
    plan = plan_remove(repo, repo.load_catalog(), [(first, None)])
    assert plan.gc_blocked == "1 snapshot manifest(s) could not be read"
    result = execute_remove(plan)
    assert result.records == 1 and not repo.game_dir(first).exists()
    assert result.garbage.skipped == "1 snapshot manifest(s) could not be read"
    assert objects(repo) == before and result.exit_code == 1


def test_an_interrupted_removal_leaves_a_folder_the_hygiene_check_reports(
    seasons: tuple[Repo, str, str], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The catalog is written first; a removal stopped right after it leaves the record's
    folder, which ``list`` and ``verify`` report and the object store still respects."""
    repo, first, _second = seasons
    before = objects(repo)

    def stopped(_path: Path) -> list[str]:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:  # undo only this (not the sandboxed APPDATA)
        patch.setattr(repo_module, "remove_tree", stopped)
        with pytest.raises(KeyboardInterrupt):
            repo.remove_game(first)
    assert all(r["game_id"] != first for r in repo.load_catalog()["games"])
    assert repo.orphan_game_dirs() == [repo.game_dir(first)]
    assert main(["list", "--repo", str(repo.root), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["orphan_record_dirs"]
    assert listed == [str(repo.game_dir(first))]
    assert main(["verify", "--repo", str(repo.root)]) == 0
    assert "record folder not in the catalog" in capsys.readouterr().out
    assert repo.collect_garbage().removed == 0 and objects(repo) == before


def test_temporary_folders_of_a_backup_keep_the_object_store(
    seasons: tuple[Repo, str, str],
) -> None:
    """Nothing is collected while an interrupted (or running) backup left temporary folders."""
    repo, first, second = seasons
    leftover = repo.new_tmp_dir(second, "20260101T000000Z")
    plan = plan_remove(repo, repo.load_catalog(), [(first, None)])
    assert plan.gc_blocked == "1 temporary folder(s) of an interrupted backup"
    result = execute_remove(plan)
    assert result.records == 1 and result.exit_code == 1
    assert result.garbage.skipped == "1 temporary folder(s) of an interrupted backup"
    assert leftover.is_dir()


def test_remove_on_the_command_line_previews_then_deletes_with_yes(
    seasons: tuple[Repo, str, str], capsys
) -> None:
    repo, first, second = seasons
    args = ["remove", "--repo", str(repo.root), "--game", first]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "Nothing was deleted. Add --yes" in out and "whole record" in out
    assert repo.has_game(first), "a preview deletes nothing"
    assert main([*args, "--dry-run", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] is None and repo.has_game(first)
    assert main([*args, "--yes", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["dry_run"] is False and data["result"]["records"] == 1
    assert not repo.has_game(first) and repo.has_game(second)
    assert main(["remove", "--repo", str(repo.root), "--game", second, "--snapshot", "x"]) == 2
    assert main(["remove", "--repo", str(repo.root), "--game", "nothing like it"]) == 2


def test_the_whole_record_goes_when_every_snapshot_is_named(
    seasons: tuple[Repo, str, str],
) -> None:
    repo, first, _second = seasons
    plan = plan_remove(repo, repo.load_catalog(), [(first, repo.snapshot_ids(first))])
    assert plan.items[0].whole and plan.records == 1
    assert (first, repo.snapshot_ids(first)[0]) in inventory(repo).snapshots
