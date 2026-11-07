"""Cleanup after a verified backup (D26, plan section 19 M13).

Rule 16: every folder removed here is a fixture copy inside the pytest temporary folder; the
recycle bin is always a fake (conftest makes the real call fail), and the fake only removes
folders below ``tmp_path``.
"""

from __future__ import annotations

import contextlib
import ctypes
import io
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from enginesave import cleanup, layout
from enginesave.backup import execute_backup, prepare_backup
from enginesave.cleanup import (
    GAME_NOT_IN_BACKUP,
    GAME_ROOT,
    GLOBAL,
    PERMANENT,
    UNLINKED,
    execute_cleanup,
    parse_size,
    plan_cleanup,
)
from enginesave.cli import main
from enginesave.errors import UsageError
from enginesave.fsutil import is_within
from enginesave.identity import link_suggestions
from enginesave.repo import Repo
from enginesave.report import render_cleanup_result
from enginesave.scan import ScanOptions, run_scan
from fixtures.builder import BuiltGame, build_f1, build_f8, make_junction, recycle_like_windows

Library = tuple[Repo, dict, BuiltGame]


class FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


def back_up(games_root: Path, saves_root: Path, dest: Path, **options: object) -> Repo:
    plan = prepare_backup([games_root], dest, saves_root_override=saves_root, **options)
    repo = Repo.open_or_create(plan.dest)
    assert all(r.status == "ok" for r in execute_backup(plan, repo).results)
    return repo


@pytest.fixture
def library(games_root: Path, saves_root: Path, tmp_path: Path) -> Library:
    built = build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "repo")
    (record,) = repo.load_catalog()["games"]
    return repo, record, built


def check(library: Library, saves_root: Path, targets, **options) -> cleanup.CleanupPlan:
    repo, record, _ = library
    return plan_cleanup(repo, record, targets, saves_root_override=saves_root, **options)


def reasons(plan: cleanup.CleanupPlan, kind: str) -> str:
    return " | ".join(next(t for t in plan.targets if t.kind == kind).reasons)


@pytest.fixture
def recycle_bin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict]:
    """A fake SHFileOperationW that records each call and removes the (temporary) folder."""
    calls: list[dict] = []

    def fake(operation: cleanup.SHFILEOPSTRUCTW) -> int:
        path = ctypes.wstring_at(operation.pFrom)
        calls.append(
            {
                "path": path,
                "raw": ctypes.wstring_at(operation.pFrom, len(path) + 2),
                "func": operation.wFunc,
                "flags": operation.fFlags,
            }
        )
        assert is_within(path, tmp_path), "the fake only ever removes temporary folders"
        shutil.rmtree(path)
        return 0

    monkeypatch.setattr(cleanup.windows, "shell_file_operation", fake)
    monkeypatch.setattr(cleanup.windows, "has_recycle_bin", lambda _anchor: True)
    monkeypatch.setattr(cleanup.windows, "drive_type", lambda _anchor: cleanup.DRIVE_FIXED)
    return calls


