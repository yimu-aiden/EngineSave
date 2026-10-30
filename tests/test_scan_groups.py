"""The scan page's four groups (0.9.0, D54) without a window: which CONFIRMED games have new
saves since their latest snapshot (new, changed and deleted files), the groups, the default
ticks and the notes."""

from __future__ import annotations

import os
from pathlib import Path

from enginesave.backup import execute_backup, prepare_backup
from enginesave.fsutil import path_key
from enginesave.gui import state
from enginesave.models import DetectedGame, GameStatus, ScanReport
from enginesave.repo import Repo
from enginesave.scan import ScanOptions, run_scan
from fixtures.builder import (
    build_f1,
    build_f3,
    build_f5,
    build_f19,
    build_f22,
    build_game,
    simple_spec,
)
from fixtures.fsprobe import needs_millisecond_times


def scan(games_root: Path, saves_root: Path, repo: Path | None) -> ScanReport:
    return run_scan(ScanOptions(roots=[games_root], saves_root_override=saves_root, repo=repo))


def back_up(games_root: Path, saves_root: Path, library: Path) -> None:
    plan = prepare_backup([games_root], library, saves_root_override=saves_root)
    execute_backup(plan, Repo.open_or_create(plan.dest))


def named(report: ScanReport, name: str) -> DetectedGame:
    return next(g for g in report.games if g.display_name == name)


def test_a_game_backed_up_without_new_saves_is_folded_away_and_not_ticked(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D54 ①③④: F1 backed up and untouched is 「已备份、无新存档」; a game never backed up is
    「有新存档可备份」 (未备份过); only the first group starts ticked."""
    build_f1(games_root, saves_root)
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    second = simple_spec("Second Game", "SecondGame", "SecondGame-1600000009")
    build_game(games_root, second, saves_root)
    report = scan(games_root, saves_root, library)
    changes = state.save_changes(report, library)
    readable, other = named(report, "Readable Game"), named(report, "Second Game")
    same = changes[state.game_key(readable)]
    assert (
        same.backed_up
        and not same.new_saves
        and (same.added, same.changed, same.removed)
        == (
            0,
            0,
            0,
        )
    )
    snapshot = Repo.open(library).latest_snapshot(readable.known_game_id)
    assert same.note() == f"无新存档，上次备份 {state.snapshot_time(snapshot)[5:10]}"
    assert changes[state.game_key(other)].note() == "未备份过"
    assert state.game_group(readable, changes) == state.GROUP_UNCHANGED
    assert state.game_group(other, changes) == state.GROUP_CHANGED
    selection = state.Selection()
    selection.load(report, state.unchanged_keys(changes))
    assert selection.games == {state.game_key(readable): False, state.game_key(other): True}
    notes = state.game_row(readable, selection, same).notes
    assert notes.startswith("无新存档，上次备份 ") and "已在备份库" not in notes
    assert state.groups_log(report.games, changes) == (
        "分组：有新存档可备份 1；已备份、无新存档 1；待确认 0；不支持 0"
    )


def test_new_changed_and_deleted_files_are_new_saves(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D54 ③: the three differences, file by file, in every source a backup copies (local
    and global with its sync/ mirror)."""
    built = build_f1(games_root, saves_root)
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    local = sorted(p for p in built.local_dir.rglob("*") if p.is_file())
    (built.local_dir / "9-1-LT1.save").write_bytes(b"a new save")  # new
    local[0].write_bytes(local[0].read_bytes() + b"!")  # changed: size
    info = local[1].stat()
    os.utime(local[1], ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))  # changed: time
    global_files = sorted(p for p in built.global_dir.rglob("*") if p.is_file())
    global_files[0].unlink()  # deleted
    report = scan(games_root, saves_root, library)
    game = named(report, "Readable Game")
    change = state.save_changes(report, library)[state.game_key(game)]
    assert (change.added, change.changed, change.removed) == (1, 2, 1)
    assert change.new_saves and change.note() == "有新存档：新增 1、改动 2、删去 1"
    assert state.game_group(game, {state.game_key(game): change}) == state.GROUP_CHANGED


