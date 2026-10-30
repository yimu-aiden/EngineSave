"""Association with known games (plan 6.6) and persisted user decisions (D12)."""

from __future__ import annotations

import shutil
from pathlib import Path

from enginesave.backup import BackupResult, execute_backup, prepare_backup
from enginesave.fsutil import iso_utc, utc_now
from enginesave.models import GameStatus, ScanReport, SourceStatus
from enginesave.repo import Repo
from enginesave.scan import ScanOptions, run_scan
from fixtures.builder import (
    GameSpec,
    build_f1,
    build_f5,
    build_f8,
    build_f18,
    build_game,
    options_stmts,
    simple_spec,
)


def backup(roots: list[Path], dest: Path, saves_root: Path, **options: object) -> BackupResult:
    plan = prepare_backup(roots, dest, saves_root_override=saves_root, **options)
    return execute_backup(plan, Repo.open_or_create(plan.dest))


def scan(roots: list[Path], saves_root: Path, repo: Path) -> ScanReport:
    return run_scan(ScanOptions(roots=roots, saves_root_override=saves_root, repo=repo))


def add_override(dest: Path, override: dict) -> None:
    repo = Repo.open(dest)
    catalog = repo.load_catalog()
    repo.set_override(catalog, override, lambda o: False)


def test_rule1_save_directory_follows_a_moved_install(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "repo"
    built = build_f1(games_root, saves_root)
    game_id = backup([games_root], dest, saves_root).results[0].game_id
    (game,) = scan([games_root], saves_root, dest).games
    assert (game.known_game_id, game.link_suggestions) == (game_id, [])

    moved = games_root / "Moved Install"
    shutil.move(str(built.root), str(moved))
    (game,) = scan([games_root], saves_root, dest).games
    assert game.known_game_id == game_id
    result = backup([games_root], dest, saves_root)
    assert result.results[0].game_id == game_id
    installs = [i["game_root"] for i in Repo.open(dest).load_game(game_id)["installs"]]
    assert installs == [str(built.root), str(moved)]


def test_suggestions_never_auto_link(games_root: Path, saves_root: Path, tmp_path: Path) -> None:
    dest = tmp_path / "repo"
    first = build_game(
        games_root, simple_spec("Saga", "SagaGame", "Saga-1", name="Saga"), saves_root
    )
    saga_id = backup([games_root], dest, saves_root).results[0].game_id
    shutil.rmtree(first.root)
    other = tmp_path / "other"
    build_game(other, simple_spec("Saga Remaster", "SagaRe", "Saga-2", name="Saga"), saves_root)
    build_game(other, simple_spec("Unrelated", "SagaGame", "Unrelated-3", name="Else"), saves_root)
    report = scan([other], saves_root, dest)
    by_name = {g.display_name: g for g in report.games}
    assert by_name["Saga"].known_game_id is None
    assert by_name["Saga"].link_suggestions == [{"game_id": saga_id, "reason": "config_name_match"}]
    assert by_name["Else"].known_game_id is None
    assert by_name["Else"].link_suggestions == [{"game_id": saga_id, "reason": "dir_name_match"}]


def test_link_root_override_wins(games_root: Path, saves_root: Path, tmp_path: Path) -> None:
    dest = tmp_path / "repo"
    alpha = build_game(games_root, simple_spec("Alpha", "Alpha", "Alpha-1"), saves_root)
    beta = build_game(games_root, simple_spec("Beta", "Beta", "Beta-1"), saves_root)
    results = {r.display_name: r.game_id for r in backup([games_root], dest, saves_root).results}
    override = {
        "game_id": results["Beta"],
        "type": "link_root",
        "game_root": str(alpha.root),
        "linked_at": iso_utc(utc_now()),
    }
    add_override(dest, override)
    games = {Path(g.game_root).name: g for g in scan([games_root], saves_root, dest).games}
    assert games["Alpha"].known_game_id == results["Beta"]
    assert games[beta.root.name].known_game_id == results["Beta"]


def test_confirm_source_promotes_candidate(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "repo"
    built = build_f5(games_root, saves_root)
    first = backup([games_root], dest, saves_root).results[0]
    manifest = Repo.open(dest).load_manifest(first.game_id, first.snapshot_id)
    assert [s["source_id"] for s in manifest["sources"]] == ["local"]
    add_override(
        dest,
        {
            "game_id": first.game_id,
            "type": "confirm_source",
            "source_id": "global",
            "path": str(built.global_dir),
            "confirmed_at": iso_utc(utc_now()),
        },
    )
    (game,) = scan([games_root], saves_root, dest).games
    source = game.source("global")
    assert source.status is SourceStatus.USER_CONFIRMED
    assert source.evidence[-1].method == "user_override"
    assert game.status is GameStatus.CONFIRMED, "D48 3: every candidate confirmed"
    second = backup([games_root], dest, saves_root).results[0]
    manifest = Repo.open(dest).load_manifest(second.game_id, second.snapshot_id)
    statuses = {s["source_id"]: s["source_status"] for s in manifest["sources"]}
    assert statuses == {"local": "CONFIRMED", "global": "USER_CONFIRMED"}


def test_confirmed_path_not_found_is_a_warning(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "repo"
    build_f1(games_root, saves_root)
    game_id = backup([games_root], dest, saves_root).results[0].game_id
    add_override(
        dest,
        {"game_id": game_id, "type": "confirm_source", "source_id": "x", "path": str(tmp_path)},
    )
    (game,) = scan([games_root], saves_root, dest).games
    assert any("was not found by this scan" in w for w in game.warnings)


def test_unlinked_records(games_root: Path, saves_root: Path, tmp_path: Path) -> None:
    dest = tmp_path / "repo"
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    results = backup([games_root], dest, saves_root, include_unlinked=["Orphan-1500000000"])
    orphan_id = next(r.game_id for r in results.results if r.kind == "unlinked")
    report = scan([games_root], saves_root, dest)
    known = {u.name: u.known_game_id for u in report.unlinked}
    assert known == {"Orphan-1500000000": orphan_id, "OrphanTwo": None}

    reinstall = tmp_path / "reinstall"
    build_game(reinstall, simple_spec("Orphan Game", "OrphanGame", "Orphan-1500000000"), None)
    (game,) = scan([reinstall], saves_root, dest).games
    assert game.known_game_id is None, "unlinked records are never merged automatically"
    assert game.link_suggestions == [
        {"game_id": orphan_id, "reason": "unlinked_save_directory_match"}
    ]


# ------------------------------------------------------------- a shared save directory (D48)


def link_roots(dest: Path) -> dict[str, str]:
    overrides = Repo.open(dest).load_catalog()["overrides"]
    return {o["game_root"]: o["game_id"] for o in overrides if o["type"] == "link_root"}


def test_two_games_sharing_a_folder_get_a_record_each(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D48 1 2: the two seasons are backed up into two records, both snapshots hold the shared
    folder, the object store keeps each content once, and each season's folder is linked to
    its record, so that the next scan tells them apart."""
    dest = tmp_path / "repo"
    one, two = build_f18(games_root, saves_root)
    result = backup([games_root], dest, saves_root)
    ids = [r.game_id for r in result.results]
    assert [r.status for r in result.results] == ["ok", "ok"] and len(set(ids)) == 2
    repo = Repo.open(dest)
    manifests = [repo.load_manifest(i, repo.latest_snapshot(i)) for i in ids]

    def shared(manifest: dict) -> list[tuple[str, str]]:
        files = manifest["files"]
        return sorted((f["path"], f["sha256"]) for f in files if f["source_id"] == "global")

    assert shared(manifests[0]) and shared(manifests[0]) == shared(manifests[1])
    digests = {f["sha256"] for m in manifests for f in m["files"]}
    objects = [p for p in (dest / "objects").rglob("*") if p.is_file()]
    assert len(objects) == len(digests), "one copy of each content"
    assert link_roots(dest) == {str(one.root): ids[0], str(two.root): ids[1]}
    first, second = scan([games_root], saves_root, dest).games
    assert (first.known_game_id, second.known_game_id) == (ids[0], ids[1])
    assert (first.link_suggestions, second.link_suggestions) == ([], [])


def test_the_record_one_game_has_is_not_taken_by_the_other(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D48 2: the season the library knows keeps its record; the other season with the same
    save directory is only offered it (shared_save_directory) and gets a record of its own
    when it is backed up, and from then on both folders are linked."""
    dest = tmp_path / "repo"
    one, two = build_f18(games_root, saves_root)
    (first,) = backup([games_root], dest, saves_root, select_roots=[one.root]).results
    assert link_roots(dest) == {}, "one record with that save directory: nothing to pin"
    season_one, season_two = scan([games_root], saves_root, dest).games
    assert season_one.known_game_id == first.game_id
    assert season_two.known_game_id is None
    assert season_two.link_suggestions == [
        {"game_id": first.game_id, "reason": "shared_save_directory"}
    ]
    again = backup([games_root], dest, saves_root).results
    assert again[0].game_id == first.game_id and again[1].game_id != first.game_id
    assert link_roots(dest) == {str(one.root): first.game_id, str(two.root): again[1].game_id}


def test_several_records_with_one_save_directory_only_give_suggestions(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D48 2: rule 1 links only to the one record with that save directory; with two such
    records a third folder is not linked, and both records are offered."""
    dest = tmp_path / "repo"
    build_f18(games_root, saves_root)
    ids = [r.game_id for r in backup([games_root], dest, saves_root).results]
    elsewhere = tmp_path / "elsewhere"
    stmts = options_stmts(
        name=None, version="S3", build_name="SharedSeasons", save_directory="shared_seasons"
    )
    build_game(elsewhere, GameSpec("Season Three", "SharedSeasons", stmts, "shared_seasons"), None)
    (game,) = scan([elsewhere], saves_root, dest).games
    assert game.known_game_id is None
    assert game.link_suggestions == [
        {"game_id": ids[0], "reason": "shared_save_directory"},
        {"game_id": ids[1], "reason": "shared_save_directory"},
    ]


def test_cleanup_keeps_a_folder_another_record_still_uses(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D48 1: cleaning up one season never removes the folder the other season still uses."""
    from enginesave.cleanup import GAME_ROOT, GLOBAL, PERMANENT, plan_cleanup

    dest = tmp_path / "repo"
    _one, two = build_f18(games_root, saves_root)
    ids = [r.game_id for r in backup([games_root], dest, saves_root).results]
    repo = Repo.open(dest)
    record = repo.resolve_game(repo.load_catalog(), ids[0])
    plan = plan_cleanup(
        repo,
        record,
        [(GAME_ROOT, None), (GLOBAL, None)],
        method=PERMANENT,
        saves_root_override=saves_root,
    )
    game, shared = plan.targets
    assert game.ok and not shared.ok
    assert any(str(two.root) in reason for reason in shared.reasons)


def test_suggestion_keys_drop_version_tails_only() -> None:
    """D66 5, M44 review: the version at the end (and the stage or platform after it) goes; a
    sequel's number, an episode and a word alone stay."""
    from enginesave.identity import suggestion_key

    for name in ("Hero v0.3", "Hero 1.2 beta", "Hero Ver.1.05", "Hero (v2)", "Hero demo"):
        assert suggestion_key(name) == "hero", name
    assert suggestion_key("PaperLantern-0.8.15r1-pc") == "paperlantern"
    for name in ("魔王物語ver1.02", "魔王物語（ver1.0）", "魔王物語Ver.1.05", "魔王物語 1.03"):
        assert suggestion_key(name) == "魔王物語", name
    kept = {
        "Hero 2": "hero2",
        "Hero Episode 3": "heroepisode3",
        "Project Alpha": "projectalpha",
        "Type R2": "typer2",
        "Big Win": "bigwin",
        "Hero RC": "herorc",
        "Hero PC": "heropc",
    }
    for name, key in kept.items():
        assert suggestion_key(name) == key, name
