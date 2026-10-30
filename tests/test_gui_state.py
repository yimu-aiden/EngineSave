"""GUI view model, settings, logging and the task runner, without Tk (plan section 17 M10)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from enginesave.appdirs import documents_dir
from enginesave.backup import BackupResult, GameResult, execute_backup, prepare_backup
from enginesave.cleanup import CleanupPlan, CleanupResult, CleanupTarget, RecycleBin
from enginesave.export import BatchResult, ExportResult, ImportPlan
from enginesave.fsutil import path_key
from enginesave.gui import state
from enginesave.gui.logging_setup import (
    BACKUP_COUNT,
    LOG_NAME,
    MAX_BYTES,
    close_logging,
    setup_logging,
)
from enginesave.gui.settings import (
    GuiSettings,
    app_dir,
    blank_settings,
    load_settings,
    log_dir,
    save_settings,
    settings_path,
)
from enginesave.gui.workers import TaskRunner
from enginesave.models import GameStatus, ScanReport, SourceStatus
from enginesave.repo import Repo
from enginesave.restore import RestorePlan, RestoreTarget
from enginesave.scan import ScanOptions, run_scan
from fixtures.builder import build_f1, build_f3, build_f5, build_f7, build_f8, build_f19


@pytest.fixture
def report(games_root: Path, saves_root: Path) -> ScanReport:
    build_f1(games_root, saves_root)  # CONFIRMED
    build_f3(games_root, saves_root)  # NOT_FOUND
    build_f19(games_root, saves_root)  # CONFLICT
    build_f5(games_root, saves_root)  # CANDIDATE
    build_f7(games_root)  # UNSUPPORTED
    build_f8(saves_root)  # two orphaned folders
    return run_scan(ScanOptions(roots=[games_root], saves_root_override=saves_root))


def by_status(report: ScanReport, status: GameStatus):
    return next(g for g in report.games if g.status is status)


def test_the_window_loads_no_library_code_until_it_is_needed() -> None:
    """D38: importing the window, its views and its dialogs loads neither the scanner nor
    the backup, cleanup, export, restore or verify code; those load when an action runs."""
    code = (
        "import sys\n"
        "import enginesave.gui.app, enginesave.gui.views.library_view\n"
        "import enginesave.gui.views.scan_view, enginesave.gui.views.dialogs\n"
        "print(','.join(m for m in sys.modules if m.startswith('enginesave.')))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    loaded = {m.split(".")[1] for m in proc.stdout.strip().split(",")}
    heavy = {"scan", "discovery", "backup", "store", "cleanup", "export", "restore", "verify"}
    assert not loaded & heavy, sorted(loaded & heavy)


def test_the_cleanup_names_the_window_writes_out_are_the_library_s() -> None:
    from enginesave import cleanup

    assert (state.GAME_ROOT, state.GLOBAL, state.UNLINKED) == cleanup.KINDS
    assert (state.RECYCLE, state.PERMANENT) == (cleanup.RECYCLE, cleanup.PERMANENT)
    assert set(state.CLEANUP_LABELS) == set(cleanup.KINDS)


# ------------------------------------------------------------------------------ selection


def test_default_checks_follow_the_status(report: ScanReport) -> None:
    statuses = {g.status for g in report.games}
    assert statuses >= {
        GameStatus.CONFIRMED,
        GameStatus.CANDIDATE,
        GameStatus.CONFLICT,
        GameStatus.NOT_FOUND,
        GameStatus.UNSUPPORTED,
    }
    selection = state.Selection()
    selection.load(report)
    checked = {g.status for g in report.games if selection.games[state.game_key(g)]}
    assert checked == {GameStatus.CONFIRMED}
    assert not any(selection.unlinked.values()) and len(selection.unlinked) == 2
    unsupported = by_status(report, GameStatus.UNSUPPORTED)
    assert selection.toggle_game(state.game_key(unsupported)) is False, "not checkable"
    assert state.game_row(unsupported, selection).check == ""


def test_toggle_one_or_everything(report: ScanReport) -> None:
    """D33 ③: one button; 全选 also when only some rows are ticked, 全不选 once all are.
    Since 0.7.0 (D42) it ticks the games only; the orphaned folders have their own button."""
    selection = state.Selection()
    selection.load(report)
    candidate = state.game_key(by_status(report, GameStatus.CANDIDATE))
    unsupported = state.game_key(by_status(report, GameStatus.UNSUPPORTED))
    assert selection.toggle_game(candidate) is True
    assert selection.toggle_unlinked("orphantwo") is True
    assert selection.counts() == (2, 1)
    assert not selection.all_checked()
    assert state.toggle_all_text(selection.all_checked()) == "全选", "some ticked: 全选"
    selection.toggle_all()  # 全选: every game that can be ticked, no orphaned folder (D42)
    checkable = sum(1 for g in report.games if g.status is not GameStatus.UNSUPPORTED)
    assert selection.counts() == (checkable, 1) and not selection.games[unsupported]
    assert selection.all_checked() and state.toggle_all_text(True) == "全不选"
    selection.toggle_all()  # 全不选: the games
    assert selection.counts() == (0, 1) and state.toggle_all_text(selection.all_checked()) == "全选"
    selection.toggle_all_unlinked()  # the orphaned-folders dialog's own button
    assert selection.counts() == (0, 2) and selection.all_unlinked_checked()
    selection.toggle_all_unlinked()
    assert selection.counts() == (0, 0)
    assert selection.toggle_game("no such game") is False
    empty = state.Selection()
    assert not empty.all_checked(), "nothing to tick: the button says 全选 and does nothing"


def test_the_tick_button_works_on_the_games_the_search_shows(report: ScanReport) -> None:
    """D46: while the search box hides games, 全选 / 全不选 work on the games shown and
    leave the hidden ticks as they are."""
    selection = state.Selection()
    selection.load(report)
    confirmed = state.game_key(by_status(report, GameStatus.CONFIRMED))
    candidate = state.game_key(by_status(report, GameStatus.CANDIDATE))
    unsupported = state.game_key(by_status(report, GameStatus.UNSUPPORTED))
    assert selection.games[confirmed] and not selection.games[candidate]
    shown = {candidate, unsupported}
    assert selection.tickable(shown) == [candidate] and not selection.all_checked(shown)
    selection.toggle_all(shown)  # 全选: the candidate; the unsupported game cannot be ticked
    assert selection.games[candidate] and not selection.games[unsupported]
    assert selection.all_checked(shown)
    selection.toggle_all(shown)  # 全不选: the games shown only
    assert not selection.games[candidate] and selection.games[confirmed]
    assert selection.tickable(set()) == [] and not selection.all_checked(set())


def test_library_actions_work_on_ticks_else_the_selected_row() -> None:
    """D33 ②: ticked records first, then the selected one; nothing makes the button grey."""
    assert state.action_targets(["a", "b"], "c") == ["a", "b"]
    assert state.action_targets([], "c") == ["c"]
    assert state.action_targets([], None) == []


def test_backup_options_translate_the_check_boxes(report: ScanReport) -> None:
    selection = state.Selection()
    selection.load(report)
    settings = GuiSettings(include_candidates=True, include_tokens=True)
    options = state.backup_options(selection, report, settings)
    assert options == {
        "select_roots": [by_status(report, GameStatus.CONFIRMED).game_root],
        "include_candidates": True,
        "include_unlinked": None,
        "include_tokens": True,
        "excludes": [],  # 0.10.0 (D63)
    }, "no orphaned folder checked must not become [] (which means every folder)"
    selection.toggle_unlinked("orphan-1500000000")
    options = state.backup_options(selection, report, settings)
    assert options["include_unlinked"] == ["Orphan-1500000000"], (
        "S-131: an orphaned folder ticked in its dialog goes with the games"
    )
    assert options["select_roots"] == [by_status(report, GameStatus.CONFIRMED).game_root]
    selection.toggle_unlinked("orphan-1500000000")
    assert state.backup_options(selection, report, settings)["include_unlinked"] is None


def test_backup_options_drive_prepare_backup(
    report: ScanReport, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    selection = state.Selection()
    selection.load(report)
    selection.toggle_unlinked("orphantwo")
    options = state.backup_options(selection, report, GuiSettings())
    plan = prepare_backup(
        [games_root], tmp_path / "repo", saves_root_override=saves_root, **options
    )
    assert [(g.kind, g.display_name) for g in plan.games] == [
        ("game", "Readable Game"),
        ("unlinked", "OrphanTwo"),
    ], "S-131: the ticked game and the orphaned folder ticked in its dialog, together"
    assert state.plan_totals(plan)[0] == 2
    warnings = state.plan_warnings(plan)
    assert warnings[0].startswith("将新建备份库")
    result = execute_backup(plan, Repo.open_or_create(plan.dest))
    counts = state.result_counts(result)
    assert (counts["ok"], counts["failed"]) == (2, 0)
    assert state.storage_hint(result) is None


# ---------------------------------------------------------------------------------- rows


def test_rows_and_details(report: ScanReport) -> None:
    selection = state.Selection()
    selection.load(report)
    confirmed = by_status(report, GameStatus.CONFIRMED)
    row = state.game_row(confirmed, selection)
    assert row.check == state.CHECKED and row.status == "CONFIRMED"
    assert (row.name, row.version, row.engine) == ("Readable Game", "1.0", "renpy 8.4.1.25072401")
    assert row.local.startswith("4 个文件") and row.global_.startswith("8 个文件")
    assert len(row.values()) == len(row.__dataclass_fields__) - 1
    candidate = state.game_row(by_status(report, GameStatus.CANDIDATE), selection)
    assert candidate.check == state.UNCHECKED and "待确认" in candidate.notes
    not_found = state.game_row(by_status(report, GameStatus.NOT_FOUND), selection)
    assert "EXPECTED_ABSENT" in not_found.local + not_found.global_
    unsupported = state.game_row(by_status(report, GameStatus.UNSUPPORTED), selection)
    assert unsupported.notes.startswith("不支持")
    orphan = next(u for u in report.unlinked if u.name == "Orphan-1500000000")
    unlinked = state.unlinked_row(orphan, selection)
    assert (unlinked.check, unlinked.files, unlinked.version) == (state.UNCHECKED, "2", "0.7.2")
    assert len(unlinked.modified) == len("2026-01-01 00:00")
    details = state.game_details(confirmed)
    for text in ("ReadableGame-1600000001", "存档来源", "证据", confirmed.game_root):
        assert text in details
    lines = state.game_detail_lines(confirmed)
    assert "\n".join(text for text, _ in lines) == details
    linked = [path for _text, path in lines if path]
    installs = [i["game_root"] for i in confirmed.installs]
    sources = [s.path for s in confirmed.sources]
    assert linked == [confirmed.game_root, *installs, *sources], "D36: 打开 beside each path"
    summary = state.scan_summary(report)
    assert "1 CONFIRMED" in summary and "孤立目录" not in summary, "D42: games only"
    assert state.unlinked_link_text(2) == "另有 2 个孤立存档目录…"
    assert state.unlinked_link_text(0) is None
    empty = ScanReport(1, "x", "2026-10-24T00:00:00Z", [], "C:\\RenPy")
    assert "没有找到" in state.scan_summary(empty)


def test_folders_a_right_click_offers_on_the_scan_page(report: ScanReport) -> None:
    """D36: a game row opens its folder and its save folders; absent ones are greyed."""
    confirmed = by_status(report, GameStatus.CONFIRMED)
    folders = state.game_folders(confirmed)
    assert [f.label for f in folders] == ["游戏目录", "全局存档目录", "本地存档目录"]
    assert all(f.exists for f in folders) and folders[0].path == Path(confirmed.game_root)
    assert folders[0].open_label == "打开游戏目录"
    absent = [
        f for f in state.game_folders(by_status(report, GameStatus.NOT_FOUND)) if not f.exists
    ]
    assert absent and all(f.open_label.endswith("（不存在）") for f in absent)
    orphan = next(u for u in report.unlinked if u.name == "OrphanTwo")
    (entry,) = state.unlinked_folders(orphan)
    assert (entry.label, entry.path, entry.exists) == ("孤立存档目录", Path(orphan.path), True)


def test_folders_a_right_click_offers_on_the_library_page(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D36: the record's folder in the library first, every install (greyed when it is not
    there, D51), the global folder resolved like the cleanup resolves it."""
    built = build_f1(games_root, saves_root)
    build_f8(saves_root)
    plan = prepare_backup(
        [games_root], tmp_path / "repo", saves_root_override=saves_root, include_unlinked=[]
    )
    repo = Repo.open_or_create(plan.dest)
    execute_backup(plan, repo)
    docs = repo.known_games(repo.load_catalog())
    game = next(d for d in docs if d.get("kind", "game") == "game")
    folders = state.record_folders(repo, game, saves_root_override=saves_root)
    assert [f.label for f in folders] == ["备份记录文件夹", "游戏目录", "全局存档目录"]
    assert folders[0].path == repo.game_dir(game["game_id"]) and all(f.exists for f in folders)
    assert path_key(folders[1].path) == path_key(built.root)
    assert path_key(folders[2].path) == path_key(built.global_dir)
    # D51 ③: a game directory that is not there is greyed 「（不存在）」, cleaned up or not; one
    # that is there again (reinstalled after a cleanup) opens, as 本体存在 counts it (D43)
    game["installs"][0]["removed_at"] = "2026-10-24T00:00:00Z"  # as after a cleanup
    again = state.record_folders(repo, game, saves_root_override=saves_root)[1]
    assert again.exists and again.open_label == "打开游戏目录"
    game["installs"][0]["game_root"] = str(tmp_path / "cleaned away")
    cleaned = state.record_folders(repo, game, saves_root_override=saves_root)[1]
    assert not cleaned.exists and cleaned.open_label == "打开游戏目录（不存在）"
    orphan = next(d for d in docs if d.get("display_name") == "OrphanTwo")
    folders = state.record_folders(repo, orphan, saves_root_override=saves_root)
    assert [f.label for f in folders] == ["备份记录文件夹", "孤立存档目录"]
    assert path_key(folders[1].path) == path_key(saves_root / "OrphanTwo")


