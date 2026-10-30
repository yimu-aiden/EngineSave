"""EngineSave's own items in the recycle bin (D41 6): read, matched, purged; nothing else is
touched. Every test works on a synthetic ``$RECYCLE.BIN`` in its temporary folder (rule 16)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import SyntheticBin
from enginesave.backup import execute_backup, prepare_backup
from enginesave.cleanup import (
    GAME_ROOT,
    GLOBAL,
    cleaned_entry,
    execute_cleanup,
    plan_cleanup,
    recycle_bin,
)
from enginesave.fsutil import ns_from_iso
from enginesave.repo import Repo
from fixtures.builder import build_f1, recycle_info_bytes, recycle_like_windows

MINUTE = 60 * 1_000_000_000


def test_info_files_are_read_as_windows_writes_them() -> None:
    when = 1_761_300_000 * 1_000_000_000
    for version in (2, 1):
        data = recycle_info_bytes("E:\\Games\\Some Game", 1234, when, version=version)
        assert recycle_bin.parse_info(data) == ("E:\\Games\\Some Game", 1234, when)
    good = recycle_info_bytes("E:\\x", 1, when)
    assert recycle_bin.parse_info(good[:20]) is None, "truncated"
    assert recycle_bin.parse_info(good[:-4]) is None, "path shorter than its length"
    assert recycle_bin.parse_info(b"\x03" + good[1:]) is None, "unknown version"
    assert recycle_bin.parse_info(b"\0" * 600) is None


@pytest.fixture
def recycled(games_root: Path, saves_root: Path, tmp_path: Path, synthetic_bin: SyntheticBin):
    """A backed-up game whose folder and global folder a cleanup moved into the bin."""
    built = build_f1(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "repo", saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    execute_backup(plan, repo)
    (record,) = repo.load_catalog()["games"]
    cleanup = plan_cleanup(
        repo, record, [(GAME_ROOT, None), (GLOBAL, None)], saves_root_override=saves_root
    )
    result = execute_cleanup(cleanup)
    assert [t["result"] for t in result.targets] == ["recycled", "recycled"]
    return repo, record, built, Path(built.root).anchor


def test_only_enginesaves_own_items_are_offered(recycled, synthetic_bin: SyntheticBin) -> None:
    """Exactly the folders a cleanup recycled, matched by path and time; a stranger, the same
    path deleted long before, and an unreadable $I are never offered."""
    repo, record, built, drive = recycled
    foreign = synthetic_bin.folder.parent.parent / "Foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("not ours", encoding="utf-8")
    recycle_like_windows(foreign, synthetic_bin.folder, time.time_ns())
    old = synthetic_bin.folder.parent.parent / "old-copy"
    old.mkdir()
    info, _ = recycle_like_windows(old, synthetic_bin.folder, time.time_ns() - 30 * MINUTE)
    info.write_bytes(recycle_info_bytes(str(built.root), 5, time.time_ns() - 30 * MINUTE))
    broken = synthetic_bin.folder / "$IBROKEN"
    broken.write_bytes(b"\x02" + b"\0" * 10)
    (synthetic_bin.folder / "$RBROKEN").mkdir()
    found = recycle_bin.ours(repo, [drive])
    assert [(o.kind, o.item.original) for o in found] == [
        (GAME_ROOT, str(built.root)),
        (GLOBAL, str(built.global_dir)),
    ]
    assert {o.game_id for o in found} == {record["game_id"]}
    assert len(synthetic_bin.pairs()) == 5, "two of ours, three strangers"


def test_the_closest_in_time_answers_for_a_cleanup(recycled, synthetic_bin: SyntheticBin) -> None:
    repo, _record, built, drive = recycled
    (real,) = [o for o in recycle_bin.ours(repo, [drive]) if o.kind == GAME_ROOT]
    near = synthetic_bin.folder.parent.parent / "near"
    near.mkdir()
    recorded = ns_from_iso(real.recorded_at)
    info, _ = recycle_like_windows(near, synthetic_bin.folder, recorded + MINUTE)
    info.write_bytes(recycle_info_bytes(str(built.root), 5, recorded + MINUTE))
    (again,) = [o for o in recycle_bin.ours(repo, [drive]) if o.kind == GAME_ROOT]
    assert again.item.info == real.item.info, "the one recorded closest in time"


def test_purging_deletes_only_the_chosen_pair_and_notes_it(
    recycled, synthetic_bin: SyntheticBin
) -> None:
    repo, record, built, drive = recycled
    stranger = synthetic_bin.folder.parent.parent / "Stranger"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("not ours", encoding="utf-8")
    s_info, s_data = recycle_like_windows(stranger, synthetic_bin.folder, time.time_ns())
    game, global_ = recycle_bin.ours(repo, [drive])
    (outcome,) = recycle_bin.purge(repo, [game])
    assert outcome["result"] == "purged" and outcome["path"] == str(built.root)
    assert not game.item.info.exists() and not game.item.data.exists()
    assert global_.item.info.exists() and global_.item.data.is_dir()
    assert s_info.exists() and (s_data / "keep.txt").read_text(encoding="utf-8") == "not ours"
    doc = repo.load_game(record["game_id"])
    cleaned = cleaned_entry(doc, built.root)
    assert cleaned is not None and cleaned["purged_at"]
    note = doc["cleanups"][0]["targets"][0]["purged_from_recycle_bin"]
    assert note["recycled_at"] == game.item.deleted_at and note["bytes"] == game.item.size
    assert [o.kind for o in recycle_bin.ours(repo, [drive])] == [GLOBAL], "not offered twice"


def test_an_entry_that_changed_is_left_alone(recycled, synthetic_bin: SyntheticBin) -> None:
    repo, _record, _built, drive = recycled
    game, _global = recycle_bin.ours(repo, [drive])
    game.item.info.write_bytes(recycle_info_bytes("E:\\Elsewhere", 1, game.item.deleted_ns))
    (outcome,) = recycle_bin.purge(repo, [game])
    assert outcome["result"] == "not_found" and game.item.data.is_dir()


def test_no_test_sees_the_real_recycle_bin() -> None:
    """conftest points the bin folder at nothing unless a test brings a synthetic one."""
    assert recycle_bin.list_items("C:\\") == [] and recycle_bin.bin_folder("C:\\") is None
