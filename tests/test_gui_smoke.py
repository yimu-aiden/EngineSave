"""The real window end to end with real widgets (plan sections 17 M10, 19 M13–M14, 20 M17).

Skipped when Tk cannot create a window. The main window stays hidden; message boxes, the name
prompt and file choosers are replaced by recorders so nothing waits for a click. Everything is
written under ``tmp_path`` (library, restore target, export, settings and log); the recycle bin
is always a fake that only removes temporary folders (rule 16).
"""

from __future__ import annotations

import contextlib
import gc
import json
import os
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from enginesave.appdirs import documents_dir
from enginesave.gui import state
from enginesave.gui.logging_setup import close_logging
from enginesave.gui.settings import GuiSettings, blank_settings, load_settings, save_settings
from fixtures.builder import build_f1, build_f5, build_f8, reinstall_copy

OVERRIDE_ENV = "ENGINESAVE_RENPY_SAVES_ROOT"


def env_without_override() -> dict[str, str]:
    """The test environment (sandboxed APPDATA) minus the conftest-wide overrides -- the
    saves root and, since 0.10.0, the other engines' root tokens (the window shows a bar for
    them too): the sandbox folders they name stand in as the system's own instead."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key != OVERRIDE_ENV and not key.startswith("ENGINESAVE_ROOT_")
    }
    env["LOCALAPPDATA"] = os.environ["ENGINESAVE_ROOT_LOCALAPPDATA"]
    locallow = Path(os.environ["ENGINESAVE_ROOT_UNITY_LOCALLOW"])  # <profile>\AppData\LocalLow
    env["USERPROFILE"] = str(locallow.parent.parent)
    return env


tkinter = pytest.importorskip("tkinter")


class Answers:
    """Stand-ins for message boxes, the name prompt and file choosers."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, str, str]] = []
        self.yes = True
        self.directory = ""
        self.save_as = ""
        self.open_file = ""
        self.typed: str | None = ""
        self.prompts: list[str] = []

    def recorder(self, kind: str):
        def show(title: str = "", message: str = "", **_options: Any) -> str:
            self.shown.append((kind, title, message))
            return "ok"

        return show

    def ask(self, title: str = "", message: str = "", **_options: Any) -> bool:
        self.shown.append(("ask", title, message))
        return self.yes

    def type_name(self, _title: str = "", prompt: str = "", **_options: Any) -> str | None:
        self.prompts.append(prompt)
        return self.typed

    def messages(self, kind: str) -> list[str]:
        return [f"{title}: {message}" for k, title, message in self.shown if k == kind]


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Answers:
    from tkinter import filedialog, messagebox, simpledialog

    replies = Answers()
    for kind in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, kind, replies.recorder(kind))
    monkeypatch.setattr(messagebox, "askyesno", replies.ask)
    monkeypatch.setattr(simpledialog, "askstring", replies.type_name)
    monkeypatch.setattr(filedialog, "askdirectory", lambda **_o: replies.directory)
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **_o: replies.save_as)
    monkeypatch.setattr(filedialog, "askopenfilename", lambda **_o: replies.open_file)
    return replies


@pytest.fixture(autouse=True)
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Explorer is never opened (D36): ``os.startfile`` records the folder instead."""
    folders: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda path, *_a: folders.append(str(path)), raising=False)
    return folders


@pytest.fixture(autouse=True)
def quick_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The window's timers, shortened (D38): results are polled every 10 ms instead of 100,
    a queued job retries after 10 ms and typing counts as paused after 50 ms instead of 400
    (20 ms instead of 200 in the search boxes). The order of events is the same; ``wait`` and
    ``settle`` still wait for conditions."""
    from enginesave.gui import app
    from enginesave.gui.views import filter_box
    from enginesave.gui.views.dialogs import base, importing, restore

    monkeypatch.setattr(app, "POLL_MS", 10)
    monkeypatch.setattr(base, "RETRY_MS", 10)
    for module in (base, importing, restore):
        monkeypatch.setattr(module, "TYPING_MS", 50)
    monkeypatch.setattr(filter_box, "FILTER_MS", 20)


@pytest.fixture
def root():
    # The previous test's window, App and Tcl interpreter reference each other and are freed by
    # the cyclic garbage collector. Collect them here, on the Tk thread: left to the automatic
    # collector they could be finalised on a worker thread, and a Tcl interpreter must not be
    # deleted from another thread. (test_closed_dialogs_are_freed checks that nothing keeps a
    # closed dialog alive.) Tk start-up failures between tests came from fd-level output
    # capture, hence ``--capture=sys`` in pyproject.toml.
    gc.collect()
    try:
        window = tkinter.Tk()
    except tkinter.TclError as exc:
        pytest.skip(f"Tk cannot open a window here: {exc}")
    window.withdraw()
    yield window
    with contextlib.suppress(tkinter.TclError):
        window.destroy()
    for thread in threading.enumerate():  # no job may outlive its test and its window
        if thread.name.startswith("enginesave-"):
            thread.join(timeout=120)
            assert not thread.is_alive(), f"{thread.name} did not finish"


def test_the_windows_environment_keeps_every_root_in_the_sandbox(isolated_env: Path) -> None:
    """M48 review: without ENGINESAVE_ROOT_* the new roots are the sandbox's stand-ins too (the
    known folders are faked in conftest), never the user's real 「文档」 or 「保存的游戏」."""
    from enginesave import roots

    env = env_without_override()
    for token in roots.ENGINE_PROFILE_TOKENS:
        path, _how = roots.profile_root(token, env)
        assert path is not None and path.is_relative_to(isolated_env), (token, path)


def test_the_real_known_folders_are_read_only_looked_up() -> None:
    """The real SHGetKnownFolderPath, read only: 「文档」 and 「保存的游戏」 are absolute paths."""
    from conftest import REAL_KNOWN_FOLDER
    from enginesave import roots

    for folder_id in roots.KNOWN_FOLDERS.values():
        found = REAL_KNOWN_FOLDER(folder_id)
        assert found is None or found.is_absolute()


def make_app(root, settings: GuiSettings, settings_file: Path):
    from enginesave.gui.app import App, apply_theme

    apply_theme(root)
    save_settings(settings, settings_file)
    return App(
        root,
        settings_file=settings_file,
        logs=settings_file.parent / "logs",
        env=env_without_override(),
    )


@pytest.fixture
def app(root, answers: Answers, tmp_path: Path, saves_root: Path):
    settings_file = tmp_path / "appdata" / "EngineSave" / "gui.json"
    window = make_app(root, GuiSettings(saves_root_override=str(saves_root)), settings_file)
    yield window
    finish(window)


def finish(window) -> None:
    if window.last_dialog is not None:  # a failed test may leave one open
        with contextlib.suppress(tkinter.TclError):
            window.last_dialog.close()
    window.stop_polling()
    if not window.closed:
        window.closed = True
        close_logging(window.panel_handler, window.file_handler)


def wait(app, timeout: float = 120.0) -> None:
    """Pump Tk events until the background job has finished and its callbacks ran."""
    deadline = time.monotonic() + timeout
    while app.runner.busy and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    app.root.update()
    assert not app.runner.busy, "background job did not finish"


def settle(app, timeout: float = 120.0) -> None:
    """Like ``wait``, and also until the open dialog's automatic work (D32) has run: a job
    queued for the runner, a preview or check waiting for typing to pause."""

    def pending() -> bool:
        dialog = app.last_dialog
        return app.runner.busy or (
            dialog is not None
            and (
                getattr(dialog, "waiting", False)
                or getattr(dialog, "pending_preview", None) is not None
                or getattr(dialog, "pending_check", None) is not None
            )
        )

    deadline = time.monotonic() + timeout
    while pending() and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    app.root.update()
    assert not pending(), "automatic work did not finish"


def tree_rows(tree) -> list[tuple[str, ...]]:
    return [tuple(str(v) for v in tree.item(iid, "values")) for iid in tree.get_children()]


def game_rows(view) -> list[tuple[str, ...]]:
    """The game rows of the scan page in their order, inside their groups (D54), folded away
    or not."""
    tree = view.games
    return [tuple(str(v) for v in tree.item(iid, "values")) for iid in view.filtered()]


def scan_groups(view) -> list[tuple[str, bool]]:
    """``(title, open)`` of the scan page's groups (D54)."""
    tree = view.games
    return [
        (str(tree.item(g, "values")[2]), bool(tree.item(g, "open"))) for g in tree.get_children()
    ]


def record_rows(library) -> list[tuple[str, ...]]:
    """The record rows of the library page, inside its two groups (D43)."""
    tree = library.record_tree
    return [
        tuple(str(v) for v in tree.item(iid, "values"))
        for group in tree.get_children()
        for iid in tree.get_children(group)
        if iid in library.records
    ]


def labels(widget) -> list[str]:
    return [str(w.cget("text")) for w in widget.winfo_children() if w.winfo_class() == "TLabel"]


def press_enter(dialog) -> None:
    """What the dialog's Enter binding does. A hidden test window has no keyboard focus, so
    a generated key event would not arrive; check the binding and call its handler."""
    assert str(dialog.bind("<Return>")), "Enter is bound"
    dialog.press_primary()


def press_escape(dialog) -> None:
    assert str(dialog.bind("<Escape>")), "Esc is bound"
    dialog.cancel()


def log_panel(app) -> str:
    app.root.update()
    app._drain_log()  # the panel is otherwise filled by the next 100 ms poll
    return app.log_text.get("1.0", "end")