def test_skipped_folders_and_scan_time_in_one_line(tmp_path: Path) -> None:
    """D34: the log gets one line per scan; the details list every folder with 「打开」."""
    skipped = [{"path": f"D:\\Games\\deep{i}", "reason": "depth_limit"} for i in range(4)]
    skipped.append({"path": "D:\\Games\\link", "reason": "symlink"})
    text = state.skip_summary(skipped)
    assert text == (
        "跳过 5 个目录：depth_limit 4、symlink 1；"
        "示例：D:\\Games\\deep0；D:\\Games\\deep1；D:\\Games\\deep2（前 3 个）"
    )
    assert state.skip_summary([]) is None
    timing = {
        "total_seconds": 75.25,
        "roots": [{"root": "D:\\Games", "seconds": 70.0}, {"root": "E:\\More", "seconds": 5.0}],
        "slowest": [{"path": "D:\\Games\\Starlit\\resources", "seconds": 42.5}],
    }
    assert state.scan_time_text(timing) == "扫描用时 75.2 秒（D:\\Games 70.0 秒；E:\\More 5.0 秒）"
    assert state.scan_time_text({}) is None
    report = ScanReport(1, "x", "t", [], "C:\\RenPy", skipped=skipped, timing=timing)
    lines = state.skipped_detail_lines(report)
    assert lines[0] == ("depth_limit（4 个）", None) and lines[1] == (
        "  D:\\Games\\deep0",
        "D:\\Games\\deep0",
    )
    assert ("symlink（1 个）", None) in lines
    assert lines[-1] == (
        "  42.5 秒  D:\\Games\\Starlit\\resources",
        "D:\\Games\\Starlit\\resources",
    )