@contextlib.contextmanager
def held_like_a_running_exe(path: Path):
    """Keep ``path`` open without write sharing, as Windows does for a running program."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


def test_the_windows_calls_are_replaced_where_they_are_looked_up(tmp_path: Path) -> None:
    """Rule 16 (D38): the package does not re-export the Windows calls, so patching
    ``enginesave.cleanup.X`` fails loudly instead of silently missing; the recycle path
    really goes through ``cleanup.windows``, where ``conftest.py`` makes it refuse."""
    for name in ("shell_file_operation", "recycle_bin_info", "list_process_images", "rename_dir"):
        assert not hasattr(cleanup, name), name
        assert callable(getattr(cleanup.windows, name))
    folder = tmp_path / "still-here"
    folder.mkdir()
    with pytest.raises(AssertionError, match="real recycle bin"):
        cleanup.execute._recycle(folder)
    assert folder.is_dir()


# ---------------------------------------------------------------------------------- checks


def test_a_fresh_verified_backup_passes(library: Library, saves_root: Path) -> None:
    repo, record, built = library
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)], method=PERMANENT)
    assert plan.ok, [t.reasons for t in plan.targets]
    assert [(t.kind, t.path) for t in plan.targets] == [
        (GAME_ROOT, str(built.root)),
        (GLOBAL, str(built.global_dir)),
    ]
    game_root, global_ = plan.targets
    assert GAME_NOT_IN_BACKUP in game_root.notes and game_root.files > 4
    assert (global_.files, plan.verified_files) == (8, 12)
    assert plan.snapshot_id == repo.latest_snapshot(record["game_id"])
    assert built.root.is_dir() and built.global_dir.is_dir(), "planning deletes nothing"


def test_a_relative_saves_root_means_below_the_current_folder(
    library: Library, saves_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, record, built = library
    monkeypatch.chdir(saves_root.parent)
    plan = plan_cleanup(repo, record, [(GLOBAL, None)], saves_root_override=Path(saves_root.name))
    assert [t.path for t in plan.targets] == [str(built.global_dir)]
    assert "not an absolute path" not in reasons(plan, GLOBAL)


def test_unbacked_or_changed_files_are_refused(library: Library, saves_root: Path) -> None:
    _, _, built = library
    (built.local_dir / "9-9-LT9.save").write_bytes(b"played after the backup")
    changed = sorted(built.global_files)[0]
    (built.global_dir / changed).write_bytes(b"changed after the backup")
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)], method=PERMANENT)
    assert not any(t.ok for t in plan.targets)
    assert "not in the backup: game/saves/9-9-LT9.save" in reasons(plan, GAME_ROOT)
    assert f"changed since the backup: {built.global_dir.name}/{changed}" in reasons(plan, GLOBAL)
    assert all("back up again before cleaning up" in t.notes for t in plan.targets)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing")
def test_a_running_game_is_refused(
    library: Library, saves_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, built = library
    with held_like_a_running_exe(built.root / "ReadableGame.exe"):
        assert cleanup.in_use(built.root / "ReadableGame.exe")
        plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)], method=PERMANENT)
    assert "ReadableGame.exe in use" in reasons(plan, GAME_ROOT)
    assert "ReadableGame.exe in use" in reasons(plan, GLOBAL), "its saves are being written"
    image = built.root / "lib" / "py3-windows-x86_64" / "ReadableGame.exe"
    monkeypatch.setattr(cleanup.windows, "list_process_images", lambda: [str(image)])
    plan = check(library, saves_root, [(GAME_ROOT, None)], method=PERMANENT)
    assert "the game is running (ReadableGame.exe)" in reasons(plan, GAME_ROOT)


def test_a_shared_global_folder_is_refused(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    _, _, built = library
    alone = check(library, saves_root, [(GLOBAL, None)], method=PERMANENT)
    assert "still used by" in reasons(alone, GLOBAL) and str(built.root) in reasons(alone, GLOBAL)
    together = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)], method=PERMANENT)
    assert together.ok
    copy = tmp_path / "more-games" / "Readable Game copy"
    shutil.copytree(built.root, copy)
    report = run_scan(ScanOptions(roots=[copy.parent], saves_root_override=saves_root))
    with_scan = check(
        library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)], method=PERMANENT, report=report
    )
    assert str(copy) in reasons(with_scan, GLOBAL), "another install from this scan uses it"


def test_paths_outside_the_whitelist_are_refused(
    library: Library, saves_root: Path, games_root: Path, tmp_path: Path
) -> None:
    repo, record, built = library
    elsewhere = check(library, saves_root, [(GAME_ROOT, str(games_root))], method=PERMANENT)
    assert "not an install folder of this record" in reasons(elsewhere, GAME_ROOT)
    scan_root = check(
        library, saves_root, [(GAME_ROOT, None)], method=PERMANENT, scan_roots=[built.root]
    )
    assert "is or contains the scan root" in reasons(scan_root, GAME_ROOT)
    wrong_kind = check(library, saves_root, [(UNLINKED, "ReadableGame-1600000001")])
    assert "only an orphaned (unlinked) record" in reasons(wrong_kind, UNLINKED)

    def problems(path: Path) -> str:
        return " | ".join(
            cleanup.plan._path_problems(path, repo=repo, scan_roots=[], saves_roots=[saves_root])
        )

    assert "drive root" in problems(Path(tmp_path.anchor))
    assert "home folder" in problems(Path.home())
    assert "home folder" in problems(Path.home().parent)
    assert "backup library" in problems(repo.root)
    assert "saves root" in problems(saves_root)
    for name in ("tokens", "persistent", "launcher-4"):
        doc = repo.load_game(record["game_id"])
        doc["identity"]["renpy_save_directory"] = name
        repo.save_game(doc)
        (saves_root / name).mkdir(exist_ok=True)
        plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)], method=PERMANENT)
        assert "belongs to Ren'Py itself" in reasons(plan, GLOBAL)


def test_links_are_refused(
    library: Library, saves_root: Path, games_root: Path, tmp_path: Path
) -> None:
    repo, record, built = library
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if not make_junction(built.local_dir / "linked", elsewhere):
        pytest.skip("cannot create a junction here")
    inside = check(library, saves_root, [(GAME_ROOT, None)], method=PERMANENT)
    assert "contains links (game/saves/linked)" in reasons(inside, GAME_ROOT)
    (built.local_dir / "linked").rmdir()
    link = games_root / "Linked Game"
    assert make_junction(link, built.root)
    doc = repo.load_game(record["game_id"])
    doc["installs"].append({"game_root": str(link)})
    repo.save_game(doc)
    plan = check(library, saves_root, [(GAME_ROOT, str(link))], method=PERMANENT)
    assert "is a link" in reasons(plan, GAME_ROOT)
    link.rmdir()


def test_an_incomplete_or_damaged_snapshot_is_refused(library: Library, saves_root: Path) -> None:
    repo, record, built = library
    snapshot_dir = repo.snapshot_dir(record["game_id"], repo.latest_snapshot(record["game_id"]))
    manifest_path = snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps({**manifest, "status": "partial"}), encoding="utf-8")
    partial = check(library, saves_root, [(GAME_ROOT, None)], method=PERMANENT)
    assert "is partial; back up again" in reasons(partial, GAME_ROOT)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    copy = layout.source_data_dir(snapshot_dir, manifest, "local") / sorted(built.local_files)[0]
    copy.unlink()  # breaks only this hard link; the object stays
    copy.write_bytes(b"damaged copy")
    damaged = check(library, saves_root, [(GAME_ROOT, None)], method=PERMANENT)
    assert "does not verify" in reasons(damaged, GAME_ROOT)
    missing = check(
        library, saves_root, [(GAME_ROOT, None)], method=PERMANENT, snapshot_id="20000101T000000Z"
    )
    assert "no snapshot 20000101T000000Z" in reasons(missing, GAME_ROOT)


def test_the_recycle_bin_must_be_real(
    library: Library, saves_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cleanup.windows, "has_recycle_bin", lambda _anchor: False)
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)])
    assert "has no recycle bin; use --permanent" in reasons(plan, GAME_ROOT)
    monkeypatch.setattr(cleanup.windows, "has_recycle_bin", lambda _anchor: True)
    monkeypatch.setattr(cleanup.windows, "drive_type", lambda _anchor: 2)  # removable
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)])
    assert "is not a fixed drive" in reasons(plan, GAME_ROOT)
    monkeypatch.setattr(cleanup.windows, "drive_type", lambda _anchor: cleanup.DRIVE_FIXED)
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)], recycle_limit=16)
    assert "above the --recycle-limit" in reasons(plan, GAME_ROOT)
    assert check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)]).ok


@pytest.mark.parametrize(
    ("capacity", "used", "disabled", "fits", "says"),
    [
        (10 * 1024**2, 0, False, True, "room for 0.0 GB"),  # 10 MB, plenty for the fixture
        (10 * 1024**2, 10 * 1024**2, False, True, "room is made first when it is cleaned up"),
        (10 * 1024**2, 0, True, False, "is turned off (Windows would delete at once)"),
        (None, None, False, True, "size unknown; folders up to 4.0 GB"),
    ],
    ids=["room", "too-small", "turned-off", "unknown"],
)
def test_the_recycle_bin_needs_room_for_the_folder(
    library: Library,
    saves_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capacity: int | None,
    used: int | None,
    disabled: bool,
    fits: bool,
    says: str,
) -> None:
    """D35: the room left in the drive's recycle bin (capacity - used - 5 %) is the limit;
    a turned-off bin refuses, an unreadable one falls back to 4 GB. Since 0.7.0 (D41) a bin
    that is too small does not refuse: the folder ``needs_room``, made when it is cleaned up.
    The real bin is never asked here, let alone filled."""
    monkeypatch.setattr(cleanup.windows, "has_recycle_bin", lambda _anchor: True)
    monkeypatch.setattr(cleanup.windows, "drive_type", lambda _anchor: cleanup.DRIVE_FIXED)
    asked: list[str] = []

    def bin_of(drive: str) -> cleanup.RecycleBin:
        asked.append(drive)
        return cleanup.RecycleBin(drive, capacity=capacity, used=used, disabled=disabled)

    monkeypatch.setattr(cleanup.windows, "recycle_bin_info", bin_of)
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)])
    assert asked == [Path(library[2].root).anchor.upper()], "asked once per drive"
    assert plan.ok is fits
    target = next(t for t in plan.targets if t.kind == GLOBAL)
    assert target.recycle_refused is not fits
    (bin_,) = plan.recycle_bins.values()
    text = " | ".join([bin_.describe(), reasons(plan, GLOBAL), *target.notes])
    assert says in text
    assert target.needs_room is (used is not None and used > 0)
    if target.needs_room:
        assert "left in the recycle bin of" in text and "capacity 0.0 GB" in text
        assert "enlarge the recycle bin (right-click it > Properties > Custom size)" in text
    data = plan.to_dict()
    assert data["recycle_limit"] is None and data["recycle_bins"][0]["disabled"] is disabled


def bin_with_room(monkeypatch: pytest.MonkeyPatch, room: int) -> None:
    """A recycle bin with exactly ``room`` bytes left (5 % kept free), on every drive."""
    capacity = 1024**3
    used = capacity - capacity * cleanup.RECYCLE_MARGIN_PERCENT // 100 - room

    def bin_of(drive: str) -> cleanup.RecycleBin:
        return cleanup.RecycleBin(drive, capacity=capacity, used=used)

    monkeypatch.setattr(cleanup.windows, "recycle_bin_info", bin_of)


def test_the_ledger_adds_up_what_a_run_moves_in() -> None:
    """D41 1: room = capacity - used at the start - moved in by this run - 5 %, per drive;
    once a folder does not fit, the drive waits for the rest of the run."""
    gb = 1024**3
    ledger = cleanup.RecycleLedger(
        bins={
            "E:\\": cleanup.RecycleBin("E:\\", capacity=100 * gb, used=50 * gb),
            "C:\\": cleanup.RecycleBin("C:\\", capacity=10 * gb, used=0),
        }
    )
    assert ledger.room("E:\\") == 45 * gb and ledger.fits("E:\\", 45 * gb)
    ledger.place("E:\\", 40 * gb)
    assert ledger.room("E:\\") == 5 * gb and not ledger.fits("E:\\", 6 * gb)
    ledger.full.add("E:\\")
    assert not ledger.fits("E:\\", 1), "a full drive waits for the rest of the run"
    assert ledger.fits("C:\\", 9 * gb), "other drives go on"
    assert ledger.room("D:\\") is None and ledger.fits("D:\\", 10**15), "unknown: not counted"
    ledger.forget("E:\\")
    assert "E:\\" not in ledger.bins and "E:\\" not in ledger.full and ledger.room("E:\\") is None


def test_a_full_recycle_bin_leaves_the_rest_for_later(
    library: Library,
    saves_root: Path,
    recycle_bin: list[dict],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D41 1: both folders fit on their own, not together. The first is recycled, the second
    is left for later (not a failure, not recorded); ``full_bins`` says what is missing."""
    repo, record, built = library
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)])
    game, global_ = (t.bytes for t in plan.targets)
    bin_with_room(monkeypatch, game + global_ - 1)
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)])
    assert plan.ok and not any(t.needs_room for t in plan.targets), "each fits on its own"
    result = execute_cleanup(plan)
    assert [t["result"] for t in result.targets] == ["recycled", "skipped_full_bin"]
    assert result.exit_code == 1 and len(recycle_bin) == 1
    assert not built.root.exists() and built.global_dir.is_dir()
    drive = Path(built.global_dir).anchor.upper()
    assert result.full_bins == [
        {"drive": drive, "room": global_ - 1, "needed": global_, "missing": 1}
    ]
    data = result.to_dict()
    assert data["skipped_full_bin"] == [str(built.global_dir)]
    (entry,) = repo.load_game(record["game_id"])["cleanups"]
    assert [t["result"] for t in entry["targets"]] == ["recycled"], "only what was done"
    assert "Left for later: 1 folder(s)" in render_cleanup_result(result)