def test_window_scan_backup_verify_restore_export(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    from enginesave.gui.views.dialogs import (
        BackupPreviewDialog,
        BackupResultDialog,
        ExportDialog,
        RestoreDialog,
        SettingsDialog,
    )

    assert app.root.title().startswith("EngineSave") and app.root.minsize() == (1000, 650)
    assert "存档寻迹" not in app.root.title(), "D37: only EngineSave"
    assert [app.notebook.tab(t, "text") for t in app.notebook.tabs()] == ["扫描与备份", "备份库"]
    assert app.notice_visible and app.notice_clear.winfo_manager() == "pack"
    assert app.notice_var.get() == f"全局存档根目录已改为：{saves_root}（仅用于测试）"
    assert app.scan_view.guide.winfo_manager() == "pack"
    guide = app.scan_view.guide_var.get()
    assert guide.startswith("首次使用，按这六步来：") and "→ 1." in guide
    assert "5. 点「备份已勾选项」" in guide and "6." in guide

    # empty states
    app.scan()
    assert any("扫描根目录" in m for m in answers.messages("showinfo"))
    app.backup_checked()
    assert any("请先扫描" in m for m in answers.messages("showinfo"))

    built = build_f1(games_root, saves_root)
    build_f5(games_root, saves_root)
    build_f8(saves_root)
    answers.directory = str(games_root)
    app.add_root()
    assert app.settings.scan_roots == [str(games_root)]
    app.scan()
    wait(app)
    assert app.report is not None and len(app.report.games) == 2
    rows = game_rows(app.scan_view)
    assert [(r[0], r[1]) for r in rows] == [  # D54: the groups' order
        (state.CHECKED, "CONFIRMED"),
        (state.UNCHECKED, "CANDIDATE"),
    ]
    assert scan_groups(app.scan_view) == [("有新存档可备份（1）", True), ("待确认（1）", True)]
    assert app.scan_view.unlinked_link.cget("text") == "另有 2 个孤立存档目录…"  # D42
    app.scan_view.toggle_game("g0")
    assert app.scan_view.games.set("g0", "check") == state.CHECKED
    app.scan_view.toggle_game("g0")
    assert app.scan_view.counts.get() == "已勾选 1 款游戏"

    app.backup_checked()
    assert any("备份库" in m for m in answers.messages("showinfo")), "no library yet"
    guide = app.scan_view.guide_var.get()
    assert "→ 1." in guide and "✔ 2." in guide, "no library yet, scan root added"
    assert app.set_repo(tmp_path / "library")
    wait(app)
    assert (tmp_path / "library" / "catalog.json").is_file()
    assert app.scan_view.guide_var.get() == (
        "已勾选 1 款游戏，点击「备份已勾选项」直接开始，或「预览…」先看将复制的内容。"
    ), "after a scan the banner says what is ticked"
    toggle = app.scan_view.toggle_button
    assert toggle.cget("text") == "全选", "some rows ticked: the one button ticks them all"
    toggle.invoke()
    assert app.scan_view.counts.get() == "已勾选 2 款游戏", "全选 ticks games only (D42)"
    assert toggle.cget("text") == "全不选"
    toggle.invoke()
    assert app.scan_view.guide_var.get().startswith("还没有勾选")
    assert toggle.cget("text") == "全选"
    app.scan_view.toggle_game("g1")

    orphans = app.scan_view.show_unlinked()  # the link: the orphaned folders (D42)
    assert orphans is not None and len(tree_rows(orphans.tree)) == 2
    assert orphans.count.get() == "已勾选 0 个孤立目录，连同扫描页勾选的 1 款游戏一起备份"
    orphans.toggle("u1")  # none ticked by default
    assert orphans.backup_button.cget("text") == "备份已勾选项", "the scan page's name (D47)"
    assert app.scan_view.counts.get() == "已勾选 1 款游戏、1 个孤立目录", "S-131"
    orphans.cancel()  # the tick stays and goes with the games

    app.preview_backup()  # 「预览…」 still shows the plan first
    wait(app)
    preview = app.last_dialog
    assert isinstance(preview, BackupPreviewDialog)
    assert len(preview.tree.get_children()) == 2, "S-131: the game and the orphaned folder"
    assert preview.primary_button is preview.start_button
    preview.cancel()
    assert app.last_dialog is None

    assert str(app.scan_view.backup_button.cget("default")) == "active"
    app.scan_view.backup_button.invoke()  # 「备份已勾选项」: one click, no preview dialog
    wait(app)
    result = app.last_dialog
    assert isinstance(result, BackupResultDialog)
    assert [r[0] for r in tree_rows(result.tree)] == ["OK", "OK"]
    texts = labels(result.body)
    assert any(t.startswith("下一步：到“备份库”页点“校验”") and "清理…" in t for t in texts)
    panel = log_panel(app)
    assert "开始备份到" in panel and "2 项" in panel and "Readable Game · local：" in panel
    assert result.primary_button is not None and result.primary_button.cget("text") == "关闭"
    press_enter(result)  # Enter closes the result (D32)
    assert app.last_dialog is None
    wait(app)
    library = app.library
    rows = record_rows(library)
    assert sorted(r[1] for r in rows) == ["OrphanTwo", "Readable Game"]
    assert {r[0] for r in rows} == {state.UNCHECKED}
    groups = [library.record_tree.item(g, "values")[1] for g in library.record_tree.get_children()]
    assert groups == ["本体存在（1）", "本体不在（1）"], "D43: the orphaned folder has no install"

    readable = next(iid for iid, row in library.records.items() if row.name == "Readable Game")
    library.record_tree.selection_set(readable)
    app.root.update()
    assert len(tree_rows(library.snapshot_tree)) == 1
    library.verify()  # nothing ticked: the selected record
    wait(app)
    assert [r[0] for r in tree_rows(library.verify_tree)] == ["OK"]
    library.toggle_button.invoke()  # 全选 → 校验 instead of 「校验全部」 (D33 ①)
    assert library.toggle_button.cget("text") == "全不选"
    library.verify()
    wait(app)
    assert [r[0] for r in tree_rows(library.verify_tree)] == ["OK", "OK"]
    library.toggle_button.invoke()  # 全不选: the actions are about the selected record again
    assert library.check_count.get() == "已勾选 0 条"

    asked_before = len(answers.messages("ask"))
    library.restore()
    restore = app.last_dialog
    assert isinstance(restore, RestoreDialog)
    settle(app)  # the preview runs by itself (D32 ①)
    assert restore.plan is not None
    assert restore.game_root.get() == str(built.root), "the install that still exists"
    target = reinstall_copy(built.root, tmp_path / "restore-target")  # the game, unpacked again
    restore.game_root.set(str(target))  # any change previews again once typing pauses
    assert restore.plan is None and restore.restore_button.instate(["disabled"])
    settle(app)
    targets = {row[0]: row[1] for row in tree_rows(restore.preview_tree)}
    assert targets["local"] == str(target / "game" / "saves")
    assert restore.restore_button.cget("text").startswith("恢复（覆盖 ")
    assert restore.restore_button.cget("text").endswith("先建安全快照）")
    restore.press_primary()  # the overwrite count was the confirmation (D32 ⑤)
    wait(app)
    assert "已恢复 12 个文件，失败 0 个" in restore.result.get()
    assert len(answers.messages("ask")) == asked_before, "no extra yes/no before restoring"
    for name, data in built.local_files.items():
        assert (target / "game" / "saves" / name).read_bytes() == data
    restore.cancel()
    wait(app)

    library.record_tree.selection_set(readable)
    app.root.update()
    library.export()
    export = app.last_dialog
    assert isinstance(export, ExportDialog) and export.single, "one record: a file"
    assert Path(export.target.get()).parent == documents_dir(), "the first time: Documents"
    assert export.primary_button is export.export_button
    archive = tmp_path / "exports" / "readable.zip"
    export.target.set(str(archive))
    export.press_primary()
    wait(app)
    assert export.result_path == archive and zipfile.is_zipfile(archive)
    assert "已导出并逐个校验" in export.result.get() and "路径已匿名化" in export.result.get()
    with zipfile.ZipFile(archive) as zf:
        header = json.loads(zf.read("EngineSave-export.json"))
    assert header["paths"] == "anonymized" and header["game"]["installs"] == []
    export.target.set(str(games_root / "inside-a-scan-root.zip"))
    export.export()
    wait(app)
    assert any("must not be inside" in m for m in answers.messages("showerror"))
    export.cancel()
    assert app.settings.last_export_dir == str(archive.parent)
    library.export()
    again = app.last_dialog
    assert Path(again.target.get()).parent == archive.parent, "the last export folder"
    again.cancel()

    app.open_settings()
    settings = app.last_dialog
    assert isinstance(settings, SettingsDialog)
    assert not hasattr(settings, "auto_scan"), "scanning at start-up was withdrawn in 0.6.0"
    settings.candidates.set(True)
    settings.press_primary()
    assert app.notice_visible, "the override is still set"
    app.clear_override()
    assert not app.notice_visible and app.settings.saves_root_override is None

    panel = log_panel(app)
    for text in ("扫描完成", "备份完成", "恢复完成", "导出完成", "已恢复使用真实的全局存档根目录"):
        assert text in panel
    app.on_close()
    saved, note = load_settings(app.settings_file)
    assert note is None and saved.include_candidates and saved.repo_path.endswith("library")
    assert saved.saves_root_override is None
    log_text = (app.settings_file.parent / "logs" / "gui.log").read_text(encoding="utf-8")
    assert "备份完成" in log_text and "图形界面已关闭" in log_text
    assert answers.messages("showerror") == [
        m for m in answers.messages("showerror") if "must not be inside" in m
    ], "no other error was shown"


def test_start_up_reads_the_library_but_never_scans(
    root, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D32 ⑦ withdrawn in 0.6.0: the window comes up first, reads the library in the
    background and scans only when asked (打开 → 扫描: 1 click); a 0.5.0 ``gui.json`` with
    ``auto_scan`` still loads."""
    from enginesave.backup import prepare_repository
    from enginesave.gui.app import App, apply_theme

    build_f1(games_root, saves_root)
    library = tmp_path / "library"
    prepare_repository(library, [games_root], saves_root_override=saves_root)
    settings_file = tmp_path / "start" / "gui.json"
    ready = GuiSettings(
        scan_roots=[str(games_root)], repo_path=str(library), saves_root_override=str(saves_root)
    )
    save_settings(ready, settings_file)
    written = json.loads(settings_file.read_text(encoding="utf-8"))
    settings_file.write_text(json.dumps({**written, "auto_scan": True}), encoding="utf-8")
    apply_theme(root)
    window = App(
        root, settings_file=settings_file, logs=tmp_path / "logs", env=env_without_override()
    )
    try:
        assert not window.runner.busy, "nothing runs before the window is up"
        root.update()  # the window is drawn; reading the library starts once it is idle
        wait(window)
        assert window.library.repo is not None and window.report is None, "no scan by itself"
        assert "开始扫描" not in log_panel(window) and window.settings == ready
        clicks = Clicks(answers)
        clicks.press(window.scan_button)  # 日常打开后看到结果: 打开 → 扫描
        wait(window)
        assert clicks.count == 1 and [g.display_name for g in window.report.games] == [
            "Readable Game"
        ]
    finally:
        finish(window)


@pytest.fixture
def recycle_bin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Rule 16: a fake recycle bin that only ever removes folders below ``tmp_path``."""
    import ctypes
    import shutil

    from enginesave import cleanup
    from enginesave.fsutil import is_within

    moved: list[str] = []

    def fake(operation: cleanup.SHFILEOPSTRUCTW) -> int:
        path = ctypes.wstring_at(operation.pFrom)
        assert is_within(path, tmp_path), "the fake only ever removes temporary folders"
        moved.append(path)
        shutil.rmtree(path)
        return 0

    monkeypatch.setattr(cleanup.windows, "shell_file_operation", fake)
    monkeypatch.setattr(cleanup.windows, "has_recycle_bin", lambda _anchor: True)
    monkeypatch.setattr(cleanup.windows, "drive_type", lambda _anchor: cleanup.DRIVE_FIXED)
    return moved


def backed_up_window(app, answers: Answers, games_root: Path, tmp_path: Path):
    """Scan root and library chosen, scanned, and 「备份已勾选项」 pressed: the result dialog."""
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "library")
    wait(app)
    app.scan()
    wait(app)
    app.backup_checked()
    wait(app)
    return app.last_dialog


def test_cleanup_after_a_complete_backup_takes_two_clicks(
    app,
    answers: Answers,
    recycle_bin: list[str],
    games_root: Path,
    saves_root: Path,
    tmp_path: Path,
) -> None:
    """清理… → 移到回收站: checked on opening, what passed is ticked, Enter recycles."""
    from enginesave.gui.views.dialogs import BackupResultDialog, CleanupDialog

    built = build_f1(games_root, saves_root)
    result = backed_up_window(app, answers, games_root, tmp_path)
    assert isinstance(result, BackupResultDialog)
    played = built.local_dir / "9-9-LT9.save"
    played.write_bytes(b"played after the backup")
    offer = [w for w in result.footer.winfo_children() if w.cget("text") == "清理…"]
    assert len(offer) == 1, "every backup is complete, so cleanup is offered"
    offer[0].invoke()
    dialog = app.last_dialog
    assert isinstance(dialog, CleanupDialog)
    assert dialog.clean_button.instate(["disabled"]), "nothing can be removed while checking"
    assert dialog.result.get() == state.CHECKING
    settle(app)
    rows = tree_rows(dialog.tree)
    assert [(r[2], r[7]) for r in rows] == [("游戏目录", "拒绝"), ("全局存档目录", "通过")]
    assert [r[0] for r in rows] == ["", state.CHECKED], "what passed is ticked (D32 ②)"
    assert "not in the backup" in rows[0][8] and dialog.tree.item("c0", "tags") == ("refused",)
    assert [r[4] for r in rows] == ["打开", "打开"], "D36: 「打开」 beside every path"
    dialog.toggle("c0")
    assert "c0" not in dialog.ticked, "a refused folder cannot be ticked"
    assert dialog.clean_button.cget("text").startswith("移到回收站（1 项，")
    press_escape(dialog)
    assert app.last_dialog is None and recycle_bin == [], "Esc deletes nothing"
    played.unlink()

    library = app.library
    library.record_tree.selection_set(next(iter(library.records)))
    app.root.update()
    library.cleanup()  # from the library page: 选记录 → 清理… → 移到回收站
    dialog = app.last_dialog
    settle(app)
    assert [r[0] for r in tree_rows(dialog.tree)] == [state.CHECKED, state.CHECKED]
    # A never-shown window records no focus in Tk, so check what show() focuses.
    assert dialog.primary_button is dialog.clean_button, "the main button gets the focus"
    assert dialog.clean_button.cget("text").startswith("移到回收站（2 项，")
    press_enter(dialog)  # Enter: no name to type for the bin
    wait(app)
    assert answers.prompts == [] and recycle_bin == [str(built.root), str(built.global_dir)]
    assert not built.root.exists() and not built.global_dir.exists()
    outcomes = [r[7] for r in tree_rows(dialog.tree)]
    assert [o.split(" · ")[0] for o in outcomes] == ["已移到回收站", "已移到回收站"]  # D41 4
    assert all(" · " in o and o.endswith(("KB", "MB", "B")) for o in outcomes)
    assert dialog.result.get().startswith("已移到回收站 2 项 · ")
    assert "清空前不释放空间" in dialog.result.get()
    assert dialog.clean_button.instate(["disabled"]), "check again before the next cleanup"
    wait(app)
    assert [r[2] for r in record_rows(library)] == ["游戏"], "D51: the kind and nothing else"
    assert not library.records["r0"].installed
    tree = library.record_tree
    assert [str(tree.item(g, "values")[1]) for g in tree.get_children()] == [
        "本体存在（0）",
        "本体不在（1）",
    ]
    assert app.settings.cleanup_method == "recycle"
    dialog.cancel()
    assert answers.messages("showerror") == []


def test_permanent_cleanup_is_one_click_and_the_library_checks_again(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """清理… → 永久删除 → 永久删除 N 项 (D40: no name to type, a red warning instead); the
    library still checks every folder again right before removing it."""
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.gui.views.dialogs import CleanupDialog
    from enginesave.repo import Repo

    built = build_f1(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "library", saves_root_override=saves_root)
    execute_backup(plan, Repo.open_or_create(plan.dest))
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "library")
    wait(app)
    library = app.library
    assert find_button(library, "清理…").instate(["disabled"]), "nothing ticked or selected"
    library.cleanup()
    assert any("先勾选记录" in m for m in answers.messages("showinfo"))
    library.record_tree.selection_set(next(iter(library.records)))
    app.root.update()
    assert find_button(library, "清理…").instate(["!disabled"])
    library.cleanup()
    dialog = app.last_dialog
    assert isinstance(dialog, CleanupDialog)
    settle(app)
    assert not dialog.warning_shown, "the recycle bin needs no warning"
    dialog.method.set("permanent")  # remembered, and checked again for this method
    assert app.settings.cleanup_method == "permanent" and dialog.warning_shown
    settle(app)
    text = dialog.clean_button.cget("text")
    assert text.startswith("永久删除 2 项（") and text.endswith("无法还原）")

    dialog.toggle("c0")  # only the global folder: its game folder would stay
    dialog.press_primary()
    wait(app)
    assert (
        built.global_dir.is_dir()
        and tree_rows(dialog.tree)[1][7] == "失败：再次检查没有通过，未删除"
    )
    assert "still used by" in tree_rows(dialog.tree)[1][8], "the reason is in the row (D41 4)"
    assert "再次检查没有通过、未删除：Readable Game" in dialog.result.get()

    dialog.check()  # 「重新检查」
    settle(app)
    assert [r[0] for r in tree_rows(dialog.tree)] == [state.CHECKED, state.CHECKED]
    (built.local_dir / "new.save").write_bytes(b"saved after the check")
    dialog.press_primary()
    wait(app)
    assert built.root.is_dir() and "再次检查没有通过" in dialog.result.get()
    (built.local_dir / "new.save").unlink()

    dialog.check()
    settle(app)
    dialog.press_primary()
    wait(app)
    assert not built.root.exists() and not built.global_dir.exists()
    assert [r[7].split(" · ")[0] for r in tree_rows(dialog.tree)] == ["已永久删除", "已永久删除"]
    assert "回收站" not in dialog.result.get()
    assert answers.prompts == [] and answers.messages("ask") == [], "D40: nothing to type"
    dialog.method.set("recycle")
    assert not dialog.warning_shown
    settle(app)
    dialog.method.set("permanent")
    settle(app)
    dialog.cancel()
    reopened = load_settings(app.settings_file)[0]
    assert reopened.cleanup_method == "permanent", "the method is remembered"
    assert answers.messages("showerror") == []


@pytest.fixture
def full_bin_window(app, answers: Answers, synthetic_bin, games_root, saves_root, tmp_path, caplog):
    """D41: backed up; the game folder went into the (synthetic) recycle bin with an earlier
    cleanup; now the global folder is too big for the room left. The cleanup dialog is open
    and its check has run."""
    from enginesave.cleanup import GAME_ROOT, execute_cleanup, plan_cleanup
    from enginesave.gui.views.dialogs import CleanupDialog
    from enginesave.repo import Repo

    caplog.set_level("INFO", logger="enginesave.gui")
    built = build_f1(games_root, saves_root)
    backed_up_window(app, answers, games_root, tmp_path).cancel()
    repo = Repo.open(tmp_path / "library")
    (record,) = repo.load_catalog()["games"]
    first = plan_cleanup(repo, record, [(GAME_ROOT, None)], saves_root_override=saves_root)
    assert execute_cleanup(first).targets[0]["result"] == "recycled"
    saves = sum(p.stat().st_size for p in built.global_dir.rglob("*") if p.is_file())
    synthetic_bin.leave_room(saves - 1)
    dialog = CleanupDialog(app, repo, [record["game_id"]])
    settle(app)
    return dialog, built


def cleanup_log(caplog: pytest.LogCaptureFixture, start: str) -> list[str]:
    """The log lines starting with ``start``, from the fixture's setup and the test itself."""
    records = [*caplog.get_records("setup"), *caplog.records]
    return [r.getMessage() for r in records if r.getMessage().startswith(start)]


def test_a_full_recycle_bin_offers_to_purge_enginesaves_earlier_items(
    app, full_bin_window, synthetic_bin, caplog: pytest.LogCaptureFixture
) -> None:
    """D41: what does not fit waits; the dialog ticks EngineSave's earlier item in the bin,
    shows what that makes room for, and 「继续清理」 purges it and recycles the rest. One log
    line per check and per run (D41 3)."""
    from enginesave.gui.views.dialogs import CleanupDialog
    from enginesave.gui.views.dialogs.cleanup import FullBinDialog

    dialog, built = full_bin_window
    rows = tree_rows(dialog.tree)
    assert [(r[2], r[7]) for r in rows] == [("游戏目录", "不存在"), ("全局存档目录", "通过")]
    assert rows[0][8].startswith("已移到回收站 · ") and rows[0][0] == "" and rows[0][5] == ""
    assert rows[1][0] == state.CHECKED and rows[1][8].endswith(state.NOTE_NEEDS_ROOM)
    assert dialog.bins.get().startswith("回收站 ") and " 剩余 " in dialog.bins.get()
    assert dialog.result.get() == "检查完成：1 项可清理（已勾选），1 项不存在。"
    dialog.press_primary()
    wait(app)
    full = app.last_dialog
    assert isinstance(full, FullBinDialog)
    assert [r[0] for r in tree_rows(full.ours_tree)] == [state.CHECKED], "ours: all ticked"
    assert tree_rows(full.ours_tree)[0][2] == str(built.root)
    assert [(r[0], r[2]) for r in tree_rows(full.waiting_tree)] == [
        (state.UNCHECKED, str(built.global_dir))
    ]
    assert "待放入 1 项" in full.heading.get() and "够了" in full.status.get()
    assert full.go_button.instate(["!disabled"])
    full.toggle("a0")  # keep EngineSave's earlier item: not enough room
    assert "还差" in full.status.get() and full.go_button.instate(["disabled"])
    full.toggle("b0")  # delete the waiting folder permanently instead: enough
    assert "够了" in full.status.get() and full.go_button.instate(["!disabled"])
    full.toggle("b0")
    full.toggle("a0")
    full.press_primary()
    wait(app)
    assert app.last_dialog is dialog and isinstance(dialog, CleanupDialog)
    assert (
        not built.global_dir.exists() and synthetic_bin.pairs() and len(synthetic_bin.pairs()) == 1
    )
    assert tree_rows(dialog.tree)[1][7].startswith("已移到回收站 · ")
    assert dialog.waiting_items == [] and dialog.clean_button.instate(["disabled"])
    assert len(cleanup_log(caplog, "清理检查：")) == 1
    first, second = cleanup_log(caplog, "清理完成：")
    assert first.endswith("失败 0，未处理 1") and "1 项进回收站" in second
    assert second.endswith("从回收站删除了 EngineSave 之前放入的 1 项（" + second.split("（")[-1])
    assert not cleanup_log(caplog, "清理 recycled"), "no line per folder any more"
    dialog.cancel()


def test_later_leaves_the_rest_and_the_main_button_goes_on(
    app, full_bin_window, synthetic_bin
) -> None:
    """「稍后再说」: the rest waits (未处理, not failed); the main button becomes 「继续清理」,
    which goes on at once once the bin has room (emptied by hand here)."""
    from enginesave.gui.views.dialogs.cleanup import FullBinDialog

    dialog, built = full_bin_window
    dialog.press_primary()
    wait(app)
    full = app.last_dialog
    assert isinstance(full, FullBinDialog)
    full.cancel()  # 稍后再说
    assert app.last_dialog is dialog and built.global_dir.is_dir()
    assert tree_rows(dialog.tree)[1][7] == state.LEFT_FOR_LATER
    assert dialog.clean_button.cget("text") == "继续清理（1 项）"
    assert dialog.clean_button.instate(["!disabled"])
    synthetic_bin.leave_room(1024**3)  # the user emptied (or enlarged) the recycle bin
    dialog.press_primary()
    wait(app)
    assert app.last_dialog is dialog, "enough room: no dialog, it goes on"
    assert not built.global_dir.exists() and dialog.waiting_items == []
    assert len(synthetic_bin.pairs()) == 2, "nothing was purged: it was not needed"
    dialog.cancel()


def test_batch_export_and_import_from_the_library_page(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.gui.views.dialogs import ExportDialog, ImportDialog
    from enginesave.repo import Repo
    from enginesave.verify import verify_repo

    build_f1(games_root, saves_root)
    build_f8(saves_root)
    plan = prepare_backup(
        [games_root], tmp_path / "library", saves_root_override=saves_root, include_unlinked=[]
    )
    execute_backup(plan, Repo.open_or_create(plan.dest))
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "library")
    wait(app)
    library = app.library
    assert library.check_count.get() == "已勾选 0 条"
    export_button = find_button(library, "导出…")
    assert export_button.instate(["disabled"]), "nothing ticked or selected: nothing to export"
    library.export()
    assert any("勾选" in m for m in answers.messages("showinfo")), "nothing ticked yet"
    library.toggle_all()
    assert {r[0] for r in record_rows(library)} == {state.CHECKED}
    assert library.toggle_button.cget("text") == "全不选" and export_button.instate(["!disabled"])
    first = next(iter(library.records))
    library.toggle_check(first)
    assert library.record_tree.set(first, "check") == state.UNCHECKED
    assert library.check_count.get() == "已勾选 2 条"
    assert library.toggle_button.cget("text") == "全选", "partly ticked: 全选"
    library.toggle_check(first)
    library.reload()
    wait(app)
    assert library.check_count.get() == "已勾选 3 条", "ticks survive a reload"

    folder = tmp_path / "exports" / "batch"
    app.settings.last_export_dir = str(folder)  # as if exported there before
    library.export()
    dialog = app.last_dialog
    assert isinstance(dialog, ExportDialog) and not dialog.single, "several: a folder"
    assert dialog.export_button.cget("text") == "导出到 batch", "the last folder, one click"
    assert dialog.open_button.instate(["disabled"])
    dialog.press_primary()
    wait(app)
    assert [r[0] for r in tree_rows(dialog.tree)] == ["已导出"] * 3
    assert "已导出 3 个、跳过 0 个、失败 0 个" in dialog.result.get()
    assert dialog.result_folder == folder and not dialog.open_button.instate(["disabled"])
    dialog.press_primary()
    wait(app)
    assert [r[0] for r in tree_rows(dialog.tree)] == ["已跳过"] * 3
    assert {r[4] for r in tree_rows(dialog.tree)} == {state.SKIP_REASONS["the file already exists"]}
    dialog.overwrite.set(True)
    dialog.tokens.set(True)
    dialog.press_primary()  # no second confirmation for "覆盖" or the keys (D32 ④)
    wait(app)
    assert [r[0] for r in tree_rows(dialog.tree)] == ["已导出"] * 3
    assert answers.messages("ask") == []
    answers.directory = str(tmp_path / "exports" / "other")
    dialog.browse()  # 「更换文件夹…」
    assert dialog.export_button.cget("text") == "导出到 other"
    dialog.folder.set(str(games_root / "inside-a-scan-root"))
    dialog.press_primary()
    wait(app)
    assert any("must not be inside" in m for m in answers.messages("showerror"))
    dialog.cancel()
    assert app.settings.export_overwrite is True
    library.export()
    reopened = app.last_dialog
    assert reopened.overwrite.get() is True, "覆盖 is remembered"
    assert reopened.tokens.get() is False and reopened.keep_paths.get() is False
    assert reopened.folder.get() == str(folder)
    reopened.cancel()

    (folder / "zz-broken.zip").write_bytes(b"not a zip")
    target = tmp_path / "imported"
    assert app.set_repo(target)
    wait(app)
    assert library.check_count.get() == "", "another library: nothing ticked"
    library.import_archives()
    importer = app.last_dialog
    assert isinstance(importer, ImportDialog) and importer.import_button.instate(["disabled"])
    answers.open_file = str(next(folder.glob("EngineSave-*.zip")))
    importer.choose_zip()  # choosing the path checks it at once
    settle(app)
    assert (
        len(tree_rows(importer.tree)) == 1 and importer.import_button.cget("text") == "导入（1 个）"
    )
    answers.directory = str(folder)
    importer.choose_folder()
    settle(app)
    checks = [r[5].split("：")[0] for r in tree_rows(importer.tree)]
    assert checks == ["可以导入"] * 3 + ["无法导入"] and "3/4" in importer.result.get()
    assert importer.import_button.cget("text") == "导入（3 个）"
    asked = len(answers.messages("ask"))
    importer.press_primary()  # 导入… → 选路径 → 导入: nothing more to confirm
    wait(app)
    assert len(answers.messages("ask")) == asked
    rows = tree_rows(importer.tree)
    assert [r[5] for r in rows[:3]] == ["已导入（新记录）"] * 3 and rows[3][5].startswith("失败：")
    assert importer.result.get().startswith("已导入 3 个、失败 1 个")
    assert importer.import_button.instate(["disabled"])
    assert app.settings.last_import_dir == str(folder)
    wait(app)
    assert len(library.records) == 3
    importer.cancel()
    imported = Repo.open(target)
    assert all(check.clean for check in verify_repo(imported, imported.load_catalog()))
    assert [m for m in answers.messages("showerror") if "must not be inside" not in m] == []


# ------------------------------------------------------------------- click counts (M17 table)


class Clicks:
    """Counts what a user does: a button pressed, Enter, a radio button, a name typed.

    Choosing a path in a file dialog and selecting the record a library action works on are
    not counted, as in the plan's table (section 20, M17).
    """

    def __init__(self, answers: Answers) -> None:
        self.count = 0
        self.answers = answers
        typed = answers.type_name

        def counted(*args: Any, **options: Any) -> str | None:
            self.count += 1  # typing the name and pressing OK
            return typed(*args, **options)

        answers.type_name = counted  # type: ignore[method-assign]

    def press(self, button) -> None:
        assert button.instate(["!disabled"]), f"{button.cget('text')} is not available"
        button.invoke()
        self.count += 1

    def enter(self, dialog) -> None:
        press_enter(dialog)
        self.count += 1


def find_button(widget, text: str):
    """The button showing ``text`` (exactly, or as the start of the text) below ``widget``."""
    for child in widget.winfo_children():
        if child.winfo_class() == "TButton" and str(child.cget("text")).startswith(text):
            return child
        found = find_button(child, text)
        if found is not None:
            return found
    return None


@pytest.fixture
def typed_prompt(monkeypatch: pytest.MonkeyPatch, answers: Answers) -> Answers:
    """``simpledialog.askstring`` goes through ``answers.type_name`` looked up at call time,
    so a ``Clicks`` counter that wraps it sees every prompt."""
    from tkinter import simpledialog

    monkeypatch.setattr(simpledialog, "askstring", lambda *a, **o: answers.type_name(*a, **o))
    return answers


def ready_library(app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path):
    """F1 backed up into a library the window has open, the record selected; scanned."""
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.repo import Repo

    built = build_f1(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "library", saves_root_override=saves_root)
    execute_backup(plan, Repo.open_or_create(plan.dest))
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "library")
    wait(app)
    app.scan()
    wait(app)
    library = app.library
    library.record_tree.selection_set(next(iter(library.records)))
    app.root.update()
    return built, library