def test_time_formatting() -> None:
    assert state.local_time("") == ""
    assert state.local_time("not a time") == "not a time"
    assert state.local_time("2026-10-24T01:27:55.123Z")[:4] == "2026"
    assert state.snapshot_time("20261024T012755Z").count(":") == 2
    assert state.snapshot_time("20261024T012755Z-2").endswith("(-2)")
    assert state.snapshot_time("odd") == "odd"


def test_library_rows_and_option_helpers(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    built = build_f1(games_root, saves_root)
    for _ in range(2):
        plan = prepare_backup([games_root], tmp_path / "repo", saves_root_override=saves_root)
        execute_backup(plan, Repo.open_or_create(plan.dest))
    repo = Repo.open(tmp_path / "repo")
    (record,) = state.record_rows(repo, repo.load_catalog())
    assert (record.name, record.kind, record.snapshots) == ("Readable Game", "game", 2)
    rows = state.snapshot_rows(repo, record.game_id)
    assert [r.snapshot_id for r in rows] == list(reversed(repo.snapshot_ids(record.game_id)))
    assert (rows[0].reason, rows[0].status, rows[0].files, rows[0].storage) == (
        "manual",
        "complete",
        "12",
        "hardlink",
    )
    default = built.root
    options = state.restore_options("SID", str(default).upper(), default, ["local"], None)
    assert options["game_root"] is None, "the pre-filled default leaves the choice to the library"
    other = state.restore_options("SID", str(tmp_path), default, ["local", "global"], saves_root)
    assert other == {
        "snapshot": "SID",
        "game_root": tmp_path,
        "sources": ["local", "global"],
        "saves_root_override": saves_root,
    }
    assert state.restore_options("SID", "  ", None, [], None)["game_root"] is None
    target = state.export_target(str(tmp_path), "Readable Game", "20261024T012755Z")
    assert target == tmp_path / "EngineSave-readablegame-20261024T012755Z.zip"
    assert state.export_target("", "X", "20261024T012755Z").parent == documents_dir()
    settings = GuiSettings(scan_roots=[str(games_root)])
    assert state.export_forbidden_roots(settings) == [games_root]
    assert state.export_folder(settings) == documents_dir(), "the first time: Documents"
    settings.last_export_dir = str(tmp_path)
    assert state.export_folder(settings) == tmp_path, "then the last export folder"


def test_documents_folder_is_an_absolute_path() -> None:
    assert documents_dir().is_absolute()


# ------------------------------------------------------------------------------ settings


def test_settings_round_trip_and_damage(tmp_path: Path) -> None:
    env = {"APPDATA": str(tmp_path / "Roaming")}
    path = settings_path(env)
    assert path == tmp_path / "Roaming" / "EngineSave" / "gui.json"
    assert log_dir(env) == app_dir(env) / "logs"
    settings, note = load_settings(path)
    assert settings == GuiSettings() and note is None
    assert (settings.cleanup_method, settings.export_overwrite) == ("recycle", False), "D32"
    settings.scan_roots = ["E:\\Games"]
    settings.repo_path = "E:\\Backup"
    settings.saves_root_override = " E:\\fake "
    settings.include_tokens = True
    settings.cleanup_method = "permanent"
    settings.export_overwrite = True
    settings.last_import_dir = "E:\\in"
    save_settings(settings, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format_version"] == 1 and data["scan_roots"] == ["E:\\Games"]
    loaded, note = load_settings(path)
    assert loaded == settings and note is None
    assert loaded.override_path == Path("E:\\fake")
    assert GuiSettings().override_path is None
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["cleanup_method"]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_settings(path)[0].cleanup_method == "recycle", "a 0.4.0 file gets the defaults"
    path.write_text(json.dumps({**data, "auto_scan": True}), encoding="utf-8")
    loaded, note = load_settings(path)  # 0.5.0's setting, withdrawn in 0.6.0: ignored
    assert note is None and loaded.repo_path == "E:\\Backup" and "auto_scan" not in vars(loaded)
    for damaged in ("{not json", "[]"):  # not a settings file at all: the defaults
        path.write_text(damaged, encoding="utf-8")
        loaded, note = load_settings(path)
        assert loaded == GuiSettings() and note is not None and "损坏" in note
    # 1.0.0 (D70 4): one field wrong -- only that field gets its default, the rest is kept
    for field, bad in (("scan_roots", "E:\\Games"), ("cleanup_method", "shred")):
        path.write_text(json.dumps({**data, field: bad}), encoding="utf-8")
        loaded, note = load_settings(path)
        assert getattr(loaded, field) == getattr(GuiSettings(), field)
        assert loaded.repo_path == "E:\\Backup" and note is not None and field in note


def test_blank_settings_are_written_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / "gui.json"
    used = GuiSettings(
        scan_roots=["E:\\Test"],
        repo_path="E:\\library",
        saves_root_override="E:\\fake-appdata",
        include_candidates=True,
        include_tokens=True,
        window_geometry="1280x820+26+26",
        last_export_dir="E:\\exports",
        cleanup_method="permanent",
        export_overwrite=True,
        last_import_dir="E:\\imports",
        mirror_folder="F:\\mirror",
        auto_mirror=False,
        mirror_verify=False,
        scan_excludes=["E:\\Novels"],
    )
    save_settings(blank_settings(used), path)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "format_version": 1,
        "scan_roots": [],
        "repo_path": "",
        "saves_root_override": None,
        "include_candidates": False,
        "include_tokens": False,
        "window_geometry": "1280x820+26+26",
        "last_export_dir": "",
        "cleanup_method": "recycle",
        "export_overwrite": False,
        "last_import_dir": "",
        "mirror_folder": "",  # 0.9.0 (D57 4): not carried over either
        "auto_mirror": True,  # 0.10.0 (D64): the defaults again
        "mirror_verify": True,
        "scan_excludes": [],  # 0.10.0 (D63): not carried over
        "language": "auto",  # 0.11.0 (D68 2): Windows' display language again
    }
    loaded, note = load_settings(path)
    assert note is None and loaded == blank_settings(used) and loaded.override_path is None
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "saves_root_override": ""}), encoding="utf-8")
    assert load_settings(path)[0].saves_root_override is None, "0.3.0 wrote an empty string"