def test_a_folder_bigger_than_the_room_waits_with_the_rest_of_its_drive(
    library: Library, saves_root: Path, recycle_bin: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D41: too big for the room left is not refused at the check (``needs_room``); when the
    cleanup runs it is left for later, and so is everything after it on that drive."""
    _, _, built = library
    bin_with_room(monkeypatch, 1)
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)])
    assert plan.ok and all(t.needs_room for t in plan.targets)
    result = execute_cleanup(plan)
    assert [t["result"] for t in result.targets] == ["skipped_full_bin"] * 2
    assert recycle_bin == [] and built.root.is_dir() and built.global_dir.is_dir()


def test_an_unreadable_recycle_bin_keeps_the_old_limit(
    library: Library, saves_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cleanup.windows, "has_recycle_bin", lambda _anchor: True)
    monkeypatch.setattr(cleanup.windows, "drive_type", lambda _anchor: cleanup.DRIVE_FIXED)
    monkeypatch.setattr(
        cleanup.windows, "recycle_bin_info", lambda drive: cleanup.RecycleBin(drive)
    )
    monkeypatch.setattr(cleanup.model, "DEFAULT_RECYCLE_LIMIT", 16)  # as if the folder were 4 GB+
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)])
    assert "the limit used when the size of the recycle bin" in reasons(plan, GLOBAL)


def test_reading_the_recycle_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry and SHQueryRecycleBinW are only read; here both are stand-ins."""
    import winreg

    values = {"MaxCapacity": 74107, "NukeOnDelete": 0}
    opened: list[str] = []

    class Key:
        def __enter__(self) -> Key:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def open_key(_root: object, path: str) -> Key:
        opened.append(path)
        return Key()

    def query(_key: object, name: str) -> tuple[int, int]:
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], winreg.REG_DWORD

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", query)
    assert cleanup.windows.read_bitbucket("{guid}") == (74107 * 1024**2, False)
    assert opened == [f"{cleanup.BITBUCKET_KEY}\\{{guid}}"]
    values["NukeOnDelete"] = 1
    del values["MaxCapacity"]
    assert cleanup.windows.read_bitbucket("{guid}") == (None, True)

    monkeypatch.setattr(cleanup.windows, "volume_guid", lambda _anchor: "{guid}")
    monkeypatch.setattr(cleanup.windows, "read_bitbucket", lambda _guid: (20 * 1024**3, False))
    monkeypatch.setattr(cleanup.windows, "recycle_bin_usage", lambda _anchor: 3 * 1024**3)
    info = cleanup.windows.read_recycle_bin("E:\\")
    assert (info.capacity, info.used, info.room) == (20 * 1024**3, 3 * 1024**3, 16 * 1024**3)
    assert info.describe() == (
        "recycle bin of E:\\: capacity 20.0 GB, used 3.0 GB, room for 16.0 GB (5% kept free)"
    )
    monkeypatch.setattr(cleanup.windows, "volume_guid", lambda _anchor: None)
    assert cleanup.windows.read_recycle_bin("E:\\").room is None


