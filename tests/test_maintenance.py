"""Library upkeep (0.9.0): the temporary folders interrupted runs leave (D55) and the
overview (D56). Only on libraries built in temporary folders."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from enginesave.backup import execute_backup, prepare_backup
from enginesave.cli import main
from enginesave.removal import LibraryStats, inventory, library_stats
from enginesave.repo import RECENT_SECONDS, Repo, tmp_folder
from fixtures.builder import build_f1, build_f8, build_game, simple_spec
from fixtures.fsprobe import needs_hard_links


@pytest.fixture
def library(games_root: Path, saves_root: Path, tmp_path: Path) -> Repo:
    build_f1(games_root, saves_root)
    build_game(games_root, simple_spec("Second", "Second", "Second-1600000002"), saves_root)
    build_f8(saves_root)
    plan = prepare_backup(
        [games_root], tmp_path / "library", saves_root_override=saves_root, include_unlinked=[]
    )
    repo = Repo.open_or_create(plan.dest)
    execute_backup(plan, repo)
    return repo


def leftovers(repo: Repo) -> tuple[Path, Path]:
    """An old ``.tmp-*`` snapshot folder (an hour ago) and a fresh ``objects/.tmp`` file."""
    game_id = repo.load_catalog()["games"][0]["game_id"]
    old = repo.snapshots_dir(game_id) / ".tmp-20261024T000000Z-abc123"
    (old / "data" / "local").mkdir(parents=True)
    (old / "data" / "local" / "1-1-LT1.save").write_bytes(b"x" * 100)
    hour_ago = time.time() - 3600
    for path in [*old.rglob("*"), old]:
        os.utime(path, (hour_ago, hour_ago))
    fresh = repo.object_tmp_dir() / "0123456789abcdef"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_bytes(b"y" * 10)
    return old, fresh


def test_leftovers_are_listed_with_size_and_time(library: Repo) -> None:
    """The old snapshot folder counts as in use while the fresh file in ``objects/.tmp``
    is being written (D59 4); once that is old too, it does not."""
    old, fresh = leftovers(library)
    found = {f.path: f for f in library.tmp_folders()}
    assert set(found) == {old, fresh}
    assert (found[old].bytes, found[old].files, found[old].recent()) == (100, 1, True)
    assert found[old].held_by_store() and not found[fresh].held_by_store()
    assert (found[fresh].bytes, found[fresh].files, found[fresh].recent()) == (10, 1, True)
    assert found[old].to_dict()["modified"].endswith("Z") and found[old].to_dict()["recent"]
    age(fresh)
    found = {f.path: f for f in library.tmp_folders()}
    assert not found[old].recent() and not found[fresh].recent()


def test_old_leftovers_go_recent_ones_stay_and_then_the_store_is_collected(
    library: Repo,
) -> None:
    """D55: the one changed within ten minutes is kept (and blocks the collection, D49), and
    while it is a file in ``objects/.tmp`` the old snapshot folder is kept too (D59 4: a
    large file of the same backup may still be being written); once it is old, everything
    goes and the store is collected."""
    old, fresh = leftovers(library)
    orphan = library.object_path("ab" * 32)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"nobody links to me")
    first = library.remove_tmp_dirs()
    assert first.removed == [] and first.recent == [str(old), str(fresh)] and first.store_busy
    assert old.exists() and fresh.exists() and orphan.exists()
    assert first.garbage is not None and first.garbage.skipped and first.exit_code == 1
    assert first.to_dict()["store_busy"] is True
    long_ago = time.time() - RECENT_SECONDS - 60
    os.utime(fresh, (long_ago, long_ago))
    second = library.remove_tmp_dirs()
    assert second.removed == [str(old), str(fresh)] and second.garbage.removed == 1
    assert not second.store_busy and "store_busy" not in second.to_dict()
    assert not orphan.exists() and second.exit_code == 0
    assert library.stale_tmp_dirs() == []


def test_only_the_leftovers_are_ever_touched(library: Repo, tmp_path: Path) -> None:
    """A path that is not a leftover is ignored, also when asked for by name; a junction
    in ``objects/.tmp`` is reported, not followed."""
    from fixtures.builder import make_junction

    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    snapshots = library.snapshots_dir(library.load_catalog()["games"][0]["game_id"])
    real = next(p for p in snapshots.iterdir())
    result = library.remove_tmp_dirs([outside, real])
    assert result.removed == [] and real.exists() and outside.exists()
    link = library.object_tmp_dir() / "linked"
    library.object_tmp_dir().mkdir(parents=True, exist_ok=True)
    if not make_junction(link, outside):
        pytest.skip("cannot create a junction here")
    result = library.remove_tmp_dirs(older_than=0)  # a junction's own time cannot be set here
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert str(link) in result.leftovers or str(link) in result.removed


def old_tmp(repo: Repo, record: int, name: str) -> Path:
    """A ``.tmp-*`` snapshot folder of the record, last changed an hour ago."""
    game_id = repo.load_catalog()["games"][record]["game_id"]
    folder = repo.snapshots_dir(game_id) / name
    (folder / "data" / "local").mkdir(parents=True)
    (folder / "data" / "local" / "own.save").write_bytes(b"z" * 50)
    return folder


def age(folder: Path, seconds: float = 3600) -> None:
    then = time.time() - seconds
    for path in [*folder.rglob("*"), folder]:
        if not os.path.isjunction(path):
            os.utime(path, (then, then))


def test_only_the_ticked_leftovers_go_each_looked_at_again(library: Repo) -> None:
    """D55: a choice deletes only what was ticked; one changed since the list was read is
    kept although it was ticked."""
    old, fresh = leftovers(library)
    age(fresh)  # nothing being written into the store
    second = old_tmp(library, 1, ".tmp-20261024T000001Z-def456")
    age(second)
    result = library.remove_tmp_dirs([second])
    assert result.removed == [str(second)] and old.exists() and fresh.exists()
    os.utime(old, None)  # changed after the list was read
    result = library.remove_tmp_dirs([old])
    assert result.removed == [] and result.recent == [str(old)] and old.exists()


@needs_hard_links
def test_hard_links_into_the_store_free_nothing_by_themselves(
    library: Repo, tmp_path: Path
) -> None:
    """A leftover's hard link to an object in use frees no space when it goes (S-167), and a
    junction in it is neither counted nor followed."""
    from fixtures.builder import make_junction

    folder = old_tmp(library, 0, ".tmp-20261024T000002Z-aaa111")
    name, obj = next(iter(library.iter_objects()))
    os.link(obj, folder / "data" / "local" / "linked.save")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"b" * 4096)
    junction = make_junction(folder / "data" / "j", outside)
    age(folder)
    found = tmp_folder(folder)
    assert (found.bytes, found.own_bytes) == (obj.stat().st_size + 50, 50)
    result = library.remove_tmp_dirs([folder], older_than=0)  # a junction's time is now
    assert result.removed == [str(folder)] and result.freed_bytes == 50
    assert obj.exists() and not folder.exists()
    if junction:
        assert (outside / "big.bin").stat().st_size == 4096, "the junction was not followed"


def test_a_read_only_leftover_file_is_deleted(library: Repo) -> None:
    path = library.object_tmp_dir() / "0123456789abcdef"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"r" * 20)
    os.chmod(path, stat.S_IREAD)
    age(path)
    result = library.remove_tmp_dirs()
    assert result.removed == [str(path)] and not path.exists() and result.freed_bytes == 20


def test_a_snapshot_folder_still_open_is_kept_and_a_snapshot_never_commits_without_its_files(
    library: Repo,
) -> None:
    """A backup reading a file back for a long time changes nothing on disk, yet still
    holds the file open: the folder is renamed before it is deleted, which Windows refuses
    then, so it is kept as in use. And should a temporary folder lose files anyway, the
    snapshot is not committed."""
    import sys

    from enginesave.errors import FatalError

    folder = old_tmp(library, 0, ".tmp-20261024T000005Z-bbb001")
    age(folder)
    handle = open(folder / "data" / "local" / "own.save", "rb")  # noqa: SIM115
    try:
        result = library.remove_tmp_dirs([folder])
        if sys.platform == "win32":
            assert result.removed == [] and result.recent == [str(folder)] and folder.exists()
    finally:
        handle.close()
    result = library.remove_tmp_dirs([folder])
    assert result.removed == [str(folder)] and not folder.exists()
    assert not any(p.name.startswith(".tmp-deleting") for p in library.stale_tmp_dirs())
    game_id = library.load_catalog()["games"][0]["game_id"]
    tmp = library.new_tmp_dir(game_id, "20261024T000006Z")
    manifest = {
        "snapshot_id": "20261024T000006Z",
        "files": [{"source_id": "local", "path": "1-1-LT1.save", "size": 5, "sha256": "0" * 64}],
    }
    with pytest.raises(FatalError, match="lost local/1-1-LT1.save"):
        library.commit_snapshot(tmp, game_id, manifest)
    assert not library.snapshot_dir(game_id, "20261024T000006Z").exists()
    library.discard_tmp(tmp)


def test_the_overview_counts_every_snapshot_it_reads_and_the_last_manual_one(
    library: Repo,
) -> None:
    """D59 5: snapshots and bytes from the same reading -- a record folder the catalog does
    not name counts in both -- and 「上次备份」 is the newest manual or imported snapshot, not
    a safety snapshot taken before a restore."""
    import shutil

    stats = library_stats(library)
    game_id = library.load_catalog()["games"][0]["game_id"]
    sid = library.snapshot_ids(game_id)[0]
    stray = library.root / "games" / "0123abcd-0000-4000-8000-000000000009"
    shutil.copytree(library.game_dir(game_id), stray)
    grown = library_stats(library)
    assert grown.snapshots == stats.snapshots + 1 and grown.records == stats.records
    assert grown.logical_bytes > stats.logical_bytes
    shutil.rmtree(stray)
    later = library.snapshot_dir(game_id, "20991231T000000Z")
    shutil.copytree(library.snapshot_dir(game_id, sid), later)
    manifest = json.loads((later / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(snapshot_id="20991231T000000Z", reason="pre_restore")
    (later / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert library_stats(library).last_backup == stats.last_backup, "the safety one is not it"


def test_stopping_at_the_second_leftover(library: Repo) -> None:
    """D62 ④: the first one is deleted, the second kept, the store not collected, exit 1."""
    for record, name in ((0, ".tmp-20261024T000003Z-aaa001"), (1, ".tmp-20261024T000004Z-aaa002")):
        age(old_tmp(library, record, name))
    first, second = library.stale_tmp_dirs()  # in the order they are looked at
    asked: list[bool] = []

    def second_time() -> bool:
        asked.append(True)
        return len(asked) > 1

    result = library.remove_tmp_dirs(should_stop=second_time)
    assert result.removed == [str(first)] and second.exists() and not first.exists()
    assert result.cancelled and result.garbage is None and result.exit_code == 1


def test_stopping_between_leftovers(library: Repo) -> None:
    leftovers(library)
    result = library.remove_tmp_dirs(should_stop=lambda: True)
    assert result.cancelled and result.removed == [] and result.garbage is None
    assert result.to_dict()["cancelled"] is True and result.exit_code == 1


def test_the_command_line_previews_then_deletes(
    library: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    old, fresh = leftovers(library)
    assert main(["remove", "--repo", str(library.root), "--tmp"]) == 0
    out = capsys.readouterr().out
    assert "may be in use, kept" in out and "Nothing was deleted" in out and old.exists()
    assert main(["remove", "--repo", str(library.root), "--tmp", "--yes"]) == 1  # all kept
    out = capsys.readouterr().out
    assert "Deleted 0 temporary folder(s)" in out and old.exists() and fresh.exists()
    assert "temporary snapshot folders are kept as well" in out, "D59 4"
    age(fresh)
    os.utime(fresh.parent, None)
    assert main(["remove", "--repo", str(library.root), "--tmp", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Deleted 2 temporary folder(s)" in out and not old.exists() and not fresh.exists()
    assert main(["remove", "--repo", str(library.root), "--tmp", "--game", "x"]) == 2
    assert main(["remove", "--repo", str(library.root)]) == 2, "--game or --tmp"
    capsys.readouterr()


def test_the_overview_agrees_with_the_inventory(library: Repo, games_root: Path) -> None:
    """D56: counts by group, stored and logical bytes from the same reading as a removal's
    estimate, the newest snapshot; a second backup of the same saves stores almost nothing."""
    stats = library_stats(library)
    found = inventory(library)
    assert (stats.records, stats.snapshots, stats.unreadable) == (4, 4, 0)
    assert (stats.installed, stats.gone, stats.unlinked) == (2, 2, 2), "orphans are 本体不在"
    assert stats.logical_bytes == sum(s.bytes for s in found.snapshots.values())
    objects = sum(p.stat().st_size for _n, p in library.iter_objects())
    ids = [
        (r["game_id"], s)
        for r in library.load_catalog()["games"]
        for s in library.snapshot_ids(r["game_id"])
    ]
    manifests = sum((library.snapshot_dir(g, s) / "manifest.json").stat().st_size for g, s in ids)
    records = sum(p.stat().st_size for p in (library.root / "games").glob("*/game.json"))
    catalog = library.catalog_path.stat().st_size
    assert stats.stored_bytes == objects + manifests + records + catalog
    assert stats.last_backup == max(library.snapshot_created(g, s) or "" for g, s in ids)
    plan = prepare_backup([games_root], library.root, include_unlinked=[], saves_root_override=None)
    execute_backup(plan, library)
    again = library_stats(library)
    assert again.logical_bytes > stats.logical_bytes
    assert again.stored_bytes - stats.stored_bytes < again.logical_bytes - stats.logical_bytes
    assert LibraryStats(stored_bytes=40, logical_bytes=100).saved_percent == 60
    assert again.to_dict()["last_mirror"] is None


def test_an_unreadable_manifest_is_counted_without_bytes(
    library: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    stats = library_stats(library)
    game_id = library.load_catalog()["games"][0]["game_id"]
    sid = library.snapshot_ids(game_id)[0]
    size = inventory(library).snapshots[(game_id, sid)]
    path = library.snapshot_dir(game_id, sid) / "manifest.json"
    os.chmod(path, 0o644)
    path.write_text("{ damaged", encoding="utf-8")
    damaged = library_stats(library)
    assert (damaged.snapshots, damaged.unreadable) == (stats.snapshots, 1)
    assert damaged.logical_bytes == stats.logical_bytes - size.bytes
    assert main(["list", "--repo", str(library.root)]) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("stored ") and "(logical " in last


def test_the_library_page_texts() -> None:
    """D55, D56, D57 in the window's words (no window needed)."""
    from enginesave.gui import state
    from enginesave.mirror import MirrorResult
    from enginesave.repo import Garbage, TmpFolder, TmpRemoval

    stats = LibraryStats(4, 2, 2, 1, 9, 0, 40 * 2**20, 100 * 2**20, "2026-10-24T01:00:00Z")
    text = state.overview_text(stats)
    assert text.startswith(
        "4 条记录（本体存在 2、本体不在 2，其中孤立目录 1） · 9 个快照 · 占用 40"
    )
    alone = LibraryStats(3, 2, 1, 0, 9)
    assert state.overview_text(alone).startswith("3 条记录（本体存在 2、本体不在 1） · ")
    assert "去重前 100" in text and "省 60%" in text and text.endswith("上次镜像 无")
    stats.unreadable = 2
    assert state.overview_text(stats).endswith("（2 个快照的清单读不出，未计入大小）")
    old = TmpFolder(Path("a"), 2048, 2, 1_600_000_000 * 10**9)
    new = TmpFolder(Path("b"), 10, 1, time.time_ns())
    assert state.tmp_ticks([old, new]) == {Path("a")}
    assert state.tmp_row_values(new, False)[0] == "" and state.tmp_row_values(new, False)[5]
    assert state.tmp_link_text(0) == "" and state.tmp_link_text(3) == "清理临时文件夹（3）…"
    done = TmpRemoval(["a"], 2048, ["b"], [], Garbage(skipped="1 temporary folder(s)"))
    assert state.tmp_done_text(done).startswith(
        "已删除 1 个临时文件夹，释放 2.0 KB；对象库这次没有回收"
    )
    assert "；1 个刚有改动，可能正在使用，保留" in state.tmp_done_text(done)
    result = MirrorResult(Path("s"), Path("t"), objects_copied=3, snapshots_copied=2)
    assert state.mirror_done_text(result).startswith("镜像完成：复制 3 个对象、2 个快照")
    result.kept_records = ["Gone Game"]
    assert "镜像里保留了 1 条备份库已删除的记录" in state.mirror_done_text(result)
    result.cancelled = True
    assert state.mirror_done_text(result).startswith("镜像已取消")
    assert state.same_drive(Path("C:/a"), Path("c:/b")) and not state.same_drive(
        Path("C:/a"), Path("D:/a")
    )