def test_the_library_page_overview_and_its_leftovers(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D56: the overview line under the library's line. D55: leftovers of interrupted runs
    are a link; the dialog ticks the old one, greys the recent one, and one click deletes
    and collects the object store; the page is read again."""
    import os

    from enginesave.gui.views.dialogs import TmpDialog

    _built, library = ready_library(app, answers, games_root, saves_root, tmp_path)
    overview = library.overview.get()
    assert overview.startswith("1 条记录（本体存在 1、本体不在 0） · 1 个快照 · 占用 ")
    assert "去重前 " in overview and "上次镜像 无" in overview
    assert not library.tmp_link.winfo_manager(), "no leftovers, no link"
    repo = library.repo
    game_id = next(iter(library.records.values())).game_id
    fresh = repo.snapshots_dir(game_id) / ".tmp-20261024T000000Z-abc123"  # being written
    fresh.mkdir()
    (fresh / "manifest.json").write_text("{}", encoding="utf-8")
    old = repo.object_tmp_dir() / "0123456789abcdef"  # left an hour ago
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"left behind")
    os.utime(old, (1_600_000_000, 1_600_000_000))
    library.reload()
    wait(app)
    assert (
        library.tmp_link.cget("text") == "清理临时文件夹（2）…" and library.tmp_link.winfo_manager()
    )
    library.clean_tmp()
    dialog = app.last_dialog
    assert isinstance(dialog, TmpDialog)
    settle(app)
    rows = {r[1]: r for r in tree_rows(dialog.tree)}
    assert rows[str(old)][0] == state.CHECKED and rows[str(fresh)][0] == ""
    assert rows[str(fresh)][5] == "可能正在使用"
    assert dialog.delete_button.cget("text") == "删除（1 个）并回收对象库空间"
    press_enter(dialog)
    wait(app)
    wait(app)  # the library page is read again
    assert not old.exists() and fresh.exists()
    assert dialog.summary.get().startswith("已删除 1 个临时文件夹，释放 ")
    assert "对象库这次没有回收" in dialog.summary.get(), "the recent one blocks the collection"
    assert library.tmp_link.cget("text") == "清理临时文件夹（1）…"
    dialog.cancel()
    # D59 4: an old snapshot folder is greyed too while something in objects/.tmp is recent
    for path in (fresh / "manifest.json", fresh):
        os.utime(path, (1_600_000_000, 1_600_000_000))
    (repo.object_tmp_dir() / "fedcba9876543210").write_bytes(b"a large file, still going")
    library.reload()
    wait(app)
    library.clean_tmp()
    dialog = app.last_dialog
    settle(app)
    rows = {r[1]: r for r in tree_rows(dialog.tree)}
    assert rows[str(fresh)][0] == "" and rows[str(fresh)][5] == "可能正在使用"
    assert dialog.delete_button.instate(["disabled"]), "nothing can be ticked"
    dialog.cancel()


def test_mirror_to_a_folder_from_the_library_page(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D57: 「镜像到…」 asks for a folder the first time, checks it (another library is
    refused), mirrors and verifies in one click, remembers the folder and the date; the next
    time the folder is offered and only what is new is copied."""
    from enginesave.gui.views.dialogs import MirrorDialog
    from enginesave.repo import Repo

    _built, library = ready_library(app, answers, games_root, saves_root, tmp_path)
    other = Repo.open_or_create(tmp_path / "other")
    catalog = other.load_catalog()
    catalog["games"].append({"game_id": "12345678-1234-1234-1234-123456789abc"})
    other.save_catalog(catalog)
    find_button(library, "镜像到…").invoke()
    dialog = app.last_dialog
    assert isinstance(dialog, MirrorDialog)
    assert dialog.target_var.get() == state.MIRROR_NONE and dialog.mirror_button.instate(
        ["disabled"]
    )
    answers.directory = str(other.root)
    dialog.choose()
    settle(app)
    assert dialog.check_var.get().startswith("不能用这个文件夹：")
    assert dialog.mirror_button.instate(["disabled"])
    answers.directory = str(tmp_path / "mirror")
    dialog.choose()
    settle(app)
    assert dialog.check_var.get().startswith(state.MIRROR_NEW)
    assert dialog.mirror_button.cget("text") == "镜像到 mirror"
    press_enter(dialog)
    wait(app)
    wait(app)  # the library page is read again
    assert dialog.result.get().startswith("镜像完成：") and "校验通过" in dialog.result.get()
    assert app.settings.mirror_folder == str(tmp_path / "mirror")
    assert "上次镜像 无" not in library.overview.get()
    dialog.cancel()
    find_button(library, "镜像到…").invoke()
    again = app.last_dialog
    settle(app)
    assert again.target_var.get() == str(tmp_path / "mirror")
    assert again.check_var.get().startswith(state.MIRROR_AGAIN)
    press_enter(again)
    wait(app)
    wait(app)
    assert "复制 0 个对象、0 个快照" in again.result.get()
    again.cancel()


def gated_copy(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """The worker's first file copy waits for ``gate`` and signals ``inside``: a job held at
    a known point, so the test can press 「取消」 while it runs."""
    from enginesave import store

    inside, gate = threading.Event(), threading.Event()
    real = store.copy_and_verify

    def copy(*args: Any) -> Any:
        if not inside.is_set():
            inside.set()
            assert gate.wait(60), "the test never released the copy"
        return real(*args)

    monkeypatch.setattr(store, "copy_and_verify", copy)
    return inside, gate


def wait_for(app, event: threading.Event, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not event.is_set() and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert event.is_set()


def test_cancel_a_backup_while_it_copies(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D58: 「取消」 shows beside the progress bar while a backup runs; pressed during a
    file, the backup finishes that file, drops the unfinished snapshot and reports what was
    done; nothing half-done is left in the library."""
    from enginesave.gui.views.dialogs import BackupResultDialog
    from enginesave.repo import Repo

    build_f1(games_root, saves_root)
    build_second(games_root, saves_root)
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "library")
    wait(app)
    app.scan()
    wait(app)
    assert not app.cancel_button.winfo_manager(), "nothing runs: no 「取消」"
    inside, gate = gated_copy(monkeypatch)
    app.scan_view.backup_button.invoke()
    wait_for(app, inside)
    assert app.cancel_button.winfo_manager() and app.cancel_button.cget("text") == "取消"
    app.cancel_task()
    assert app.cancel_button.cget("text") == "正在取消…" and app.stop_requested()
    gate.set()
    wait(app)
    dialog = app.last_dialog
    assert isinstance(dialog, BackupResultDialog)
    assert state.CANCELLED_BACKUP in labels(dialog.body)
    assert not app.cancel_button.winfo_manager()
    repo = Repo.open(tmp_path / "library")
    assert repo.stale_tmp_dirs() == [] and repo.load_catalog()["games"] == []
    assert "已取消备份：完成 0 项，未做 2 项" in log_panel(app)
    dialog.cancel()


# ----------------------------------------------------------------- 0.10.0 (D61, D62, D64)


def two_games_scanned(app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path):
    """F1 and a second game, a new library chosen, scanned: both in the first group."""
    build_f1(games_root, saves_root)
    build_second(games_root, saves_root)
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "library")
    wait(app)
    app.scan()
    wait(app)
    return app.scan_view


EARLIER = "2026-10-20T00:00:00Z"


def mirrored_before(app, tmp_path: Path, folder: Path) -> None:
    """D64: the library ``tmp_path/library`` was mirrored to ``folder`` with success once
    (``mirrors.json``), which the automatic mirror follows."""
    from enginesave.mirror import record_mirror

    record_mirror(app.settings_file.parent, tmp_path / "library", folder, EARLIER)


def one_click_backup(app):
    """「备份已勾选项」, until everything it set off (the library read again, an automatic
    mirror) has finished; the result dialog."""
    from enginesave.gui.views.dialogs import BackupResultDialog

    app.scan_view.backup_button.invoke()
    wait(app)
    wait(app)
    dialog = app.last_dialog
    assert isinstance(dialog, BackupResultDialog)
    return dialog


def test_games_just_backed_up_are_unticked(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D61 ①: the ticks are spent -- the games backed up move to the closed group unticked,
    so the next click does not take a second snapshot of the same saves."""
    view = two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    assert view.counts.get() == "已勾选 2 款游戏"
    one_click_backup(app).cancel()
    assert not any(app.selection.games.values())
    assert view.counts.get() == "已勾选 0 款游戏"
    assert scan_groups(view) == [("已备份、无新存档（2）", False)]
    assert "2 款刚备份完的游戏已取消勾选" in log_panel(app)


def test_ticks_in_a_closed_group_are_counted_and_named(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D61 ②: a tick that cannot be seen is said, in the count and on the group's title."""
    view = two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    one_click_backup(app).cancel()
    key = next(iter(app.selection.games))
    app.selection.games[key] = True
    view.refresh_checks()
    assert view.counts.get() == "已勾选 1 款游戏（其中 1 款在收起的组里）"
    assert scan_groups(view) == [("已备份、无新存档（2）（已勾选 1）", False)]
    view.set_group_open(state.GROUP_UNCHANGED, True)
    assert view.counts.get() == "已勾选 1 款游戏"
    assert scan_groups(view) == [("已备份、无新存档（2）", True)]


def test_cancelling_while_the_scan_compares_keeps_the_last_scan(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D61 ③: the comparison at the end of a scan has safe points between games; stopped
    there, nothing changes on the scan page."""
    from enginesave.gui.state import changes as changes_module

    view = two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    one_click_backup(app).cancel()
    first, before = app.report, scan_groups(view)
    real = changes_module.save_change

    def stop_after_the_first(*args: Any, **kwargs: Any) -> Any:
        app.stop_event.set()  # 「取消」 pressed while the first game is compared
        return real(*args, **kwargs)

    monkeypatch.setattr(changes_module, "save_change", stop_after_the_first)
    app.scan()
    wait(app)
    assert app.report is first and scan_groups(view) == before
    assert "已取消扫描" in log_panel(app)


def test_another_library_drops_what_the_scan_knew_of_the_old_one(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D61 ④: after choosing another library no row says 已在备份库 any more; each says to
    scan again, until it is scanned again."""
    view = two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    one_click_backup(app).cancel()
    assert all(g.known_game_id for g in app.report.games)
    assert app.set_repo(tmp_path / "other")
    wait(app)
    assert all(g.known_game_id is None and not g.link_suggestions for g in app.report.games)
    notes = [row[-1] for row in game_rows(view)]
    assert notes and all(state.LIBRARY_SWITCHED in n and "已在备份库" not in n for n in notes)
    app.scan()
    wait(app)
    assert not any(state.LIBRARY_SWITCHED in row[-1] for row in game_rows(view))


def test_a_backup_is_mirrored_automatically_and_the_mirror_is_a_library(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D64: with a mirror folder the backup that worked is mirrored in the background, the
    result dialog's last line says so, and it is the last mirror. D62 ④: the mirror chosen
    as the library shows the same records in the same groups."""
    from enginesave.mirror import last_mirror
    from enginesave.repo import Repo

    view = two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    mirrored_before(app, tmp_path, mirror_dir)
    dialog = one_click_backup(app)
    text = dialog.mirror_var.get()
    assert text.startswith(f"已镜像到 {mirror_dir}：复制 ") and text.endswith("，校验通过。")
    assert dialog.mirror_stop is None and "自动镜像：" in log_panel(app)
    library_root = tmp_path / "library"
    assert last_mirror(app.settings_file.parent, library_root) is not None
    assert len(Repo.open(mirror_dir).load_catalog()["games"]) == 2
    groups = [
        app.library.record_tree.item(g, "values")[1] for g in app.library.record_tree.get_children()
    ]
    dialog.cancel()
    assert app.set_repo(mirror_dir)
    wait(app)
    mirrored = [
        app.library.record_tree.item(g, "values")[1] for g in app.library.record_tree.get_children()
    ]
    assert mirrored == groups and len(record_rows(app.library)) == 2
    assert app.set_repo(library_root)
    wait(app)
    assert view.counts.get().startswith("已勾选 ")


def test_no_automatic_mirror_without_a_folder_or_with_the_setting_off(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    (tmp_path / "mirror").mkdir()
    app.settings.mirror_folder = str(tmp_path / "mirror")  # chosen, never mirrored with success
    dialog = one_click_backup(app)
    assert dialog.mirror_var.get() == "" and not dialog.mirror_label.winfo_manager()
    assert "未自动镜像：这个备份库还没有成功镜像过" in log_panel(app), "D66 9: said why"
    dialog.cancel()
    mirrored_before(app, tmp_path, tmp_path / "mirror")
    app.settings.auto_mirror = False
    save = next((games_root / "Second Game" / "game" / "saves").glob("*.save"))
    save.write_bytes(save.read_bytes() + b" more")
    app.scan()
    wait(app)
    before = log_panel(app)
    dialog = one_click_backup(app)
    assert dialog.mirror_var.get() == "" and "自动镜像" not in log_panel(app)[len(before) :]
    assert not any((tmp_path / "mirror").iterdir())


def test_an_unreachable_or_refused_mirror_folder_is_only_a_line(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D64: a folder that is not there (a drive not plugged in) or that the check refuses
    is a line in the log and the status bar; no message box, the backup's result stands."""
    from enginesave.repo import Repo

    view = two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    unplugged = tmp_path / "unplugged" / "Mirror"
    mirrored_before(app, tmp_path, unplugged)
    dialog = one_click_backup(app)
    assert f"未自动镜像：镜像文件夹不在（{unplugged}）" in log_panel(app)
    assert app.status_var.get().startswith("未自动镜像")
    assert dialog.mirror_var.get().startswith("未自动镜像：镜像文件夹不在")
    assert not unplugged.exists(), "never created"
    dialog.cancel()
    other = Repo.open_or_create(tmp_path / "other")
    catalog = other.load_catalog()
    catalog["games"].append({"game_id": "12345678-1234-1234-1234-123456789abc"})
    other.save_catalog(catalog)
    mirrored_before(app, tmp_path, other.root)
    app.selection.games = dict.fromkeys(app.selection.games, True)
    view.refresh_checks()
    dialog = one_click_backup(app)
    assert dialog.mirror_var.get().startswith("未自动镜像：") and "another library" in (
        dialog.mirror_var.get()
    )
    assert answers.messages("showerror") == [] and answers.messages("showinfo") == []


def test_an_automatic_mirror_is_cancelled_from_the_result_dialog(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D64: the result dialog stays usable while the mirror runs; its 「取消镜像」 stops it
    (the main window's 「取消」 is behind the dialog)."""
    from enginesave import mirror
    from enginesave.mirror import last_mirror

    two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    (tmp_path / "mirror").mkdir()
    mirrored_before(app, tmp_path, tmp_path / "mirror")
    inside, gate = threading.Event(), threading.Event()
    real = mirror._mirror_object

    def held(*args: Any, **kwargs: Any) -> None:
        if not inside.is_set():
            inside.set()
            assert gate.wait(60), "the test never released the mirror"
        real(*args, **kwargs)

    monkeypatch.setattr(mirror, "_mirror_object", held)
    app.scan_view.backup_button.invoke()
    wait_for(app, inside)
    dialog = app.last_dialog
    assert dialog.mirror_stop is not None and dialog.mirror_stop.winfo_manager()
    assert dialog.mirror_var.get().startswith("正在自动镜像到 ")
    dialog.mirror_stop.invoke()
    assert app.stop_requested() and dialog.mirror_stop.cget("text") == "正在取消…"
    gate.set()
    wait(app)
    assert dialog.mirror_var.get().startswith("自动镜像已取消") and dialog.mirror_stop is None
    assert last_mirror(app.settings_file.parent, tmp_path / "library")["at"] == EARLIER
    dialog.cancel()


def test_a_partial_backup_stays_ticked(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D61 ①: only a complete snapshot spends the tick; a game with a file that could not be
    read (partial) stays ticked for the next try."""
    from enginesave import store

    view = two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    real = store.copy_and_verify

    def locked_once(repo: Any, src: Path, target: Path) -> Any:
        if "Second" in str(src) and not getattr(locked_once, "done", False):
            locked_once.done = True  # type: ignore[attr-defined]
            raise store.SourceReadError("the file is locked by the game")
        return real(repo, src, target)

    monkeypatch.setattr(store, "copy_and_verify", locked_once)
    one_click_backup(app).cancel()
    ticked = {g.display_name: app.selection.games[state.game_key(g)] for g in app.report.games}
    assert ticked == {"Readable Game": False, "Second Game": True}
    assert view.counts.get().startswith("已勾选 1 款游戏")


def test_confirm_link_waits_for_a_rescan_after_another_library(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """After choosing another library the scan no longer knows the games' records there, so
    「确认 / 关联…」 asks for a scan first instead of offering only a new record."""
    from enginesave.gui.views.dialogs import ConfirmLinkDialog

    two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    one_click_backup(app).cancel()
    assert app.set_repo(tmp_path / "library")  # the same library chosen again
    wait(app)
    assert app.set_repo(tmp_path / "other") and app.set_repo(tmp_path / "library")
    wait(app)
    answers.shown.clear()
    assert app.confirm_link(app.report.games[0]) is None
    assert any(state.LIBRARY_SWITCHED in m for m in answers.messages("showinfo"))
    app.scan()
    wait(app)
    dialog = app.confirm_link(app.report.games[0])
    assert isinstance(dialog, ConfirmLinkDialog)
    settle(app)
    dialog.cancel()


def test_cleanup_from_the_result_dialog_stops_the_automatic_mirror(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D64: 「清理…」 in the result dialog closes it, and with it the mirror's own 「取消」;
    the mirror is stopped at its safe point first, so the cleanup check does not wait for a
    whole mirror behind a modal dialog (mirroring again goes on from there)."""
    from enginesave import mirror

    two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    (tmp_path / "mirror").mkdir()
    mirrored_before(app, tmp_path, tmp_path / "mirror")
    inside, gate = threading.Event(), threading.Event()
    real = mirror._mirror_object

    def held(*args: Any, **kwargs: Any) -> None:
        if not inside.is_set():
            inside.set()
            assert gate.wait(60), "the test never released the mirror"
        real(*args, **kwargs)

    monkeypatch.setattr(mirror, "_mirror_object", held)
    app.scan_view.backup_button.invoke()
    wait_for(app, inside)
    dialog = app.last_dialog
    clean = find_button(dialog, "清理…")
    assert clean is not None and dialog.mirror_stop is not None
    clean.invoke()
    assert app.stop_requested(), "the mirror was asked to stop"
    gate.set()
    settle(app)  # the mirror stops, then the cleanup dialog runs its own check
    assert "自动镜像已取消" in log_panel(app)
    app.last_dialog.cancel()


def test_closing_the_result_dialog_stops_the_automatic_mirror(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D66 9: closing the result dialog (its own 「取消镜像」 goes with it) stops the
    automatic mirror at its next safe point instead of letting it run on unseen."""
    from enginesave import mirror

    two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    (tmp_path / "mirror").mkdir()
    mirrored_before(app, tmp_path, tmp_path / "mirror")
    inside, gate = threading.Event(), threading.Event()
    real = mirror._mirror_object

    def held(*args: Any, **kwargs: Any) -> None:
        if not inside.is_set():
            inside.set()
            assert gate.wait(60), "the test never released the mirror"
        real(*args, **kwargs)

    monkeypatch.setattr(mirror, "_mirror_object", held)
    app.scan_view.backup_button.invoke()
    wait_for(app, inside)
    dialog = app.last_dialog
    assert dialog.mirror_stop is not None
    dialog.close()
    assert app.stop_requested(), "the mirror was asked to stop"
    gate.set()
    settle(app)
    panel = log_panel(app)
    assert "关闭结果窗口，先停下自动镜像" in panel and "自动镜像已取消" in panel


def test_a_failed_or_cancelled_backup_says_why_it_was_not_mirrored() -> None:
    from types import SimpleNamespace

    def result(cancelled: bool, *statuses: str) -> SimpleNamespace:
        items = [
            SimpleNamespace(status=s, snapshot_id="x" if s == "ok" else None) for s in statuses
        ]
        return SimpleNamespace(cancelled=cancelled, results=items)

    assert state.not_mirrored_reason(result(True, "ok")) == "备份时按了“取消”"
    assert state.not_mirrored_reason(result(False, "ok", "failed")) == "备份有失败的项"
    assert state.not_mirrored_reason(result(False)) == "这次没有写出快照"


def test_the_window_logs_why_a_backup_is_not_mirrored(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """M44 review (D66 9): a backup with a failed game is not mirrored, and the log says why;
    a result window closed before the mirror started stops it too."""
    from types import SimpleNamespace

    from enginesave.backup import BackupResult, GameResult

    two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    (tmp_path / "mirror").mkdir()
    mirrored_before(app, tmp_path, tmp_path / "mirror")
    app.settings.auto_mirror = True
    open_window = SimpleNamespace(winfo_exists=lambda: True)
    failed = BackupResult(results=[GameResult("Readable Game", "game", "failed")])
    app.auto_mirror(open_window, failed, tmp_path / "library")
    assert "未自动镜像：备份有失败的项" in log_panel(app) and not app.runner.busy
    worked = BackupResult(results=[GameResult("Readable Game", "game", "ok", snapshot_id="x")])
    closed = SimpleNamespace(winfo_exists=lambda: False)
    app.auto_mirror(closed, worked, tmp_path / "library")
    assert "未自动镜像：结果窗口已关闭" in log_panel(app) and not app.runner.busy
    assert not any((tmp_path / "mirror").iterdir())


def test_exclude_patterns_that_skip_everything_are_said() -> None:
    """D66 8, M44 review: by what a pattern skips below the root, not by its characters."""
    patterns = ["*", "*\\*", "E:\\Games\\*", "*\\novels", ".*", "?", "*.*", "E:\\Games", "Games"]
    warnings = state.exclude_warnings(patterns, ["E:\\Games"])
    everything = "会跳过扫描根目录 E:\\Games 下的所有文件夹"
    assert warnings == [
        f"“*”{everything}",
        f"“*\\*”{everything}",
        f"“E:\\Games\\*”{everything}",
        f"“E:\\Games”：{state.root_excluded_text('E:\\Games')}",
        f"“Games”：{state.root_excluded_text('E:\\Games')}",
    ]
    assert state.exclude_warnings(["*"], []) == ["“*”会跳过扫描根目录下的所有文件夹"]
    assert state.exclude_warnings([".*", "novels"], []) == []


def test_the_settings_dialog_warns_of_such_a_pattern_and_keeps_it(app, answers: Answers) -> None:
    """D66 8: 「确定」 with a pattern that skips everything says so; the settings are kept."""
    from enginesave.gui.views.dialogs import SettingsDialog

    app.open_settings()
    dialog = app.last_dialog
    assert isinstance(dialog, SettingsDialog)
    dialog.pattern.set("*")
    dialog.press_primary()
    assert any("下的所有文件夹" in m for m in answers.messages("showwarning"))
    assert app.settings.scan_excludes == ["*"]
    assert load_settings(app.settings_file)[0].scan_excludes == ["*"]


def test_a_scan_with_everything_skipped_says_so() -> None:
    from enginesave.models import ScanReport

    report = ScanReport.from_dict(
        {"format_version": 1, "generated_at": "x", "tool_version": "x", "roots": [],
         "renpy_saves_root": "x", "games": [], "unlinked": [],
         "skipped": [{"path": "E:\\a", "reason": "excluded"}], "warnings": []}
    )  # fmt: skip
    assert "全部被“扫描时跳过的文件夹”跳过" in state.scan_summary(report)
    report.skipped.append({"path": "E:\\b", "reason": "permission_denied"})
    assert "全部被" not in state.scan_summary(report), "only when every skip was the list's"
    report.skipped = []
    assert state.scan_summary(report) == "扫描完成：没有找到游戏，也没有孤立的全局存档目录。"


def test_a_cancel_during_the_backup_skips_the_automatic_mirror(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D64: 「取消」 pressed after the backup's last safe point (while its result is compared)
    lets the backup finish, but no mirror starts after it."""
    from enginesave.gui import scan_backup

    two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    (tmp_path / "mirror").mkdir()
    mirrored_before(app, tmp_path, tmp_path / "mirror")
    real = scan_backup.changes_after

    def pressed(*args: Any, **kwargs: Any) -> Any:
        app.stop_event.set()  # 「取消」, too late for the backup's safe points
        return real(*args, **kwargs)

    monkeypatch.setattr(scan_backup, "changes_after", pressed)
    dialog = one_click_backup(app)
    assert "未自动镜像：备份时按了“取消”" in log_panel(app)
    assert dialog.mirror_var.get() == "" and not any((tmp_path / "mirror").iterdir())
    dialog.cancel()


def test_temporary_folders_are_not_cleaned_while_a_job_runs(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D62 ④: 「清理临时文件夹」 does not open while this window backs up (the backup is
    writing the temporary folders)."""
    from enginesave.gui.views.dialogs import TmpDialog

    two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    one_click_backup(app).cancel()  # the library exists now
    app.selection.games = dict.fromkeys(app.selection.games, True)
    inside, gate = gated_copy(monkeypatch)
    app.scan_view.backup_button.invoke()
    wait_for(app, inside)
    app.library.clean_tmp()
    assert any("还在进行" in m for m in answers.messages("showinfo"))
    assert not isinstance(app.last_dialog, TmpDialog)
    gate.set()
    wait(app)
    app.last_dialog.cancel()


def test_folders_to_skip_are_set_in_the_settings_and_used_by_the_scan(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D63: 「扫描时跳过的文件夹」 -- a folder chosen (kept as its path) and a typed pattern;
    the scan skips both (reason ``excluded``, shown in 「查看跳过的目录」), the rescan before a
    backup too; a scan root that is on the list itself gets a word of warning."""
    from enginesave.gui.views.dialogs import SettingsDialog
    from fixtures.builder import build_game, simple_spec

    build_f1(games_root, saves_root)
    novels = games_root / "Novels [2026]"
    build_game(novels, simple_spec("Hidden Game", "HiddenGame", "Hidden-1"), saves_root)
    build_game(games_root / "Comics", simple_spec("Other Game", "OtherGame", "Other-1"), saves_root)
    app.open_settings()
    dialog = app.last_dialog
    assert isinstance(dialog, SettingsDialog) and dialog.exclude_list.size() == 0
    answers.directory = str(novels)
    dialog.add_exclude_folder()
    dialog.add_exclude(f'"{novels}"')  # Explorer's "Copy as path": the same folder
    dialog.pattern.set("*\\comics")
    dialog.add_pattern()
    dialog.pattern.set("*\\leftover*")  # typed, not added: taken with 「确定」 too
    assert list(dialog.exclude_list.get(0, "end")) == [str(novels), "*\\comics"]
    dialog.exclude_list.selection_set(1)
    dialog.remove_exclude()
    dialog.pattern.set("*\\comics")
    dialog.add_pattern()
    dialog.pattern.set("*\\leftover*")
    dialog.press_primary()
    assert app.settings.scan_excludes == [str(novels), "*\\comics", "*\\leftover*"]
    assert load_settings(app.settings_file)[0].scan_excludes == app.settings.scan_excludes
    answers.directory = str(games_root)
    app.add_root()
    assert not answers.messages("showinfo"), "the root itself is not on the list"
    app.scan()
    wait(app)
    assert [g.display_name for g in app.report.games] == ["Readable Game"]
    assert {"path": str(novels), "reason": "excluded"} in app.report.skipped
    assert any(
        text.startswith("excluded（2 个）") for text, _p in state.skipped_detail_lines(app.report)
    )
    options = state.backup_options(app.selection, app.report, app.settings)
    assert options["excludes"] == app.settings.scan_excludes
    answers.directory = str(games_root / "Comics")
    app.add_root()
    assert any("扫描时跳过的文件夹" in m for m in answers.messages("showinfo"))


def test_a_dialogs_job_is_cancelled_from_the_dialog(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D58: the main window's 「取消」 is out of reach while a modal dialog runs its job, so the
    dialog shows its own; Escape (the close box) asks the same instead of being ignored, and
    the dialog stays open until the job has stopped."""
    from enginesave import mirror
    from enginesave.gui.views.dialogs import MirrorDialog
    from enginesave.repo import Repo

    _built, library = ready_library(app, answers, games_root, saves_root, tmp_path)
    inside, gate = threading.Event(), threading.Event()
    real = mirror._mirror_object

    def held(*args: Any, **kwargs: Any) -> None:
        if not inside.is_set():
            inside.set()
            assert gate.wait(60), "the test never released the mirror"
        real(*args, **kwargs)

    monkeypatch.setattr(mirror, "_mirror_object", held)
    find_button(library, "镜像到…").invoke()
    dialog = app.last_dialog
    assert isinstance(dialog, MirrorDialog)
    assert dialog.stop_button is None or not dialog.stop_button.winfo_manager()
    answers.directory = str(tmp_path / "mirror")
    dialog.choose()
    settle(app)
    press_enter(dialog)
    wait_for(app, inside)
    assert dialog.stop_button is not None and dialog.stop_button.winfo_manager() == "pack"
    assert dialog.stop_button.cget("text") == "取消" and dialog.stop_button.instate(["!disabled"])
    dialog.cancel()  # what Escape and the close box call
    assert app.stop_requested() and dialog.winfo_exists() and dialog.busy
    assert dialog.stop_button.cget("text") == "正在取消…"
    gate.set()
    wait(app)
    wait(app)  # the library page is read again
    assert dialog.result.get().startswith("镜像已取消") and not dialog.stop_button.winfo_manager()
    assert Repo.open(tmp_path / "mirror").stale_tmp_dirs() == []
    assert "上次镜像 无" in library.overview.get(), "a cancelled mirror is not the last one"
    dialog.cancel()
    assert not dialog.winfo_exists()


def test_closing_during_a_job_stops_it_first(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D58: closing the window asks the running job to stop at its safe point and closes once
    it has; nothing else starts meanwhile."""
    from enginesave.repo import Repo

    build_f1(games_root, saves_root)
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "library")
    wait(app)
    app.scan()
    wait(app)
    destroyed: list[bool] = []
    real_destroy = app.root.destroy

    def destroy() -> None:  # recorded, and really done: the next test needs a fresh Tk
        destroyed.append(True)
        real_destroy()

    monkeypatch.setattr(app.root, "destroy", destroy)
    inside, gate = gated_copy(monkeypatch)
    app.scan_view.backup_button.invoke()
    wait_for(app, inside)
    app.on_close()
    assert app.closing and app.stop_requested() and not destroyed
    assert not app.run("另一件事", lambda _p: None, lambda _v: None), "nothing new starts"
    gate.set()
    deadline = time.monotonic() + 60
    while not destroyed and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert destroyed and app.closed
    assert Repo.open(tmp_path / "library").stale_tmp_dirs() == []


def test_closing_while_a_result_box_is_open_closes_after_it(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """A job's message box runs its own event loop, in which timers fire: closing asked for
    while the job ran must not destroy the window inside the box (S-168), only after it."""
    from tkinter import messagebox

    from enginesave import verify

    _built, library = ready_library(app, answers, games_root, saves_root, tmp_path)
    destroyed: list[bool] = []
    real_destroy = app.root.destroy

    def destroy() -> None:
        destroyed.append(True)
        real_destroy()

    monkeypatch.setattr(app.root, "destroy", destroy)
    inside_box: list[bool] = []

    def box(_title: str = "", _message: str = "", **_options: Any) -> str:
        for _ in range(30):  # the native box serves events, timers too, until it is closed
            app.root.update()
            time.sleep(0.01)
        inside_box.append(bool(destroyed))
        return "ok"

    monkeypatch.setattr(messagebox, "showerror", box)
    inside, gate = threading.Event(), threading.Event()

    def held(*_args: Any, **_kwargs: Any) -> None:
        inside.set()
        assert gate.wait(60), "the test never released the verification"
        raise OSError(5, "the drive went away")

    monkeypatch.setattr(verify, "verify_repo", held)
    library.verify()
    wait_for(app, inside)
    app.on_close()
    gate.set()
    deadline = time.monotonic() + 60
    while not destroyed and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert inside_box == [False], "the error box was shown, and the window outlived it"
    assert destroyed and app.closed


def test_clicks_backup_and_clean_up_afterwards(
    app,
    typed_prompt: Answers,
    recycle_bin: list[str],
    games_root: Path,
    saves_root: Path,
    tmp_path: Path,
) -> None:
    """扫描后备份: 1; 备份完成后清理（回收站）: 2."""
    built = build_f1(games_root, saves_root)
    typed_prompt.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "library")
    wait(app)
    app.scan()
    wait(app)
    clicks = Clicks(typed_prompt)
    clicks.press(app.scan_view.backup_button)  # 备份已勾选项
    wait(app)
    assert clicks.count == 1 and [r[0] for r in tree_rows(app.last_dialog.tree)] == ["OK"]

    clicks = Clicks(typed_prompt)
    clicks.press(find_button(app.last_dialog.footer, "清理…"))
    settle(app)
    clicks.enter(app.last_dialog)  # 移到回收站
    wait(app)
    assert clicks.count == 2 and not built.root.exists() and not built.global_dir.exists()
    assert recycle_bin == [str(built.root), str(built.global_dir)]
    app.last_dialog.cancel()


def test_clicks_permanent_cleanup(
    app, typed_prompt: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """清理（永久删除）: 清理… → 选永久 → 永久删除 N 项 = 3 (D40: no name to type)."""
    built, library = ready_library(app, typed_prompt, games_root, saves_root, tmp_path)
    clicks = Clicks(typed_prompt)
    clicks.press(find_button(library, "清理…"))
    settle(app)
    dialog = app.last_dialog
    clicks.press(dialog.method_buttons["permanent"])
    settle(app)
    clicks.press(dialog.clean_button)  # 永久删除 2 项（…，无法还原）
    wait(app)
    assert clicks.count == 3 and not built.root.exists() and not built.global_dir.exists()
    dialog.cancel()


def test_clicks_clean_up_from_the_library_page(
    app,
    typed_prompt: Answers,
    recycle_bin: list[str],
    games_root: Path,
    saves_root: Path,
    tmp_path: Path,
) -> None:
    """从备份库页清理: 选记录 → 清理… → 移到回收站 = 3."""
    built, library = ready_library(app, typed_prompt, games_root, saves_root, tmp_path)
    library.record_tree.selection_remove(library.record_tree.selection())
    app.root.update()
    clicks = Clicks(typed_prompt)
    library.record_tree.selection_set(next(iter(library.records)))  # 选记录 (counted here)
    app.root.update()
    clicks.count += 1
    clicks.press(find_button(library, "清理…"))
    settle(app)
    clicks.press(app.last_dialog.clean_button)
    wait(app)
    assert clicks.count == 3 and not built.root.exists()
    app.last_dialog.cancel()


def test_clicks_restore_export_verify(
    app, typed_prompt: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """恢复: 2; 导出单个快照: 2; 批量导出: 2; 导入: 2 + 选路径; 校验: 2; 全选 → 校验: 2."""
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.repo import Repo

    built, library = ready_library(app, typed_prompt, games_root, saves_root, tmp_path)

    clicks = Clicks(typed_prompt)
    clicks.press(find_button(library, "恢复…"))
    settle(app)
    clicks.press(app.last_dialog.restore_button)
    wait(app)
    assert clicks.count == 2 and app.last_dialog.result.get().startswith("已恢复 12 个文件")
    app.last_dialog.cancel()

    app.settings.last_export_dir = str(tmp_path / "exports")  # as after an earlier export
    clicks = Clicks(typed_prompt)
    clicks.press(find_button(library, "导出…"))  # the selected record: one file
    clicks.press(app.last_dialog.export_button)
    wait(app)
    assert clicks.count == 2 and app.last_dialog.result_path.parent == tmp_path / "exports"
    app.last_dialog.cancel()

    build_f8(saves_root)  # a second record, so that 导出… exports several into a folder
    plan = prepare_backup(
        [games_root], tmp_path / "library", saves_root_override=saves_root, include_unlinked=[]
    )
    execute_backup(plan, Repo.open(tmp_path / "library"))
    library.reload()
    wait(app)
    library.toggle_all()  # ticking is choosing what, not counted
    app.settings.last_export_dir = str(tmp_path / "batch")  # as after an earlier batch export
    clicks = Clicks(typed_prompt)
    clicks.press(find_button(library, "导出…"))
    assert not app.last_dialog.single
    clicks.press(app.last_dialog.export_button)  # 导出到 batch
    wait(app)
    assert clicks.count == 2 and "已导出 3 个" in app.last_dialog.result.get()
    app.last_dialog.cancel()

    assert app.set_repo(tmp_path / "second-library")
    wait(app)
    clicks = Clicks(typed_prompt)
    clicks.press(find_button(library, "导入…"))
    typed_prompt.directory = str(tmp_path / "batch")
    app.last_dialog.choose_folder()  # 选路径 (counted apart, as in the plan's table)
    settle(app)
    clicks.press(app.last_dialog.import_button)
    wait(app)
    assert clicks.count == 2 and app.last_dialog.result.get().startswith("已导入 3 个")
    app.last_dialog.cancel()
    wait(app)

    library.record_tree.selection_set(next(iter(library.records)))
    app.root.update()
    clicks = Clicks(typed_prompt)
    clicks.count += 1  # 选记录
    clicks.press(find_button(library, "校验"))
    wait(app)
    assert clicks.count == 2 and [r[0] for r in tree_rows(library.verify_tree)] == ["OK"]
    library.record_tree.selection_remove(library.record_tree.selection())
    app.root.update()
    assert find_button(library, "校验").instate(["disabled"]), "nothing ticked or selected"
    clicks = Clicks(typed_prompt)
    clicks.press(library.toggle_button)  # 全选 → 校验 replaces 「校验全部」 (D33 ⑥)
    clicks.press(find_button(library, "校验"))
    wait(app)
    assert clicks.count == 2 and [r[0] for r in tree_rows(library.verify_tree)] == ["OK"] * 3
    assert built.root.is_dir()


@pytest.mark.parametrize("same_second", [False, True])
def test_import_dialog_skips_what_was_imported_before(
    app,
    answers: Answers,
    monkeypatch: pytest.MonkeyPatch,
    games_root: Path,
    saves_root: Path,
    tmp_path: Path,
    same_second: bool,
) -> None:
    """D31: already imported archives show as skipped; the tick imports them again."""
    from datetime import UTC, datetime

    from enginesave import backup
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.export import export_records
    from enginesave.gui.views.dialogs import ImportDialog
    from enginesave.repo import Repo

    if same_second:  # both backups in one second: the newer snapshot id ends in "-1"
        moment = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(backup, "utc_now", lambda: moment)
    _built, library = ready_library(app, answers, games_root, saves_root, tmp_path)
    source = Repo.open(tmp_path / "library")
    (game_id,) = [r["game_id"] for r in source.load_catalog()["games"]]
    folder = tmp_path / "exports"
    export_records(source, [game_id], folder, saves_root_override=saves_root)
    target = tmp_path / "target"
    assert app.set_repo(target)
    wait(app)
    answers.directory = str(folder)

    def open_folder() -> ImportDialog:
        library.import_archives()
        importer = app.last_dialog
        assert isinstance(importer, ImportDialog)
        importer.choose_folder()
        settle(app)
        return importer

    importer = open_folder()
    importer.press_primary()
    wait(app)
    assert [r[5] for r in tree_rows(importer.tree)] == ["已导入（新记录）"]
    importer.cancel()

    importer = open_folder()
    assert tree_rows(importer.tree)[0][5].startswith("已导入过（将跳过）：快照 ")
    assert importer.import_button.instate(["disabled"]), "nothing left to import"
    assert "1 个将跳过：已导入过" in importer.result.get()
    importer.reimport.set(True)  # only the rows and the button change, no new check
    assert tree_rows(importer.tree)[0][5].startswith("重新导入：已导入过（快照 ")
    assert importer.import_button.cget("text") == "导入（1 个）"
    importer.reimport.set(False)
    assert importer.import_button.instate(["disabled"])
    importer.cancel()

    plan = prepare_backup([games_root], source.root, saves_root_override=saves_root)
    execute_backup(plan, source)  # a newer snapshot of the same game
    export_records(source, [game_id], folder, saves_root_override=saves_root)
    older, newer = source.snapshot_ids(game_id)
    if same_second:
        assert newer == f"{older} (2)"
    # The folder is listed by file name, and "<id>-1.zip" sorts before "<id>.zip": find the
    # rows by archive, not by position (the order used to make this test fail now and then).
    name = {sid: f"EngineSave-readablegame-{sid}.zip" for sid in (older, newer)}
    importer = open_folder()
    checks = {r[0]: r[5] for r in tree_rows(importer.tree)}
    assert set(checks) == set(name.values())
    assert checks[name[older]].startswith("已导入过（将跳过）")
    assert checks[name[newer]].startswith("可以导入")
    assert importer.import_button.cget("text") == "导入（1 个）"
    importer.press_primary()
    wait(app)
    rows = {r[0]: r[5] for r in tree_rows(importer.tree)}
    assert rows[name[older]].startswith("已跳过：已导入过（快照 ")
    assert rows[name[newer]] == "已导入（已有记录）"
    assert importer.result.get().startswith("已导入 1 个、跳过 1 个（已导入过）、失败 0 个。")
    assert len(Repo.open(target).snapshot_ids(game_id)) == 2
    importer.cancel()


def test_right_click_menus_and_open_beside_every_path(
    app,
    answers: Answers,
    opened: list[str],
    monkeypatch: pytest.MonkeyPatch,
    games_root: Path,
    saves_root: Path,
    tmp_path: Path,
) -> None:
    """D36: rows open or copy their folders from a right-click menu, dialogs have 「打开」
    beside every path; Explorer is a recorder here, the clipboard too."""
    from enginesave.gui.views.dialogs import (
        BackupPreviewDialog,
        CleanupDialog,
        RestoreDialog,
        TextDialog,
    )
    from enginesave.gui.views.menus import COPY_PATH

    copied: list[str] = []
    monkeypatch.setattr(app.root, "clipboard_clear", copied.clear)
    monkeypatch.setattr(app.root, "clipboard_append", copied.append)

    def entries(menu) -> list[tuple[str, str]]:
        last = menu.index("end")
        return [
            (menu.entrycget(i, "label"), menu.entrycget(i, "state"))
            for i in range(last + 1)
            if menu.type(i) == "command"
        ]

    build_f8(saves_root)
    built, library = ready_library(app, answers, games_root, saves_root, tmp_path)
    scan = app.scan_view
    menu = scan.game_menus.menu_for("g0")
    assert entries(menu) == [
        ("确认 / 关联…", "normal"),  # D48 3
        ("打开游戏目录", "normal"),
        ("打开全局存档目录", "normal"),
        ("打开本地存档目录", "normal"),
    ]
    menu.invoke(menu.index("打开游戏目录"))
    assert opened == [str(built.root)]
    copy = menu.nametowidget(menu.entrycget(menu.index(COPY_PATH), "menu"))
    copy.invoke(1)
    assert copied == [str(built.global_dir)]
    orphans = scan.show_unlinked()  # D42: in their own dialog, 「打开」 beside each
    assert orphans is not None and set(orphans.links) == {"u0", "u1"}
    before = len(opened)
    assert orphans.open_link("u0") and len(opened) == before + 1
    orphans.cancel()

    record = next(iter(library.records))
    menu = library.record_menus.menu_for(record)
    assert [label for label, _ in entries(menu)] == [
        "打开备份记录文件夹",
        "打开游戏目录",
        "打开全局存档目录",
        "按当前名字重命名文件夹…",  # 0.11.0 (D67 1)
    ]
    repo_folder = library.repo.game_dir(library.records[record].game_id)
    menu.invoke(0)
    library.open_record_folder(record)  # what a double click does: the menu's first entry
    assert opened[-2:] == [str(repo_folder), str(repo_folder)]
    moved = built.global_dir.with_name("moved-away")
    built.global_dir.rename(moved)  # a folder that is not there is greyed, not an error
    assert entries(library.record_menus.menu_for(record))[2] == (
        "打开全局存档目录（不存在）",
        "disabled",
    )
    assert not app.open_folder(built.global_dir)
    assert any("这个文件夹不存在" in m for m in answers.messages("showinfo"))
    moved.rename(built.global_dir)

    scan.games.selection_set("g0")
    scan.details()
    details = app.last_dialog
    assert isinstance(details, TextDialog) and len(details.links) >= 3
    first = next(iter(details.links))
    assert details.open_link(first) and opened[-1] == str(built.root)
    details.cancel()

    assert scan.games.set("g0", "check") == state.UNCHECKED, "D54: backed up, no new saves"
    scan.toggle_game("g0")
    app.preview_backup()
    wait(app)
    preview = app.last_dialog
    assert isinstance(preview, BackupPreviewDialog)
    (row, path), *_ = preview.links.items()
    assert preview.tree.set(row, "open") == "打开" and preview.open_link(row)
    assert opened[-1] == str(path)
    preview.cancel()

    library.restore()
    restore = app.last_dialog
    assert isinstance(restore, RestoreDialog)
    settle(app)
    restore.open_game_root()
    assert opened[-1] == str(built.root)
    local = next(iid for iid in restore.links if restore.preview_tree.set(iid, "source") == "local")
    assert restore.open_link(local) and opened[-1] == str(built.local_dir)
    restore.cancel()

    library.cleanup()
    cleanup = app.last_dialog
    assert isinstance(cleanup, CleanupDialog)
    settle(app)
    assert cleanup.open_link("c0") and opened[-1] == str(built.root)
    cleanup.cancel()
    assert answers.messages("showerror") == []


def test_a_scan_logs_one_line_for_the_skipped_folders(
    app, answers: Answers, games_root: Path, saves_root: Path
) -> None:
    """D34: the log and the status bar summarise by reason; the link lists every folder."""
    from enginesave.gui.views.dialogs import TextDialog

    build_f1(games_root, saves_root)
    for index in range(4):  # deeper than the scan goes: skipped (depth_limit)
        games_root.joinpath(f"deep{index}", *(f"l{n}" for n in range(8))).mkdir(parents=True)
    answers.directory = str(games_root)
    app.add_root()
    app.scan()
    wait(app)
    panel = log_panel(app)
    assert panel.count("跳过 4 个目录：depth_limit 4；示例：") == 1 and "扫描用时 " in panel
    assert "跳过：" not in panel, "no line per folder any more"
    status = app.status_var.get()
    assert status.startswith("扫描完成；扫描用时 ") and "跳过 4 个目录" in status
    link = app.scan_view.skipped_link
    assert link.winfo_manager() == "pack" and link.cget("text") == "查看跳过的目录（4）"
    details = app.scan_view.show_skipped()
    assert isinstance(details, TextDialog) and len(details.links) == 4
    details.cancel()


def test_restore_waits_only_when_the_game_folder_is_gone(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D32 ①: the preview runs by itself unless the folder to restore into is unknown."""
    import shutil

    from enginesave.gui.views.dialogs import RestoreDialog

    built, library = ready_library(app, answers, games_root, saves_root, tmp_path)
    reinstalled = reinstall_copy(built.root, tmp_path / "reinstalled")
    shutil.rmtree(built.root)  # a fixture copy in tmp_path: the game was uninstalled
    library.restore()
    dialog = app.last_dialog
    assert isinstance(dialog, RestoreDialog)
    settle(app)
    assert dialog.plan is None and dialog.result.get().startswith("请选择游戏目录")
    assert dialog.restore_button.instate(["disabled"])
    dialog.source_vars["local"].set(False)  # only the global saves: no folder needed
    settle(app)
    assert dialog.plan is not None and [t.source_id for t in dialog.plan.targets] == ["global"]
    dialog.source_vars["local"].set(True)
    answers.directory = str(reinstalled)
    dialog.browse()
    settle(app)
    assert dialog.plan is not None and not dialog.restore_button.instate(["disabled"])
    targets = {t.source_id: t.target_dir for t in dialog.plan.targets}
    assert targets["local"] == reinstalled / "game" / "saves"
    dialog.cancel()
    assert answers.messages("showerror") == []


def test_restore_offers_the_installs_and_waits_when_there_are_several(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D39: a newer copy the scan recognises is a candidate too; with two nothing is filled
    in and the preview waits; a folder around the game is replaced by the game inside."""
    from enginesave.gui.views.dialogs import RestoreDialog

    built, library = ready_library(app, answers, games_root, saves_root, tmp_path)
    wrapper = games_root / "ReadableGame-1.0-pc"
    newer = reinstall_copy(built.root, wrapper / "Readable Game 1.0")
    app.scan()  # the copy is found and recognised as the record
    wait(app)
    library.restore()
    dialog = app.last_dialog
    assert isinstance(dialog, RestoreDialog)
    settle(app)
    assert [str(p) for p, _ in dialog.candidates] == [str(built.root), str(newer)]
    assert dialog.game_root.get() == "" and dialog.plan is None
    assert dialog.result.get().startswith("请选择游戏目录：这款游戏有 2 个游戏目录")
    assert list(dialog.root_choice.cget("values")) == [str(built.root), str(newer)]
    dialog.game_root.set(str(wrapper))  # the folder around it, as unpacked
    settle(app)
    assert dialog.plan is not None and dialog.plan.game_root == newer
    assert "里面才是游戏目录" in dialog.result.get()
    targets = {t.source_id: t.target_dir for t in dialog.plan.targets}
    assert targets["local"] == newer / "game" / "saves"
    dialog.cancel()
    assert answers.messages("showerror") == []


def build_second(games_root: Path, saves_root: Path):
    """A second game beside F1, with its own global saves."""
    from fixtures.builder import build_game, simple_spec

    spec = simple_spec("Second Game", "SecondGame", "SecondGame-1600000009")
    return build_game(games_root, spec, saves_root)


def typed(app, box, text: str) -> None:
    """Type into a search box: nothing changes until typing pauses (D46)."""
    box.hide_hint()
    box.var.set(text)
    assert box.timer is not None, "the query waits for typing to pause"
    deadline = time.monotonic() + 10
    while box.timer is not None and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert box.timer is None


def test_the_scan_page_folds_games_without_new_saves(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D54: four groups (empty ones left out); a game backed up and untouched is folded away
    and unticked; 「全选」 works on what can be seen; a click on a title, Tk's triangle or
    ←/→ open and close a group, remembered across scans; a search opens every group for the
    time being; after a backup the game moves to 「已备份、无新存档」, unticked (D61 ①)."""
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.repo import Repo
    from fixtures.builder import build_f22

    build_f1(games_root, saves_root)
    library = tmp_path / "library"
    plan = prepare_backup([games_root], library, saves_root_override=saves_root)
    execute_backup(plan, Repo.open_or_create(plan.dest))
    build_second(games_root, saves_root)
    build_f22(games_root)  # Emuera: 不支持
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(library)
    wait(app)
    app.scan()
    wait(app)
    view = app.scan_view
    assert scan_groups(view) == [
        ("有新存档可备份（1）", True),
        ("已备份、无新存档（1）", False),
        ("不支持（1：Emuera 1）", False),  # D61 ⑤
    ]
    rows = {r[2]: r for r in game_rows(view)}
    assert rows["Second Game"][0] == state.CHECKED and rows["Second Game"][7].startswith("未备份过")
    readable = rows["Readable Game"]
    assert readable[0] == state.UNCHECKED and readable[7].startswith("无新存档，上次备份 ")
    assert view.counts.get() == "已勾选 1 款游戏"
    assert "分组：有新存档可备份 1；已备份、无新存档 1；待确认 0；不支持 1" in log_panel(app)

    toggle = view.toggle_button
    assert toggle.cget("text") == "全不选", "every game that can be seen is ticked"
    toggle.invoke()
    toggle.invoke()
    assert view.counts.get() == "已勾选 1 款游戏", "the folded game was not ticked"
    view.toggle_group("group-unchanged")  # a click on its title row
    assert scan_groups(view)[1] == ("已备份、无新存档（1）", True)
    assert toggle.cget("text") == "全选", "the game folded away before can be seen now"

    view.filter.set("readable")  # D54 2: every group opens while searching
    assert scan_groups(view) == [
        ("有新存档可备份（0/1）", True),
        ("已备份、无新存档（1/1）", True),
        ("不支持（0/1）", True),
    ]
    view.filter.clear()
    assert [is_open for _title, is_open in scan_groups(view)] == [True, True, False]

    view.games.item("group-unchanged", open=False)  # Tk's triangle or ←, then its event
    view.games.event_generate("<<TreeviewClose>>")
    app.root.update()
    assert view.group_open["unchanged"] is False
    view.set_group_open("unsupported", True)
    app.scan()
    wait(app)
    assert [is_open for _title, is_open in scan_groups(view)] == [True, False, True]

    view.backup_button.invoke()  # Second Game
    wait(app)
    app.last_dialog.cancel()
    assert scan_groups(view) == [("已备份、无新存档（2）", False), ("不支持（1：Emuera 1）", True)]
    assert view.counts.get() == "已勾选 0 款游戏", "D61 ①: the tick is spent"


def test_the_groups_follow_backups_removals_and_another_library(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D54 after the scan: two backups from one scan (the first one's record is kept);
    Space and Tk's triangle on a folded group; a removed record, then another library, tick
    the games again that lost their "no new saves"; a backup from 「预览…」 regroups too."""
    from enginesave.gui.views.dialogs import BackupPreviewDialog
    from enginesave.removal import execute_remove, plan_remove
    from enginesave.repo import Repo
    from fixtures.builder import build_game, simple_spec

    for name in ("Alpha", "Bravo"):
        build_game(games_root, simple_spec(name, name, f"{name}-1600000001"), saves_root)
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "first")
    wait(app)
    app.scan()
    wait(app)
    view = app.scan_view
    iids = {game.display_name: iid for iid, game in view.games_by_iid.items()}

    def notes() -> dict[str, str]:
        return {r[2]: r[7].split("；")[0] for r in game_rows(view)}

    def ticked() -> dict[str, bool]:
        return {r[2]: r[0] == state.CHECKED for r in game_rows(view)}

    view.toggle_game(iids["Bravo"])
    view.backup_button.invoke()  # Alpha
    wait(app)
    app.last_dialog.cancel()
    assert ticked() == {"Alpha": False, "Bravo": False}, "D61 ①: Alpha's tick is spent"
    view.toggle_game(iids["Bravo"])
    view.backup_button.invoke()  # Bravo
    wait(app)
    app.last_dialog.cancel()
    assert scan_groups(view) == [("已备份、无新存档（2）", False)]
    assert all(n.startswith("无新存档，上次备份 ") for n in notes().values()), notes()

    view.games.selection_set(iids["Alpha"])  # e.g. Shift-selected into the folded group
    view.space_toggle()
    view.toggle_game(iids["Bravo"])
    assert ticked() == {"Alpha": False, "Bravo": True}, "Space leaves folded games alone"
    view.games.item("group-unchanged", open=True)  # Tk's triangle or →, then its event
    view.games.event_generate("<<TreeviewOpen>>")
    app.root.update()
    assert view.group_open["unchanged"] is True

    save = next((games_root / "Alpha" / "game" / "saves").glob("*.save"))
    save.write_bytes(save.read_bytes() + b" played on")  # Alpha has a new save now
    view.backup_button.invoke()  # Bravo only
    wait(app)
    app.last_dialog.cancel()
    assert notes()["Alpha"].startswith("有新存档") and ticked()["Alpha"], "ticked again"

    repo = Repo.open(tmp_path / "first")
    alpha = next(g for g in app.report.games if g.display_name == "Alpha")
    execute_remove(plan_remove(repo, repo.load_catalog(), [(alpha.known_game_id, None)]))
    app.library_changed(compare=True)  # as the 「删除…」 dialog does
    wait(app)
    assert notes()["Alpha"] == "未备份过" and ticked() == {"Alpha": True, "Bravo": False}
    row = next(r for r in game_rows(view) if r[2] == "Alpha")
    assert "已在备份库" not in row[7], "its record is gone"

    assert app.set_repo(tmp_path / "second")  # another library: compared with nothing yet
    wait(app)
    assert scan_groups(view) == [("有新存档可备份（2）", True)]
    assert ticked() == {"Alpha": True, "Bravo": True}, "Bravo was folded away: ticked again"
    assert "备份库已更换" in log_panel(app)

    app.preview_backup()
    wait(app)
    preview = app.last_dialog
    assert isinstance(preview, BackupPreviewDialog)
    press_enter(preview)  # 开始备份
    wait(app)
    app.last_dialog.cancel()
    assert scan_groups(view) == [("已备份、无新存档（2）", True)]


def test_a_removal_that_fails_halfway_still_compares_again(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D49 with D54: the record went before the removal failed; the error is shown and the
    scan page no longer holds the game as 已备份、无新存档 (it is ticked again)."""
    from enginesave import removal
    from enginesave.gui.views.dialogs import RemoveDialog

    ready_library(app, answers, games_root, saves_root, tmp_path)
    app.scan()
    wait(app)
    view = app.scan_view
    assert scan_groups(view) == [("已备份、无新存档（1）", False)]
    real = removal.execute_remove

    def halfway(plan, found=None, should_stop=None):  # noqa: ANN001, ANN202
        real(plan, found, should_stop)
        raise OSError(32, "catalog.json is locked by another program")

    monkeypatch.setattr(removal, "execute_remove", halfway)
    library = app.library
    library.record_tree.selection_set(next(iter(library.records)))
    app.root.update()
    library.remove()
    dialog = app.last_dialog
    assert isinstance(dialog, RemoveDialog)
    settle(app)
    press_enter(dialog)
    wait(app)
    assert any("locked" in m for m in answers.messages("showerror"))
    assert scan_groups(view) == [("有新存档可备份（1）", True)]
    assert game_rows(view)[0][0] == state.CHECKED
    dialog.cancel()


def test_a_cancelled_removal_keeps_the_rows_it_did_not_delete(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """D58: stopped after the first of two records, the dialog removes only that record's
    rows; the other one is still listed (it is still in the library)."""
    from enginesave import removal
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.gui.views.dialogs import RemoveDialog
    from enginesave.repo import Repo

    build_f1(games_root, saves_root)
    build_second(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "library", saves_root_override=saves_root)
    execute_backup(plan, Repo.open_or_create(plan.dest))
    assert app.set_repo(tmp_path / "library")
    wait(app)
    real = removal.execute_remove

    def first_only(plan, found=None, should_stop=None):  # noqa: ANN001, ANN202
        asked = iter(range(100))
        return real(plan, found, lambda: next(asked) >= 1)

    monkeypatch.setattr(removal, "execute_remove", first_only)
    library = app.library
    library.toggle_button.invoke()  # both records
    library.remove()
    dialog = app.last_dialog
    assert isinstance(dialog, RemoveDialog)
    settle(app)
    press_enter(dialog)
    wait(app)
    left = Repo.open(tmp_path / "library").load_catalog()["games"]
    assert len(left) == 1 and "已取消" in dialog.summary.get()
    shown = {dialog.rows[iid] for iid in dialog.rows if dialog.tree.exists(iid)}
    kept = left[0]["game_id"]
    assert kept in shown and all((t if isinstance(t, str) else t[0]) == kept for t in shown), (
        "only the record that is still there keeps its rows"
    )
    dialog.cancel()


def test_the_scan_page_search_box_hides_games_but_keeps_their_ticks(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D46: name, folder or save directory, any case; a hidden game stays ticked, is counted
    and backed up; Esc shows every game; Ctrl+F; the next scan keeps the query."""
    build_f1(games_root, saves_root)
    build_second(games_root, saves_root)
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "library")
    wait(app)
    app.scan()
    wait(app)
    view = app.scan_view

    def names() -> list[str]:
        return sorted(r[2] for r in game_rows(view))

    assert names() == ["Readable Game", "Second Game"]
    assert view.filter.query == "" and view.filter.var.get() == state.SEARCH_PLACEHOLDER
    assert view.counts.get() == "已勾选 2 款游戏"
    typed(app, view.filter, "READABLE")
    assert names() == ["Readable Game"]
    assert view.counts.get() == "已勾选 2 款游戏（其中 1 款被筛选隐藏）"
    toggle = view.toggle_button
    assert toggle.cget("text") == "全不选", "every game shown is ticked"
    toggle.invoke()  # works on the games shown: the hidden tick stays
    assert view.counts.get() == "已勾选 1 款游戏（其中 1 款被筛选隐藏）"
    assert toggle.cget("text") == "全选"
    toggle.invoke()
    view.filter.set("secondgame-1600000009")  # the save directory
    assert names() == ["Second Game"]
    view.filter.set("nothing like it")
    assert names() == [] and view.counts.get() == "已勾选 2 款游戏（其中 2 款被筛选隐藏）"
    assert toggle.instate(["disabled"]), "no game shown: nothing to tick"
    assert str(view.filter.entry.bind("<Escape>")), "Esc is bound"
    view.filter.clear()
    assert names() == ["Readable Game", "Second Game"] and view.filter.query == ""

    assert str(app.root.bind("<Control-f>")), "Ctrl+F is bound"
    view.filter.show_hint()
    app.focus_filter()  # the scan page is shown: its box
    assert not view.filter.hinting and view.filter.var.get() == ""
    app.notebook.select(app.library)
    app.library.filter.show_hint()
    app.focus_filter()
    assert not app.library.filter.hinting
    app.notebook.select(app.scan_view)

    view.filter.set("readable")
    app.scan()  # the query is kept: the new rows are filtered as well
    wait(app)
    assert names() == ["Readable Game"] and view.filter.query == "readable"
    view.backup_button.invoke()  # 备份已勾选项: the hidden game too
    wait(app)
    assert [r[0] for r in tree_rows(app.last_dialog.tree)] == ["OK", "OK"]
    app.last_dialog.cancel()


def test_library_groups_search_and_a_restore_moves_a_record_back(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D43, D51: 「本体存在」, 「本体不在」. D46: the search box filters both groups (「无匹配」
    in an empty one), ticks on hidden records still count, Esc shows all, a reload keeps the query.
    Restoring into a reinstalled folder records it, and the record moves back."""
    import shutil

    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.gui.views.dialogs import RestoreDialog
    from enginesave.repo import Repo

    readable = build_f1(games_root, saves_root)
    build_second(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "library", saves_root_override=saves_root)
    execute_backup(plan, Repo.open_or_create(plan.dest))
    reinstalled = reinstall_copy(readable.root, tmp_path / "reinstalled")
    shutil.rmtree(readable.root)  # a fixture copy in tmp_path: uninstalled by hand
    assert app.set_repo(tmp_path / "library")
    wait(app)
    library = app.library
    tree = library.record_tree

    def groups() -> list[str]:
        return [str(tree.item(g, "values")[1]) for g in tree.get_children()]

    def grouped() -> list[list[str]]:
        return [
            [str(tree.item(iid, "values")[1]).strip() for iid in tree.get_children(g)]
            for g in tree.get_children()
        ]

    assert groups() == ["本体存在（1）", "本体不在（1）"]
    assert grouped() == [["Second Game"], ["Readable Game"]]
    assert all("group" in tree.item(g, "tags") for g in tree.get_children())
    iid = next(i for i, r in library.records.items() if r.name == "Readable Game")
    assert str(tree.item(iid, "values")[2]) == "游戏", "D51: the kind and nothing else"

    library.toggle_all()
    assert library.check_count.get() == "已勾选 2 条"
    typed(app, library.filter, "READABLE")
    assert groups() == ["本体存在（0/1）", "本体不在（1/1）"]
    assert grouped() == [[state.NO_MATCH], ["Readable Game"]]
    assert library.check_count.get() == "已勾选 2 条（其中 1 项被筛选隐藏）"
    assert library.targets() == [r.game_id for r in library.records.values()]
    assert library.toggle_button.cget("text") == "全不选", "every record shown is ticked"
    library.toggle_all()  # works on the records shown: the hidden tick stays
    assert library.check_count.get() == "已勾选 1 条（其中 1 项被筛选隐藏）"
    assert library.toggle_button.cget("text") == "全选"
    library.toggle_all()
    library.filter.set("ReadableGame-1600000001")  # the save directory
    assert grouped() == [[state.NO_MATCH], ["Readable Game"]]
    library.filter.clear()  # Esc
    assert groups() == ["本体存在（1）", "本体不在（1）"]
    assert library.check_count.get() == "已勾选 2 条"
    library.toggle_all()

    library.filter.set("readable")
    library.reload()
    wait(app)
    assert groups() == ["本体存在（0/1）", "本体不在（1/1）"], "a reload keeps the query"
    tree.selection_set(iid)
    app.root.update()
    library.restore()
    dialog = app.last_dialog
    assert isinstance(dialog, RestoreDialog)
    settle(app)
    answers.directory = str(reinstalled)
    dialog.browse()
    settle(app)
    dialog.restore()
    wait(app)
    wait(app)  # the library is read again once the restore is done
    assert dialog.result.get().startswith("已恢复")
    dialog.cancel()
    assert groups() == ["本体存在（1/2）", "本体不在（0/0）"]
    assert grouped() == [["Readable Game"], [state.NO_MATCH]]
    assert library.records[iid].installed and library.selected_record() is library.records[iid]
    doc = Repo.open(tmp_path / "library").load_game(library.records[iid].game_id)
    assert str(reinstalled) in [i["game_root"] for i in doc["installs"]]


def test_confirm_link_from_the_menu_makes_a_candidate_ready_to_back_up(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D48 3: right-click 「确认 / 关联…」 on a CANDIDATE game: a new record is chosen at first
    and its folder is ticked; one click writes the link and the confirmed folder, the row
    turns CONFIRMED and ticked, and 「备份已勾选项」 then backs the folder up. The details
    dialog has the same button."""
    from enginesave.gui.views.dialogs import ConfirmLinkDialog, TextDialog
    from enginesave.models import GameStatus
    from enginesave.repo import Repo
    from fixtures.builder import build_f6

    ready_library(app, answers, games_root, saves_root, tmp_path)
    nameless = build_f6(games_root, saves_root)  # unpacked after the first backup
    app.scan()
    wait(app)
    view = app.scan_view

    def row() -> tuple[str, object]:
        return next(
            (i, g) for i, g in view.games_by_iid.items() if g.display_name == "Nameless Quest"
        )

    iid, game = row()
    assert game.status is GameStatus.CANDIDATE and view.games.set(iid, "check") == state.UNCHECKED
    view.games.selection_set(iid)
    view.details()
    details = app.last_dialog
    assert isinstance(details, TextDialog) and find_button(details.footer, "确认 / 关联…")
    details.cancel()

    menu = view.game_menus.menu_for(iid)
    menu.invoke(0)  # 确认 / 关联…
    dialog = app.last_dialog
    assert isinstance(dialog, ConfirmLinkDialog)
    assert dialog.chosen().game_id is None, "a new record at first, never a guessed one"
    assert dialog.confirm_button.cget("text") == "确认（1 个来源）"
    press_enter(dialog)
    wait(app)
    wait(app)  # the library is read again
    assert app.last_dialog is None
    iid, game = row()
    assert game.status is GameStatus.CONFIRMED and view.games.set(iid, "check") == state.CHECKED
    repo = Repo.open(tmp_path / "library")
    catalog = repo.load_catalog()
    record = next(r for r in catalog["games"] if r["display_name"] == "Nameless Quest")
    assert record["snapshot_count"] == 0, "nothing was backed up by the dialog"
    assert sorted((o["type"], o["game_id"]) for o in catalog["overrides"]) == [
        ("confirm_source", record["game_id"]),
        ("link_root", record["game_id"]),
    ]
    assert "已关联：" in log_panel(app)

    view.backup_button.invoke()
    wait(app)
    # D54: only Nameless Quest is ticked; Readable Game was backed up and has no new saves
    assert [r[0] for r in tree_rows(app.last_dialog.tree)] == ["OK"]
    app.last_dialog.cancel()
    manifest = repo.load_manifest(record["game_id"], repo.latest_snapshot(record["game_id"]))
    origins = [s["origin_path"] for s in manifest["sources"]]
    assert str(saves_root / "NamelessQuest-123456") in origins and nameless.root.is_dir()


def test_delete_a_snapshot_then_the_record_from_the_library_page(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D49: with no tick and a snapshot selected only that snapshot starts ticked; one click
    deletes it permanently and the library is read again. Then the whole record, after its
    game folder is gone: the red line warns, the record and its folder go, ticks are cleared."""
    import shutil

    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.gui.views.dialogs import RemoveDialog
    from enginesave.repo import Repo

    built, library = ready_library(app, answers, games_root, saves_root, tmp_path)
    for path in built.local_dir.rglob("*"):
        path.write_bytes(path.read_bytes() + b" changed")  # a second snapshot that differs
    plan = prepare_backup([games_root], tmp_path / "library", saves_root_override=saves_root)
    execute_backup(plan, Repo.open(tmp_path / "library"))
    library.reload()
    wait(app)
    iid = next(iter(library.records))
    library.record_tree.selection_set(iid)
    app.root.update()
    game_id = library.records[iid].game_id
    old, new = Repo.open(tmp_path / "library").snapshot_ids(game_id)
    older = next(s for s, sid in library.snapshots.items() if sid == old)
    library.snapshot_tree.selection_set(older)
    app.root.update()
    assert find_button(library, "删除…").instate(["!disabled"])

    library.remove()
    dialog = app.last_dialog
    assert isinstance(dialog, RemoveDialog)
    settle(app)
    assert dialog.ticked == {(game_id, old)}, "only the selected snapshot"
    assert dialog.summary.get().startswith("将删除 0 条记录、1 个快照，释放约 ")
    assert dialog.remove_button.cget("text") == "永久删除（1 个快照）"
    assert not dialog.warning_label.winfo_manager(), "the game is installed: no red line"
    press_enter(dialog)
    wait(app)
    wait(app)  # the library is read again
    assert dialog.summary.get().startswith("已删除 0 条记录、1 个快照，释放 ")
    assert dialog.remove_button.instate(["disabled"])
    dialog.cancel()
    repo = Repo.open(tmp_path / "library")
    assert repo.snapshot_ids(game_id) == [new]
    assert "已删除 0 条记录、1 个快照" in log_panel(app)

    shutil.rmtree(built.root)  # a fixture copy in tmp_path: the game is gone
    library.reload()
    wait(app)
    library.toggle_all()  # ticked: the whole record
    library.remove()
    dialog = app.last_dialog
    settle(app)
    assert dialog.ticked == {(game_id, new)}
    assert dialog.summary.get().startswith("将删除 1 条记录、1 个快照")
    assert dialog.warning.get() == "其中 1 条记录的本体已不在，删除后这些存档再也找不回。"
    dialog.toggle("r0")  # the record row clears all its snapshots
    assert dialog.ticked == set() and dialog.remove_button.instate(["disabled"])
    dialog.toggle("r0")
    press_enter(dialog)
    wait(app)
    wait(app)
    dialog.cancel()
    assert repo.load_catalog()["games"] == [] and not repo.game_dir(game_id).exists()
    assert library.records == {} and library.checked == set()
    assert list(repo.iter_objects()) == [], "nothing else used those objects"


def test_the_window_keeps_the_scan_cache_beside_its_settings(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path, reads: list[str]
) -> None:
    """D52 3: the window's scans share ``scan-cache.json`` beside ``gui.json``; the read-only
    rescan before 「备份已勾选项」 then opens no script."""
    build_f1(games_root, saves_root)
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "library")
    wait(app)
    app.scan()
    wait(app)
    assert app.cache_file == app.settings_file.parent / "scan-cache.json"
    assert app.cache_file.is_file() and reads
    reads.clear()
    app.scan_view.backup_button.invoke()
    wait(app)
    assert reads == [], "the rescan before the backup comes from the cache"
    assert [r[0] for r in tree_rows(app.last_dialog.tree)] == ["OK"]
    app.last_dialog.cancel()


def test_library_tables_are_headed_in_chinese_and_the_type_fits(app) -> None:
    """D47 8: the tables' headings are Chinese (the codes in the cells stay English); the
    「类型」 column shows 「孤立目录」 whole, also when the window is narrow."""
    from tkinter import font as tkfont

    library = app.library

    def headings(tree) -> list[str]:
        return [str(tree.heading(column, "text")) for column in tree["columns"]]

    assert headings(library.record_tree) == ["勾选", "名称", "类型", "快照数", "最近快照"]
    assert headings(library.snapshot_tree) == [
        "快照 ID", "时间（本地）", "原因", "状态", "文件数", "大小", "存储",
    ]  # fmt: skip
    assert headings(library.verify_tree) == [
        "结果", "名称", "快照", "一致", "不一致", "缺失", "多余", "说明",
    ]  # fmt: skip
    needed = tkfont.nametofont("TkDefaultFont").measure(state.WIDEST_KIND)
    column = library.record_tree.column("kind")
    assert int(column["width"]) > needed and int(column["minwidth"]) > needed


def test_closed_dialogs_are_freed(app, games_root: Path, saves_root: Path, tmp_path: Path) -> None:
    """Variable traces live in the Tcl interpreter: closing a dialog removes them, otherwise
    every restore or cleanup dialog ever opened would stay in memory."""
    import weakref

    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.gui.views.dialogs import CleanupDialog, ExportDialog, RestoreDialog
    from enginesave.repo import Repo

    build_f1(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "library", saves_root_override=saves_root)
    execute_backup(plan, Repo.open_or_create(plan.dest))
    repo = Repo.open(tmp_path / "library")
    (record,) = repo.load_catalog()["games"]
    for kind in (RestoreDialog, CleanupDialog, ExportDialog):
        dialog = kind(
            app, repo, record["game_id"] if kind is RestoreDialog else [record["game_id"]]
        )
        settle(app)
        commands = [name for _variable, name in dialog.traces]
        assert commands and all(app.root.tk.call("info", "commands", c) for c in commands)
        freed = weakref.ref(dialog)
        dialog.close()
        del dialog
        gc.collect()
        assert freed() is None, f"a closed {kind.__name__} is freed"
        assert not any(app.root.tk.call("info", "commands", c) for c in commands)


def test_blank_settings_show_guidance_and_no_override_bar(
    root, answers: Answers, tmp_path: Path
) -> None:
    """The delivered state (M12 DoD): guidance in the main area, no override bar."""
    from enginesave.gui.app import App
    from enginesave.gui.views.library_view import NO_LIBRARY

    settings_file = tmp_path / "gui.json"
    save_settings(blank_settings(GuiSettings(window_geometry="1280x820+26+26")), settings_file)
    window = App(
        root, settings_file=settings_file, logs=tmp_path / "logs", env=env_without_override()
    )
    try:
        root.update()
        assert not window.notice_visible
        assert window.scan_view.guide.winfo_manager() == "pack"
        assert window.scan_view.guide_var.get() == state.guidance(blank_settings())
        assert window.library.status.get() == NO_LIBRARY
        assert window.saves_override() is None
    finally:
        window.on_close()
    assert load_settings(settings_file)[0] == blank_settings(
        GuiSettings(window_geometry="1280x820+26+26")
    ), "a hidden window does not overwrite the geometry"


def test_override_from_the_environment_shows_a_bar_without_clear(
    root, answers: Answers, tmp_path: Path
) -> None:
    from enginesave.gui.app import App

    settings_file = tmp_path / "gui.json"
    save_settings(blank_settings(), settings_file)
    env = {**env_without_override(), OVERRIDE_ENV: str(tmp_path / "env-saves")}
    window = App(root, settings_file=settings_file, logs=tmp_path / "logs", env=env)
    try:
        root.update()
        assert window.notice_visible and "ENGINESAVE_RENPY_SAVES_ROOT" in window.notice_var.get()
        assert window.notice_clear.winfo_manager() == "", "only the environment can clear it"
    finally:
        window.on_close()


def test_the_override_bar_follows_the_scan_roots_at_once(
    root, answers: Answers, tmp_path: Path
) -> None:
    """M44 review (D66 7): adding a scan root a root token is moved onto shows the conflict
    on the bar at once, removing it takes the conflict away."""
    from enginesave import roots
    from enginesave.gui.app import App

    settings_file = tmp_path / "gui.json"
    save_settings(blank_settings(), settings_file)
    games = tmp_path / "games"
    (games / "profile").mkdir(parents=True)
    env = {**env_without_override(), roots.env_name(roots.LOCALAPPDATA): str(games / "profile")}
    window = App(root, settings_file=settings_file, logs=tmp_path / "logs", env=env)
    try:
        root.update()
        assert window.notice_visible and "冲突" not in window.notice_var.get()
        answers.directory = str(games)
        window.add_root()
        assert "冲突" in window.notice_var.get() and "与扫描根目录" in window.notice_var.get()
        window.roots_list.selection_set(0)
        window.remove_root()
        assert "冲突" not in window.notice_var.get()
    finally:
        window.on_close()


def test_damaged_settings_fall_back_to_defaults(root, answers: Answers, tmp_path: Path) -> None:
    from enginesave.gui.app import App

    settings_file = tmp_path / "gui.json"
    settings_file.write_text("{broken", encoding="utf-8")
    window = App(root, settings_file=settings_file, logs=tmp_path / "logs")
    try:
        root.update()
        window._drain_log()
        assert window.settings == GuiSettings()
        assert "设置文件已损坏" in window.log_text.get("1.0", "end")
    finally:
        window.on_close()


def test_a_migration_cancelled_in_the_dialog_goes_on_when_pressed_again(
    app, answers: Answers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M45 review: 「迁移」 again after 「取消」 plans again and finishes; the status bar says
    the library is being migrated (not mirrored)."""
    from enginesave.gui.views.dialogs import MigrateDialog
    from enginesave.repo import Repo

    old = tmp_path / "old"
    with zipfile.ZipFile(Path(__file__).parent / "fixtures" / "library_v1.zip") as archive:
        archive.extractall(old)
    assert app.set_repo(old)
    wait(app)
    dialog = app.last_dialog
    assert isinstance(dialog, MigrateDialog)
    statuses: list[str] = []
    app.status_var.trace_add("write", lambda *_a: statuses.append(app.status_var.get()))
    asked = iter(range(1000))
    monkeypatch.setattr(app, "stop_requested", lambda: next(asked) >= 2)
    dialog.press_primary()
    wait(app)
    assert Repo.open(old).format_version == 2 and "已取消" in dialog.result.get()  # D70 1
    assert any(s.startswith("正在迁移 ") for s in statuses)
    assert not any("镜像" in s for s in statuses)
    monkeypatch.setattr(app, "stop_requested", lambda: False)
    assert dialog.migrate_button.instate(["!disabled"])
    dialog.press_primary()
    wait(app)
    wait(app)
    assert Repo.open(old).format_version == 2
    assert not isinstance(app.last_dialog, MigrateDialog), "closed once it is done"


def test_an_old_library_offers_its_migration_and_is_read_only_until_then(
    app, answers: Answers, tmp_path: Path
) -> None:
    """D67 8: a library of format 1 asks once, when it is opened: 「稍后」 leaves it read-only
    (the buttons that write are greyed, the page says why), a write asks again, 「迁移」 renames
    its record folders; then 「按当前名字重命名文件夹…」 follows a record's name."""
    from enginesave.gui.views.dialogs import MigrateDialog
    from enginesave.repo import Repo

    old = tmp_path / "old"
    with zipfile.ZipFile(Path(__file__).parent / "fixtures" / "library_v1.zip") as archive:
        archive.extractall(old)
    assert app.set_repo(old)
    wait(app)
    dialog = app.last_dialog
    assert isinstance(dialog, MigrateDialog) and dialog.primary_button is dialog.migrate_button
    assert dialog.migrate_button.cget("text") == "迁移"
    dialog.cancel()  # 「稍后」
    library = app.library
    assert state.READ_ONLY in library.status.get()
    first = next(iter(library.records))
    library.record_tree.selection_set(first)
    app.root.update()
    buttons = {w.cget("text"): w for w, _when in app.actions if hasattr(w, "cget")}
    for name in ("恢复…", "导入…", "清理…", "删除…"):
        assert buttons[name].instate(["disabled"]), name
    assert buttons["校验"].instate(["!disabled"]) and buttons["镜像到…"].instate(["!disabled"])
    menu = library.record_menus.menu_for(first)
    assert menu.entrycget("end", "state") == "disabled", "no rename while read-only"
    library.reload()  # opened again in this session: not asked again
    wait(app)
    assert not isinstance(app.last_dialog, MigrateDialog)
    assert not app.writable_library()  # a write asks again
    dialog = app.last_dialog
    assert isinstance(dialog, MigrateDialog)
    dialog.press_primary()
    wait(app)
    wait(app)  # the library is read again
    assert not isinstance(app.last_dialog, MigrateDialog)
    repo = Repo.open(old)
    assert repo.format_version == 2 and state.READ_ONLY not in library.status.get()
    assert "Readable Game" in {p.name for p in (old / "games").iterdir()}
    assert "迁移完成" in log_panel(app)
    readable = next(i for i, r in library.records.items() if r.name == "Readable Game")
    library.rename_folder(readable)  # the same name: said, nothing changes
    assert any("文件夹已经是" in m for m in answers.messages("showinfo"))
    game_id = library.records[readable].game_id
    doc = repo.load_game(game_id)
    doc["display_name"] = "Readable Game Two"
    repo.save_game(doc)
    library.rename_folder(readable)
    wait(app)
    wait(app)
    assert any(r"games\Readable Game Two" in m for _k, _t, m in answers.shown if _k == "ask")
    assert Repo.open(old).folder_name(game_id) == "Readable Game Two"
    assert (old / "games" / "Readable Game Two" / "game.json").is_file()


def test_the_window_switches_to_english_and_back_keeping_what_it_holds(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D68 3: 「界面语言」 in the settings dialog builds the whole window again in English after
    「确定」 (tabs, buttons, groups, counts, the log line); the scan's results, the ticks, the
    groups and the log stay; gui.json remembers it; back to 「跟随系统」 (Chinese here) the same
    way. No text of the window lacks its English; the top bar has no language button."""
    from enginesave.gui import i18n
    from enginesave.gui.views.dialogs.settings import language_label

    def choose(choice: str) -> None:
        app.open_settings()
        dialog = app.last_dialog
        assert dialog.language_box.cget("values")  # 跟随系统 / 中文 / English
        dialog.language.set(language_label(choice))
        dialog.accept()
        app.root.update()

    scan = two_games_scanned(app, answers, games_root, saves_root, tmp_path)
    app.scan_view.toggle_game(next(iter(scan.games_by_iid)))  # a tick of the user's own
    ticked = dict(app.selection.games)
    rows = len(scan.games_by_iid)
    del scan  # the old page: nothing of the test keeps it
    before = app.log_text.get("1.0", "end-1c")
    assert not hasattr(app, "language_button")
    assert load_settings(app.settings_file)[0].language == "auto"
    import weakref

    old_pages = [weakref.ref(app.scan_view), weakref.ref(app.library)]
    app.scan_view.filter.set("x")  # a query and a pending timer at the switch
    app.scan_view.filter.clear()
    app.scan_view.filter.var.set("ab")
    choose(i18n.EN)
    gc.collect()
    assert all(page() is None for page in old_pages), "the old pages are freed (M47 review)"
    assert i18n.language() == "en" and load_settings(app.settings_file)[0].language == "en"
    tabs = [app.notebook.tab(t, "text") for t in app.notebook.tabs()]
    assert tabs == ["Scan & back up", "Library"]
    assert app.scan_button.cget("text") == "Scan (F5)"
    assert app.scan_view.backup_button.cget("text").startswith("Back up ticked")
    assert len(app.scan_view.games_by_iid) == rows, "the scan's results stay"
    menus = [app.scan_view.game_menu(i) for i in app.scan_view.games_by_iid]
    firsts = [m.entrycget(0, "label") for m in menus if m is not None]
    assert "Confirm / link…" in firsts and not any(i18n.CJK.search(f) for f in firsts)
    assert app.selection.games == ticked, "the ticks stay"
    titles = [app.scan_view.games.item(i, "values") for i in app.scan_view.games.get_children()]
    assert titles and not any(i18n.CJK.search(" ".join(map(str, t))) for t in titles)
    assert log_panel(app).startswith(before), "the log panel's lines stay"
    assert "Window language: English" in log_panel(app)
    app.notebook.select(1)
    wait(app)
    app.open_settings()  # the dialog in English too, 「跟随系统」 among the choices
    values = app.last_dialog.language_box.cget("values")
    assert "Same as Windows" in values and "中文" in values and "English" in values
    app.last_dialog.cancel()
    app.root.update()
    assert not i18n.missing(), f"texts without English: {sorted(i18n.missing())[:5]}"
    lines = log_panel(app)
    choose(i18n.EN)  # the same language: nothing is built again, nothing said
    assert log_panel(app).count("Window language") == lines.count("Window language")
    choose(i18n.AUTO)
    assert i18n.language() == "zh" and app.scan_button.cget("text") == "扫描 (F5)"
    assert load_settings(app.settings_file)[0].language == "auto"
    assert len(app.scan_view.games_by_iid) == rows


def test_the_remove_dialog_does_not_delete_a_snapshot_made_after_it_opened(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """1.0.0 (D72 R06): the whole record ticked in the dialog, then a backup elsewhere adds a
    snapshot; 「永久删除」 deletes nothing and says to open the dialog again."""
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.gui.views.dialogs import RemoveDialog
    from enginesave.repo import Repo

    built, library = ready_library(app, answers, games_root, saves_root, tmp_path)
    library.toggle_all()  # the whole record
    library.remove()
    dialog = app.last_dialog
    assert isinstance(dialog, RemoveDialog)
    settle(app)
    for path in built.local_dir.rglob("*"):
        path.write_bytes(path.read_bytes() + b" changed")
    plan = prepare_backup([games_root], tmp_path / "library", saves_root_override=saves_root)
    execute_backup(plan, Repo.open(tmp_path / "library"))
    repo = Repo.open(tmp_path / "library")
    game_id = repo.load_catalog()["games"][0]["game_id"]
    assert len(repo.snapshot_ids(game_id)) == 2
    press_enter(dialog)
    wait(app)
    wait(app)
    assert len(Repo.open(tmp_path / "library").snapshot_ids(game_id)) == 2
    assert any("什么都没有删除" in m for _k, _t, m in answers.shown)


def old_fixture_library(tmp_path: Path) -> Path:
    old = tmp_path / "old"
    with zipfile.ZipFile(Path(__file__).parent / "fixtures" / "library_v1.zip") as archive:
        archive.extractall(old)
    return old


def test_a_finished_migration_closes_its_dialog_without_an_error(
    app, answers: Answers, tmp_path: Path
) -> None:
    """D70 2 (the user's log of 2026-10-26 22:21): the migration finished and closed its
    dialog, then the base class refreshed the destroyed dialog's buttons -- a TclError box.
    Now no error is shown and the library is migrated."""
    from enginesave.gui.views.dialogs import MigrateDialog
    from enginesave.repo import Repo

    old = old_fixture_library(tmp_path)
    errors: list[BaseException] = []
    app.root.report_callback_exception = lambda kind, exc, tb: errors.append(exc)
    assert app.set_repo(old)
    wait(app)
    dialog = app.last_dialog
    assert isinstance(dialog, MigrateDialog)
    dialog.press_primary()
    wait(app)
    wait(app)
    assert Repo.open(old).format_version == 2
    assert not dialog.winfo_exists()
    assert errors == [] and answers.messages("showerror") == []


def test_a_half_migrated_library_offers_the_rest_when_it_is_opened(
    app, answers: Answers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D70 1: a library left format 2 with a record not renamed (a rename that failed) asks
    again when it is opened, saying the last migration did not finish; 「迁移」 finishes it."""
    from enginesave import migrate
    from enginesave.gui.views.dialogs import MigrateDialog
    from enginesave.repo import Repo

    old = old_fixture_library(tmp_path)
    real_rename = os.rename
    calls: list[str] = []

    def first_fails(src: str, dst: str) -> None:
        calls.append(src)
        if len(calls) == 1:
            raise PermissionError(13, "synthetic: in use", src)
        real_rename(src, dst)

    monkeypatch.setattr(migrate.os, "rename", first_fails)
    assert migrate.execute_migration(Repo.open(old)).failed
    monkeypatch.setattr(migrate.os, "rename", real_rename)
    assert app.set_repo(old)
    wait(app)
    wait(app)
    dialog = app.last_dialog
    assert isinstance(dialog, MigrateDialog)
    assert "上次没有迁移完" in dialog.intro.cget("text")
    dialog.press_primary()
    wait(app)
    wait(app)
    assert not migrate.plan_migration(Repo.open(old)).needed


def test_a_migrated_library_missing_readmes_gets_them_in_the_background(
    app, answers: Answers, tmp_path: Path
) -> None:
    """D70 1: format 2 but snapshots without README.txt: written when the library is
    opened, one line in the log; no dialog."""
    from enginesave.gui.views.dialogs import MigrateDialog
    from enginesave.layout import README, missing_readmes
    from enginesave.migrate import execute_migration
    from enginesave.repo import Repo

    old = old_fixture_library(tmp_path)
    assert not execute_migration(Repo.open(old)).failed
    repo = Repo.open(old)
    record = repo.load_catalog()["games"][0]["game_id"]
    snapshot = repo.snapshot_ids(record)[0]
    (repo.snapshot_dir(record, snapshot) / README).unlink()
    assert app.set_repo(old)
    for _ in range(4):
        wait(app)
    assert not list(missing_readmes(Repo.open(old)))
    assert "补写说明文件 README.txt" in log_panel(app)
    assert not isinstance(app.last_dialog, MigrateDialog)


def test_a_settings_file_with_one_bad_field_keeps_the_rest_after_the_window_closes(
    root, answers: Answers, tmp_path: Path
) -> None:
    """D70 4: a field of the wrong type is reset (said in the log); closing the window writes
    the file with every other setting as it was. D72 R17: a window size Tk refuses opens the
    window at the default size instead of not opening it."""
    from enginesave.gui.app import DEFAULT_GEOMETRY, App
    from enginesave.repo import Repo

    settings = tmp_path / "appdata" / "EngineSave" / "gui.json"
    Repo.open_or_create(tmp_path / "lib")  # a library that is there: nothing to ask
    good = GuiSettings(scan_roots=[str(tmp_path / "games")], repo_path=str(tmp_path / "lib"))
    save_settings(good, settings)
    data = json.loads(settings.read_text(encoding="utf-8"))
    data |= {"include_tokens": "yes", "window_geometry": "not a geometry"}
    settings.write_text(json.dumps(data), encoding="utf-8")
    window = App(root, settings_file=settings, logs=tmp_path / "logs", env=env_without_override())
    try:
        root.update()
        window._drain_log()
        log = window.log_text.get("1.0", "end")
        assert "include_tokens" in log and "窗口位置与大小无效" in log
        assert window.settings.scan_roots == good.scan_roots
        assert window.settings.include_tokens is False
        deadline = time.monotonic() + 30
        while window.runner.busy and time.monotonic() < deadline:  # reading the library
            root.update()
            time.sleep(0.01)
    finally:
        window.on_close()
    kept = load_settings(settings)[0]
    assert kept.scan_roots == good.scan_roots and kept.repo_path == good.repo_path
    assert kept.window_geometry != "not a geometry"
    assert DEFAULT_GEOMETRY


def test_a_language_switch_keeps_the_library_pages_ticks_selection_and_verification(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D70 5: three records ticked, one selected, verified once; switched to English and back:
    the ticks, the selected record and the verification results are all still there."""
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.gui import i18n
    from enginesave.gui.views.dialogs.settings import language_label
    from enginesave.repo import Repo
    from fixtures.builder import build_game, simple_spec

    build_f1(games_root, saves_root)
    for n in (2, 3):
        spec = simple_spec(f"Game {n}", f"Game{n}", f"game-{n}", local_saves=1, global_saves=1)
        build_game(games_root, spec, saves_root)
    plan = prepare_backup([games_root], tmp_path / "library", saves_root_override=saves_root)
    execute_backup(plan, Repo.open_or_create(plan.dest))
    assert app.set_repo(tmp_path / "library")
    wait(app)
    library = app.library
    assert len(library.records) == 3
    library.toggle_all()
    ticked = set(library.checked)
    chosen = sorted(library.records)[1]
    library.record_tree.selection_set(chosen)
    app.root.update()
    selected = library.records[chosen].game_id
    library.verify()
    wait(app)
    rows = [library.verify_tree.item(i, "values") for i in library.verify_tree.get_children()]
    assert rows and all(r[0] == "OK" for r in rows)

    def choose(choice: str) -> None:
        app.open_settings()
        app.last_dialog.language.set(language_label(choice))
        app.last_dialog.accept()
        app.root.update()
        wait(app)

    for choice in (i18n.EN, i18n.AUTO):
        choose(choice)
        library = app.library
        assert library.checked == ticked, choice
        current = library.selected_record()
        assert current is not None and current.game_id == selected, choice
        again = [library.verify_tree.item(i, "values") for i in library.verify_tree.get_children()]
        assert [r[:7] for r in again] == [r[:7] for r in rows], choice
    assert i18n.language() == "zh"


# ------------------------------------------------------------------------- 1.2.0 (D73)


def test_auto_snapshots_and_where_a_snapshot_came_from_are_shown(
    app, answers: Answers, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D73 3: an automatic backup says 「自动」 on the library page; D73 14: the restore
    dialog says which version a snapshot came from (「来自 1.0」)."""
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.gui.views.dialogs import RestoreDialog
    from enginesave.repo import Repo

    built = build_f1(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "library", saves_root_override=saves_root)
    execute_backup(plan, Repo.open_or_create(plan.dest), reason="auto")
    answers.directory = str(games_root)
    app.add_root()
    assert app.set_repo(tmp_path / "library")
    wait(app)
    library = app.library
    library.record_tree.selection_set(next(iter(library.records)))
    app.root.update()
    rows = [library.snapshot_tree.item(i, "values") for i in library.snapshot_tree.get_children()]
    assert [str(r[2]) for r in rows] == ["自动"]
    library.restore()
    dialog = app.last_dialog
    assert isinstance(dialog, RestoreDialog)
    settle(app)
    assert dialog.snapshot.get().endswith("来自 1.0")
    assert str(built.root) in {str(p) for p, _ in dialog.candidates}
    dialog.cancel()
    assert answers.messages("showerror") == []


def test_the_window_opens_the_library_and_record_it_is_given(
    root, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D73 10: ``--library DIR --game ID``: the library page with the record selected; the
    settings keep their own library."""
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.gui.app import App, option_value
    from enginesave.repo import Repo

    build_f1(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "library", saves_root_override=saves_root)
    execute_backup(plan, Repo.open_or_create(plan.dest))
    settings_file = tmp_path / "appdata" / "EngineSave" / "gui.json"
    usual = tmp_path / "usual library"
    save_settings(GuiSettings(repo_path=str(usual)), settings_file)
    window = App(
        root,
        settings_file=settings_file,
        logs=settings_file.parent / "logs",
        env=env_without_override(),
        library=tmp_path / "library",
        game="ReadableGame-1600000001",
    )
    try:
        window.root.update()  # the library is read once the window is up (after_idle)
        wait(window)
        window.root.update()
        assert window.notebook.select() == str(window.library)
        selected = window.library.selected_record()
        assert selected is not None and selected.name == "Readable Game"
        window.save_settings()
        assert json.loads(settings_file.read_text(encoding="utf-8"))["repo_path"] == str(usual)
    finally:
        finish(window)
    args = ["--library", r"D:\备份\库", "--game=abc"]
    assert option_value(args, "--library") == r"D:\备份\库"
    assert option_value(args, "--game") == "abc" and option_value(args, "--x") is None