def test_override_notice_and_first_run_guidance() -> None:
    blank = blank_settings()
    assert state.override_notice(blank, {}) is None
    assert state.override_notice(GuiSettings(saves_root_override="  "), {}) is None
    notice = state.override_notice(GuiSettings(saves_root_override="E:\\fake"), {})
    assert notice == state.OverrideNotice(
        "全局存档根目录已改为：E:\\fake（仅用于测试）", clearable=True
    )
    from_env = state.override_notice(blank, {"ENGINESAVE_RENPY_SAVES_ROOT": "E:\\env"})
    assert from_env is not None and not from_env.clearable and "E:\\env" in from_env.text
    first = state.guidance(blank)
    assert first is not None and first.startswith("首次使用，按这六步来：")
    assert len(first.splitlines()) == 7 and "→ 1." in first and "✔" not in first
    second = state.guidance(GuiSettings(repo_path="E:\\library"))
    assert "✔ 1." in second and "→ 2." in second
    ready = GuiSettings(repo_path="E:\\library", scan_roots=["E:\\Games"])
    assert state.guidance(ready) is None, "nothing to say before the first scan"


def test_banner_after_a_scan_and_next_steps_after_a_backup(report: ScanReport) -> None:
    ready = GuiSettings(repo_path="E:\\library", scan_roots=["E:\\Games"])
    assert state.guidance(ready, report, (2, 1)) == (  # S-131: the orphans go along
        "已勾选 2 款游戏、1 个孤立目录，点击「备份已勾选项」直接开始，或「预览…」先看将复制的内容。"
    )
    assert state.guidance(ready, report, (2, 0)).startswith("已勾选 2 款游戏，点击")
    assert state.guidance(ready, report, (0, 1)).startswith("已勾选 0 款游戏、1 个孤立目录")
    assert "备份已勾选项" in state.guidance(ready, report, (0, 0))
    assert "5. 点「备份已勾选项」" in state.guidance(GuiSettings())
    assert state.guidance(ready, report, (0, 0)).startswith("还没有勾选")
    empty = ScanReport(2, "0", "2026-10-24T00:00:00Z", [], "E:\\RenPy")
    assert state.guidance(ready, empty, (0, 0)) is None, "nothing was found"
    assert state.guidance(GuiSettings(), report, (2, 1)).startswith("首次使用")
    done = BackupResult([GameResult("A", "game", "ok", game_id="a")])
    text = state.backup_next_steps(done)
    assert text.startswith("下一步：到“备份库”页点“校验”") and "清理…" in text
    partial = BackupResult([GameResult("A", "game", "partial", game_id="a")])
    assert "清理" not in state.backup_next_steps(partial), "cleanup only after complete backups"
    assert state.backup_next_steps(BackupResult([GameResult("A", "game", "failed")])) is None


def test_export_summary_explains_paths_and_omissions() -> None:
    common = {
        "path": "E:\\exports\\x.zip",
        "game_id": "g",
        "display_name": "Game",
        "snapshot_id": "20261024T012755Z",
        "file_count": 3,
        "total_bytes": 2048,
        "archive_bytes": 1024,
        "includes_tokens": False,
    }
    anonymous = ExportResult(
        **common,
        excluded_sources=["extra-1", "machine_tokens"],
        paths="anonymized",
        excluded_reasons={"extra-1": "absolute_path", "machine_tokens": "private_keys"},
    )
    text = state.export_summary(anonymous)
    assert text.startswith("已导出并逐个校验 3 个文件") and "路径已匿名化" in text
    assert "未包含 tokens" in text and "extra-1" in text and "保留原始路径" in text
    kept = ExportResult(**common, excluded_sources=[], paths="original", excluded_reasons={})
    assert "保留了本机的原始路径" in state.export_summary(kept)


# ----------------------------------------------------------------- batch export / import


def test_batch_export_rows_and_summary() -> None:
    def item(result: str, **extra: object) -> dict:
        base = {
            "game_id": "g",
            "display_name": "Game",
            "snapshot_id": "20261024T000000Z",
            "path": "E:\\out\\EngineSave-game-20261024T000000Z.zip",
            "result": result,
            "reason": None,
            "export": None,
        }
        return {**base, **extra}

    done = {"file_count": 3, "archive_bytes": 2048, "paths": "anonymized", "includes_tokens": True}
    batch = BatchResult(
        "export",
        "E:\\out",
        [
            item("exported", export=done),
            item("skipped", reason="the file already exists"),
            item("skipped", snapshot_id=None, path=None, reason="no manual or imported snapshot"),
            item("failed", reason="boom"),
        ],
    )
    rows = state.batch_export_rows(batch)
    assert [r[0] for r in rows] == ["已导出", "已跳过", "已跳过", "失败"]
    assert (
        rows[0][3] == "EngineSave-game-20261024T000000Z.zip"
        and rows[0][4] == "3 个文件，归档 2.0 KB"
    )
    assert "覆盖同名文件" in rows[1][4] and rows[2][2:4] == (state.NO_VALUE, state.NO_VALUE)
    assert rows[3][4] == "boom"
    text = state.batch_export_summary(batch)
    assert text.startswith("已导出 1 个、跳过 2 个、失败 1 个") and "E:\\out" in text
    assert "路径已匿名化" in text and "私钥" in text


def test_import_rows_and_summary(tmp_path: Path) -> None:
    header = {
        "game": {"display_name": "Game"},
        "snapshot_id": "20261024T000000Z",
        "manifest": {"files": [{"size": 1024}, {"size": 1024}], "sources": []},
    }
    plan = ImportPlan(tmp_path, tmp_path / "a.zip", header, "g", "identity")
    previews = [(tmp_path / "a.zip", plan, None), (tmp_path / "b.zip", None, "not an export")]
    rows = state.import_preview_rows(previews)
    assert rows[0] == (
        "a.zip",
        "Game",
        "20261024T000000Z",
        "2",
        "2.0 KB",
        "可以导入：已有记录（存档目录或游戏目录相同）",
    )
    assert rows[1][0] == "b.zip" and rows[1][5] == "无法导入：not an export"
    copy = ImportPlan(tmp_path, tmp_path / "c.zip", header, "g", "identity", duplicate_of="a.zip")
    with_copy = [*previews, (tmp_path / "c.zip", copy, None)]
    assert state.import_preview_rows(with_copy)[2][5] == "批内重复（将跳过）：与 a.zip 是同一快照"
    assert state.import_preview_rows(with_copy, True)[2][5] == "重新导入：与 a.zip 是同一快照"
    assert state.import_counts(with_copy) == (1, 1, 1), "S-098: the copy is skipped"
    assert state.import_counts(with_copy, True) == (2, 0, 1)
    assert "1 个将跳过：已导入过，或与同一文件夹里前面的文件是同一快照" in (
        state.import_check_summary(1, 3, 1)
    )
    done = {
        "display_name": "Game",
        "snapshot_id": "20261024T010101Z",
        "file_count": 2,
        "total_bytes": 2048,
        "created_record": True,
    }
    batch = BatchResult(
        "import",
        str(tmp_path),
        [
            {"archive": str(tmp_path / "a.zip"), "result": "imported", "import": done},
            {"archive": str(tmp_path / "b.zip"), "result": "failed", "import": None, "error": "x"},
        ],
    )
    rows = state.import_rows(batch)
    assert rows[0][2] == "20261024T010101Z" and rows[0][5] == "已导入（新记录）"
    assert rows[1][5] == "失败：x"
    assert state.import_summary(batch).startswith("已导入 1 个、失败 1 个。")


