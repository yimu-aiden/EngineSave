"""Backup scenarios (plan section 8 and 10.3)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from enginesave import backup, store
from enginesave.backup import BackupPlan, BackupResult, execute_backup, prepare_backup
from enginesave.errors import UsageError
from enginesave.fsutil import ns_from_iso
from enginesave.repo import Repo
from enginesave.verify import verify_repo
from fixtures.builder import (
    build_f1,
    build_f3,
    build_f5,
    build_f6,
    build_f7,
    build_f8,
    build_f19,
    stored,
)
from fixtures.fsprobe import needs_hard_links


def do_backup(
    roots: list[Path], dest: Path, saves_root: Path, **options: object
) -> tuple[BackupPlan, Repo, BackupResult]:
    plan = prepare_backup(roots, dest, saves_root_override=saves_root, **options)
    repo = Repo.open_or_create(plan.dest)
    return plan, repo, execute_backup(plan, repo)


def leftovers(dest: Path) -> list[Path]:
    return list(dest.glob("games/*/snapshots/.tmp-*"))


def tree_state(*roots: Path) -> dict[str, tuple[int, int, str]]:
    state = {}
    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                path = Path(dirpath) / name
                st = path.stat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                state[str(path)] = (st.st_mtime_ns, st.st_size, digest)
    return state


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    return tmp_path / "backup"


@needs_hard_links
def test_backup_is_complete_verified_and_described(
    games_root: Path, saves_root: Path, dest: Path
) -> None:
    built = build_f1(games_root, saves_root)
    _, repo, result = do_backup([games_root], dest, saves_root)
    assert result.exit_code == 0
    (outcome,) = result.results
    assert outcome.status == "ok"
    assert outcome.file_count == len(built.local_files) + len(built.global_files)
    manifest = repo.load_manifest(outcome.game_id, outcome.snapshot_id)
    assert (manifest["status"], manifest["reason"]) == ("complete", "manual")
    assert manifest["snapshot_id"] == outcome.snapshot_id
    by_id = {s["source_id"]: s for s in manifest["sources"]}
    assert {k: (v["file_count"], v["consistency"]) for k, v in by_id.items()} == {
        "local": (4, "stable"),
        "global": (8, "stable"),
    }
    assert by_id["global"]["root_token"] == "RENPY_SAVES_ROOT"
    assert by_id["global"]["relative_root"] == "ReadableGame-1600000001"
    for item in manifest["files"]:
        file = stored(repo, outcome.game_id, outcome.snapshot_id, item["source_id"], item["path"])
        assert hashlib.sha256(file.read_bytes()).hexdigest() == item["sha256"]
    original = built.local_dir / "persistent"
    entry = next(
        f for f in manifest["files"] if (f["source_id"], f["path"]) == ("local", "persistent")
    )
    assert ns_from_iso(entry["mtime"]) // 1_000_000 == original.stat().st_mtime_ns // 1_000_000
    assert (manifest["format_version"], manifest["storage"]) == (3, "hardlink")
    persistent = stored(repo, outcome.game_id, outcome.snapshot_id, "local", "persistent")
    assert persistent.parent.parts[-4:] == ("data", "GAME_ROOT", "game", "saves"), "D67 3"
    assert persistent.stat().st_nlink >= 2, "data files are hard links into the object store"

    catalog = repo.load_catalog()
    (entry,) = catalog["games"]
    assert (entry["display_name"], entry["kind"], entry["snapshot_count"]) == (
        "Readable Game",
        "game",
        1,
    )
    doc = repo.load_game(outcome.game_id)
    assert doc["identity"]["renpy_save_directory"] == "ReadableGame-1600000001"
    assert [i["game_root"] for i in doc["installs"]] == [str(built.root)]
    assert {t["source_id"]: t["root_token"] for t in doc["source_templates"]} == {
        "local": "GAME_ROOT",
        "global": "RENPY_SAVES_ROOT",
    }
    checks = verify_repo(repo, catalog)
    assert [(c.clean, c.ok) for c in checks] == [(True, 12)]
    assert leftovers(dest) == []


def test_backup_never_modifies_sources(games_root: Path, saves_root: Path, dest: Path) -> None:
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    before = tree_state(games_root, saves_root)
    do_backup([games_root], dest, saves_root, include_unlinked=[], include_tokens=True)
    assert tree_state(games_root, saves_root) == before


def test_failure_midway_commits_nothing(
    games_root: Path, saves_root: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_f1(games_root, saves_root)
    _, repo, first = do_backup([games_root], dest, saves_root)
    first_state = tree_state(dest / "games")
    real, calls = store.hash_file, {"n": 0}

    def flaky(path: Path) -> tuple[str, int]:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash")
        return real(path)

    monkeypatch.setattr(store, "hash_file", flaky)
    _, repo, second = do_backup([games_root], dest, saves_root)
    (outcome,) = second.results
    assert second.exit_code == 1
    assert outcome.status == "failed" and "simulated crash" in outcome.message
    assert leftovers(dest) == []
    assert repo.snapshot_ids(first.results[0].game_id) == [first.results[0].snapshot_id]
    assert tree_state(dest / "games") == {k: v for k, v in first_state.items()}, (
        "existing snapshot and metadata untouched"
    )


def test_failed_first_backup_leaves_no_game_folder(
    games_root: Path, saves_root: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_f1(games_root, saves_root)

    def boom(path: Path) -> tuple[str, int]:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store, "hash_file", boom)
    _, repo, result = do_backup([games_root], dest, saves_root)
    assert result.results[0].status == "failed"
    assert list((dest / "games").glob("*")) == []
    assert repo.load_catalog()["games"] == []


def test_change_during_copy_marks_partial(
    games_root: Path, saves_root: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = build_f1(games_root, saves_root)
    real_copy, touched = store.copy_and_verify, []

    def copy_then_touch(repo: Repo, src: Path, target: Path) -> tuple[str, int, str]:
        result = real_copy(repo, src, target)
        if not touched:
            victim = built.local_dir / "persistent"
            st = victim.stat()
            os.utime(victim, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
            touched.append(victim)
        return result

    monkeypatch.setattr(store, "copy_and_verify", copy_then_touch)
    _, repo, result = do_backup([games_root], dest, saves_root)
    (outcome,) = result.results
    assert result.exit_code == 1
    assert outcome.status == "partial"
    assert outcome.changed_sources == ["local"]
    assert "close the game" in outcome.message
    manifest = repo.load_manifest(outcome.game_id, outcome.snapshot_id)
    assert manifest["status"] == "partial"
    consistency = {s["source_id"]: s["consistency"] for s in manifest["sources"]}
    assert consistency == {"local": "changed_during_backup", "global": "stable"}


def test_unreadable_source_file_is_reported(
    games_root: Path, saves_root: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_f1(games_root, saves_root)
    real_copy = store.copy_and_verify

    def locked(repo: Repo, src: Path, target: Path) -> tuple[str, int, str]:
        if src.name == "persistent" and src.parent.name == "saves":
            raise store.SourceReadError("PermissionError: [WinError 32] file is in use")
        return real_copy(repo, src, target)

    monkeypatch.setattr(store, "copy_and_verify", locked)
    _, repo, result = do_backup([games_root], dest, saves_root)
    (outcome,) = result.results
    assert (result.exit_code, outcome.status, outcome.file_count) == (1, "partial", 11)
    assert outcome.errors == [
        {
            "source_id": "local",
            "path": "persistent",
            "error": "PermissionError: [WinError 32] file is in use",
        }
    ]
    assert all(c.clean for c in verify_repo(repo, repo.load_catalog()))


def test_readback_mismatch_discards_the_copy(
    games_root: Path, saves_root: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_f1(games_root, saves_root)
    real = store.hash_file

    def corrupt(path: Path) -> tuple[str, int]:
        digest, size = real(path)
        if path.name == "1-1-LT1.save" and "GAME_ROOT" in path.parts:  # the local copy (D67 3)
            return "0" * 64, size
        return digest, size

    monkeypatch.setattr(store, "hash_file", corrupt)
    _, repo, result = do_backup([games_root], dest, saves_root)
    (outcome,) = result.results
    assert outcome.status == "partial"
    assert [(e["source_id"], e["path"]) for e in outcome.errors] == [("local", "1-1-LT1.save")]
    folder = stored(repo, outcome.game_id, outcome.snapshot_id, "local")
    assert not (folder / "1-1-LT1.save").exists()


def test_repeated_backup_keeps_earlier_snapshot(
    games_root: Path, saves_root: Path, dest: Path
) -> None:
    build_f1(games_root, saves_root)
    _, repo, first = do_backup([games_root], dest, saves_root)
    game_id, first_id = first.results[0].game_id, first.results[0].snapshot_id
    before = tree_state(repo.snapshot_dir(game_id, first_id))
    plan, repo, second = do_backup([games_root], dest, saves_root)
    assert plan.repo_state == "existing"
    assert second.results[0].game_id == game_id
    ids = repo.snapshot_ids(game_id)
    assert len(ids) == 2 and ids[0] == first_id
    assert tree_state(repo.snapshot_dir(game_id, first_id)) == before
    assert repo.load_catalog()["games"][0]["snapshot_count"] == 2
    assert len(repo.load_game(game_id)["installs"]) == 1


def test_game_without_save_directory_keeps_its_identity(
    games_root: Path, saves_root: Path, dest: Path
) -> None:
    build_f6(games_root, saves_root)
    _, _, first = do_backup([games_root], dest, saves_root)
    _, repo, second = do_backup([games_root], dest, saves_root)
    assert first.results[0].game_id == second.results[0].game_id
    assert len(repo.load_catalog()["games"]) == 1


def test_destination_safety(games_root: Path, saves_root: Path, tmp_path: Path) -> None:
    build_f1(games_root, saves_root)
    for bad in (games_root / "backup", saves_root / "backup", tmp_path):
        with pytest.raises(UsageError):
            prepare_backup([games_root], bad, saves_root_override=saves_root)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("someone else's folder")
    with pytest.raises(UsageError, match="not empty"):
        prepare_backup([games_root], foreign, saves_root_override=saves_root)
    assert not list(foreign.glob("catalog.json"))


def test_candidates_and_conflicts_back_up_local_only_by_default(
    games_root: Path, saves_root: Path, dest: Path
) -> None:
    build_f5(games_root, saves_root)
    build_f19(games_root, saves_root)  # CONFLICT
    plan = prepare_backup([games_root], dest, saves_root_override=saves_root)
    assert {g.display_name: [s.source.source_id for s in g.sources] for g in plan.games} == {
        "Dynamic Game": ["local"],
        "Split Story": ["local"],
    }
    wide = prepare_backup(
        [games_root], dest, saves_root_override=saves_root, include_candidates=True
    )
    assert all([s.source.source_id for s in g.sources] == ["local", "global"] for g in wide.games)


def test_unsupported_and_not_found_are_skipped_with_reasons(
    games_root: Path, saves_root: Path, dest: Path
) -> None:
    build_f3(games_root, saves_root)
    build_f7(games_root)
    plan = prepare_backup([games_root], dest, saves_root_override=saves_root)
    assert plan.games == []
    reasons = {s["name"]: s["reason"] for s in plan.skipped}
    assert reasons["Foo"] == "unsupported engine or structure"
    assert reasons["Fresh Game"].startswith("no CONFIRMED/USER_CONFIRMED source with files")


def test_include_unlinked(games_root: Path, saves_root: Path, dest: Path) -> None:
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    assert all(
        g.kind == "game"
        for g in prepare_backup([games_root], dest, saves_root_override=saves_root).games
    )
    every = prepare_backup([games_root], dest, saves_root_override=saves_root, include_unlinked=[])
    assert sorted(g.display_name for g in every.games if g.kind == "unlinked") == [
        "Orphan-1500000000",
        "OrphanTwo",
    ]
    with pytest.raises(UsageError, match="no such folder"):
        prepare_backup(
            [games_root], dest, saves_root_override=saves_root, include_unlinked=["Nope"]
        )
    _, repo, result = do_backup(
        [games_root], dest, saves_root, include_unlinked=["orphan-1500000000"]
    )
    orphan = next(r for r in result.results if r.kind == "unlinked")
    assert (orphan.status, orphan.file_count) == ("ok", 2)
    entry = next(e for e in repo.load_catalog()["games"] if e["kind"] == "unlinked")
    assert entry["display_name"] == "Orphan-1500000000"
    assert entry["identity"]["renpy_save_directory"] == "Orphan-1500000000"
    (source,) = repo.load_manifest(orphan.game_id, orphan.snapshot_id)["sources"]
    assert (source["source_id"], source["root_token"], source["relative_root"]) == (
        "global",
        "RENPY_SAVES_ROOT",
        "Orphan-1500000000",
    )


def test_include_tokens(games_root: Path, saves_root: Path, dest: Path) -> None:
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    plain = prepare_backup([games_root], dest, saves_root_override=saves_root)
    assert all(s.source.source_id != "machine_tokens" for s in plain.games[0].sources)
    _, repo, result = do_backup([games_root], dest, saves_root, include_tokens=True)
    outcome = result.results[0]
    manifest = repo.load_manifest(outcome.game_id, outcome.snapshot_id)
    tokens = next(s for s in manifest["sources"] if s["source_id"] == "machine_tokens")
    assert (tokens["kind"], tokens["relative_root"]) == ("machine_tokens", "tokens")
    paths = {f["path"] for f in manifest["files"] if f["source_id"] == "machine_tokens"}
    assert paths == {"security_keys.txt", "upgraded.txt"}


def test_select(games_root: Path, saves_root: Path, dest: Path) -> None:
    build_f1(games_root, saves_root)
    build_f5(games_root, saves_root)
    by_dir = prepare_backup(
        [games_root], dest, saves_root_override=saves_root, select=["readablegame-1600000001"]
    )
    assert [g.display_name for g in by_dir.games] == ["Readable Game"]
    by_root = prepare_backup(
        [games_root], dest, saves_root_override=saves_root, select=["Dynamic Game"]
    )
    assert [g.display_name for g in by_root.games] == ["Dynamic Game"]
    with pytest.raises(UsageError, match="matched no scanned game"):
        prepare_backup([games_root], dest, saves_root_override=saves_root, select=["nope"])


def test_select_roots_picks_exact_install_folders(
    games_root: Path, saves_root: Path, dest: Path
) -> None:
    readable = build_f1(games_root, saves_root)
    build_f5(games_root, saves_root)
    build_f8(saves_root)
    options = {"saves_root_override": saves_root}
    chosen = prepare_backup([games_root], dest, select_roots=[str(readable.root)], **options)
    assert [g.display_name for g in chosen.games] == ["Readable Game"]
    upper = str(readable.root).upper()
    assert len(prepare_backup([games_root], dest, select_roots=[upper], **options).games) == 1
    none = prepare_backup([games_root], dest, select_roots=[], **options)
    assert none.games == [] and none.skipped == []
    only_unlinked = prepare_backup(
        [games_root], dest, select_roots=[], include_unlinked=["OrphanTwo"], **options
    )
    assert [(g.kind, g.display_name) for g in only_unlinked.games] == [("unlinked", "OrphanTwo")]
    with pytest.raises(UsageError, match="not found by the scan"):
        prepare_backup([games_root], dest, select_roots=[str(games_root / "gone")], **options)


def test_progress_callback_precedes_each_game(
    games_root: Path, saves_root: Path, dest: Path
) -> None:
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    plan = prepare_backup([games_root], dest, saves_root_override=saves_root, include_unlinked=[])
    calls: list[tuple[int, int, str]] = []
    result = execute_backup(
        plan, Repo.open_or_create(plan.dest), progress=lambda *args: calls.append(args)
    )
    assert [r.status for r in result.results] == ["ok", "ok", "ok"]
    assert calls == [(i, 3, g.display_name) for i, g in enumerate(plan.games)]


def test_prepare_repository_checks_before_creating(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    for bad in (games_root / "library", saves_root / "library"):
        with pytest.raises(UsageError):
            backup.prepare_repository(bad, [games_root], saves_root_override=saves_root)
        assert not bad.exists()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("someone else's folder", encoding="utf-8")
    with pytest.raises(UsageError, match="not empty"):
        backup.prepare_repository(foreign, [games_root], saves_root_override=saves_root)
    created = backup.prepare_repository(
        tmp_path / "library", [games_root], saves_root_override=saves_root
    )
    assert (created.root / "catalog.json").is_file()
    again = backup.prepare_repository(created.root, [games_root], saves_root_override=saves_root)
    assert again.load_catalog()["games"] == []
