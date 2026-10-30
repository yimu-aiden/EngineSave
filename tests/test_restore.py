"""Restore scenarios (plan 9.2 and 10.3)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from enginesave import store
from enginesave.backup import execute_backup, prepare_backup
from enginesave.errors import FatalError, UsageError
from enginesave.fsutil import ns_from_iso
from enginesave.repo import Repo
from enginesave.restore import (
    RECORDED_INSTALL,
    SCANNED_INSTALL,
    execute_restore,
    install_candidates,
    plan_restore,
)
from enginesave.scan import ScanOptions, run_scan
from enginesave.verify import verify_repo
from fixtures.builder import (
    BuiltGame,
    build_f1,
    build_f4,
    build_f8,
    build_game,
    reinstall_copy,
    simple_spec,
    stored,
)


def back_up(roots: list[Path], dest: Path, saves_root: Path, **options: object) -> Repo:
    plan = prepare_backup(roots, dest, saves_root_override=saves_root, **options)
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    return repo


def plan(repo: Repo, game: str, saves_root: Path, **kwargs: object):
    return plan_restore(repo, repo.load_catalog(), game, saves_root_override=saves_root, **kwargs)


@pytest.fixture
def f1_backed_up(games_root: Path, saves_root: Path, tmp_path: Path) -> tuple[BuiltGame, Repo]:
    built = build_f1(games_root, saves_root)
    return built, back_up([games_root], tmp_path / "repo", saves_root)


def test_restore_after_the_game_and_its_saves_are_deleted(
    f1_backed_up: tuple[BuiltGame, Repo], saves_root: Path, tmp_path: Path
) -> None:
    built, repo = f1_backed_up
    assert built.global_dir is not None
    new_root = reinstall_copy(built.root, tmp_path / "Reinstalled")  # unpacked elsewhere
    shutil.rmtree(built.root)
    shutil.rmtree(built.global_dir)
    assert all(c.clean for c in verify_repo(repo, repo.load_catalog()))

    restore_plan = plan(repo, "readable game", saves_root, game_root=new_root)
    assert (restore_plan.game_root, restore_plan.notes) == (new_root, []), "same version"
    local, global_ = restore_plan.targets
    assert (local.target_dir, local.resolved_by, local.creates_dir) == (
        new_root / "game" / "saves",
        "--game-root",
        True,
    )
    assert (global_.target_dir, global_.resolved_by) == (built.global_dir, "override")
    result = execute_restore(restore_plan)
    assert (result.written, result.failures, result.safety_snapshot) == (12, [], None)
    for name, data in built.local_files.items():
        assert (new_root / "game" / "saves" / name).read_bytes() == data
    for rel, data in built.global_files.items():
        assert (built.global_dir / rel).read_bytes() == data
    item = restore_plan.manifest["files"][0]
    restored = new_root / "game" / "saves" / item["path"]
    assert restored.stat().st_mtime_ns // 1_000_000 == ns_from_iso(item["mtime"]) // 1_000_000
    assert not list(new_root.rglob("*.enginesave-tmp"))


def test_a_restore_records_the_game_folder_as_an_install(
    f1_backed_up: tuple[BuiltGame, Repo],
    saves_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D43: after a cleanup the record is "backup only"; restoring the local saves into a
    reinstalled copy records that folder as an install, and the record is installed again."""
    from enginesave.cleanup import GAME_ROOT, PERMANENT, execute_cleanup, plan_cleanup
    from enginesave.cli import main
    from enginesave.repo import install_present

    built, repo = f1_backed_up
    (record,) = repo.load_catalog()["games"]
    cleanup = plan_cleanup(
        repo, record, [(GAME_ROOT, None)], method=PERMANENT, saves_root_override=saves_root
    )
    execute_cleanup(cleanup)
    assert not install_present(repo.load_game(record["game_id"]))
    assert main(["list", "--repo", str(repo.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["games"][0]["install_present"] is False
    assert main(["list", "--repo", str(repo.root)]) == 0
    assert "backup only" in capsys.readouterr().out

    new_root = reinstall_copy(build_f1(tmp_path / "again", saves_root).root, tmp_path / "New")
    shutil.rmtree(tmp_path / "again")
    result = execute_restore(plan(repo, "readable game", saves_root, game_root=new_root))
    assert result.failures == [] and result.registered_install == str(new_root)
    doc = repo.load_game(record["game_id"])
    (old, new) = doc["installs"]
    assert old["removed_at"] and "removed_at" not in new and new["game_root"] == str(new_root)
    assert new["first_seen"] == new["last_seen"] and install_present(doc)
    assert "cleaned_at" not in repo.load_catalog()["games"][0], "no longer 'game removed'"
    assert main(["list", "--repo", str(repo.root)]) == 0
    out = capsys.readouterr().out
    assert "installed" in out and "backup only" not in out and "game removed" not in out


def test_restoring_global_saves_only_records_no_install(
    f1_backed_up: tuple[BuiltGame, Repo], saves_root: Path
) -> None:
    built, repo = f1_backed_up
    before = repo.load_game(repo.load_catalog()["games"][0]["game_id"])["installs"]
    result = execute_restore(plan(repo, "readable game", saves_root, sources=["global"]))
    assert result.registered_install is None
    assert repo.load_game(repo.load_catalog()["games"][0]["game_id"])["installs"] == before


def test_restoring_into_the_cleaned_folder_again_makes_it_current(
    f1_backed_up: tuple[BuiltGame, Repo], saves_root: Path, tmp_path: Path
) -> None:
    """D43: the game unpacked again where it was before: that install loses its
    ``removed_at`` (no second entry), and the record is no longer "game removed"."""
    from enginesave.cleanup import GAME_ROOT, PERMANENT, execute_cleanup, plan_cleanup

    built, repo = f1_backed_up
    (record,) = repo.load_catalog()["games"]
    unpacked = reinstall_copy(built.root, tmp_path / "unpacked")
    cleanup = plan_cleanup(
        repo, record, [(GAME_ROOT, None)], method=PERMANENT, saves_root_override=saves_root
    )
    execute_cleanup(cleanup)
    assert repo.load_game(record["game_id"])["installs"][0]["removed_at"]
    shutil.move(unpacked, built.root)  # a fixture copy in tmp_path, back in its old place
    result = execute_restore(plan(repo, "readable game", saves_root, game_root=built.root))
    (install,) = repo.load_game(record["game_id"])["installs"]
    assert result.registered_install == str(built.root) and "removed_at" not in install
    assert "cleaned_at" not in repo.load_catalog()["games"][0]


def test_no_install_is_recorded_after_a_failed_file_or_a_dry_run(
    f1_backed_up: tuple[BuiltGame, Repo],
    saves_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D43: only a restore whose local saves all went in records the folder."""
    from enginesave.cli import main
    from enginesave.restore import RestoreResult, register_install

    built, repo = f1_backed_up
    game_id = repo.load_catalog()["games"][0]["game_id"]
    before = repo.load_game(game_id)["installs"]
    new_root = reinstall_copy(built.root, tmp_path / "New")
    args = ["restore", "--repo", str(repo.root), "--game", "readable game", "--json"]
    args += ["--game-root", str(new_root), "--renpy-saves-root", str(saves_root), "--dry-run"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["plan"]["game_root"] == str(new_root)
    assert repo.load_game(game_id)["installs"] == before, "a dry run writes nothing"
    planned = plan(repo, "readable game", saves_root, game_root=new_root)
    failed = RestoreResult(written=3, failures=[{"source_id": "local", "path": "1-1-LT1.save"}])
    assert register_install(planned, failed) is None
    assert repo.load_game(game_id)["installs"] == before


def test_second_restore_protects_current_files_and_keeps_extras(
    f1_backed_up: tuple[BuiltGame, Repo], saves_root: Path
) -> None:
    built, repo = f1_backed_up
    extra = built.local_dir / "extra-LT1.save"
    extra.write_bytes(b"made after the backup")
    changed = built.local_dir / "1-1-LT1.save"
    changed.write_bytes(b"newer progress")
    restore_plan = plan(repo, "readable game", saves_root)
    local = restore_plan.targets[0]
    assert (local.resolved_by, local.overwrites, local.extra_in_target) == (
        RECORDED_INSTALL,
        4,
        1,
    )
    result = execute_restore(restore_plan)
    assert result.failures == [] and result.safety_snapshot is not None
    assert changed.read_bytes() == built.local_files["1-1-LT1.save"]
    assert extra.read_bytes() == b"made after the backup", "extra files are never deleted"
    game_id = restore_plan.game_id
    safety = repo.load_manifest(game_id, result.safety_snapshot)
    assert safety["reason"] == "pre_restore" and safety["status"] == "complete"
    saved = {(f["source_id"], f["path"]) for f in safety["files"]}
    assert ("local", "extra-LT1.save") in saved
    assert repo.latest_snapshot(game_id) == restore_plan.snapshot_id
    assert all(c.clean for c in verify_repo(repo, repo.load_catalog()))


def test_missing_install_needs_game_root(
    f1_backed_up: tuple[BuiltGame, Repo], saves_root: Path
) -> None:
    built, repo = f1_backed_up
    shutil.rmtree(built.root)
    with pytest.raises(UsageError, match="pass --game-root"):
        plan(repo, "readable game", saves_root)
    only_global = plan(repo, "readable game", saves_root, sources=["global"])
    assert [t.source_id for t in only_global.targets] == ["global"]
    with pytest.raises(UsageError, match="available: local, global"):
        plan(repo, "readable game", saves_root, sources=["nope"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("files.path", "../../evil.save"),
        ("files.path", "C:\\Windows\\evil.save"),
        ("sources.relative_root", "../outside"),
        ("sources.source_id", "../x"),
    ],
)
def test_manifest_path_injection_is_refused(
    f1_backed_up: tuple[BuiltGame, Repo], saves_root: Path, field: str, value: str
) -> None:
    built, repo = f1_backed_up
    catalog = repo.load_catalog()
    game_id = catalog["games"][0]["game_id"]
    snapshot_id = repo.snapshot_ids(game_id)[0]
    manifest_path = repo.snapshot_dir(game_id, snapshot_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    section, key = field.split(".")
    index = 0 if section == "files" else 1
    manifest[section][index][key] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = sorted(p.name for p in built.root.parent.parent.iterdir())
    with pytest.raises(FatalError, match="invalid or unsafe"):
        plan(repo, "readable game", saves_root)
    assert sorted(p.name for p in built.root.parent.parent.iterdir()) == before


def test_failed_safety_snapshot_aborts(
    f1_backed_up: tuple[BuiltGame, Repo], saves_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built, repo = f1_backed_up
    changed = built.local_dir / "1-1-LT1.save"
    changed.write_bytes(b"newer progress")
    restore_plan = plan(repo, "readable game", saves_root)

    def boom(path: Path) -> tuple[str, int]:
        raise RuntimeError("disk failure")

    monkeypatch.setattr(store, "hash_file", boom)
    with pytest.raises(FatalError, match="safety snapshot failed"):
        execute_restore(restore_plan)
    assert changed.read_bytes() == b"newer progress"
    assert len(repo.snapshot_ids(restore_plan.game_id)) == 1


def test_corrupt_stored_copy_is_not_restored(
    f1_backed_up: tuple[BuiltGame, Repo], saves_root: Path, tmp_path: Path
) -> None:
    built, repo = f1_backed_up
    fresh = reinstall_copy(built.root, tmp_path / "fresh")
    restore_plan = plan(repo, "readable game", saves_root, game_root=fresh)
    ids = (restore_plan.game_id, restore_plan.snapshot_id)
    # Bit rot in a stored file damages the shared object: every mirror of it is affected.
    stored(repo, *ids, "local", "persistent").write_bytes(b"bit rot")
    result = execute_restore(restore_plan)
    assert result.exit_code == 1
    assert [(f["source_id"], f["path"]) for f in result.failures] == [
        ("local", "persistent"),
        ("global", "persistent"),
        ("global", "sync/persistent"),
    ]
    assert all("does not match the manifest" in f["error"] for f in result.failures)
    assert not (tmp_path / "fresh" / "game" / "saves" / "persistent").exists()
    assert built.global_dir is not None
    assert (built.global_dir / "persistent").read_bytes() == built.global_files["persistent"]
    assert result.written == 9
    # The safety snapshot met the damaged object and stored a correct plain copy instead.
    safety = repo.load_manifest(restore_plan.game_id, result.safety_snapshot)
    assert (safety["status"], safety["storage"]) == ("complete", "mixed")


def test_several_installs(games_root: Path, saves_root: Path, tmp_path: Path) -> None:
    v1, v2 = build_f4(games_root, saves_root)
    repo = back_up([games_root], tmp_path / "repo", saves_root)
    with pytest.raises(UsageError, match="several game-root sources"):
        plan(repo, "game a", saves_root, game_root=tmp_path / "x")
    x = reinstall_copy(v2.root, tmp_path / "x")
    one = plan(repo, "game a", saves_root, game_root=x, sources=["local-2"])
    assert one.targets[0].target_dir == x / "game" / "saves"
    both = plan(repo, "game a", saves_root)
    roots = {t.source_id: (t.target_dir, t.resolved_by) for t in both.targets}
    assert roots["local"] == (v1.local_dir, "original install")
    assert roots["local-2"] == (v2.local_dir, "original install")


def test_unlinked_record_restores_into_saves_root(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    build_f8(saves_root)
    repo = back_up([games_root], tmp_path / "repo", saves_root, include_unlinked=["OrphanTwo"])
    shutil.rmtree(saves_root / "OrphanTwo")
    restore_plan = plan(repo, "OrphanTwo", saves_root)
    assert [t.target_dir for t in restore_plan.targets] == [saves_root / "OrphanTwo"]
    assert execute_restore(restore_plan).written == 1
    assert (saves_root / "OrphanTwo" / "persistent").is_file()


def test_tokens_only_when_explicitly_requested(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    repo = back_up([games_root], tmp_path / "repo", saves_root, include_tokens=True)
    default = plan(repo, "readable game", saves_root)
    assert "machine_tokens" not in [t.source_id for t in default.targets]
    assert not default.includes_tokens
    explicit = plan(repo, "readable game", saves_root, sources=["machine_tokens"])
    assert explicit.includes_tokens
    assert explicit.targets[0].target_dir == saves_root / "tokens"


def test_target_inside_repository_is_refused(
    f1_backed_up: tuple[BuiltGame, Repo], saves_root: Path
) -> None:
    _, repo = f1_backed_up
    with pytest.raises(UsageError, match="overlaps the backup repository"):
        plan(repo, "readable game", saves_root, game_root=repo.root / "games")


# ------------------------------------------------------- --game-root and candidates (D39, M21)


def test_game_root_must_be_that_game(
    f1_backed_up: tuple[BuiltGame, Repo], games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """The folder an archive unpacks into is replaced by the game inside it; no game, two
    games or another game's folder is refused (the gap the third real sample showed)."""
    built, repo = f1_backed_up
    wrapper = tmp_path / "ReadableGame-1.0-pc"
    inner = reinstall_copy(built.root, wrapper / "ReadableGame-1.0-pc")
    restore_plan = plan(repo, "readable game", saves_root, game_root=wrapper)
    assert restore_plan.game_root == inner and restore_plan.given_root == wrapper
    assert restore_plan.targets[0].target_dir == inner / "game" / "saves"
    assert any("holds the game in" in note for note in restore_plan.notes)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(UsageError, match="is not the folder of a game"):
        plan(repo, "readable game", saves_root, game_root=empty)
    reinstall_copy(built.root, wrapper / "second copy")
    with pytest.raises(UsageError, match="is not the folder of a game"):
        plan(repo, "readable game", saves_root, game_root=wrapper)  # two games in it
    other = build_game(tmp_path / "others", simple_spec("Other Game", "Other", "Other-1"), None)
    with pytest.raises(UsageError, match=r"save directory Other-1\), not Readable Game"):
        plan(repo, "readable game", saves_root, game_root=other.root)


def test_several_installs_are_never_chosen_by_themselves(
    f1_backed_up: tuple[BuiltGame, Repo], saves_root: Path, tmp_path: Path
) -> None:
    """D39: the record's install plus an install a scan recognised as this record."""
    built, repo = f1_backed_up
    newer = reinstall_copy(built.root, tmp_path / "unpacked" / "Readable Game 1.0")
    report = run_scan(
        ScanOptions(roots=[tmp_path / "unpacked"], saves_root_override=saves_root, repo=repo.root)
    )
    (doc,) = repo.known_games(repo.load_catalog())
    assert install_candidates(doc) == [(built.root, RECORDED_INSTALL)]
    assert install_candidates(doc, report) == [
        (built.root, RECORDED_INSTALL),
        (newer, SCANNED_INSTALL),
    ]
    with pytest.raises(UsageError, match="installed in several places; choose one"):
        plan(repo, "readable game", saves_root, report=report)
    assert plan(repo, "readable game", saves_root).game_root == built.root, "the scan not given"
    chosen = plan(repo, "readable game", saves_root, report=report, game_root=newer)
    assert chosen.targets[0].target_dir == newer / "game" / "saves"
    only_global = plan(repo, "readable game", saves_root, report=report, sources=["global"])
    assert only_global.game_root is None, "no local saves: no install to choose"
    shutil.rmtree(built.root)  # the old install is gone: the scanned one is the only candidate
    alone = plan(repo, "readable game", saves_root, report=report)
    assert (alone.game_root, alone.targets[0].resolved_by) == (newer, SCANNED_INSTALL)


def test_a_different_game_version_is_said_not_refused(
    f1_backed_up: tuple[BuiltGame, Repo], saves_root: Path, tmp_path: Path
) -> None:
    built, repo = f1_backed_up
    upgraded = build_game(
        tmp_path / "upgraded",
        simple_spec(
            "Readable Game 2.0",
            "ReadableGame",
            "ReadableGame-1600000001",
            name="Readable Game",
            version="2.0",
            local_saves=0,
        ),
        None,
    )
    restore_plan = plan(repo, "readable game", saves_root, game_root=upgraded.root)
    assert (restore_plan.saved_version, restore_plan.target_version) == ("1.0", "2.0")
    assert restore_plan.notes == [
        "the saves come from version 1.0, the target is version 2.0; "
        "whether the game loads them depends on the game"
    ]
    assert execute_restore(restore_plan).failures == []


def test_the_preview_names_the_game_that_shares_the_global_folder(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D48 1: restoring one season's global saves overwrites the other season's saves in the
    same folder: the plan says so, names it with its version, and ``--json`` lists it."""
    from enginesave.cli import main
    from fixtures.builder import build_f18

    one, _two = build_f18(games_root, saves_root)
    repo = back_up([games_root], tmp_path / "repo", saves_root)
    season_one = next(
        r["game_id"]
        for r in repo.load_catalog()["games"]
        if repo.load_game(r["game_id"])["installs"][0]["game_root"] == str(one.root)
    )
    preview = plan(repo, season_one, saves_root, sources=["global"])
    assert preview.shared_with == ["SharedSeasons (Season2-v9.00)"]
    args = ["restore", "--repo", str(repo.root), "--game", season_one, "--sources", "global"]
    args += ["--renpy-saves-root", str(saves_root), "--dry-run", "--json"]
    assert main(args) == 0
    data = json.loads(capsys.readouterr().out)["plan"]
    assert data["shared_with"] == ["SharedSeasons (Season2-v9.00)"]
    assert any(
        "is shared with SharedSeasons (Season2-v9.00); restoring also overwrites" in note
        for note in data["notes"]
    )
