"""Mirroring a library (0.9.0, D57), only ever between libraries in temporary folders: to an
empty folder, to the last mirror, refused for another library or a forbidden place, on a
drive without hard links, interrupted and run again, after the library grew, and after it
dropped a record."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from enginesave import mirror
from enginesave.backup import execute_backup, prepare_backup
from enginesave.cli import main
from enginesave.errors import Stopped, UsageError
from enginesave.mirror import check_target, forbidden_places, last_mirror, mirror_repo
from enginesave.removal import execute_remove, library_stats, plan_remove
from enginesave.repo import Repo
from enginesave.verify import verify_objects, verify_repo
from fixtures.builder import build_f1, build_f8, build_game, simple_spec, stored
from fixtures.fsprobe import needs_hard_links


def back_up(games_root: Path, saves_root: Path, library: Path) -> Repo:
    plan = prepare_backup(
        [games_root], library, saves_root_override=saves_root, include_unlinked=[]
    )
    repo = Repo.open_or_create(plan.dest)
    execute_backup(plan, repo)
    return repo


@pytest.fixture
def library(games_root: Path, saves_root: Path, tmp_path: Path) -> Repo:
    """F1, a second game and two orphaned folders, backed up once."""
    build_f1(games_root, saves_root)
    build_game(games_root, simple_spec("Second", "Second", "Second-1600000002"), saves_root)
    build_f8(saves_root)
    return back_up(games_root, saves_root, tmp_path / "library")


def state(folder: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in folder.rglob("*")
        if p.is_file()
    }


def files(repo: Repo) -> list[tuple[str, str, dict]]:
    """Every manifest entry of every snapshot: (record, snapshot, entry)."""
    return [
        (r["game_id"], sid, item)
        for r in repo.load_catalog()["games"]
        for sid in repo.snapshot_ids(r["game_id"])
        for item in repo.load_manifest(r["game_id"], sid)["files"]
    ]


def clean(repo: Repo) -> bool:
    checks = verify_repo(repo, repo.load_catalog())
    return bool(checks) and all(c.clean for c in checks) and verify_objects(repo).clean


@needs_hard_links
def test_a_mirror_into_an_empty_folder_is_a_complete_library(library: Repo, tmp_path: Path) -> None:
    target = tmp_path / "mirror"
    result = mirror_repo(library, target)
    assert result.exit_code == 0 and result.new and result.verified
    copy = Repo.open(target)
    ours, theirs = library.load_catalog(), copy.load_catalog()
    assert theirs["created_at"] == ours["created_at"], "recognised as this library's mirror"
    assert [r["game_id"] for r in theirs["games"]] == [r["game_id"] for r in ours["games"]]
    for record in ours["games"]:
        game_id = record["game_id"]
        assert copy.snapshot_ids(game_id) == library.snapshot_ids(game_id)
        for sid in library.snapshot_ids(game_id):
            mine = (library.snapshot_dir(game_id, sid) / "manifest.json").read_bytes()
            assert (copy.snapshot_dir(game_id, sid) / "manifest.json").read_bytes() == mine
    assert result.objects_copied == sum(1 for _ in library.iter_objects())
    assert result.to_dict()["storage"] == "hardlink"
    assert copy.stale_tmp_dirs() == [] and clean(copy)
    for game_id, sid, item in files(copy):  # linked to the mirror's own objects
        placed = stored(copy, game_id, sid, item["source_id"], item["path"])
        assert os.path.samefile(placed, copy.object_path(item["sha256"]))
        assert not os.path.samefile(placed, library.object_path(item["sha256"]))


def test_a_second_run_copies_nothing_and_a_new_backup_only_what_is_new(
    library: Repo, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    again = mirror_repo(library, target)
    assert (again.objects_copied, again.snapshots_copied) == (0, 0)
    assert again.objects_present == sum(1 for _ in library.iter_objects())
    assert not again.new and again.exit_code == 0
    save = next((games_root / "Second" / "game" / "saves").glob("*.save"))
    save.write_bytes(save.read_bytes() + b" played on")
    before = {name for name, _ in library.iter_objects()}
    back_up(games_root, saves_root, library.root)
    new = {name for name, _ in library.iter_objects()} - before
    grown = mirror_repo(library, target)
    assert grown.objects_copied == len(new) >= 1
    records = len(library.load_catalog()["games"])
    assert grown.snapshots_copied == records, "every record has a new snapshot (orphans too)"
    assert grown.exit_code == 0 and clean(Repo.open(target))


def test_another_library_is_refused_and_left_alone(
    library: Repo, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D57 1: a library with records this one does not have is not a mirror of it."""
    other_games = tmp_path / "other-games"
    other_games.mkdir()
    build_game(other_games, simple_spec("Elsewhere", "Elsewhere", "Elsewhere-1"), saves_root)
    other = back_up(other_games, saves_root, tmp_path / "other")
    before = state(other.root)
    with pytest.raises(UsageError, match="another library"):
        mirror_repo(library, other.root)
    assert state(other.root) == before
    catalog = other.load_catalog()  # made in the same second: the same created_at
    catalog["created_at"] = library.load_catalog()["created_at"]
    other.save_catalog(catalog)
    before = state(other.root)
    with pytest.raises(UsageError, match="another library"):
        mirror_repo(library, other.root)
    assert state(other.root) == before, "the time alone does not make it a mirror"
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(UsageError, match="not empty and is not an EngineSave library"):
        mirror_repo(library, stray)
    assert sorted(p.name for p in stray.iterdir()) == ["notes.txt"]


