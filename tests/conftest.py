from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """D66 11: where the temporary folders of this run are, for ``fixtures.fsprobe``: the
    folder ``--basetemp`` is made in, or ``PYTEST_DEBUG_TEMPROOT``."""
    base = config.option.basetemp
    folder = Path(base).resolve().parent if base else os.environ.get("PYTEST_DEBUG_TEMPROOT")
    if folder and Path(folder).is_dir():
        os.environ["ENGINESAVE_FSPROBE_DIR"] = str(folder)


# Captured at import time, before any test-level isolation, for the optional real-sample tests.
REAL_APPDATA = os.environ.get("APPDATA")

from enginesave import roots as _roots  # noqa: E402

REAL_KNOWN_FOLDER = _roots.known_folder  # the one test of the real reader calls it, read only


@pytest.fixture(autouse=True, scope="session")
def real_settings_untouched():
    """0.9.0 (D52 3, D57): no test reads or writes the user's real ``scan-cache.json`` or
    ``mirrors.json``. Session scope, so it also covers module fixtures that scan with the real
    ``APPDATA`` (the real-sample tests); neither file is derived from a scan's environment."""
    from enginesave import mirror, scan_cache
    from enginesave.fsutil import is_within

    patch = pytest.MonkeyPatch()
    # SaveTrail's folder too: EngineSave takes its settings over from there once
    forbidden = (
        [Path(REAL_APPDATA) / "EngineSave", Path(REAL_APPDATA) / "SaveTrail"]
        if REAL_APPDATA
        else []
    )

    def checked(path: Path) -> None:
        if any(is_within(path, folder) for folder in forbidden):
            raise AssertionError(f"a test reached the real configuration folder: {path}")

    for module in (scan_cache, mirror):
        real_read, real_write = module._read, module._write

        def read(path: Path, real_read=real_read) -> str:
            checked(path)
            return real_read(path)

        def write(path: Path, text: str, real_write=real_write) -> None:
            checked(path)
            real_write(path, text)

        patch.setattr(module, "_read", read)
        patch.setattr(module, "_write", write)
    yield
    patch.undo()


