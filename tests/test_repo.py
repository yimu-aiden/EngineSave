from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enginesave.errors import FatalError, UsageError
from enginesave.repo import (
    CATALOG,
    Repo,
    base_snapshot_id,
    destination_state,
    snapshot_sort_key,
)

MOMENT = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)


def manifest(snapshot_id: str, reason: str = "manual") -> dict:
    return {"format_version": 1, "snapshot_id": snapshot_id, "reason": reason}


def test_create_and_reopen(tmp_path: Path) -> None:
    dest = tmp_path / "repo"
    repo = Repo.open_or_create(dest)
    catalog = repo.load_catalog()
    assert catalog["format_version"] == 2  # D67 8
    assert catalog["games"] == [] and catalog["overrides"] == []
    assert Repo.open_or_create(dest).root == dest
    assert destination_state(dest) == "existing"


def test_refuses_non_empty_foreign_folder(tmp_path: Path) -> None:
    (tmp_path / "someone-elses.txt").write_text("x")
    with pytest.raises(UsageError, match="not empty"):
        Repo.open_or_create(tmp_path)
    assert destination_state(tmp_path / "missing") == "new"
    (tmp_path / "empty").mkdir()
    assert destination_state(tmp_path / "empty") == "new"
    with pytest.raises(UsageError):
        Repo.open(tmp_path / "empty")


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"format_version": 99, "games": []}', '{"format_version": 1}']
)
def test_corrupt_catalog_is_fatal(tmp_path: Path, content: str) -> None:
    (tmp_path / CATALOG).write_text(content)
    with pytest.raises(FatalError):
        Repo.open(tmp_path)


def test_snapshot_ids_and_same_second_collisions(tmp_path: Path) -> None:
    repo = Repo.open_or_create(tmp_path / "repo")
    game_id = str(uuid.uuid4())
    base = MOMENT.astimezone().strftime("%Y-%m-%d %H-%M-%S")  # D67 2: local time
    assert base_snapshot_id(MOMENT) == base
    committed = []
    for _ in range(3):
        snapshot_id = repo.next_snapshot_id(game_id, MOMENT)
        tmp = repo.new_tmp_dir(game_id, snapshot_id)
        (tmp / "data").mkdir()
        committed.append(repo.commit_snapshot(tmp, game_id, manifest(snapshot_id)))
    assert committed == [base, f"{base} (2)", f"{base} (3)"]
    assert repo.snapshot_ids(game_id) == committed
    for snapshot_id in committed:
        assert repo.load_manifest(game_id, snapshot_id)["snapshot_id"] == snapshot_id
    assert repo.stale_tmp_dirs() == []


def test_commit_skips_ids_taken_concurrently(tmp_path: Path) -> None:
    repo = Repo.open_or_create(tmp_path / "repo")
    game_id = str(uuid.uuid4())
    snapshot_id = repo.next_snapshot_id(game_id, MOMENT)
    tmp = repo.new_tmp_dir(game_id, snapshot_id)
    repo.snapshot_dir(game_id, snapshot_id).mkdir()  # someone else committed meanwhile
    final = repo.commit_snapshot(tmp, game_id, manifest(snapshot_id))
    assert final == f"{base_snapshot_id(MOMENT)} (2)"
    assert (
        json.loads((repo.snapshot_dir(game_id, final) / "manifest.json").read_text())["snapshot_id"]
        == final
    )


def test_sort_key_orders_numeric_suffixes() -> None:
    ids = ["20261023T120000Z-10", "20261023T120000Z", "20261023T120000Z-2", "20261024T000000Z"]
    assert sorted(ids, key=snapshot_sort_key) == [
        "20261023T120000Z",
        "20261023T120000Z-2",
        "20261023T120000Z-10",
        "20261024T000000Z",
    ]
    new = ["2026-10-23 12-00-00 (10)", "2026-10-23 12-00-00", "2026-10-23 12-00-00 (2)"]
    assert sorted(new, key=snapshot_sort_key) == [
        "2026-10-23 12-00-00",
        "2026-10-23 12-00-00 (2)",
        "2026-10-23 12-00-00 (10)",
    ]


def test_invalid_ids_are_rejected(tmp_path: Path) -> None:
    repo = Repo.open_or_create(tmp_path / "repo")
    with pytest.raises(FatalError):
        repo.game_dir("../../etc")
    with pytest.raises(UsageError):
        repo.snapshot_dir(str(uuid.uuid4()), "..")


def test_resolve_game_by_id_prefix_save_directory_and_name(tmp_path: Path) -> None:
    repo = Repo.open_or_create(tmp_path / "repo")
    a, b = "aaaaaaaa-0000-4000-8000-000000000001", "aaaaaaab-0000-4000-8000-000000000002"
    catalog = {
        "games": [
            {
                "game_id": a,
                "display_name": "Moonlit Harbor",
                "identity": {"renpy_save_directory": "MoonlitHarbor"},
            },
            {
                "game_id": b,
                "display_name": "Other",
                "identity": {"renpy_save_directory": "Other-1"},
            },
        ]
    }
    assert repo.resolve_game(catalog, a)["game_id"] == a
    assert repo.resolve_game(catalog, "aaaaaaab")["game_id"] == b
    assert repo.resolve_game(catalog, "moonlitharbor")["game_id"] == a
    assert repo.resolve_game(catalog, "other")["game_id"] == b
    with pytest.raises(UsageError, match="ambiguous"):
        repo.resolve_game(catalog, "aaaaaaa")
    with pytest.raises(UsageError, match="no game"):
        repo.resolve_game(catalog, "nothing")


def test_latest_prefers_manual_snapshots(tmp_path: Path) -> None:
    repo = Repo.open_or_create(tmp_path / "repo")
    game_id = str(uuid.uuid4())
    assert repo.latest_snapshot(game_id) is None
    for reason in ("manual", "pre_restore"):
        snapshot_id = repo.next_snapshot_id(game_id, MOMENT)
        tmp = repo.new_tmp_dir(game_id, snapshot_id)
        repo.commit_snapshot(tmp, game_id, manifest(snapshot_id, reason))
    assert repo.latest_snapshot(game_id) == base_snapshot_id(MOMENT)