def test_an_empty_library_of_its_own_is_accepted(library: Repo, tmp_path: Path) -> None:
    """A new library (no records, another ``created_at``) holds nothing to lose."""
    empty = Repo.open_or_create(tmp_path / "empty")
    result = mirror_repo(library, empty.root)
    assert result.exit_code == 0 and not result.new and clean(empty)


def test_forbidden_places(library: Repo, saves_root: Path, games_root: Path) -> None:
    """Not the library, inside it or around it; not in the saves root, a recorded game
    directory or save folder, or a scan root (the window adds those)."""
    forbidden = forbidden_places(library, saves_root=saves_root, also=[games_root])
    for target in (
        library.root,
        library.root / "mirror",
        library.root.parent,
        saves_root / "mirror",
        games_root / "Readable Game" / "mirror",
        games_root / "mirror",
    ):
        with pytest.raises(UsageError):
            check_target(library, target, forbidden)
        assert not (target.name == "mirror" and target.exists())


@pytest.mark.parametrize(
    "token", ["APPDATA", "LOCALAPPDATA", "DOCUMENTS", "SAVED_GAMES", "VIRTUALSTORE"]
)
def test_a_profile_root_itself_is_no_mirror(library: Repo, token: str) -> None:
    """0.11.0 (D69 3): not the folder a root token stands for (pointed into the sandbox) or
    around it; a folder inside it that no record names is fine."""
    root = Path(os.environ[f"ENGINESAVE_ROOT_{token}"])
    root.mkdir(parents=True, exist_ok=True)
    forbidden = forbidden_places(library)
    for target in (root, root.parent):
        with pytest.raises(UsageError, match="user profile|game, save or scanned"):
            check_target(library, target, forbidden)
    assert check_target(library, root / "EngineSave mirror", forbidden) == "new"


def test_a_junction_does_not_lead_the_mirror_into_a_forbidden_place(
    library: Repo, saves_root: Path, tmp_path: Path
) -> None:
    """A target that is (or lies under) a junction into the library or the saves root is
    refused like the place itself: links are followed before comparing."""
    from fixtures.builder import make_junction

    empty_inside = saves_root / "empty"
    empty_inside.mkdir()
    for link, place in (
        (tmp_path / "to-library", library.root),
        (tmp_path / "to-saves", empty_inside),
    ):
        if not make_junction(link, place):
            pytest.skip("cannot create a junction here")
        forbidden = forbidden_places(library, saves_root=saves_root)
        with pytest.raises(UsageError):
            check_target(library, link, forbidden)
        with pytest.raises(UsageError):
            check_target(library, link / "below", forbidden)
    assert list(empty_inside.iterdir()) == []


def test_a_damaged_record_does_not_stop_the_others(library: Repo, tmp_path: Path) -> None:
    catalog = library.load_catalog()
    broken = catalog["games"][0]["game_id"]
    (library.game_dir(broken) / "game.json").unlink()
    result = mirror_repo(library, tmp_path / "mirror")
    assert result.exit_code == 1 and any("game.json" in f for f in result.failed)
    ids = [r["game_id"] for r in Repo.open(tmp_path / "mirror").load_catalog()["games"]]
    assert broken not in ids and len(ids) == len(catalog["games"]) - 1
    assert clean(Repo.open(tmp_path / "mirror"))