@pytest.fixture(autouse=True)
def isolated_env(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Keep every test away from the real ``%APPDATA%\\RenPy`` and ``RENPY_PATH_TO_SAVES``, and
    (0.10.0, D65 1) from the real profile folders other engines save in: each root token is
    pointed into the sandbox (``ENGINESAVE_ROOT_<TOKEN>`` comes before the system default)."""
    sandbox = tmp_path_factory.mktemp("env")
    monkeypatch.setenv("APPDATA", str(sandbox / "AppData" / "Roaming"))
    monkeypatch.setenv("ENGINESAVE_RENPY_SAVES_ROOT", str(sandbox / "default-saves-root"))
    monkeypatch.delenv("ENGINESAVE_ROOT_RENPY_SAVES_ROOT", raising=False)
    for token, folder in (
        ("UNITY_LOCALLOW", sandbox / "profile" / "AppData" / "LocalLow"),
        ("LOCALAPPDATA", sandbox / "AppData" / "Local"),
        ("APPDATA", sandbox / "AppData" / "Roaming"),
        ("DOCUMENTS", sandbox / "profile" / "Documents"),  # 0.11.0 (D69 3)
        ("SAVED_GAMES", sandbox / "profile" / "Saved Games"),
        ("VIRTUALSTORE", sandbox / "AppData" / "Local" / "VirtualStore"),
    ):
        monkeypatch.setenv(f"ENGINESAVE_ROOT_{token}", str(folder))
    monkeypatch.delenv("RENPY_PATH_TO_SAVES", raising=False)
    # 0.10.0 (D65): Unity's PlayerPrefs -- no test reads the user's registry by accident (the
    # one that checks the real reader calls it itself, read-only); nothing ever writes it
    from enginesave import registry

    monkeypatch.setattr(registry, "reader", lambda subkey: None)
    # 0.11.0 (D69 3, M48 review): 「文档」 and 「保存的游戏」 come from Windows' known folders,
    # not from USERPROFILE: a test that drops ENGINESAVE_ROOT_* (the window's
    # env_without_override) gets the sandbox's stand-ins, never the user's real folders
    from enginesave import roots

    known = {
        roots.KNOWN_FOLDERS[roots.DOCUMENTS]: sandbox / "profile" / "Documents",
        roots.KNOWN_FOLDERS[roots.SAVED_GAMES]: sandbox / "profile" / "Saved Games",
    }
    monkeypatch.setattr(roots, "known_folder", lambda folder_id: known.get(folder_id))
    # 0.11.0 (D68): the window speaks Chinese in the tests whatever this Windows' language;
    # a test that switches to English is back in Chinese afterwards
    from enginesave.gui import i18n

    monkeypatch.setattr(i18n, "default_language", lambda: i18n.ZH)
    i18n.set_language(i18n.ZH)
    yield sandbox
    i18n.set_language(i18n.ZH)


@pytest.fixture(autouse=True)
def no_real_dialogs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Rule 15: no test opens a window the user would see. A real message box or file dialog
    is modal: a test that reached one would wait on the user's screen (it happened once, in
    M51). Each is replaced by one that answers "no"/nothing and is recorded; the test then
    fails. A test that expects dialogs uses its own fakes (``answers`` in the GUI tests),
    which replace these."""
    from tkinter import filedialog, messagebox

    opened: list[str] = []

    def fake(name: str) -> object:
        def dialog(*args: object, **_kwargs: object) -> object:
            opened.append(f"{name}{args[:2]!r}")
            return False if name.startswith("ask") and "file" not in name else None

        return dialog

    for name in (
        "showinfo", "showwarning", "showerror", "askyesno", "askokcancel", "askyesnocancel",
        "askquestion", "askretrycancel",
    ):  # fmt: skip
        monkeypatch.setattr(messagebox, name, fake(name))
    for name in ("askdirectory", "askopenfilename", "asksaveasfilename", "askopenfilenames"):
        monkeypatch.setattr(filedialog, name, fake(name))
    yield opened
    assert not opened, f"a test reached a real dialog: {opened[:3]}"


@pytest.fixture(autouse=True)
def cleanup_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 16: no test reaches the real recycle bin or depends on the processes running here.

    The Windows calls live in ``enginesave.cleanup.windows`` and every caller looks them up
    there, so this is the one place to replace them. A test that wants the recycle path
    replaces ``shell_file_operation`` with its own fake.
    """
    from enginesave.cleanup import RecycleBin, recycle_bin, windows

    def refuse(_operation: object) -> int:
        raise AssertionError("a test reached the real recycle bin; fake shell_file_operation")

    monkeypatch.setattr(windows, "shell_file_operation", refuse)
    monkeypatch.setattr(windows, "list_process_images", lambda: [])
    # D41: no test lists or purges the real $RECYCLE.BIN; tests of it bring a synthetic one.
    monkeypatch.setattr(windows, "user_sid", lambda: "S-1-5-21-0-0-0-1000")
    monkeypatch.setattr(recycle_bin, "bin_folder", lambda _drive: None)
    # D35: a roomy recycle bin, whatever this machine's bins hold; tests of the capacity
    # rules replace it (the real one is only ever asked, never filled).
    monkeypatch.setattr(
        windows,
        "recycle_bin_info",
        lambda drive: RecycleBin(drive, capacity=64 * 1024**3, used=0),
    )


class SyntheticBin:
    """A recycle bin folder (``$RECYCLE.BIN\\<SID>``) inside the test's temporary folder."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.capacity = 1024**3

    def used(self) -> int:
        return sum(p.stat().st_size for p in self.folder.rglob("*") if p.is_file())

    def pairs(self) -> list[str]:
        return sorted(p.name[2:] for p in self.folder.glob("$I*"))

    def leave_room(self, room: int) -> None:
        """Set the capacity so that exactly ``room`` bytes are left (5 % kept free)."""
        used = self.used()
        capacity = (room + used) * 100 // 95
        while capacity - used - capacity * 5 // 100 < room:
            capacity += 1
        while capacity - used - capacity * 5 // 100 > room:
            capacity -= 1
        self.capacity = capacity


@pytest.fixture
def synthetic_bin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SyntheticBin:
    """D41: the shell "recycles" a temporary folder into a synthetic bin as ``$R``/``$I``
    pairs (with the time it happened); the bin's size is what is in it; listing and purging
    work on this folder only. The real recycle bin is never touched."""
    import ctypes
    import time

    from enginesave.cleanup import DRIVE_FIXED, RecycleBin, recycle_bin, windows
    from enginesave.fsutil import is_within
    from fixtures.builder import recycle_like_windows

    synthetic = SyntheticBin(tmp_path / "$RECYCLE.BIN" / "S-1-5-21-0-0-0-1000")
    synthetic.folder.mkdir(parents=True)

    def recycle(operation: windows.SHFILEOPSTRUCTW) -> int:
        path = Path(ctypes.wstring_at(operation.pFrom))
        assert is_within(path, tmp_path), "the synthetic bin only takes temporary folders"
        recycle_like_windows(path, synthetic.folder, time.time_ns())
        return 0

    def bin_of(drive: str) -> RecycleBin:
        return RecycleBin(drive, capacity=synthetic.capacity, used=synthetic.used())

    monkeypatch.setattr(windows, "shell_file_operation", recycle)
    monkeypatch.setattr(windows, "has_recycle_bin", lambda _anchor: True)
    monkeypatch.setattr(windows, "drive_type", lambda _anchor: DRIVE_FIXED)
    monkeypatch.setattr(windows, "recycle_bin_info", bin_of)
    monkeypatch.setattr(recycle_bin, "bin_folder", lambda _drive: synthetic.folder)
    return synthetic


@pytest.fixture
def saves_root(tmp_path: Path) -> Path:
    path = tmp_path / "RenPy"
    path.mkdir()
    return path


@pytest.fixture
def games_root(tmp_path: Path) -> Path:
    path = tmp_path / "games"
    path.mkdir()
    return path


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every script the detector reads: loose files, archive indexes and archive members."""
    from enginesave.engines.renpy import detector

    seen: list[str] = []
    real_bytes, real_index, real_member = (
        detector._read_bytes,
        detector.read_index,
        detector.read_member,
    )

    def read_bytes(path: Path) -> bytes:
        seen.append(Path(path).name)
        return real_bytes(path)

    def read_index(path: Path):  # noqa: ANN202 - the reader's own type
        seen.append(f"{Path(path).name}#index")
        return real_index(path)

    def read_member(archive: Path, member, stream=None):  # noqa: ANN001, ANN202
        seen.append(f"{Path(archive).name}!{member.name}")
        return real_member(archive, member, stream=stream)

    monkeypatch.setattr(detector, "_read_bytes", read_bytes)
    monkeypatch.setattr(detector, "read_index", read_index)
    monkeypatch.setattr(detector, "read_member", read_member)
    return seen
