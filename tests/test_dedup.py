"""D18 (0.2.0): content-addressed object store, hard links, copy fallback, v1/v2 coexistence."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from enginesave import store
from enginesave.backup import BackupResult, execute_backup, prepare_backup
from enginesave.cli import main
from enginesave.repo import Repo
from enginesave.restore import execute_restore, plan_restore
from enginesave.verify import verify_objects, verify_repo
from fixtures.builder import BuiltGame, build_f1, reinstall_copy, stored, to_v1
from fixtures.fsprobe import needs_hard_links


def back_up(games_root: Path, saves_root: Path, dest: Path) -> tuple[Repo, BackupResult]:
    plan = prepare_backup([games_root], dest, saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    return repo, execute_backup(plan, repo)


def objects(repo: Repo) -> dict[str, int]:
    return {name: path.stat().st_size for name, path in repo.iter_objects()}


def downgrade_to_v1(repo: Repo, game_id: str, snapshot_id: str) -> None:
    """Turn a snapshot into the 0.1.0 layout: plain copies and manifest format 1."""
    to_v1(repo, game_id, snapshot_id)


@pytest.fixture
def f1(games_root: Path, saves_root: Path) -> BuiltGame:
    return build_f1(games_root, saves_root)


@needs_hard_links
def test_mirrors_share_one_object_per_content(
    f1: BuiltGame, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    repo, result = back_up(games_root, saves_root, tmp_path / "repo")
    (outcome,) = result.results
    assert (outcome.status, outcome.storage, outcome.file_count) == ("ok", "hardlink", 12)
    stored = objects(repo)
    assert len(stored) == len(f1.local_files) == 4, "local, global and sync/ are one content"
    assert sum(stored.values()) == sum(len(d) for d in f1.local_files.values())
    manifest = repo.load_manifest(outcome.game_id, outcome.snapshot_id)
    assert {f["sha256"] for f in manifest["files"]} == set(stored)


def test_second_backup_adds_no_objects(
    f1: BuiltGame, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    repo, _ = back_up(games_root, saves_root, tmp_path / "repo")
    before = objects(repo)
    repo, result = back_up(games_root, saves_root, tmp_path / "repo")
    assert result.results[0].status == "ok"
    assert objects(repo) == before
    assert all(c.clean for c in verify_repo(repo, repo.load_catalog()))
    assert verify_objects(repo).clean and verify_objects(repo).ok == 4


def test_falls_back_to_copies_without_hard_links(
    f1: BuiltGame,
    games_root: Path,
    saves_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_links(src: str, dst: str) -> None:
        raise OSError(1, "Incorrect function (simulated exFAT)")

    monkeypatch.setattr(os, "link", no_links)
    repo, result = back_up(games_root, saves_root, tmp_path / "repo")
    (outcome,) = result.results
    assert (outcome.status, outcome.storage) == ("ok", "copy")
    persistent = stored(repo, outcome.game_id, outcome.snapshot_id, "local", "persistent")
    assert persistent.stat().st_nlink == 1
    assert all(c.clean for c in verify_repo(repo, repo.load_catalog()))


@needs_hard_links
def test_mixed_storage_when_some_links_fail(
    f1: BuiltGame,
    games_root: Path,
    saves_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_link = os.link

    def limited(src: str, dst: str) -> None:
        if "sync" in Path(dst).parts:
            raise OSError(1142, "too many links (simulated NTFS limit)")
        real_link(src, dst)

    monkeypatch.setattr(os, "link", limited)
    repo, result = back_up(games_root, saves_root, tmp_path / "repo")
    assert (result.results[0].status, result.results[0].storage) == ("ok", "mixed")
    assert all(c.clean for c in verify_repo(repo, repo.load_catalog()))


@pytest.mark.parametrize("same_size", [False, True])
@needs_hard_links
def test_damaged_object_is_bypassed_not_modified(
    f1: BuiltGame, games_root: Path, saves_root: Path, tmp_path: Path, same_size: bool
) -> None:
    repo, first = back_up(games_root, saves_root, tmp_path / "repo")
    manifest = repo.load_manifest(first.results[0].game_id, first.results[0].snapshot_id)
    digest = next(f["sha256"] for f in manifest["files"] if f["path"] == "persistent")
    damaged = repo.object_path(digest)
    size = damaged.stat().st_size
    damage = b"x" * size if same_size else b"bit rot"
    damaged.write_bytes(damage)

    repo, second = back_up(games_root, saves_root, tmp_path / "repo")
    (outcome,) = second.results
    assert (outcome.status, outcome.storage) == ("ok", "mixed")
    assert damaged.read_bytes() == damage, "objects are never rewritten"
    checks = {c.snapshot_id: c for c in verify_repo(repo, repo.load_catalog())}
    assert checks[outcome.snapshot_id].clean
    assert not checks[first.results[0].snapshot_id].clean
    assert verify_objects(repo).mismatched == [digest]


def test_failed_backup_leaves_no_object_temp_files(
    f1: BuiltGame,
    games_root: Path,
    saves_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(path: Path) -> tuple[str, int]:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store, "hash_file", boom)
    repo, result = back_up(games_root, saves_root, tmp_path / "repo")
    assert result.results[0].status == "failed"
    assert list(repo.object_tmp_dir().iterdir()) == []
    assert repo.stale_tmp_dirs() == []


def test_v1_and_v2_snapshots_coexist(
    f1: BuiltGame, games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    dest = tmp_path / "repo"
    repo, first = back_up(games_root, saves_root, dest)
    game_id, v1_id = first.results[0].game_id, first.results[0].snapshot_id
    downgrade_to_v1(repo, game_id, v1_id)
    repo, second = back_up(games_root, saves_root, dest)
    v2_id = second.results[0].snapshot_id
    assert repo.load_manifest(game_id, v1_id)["format_version"] == 1
    assert repo.load_manifest(game_id, v2_id)["format_version"] == 3  # linked, readable (D67 3)
    assert [c.clean for c in verify_repo(repo, repo.load_catalog())] == [True, True]

    assert main(["show", "--repo", str(dest), "--game", "readable game"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("  2026")]
    assert "storage" not in lines[0] and lines[1].endswith("storage: hardlink")

    target = reinstall_copy(f1.root, tmp_path / "restored")
    restore = plan_restore(
        repo,
        repo.load_catalog(),
        game_id,
        snapshot=v1_id,
        game_root=target,
        saves_root_override=tmp_path / "fake-appdata",
    )
    assert execute_restore(restore).written == 12
    assert (target / "game" / "saves" / "persistent").read_bytes() == f1.local_files["persistent"]


def test_verify_objects_cli(
    f1: BuiltGame, games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    dest = tmp_path / "repo"
    repo, _ = back_up(games_root, saves_root, dest)
    assert main(["verify", "--repo", str(dest), "--objects"]) == 0
    assert "Objects: 4 ok, 0 mismatched, 0 unexpected" in capsys.readouterr().out
    stray = repo.object_path("0" * 64).parent / "not-a-digest"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"?")
    assert main(["verify", "--repo", str(dest), "--objects", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["objects"]["unexpected"] == ["00/not-a-digest"]
    assert main(["verify", "--repo", str(dest), "--json"]) == 0
    assert "objects" not in json.loads(capsys.readouterr().out)