def test_a_drive_without_hard_links_gets_copies(
    library: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_links(*_args: object) -> None:
        raise OSError(1, "hard links are not supported here")

    monkeypatch.setattr(mirror.os, "link", no_links)
    result = mirror_repo(library, tmp_path / "fat32")
    assert result.to_dict()["storage"] == "copy" and result.exit_code == 0
    copy = Repo.open(tmp_path / "fat32")
    assert clean(copy)
    for game_id, sid, _item in files(copy):  # the manifests say how the mirror holds them
        assert copy.load_manifest(game_id, sid)["storage"] == "copy"
    ours, theirs = library_stats(library), library_stats(copy)
    assert theirs.stored_bytes >= ours.stored_bytes + ours.logical_bytes - 1024, "copies count"


def test_an_interrupted_mirror_is_a_smaller_mirror_and_the_next_run_completes_it(
    library: Repo, tmp_path: Path
) -> None:
    """D58 with D57 4: stopped between files the mirror keeps what is whole (no
    half snapshot, no temporary folder) and is still recognised; running again finishes."""
    target = tmp_path / "mirror"
    total = sum(1 for _ in library.iter_objects()) + sum(
        len(library.load_manifest(r["game_id"], s)["files"])
        for r in library.load_catalog()["games"]
        for s in library.snapshot_ids(r["game_id"])
    )
    for stop_at in (1, total - 2):
        steps = iter(range(10_000))
        stopped = mirror_repo(library, target, should_stop=lambda s=steps, n=stop_at: next(s) >= n)
        assert stopped.cancelled and stopped.checks is None
        copy = Repo.open(target)
        assert copy.stale_tmp_dirs() == [], "a snapshot being assembled was discarded"
        assert check_target(library, target) == "mirror"
        listed = copy.load_catalog()["games"]
        arrived = [
            r["game_id"] for r in library.load_catalog()["games"] if copy.has_game(r["game_id"])
        ]
        assert [r["game_id"] for r in listed] == arrived, "the catalog was written when stopped"
        assert (len(listed) == 0) == (stop_at == 1)
        for record in listed:
            assert record["snapshot_count"] == len(copy.snapshot_ids(record["game_id"]))
    done = mirror_repo(library, target)
    assert not done.cancelled and done.exit_code == 0 and clean(Repo.open(target))


def test_a_failure_is_reported_and_the_next_run_repairs_it(
    library: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real = mirror._place

    def flaky(obj: Path, target: Path, digest: str, size: int) -> str:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        return real(obj, target, digest, size)

    monkeypatch.setattr(mirror, "_place", flaky)
    target = tmp_path / "mirror"
    broken = mirror_repo(library, target)
    assert broken.exit_code == 1 and len(broken.failed) == 1 and "No space" in broken.failed[0]
    assert Repo.open(target).stale_tmp_dirs() == []
    monkeypatch.setattr(mirror, "_place", real)
    fixed = mirror_repo(library, target)
    assert fixed.snapshots_copied == 1 and fixed.exit_code == 0 and clean(Repo.open(target))


def test_a_record_the_library_dropped_stays_in_the_mirror(library: Repo, tmp_path: Path) -> None:
    """D57 2: only adds. The library deletes a record (D49); the next mirror still runs (it
    is recognised by the catalog's ``created_at``) and keeps the record."""
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    catalog = library.load_catalog()
    second = next(r for r in catalog["games"] if r["display_name"] == "Second")
    execute_remove(plan_remove(library, catalog, [(second["game_id"], None)]))
    again = mirror_repo(library, target)
    assert again.exit_code == 0 and again.kept_records == ["Second"]
    ids = [r["game_id"] for r in Repo.open(target).load_catalog()["games"]]
    assert second["game_id"] in ids and clean(Repo.open(target))


def test_a_damaged_object_is_not_copied_into_the_mirror(library: Repo, tmp_path: Path) -> None:
    name, path = next(iter(library.iter_objects()))
    os.chmod(path, 0o644)
    data = path.read_bytes()
    with open(path, "r+b") as f:  # the same size, other bytes
        f.write(bytes([data[0] ^ 0xFF]) + data[1:])
    result = mirror_repo(library, tmp_path / "mirror")
    assert result.exit_code == 1
    assert any(f.startswith(f"object {name}") for f in result.failed), result.failed
    assert not Repo.open(tmp_path / "mirror").object_path(name).exists()


def test_the_last_mirror_is_remembered_beside_the_settings(
    library: Repo, tmp_path: Path, isolated_env: Path
) -> None:
    config = isolated_env / "AppData" / "Roaming" / "EngineSave"
    assert last_mirror(config, library.root) is None
    mirror.record_mirror(config, library.root, tmp_path / "mirror", "2026-10-25T10:00:00Z")
    assert last_mirror(config, library.root) == {
        "to": str(tmp_path / "mirror"),
        "at": "2026-10-25T10:00:00Z",
    }
    (config / mirror.MIRRORS_FILE).write_text("{ damaged", encoding="utf-8")
    assert last_mirror(config, library.root) is None
    assert json.loads(json.dumps(mirror_repo(library, tmp_path / "m").to_dict()))["verified"]
    for entry in ({"to": 3, "at": "2026-10-25T10:00:00Z"}, {"to": "x", "at": 7}, {"to": "x"}):
        (config / mirror.MIRRORS_FILE).write_text(
            json.dumps({"format": 1, "mirrors": {mirror.path_key(library.root): entry}}),
            encoding="utf-8",
        )
        assert last_mirror(config, library.root) is None, entry


# ------------------------------------------------------------- after the M34 review (S-167)


def test_another_name_for_the_library_or_a_save_folder_is_refused(
    library: Repo, saves_root: Path
) -> None:
    """D57 1: a ``\\\\?\\`` path names the same folder; it is refused like the folder."""
    forbidden = forbidden_places(library, saves_root=saves_root)
    for place in (library.root, saves_root):
        for target in (place, place / "mirror"):
            with pytest.raises(UsageError):
                check_target(library, Path("\\\\?\\" + str(target)), forbidden)
    assert not (library.root / "mirror").exists() and not (saves_root / "mirror").exists()


def test_an_administrative_share_of_the_library_is_refused(library: Repo) -> None:
    """``\\\\localhost\\C$\\…`` is the library under another name (only checked here)."""
    drive, rest = os.path.splitdrive(str(library.root))
    share = Path(f"\\\\localhost\\{drive[0]}$" + rest)
    try:
        reachable = share.is_dir()
    except OSError:
        reachable = False
    if not reachable:
        pytest.skip("the administrative share is not reachable here")
    for target in (share, share / "mirror", share.parent):
        with pytest.raises(UsageError):
            check_target(library, target)


def test_an_extra_save_folder_is_a_forbidden_place(library: Repo, tmp_path: Path) -> None:
    """``config.extra_savedirs`` folders are recorded with an absolute root: no mirror there
    (it would be backed up into the library and grow every round)."""
    extra = tmp_path / "extra-saves"
    extra.mkdir()
    game_id = library.load_catalog()["games"][0]["game_id"]
    doc = library.load_game(game_id)
    template = dict(doc["source_templates"][0])
    template.update(source_id="extra-1", root_token="ABSOLUTE", relative_root=str(extra))
    doc["source_templates"].append(template)
    library.save_game(doc)
    forbidden = forbidden_places(library)
    for target in (extra, extra / "mirror"):
        with pytest.raises(UsageError):
            check_target(library, target, forbidden)


@needs_hard_links
def test_a_file_already_there_is_never_copied_over(tmp_path: Path) -> None:
    """A copy over a hard link would write into the object it links to: both placements
    refuse a target that exists (mirror and backup/import)."""
    import hashlib

    from enginesave import store

    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"the first object")
    second.write_bytes(b"another object!!")
    digest = hashlib.sha256(second.read_bytes()).hexdigest()
    target = tmp_path / "placed"
    os.link(first, target)
    with pytest.raises(FileExistsError):
        mirror._place(second, target, digest, 16)
    with pytest.raises(FileExistsError):
        store._place(second, target)
    assert first.read_bytes() == b"the first object"


def test_two_entries_for_one_name_never_write_into_an_object(library: Repo, tmp_path: Path) -> None:
    game_id = library.load_catalog()["games"][0]["game_id"]
    sid = library.snapshot_ids(game_id)[0]
    path = library.snapshot_dir(game_id, sid) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    first = manifest["files"][0]
    other = next(name for name, _ in library.iter_objects() if name != first["sha256"])
    size = library.object_path(other).stat().st_size
    manifest["files"].append(dict(first, path=first["path"].upper(), sha256=other, size=size))
    os.chmod(path, 0o644)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    result = mirror_repo(library, tmp_path / "mirror")
    assert result.exit_code == 1 and any(sid in f for f in result.failed), result.failed
    copy = Repo.open(tmp_path / "mirror")
    assert verify_objects(copy).clean and copy.stale_tmp_dirs() == []


def test_the_mirror_is_recognised_after_a_killed_run_and_a_deletion(
    library: Repo, tmp_path: Path
) -> None:
    """A run killed before its catalog listed a record leaves the record's folder; once the
    library deletes that record, the mirror is still this library's (S-160)."""
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    catalog = library.load_catalog()
    second = next(r for r in catalog["games"] if r["display_name"] == "Second")
    copy = Repo.open(target)
    theirs = copy.load_catalog()
    theirs["games"] = [r for r in theirs["games"] if r["game_id"] != second["game_id"]]
    copy.save_catalog(theirs)
    execute_remove(plan_remove(library, catalog, [(second["game_id"], None)]))
    assert check_target(library, target) == "mirror"
    assert mirror_repo(library, target).exit_code == 0


def test_record_folders_the_catalog_does_not_list_still_count(
    library: Repo, tmp_path: Path
) -> None:
    """Another library's record folder makes it another library even when its catalog lists
    nothing; a mirror with a folder that is no record's is not written into either."""
    other = Repo.open_or_create(tmp_path / "other")
    (other.root / "games" / "0123abcd-0000-4000-8000-000000000001").mkdir(parents=True)
    before = state(other.root)
    with pytest.raises(UsageError, match="another library"):
        mirror_repo(library, other.root)
    assert state(other.root) == before
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    second = next(r for r in library.load_catalog()["games"] if r["display_name"] == "Second")
    execute_remove(plan_remove(library, library.load_catalog(), [(second["game_id"], None)]))
    (target / "games" / "notes").mkdir()
    with pytest.raises(UsageError, match="another library"):
        check_target(library, target)


def test_a_damaged_record_file_does_not_replace_the_mirrors_copy(
    library: Repo, tmp_path: Path
) -> None:
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    game_id = library.load_catalog()["games"][0]["game_id"]
    good = (Repo.open(target).game_dir(game_id) / "game.json").read_bytes()
    (library.game_dir(game_id) / "game.json").write_text("{ damaged", encoding="utf-8")
    result = mirror_repo(library, target)
    assert result.exit_code == 1 and any("game.json" in f for f in result.failed)
    assert (Repo.open(target).game_dir(game_id) / "game.json").read_bytes() == good
    assert clean(Repo.open(target))


def test_the_library_is_only_read_and_its_record_files_are_refreshed(
    library: Repo, tmp_path: Path
) -> None:
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    game_id = library.load_catalog()["games"][0]["game_id"]
    doc = library.load_game(game_id)
    doc["updated_at"] = "2026-10-25T00:00:00Z"
    library.save_game(doc)
    before = state(library.root)
    assert mirror_repo(library, target).exit_code == 0
    assert state(library.root) == before, "nothing in the library changes"
    mine = (library.game_dir(game_id) / "game.json").read_bytes()
    assert (Repo.open(target).game_dir(game_id) / "game.json").read_bytes() == mine


def test_a_full_drive_is_reported_not_raised(
    library: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(*_args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mirror, "_write_catalog", full)
    result = mirror_repo(library, tmp_path / "mirror")
    assert result.exit_code == 1 and any(f.startswith("catalog.json") for f in result.failed)
    monkeypatch.undo()

    def no_folder(_self: Repo, game_id: str, snapshot_id: str) -> Path:
        from enginesave.errors import FatalError

        raise FatalError(f"cannot create snapshot folder for {snapshot_id}")

    monkeypatch.setattr(Repo, "new_tmp_dir", no_folder)
    result = mirror_repo(library, tmp_path / "other")
    assert result.exit_code == 1 and result.snapshots_copied == 0
    assert len(result.failed) == sum(
        len(library.snapshot_ids(r["game_id"])) for r in library.load_catalog()["games"]
    )


def test_an_unreadable_manifest_counts_only_when_the_mirror_lacks_the_snapshot(
    library: Repo, tmp_path: Path
) -> None:
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    game_id = library.load_catalog()["games"][0]["game_id"]
    path = library.snapshot_dir(game_id, library.snapshot_ids(game_id)[0]) / "manifest.json"
    os.chmod(path, 0o644)
    path.write_text("{ damaged", encoding="utf-8")
    again = mirror_repo(library, target)
    assert again.unreadable == [] and again.exit_code == 0
    assert again.snapshots_present == len(files_by_snapshot(library))
    fresh = mirror_repo(library, tmp_path / "fresh")
    assert len(fresh.unreadable) == 1 and fresh.exit_code == 1


def files_by_snapshot(repo: Repo) -> list[tuple[str, str]]:
    return [
        (r["game_id"], sid)
        for r in repo.load_catalog()["games"]
        for sid in repo.snapshot_ids(r["game_id"])
    ]


@needs_hard_links
def test_the_mirror_is_verified_not_the_library_and_kept_objects_are_not_copied_again(
    library: Repo, tmp_path: Path
) -> None:
    """An object of another size is copied again; one of the same name and size is kept by
    the copying, and the verification afterwards (of the mirror) finds it damaged, copies it
    again and places the snapshot files linked to the damaged bytes again (D59 3)."""
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    copy = Repo.open(target)
    (name_a, path_a), (name_b, path_b) = list(copy.iter_objects())[:2]
    os.chmod(path_a, 0o644)
    path_a.unlink()  # its snapshot files keep their own link
    path_a.write_bytes(b"short")
    again = mirror_repo(library, target)
    assert again.objects_copied == 1 and again.exit_code == 0
    assert path_a.read_bytes() == library.object_path(name_a).read_bytes()
    os.chmod(path_b, 0o644)
    data = path_b.read_bytes()
    path_b.write_bytes(bytes([data[0] ^ 0xFF]) + data[1:])
    linked = [f for f in files(copy) if f[2]["sha256"] == name_b]
    damaged = mirror_repo(library, target)
    assert damaged.objects_copied == 0 and damaged.objects_recopied == 1
    assert damaged.files_replaced == len(linked) >= 1, "the files linked to the bad bytes"
    assert damaged.verified is True and damaged.exit_code == 0 and damaged.failed == []
    assert path_b.read_bytes() == data, "the object is whole again"
    assert clean(Repo.open(target)) and clean(library)
    assert copy.stale_tmp_dirs() == [] and not list(target.rglob("*enginesave-repair*"))
    again = mirror_repo(library, target)
    assert (again.objects_recopied, again.files_replaced, again.verified) == (0, 0, True)


def test_a_second_ctrl_c_leaves_no_half_snapshot(library: Repo, tmp_path: Path) -> None:
    objects = sum(1 for _ in library.iter_objects())
    calls = iter(range(10_000))

    def interrupt() -> bool:
        if next(calls) == objects + 1:  # in the middle of the first snapshot
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt):
        mirror_repo(library, tmp_path / "mirror", should_stop=interrupt)
    copy = Repo.open(tmp_path / "mirror")
    assert copy.stale_tmp_dirs() == []
    for record in copy.load_catalog()["games"]:
        assert record["snapshot_count"] == len(copy.snapshot_ids(record["game_id"]))


def test_a_mirror_stopped_while_verifying_is_complete_but_unverified(
    library: Repo, tmp_path: Path
) -> None:
    """D59 2: every copying step answers no, the first question of the verification yes: the
    mirror is whole; the result is not cancelled but ``verify_stopped``, not verified, exit 0
    (so it is 「上次镜像」)."""
    snapshots = {(g, s) for g, s, _item in files(library)}  # M44 review: the re-link pass
    copying = sum(1 for _ in library.iter_objects()) + len(files(library)) + len(snapshots)
    asked = iter(range(10_000))
    result = mirror_repo(library, tmp_path / "mirror", should_stop=lambda: next(asked) >= copying)
    assert result.verify_stopped and not result.cancelled and result.exit_code == 0
    assert result.checks is None and result.objects is None and result.verified is None
    assert result.snapshots_copied == len(files_by_snapshot(library))
    data = result.to_dict()
    assert data["verify_stopped"] is True and "cancelled" not in data
    assert clean(Repo.open(tmp_path / "mirror"))


def test_verifying_the_objects_stops_between_objects(library: Repo) -> None:
    with pytest.raises(Stopped) as stopped:
        verify_objects(library, should_stop=lambda: True)
    assert stopped.value.partial is not None and stopped.value.partial.ok == 0


def test_the_command_line(
    library: Repo, tmp_path: Path, isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``mirror``: ``--no-verify`` (nothing verified), a refused folder (2), a mirror with a
    failure (1); only one that worked becomes 「上次镜像」."""
    config = isolated_env / "AppData" / "Roaming" / "EngineSave"
    target = tmp_path / "mirror"
    args = ["mirror", "--repo", str(library.root), "--json"]
    assert main([*args, "--to", str(target), "--no-verify"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verified"] is None and data["checks"] is None and data["objects"] is None
    remembered = last_mirror(config, library.root)
    assert remembered is not None and remembered["to"] == str(target)
    assert main([*args, "--to", str(library.root / "inside")]) == 2
    capsys.readouterr()
    name, path = next(iter(library.iter_objects()))
    os.chmod(path, 0o644)
    data = path.read_bytes()
    with open(path, "r+b") as f:
        f.write(bytes([data[0] ^ 0xFF]) + data[1:])
    assert main([*args, "--to", str(tmp_path / "second")]) == 1
    assert any(name in f for f in json.loads(capsys.readouterr().out)["failed"])
    assert last_mirror(config, library.root) == remembered, "a failed mirror is not the last"


# ------------------------------------------------------------------ 0.10.0 (D59, S-169..)


@pytest.mark.parametrize(
    "links",
    [pytest.param(True, id="hard-links", marks=needs_hard_links), pytest.param(False, id="copies")],
)
def test_saves_restored_from_the_mirror_are_the_originals(
    games_root: Path, saves_root: Path, tmp_path: Path, links: bool
) -> None:
    """D62 ④: the mirror is a library of its own -- the saves deleted, then restored from it,
    are byte for byte what was backed up (a mirror of hard links and one of copies)."""
    import shutil

    from enginesave.restore import execute_restore, plan_restore

    built = build_f1(games_root, saves_root)
    library = back_up(games_root, saves_root, tmp_path / "library")
    target = tmp_path / "mirror"
    if links:
        result = mirror_repo(library, target)
    else:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mirror.os, "link", lambda *_a: (_ for _ in ()).throw(OSError(1, "no")))
            result = mirror_repo(library, target)
    assert result.exit_code == 0 and result.to_dict()["storage"] == (
        "hardlink" if links else "copy"
    )
    assert built.global_dir is not None
    shutil.rmtree(built.root / "game" / "saves")
    shutil.rmtree(built.global_dir)
    copy = Repo.open(target)
    plan = plan_restore(copy, copy.load_catalog(), "readable game", saves_root_override=saves_root)
    restored = execute_restore(plan)
    assert restored.failures == [] and restored.safety_snapshot is None
    for name, data in built.local_files.items():
        assert (built.root / "game" / "saves" / name).read_bytes() == data
    for rel, data in built.global_files.items():
        assert (built.global_dir / rel).read_bytes() == data
    assert clean(copy) and clean(library)


def test_a_file_under_games_does_not_make_a_mirror_another_library(
    library: Repo, tmp_path: Path
) -> None:
    """D59 1: only folders are records; a ``desktop.ini`` (or a sync tool's file) is not."""
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    (target / "games" / "desktop.ini").write_text("[.ShellClassInfo]", encoding="utf-8")
    (target / "games" / ".DS_Store").write_bytes(b"\0")
    assert check_target(library, target) == "mirror"
    second = next(r for r in library.load_catalog()["games"] if r["display_name"] == "Second")
    execute_remove(plan_remove(library, library.load_catalog(), [(second["game_id"], None)]))
    assert check_target(library, target) == "mirror", "also by lineage, files are ignored"
    assert mirror_repo(library, target).exit_code == 0


def test_a_junction_inside_the_mirror_is_refused_not_followed(
    library: Repo, tmp_path: Path
) -> None:
    """A junction where a library has folders (a record folder pointing back into the
    library, say) would lead the copying and a repair out of the mirror: refused, named,
    nothing written; the library is untouched."""
    from fixtures.builder import make_junction

    target = tmp_path / "mirror"
    mirror_repo(library, target)
    game_id = library.load_catalog()["games"][0]["game_id"]
    record = Repo.open(target).game_dir(game_id)
    import shutil

    shutil.rmtree(record)
    if not make_junction(record, library.game_dir(game_id)):
        pytest.skip("cannot create a junction here")
    before = state(library.root)
    with pytest.raises(UsageError, match="links or junctions") as refused:
        mirror_repo(library, target)
    assert record.name in str(refused.value) and state(library.root) == before  # D67 1
    os.rmdir(record)
    other = tmp_path / "other"
    (other / "games").mkdir(parents=True)
    shutil.copy2(library.root / "catalog.json", other / "catalog.json")
    if make_junction(other / "objects", library.root / "objects"):
        with pytest.raises(UsageError, match="links or junctions"):
            check_target(library, other)


def test_a_repair_leaves_nothing_behind_and_says_each_failure_once(
    library: Repo, tmp_path: Path
) -> None:
    """The repair writes in the mirror's ``objects/.tmp`` (where leftovers are found, D55),
    never beside a snapshot's files; an object it cannot repair is reported once, with
    ``--repair`` too, and a file by its source as well as its path."""
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    copy = Repo.open(target)
    name, path = next(iter(copy.iter_objects()))
    os.chmod(path, 0o644)
    path.write_bytes(b"x" * path.stat().st_size)
    ours = library.object_path(name)  # damaged in the library too: nothing to repair it from
    os.chmod(ours, 0o644)
    ours.write_bytes(b"y" * ours.stat().st_size)
    result = mirror_repo(library, target, repair=True)
    assert sum(1 for f in result.failed if f.startswith(f"object {name}")) == 1, result.failed
    assert all(
        " local/" in f or " global" in f or f.startswith("object ") for f in result.failed
    ), "a file is named with its source"
    leftovers = list(copy.object_tmp_dir().iterdir()) if copy.object_tmp_dir().exists() else []
    assert leftovers == [] and not list(target.rglob("*repair*"))


def test_a_refusal_names_what_is_in_the_way(library: Repo, tmp_path: Path) -> None:
    other = Repo.open_or_create(tmp_path / "other")
    for name in ("Notes", "Pictures", "Saves", "Zeta"):
        (other.root / "games" / name).mkdir(parents=True)
    with pytest.raises(UsageError) as refused:
        check_target(library, other.root)
    message = str(refused.value)
    assert "4 record(s) or folder(s)" in message and "Notes, Pictures, Saves, ..." in message
    assert "Zeta" not in message, "at most three names"


@needs_hard_links  # M44 review: the snapshot files share the damaged object's bytes
def test_repair_checks_the_objects_the_mirror_already_has(library: Repo, tmp_path: Path) -> None:
    """``--repair`` (D59 3): a same-size object with other bytes is found while copying, not
    trusted by its size; without ``--repair`` and without the check afterwards it stays."""
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    copy = Repo.open(target)
    name, path = next(iter(copy.iter_objects()))
    os.chmod(path, 0o644)
    good = path.read_bytes()
    path.write_bytes(bytes([good[0] ^ 0xFF]) + good[1:])
    trusting = mirror_repo(library, target, verify=False)
    assert trusting.objects_recopied == 0 and path.read_bytes() != good
    repaired = mirror_repo(library, target, verify=False, repair=True)
    assert repaired.objects_recopied == 1 and repaired.objects_present == (
        sum(1 for _ in library.iter_objects()) - 1
    )
    assert path.read_bytes() == good and repaired.exit_code == 0
    # D66 2: the snapshot files that kept the bad bytes are linked again without the check
    assert repaired.files_replaced >= 1 and clean(copy)
    final = mirror_repo(library, target)
    assert final.files_replaced == 0 and final.verified is True


def damage_shared_object(copy: Repo, first: bool = False) -> Path:
    """Flip a byte, in place, of the mirror's object that the most snapshot files link to
    (``first``: of the first object, the first one a mirror copies)."""
    paths = [p for _n, p in copy.iter_objects()]
    path = paths[0] if first else max(paths, key=lambda p: p.stat().st_nlink)
    os.chmod(path, 0o644)
    good = path.read_bytes()
    with open(path, "r+b") as f:
        f.write(bytes([good[0] ^ 0xFF]))
    return path


@needs_hard_links
def test_repair_relinks_the_snapshots_only_the_mirror_keeps(
    library: Repo, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """M44 review: ``--repair --no-verify`` re-links a snapshot the library has removed since
    (the mirror keeps it, D57 2), not only the library's own."""
    back_up(games_root, saves_root, library.root)  # a second snapshot of each game
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    catalog = library.load_catalog()
    record = next(r for r in catalog["games"] if r["display_name"] == "Readable Game")
    first = library.snapshot_ids(record["game_id"])[0]
    execute_remove(plan_remove(library, catalog, [(record["game_id"], [first])]))
    copy = Repo.open(target)
    assert first in copy.snapshot_ids(record["game_id"])
    damage_shared_object(copy)
    assert not clean(copy)
    repaired = mirror_repo(library, target, verify=False, repair=True)
    assert repaired.objects_recopied == 1 and repaired.exit_code == 0
    assert clean(copy), "the kept snapshot too"


@needs_hard_links
def test_a_later_repair_heals_what_an_interrupted_one_left(library: Repo, tmp_path: Path) -> None:
    """M44 review: a repair stopped after copying an object again leaves snapshot files on
    the damaged bytes; the next ``--repair --no-verify`` finds them by their links."""
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    copy = Repo.open(target)
    damage_shared_object(copy, first=True)
    asked = iter(range(10**6))
    stopped = mirror_repo(  # stopped right after the first object
        library, target, verify=False, repair=True, should_stop=lambda: next(asked) > 0
    )
    assert stopped.cancelled and stopped.objects_recopied == 1 and stopped.files_replaced == 0
    assert not clean(copy)
    again = mirror_repo(library, target, verify=False, repair=True)
    assert again.objects_recopied == 0 and again.files_replaced >= 1 and again.exit_code == 0
    assert clean(copy)


def test_a_damaged_object_the_library_lacks_is_reported(
    library: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copying again needs the library's own object: without it the damage is reported, and
    nothing in the mirror is replaced by something unchecked."""
    target = tmp_path / "mirror"
    mirror_repo(library, target)
    copy = Repo.open(target)
    name, path = next(iter(copy.iter_objects()))
    os.chmod(path, 0o644)
    path.write_bytes(b"x" * path.stat().st_size)
    real = Repo.object_path

    def missing_in_library(self: Repo, digest: str) -> Path:
        found = real(self, digest)
        return found.with_name("gone") if self.root == library.root and digest == name else found

    monkeypatch.setattr(Repo, "object_path", missing_in_library)
    result = mirror_repo(library, target)
    assert result.exit_code == 1 and result.objects_recopied == 0
    assert any(f.startswith(f"object {name}: damaged in the mirror") for f in result.failed)
    assert path.read_bytes() == b"x" * path.stat().st_size


def test_the_command_line_when_the_verification_is_stopped(
    library: Repo,
    tmp_path: Path,
    isolated_env: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D59 2: exit 0, ``mirror complete; verification stopped``, recorded as the last mirror;
    ``--repair`` is accepted."""
    from enginesave.cli import mirroring

    snapshots = {(g, s) for g, s, _item in files(library)}  # M44 review: the re-link pass
    copying = sum(1 for _ in library.iter_objects()) + len(files(library)) + len(snapshots)
    asked = iter(range(10_000))
    monkeypatch.setattr(mirroring, "should_stop", lambda: next(asked) >= copying)
    config = isolated_env / "AppData" / "Roaming" / "EngineSave"
    target = tmp_path / "mirror"
    code = main(["mirror", "--repo", str(library.root), "--to", str(target), "--repair"])
    out = capsys.readouterr().out
    assert code == 0 and "mirror complete; verification stopped" in out
    remembered = last_mirror(config, library.root)
    assert remembered is not None and remembered["to"] == str(target)
    from test_schemas import conforms

    monkeypatch.setattr(mirroring, "should_stop", lambda: False)
    assert main(["mirror", "--repo", str(library.root), "--to", str(target), "--json"]) == 0
    conforms("mirror", json.loads(capsys.readouterr().out))
