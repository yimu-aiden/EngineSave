from __future__ import annotations

import json
from pathlib import Path

import pytest

from enginesave.backup import execute_backup, prepare_backup
from enginesave.errors import UsageError
from enginesave.repo import Repo
from enginesave.verify import verify_repo
from fixtures.builder import build_f1, stored
from fixtures.fsprobe import needs_hard_links


@pytest.fixture
def backed_up(games_root: Path, saves_root: Path, tmp_path: Path) -> tuple[Repo, str, str]:
    build_f1(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "backup", saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    outcome = execute_backup(plan, repo).results[0]
    return repo, outcome.game_id, outcome.snapshot_id


def data_dir(repo: Repo, game_id: str, snapshot_id: str) -> dict[str, Path]:
    """Each source's folder in the snapshot (D67 3)."""
    return {s: stored(repo, game_id, snapshot_id, s) for s in ("local", "global")}


@needs_hard_links
def test_detects_mismatch_missing_and_extra(backed_up: tuple[Repo, str, str]) -> None:
    repo, game_id, snapshot_id = backed_up
    data = data_dir(repo, game_id, snapshot_id)
    # Writing into a data file writes through the hard link into the shared object, so every
    # path stored with the same content (local, global, global/sync mirrors) is damaged.
    (data["local"] / "1-1-LT1.save").write_bytes(b"tampered")
    (data["global"] / "sync" / "persistent").unlink()
    (data["global"] / "stray.save").write_bytes(b"x")
    (check,) = verify_repo(repo, repo.load_catalog(), game_id)
    assert not check.clean
    assert check.mismatched == [
        "local/1-1-LT1.save",
        "global/1-1-LT1.save",
        "global/sync/1-1-LT1.save",
    ]
    assert check.missing == ["global/sync/persistent"]
    assert check.extra == ["RENPY_SAVES_ROOT/ReadableGame-1600000001/stray.save"]  # D67 3
    assert check.ok == 8


def test_replaced_link_only_affects_that_path(backed_up: tuple[Repo, str, str]) -> None:
    repo, game_id, snapshot_id = backed_up
    data = data_dir(repo, game_id, snapshot_id)
    target = data["local"] / "1-1-LT1.save"
    target.unlink()
    target.write_bytes(b"tampered")
    (check,) = verify_repo(repo, repo.load_catalog(), game_id)
    assert check.mismatched == ["local/1-1-LT1.save"]
    assert check.ok == 11


def test_unsafe_manifest_paths_are_problems(backed_up: tuple[Repo, str, str]) -> None:
    repo, game_id, snapshot_id = backed_up
    manifest_path = repo.snapshot_dir(game_id, snapshot_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../../../../evil.save"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (check,) = verify_repo(repo, repo.load_catalog(), game_id, snapshot_id)
    assert check.problems and "unsafe" in check.problems[0]
    assert not check.clean


def test_unreadable_manifest_is_a_problem(backed_up: tuple[Repo, str, str]) -> None:
    repo, game_id, snapshot_id = backed_up
    (repo.snapshot_dir(game_id, snapshot_id) / "manifest.json").write_text("{broken")
    (check,) = verify_repo(repo, repo.load_catalog())
    assert check.problems and not check.clean


def test_argument_errors(backed_up: tuple[Repo, str, str]) -> None:
    repo, game_id, _ = backed_up
    catalog = repo.load_catalog()
    with pytest.raises(UsageError, match="requires --game"):
        verify_repo(repo, catalog, None, "20260101T000000Z")
    with pytest.raises(UsageError, match="no snapshot"):
        verify_repo(repo, catalog, game_id, "20200101T000000Z")