def test_main_buttons_say_what_they_do(tmp_path: Path) -> None:
    """D32 ③: the primary buttons name the action, with counts."""
    assert state.batch_export_primary_text("") == "导出"
    assert state.batch_export_primary_text(str(tmp_path / "batch")) == "导出到 batch"
    assert state.batch_export_primary_text("E:\\") == "导出到 E:\\"
    assert (state.import_primary_text(0), state.import_primary_text(3)) == ("导入", "导入（3 个）")
    assert state.import_check_summary(3, 4) == "检查完成：3/4 个导出文件可以导入。点“导入”开始。"
    assert state.import_check_summary(0, 1) == "检查完成：0/1 个导出文件可以导入。"

    def target(files: int, overwrites: int = 0, extra: int = 0) -> RestoreTarget:
        source = {"source_id": "local", "kind": "local", "root_token": "GAME_ROOT"}
        return RestoreTarget(source, tmp_path, "game_root", [{}] * files, overwrites, extra)

    def plan(*targets: RestoreTarget) -> RestorePlan:
        return RestorePlan(None, "g", "Game", "game", "SID", {}, list(targets))  # type: ignore[arg-type]

    assert state.restore_primary_text(plan(target(4), target(8, overwrites=5))) == (
        "恢复（覆盖 5 个文件，先建安全快照）"
    )
    assert (
        state.restore_primary_text(plan(target(4, extra=2)))
        == "恢复（写入 4 个文件，先建安全快照）"
    )
    assert state.restore_primary_text(plan(target(4))) == "恢复（写入 4 个文件）"


