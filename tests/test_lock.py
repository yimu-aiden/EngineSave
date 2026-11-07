"""1.2.0 (D73 4): one writer per library -- the write lock, exit code 4, stale locks."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from enginesave import lock
from enginesave.cli import main
from enginesave.lock import LOCK_NAME, LibraryBusy, library_lock
from enginesave.repo import Repo, destination_state
from fixtures.builder import build_f1

CLI = "import sys; from enginesave.cli import main; sys.exit(main(sys.argv[1:]))"


def holder(pid: int, **extra: object) -> dict:
    """A lock taken just now (after the process ``pid`` started) by another process."""
    from enginesave.fsutil import iso_utc, utc_now

    return {
        "pid": pid,
        "command": "enginesave backup",
        "started_at": iso_utc(utc_now()),
        "host": socket.gethostname(),
        "token": "theirs",
        **extra,
    }


def write_lock(root: Path, data: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK_NAME
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def other_process():
    """A live process that is not this one (it only sleeps)."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield child
    finally:
        child.kill()
        child.wait()


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_the_lock_is_held_while_the_block_runs_and_removed_after(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    with library_lock(root, "test"):
        data = json.loads((root / LOCK_NAME).read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid() and data["command"] == "test"
        assert data["started_at"].endswith("Z") and data["host"] == socket.gethostname()
        with library_lock(root, "nested"):  # the same process again: counted, not refused
            assert lock.holds(root)
        assert (root / LOCK_NAME).is_file()
    assert not (root / LOCK_NAME).exists() and not lock.holds(root)


def test_the_lock_goes_on_an_error_or_ctrl_c_too(tmp_path: Path) -> None:
    for error in (RuntimeError("boom"), KeyboardInterrupt()):
        with pytest.raises(type(error)), library_lock(tmp_path, "test"):
            raise error
        assert not (tmp_path / LOCK_NAME).exists()


def test_another_live_process_holds_it(tmp_path: Path, other_process) -> None:
    write_lock(tmp_path, holder(other_process.pid))
    with pytest.raises(LibraryBusy) as caught, library_lock(tmp_path, "test"):
        pass
    assert caught.value.exit_code == 4
    assert caught.value.holder["pid"] == other_process.pid
    assert "token" not in caught.value.holder
    assert (tmp_path / LOCK_NAME).is_file(), "someone else's lock is never removed"


def test_a_stale_lock_is_removed_and_logged(tmp_path: Path, caplog) -> None:
    write_lock(tmp_path, holder(dead_pid()))
    with library_lock(tmp_path, "test"):
        assert json.loads((tmp_path / LOCK_NAME).read_text())["pid"] == os.getpid()
    assert "removed the stale lock" in caplog.text


def test_a_reused_process_id_makes_the_lock_stale(tmp_path: Path, other_process) -> None:
    """The process id belongs to a process started after the lock was taken."""
    time.sleep(0.05)
    write_lock(tmp_path, holder(other_process.pid, started_at="2000-01-01T00:00:00Z"))
    with library_lock(tmp_path, "test"):
        pass


def test_another_computers_lock_is_never_stale(tmp_path: Path) -> None:
    write_lock(tmp_path, holder(dead_pid(), host="SOME-NAS-CLIENT"))
    with pytest.raises(LibraryBusy, match="on SOME-NAS-CLIENT"), library_lock(tmp_path, "t"):
        pass


def test_an_unreadable_lock_is_busy_until_it_is_old(tmp_path: Path) -> None:
    path = tmp_path / LOCK_NAME
    path.write_bytes(b"")
    with pytest.raises(LibraryBusy), library_lock(tmp_path, "test"):
        pass
    old = time.time() - lock.UNREADABLE_STALE_SECONDS - 5
    os.utime(path, (old, old))
    with library_lock(tmp_path, "test"):
        pass


def test_a_new_library_folder_holding_only_the_lock_counts_as_empty(tmp_path: Path) -> None:
    root = tmp_path / "new"
    with library_lock(root, "backup", create=True):
        assert root.is_dir() and destination_state(root) == "new"
        Repo.open_or_create(root)
    assert (root / "catalog.json").is_file() and not (root / LOCK_NAME).exists()
    empty = tmp_path / "unused"
    with library_lock(empty, "backup", create=True):
        pass
    assert not empty.exists(), "a folder made only for the lock is removed again"


def run(args: list[str], capsys) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_writing_command_exits_4_and_says_who_holds_it(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys, other_process
) -> None:
    build_f1(games_root, saves_root)
    dest = tmp_path / "library"
    common = [str(games_root), "--renpy-saves-root", str(saves_root), "--dest", str(dest)]
    assert run(["backup", *common, "--yes"], capsys)[0] == 0
    write_lock(dest, holder(other_process.pid))
    code, out, err = run(["backup", *common, "--yes", "--json"], capsys)
    assert code == 4 and "the library is busy" in err and "try again later" in err
    data = json.loads(out)
    assert data["error"] == "library_busy" and data["holder"]["pid"] == other_process.pid
    assert data["holder"]["command"] == "enginesave backup"
    assert (
        len(Repo.open(dest).snapshot_ids(Repo.open(dest).load_catalog()["games"][0]["game_id"]))
        == 1
    )
    game = ["--repo", str(dest), "--game", "readable game"]
    for args in (
        ["link", *game, "--game-root", str(games_root / "Readable Game 1.0")],
        ["remove", *game, "--yes"],
        ["restore", *game, "--yes", "--renpy-saves-root", str(saves_root)],
        ["migrate", "--repo", str(dest)],
    ):
        assert run(args, capsys)[0] == 4, args
    # reading is never held up, nor are previews
    assert run(["list", "--repo", str(dest), "--json"], capsys)[0] == 0
    assert run(["verify", "--repo", str(dest)], capsys)[0] == 0
    assert run(["show", *game], capsys)[0] == 0
    assert run(["remove", *game], capsys)[0] == 0  # without --yes: only the preview
    assert run(["backup", *common, "--dry-run"], capsys)[0] == 0
    assert (dest / LOCK_NAME).is_file()


def test_two_backups_at_once_the_second_exits_4(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys, monkeypatch
) -> None:
    """While one backup runs (here: it is held at its first game), a second one into the same
    library stops at once with exit code 4 and writes nothing."""
    from enginesave import backup

    build_f1(games_root, saves_root)
    dest = tmp_path / "library"
    common = [str(games_root), "--renpy-saves-root", str(saves_root), "--dest", str(dest)]
    seen: list[int] = []
    real = backup.backup_game

    def second_backup_meanwhile(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        child = subprocess.run(
            [sys.executable, "-c", CLI, "backup", *common, "--yes", "--json"],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(lock.__file__).parents[1])},
            check=False,
        )
        seen.append(child.returncode)
        assert json.loads(child.stdout)["error"] == "library_busy"
        return real(*args, **kwargs)

    monkeypatch.setattr(backup, "backup_game", second_backup_meanwhile)
    assert run(["backup", *common, "--yes"], capsys)[0] == 0
    assert seen == [4]
    repo = Repo.open(dest)
    (record,) = repo.load_catalog()["games"]
    assert len(repo.snapshot_ids(record["game_id"])) == 1
    assert not (dest / LOCK_NAME).exists()


def test_the_window_says_the_library_is_busy(tmp_path: Path, other_process) -> None:
    from enginesave.gui.app import BUSY_TEXT, locked_job

    write_lock(tmp_path, holder(other_process.pid))
    job = locked_job(lambda _p: "written", tmp_path, "备份")
    with pytest.raises(LibraryBusy) as caught:
        job(lambda _v: None)
    assert str(caught.value).startswith(BUSY_TEXT)
    (tmp_path / LOCK_NAME).unlink()
    assert job(lambda _v: None) == "written" and not (tmp_path / LOCK_NAME).exists()


def test_a_mirror_target_holding_only_a_lock_is_new(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    from enginesave.mirror import check_target

    build_f1(games_root, saves_root)
    dest = tmp_path / "library"
    args = [str(games_root), "--renpy-saves-root", str(saves_root), "--dest", str(dest), "--yes"]
    assert run(["backup", *args], capsys)[0] == 0
    target = tmp_path / "mirror"
    with library_lock(target, "mirror", create=True):
        assert check_target(Repo.open(dest), target) == "new"


def test_a_folder_that_is_no_library_never_gets_a_lock(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys, monkeypatch
) -> None:
    """A non-empty folder that is not a library is never written into (D72), not even the
    lock: the backup refuses it with exit code 2 as before."""
    taken: list[str] = []
    real = lock._create
    monkeypatch.setattr(
        lock, "_create", lambda path, cmd: taken.append(str(path)) or real(path, cmd)
    )
    build_f1(games_root, saves_root)
    theirs = tmp_path / "my documents"
    theirs.mkdir()
    (theirs / "letter.txt").write_text("mine")
    args = [str(games_root), "--renpy-saves-root", str(saves_root), "--dest", str(theirs), "--yes"]
    code, _, err = run(["backup", *args], capsys)
    assert code == 2 and "is not empty and has no catalog.json" in err
    assert taken == [] and sorted(p.name for p in theirs.iterdir()) == ["letter.txt"]