# ------------------------------------------------------------------------------- execution


def test_permanent_cleanup_removes_and_records(
    library: Library, saves_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, record, built = library
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)], method=PERMANENT)
    result = execute_cleanup(plan)
    assert [t["result"] for t in result.targets] == ["deleted", "deleted"]
    assert not built.root.exists() and not built.global_dir.exists()
    assert result.freed_bytes == sum(t.bytes for t in plan.targets)
    assert not list(built.root.parent.glob("*.enginesave-deleting-*"))
    doc = repo.load_game(record["game_id"])
    (entry,) = doc["cleanups"]
    assert (entry["snapshot_id"], entry["method"]) == (plan.snapshot_id, PERMANENT)
    assert [t["result"] for t in entry["targets"]] == ["deleted", "deleted"]
    assert doc["installs"][0]["removed_at"] == entry["at"]
    (row,) = repo.load_catalog()["games"]
    assert row["cleaned_at"] == entry["at"]
    assert main(["list", "--repo", str(repo.root)]) == 0
    assert f"game removed {entry['at'][:10]}" in capsys.readouterr().out
    assert main(["list", "--repo", str(repo.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["games"][0]["cleaned_at"] == entry["at"]


def test_a_failed_rename_deletes_nothing(
    library: Library, saves_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, record, built = library

    def busy(_source: str, _target: str) -> None:
        raise PermissionError(32, "being used by another process")

    monkeypatch.setattr(cleanup.windows, "rename_dir", busy)
    plan = check(library, saves_root, [(GAME_ROOT, None)], method=PERMANENT)
    (outcome,) = execute_cleanup(plan).targets
    assert outcome["result"] == "failed" and "could not rename" in outcome["error"]
    assert built.root.is_dir() and (built.local_dir / sorted(built.local_files)[0]).is_file()
    assert repo.load_game(record["game_id"])["cleanups"][0]["targets"][0]["result"] == "failed"
    assert "removed_at" not in repo.load_game(record["game_id"])["installs"][0]


def test_recycling_goes_through_the_shell_with_undo(
    library: Library, saves_root: Path, recycle_bin: list[dict]
) -> None:
    _, _, built = library
    plan = check(library, saves_root, [(GAME_ROOT, None)])
    assert plan.method == cleanup.RECYCLE and plan.ok
    (outcome,) = execute_cleanup(plan).targets
    assert outcome["result"] == "recycled" and not built.root.exists()
    (call,) = recycle_bin
    assert call["raw"] == f"{built.root}\0\0", "absolute path, double NUL"
    assert call["func"] == cleanup.FO_DELETE
    assert call["flags"] == (
        cleanup.FOF_ALLOWUNDO
        | cleanup.FOF_NOCONFIRMATION
        | cleanup.FOF_NOERRORUI
        | cleanup.FOF_SILENT
    )


def test_a_failed_recycle_never_becomes_a_permanent_delete(
    library: Library, saves_root: Path, recycle_bin: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, built = library
    monkeypatch.setattr(cleanup.windows, "shell_file_operation", lambda _operation: 0x78)
    (outcome,) = execute_cleanup(check(library, saves_root, [(GAME_ROOT, None)])).targets
    assert outcome["result"] == "failed" and "refused the folder" in outcome["error"]
    assert built.root.is_dir()
    monkeypatch.setattr(cleanup.windows, "shell_file_operation", lambda _operation: 0)
    (outcome,) = execute_cleanup(check(library, saves_root, [(GAME_ROOT, None)])).targets
    assert outcome["result"] == "failed" and "still there" in outcome["error"]
    assert built.root.is_dir() and built.local_dir.is_dir()


def test_folders_are_checked_again_right_before_removal(library: Library, saves_root: Path) -> None:
    _, _, built = library
    plan = check(library, saves_root, [(GAME_ROOT, None)], method=PERMANENT)
    assert plan.ok
    (built.local_dir / "9-9-LT9.save").write_bytes(b"saved while the dialog was open")
    (outcome,) = execute_cleanup(plan).targets
    assert outcome["result"] == "failed" and "changed since the check" in outcome["error"]
    assert built.root.is_dir()


def test_execute_needs_every_folder_to_pass_unless_skipping(
    library: Library, saves_root: Path
) -> None:
    _, _, built = library
    (built.global_dir / sorted(built.global_files)[0]).write_bytes(b"changed")
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)], method=PERMANENT)
    assert [t.ok for t in plan.targets] == [True, False]
    with pytest.raises(UsageError):
        execute_cleanup(plan)
    result = execute_cleanup(plan, skip_failed=True)
    assert [t["kind"] for t in result.targets] == [GAME_ROOT]
    assert not built.root.exists() and built.global_dir.is_dir()


def test_a_folder_already_cleaned_up_is_neither_passed_nor_refused(
    library: Library, saves_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D41: checking again after a cleanup says "already cleaned up (how, when)" instead of
    refusing a folder that does not exist; the CLI has nothing to do and exits 0."""
    repo, record, built = library
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)], method=PERMANENT)
    result = execute_cleanup(plan)
    times = {t["kind"]: t["at"] for t in result.targets}
    (entry,) = repo.load_game(record["game_id"])["cleanups"]
    assert [t["at"] for t in entry["targets"]] == [times[GAME_ROOT], times[GLOBAL]]
    again = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)])
    assert [t.already_cleaned for t in again.targets] == [
        {"method": PERMANENT, "at": times[GAME_ROOT]},
        {"method": PERMANENT, "at": times[GLOBAL]},
    ]
    assert again.active == [] and again.passed == [] and not again.ok
    assert all(not t.reasons and not t.gone for t in again.targets)
    assert again.targets[0].path == str(built.root), "the cleaned-up install is still named"
    base = ["cleanup", "--repo", str(repo.root), "--game", "readable game", "--game-root"]
    code, out, _ = run([*base, "--global", "--dry-run"], capsys)
    assert code == 0 and "CLEANED" in out and "deleted permanently" in out
    assert "Nothing to clean up" in out
    code, out, _ = run([*base, str(built.root), "--json"], capsys)  # named explicitly, no --yes
    data = json.loads(out)
    assert code == 0 and data["result"] is None
    assert data["plan"]["targets"][0]["already_cleaned"]["method"] == PERMANENT


def test_a_missing_folder_without_a_cleanup_is_gone(library: Library, saves_root: Path) -> None:
    """D41: not there and no cleanup recorded (deleted by hand, or never created): "gone",
    not a failure; the other folders are checked as usual."""
    _, _, built = library
    shutil.rmtree(built.global_dir)  # a temporary copy
    plan = check(library, saves_root, [(GAME_ROOT, None), (GLOBAL, None)], method=PERMANENT)
    game, global_ = plan.targets
    assert global_.gone and global_.already_cleaned is None and not global_.reasons
    assert game.ok and plan.active == [game] and plan.ok
    assert plan.to_dict()["targets"][1]["gone"] is True
    shutil.rmtree(built.root)  # removed between the check and the cleanup
    result = execute_cleanup(plan)
    assert [t["result"] for t in result.targets] == ["gone"] and result.exit_code == 0


def test_an_orphaned_folder_can_be_cleaned_up(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    build_f8(saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "repo", include_unlinked=["OrphanTwo"])
    (record,) = repo.load_catalog()["games"]
    assert record["kind"] == "unlinked"
    folder = saves_root / "OrphanTwo"
    wrong = plan_cleanup(repo, record, [(UNLINKED, "OrphanOne")], saves_root_override=saves_root)
    assert "is not the folder of this record" in reasons(wrong, UNLINKED)
    as_global = plan_cleanup(repo, record, [(GLOBAL, None)], saves_root_override=saves_root)
    assert "use the unlinked target" in reasons(as_global, GLOBAL)
    plan = plan_cleanup(
        repo, record, [(UNLINKED, "OrphanTwo")], saves_root_override=saves_root, method=PERMANENT
    )
    assert plan.ok, plan.targets[0].reasons
    assert execute_cleanup(plan).targets[0]["result"] == "deleted" and not folder.exists()
    assert (saves_root / "tokens").is_dir(), "nothing else in the saves root is touched"


def test_reinstalling_clears_the_cleanup_mark(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "repo")
    (record,) = repo.load_catalog()["games"]
    plan = plan_cleanup(
        repo, record, [(GAME_ROOT, None)], saves_root_override=saves_root, method=PERMANENT
    )
    execute_cleanup(plan)
    assert repo.load_catalog()["games"][0]["cleaned_at"]
    doc = repo.load_game(record["game_id"])
    known = [doc]
    scanned_again = build_f1(tmp_path / "new-games", saves_root)
    report = run_scan(
        ScanOptions(roots=[scanned_again.root.parent], saves_root_override=saves_root)
    )
    (game,) = report.games
    game.identity.renpy_save_directory = "Different-1"
    assert link_suggestions(game, known) == [], "no link suggestions to a cleaned-up record"
    shutil.rmtree(scanned_again.root)
    build_f1(games_root, saves_root)
    back_up(games_root, saves_root, repo.root)
    assert "cleaned_at" not in repo.load_catalog()["games"][0]
    assert "removed_at" not in repo.load_game(record["game_id"])["installs"][0]


# ------------------------------------------------------------------------------------- CLI


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_checks_and_dry_run(
    library: Library, saves_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, record, built = library
    base = ["cleanup", "--repo", str(repo.root), "--game", "readable game"]
    base += ["--renpy-saves-root", str(saves_root)]
    code, _, err = run(base, capsys)
    assert code == 2 and "choose what to clean up" in err
    code, out, _ = run([*base, "--game-root", "--global", "--permanent", "--dry-run"], capsys)
    assert code == 0 and "cleanup plan" in out and f"OK       game folder    {built.root}" in out
    assert "note: the game itself (program and assets) is not in the backup" in out
    assert "Checks: 2 of 2 folders can be cleaned up." in out
    (built.local_dir / "9-9-LT9.save").write_bytes(b"new")
    code, out, _ = run([*base, "--game-root", "--global", "--permanent", "--dry-run"], capsys)
    assert code == 2 and "REFUSED  game folder" in out and "not in the backup" in out
    code, _, err = run([*base, "--game-root", "--global", "--permanent", "--yes"], capsys)
    assert code == 2 and "1 folder(s) failed the checks; nothing was deleted" in err
    assert built.root.is_dir() and built.global_dir.is_dir()
    code, _, err = run([*base, "--game-root", "--recycle-limit", "lots"], capsys)
    assert code == 2 and "not a size" in err


def test_cli_confirmation_and_skip_failed(
    library: Library,
    saves_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, built = library
    base = ["cleanup", "--repo", str(repo.root), "--game", "readable game", "--permanent"]
    base += ["--renpy-saves-root", str(saves_root), "--game-root", "--global"]
    monkeypatch.setattr("sys.stdin", io.StringIO("yes\n"))
    code, _, err = run(base, capsys)
    assert code == 2 and "without --yes" in err
    for answer in ("no\n", f"{built.root.name}\n"):  # D40: only 'yes', no folder names
        monkeypatch.setattr("sys.stdin", FakeTty(answer))
        code, _, err = run(base, capsys)
        assert code == 1 and "Aborted: nothing was deleted." in err and built.root.is_dir()
    assert "Delete 2 folder(s) permanently? This cannot be undone." in err
    (built.global_dir / sorted(built.global_files)[0]).write_bytes(b"changed")
    monkeypatch.setattr("sys.stdin", FakeTty("yes\n"))
    code, out, err = run([*base, "--skip-failed"], capsys)
    assert code == 0 and "Delete 1 folder(s) permanently?" in err
    assert "DELETED   game folder" in out and not built.root.exists()
    assert built.global_dir.is_dir(), "the refused folder stays"


@pytest.fixture
def recycled_earlier(library: Library, saves_root: Path, synthetic_bin) -> list[str]:
    """The game folder went into the (synthetic) bin with an earlier cleanup; now its global
    folder is too big for the room left. Returns the command line for it."""
    repo, _, built = library
    first = check(library, saves_root, [(GAME_ROOT, None)])
    assert execute_cleanup(first).targets[0]["result"] == "recycled"
    saves = check(library, saves_root, [(GLOBAL, None)]).targets[0].bytes
    synthetic_bin.leave_room(saves - 1)
    return [
        "cleanup",
        "--repo",
        str(repo.root),
        "--game",
        "readable game",
        "--global",
        "--renpy-saves-root",
        str(saves_root),
    ]


@pytest.mark.parametrize("mode", ["stop", None])
def test_cli_full_bin_stops_by_default_without_a_console(
    recycled_earlier: list[str],
    library: Library,
    synthetic_bin,
    capsys: pytest.CaptureFixture[str],
    mode: str | None,
) -> None:
    """D41: non-interactive (or --on-full-bin stop): what does not fit waits, exit code 1."""
    _, _, built = library
    extra = ["--on-full-bin", mode] if mode else []
    code, out, _ = run([*recycled_earlier, "--yes", "--json", *extra], capsys)
    data = json.loads(out)
    assert code == 1 and built.global_dir.is_dir()
    assert data["result"]["skipped_full_bin"] == [str(built.global_dir)]
    assert data["full_bin"]["mode"] == "stop" and data["full_bin"]["waiting"] == [
        str(built.global_dir)
    ]
    assert data["full_bin"]["purged"] == [] and len(synthetic_bin.pairs()) == 1
    from test_schemas import conforms

    conforms("cleanup", data)


def test_cli_full_bin_stopped_keeps_what_waits(
    recycled_earlier: list[str],
    library: Library,
    synthetic_bin,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D58: Ctrl+C while the full bin is dealt with (here before the permanent delete): exit
    130, ``cancelled``, and the folder not reached is still waiting, untouched."""
    from enginesave.cli import cleaning

    _, _, built = library
    asked: list[bool] = []

    def stop_on_second() -> bool:
        asked.append(True)
        return len(asked) > 1  # the cleanup itself goes on; the follow-up is stopped

    monkeypatch.setattr(cleaning, "should_stop", stop_on_second)
    args = [*recycled_earlier, "--yes", "--json", "--on-full-bin", "permanent"]
    code, out, err = run(args, capsys)
    data = json.loads(out)
    assert code == 130 and "stopped at a safe point" in err
    assert data["full_bin"]["cancelled"] is True and data["full_bin"]["results"] == []
    assert data["full_bin"]["waiting"] == [str(built.global_dir)] and built.global_dir.is_dir()
    from test_schemas import conforms

    conforms("cleanup", data)


def test_cli_full_bin_purges_enginesaves_earlier_items(
    recycled_earlier: list[str],
    library: Library,
    synthetic_bin,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--on-full-bin purge-ours: EngineSave's own earlier item (the game folder) is deleted
    from the bin, then the global folder is recycled; nothing else in the bin is touched."""
    repo, record, built = library
    stranger = built.root.parent.parent / "Stranger"
    stranger.mkdir()
    s_info, s_data = recycle_like_windows(stranger, synthetic_bin.folder, time.time_ns())
    code, out, _ = run(
        [*recycled_earlier, "--yes", "--json", "--on-full-bin", "purge-ours"], capsys
    )
    data = json.loads(out)
    assert code == 0 and not built.global_dir.exists()
    (purged,) = data["full_bin"]["purged"]
    assert purged["result"] == "purged" and purged["path"] == str(built.root)
    (resumed,) = data["full_bin"]["results"]
    assert [t["result"] for t in resumed["targets"]] == ["recycled"]
    assert data["full_bin"]["waiting"] == []
    assert s_info.exists() and s_data.is_dir(), "a stranger in the bin is never touched"
    names = {Path(p).name for p in synthetic_bin.folder.glob("$I*")}
    assert len(names) == 2, "the stranger and the global folder just recycled"
    doc = repo.load_game(record["game_id"])
    assert doc["cleanups"][0]["targets"][0]["purged_from_recycle_bin"]["at"]
    from test_schemas import conforms

    conforms("cleanup", data)


def test_cli_full_bin_permanent_deletes_the_rest(
    recycled_earlier: list[str], library: Library, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, built = library
    code, out, _ = run([*recycled_earlier, "--yes", "--on-full-bin", "permanent"], capsys)
    assert code == 0 and not built.global_dir.exists()
    assert "SKIPPED_FULL_BIN" in out and "DELETED" in out


def test_cli_full_bin_asks_in_a_console(
    recycled_earlier: list[str],
    library: Library,
    synthetic_bin,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ask (a console, no --yes): the plan's yes, then whether to purge EngineSave's items."""
    _, _, built = library
    monkeypatch.setattr("sys.stdin", FakeTty("yes\nyes\n"))
    code, out, err = run(recycled_earlier, capsys)
    assert code == 0 and not built.global_dir.exists()
    assert "Delete these 1 folder(s) from the recycle bin?" in err
    assert str(built.root) in err and "RECYCLED" in out
    monkeypatch.setattr("sys.stdin", FakeTty("no\n"))  # nothing to clean up now
    code, out, _ = run(recycled_earlier, capsys)
    assert code == 0 and "Nothing to clean up" in out


def test_parse_size() -> None:
    assert parse_size("4G") == 4 * 1024**3
    assert parse_size("500mb") == 500 * 1024**2
    assert parse_size("1048576") == 1048576
    assert parse_size("1.5K") == 1536
    for bad in ("", "lots", "-1", "0"):
        with pytest.raises(UsageError):
            parse_size(bad)


def test_a_program_checks_then_cleans_up_an_old_version_without_a_console(
    library: Library,
    saves_root: Path,
    synthetic_bin,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1.2.0 (D73): how a program removes an old version -- ``--dry-run --json`` for the
    complete checks (exit 2 when one fails, nothing deleted), then ``--yes --json`` with no
    console: recycled, and the full-bin choice is ``stop`` by default (D41)."""
    from test_schemas import conforms

    repo, _, built = library
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a console
    base = ["cleanup", "--repo", str(repo.root), "--game", "readable game"]
    base += ["--renpy-saves-root", str(saves_root), "--game-root", str(built.root), "--json"]
    (built.local_dir / "9-9-LT9.save").write_bytes(b"not backed up")
    code, out, _ = run([*base, "--dry-run"], capsys)
    data = json.loads(out)
    assert code == 2 and data["dry_run"] is True and data["result"] is None
    (target,) = data["plan"]["targets"]
    assert target["ok"] is False and any("not in the backup" in r for r in target["reasons"])
    conforms("cleanup", data)
    (built.local_dir / "9-9-LT9.save").unlink()
    code, out, _ = run([*base, "--dry-run"], capsys)
    assert code == 0 and json.loads(out)["plan"]["targets"][0]["ok"] is True
    assert built.root.is_dir()
    code, out, _ = run([*base, "--yes"], capsys)
    data = json.loads(out)
    assert code == 0 and not built.root.exists()
    assert data["result"]["targets"][0]["result"] == "recycled"
    assert len(synthetic_bin.pairs()) == 1
    conforms("cleanup", data)
    record = repo.load_game(repo.load_catalog()["games"][0]["game_id"])
    assert record["installs"][0]["removed_at"], "the record remembers the version is gone"
