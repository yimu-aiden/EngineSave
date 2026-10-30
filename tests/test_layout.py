"""0.11.0 (D67, M45): a readable library -- record folders named after the games, snapshot
folders named after the local time, snapshots ordered by their manifests' ``created_at`` --
and the one-time migration of a library of format 1. ``tests/fixtures/library_v1.zip`` is a
small synthetic library made with the code of SaveTrail 0.10.0 and kept as it is; every test
works on a copy of it in its own temporary folder."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from enginesave import backup
from enginesave.backup import execute_backup, prepare_backup
from enginesave.cli import main
from enginesave.errors import FatalError, UsageError
from enginesave.migrate import ADOPT, DONE, RENAME, execute_migration, plan_migration
from enginesave.mirror import mirror_repo
from enginesave.repo import (
    GAME_FILE,
    GAME_ID_RE,
    NEW_SNAPSHOT_ID,
    Repo,
    _check_format,
    base_snapshot_id,
    clean_folder_name,
    folder_name_for,
    valid_folder,
)
from enginesave.verify import verify_objects, verify_repo
from fixtures.builder import build_f1, build_game, make_junction, simple_spec

FIXTURE = Path(__file__).parent / "fixtures" / "library_v1.zip"
ID = "abcdef01-2345-4678-9abc-def012345678"
FIXTURE_FOLDERS = {
    "中文游戏",
    "Bad Name 1",
    "Readable Game",
    "Twin Game",
    "Twin Game (1.0)",
    "Fixture Ace",
    "Orphan-1",
}


@pytest.fixture
def old_library(tmp_path: Path) -> Path:
    """A copy of the format-1 fixture library."""
    root = tmp_path / "old"
    with zipfile.ZipFile(FIXTURE) as archive:
        archive.extractall(root)
    return root


def snapshots(root: Path) -> dict[str, dict[str, list[tuple[str, str, str]]]]:
    """Record id → snapshot id → its files ``(source, path, sha256)``."""
    repo = Repo.open(root)
    return {
        r["game_id"]: {
            sid: sorted(
                (f["source_id"], f["path"], f["sha256"])
                for f in repo.load_manifest(r["game_id"], sid)["files"]
            )
            for sid in repo.snapshot_ids(r["game_id"])
        }
        for r in repo.load_catalog()["games"]
    }


def clean(root: Path) -> bool:
    repo = Repo.open(root)
    checks = verify_repo(repo, repo.load_catalog())
    return bool(checks) and all(c.clean for c in checks) and verify_objects(repo).clean


def refused_by_0_10_0(data: dict[str, Any]) -> bool:
    """What 0.10.0's ``_check_format`` did with a catalog or a game.json: anything but
    format_version 1 is refused."""
    try:
        _check_format(data, "x", Path("x"), (1,))
    except FatalError:
        return True
    return False


def back_up(games_root: Path, saves_root: Path, library: Path) -> Repo:
    plan = prepare_backup([games_root], library, saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    return Repo.open(plan.dest)


# ----------------------------------------------------------------------- folder names


def test_folder_names_follow_the_game_names() -> None:
    """D67 1: characters Windows forbids and control characters go, white space runs become
    one space, trailing dots and spaces go, 80 characters at most; device names and empty
    names become ``game <id>``."""
    assert clean_folder_name('Bad: "Name"? <1>|*.') == "Bad Name 1"
    assert clean_folder_name("  Spaced\t\tName  ") == "Spaced Name"
    assert clean_folder_name("Trailing... . ") == "Trailing"
    assert clean_folder_name("A\x01B\x7fC") == "A B C"
    assert clean_folder_name("中文游戏") == "中文游戏"
    assert clean_folder_name("テスト　ゲーム") == "テスト ゲーム"
    assert len(clean_folder_name("x" * 100)) == 80
    assert clean_folder_name("a" * 79 + " b") == "a" * 79
    for device in ("CON", "nul", "Com1", "LPT9", "con.txt", "AUX .ini", "  ", "...", ""):
        assert clean_folder_name(device) == "", device
        assert folder_name_for(device, None, ID, []) == "game abcdef01"
    assert clean_folder_name("COM10") == "COM10"
    assert [valid_folder(n) for n in ("ok", "a/b", "..", "x.", "x ", "CON", "", "a\\b")] == [
        True, False, False, False, False, False, False, False,
    ]  # fmt: skip


def test_a_name_taken_gets_the_version_then_the_id() -> None:
    """D67 1: ``<name>``, ``<name> (<version>)``, ``<name> (<id>)``, without regard to case."""
    assert folder_name_for("Twin Game", "1.0", ID, ["TWIN GAME"]) == "Twin Game (1.0)"
    taken = ["twin game", "Twin Game (1.0)"]
    assert folder_name_for("Twin Game", "1.0", ID, taken) == "Twin Game (abcdef01)"
    assert folder_name_for("Twin Game", None, ID, ["twin game"]) == "Twin Game (abcdef01)"
    assert folder_name_for("Twin Game", "1.0/beta?", ID, ["twin game"]) == "Twin Game (1.0 beta)"
    assert folder_name_for("Twin Game", "", ID, []) == "Twin Game"
    every = ["twin game", "twin game (abcdef01)", f"twin game ({ID})"]
    assert folder_name_for("Twin Game", None, ID, every) == ID


# --------------------------------------------------------------------- new backups (D67 1, 2)


def test_a_backup_names_its_record_folder_and_snapshot_after_the_game_and_the_time(
    games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_f1(games_root, saves_root)
    for folder, exe, directory in (("Twin A", "TwinA", "TwinA-1"), ("Twin B", "TwinB", "TwinB-2")):
        build_game(games_root, simple_spec(folder, exe, directory, name="Twin"), saves_root)
    moment = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(backup, "utc_now", lambda: moment)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    catalog = repo.load_catalog()
    assert catalog["format_version"] == 2
    folders = {r["display_name"] + r["game_id"][:0]: r.get("folder") for r in catalog["games"]}
    assert folders["Readable Game"] == "Readable Game"
    assert sorted(r["folder"] for r in catalog["games"] if r["display_name"] == "Twin") == [
        "Twin",
        "Twin (1.0)",
    ]
    for record in catalog["games"]:
        folder = repo.root / "games" / record["folder"]
        assert (folder / GAME_FILE).is_file() and repo.game_dir(record["game_id"]) == folder
        assert json.loads((folder / GAME_FILE).read_text(encoding="utf-8"))["format_version"] == 2
    local = moment.astimezone().strftime("%Y-%m-%d %H-%M-%S")
    readable = next(r for r in catalog["games"] if r["display_name"] == "Readable Game")
    assert repo.snapshot_ids(readable["game_id"]) == [local] == [base_snapshot_id(moment)]
    back_up(games_root, saves_root, repo.root)  # the same second again
    assert Repo.open(repo.root).snapshot_ids(readable["game_id"]) == [local, f"{local} (2)"]
    assert not any(GAME_ID_RE.match(p.name) for p in (repo.root / "games").iterdir())


def test_snapshots_are_ordered_by_when_their_manifests_say_they_were_made(tmp_path: Path) -> None:
    """D67 2: by ``created_at``, whatever the ids say (old UTC ids and new local ones mixed);
    a manifest that cannot be read is placed by the time its id names."""
    repo = Repo.open_or_create(tmp_path / "library")
    game_id = ID
    repo.assign_folder(game_id, "Mixed", None)

    def commit(snapshot_id: str, created: str, reason: str = "manual") -> None:
        tmp = repo.new_tmp_dir(game_id, snapshot_id)
        manifest = {"format_version": 2, "snapshot_id": snapshot_id, "created_at": created,
                    "reason": reason, "files": []}  # fmt: skip
        repo.commit_snapshot(tmp, game_id, manifest)

    late_id = datetime(2026, 10, 26, 13, 0, tzinfo=UTC).astimezone().strftime("%Y-%m-%d %H-%M-%S")
    commit("20261026T120000Z", "2026-10-26T12:00:00Z")
    commit(late_id, "2026-10-26T11:00:00Z")  # the id says later, the manifest earlier
    broken_id = datetime(2026, 10, 26, 14, 0, tzinfo=UTC).astimezone().strftime("%Y-%m-%d %H-%M-%S")
    commit(broken_id, "2026-10-26T14:00:00Z", reason="pre_restore")
    (repo.snapshot_dir(game_id, broken_id) / "manifest.json").write_text("{", encoding="utf-8")
    assert repo.snapshot_ids(game_id) == [late_id, "20261026T120000Z", broken_id]
    assert repo.latest_snapshot(game_id) == "20261026T120000Z"
    assert repo.snapshot_created(game_id, late_id) == "2026-10-26T11:00:00Z"
    assert repo.snapshot_created(game_id, broken_id) == "2026-10-26T14:00:00Z"


# ---------------------------------------------------------------------- migration (D67 8)


def test_the_fixture_is_a_library_of_0_10_0(old_library: Path) -> None:
    catalog = json.loads((old_library / "catalog.json").read_text(encoding="utf-8"))
    assert catalog["format_version"] == 1 and len(catalog["games"]) == 7
    assert {o["type"] for o in catalog["overrides"]} == {"link_root", "confirm_source"}
    folders = [p.name for p in (old_library / "games").iterdir()]
    assert len(folders) == 7 and all(GAME_ID_RE.match(name) for name in folders)
    formats = [
        json.loads(m.read_text(encoding="utf-8"))["format_version"]
        for m in (old_library / "games").glob("*/snapshots/*/manifest.json")
    ]
    assert sorted(formats) == [1] + [2] * 13
    assert not refused_by_0_10_0(catalog)


def test_migrating_keeps_every_record_snapshot_object_and_decision(old_library: Path) -> None:
    """D67 8, 9: the folders are renamed, the catalog and game.json get format 2 and a copy of
    the catalog is kept; ids, snapshots, objects, overrides and cleanup records do not change;
    0.10.0 refuses the library afterwards."""
    original = (old_library / "catalog.json").read_bytes()
    before = snapshots(old_library)
    repo = Repo.open(old_library)
    refs = repo.referenced_objects()[0]
    catalog = repo.load_catalog()
    docs = {r["game_id"]: repo.load_game(r["game_id"]) for r in catalog["games"]}
    plan = plan_migration(repo)
    assert plan.needed and not plan.refusals
    assert {m.new for m in plan.moves} == FIXTURE_FOLDERS
    assert all(m.state == RENAME and m.old == m.game_id for m in plan.moves)
    result = execute_migration(repo, plan)
    assert (result.renamed, result.recorded, result.failed) == (7, 7, [])
    assert result.format_version == 2 and not result.cancelled
    backup_copy = Path(str(result.backup))
    assert backup_copy.name.startswith("catalog.json.bak-") and backup_copy.read_bytes() == original
    assert {p.name for p in (old_library / "games").iterdir()} == FIXTURE_FOLDERS
    migrated = Repo.open(old_library)
    after = migrated.load_catalog()
    assert after["format_version"] == 2 and refused_by_0_10_0(after)
    assert after["overrides"] == catalog["overrides"]
    for old, new in zip(catalog["games"], after["games"], strict=True):
        assert {k: v for k, v in new.items() if k != "folder"} == old
        assert new["folder"] in FIXTURE_FOLDERS
        doc = migrated.load_game(new["game_id"])
        assert doc == {**docs[new["game_id"]], "format_version": 2} and refused_by_0_10_0(doc)
    assert snapshots(old_library) == before
    assert migrated.referenced_objects()[0] == refs
    assert clean(old_library)
    # D67 4, 5: every old snapshot got its README.txt, the library its INDEX.txt
    assert result.readmes == 14 and result.index
    readmes = list((old_library / "games").glob("*/snapshots/*/README.txt"))
    assert len(readmes) == 14
    index = (old_library / "INDEX.txt").read_text(encoding="utf-8-sig")
    assert all(f"games\\{name}" in index for name in FIXTURE_FOLDERS)
    odd = next(r for r in after["games"] if r["display_name"].startswith("Bad"))
    assert odd["folder"] == "Bad Name 1" and odd.get("cleaned_at"), "its cleanup is kept"
    assert execute_migration(Repo.open(old_library)).already


def test_both_formats_read_alike(
    old_library: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D67 8: list, show and verify give the same on the library before and after."""
    migrated = tmp_path / "migrated"
    shutil.copytree(old_library, migrated)
    execute_migration(Repo.open(migrated))

    def listing(root: Path) -> list[tuple[str, int, str | None]]:
        assert main(["list", "--repo", str(root), "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        return [(g["game_id"], g["snapshot_count"], g["latest_snapshot"]) for g in data["games"]]

    assert listing(old_library) == listing(migrated)
    for root in (old_library, migrated):
        assert main(["verify", "--repo", str(root)]) == 0
        capsys.readouterr()
    game_id = listing(migrated)[0][0]
    shown = []
    for root in (old_library, migrated):
        assert main(["show", "--repo", str(root), "--game", game_id, "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        shown.append([s["snapshot_id"] for s in data["snapshots"]])
    assert shown[0] == shown[1] and shown[0]


def test_an_interrupted_migration_goes_on_from_where_it_stopped(old_library: Path) -> None:
    """D67 8: stopped between records, the renamed ones are recorded; since 1.0.0 (D70 1)
    the catalog is format 2 before the first rename, so 0.10.0 cannot open the half-renamed
    library, and the records not renamed yet are read at games/<id>. A folder renamed whose
    catalog entry was not written yet is found by the id in its game.json; running it again
    finishes."""
    before = snapshots(old_library)
    asked = iter(range(100))
    stopped = execute_migration(Repo.open(old_library), should_stop=lambda: next(asked) >= 2)
    assert stopped.cancelled and stopped.renamed == 2 and stopped.format_version == 2
    repo = Repo.open(old_library)
    assert not repo.old_format and repo.load_catalog()["format_version"] == 2
    catalog = json.loads((old_library / "catalog.json").read_text(encoding="utf-8"))
    assert refused_by_0_10_0(catalog)  # 0.10.0 cannot open it half-renamed
    assert snapshots(old_library) == before  # every record still reads its snapshots
    plan = plan_migration(repo)
    assert [m.state for m in plan.moves].count(DONE) == 2
    crashed = next(m for m in plan.moves if m.state == RENAME)  # renamed, then a crash
    games = old_library / "games"
    os.rename(games / crashed.old, games / crashed.new)
    again = plan_migration(Repo.open(old_library))
    assert next(m for m in again.moves if m.game_id == crashed.game_id).state == ADOPT
    finished = execute_migration(Repo.open(old_library))
    assert finished.format_version == 2 and not finished.failed
    assert {p.name for p in games.iterdir()} == FIXTURE_FOLDERS
    assert snapshots(old_library) == before and clean(old_library)
    assert len(list(old_library.glob("catalog.json.bak-*"))) == 2


def test_migration_is_refused_with_temporary_folders_or_links(
    old_library: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D67 8: a backup's temporary snapshot folder, or a link or junction in games\\, and
    nothing is migrated (exit code 2); the refusal says how to clean up first."""
    original = (old_library / "catalog.json").read_bytes()
    record = next((old_library / "games").iterdir())
    leftover = record / "snapshots" / ".tmp-20261026T000000Z-abc123"
    leftover.mkdir()
    code = main(["migrate", "--repo", str(old_library)])
    assert code == 2 and "remove --repo" in capsys.readouterr().err
    assert main(["migrate", "--repo", str(old_library), "--dry-run"]) == 2
    assert "Refused:" in capsys.readouterr().out
    leftover.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if not make_junction(old_library / "games" / "link", elsewhere):
        pytest.skip("cannot create a junction here")
    with pytest.raises(UsageError, match="junction"):
        execute_migration(Repo.open(old_library))
    assert (old_library / "catalog.json").read_bytes() == original
    assert not list(old_library.glob("catalog.json.bak-*"))
    assert sum(GAME_ID_RE.match(p.name) is not None for p in record.parent.iterdir()) == 7


def test_a_library_of_format_1_is_read_but_not_written(
    old_library: Path, games_root: Path, saves_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D67 8: commands that write refuse it with the migrate hint (exit code 2) before they do
    anything; reading, ``--dry-run`` and ``remove --tmp`` (needed before a migration) work."""
    build_f1(games_root, saves_root)
    repo = str(old_library)
    game_id = json.loads((old_library / "catalog.json").read_text(encoding="utf-8"))["games"][0][
        "game_id"
    ]
    refused = [
        ["backup", str(games_root), "--dest", repo, "--yes"],
        ["link", "--repo", repo, "--game", game_id, "--game-root", str(games_root)],
        ["remove", "--repo", repo, "--game", game_id, "--yes"],
        ["import", "--repo", repo, "--from", str(old_library / "catalog.json"), "--yes"],
    ]
    for args in refused:
        assert main(args) == 2, args
        assert "enginesave migrate --repo" in capsys.readouterr().err, args
    for args in (
        ["list", "--repo", repo],
        ["verify", "--repo", repo],
        ["show", "--repo", repo, "--game", game_id],
        ["remove", "--repo", repo, "--tmp", "--yes"],
        ["backup", str(games_root), "--dest", repo, "--dry-run"],
    ):
        assert main(args) == 0, args
        capsys.readouterr()
    plan = prepare_backup([games_root], old_library, saves_root_override=saves_root)
    with pytest.raises(UsageError, match="migrate"):
        execute_backup(plan, Repo.open(old_library))
    assert (
        json.loads((old_library / "catalog.json").read_text(encoding="utf-8"))["format_version"]
        == 1
    )


# ------------------------------------------------------------------------ mirror (D67 8)


def test_a_mirror_keeps_its_library_s_format_and_is_migrated_with_it(
    old_library: Path, tmp_path: Path
) -> None:
    """D67 8: a library of format 1 makes a mirror of format 1; once the library is migrated,
    its next mirror migrates the mirror first and gives it the same folder names; a library
    of format 1 never writes into a mirror of format 2."""
    target = tmp_path / "mirror"
    first = mirror_repo(Repo.open(old_library), target)
    assert first.exit_code == 0 and not first.migrated
    assert json.loads((target / "catalog.json").read_text(encoding="utf-8"))["format_version"] == 1
    assert all(GAME_ID_RE.match(p.name) for p in (target / "games").iterdir())
    second_old = tmp_path / "old-copy"
    shutil.copytree(old_library, second_old)
    execute_migration(Repo.open(old_library))
    again = mirror_repo(Repo.open(old_library), target)
    assert again.migrated and again.exit_code == 0 and again.verified is True
    mirror = Repo.open(target)
    assert mirror.format_version == 2
    assert {p.name for p in (target / "games").iterdir()} == FIXTURE_FOLDERS
    ours = {r["game_id"]: r["folder"] for r in Repo.open(old_library).load_catalog()["games"]}
    assert {r["game_id"]: r["folder"] for r in mirror.load_catalog()["games"]} == ours
    assert snapshots(target) == snapshots(old_library)
    with pytest.raises(UsageError, match="not migrated"):
        mirror_repo(Repo.open(second_old), target)


def test_renaming_a_folder_after_the_name_and_the_mirror_following_it(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D67 1: 「按当前名字重命名文件夹…」 renames the record's folder after its name now (the
    catalog says so); the next mirror renames the mirror's folder of that record (by its
    id); a name taken there by a record the mirror keeps (D57 2) leaves the mirror's folder as
    it is, and says so."""
    build_f1(games_root, saves_root)
    build_game(games_root, simple_spec("Second", "Second", "Second-1600000002"), saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    by_name = {r["display_name"]: r["game_id"] for r in repo.load_catalog()["games"]}
    game_id, second = by_name["Readable Game"], by_name["Second"]
    assert repo.folder_rename(game_id) == ("Readable Game", "Readable Game")
    target = tmp_path / "mirror"
    mirror_repo(repo, target)
    doc = repo.load_game(game_id)
    doc["display_name"] = "Readable Game: Director's Cut"
    repo.save_game(doc)
    assert repo.rename_folder(game_id) == ("Readable Game", "Readable Game Director's Cut")
    renamed = Repo.open(repo.root)
    folders = {r["game_id"]: r["folder"] for r in renamed.load_catalog()["games"]}
    assert folders[game_id] == "Readable Game Director's Cut"
    assert renamed.snapshot_ids(game_id) and clean(repo.root)
    followed = mirror_repo(renamed, target)
    assert followed.exit_code == 0 and followed.folders_kept == []
    assert {p.name for p in (target / "games").iterdir()} == {
        "Readable Game Director's Cut",
        "Second",
    }
    assert clean(target)
    renamed.remove_game(game_id)  # the mirror keeps it, under its name
    doc = renamed.load_game(second)
    doc["display_name"] = "Readable Game Director's Cut"
    renamed.save_game(doc)
    assert renamed.rename_folder(second) == ("Second", "Readable Game Director's Cut")
    kept = mirror_repo(Repo.open(repo.root), target)
    assert kept.exit_code == 0 and len(kept.folders_kept) == 1
    mirror = Repo.open(target)
    assert mirror.folder_name(second) == "Second"
    assert mirror.folder_name(game_id) == "Readable Game Director's Cut"
    assert clean(target)


def test_a_snapshot_id_with_spaces_on_the_command_line(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D67 2: ``--snapshot "2026-10-26 18-15-00"`` (one argument, quoted in a shell)."""
    build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    game_id = repo.load_catalog()["games"][0]["game_id"]
    (snapshot_id,) = repo.snapshot_ids(game_id)
    assert " " in snapshot_id
    code = main(["show", "--repo", str(repo.root), "--game", game_id, "--snapshot", snapshot_id])
    assert code == 0 and snapshot_id in capsys.readouterr().out
    import re

    assert re.fullmatch(NEW_SNAPSHOT_ID, snapshot_id)


# ------------------------------------------------------------------- M45 review fixes


def two_games(games_root: Path, saves_root: Path, library: Path) -> Repo:
    build_f1(games_root, saves_root)
    build_game(games_root, simple_spec("Second", "Second", "Second-1600000002"), saves_root)
    return back_up(games_root, saves_root, library)


def test_a_mirror_goes_on_after_a_record_folder_was_left_half_made(
    games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M45 review: the copy of a new record's game.json fails after its folder was made: the
    folder goes again; one left by a killed run (empty) is taken over; the mirror is never
    refused as another library for it."""
    from enginesave import mirror

    build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    target = tmp_path / "mirror"
    assert mirror_repo(repo, target).exit_code == 0
    build_game(games_root, simple_spec("Second", "Second", "Second-1600000002"), saves_root)
    repo = back_up(games_root, saves_root, repo.root)
    real = mirror._copy_text

    def full(source: Path, target_file: Path) -> None:
        if target_file.parent.name == "Second":
            target_file.parent.mkdir(parents=True, exist_ok=True)
            (target_file.parent / ".game.json.tmp-abcd").write_bytes(b"half")
            raise OSError(28, "No space left on device")
        real(source, target_file)

    monkeypatch.setattr(mirror, "_copy_text", full)
    failed = mirror_repo(repo, target)
    assert failed.exit_code == 1 and not (target / "games" / "Second").exists()
    (target / "games" / "Second").mkdir()  # as a killed run leaves it
    monkeypatch.setattr(mirror, "_copy_text", real)
    again = mirror_repo(repo, target)
    assert again.exit_code == 0 and again.folders_kept == []
    assert {p.name for p in (target / "games").iterdir()} == {"Readable Game", "Second"}
    assert clean(target)


def test_snapshot_order_is_remembered_across_library_objects(
    games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M45 review: a manifest whose file did not change is not read again by the next Repo
    (the window makes a new one for each reload)."""
    repo = two_games(games_root, saves_root, tmp_path / "library")
    ids = [r["game_id"] for r in repo.load_catalog()["games"]]
    first = [repo.snapshot_ids(g) for g in ids]
    opened: list[str] = []
    real_open = open

    def counting_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if str(file).endswith("manifest.json"):
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    again = Repo.open(repo.root)
    assert [again.snapshot_ids(g) for g in ids] == first
    assert opened == [], "every created_at came from the cache"


def test_names_windows_gives_as_short_names_are_never_record_folders() -> None:
    """M45 review: ``READAB~1`` could already be another folder's 8.3 name."""
    from enginesave.repo import short_name

    assert short_name("READAB~1") and short_name("abc~12.txt") and not short_name("Re~Zero")
    assert clean_folder_name("READAB~1") == ""
    assert folder_name_for("READAB~1", None, ID, []) == "game abcdef01"
    assert not valid_folder("READAB~1")


def test_a_manifest_s_created_at_is_its_own_not_a_nested_one(tmp_path: Path) -> None:
    """M45 review: only the top-level third key is read from the head; any other manifest
    is parsed whole."""
    repo = Repo.open_or_create(tmp_path / "library")
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 2,
                "note": {"created_at": "2099-01-01T00:00:00Z"},
                "snapshot_id": "x",
                "created_at": "2026-10-26T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    assert repo.created_ns(path) == int(datetime(2026, 10, 26, 12, tzinfo=UTC).timestamp()) * 10**9


def test_a_leftover_record_folder_still_counts_in_the_inventory(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """M45 review: a folder of no record (a removal that could not delete everything) keeps
    its snapshots in the inventory, as in 0.10.0, so the estimate and the store agree."""
    from enginesave.removal import inventory

    repo = two_games(games_root, saves_root, tmp_path / "library")
    record = repo.load_catalog()["games"][0]
    leftover = repo.root / "games" / "Readable Game (old)"
    shutil.copytree(repo.game_dir(record["game_id"]) / "snapshots", leftover / "snapshots")
    found = inventory(Repo.open(repo.root))
    assert any(key == "Readable Game (old)" for key, _sid in found.snapshots)


def test_a_migrated_library_with_a_record_without_folder_is_migrated(old_library: Path) -> None:
    """M45 review: a record whose folder is gone gets its name once; afterwards the library is
    'already migrated', with no second copy of the catalog."""
    record = json.loads((old_library / "catalog.json").read_text(encoding="utf-8"))["games"][0]
    shutil.rmtree(old_library / "games" / record["game_id"])
    first = execute_migration(Repo.open(old_library))
    assert first.format_version == 2 and first.missing
    assert not plan_migration(Repo.open(old_library)).needed
    assert execute_migration(Repo.open(old_library)).already
    assert len(list(old_library.glob("catalog.json.bak-*"))) == 1


def test_the_mirror_follows_case_only_renames_and_swaps(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """M45 review: a rename that only changes case reaches the mirror's catalog too (no rename
    again on the next run); two records that swap names in the library swap in the mirror."""
    repo = two_games(games_root, saves_root, tmp_path / "library")
    ids = {r["display_name"]: r["game_id"] for r in repo.load_catalog()["games"]}
    target = tmp_path / "mirror"
    mirror_repo(repo, target)

    def rename(game_id: str, name: str) -> None:
        doc = repo.load_game(game_id)
        doc["display_name"] = name
        repo.save_game(doc)
        repo.rename_folder(game_id)

    rename(ids["Readable Game"], "READABLE GAME")
    assert mirror_repo(Repo.open(repo.root), target).exit_code == 0
    mirror = Repo.open(target)
    entry = next(r for r in mirror.load_catalog()["games"] if r["game_id"] == ids["Readable Game"])
    assert entry["folder"] == "READABLE GAME"
    assert "READABLE GAME" in os.listdir(target / "games")
    rename(ids["Readable Game"], "Swap")  # through a third name
    rename(ids["Second"], "READABLE GAME")
    rename(ids["Readable Game"], "Second")
    swapped = mirror_repo(Repo.open(repo.root), target)
    assert swapped.exit_code == 0 and swapped.folders_kept == []
    mirror = Repo.open(target)
    assert mirror.folder_name(ids["Readable Game"]) == "Second"
    assert mirror.folder_name(ids["Second"]) == "READABLE GAME"
    assert clean(target)


# ------------------------------------------------- 1.0.0 (D70 1, D72 R14): the migration


def catalog_bytes(root: Path) -> bytes:
    return (root / "catalog.json").read_bytes()


def test_a_failed_rename_leaves_a_library_0_10_cannot_open_and_running_again_finishes(
    old_library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D70 1: one rename fails -- the catalog is format 2 already (0.10.0 refuses it, so it
    cannot give the record a second folder), every record still reads its snapshots (the
    failed one at games/<id>), and running the migration again finishes the job."""
    from enginesave import migrate

    before = snapshots(old_library)
    real_rename = os.rename
    failed: list[str] = []

    def flaky(src: str, dst: str) -> None:
        if not failed:
            failed.append(src)
            raise PermissionError(13, "synthetic: the folder is in use", src)
        real_rename(src, dst)

    monkeypatch.setattr(migrate.os, "rename", flaky)
    result = execute_migration(Repo.open(old_library))
    assert len(result.failed) == 1 and result.renamed == len(FIXTURE_FOLDERS) - 1
    assert result.format_version == 2
    assert refused_by_0_10_0(json.loads(catalog_bytes(old_library)))
    assert snapshots(old_library) == before
    left = plan_migration(Repo.open(old_library))
    assert left.folders_needed and [m.state for m in left.moves].count(RENAME) == 1
    monkeypatch.setattr(migrate.os, "rename", real_rename)
    finished = execute_migration(Repo.open(old_library))
    assert not finished.failed and finished.renamed == 1
    assert {p.name for p in (old_library / "games").iterdir()} == FIXTURE_FOLDERS
    assert not plan_migration(Repo.open(old_library)).needed
    assert snapshots(old_library) == before and clean(old_library)


def test_a_folder_whose_game_json_names_another_record_is_not_renamed(old_library: Path) -> None:
    """D70 1: ``games/<id>`` whose game.json holds another id is not taken for that record."""
    games = old_library / "games"
    folder = next(p for p in sorted(games.iterdir()) if p.is_dir())
    doc = json.loads((folder / "game.json").read_text(encoding="utf-8"))
    doc["game_id"] = ID  # an id no record of the catalog has
    (folder / "game.json").write_text(json.dumps(doc), encoding="utf-8")
    result = execute_migration(Repo.open(old_library))
    assert any(f"holds the record {ID}" in f for f in result.failed)
    assert folder.is_dir()  # left where it is


def test_a_record_in_two_folders_is_refused(old_library: Path) -> None:
    """D70 1: the same record id at games/<id> and in another folder: which one is the record
    is not known -- refused, nothing changes."""
    games = old_library / "games"
    folder = next(p for p in sorted(games.iterdir()) if p.is_dir())
    shutil.copytree(folder, games / "A copy of the record")
    before = catalog_bytes(old_library)
    plan = plan_migration(Repo.open(old_library))
    assert any("is in two folders" in r and "A copy of the record" in r for r in plan.refusals)
    with pytest.raises(UsageError, match="two folders"):
        execute_migration(Repo.open(old_library))
    assert catalog_bytes(old_library) == before
    assert main(["migrate", "--repo", str(old_library)]) == 2


def test_r14_a_record_folder_renamed_to_another_id_shaped_name_is_found(tmp_path: Path) -> None:
    """D72 R14: a record's folder renamed (a rename or migration interrupted after the move,
    before the catalog was written) to a name shaped like an id: found by the id in its
    game.json, its snapshots readable -- in a migrated library and during a migration."""
    games_root, saves = tmp_path / "games", tmp_path / "saves"
    games_root.mkdir()
    saves.mkdir()
    build_f1(games_root, saves)
    plan = prepare_backup([games_root], tmp_path / "repo", saves_root_override=saves)
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    game_id = repo.load_catalog()["games"][0]["game_id"]
    snapshot_ids = repo.snapshot_ids(game_id)
    renamed = repo.game_dir(game_id).with_name("11111111-2222-3333-4444-555555555555")
    os.rename(repo.game_dir(game_id), renamed)
    reopened = Repo.open(repo.root)
    assert reopened.snapshot_ids(game_id) == snapshot_ids
    assert reopened.game_dir(game_id) == renamed


def test_r14_an_old_library_with_an_id_shaped_stray_migrates(old_library: Path) -> None:
    games = old_library / "games"
    entry = next(e for e in json.loads(catalog_bytes(old_library))["games"] if e.get("game_id"))
    stray = games / "11111111-2222-3333-4444-555555555555"
    os.rename(games / entry["game_id"], stray)
    plan = plan_migration(Repo.open(old_library))
    assert next(m for m in plan.moves if m.game_id == entry["game_id"]).state == ADOPT
    finished = execute_migration(Repo.open(old_library))
    assert not finished.failed
    assert Repo.open(old_library).snapshot_ids(entry["game_id"])


def test_a_migrated_library_without_its_readmes_gets_them(old_library: Path) -> None:
    """D70 1: format 2 with README.txt missing (the migration stopped in its last step): the
    window writes them in the background through ``write_missing_readmes``."""
    from enginesave.layout import README, missing_readmes
    from enginesave.migrate import write_missing_readmes

    assert not execute_migration(Repo.open(old_library)).failed
    repo = Repo.open(old_library)
    gone = []
    for game_id, snapshot_id in [
        (r["game_id"], s)
        for r in repo.load_catalog()["games"]
        for s in repo.snapshot_ids(r["game_id"])
    ][:2]:
        (repo.snapshot_dir(game_id, snapshot_id) / README).unlink()
        gone.append((game_id, snapshot_id))
    assert sorted(missing_readmes(repo)) == sorted(gone)
    assert plan_migration(repo).readmes_missing == 2 and not plan_migration(repo).folders_needed
    result = write_missing_readmes(Repo.open(old_library))
    assert result.readmes == 2 and not list(missing_readmes(Repo.open(old_library)))