def test_one_click_backup_writes_the_plan_to_the_log(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    build_f1(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "repo", saves_root_override=saves_root)
    lines = state.backup_plan_lines(plan)
    games, files, size = state.plan_totals(plan)
    assert (
        lines[0] == f"开始备份到 {plan.dest}：{games} 项，{files} 个文件，{state.size_text(size)}"
    )
    assert any(line.startswith("  Readable Game · local：") for line in lines[1:])
    assert any(line.startswith("  Readable Game · global：") for line in lines[1:])
    status = state.backup_status((2, 12, 2048), (0, 2, "Readable Game"))
    assert status == "正在备份 Readable Game（1/2）… 共 2 项、12 个文件、2.0 KB"


# ------------------------------------------------------------------------------- cleanup


def test_cleanup_is_offered_only_after_a_complete_backup() -> None:
    ok = GameResult("A", "game", "ok", game_id="a")
    orphan = GameResult("B", "unlinked", "ok", game_id="b")
    partial = GameResult("C", "game", "partial", game_id="c")
    assert state.cleanup_candidates(BackupResult([ok, orphan])) == ["a", "b"]
    assert state.cleanup_candidates(BackupResult([ok, partial])) == []
    assert state.cleanup_candidates(BackupResult([ok, GameResult("D", "game", "failed")])) == []
    assert state.cleanup_candidates(BackupResult([])) == []


def test_records_are_grouped_by_whether_an_install_is_there(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D43, D51: 本体存在 when an install folder is on disk, 本体不在 otherwise — cleaned up or
    deleted by hand alike; the orphaned folder has no install at all."""
    import shutil

    from fixtures.builder import build_f2

    first = build_f1(games_root, saves_root)
    second = build_f2(games_root, saves_root)
    build_f8(saves_root)
    plan = prepare_backup(
        [games_root], tmp_path / "repo", saves_root_override=saves_root, include_unlinked=[]
    )
    repo = Repo.open_or_create(plan.dest)
    execute_backup(plan, repo)
    shutil.rmtree(second.root)  # deleted by hand: no cleanup recorded
    rows = {r.name: r for r in state.record_rows(repo, repo.load_catalog())}
    assert rows["Readable Game"].installed and first.root.is_dir()
    deleted = next(r for r in rows.values() if r.kind == "game" and not r.installed)
    assert deleted.values()[1] == "游戏", "D51: the type column says the kind and nothing else"
    orphans = [r for r in rows.values() if r.kind == "unlinked"]
    assert orphans and all(not r.installed and r.values()[1] == "孤立目录" for r in orphans)
    groups = state.record_groups(list(rows.values()))
    assert [(name, len(members)) for name, members in groups] == [
        ("本体存在", 1),
        ("本体不在", len(rows) - 1),
    ]


def test_the_type_column_says_the_kind_of_record_only() -> None:
    """D51: 「游戏」 or 「孤立目录」, installed or not, cleaned up or not; no date."""
    rows = [
        state.RecordRow("a", "A", "game", 1, "", installed=True),
        state.RecordRow("b", "B", "game", 1, ""),
        state.RecordRow("d", "D", "unlinked", 1, ""),
    ]
    assert [r.values()[1] for r in rows] == ["游戏", "游戏", "孤立目录"]
    assert state.WIDEST_KIND == "孤立目录"
    assert (state.INSTALLED_GROUP, state.GONE_GROUP) == ("本体存在", "本体不在")
    assert state.CLEANUP_LABELS == {
        "game_root": "游戏目录",
        "global": "全局存档目录",
        "unlinked": "孤立存档目录",
    }


def test_the_search_box_matches_names_folders_and_ids() -> None:
    """D46: case-insensitive substrings of the name, the save directory, the install folder
    names and the first 8 characters of the id; each group filtered on its own."""
    a = state.RecordRow(
        "abcdef12-3456", "Moonlit Harbor", "game", 1, "", installed=True,
        search=("Moonlit Harbor", "MoonlitHarbor", "Moonlit Harbor - v1.0.1", "abcdef12"),
    )  # fmt: skip
    b = state.RecordRow(
        "99887766-5544", "Willow", "game", 1, "", search=("Willow", "Willowpromise-1", "99887766")
    )
    assert state.matches("harbor", a.search) and state.matches("V1.0", a.search)
    assert state.matches("ABCDEF", a.search) and not state.matches("3456", a.search)
    assert state.matches("", a.search) and state.matches("  ", b.search)
    groups = state.record_groups([a, b], "wil")
    assert [(name, [r.name for r in rows]) for name, rows in groups] == [
        ("本体存在", []),
        ("本体不在", ["Willow"]),
    ]
    assert state.group_label("本体存在", 0, 1, "etl") == "本体存在（0/1）"
    assert state.group_label("本体不在", 1, 1) == "本体不在（1）"
    assert state.check_count_text(3, 0) == "已勾选 3 条"
    assert state.check_count_text(3, 2) == "已勾选 3 条（其中 2 项被筛选隐藏）"
    assert state.NO_MATCH == "无匹配" and state.SEARCH_PLACEHOLDER == "搜索游戏…"


def test_the_scan_page_search_looks_at_names_folders_and_save_directories(
    report: ScanReport,
) -> None:
    confirmed = by_status(report, GameStatus.CONFIRMED)
    fields = state.game_search_fields(confirmed)
    assert confirmed.display_name in fields and Path(confirmed.game_root).name in fields
    assert confirmed.identity.renpy_save_directory in fields
    assert state.matches(confirmed.identity.renpy_save_directory.upper(), fields)


def test_cleanup_requests_and_labels(tmp_path: Path) -> None:
    kept, gone, removed = tmp_path / "kept", tmp_path / "gone", tmp_path / "removed"
    kept.mkdir()
    removed.mkdir()
    doc = {
        "game_id": "g",
        "display_name": "Game",
        "kind": "game",
        "identity": {"renpy_save_directory": "Game-1"},
        "installs": [
            {"game_root": str(kept)},
            {"game_root": str(gone)},
            {"game_root": str(removed), "removed_at": "2026-10-24T00:00:00Z"},
        ],
    }
    # D41: a cleaned-up install is asked for too (the check shows it as cleaned); one that is
    # simply not there any more is not.
    assert state.cleanup_requests(doc) == [
        ("game_root", str(kept)),
        ("game_root", str(removed)),
        ("global", None),
    ]
    no_global = {**doc, "identity": {"renpy_save_directory": None}, "installs": []}
    assert state.cleanup_requests(no_global) == []
    orphan = {"game_id": "o", "kind": "unlinked", "identity": {"renpy_save_directory": "Orph"}}
    assert state.cleanup_requests(orphan) == [("unlinked", "Orph")]
    assert state.record_label(doc) == "Game  (g)"
    assert state.record_label(orphan) == "o  [孤立目录]  (o)"


def test_cleaned_and_gone_folders_are_both_not_there(tmp_path: Path) -> None:
    """D51 ②: the 「检查」 column says 「不存在」 for both; 「说明」 still says when a cleanup
    removed it; the summary and the log line count them together."""
    cleaned = CleanupTarget("global", str(tmp_path / "G-1"))
    cleaned.already_cleaned = {"method": "recycle", "at": "2026-10-24T12:58:52Z"}
    gone = CleanupTarget("game_root", str(tmp_path / "gone"), gone=True)
    assert state.cleanup_verdict(cleaned) == "不存在"
    assert state.cleanup_note(cleaned).startswith("已移到回收站 · 2026-10-24 ")
    cleaned.already_cleaned["purged_at"] = "2026-10-25T00:00:00Z"
    assert state.cleanup_note(cleaned).endswith("（已从回收站删除）")
    assert (state.cleanup_verdict(gone), state.cleanup_note(gone)) == ("不存在", "没有清理记录")
    passed = CleanupTarget("game_root", str(tmp_path / "here"), ok=True)
    plan = types.SimpleNamespace(targets=[passed, cleaned, gone])  # what the summary reads
    assert state.cleanup_check_summary([plan]) == "检查完成：1 项可清理（已勾选），2 项不存在。"
    log_line = state.cleanup_check_log([plan])
    assert log_line == "清理检查：1 条记录，1 项可清理，2 项不存在，0 项未通过"


def test_cleanup_texts(tmp_path: Path) -> None:
    """D41 4: short texts; one line per run."""
    root = CleanupTarget("game_root", str(tmp_path / "My Game"), 10, 3 * 1024**2, True)
    saves = CleanupTarget("global", str(tmp_path / "MyGame-1"), 4, 2048, True)
    refused = CleanupTarget("global", str(tmp_path / "x"), reasons=["one", "two"])
    assert "无法还原" in state.PERMANENT_WARNING and "游戏本体" in state.PERMANENT_WARNING
    assert state.METHOD_TEXTS == {"recycle": "移到回收站", "permanent": "永久删除（无法还原）"}
    assert (state.cleanup_verdict(root), state.cleanup_verdict(refused)) == ("通过", "拒绝")
    assert state.cleanup_note(root) == "本体不在备份，需重新下载"
    assert state.cleanup_note(refused) == "one；two"
    assert state.cleanup_note(saves) == "已备份并校验"
    saves.needs_room = True
    assert state.cleanup_note(saves) == "已备份并校验；回收站放不下，执行时先腾空间"

    def outcome(result: str, error: str | None = None) -> dict:
        return {"kind": "global", "path": "P", "bytes": 1024, "result": result, "error": error}

    assert state.cleanup_outcome(outcome("recycled")) == "已移到回收站 · 1.0 KB"
    assert state.cleanup_outcome(outcome("deleted")) == "已永久删除 · 1.0 KB"
    assert state.cleanup_outcome(outcome("skipped_full_bin")) == "未处理：回收站已满"
    assert state.cleanup_outcome(outcome("failed", "busy")) == "失败：busy"
    plan = cleanup_plan("Game", [root, saves])
    recycled = CleanupResult("2026-10-24T00:00:00Z", "recycle", [outcome("recycled")])
    text = state.cleanup_batch_summary([(plan, recycled)], "recycle")
    assert text == "已移到回收站 1 项 · 1.0 KB。回收站里的内容清空前不释放空间。"
    assert "P" not in text, "no paths in the summary: they are in the rows"
    mixed = CleanupResult(
        "2026-10-24T00:00:00Z",
        "permanent",
        [outcome("deleted"), outcome("failed", "busy"), outcome("skipped_full_bin")],
    )
    stopped = cleanup_plan("Other", [refused])
    text = state.cleanup_batch_summary([(plan, mixed), (stopped, None)], "permanent")
    assert text == (
        "已永久删除 1 项 · 1.0 KB；失败 1 项；未处理 1 项（回收站已满）；"
        "再次检查没有通过、未删除：Other。"
    )
    log = state.cleanup_done_log([(plan, mixed), (stopped, None)])
    assert log == "清理完成：0 项进回收站 / 1 项永久删除，释放 1.0 KB，失败 2，未处理 1"
    purged = [{"result": "purged", "bytes": 2048}, {"result": "not_found", "bytes": 1}]
    assert state.cleanup_done_log([(plan, recycled)], purged).endswith(
        "；从回收站删除了 EngineSave 之前放入的 1 项（2.0 KB）"
    )


def cleanup_plan(name: str, targets: list[CleanupTarget]) -> CleanupPlan:
    return CleanupPlan(None, "g", name, "game", "SID", "manual", "recycle", 1, targets, [], {}, 9)  # type: ignore[arg-type]


def test_cleanup_buttons_and_check_summary(tmp_path: Path) -> None:
    root = CleanupTarget("game_root", str(tmp_path / "My Game"), 10, 3 * 1024**2, True)
    saves = CleanupTarget("global", str(tmp_path / "MyGame-1"), 4, 2048, True)
    refused = CleanupTarget("global", str(tmp_path / "x"), reasons=["busy"])
    cleaned = CleanupTarget("global", str(tmp_path / "y"))
    cleaned.already_cleaned = {"method": "recycle", "at": "2026-10-24T00:00:00Z"}
    assert state.cleanup_primary_text("recycle", []) == "移到回收站"
    assert state.cleanup_primary_text("recycle", [root, saves]) == "移到回收站（2 项，3.0 MB）"
    assert state.cleanup_primary_text("permanent", []) == "永久删除"
    assert state.cleanup_primary_text("permanent", [saves]) == "永久删除 1 项（2.0 KB，无法还原）"
    assert state.continue_primary_text(3) == "继续清理（3 项）"
    plans = [cleanup_plan("Game", [root, refused]), cleanup_plan("Old", [cleaned])]
    assert (
        state.cleanup_check_summary(plans)
        == "检查完成：1 项可清理（已勾选），1 项不存在，1 项未通过。"
    )
    assert (
        state.cleanup_check_log(plans) == "清理检查：2 条记录，1 项可清理，1 项不存在，1 项未通过"
    )
    assert state.cleanup_check_summary([cleanup_plan("Game", [refused])]) == (
        "检查完成：0 项可清理，1 项未通过。"
    )
    refused.recycle_refused = True
    assert state.cleanup_check_summary([cleanup_plan("Game", [refused])]).endswith(
        "放不进回收站的项可以改选“永久删除”。"
    )


def test_recycle_bin_lines_and_the_full_bin_dialog() -> None:
    r"""D41: 回收站 E:\ 剩余 X / Y; the full-bin dialog's heading and live line."""
    gb = 1024**3
    bin_ = RecycleBin("E:\\", capacity=100 * gb, used=50 * gb)
    assert state.bin_line(bin_) == r"回收站 E:\ 剩余 45.0 GB / 100.0 GB"
    assert state.bin_line(bin_, room=-5) == r"回收站 E:\ 剩余 0 B / 100.0 GB"
    assert state.bin_line(RecycleBin("C:\\", disabled=True)) == r"回收站 C:\ 已关闭（会直接删除）"
    assert "大小未知" in state.bin_line(RecycleBin("D:\\"))
    rooms = {"E:\\": 1 * gb}
    waiting = [("E:\\", 3 * gb, False), ("E:\\", 1 * gb, False)]
    ours = [("E:\\", 2 * gb, True), ("E:\\", 5 * gb, False)]
    (need,) = state.bin_needs(rooms, waiting, ours)
    assert (need.room, need.waiting, need.count, need.purge, need.missing) == (
        1 * gb,
        4 * gb,
        2,
        2 * gb,
        1 * gb,
    )
    assert state.full_bin_heading([need]) == [
        r"回收站 E:\ 剩余 1.0 GB，待放入 2 项共 4.0 GB，还差 3.0 GB"
    ]
    assert state.full_bin_status([need]) == "腾出 2.0 GB · 还差 1.0 GB"
    (enough,) = state.bin_needs(rooms, [("E:\\", 3 * gb, False), ("E:\\", 1 * gb, True)], ours)
    assert enough.missing == 0 and state.full_bin_status([enough]).endswith("够了，可以继续清理")


# ------------------------------------------------------------------------------- logging


def test_logging_writes_file_and_panel(tmp_path: Path) -> None:
    panel, file_handler = setup_logging(tmp_path / "logs")
    try:
        assert (file_handler.maxBytes, file_handler.backupCount) == (MAX_BYTES, BACKUP_COUNT)
        logging.getLogger("enginesave.gui").info("扫描完成：%d 款游戏", 2)
        logging.getLogger("enginesave.backup").warning("object damaged")
        lines = panel.drain()
        assert lines[0].endswith("扫描完成：2 款游戏")
        assert "[WARNING] object damaged" in lines[1]
        assert panel.drain() == []
    finally:
        close_logging(panel, file_handler)
    text = (tmp_path / "logs" / LOG_NAME).read_text(encoding="utf-8")
    assert "扫描完成：2 款游戏" in text and "WARNING enginesave.backup: object damaged" in text


# -------------------------------------------------------------------------------- workers


def drain(runner: TaskRunner, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while runner.busy and time.monotonic() < deadline:
        runner.poll()
        time.sleep(0.01)
    runner.poll()
    assert not runner.busy


def test_task_runner_delivers_results_on_the_polling_thread() -> None:
    busy: list[tuple[bool, str]] = []
    runner = TaskRunner(on_busy=lambda flag, name: busy.append((flag, name)))
    seen: dict[str, object] = {}
    worker_threads: list[str] = []

    def job(progress):
        worker_threads.append(threading.current_thread().name)
        progress(1)
        progress(2)
        return "result"

    assert runner.run_in_thread(
        "demo",
        job,
        lambda value: seen.update(done=(value, threading.current_thread().name)),
        lambda exc: seen.update(error=exc),
        lambda value: seen.setdefault("progress", []).append(value),
    )
    assert runner.run_in_thread("second", job, print, print) is False, "one job at a time"
    drain(runner)
    assert seen["done"] == ("result", threading.current_thread().name)
    assert seen["progress"] == [1, 2] and "error" not in seen
    assert worker_threads == ["enginesave-demo"]
    assert busy == [(True, "demo"), (False, "")]

    def broken(_progress):
        raise ValueError("boom")

    runner.run_in_thread("broken", broken, lambda _v: None, lambda exc: seen.update(error=exc))
    drain(runner)
    assert isinstance(seen["error"], ValueError)


def reachable_from(start: object) -> set[int]:
    """Objects a function keeps alive through closures, defaults, bound methods and containers
    (module globals and classes are not followed)."""
    seen: set[int] = set()
    todo = [start]
    while todo:
        item = todo.pop()
        if id(item) in seen or isinstance(item, type | types.ModuleType):
            continue
        seen.add(id(item))
        if isinstance(item, types.FunctionType):
            todo += [cell.cell_contents for cell in item.__closure__ or ()]
            todo += list(item.__defaults__ or ())
        elif isinstance(item, types.MethodType):
            todo += [item.__self__, item.__func__]
        elif isinstance(item, dict):
            todo += [*item.keys(), *item.values()]
        elif isinstance(item, list | tuple | set):
            todo += list(item)
        elif hasattr(item, "__dict__"):
            todo.append(vars(item))
    return seen


def test_the_worker_thread_cannot_reach_the_window() -> None:
    """Tk objects must be destroyed on the Tk thread, so the worker holds only job and queue."""

    class Window:  # stands for the App and its widgets
        def done(self, _value: object) -> None: ...

        def busy(self, _flag: bool, _name: str) -> None: ...

    window = Window()
    release = threading.Event()
    runner = TaskRunner(on_busy=window.busy)
    runner.run_in_thread("reach", lambda _progress: release.wait(5), window.done, window.done)
    worker = next(t for t in threading.enumerate() if t.name == "enginesave-reach")
    reachable = reachable_from(worker._target)  # what the thread itself keeps alive
    release.set()
    drain(runner)
    assert id(window) not in reachable and id(runner) not in reachable
    assert runner._callbacks == {}, "callbacks are released once the job has ended"


# ------------------------------------------------------- a shared folder, confirm / link (D48)


def test_records_that_share_a_name_show_their_version(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D48 2: the two seasons are both called SharedSeasons: the library page tells them
    apart by version; without a version the id's start does it."""
    from fixtures.builder import build_f18

    build_f18(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "repo", saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    execute_backup(plan, repo)
    rows = state.record_rows(repo, repo.load_catalog())
    assert [r.name for r in rows] == [
        "SharedSeasons · Season-1-Extra",
        "SharedSeasons · Season2-v9.00",
    ]
    assert all("SharedSeasons" in r.search for r in rows), "the search still finds the name"
    names = state.distinct_names(
        [("aaaaaaaa1", "A", "1.0"), ("bbbbbbbb2", "A", "1.0"), ("c", "C", None)]
    )
    assert names == {"aaaaaaaa1": "A · aaaaaaaa", "bbbbbbbb2": "A · bbbbbbbb", "c": "C"}


def test_a_shared_folder_is_noted_on_the_scan_page(games_root: Path, saves_root: Path) -> None:
    from fixtures.builder import build_f18

    build_f18(games_root, saves_root)
    report = run_scan(ScanOptions(roots=[games_root], saves_root_override=saves_root))
    selection = state.Selection()
    selection.load(report)
    rows = [state.game_row(g, selection) for g in report.games]
    assert [r.status for r in rows] == ["CONFIRMED", "CONFIRMED"]
    assert all("与 1 款游戏共用全局存档目录" in r.notes for r in rows)
    assert all(r.check == state.CHECKED for r in rows), "both are ticked: both back it up"
    assert state.checked_text(2, 0) == "已勾选 2 款游戏"
    assert (
        state.checked_text(2, 1, hidden=1) == "已勾选 2 款游戏（其中 1 款被筛选隐藏）、1 个孤立目录"
    )


def test_the_restore_preview_names_the_games_sharing_the_folder() -> None:
    """D48 1: restoring into a shared folder overwrites the other game's saves in it too."""
    plan = RestorePlan(None, "g", "Game", "game", "s", {}, [], shared_with=["Other (2.0)"])  # type: ignore[arg-type]
    assert state.restore_notes(plan) == [
        "注意：全局存档目录与 Other (2.0) 共用，恢复会一并覆盖其中 Other (2.0) 的存档"
    ]


def test_confirm_link_offers_the_record_new_or_suggested(report: ScanReport) -> None:
    """D48 3: the linked record is chosen at first; without one a new record is, never a
    suggested record by guessing. Every CANDIDATE folder is listed and ticked."""
    candidate = by_status(report, GameStatus.CANDIDATE)
    candidate.link_suggestions = [
        {"game_id": "r1", "reason": "shared_save_directory"},
        {"game_id": "r2", "reason": "dir_name_match"},
    ]
    names = {"r1": "Season · 1.0", "r2": "Other"}
    choices, first = state.link_choices(candidate, names)
    assert [(c.game_id, c.label) for c in choices] == [
        ("r1", "Season · 1.0（存档目录相同，可能是共用存档目录的另一款游戏）"),
        ("r2", "Other（目录名或启动程序名相同）"),
        (None, "新建记录"),
    ]
    assert choices[first].game_id is None
    candidate.known_game_id = "r2"
    choices, first = state.link_choices(candidate, names)
    assert [c.game_id for c in choices] == ["r2", "r1", None] and first == 0
    assert choices[0].label == "Other（当前关联的记录）"
    sources = state.candidate_sources(candidate)
    assert sources and all(s.status is SourceStatus.CANDIDATE for s in sources)
    values = state.candidate_values(sources[0], state.CHECKED)
    assert values[0] == state.CHECKED and values[1] == sources[0].path
    assert state.confirm_primary_text(2) == "确认（2 个来源）"
    assert state.confirm_primary_text(0) == "确认关联"
    assert state.LINK_TITLE == "确认 / 关联"


def test_a_game_scanned_again_keeps_or_gains_its_tick(report: ScanReport) -> None:
    """After 「确认 / 关联…」 the row is ticked when the game can be backed up now."""
    import copy

    selection = state.Selection()
    selection.load(report)
    candidate = by_status(report, GameStatus.CANDIDATE)
    assert not selection.games[state.game_key(candidate)]
    fresh = copy.deepcopy(candidate)
    fresh.status = GameStatus.CONFIRMED
    selection.replace(candidate, fresh)
    assert selection.games[state.game_key(fresh)]
    unsupported = by_status(report, GameStatus.UNSUPPORTED)
    selection.replace(unsupported, unsupported)
    assert state.game_key(unsupported) in selection.locked


# ---------------------------------------------------------------- deleting backups (D49)


def test_the_delete_dialog_ticks_counts_and_warns() -> None:
    """D49 1: every snapshot starts ticked, or only the snapshot selected without ticks; the
    ticks become requests (all of a record = the whole record); the summary, the red line
    for records whose game is gone and the button say what one click does."""
    from enginesave.removal import Inventory, SnapshotSize

    a = state.RemoveRecord("a", "A", "game", True, ("s1", "s2"))
    b = state.RemoveRecord("b", "B", "game", False, ("s3",))
    empty = state.RemoveRecord("c", "C", "game", False, ())
    records = [a, b, empty]
    assert state.initial_ticks(records) == {("a", "s1"), ("a", "s2"), ("b", "s3"), ("c", "")}
    assert state.initial_ticks([a], "s1") == {("a", "s1")}, "only the selected snapshot"
    assert state.initial_ticks([a], "nope") == {("a", "s1"), ("a", "s2")}
    ticked = {("a", "s1"), ("b", "s3"), ("c", "")}
    assert state.remove_requests(records, ticked) == [("a", ["s1"]), ("b", None), ("c", None)]
    shared = {"x" * 64: 100}
    found = Inventory(
        {
            ("a", "s1"): SnapshotSize(1, 100, {"y" * 64: 100}, 0, 10),
            ("a", "s2"): SnapshotSize(1, 100, shared, 0, 10),
            ("b", "s3"): SnapshotSize(1, 100, shared, 0, 10),
        }
    )
    counts = state.remove_counts(records, ticked, found)
    assert (counts.records, counts.snapshots, counts.gone) == (2, 2, 2)
    assert counts.freed == 100 + 10 + 10, "s1's own object and two manifests; x is still used"
    assert state.remove_summary(counts, None) == "将删除 2 条记录、2 个快照，释放约 120 B。"
    assert state.remove_summary(counts, "1 snapshot manifest(s) could not be read").endswith(
        "（对象库这次不回收：1 snapshot manifest(s) could not be read）。"
    )
    assert state.gone_warning(2) == "其中 2 条记录的本体已不在，删除后这些存档再也找不回。"
    assert state.gone_warning(0) is None
    assert state.remove_primary_text(counts) == "永久删除（2 个快照）"
    only_empty = state.remove_counts(records, {("c", "")}, found)
    assert state.remove_primary_text(only_empty) == "永久删除（1 条记录）"


@pytest.mark.parametrize("language", ["", "zh-TW", 3, None])
def test_an_unknown_language_is_auto_and_keeps_every_other_setting(
    tmp_path: Path, language: object
) -> None:
    """D70 4: ``"language": ""`` (the 0.11 development version wrote it) or ``"zh-TW"`` made
    the whole file "damaged" and every setting was lost when the window closed. Now it is
    ``auto`` and the rest is kept."""
    path = tmp_path / "gui.json"
    used = GuiSettings(scan_roots=["E:\\Games"], repo_path="E:\\Backup", language="en")
    save_settings(used, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "language": language}), encoding="utf-8")
    loaded, note = load_settings(path)
    assert loaded.language == "auto"
    assert loaded.scan_roots == ["E:\\Games"] and loaded.repo_path == "E:\\Backup"
    assert note is None  # nothing was lost to say