@needs_millisecond_times
def test_a_size_alone_or_a_millisecond_alone_is_a_change(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """Each half of "changed" on its own: same time and another size; same size and a time
    one millisecond later."""
    built = build_f1(games_root, saves_root)
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    first, second = sorted(p for p in built.local_dir.rglob("*") if p.is_file())[:2]
    info = first.stat()
    first.write_bytes(first.read_bytes() + b"!")
    os.utime(first, ns=(info.st_atime_ns, info.st_mtime_ns))  # the same time, a new size
    info = second.stat()
    os.utime(second, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))  # +1 ms
    report = scan(games_root, saves_root, library)
    change = state.save_changes(report, library)[state.game_key(report.games[0])]
    assert (change.added, change.changed, change.removed) == (0, 2, 0)


def test_a_save_dated_before_1970_is_compared_like_any_other(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """Windows refuses to format such a time; the comparison computes it (M33 review)."""
    built = build_f1(games_root, saves_root)
    old = sorted(p for p in built.local_dir.rglob("*") if p.is_file())[0]
    os.utime(old, ns=(-50 * 365 * 86400 * 10**9, -50 * 365 * 86400 * 10**9))
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    report = scan(games_root, saves_root, library)
    change = state.save_changes(report, library)[state.game_key(report.games[0])]
    assert change.backed_up and not change.new_saves, change
    os.utime(old, ns=(-40 * 365 * 86400 * 10**9, -40 * 365 * 86400 * 10**9))
    change = state.save_changes(report, library)[state.game_key(report.games[0])]
    assert (change.added, change.changed, change.removed) == (0, 1, 0)


@needs_millisecond_times
def test_a_time_within_the_manifests_precision_is_no_change(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """The manifest keeps milliseconds: a file touched by less than that is the same file."""
    built = build_f1(games_root, saves_root)
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    first = sorted(p for p in built.local_dir.rglob("*") if p.is_file())[0]
    info = first.stat()
    whole = info.st_mtime_ns - info.st_mtime_ns % 1_000_000
    os.utime(first, ns=(info.st_atime_ns, whole + 999))  # same millisecond
    report = scan(games_root, saves_root, library)
    change = state.save_changes(report, library)[state.game_key(report.games[0])]
    assert not change.new_saves, change


def test_pending_and_unsupported_games_have_their_own_groups(
    games_root: Path, saves_root: Path
) -> None:
    """D54 ①: CANDIDATE (F5), CONFLICT (F19), NOT_FOUND (F3) are 「待确认」, UNSUPPORTED
    (Emuera, F22) is 「不支持」; no comparison is made for them, and they start unticked."""
    build_f3(games_root, saves_root)
    build_f5(games_root, saves_root)
    build_f19(games_root, saves_root)
    build_f22(games_root)
    report = scan(games_root, saves_root, None)
    changes = state.save_changes(report, None)
    assert changes == {}, "only CONFIRMED games are compared"
    groups = {g.status: state.game_group(g, changes) for g in report.games}
    assert groups == {
        GameStatus.NOT_FOUND: state.GROUP_PENDING,
        GameStatus.CANDIDATE: state.GROUP_PENDING,
        GameStatus.CONFLICT: state.GROUP_PENDING,
        GameStatus.UNSUPPORTED: state.GROUP_UNSUPPORTED,
    }
    selection = state.Selection()
    selection.load(report, state.unchanged_keys(changes))
    assert not any(selection.games.values())
    assert [state.SCAN_GROUP_NAMES[g] for g in state.SCAN_GROUPS] == [
        "有新存档可备份",
        "已备份、无新存档",
        "待确认",
        "不支持",
    ]
    assert [state.GROUP_OPEN_BY_DEFAULT[g] for g in state.SCAN_GROUPS] == [
        True,
        False,
        True,
        False,
    ]
    assert state.group_title(state.GROUP_PENDING, 3, 3) == "待确认（3）"
    assert state.group_title(state.GROUP_PENDING, 1, 3, "x") == "待确认（1/3）"


def test_a_snapshot_that_cannot_be_read_counts_as_new_saves(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """Conservative: a damaged manifest cannot say "nothing new", so the game stays in the
    first group, ticked, with a note."""
    build_f1(games_root, saves_root)
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    repo = Repo.open(library)
    report = scan(games_root, saves_root, library)
    game = report.games[0]
    snapshot = repo.latest_snapshot(game.known_game_id)
    manifest = repo.snapshot_dir(game.known_game_id, snapshot) / "manifest.json"
    manifest.write_text("{ damaged", encoding="utf-8")
    change = state.save_changes(report, library)[state.game_key(game)]
    assert change.backed_up and change.error and change.new_saves
    assert change.note() == "无法与上次备份比对"
    assert state.unchanged_keys({state.game_key(game): change}) == set()


def test_a_manifest_of_the_wrong_shape_counts_as_new_saves(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """Valid JSON with nonsense entries: the comparison says so, the scan job goes on."""
    import json

    build_f1(games_root, saves_root)
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    repo = Repo.open(library)
    report = scan(games_root, saves_root, library)
    game = report.games[0]
    path = repo.snapshot_dir(game.known_game_id, repo.latest_snapshot(game.known_game_id))
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"] = [1, "two", {"size": "many"}]
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    change = state.save_changes(report, library)[state.game_key(game)]
    assert change.error and change.new_saves


def test_an_anonymised_snapshot_is_matched_by_kind_and_place(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """A snapshot imported from an anonymised export has no real ``origin_path``; its
    sources are matched by kind and relative root, so identical saves are not "new"."""
    import json

    build_f1(games_root, saves_root)
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    repo = Repo.open(library)
    report = scan(games_root, saves_root, library)
    game = report.games[0]
    path = repo.snapshot_dir(game.known_game_id, repo.latest_snapshot(game.known_game_id))
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    for source in manifest["sources"]:
        source["origin_path"] = f"<anonymised>/{source['relative_root']}"
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    change = state.save_changes(report, library)[state.game_key(game)]
    assert change.backed_up and not change.new_saves, change


def test_a_deleted_record_is_not_backed_up_any_more(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D49 then D54: once its record is deleted, the game is 「未备份过」 again."""
    from enginesave.removal import execute_remove, plan_remove

    build_f1(games_root, saves_root)
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    report = scan(games_root, saves_root, library)
    game = report.games[0]
    before = state.save_changes(report, library)
    repo = Repo.open(library)
    execute_remove(plan_remove(repo, repo.load_catalog(), [(game.known_game_id, None)]))
    after = state.save_changes(report, library)
    assert after[state.game_key(game)].note() == "未备份过"
    assert state.newly_changed(before, after) == {state.game_key(game)}
    assert state.newly_changed(after, before) == set(), "ticks are only ever added"


def test_a_source_the_snapshot_does_not_have_is_all_new(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """The game was backed up before it had global saves: every global file is new."""
    built = build_f1(games_root, saves_root)
    moved = tmp_path / "away"
    built.global_dir.rename(moved)
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    moved.rename(built.global_dir)
    report = scan(games_root, saves_root, library)
    change = state.save_changes(report, library)[state.game_key(report.games[0])]
    count = sum(1 for p in built.global_dir.rglob("*") if p.is_file())
    assert (change.added, change.changed, change.removed) == (count, 0, 0)


def test_a_backup_names_the_records_of_the_games_it_stored(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """After 「备份已勾选项」 the window compares again (D54); a game backed up for the first
    time has no record in the scan yet, the backup's result names it."""
    build_f1(games_root, saves_root)
    report = scan(games_root, saves_root, None)
    library = tmp_path / "library"
    plan = prepare_backup([games_root], library, saves_root_override=saves_root)
    result = execute_backup(plan, Repo.open_or_create(plan.dest))
    ids = state.backed_up_ids(plan, result)
    game = report.games[0]
    assert ids == {path_key(game.game_root): result.results[0].game_id}
    assert state.save_changes(report, library)[state.game_key(game)].note() == "未备份过"
    after = state.save_changes(report, library, game_ids=ids)[state.game_key(game)]
    assert after.backed_up and not after.new_saves
    selection = state.Selection()
    selection.load(report)
    selection.replace(game, game, unchanged=True)
    assert selection.games[state.game_key(game)], "a tick stays when the game was ticked"
    selection.games[state.game_key(game)] = False
    selection.replace(game, game, unchanged=True)  # 「确认 / 关联…」, no new saves
    assert not selection.games[state.game_key(game)], "folded away, not ticked"
    selection.replace(game, game, unchanged=False)
    assert selection.games[state.game_key(game)], "new saves: ticked"


# ------------------------------------------------------------------- 0.10.0 (D61, D62 ④)


def test_candidate_sources_are_compared_only_when_backups_include_them(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D62 ④: the comparison looks at what a backup would copy -- a CANDIDATE source only
    with 「包含 CANDIDATE」 on (here one the snapshot does not have: all its files new)."""
    import dataclasses

    from enginesave.models import SaveSource, SourceKind, SourceStatus

    build_f1(games_root, saves_root)
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    report = scan(games_root, saves_root, library)
    game = report.games[0]
    extra = tmp_path / "LooksLikeIt"
    extra.mkdir()
    (extra / "1-1-LT1.save").write_bytes(b"maybe theirs")
    local = next(s for s in game.sources if s.kind is SourceKind.LOCAL)
    candidate = dataclasses.replace(
        local,
        source_id="candidate-1",
        kind=SourceKind.GLOBAL,
        path=str(extra),
        relative_root=extra.name,
        status=SourceStatus.CANDIDATE,
        exists=True,
    )
    assert isinstance(candidate, SaveSource)
    game.sources.append(candidate)
    without = state.save_changes(report, library)[state.game_key(game)]
    with_candidates = state.save_changes(report, library, include_candidates=True)
    assert not without.new_saves
    assert with_candidates[state.game_key(game)].added == 1


def test_the_comparison_stops_between_games(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D61 ③: 「取消」 at the end of a scan stops between the games being compared."""
    import pytest

    from enginesave.errors import Stopped

    build_f1(games_root, saves_root)
    build_game(games_root, simple_spec("Second Game", "SecondGame", "SecondGame-16"), saves_root)
    library = tmp_path / "library"
    back_up(games_root, saves_root, library)
    report = scan(games_root, saves_root, library)
    asked: list[bool] = []

    def second_time() -> bool:
        asked.append(True)
        return len(asked) > 1

    with pytest.raises(Stopped):
        state.save_changes(report, library, should_stop=second_time)
    assert len(asked) == 2, "asked before each game"
    assert len(state.save_changes(report, library, should_stop=lambda: False)) == 2


def test_group_titles_say_what_a_closed_group_holds() -> None:
    """D61 ②⑤: ticks inside a closed group, and the engines of the unsupported group (the
    most common first, three at most); a search shows matches/total as before."""
    engines = ["emuera"] * 3 + ["unity"] * 2 + ["godot", "rpgmaker_mv_mz", "rpgmaker_xp_vx_vxace"]
    title = state.group_title(state.GROUP_UNSUPPORTED, 8, 8, engines=engines)
    assert title == "不支持（8：Emuera 3、Unity 2、RPG Maker 2…）", "one short name per family"
    assert state.group_title(state.GROUP_UNSUPPORTED, 2, 8, "x", engines=engines) == "不支持（2/8）"
    folded = state.group_title(state.GROUP_UNCHANGED, 5, 5, folded_ticks=2)
    assert folded == "已备份、无新存档（5）（已勾选 2）"
    assert state.group_title(state.GROUP_CHANGED, 3, 3) == "有新存档可备份（3）"
    assert state.checked_text(3, 0, folded=2) == "已勾选 3 款游戏（其中 2 款在收起的组里）"
    assert state.checked_text(3, 1, hidden=1, folded=1) == (
        "已勾选 3 款游戏（其中 1 款被筛选隐藏、1 款在收起的组里）、1 个孤立目录"
    )
