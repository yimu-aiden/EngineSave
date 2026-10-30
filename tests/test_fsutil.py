from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from enginesave import fsutil
from enginesave.fsutil import UnsafePathError
from fixtures import builder


def make_junction(link: Path, target: Path) -> None:
    if not builder.make_junction(link, target):
        pytest.skip("cannot create a junction or symlink here")


@pytest.mark.parametrize(
    "rel",
    [
        "../evil",
        "..\\evil",
        "a/../b",
        "/abs/file",
        "\\abs\\file",
        "C:\\Windows\\x",
        "c:relative",
        "a//b",
        "./a",
        "CON",
        "aux.txt",
        "dir/nul.save",
        "a:stream",
        "trailing./x",
        "space /x",
        "nul\0byte",
        "",
    ],
)
def test_safe_join_rejects_unsafe(tmp_path: Path, rel: str) -> None:
    with pytest.raises(UnsafePathError):
        fsutil.safe_join(tmp_path, rel)


def test_safe_join_accepts_nested_posix_and_windows_separators(tmp_path: Path) -> None:
    assert fsutil.safe_join(tmp_path, "sync/1-1-LT1.save") == tmp_path / "sync" / "1-1-LT1.save"
    assert fsutil.safe_join(tmp_path, "sync\\persistent") == tmp_path / "sync" / "persistent"


@pytest.mark.parametrize("name", ["", "..", "a..b", "a/b", "a\\b", "C:", "x\0"])
def test_safe_relative_component_rejects(name: str) -> None:
    with pytest.raises(UnsafePathError):
        fsutil.safe_relative_component(name)


def test_safe_relative_component_accepts_typical_ids() -> None:
    for name in ["MoonlitHarbor", "Willowpromise-1637695238", "Lanternfall-1574721269-steam"]:
        assert fsutil.safe_relative_component(name) == name


def test_is_within_and_same_path(tmp_path: Path) -> None:
    assert fsutil.is_within(tmp_path / "a" / "b", tmp_path)
    assert fsutil.is_within(tmp_path, tmp_path)
    assert not fsutil.is_within(tmp_path, tmp_path / "a")
    assert not fsutil.is_within(tmp_path.parent / (tmp_path.name + "x"), tmp_path)
    if os.name == "nt":
        assert fsutil.same_path(str(tmp_path).upper(), str(tmp_path).lower())


def test_sha256_file_streams(tmp_path: Path) -> None:
    data = os.urandom(fsutil.CHUNK_SIZE + 123)
    target = tmp_path / "blob.bin"
    target.write_bytes(data)
    assert fsutil.sha256_file(target) == (hashlib.sha256(data).hexdigest(), len(data))


def test_atomic_write_text_replaces_without_leftovers(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "catalog.json"
    fsutil.atomic_write_text(target, "first\n")
    fsutil.atomic_write_text(target, "second\n")
    assert target.read_bytes() == b"second\n"
    assert sorted(p.name for p in target.parent.iterdir()) == ["catalog.json"]


def test_atomic_write_text_cleans_up_on_failure(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "catalog.json"
    target.write_text("old")

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fsutil.os, "replace", boom)
    with pytest.raises(OSError):
        fsutil.atomic_write_text(target, "new")
    assert target.read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["catalog.json"]


def test_long_path_prefix(tmp_path: Path) -> None:
    long_dir = tmp_path.joinpath(*(["d" * 50] * 6))
    text = fsutil.long_path(long_dir)
    if os.name == "nt":
        assert text.startswith("\\\\?\\")
        assert fsutil.long_path(text) == text
    assert fsutil.long_path(tmp_path) == os.path.abspath(tmp_path)


def test_long_paths_round_trip(tmp_path: Path) -> None:
    deep = tmp_path.joinpath(*(["segment" * 6] * 6))
    target = deep / "1-1-LT1.save"
    os.makedirs(fsutil.long_path(deep))
    fsutil.atomic_write_text(target, "payload")
    files, errors = fsutil.walk_files(tmp_path)
    assert not errors
    assert [f.rel.rsplit("/", 1)[-1] for f in files] == ["1-1-LT1.save"]
    assert fsutil.sha256_file(target)[1] == len("payload")


def test_walk_files_is_sorted_and_recursive(tmp_path: Path) -> None:
    for rel in ["b.save", "a.save", "sync/z.save", "sync/deeper/x.save"]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * len(rel))
    files, errors = fsutil.walk_files(tmp_path)
    assert not errors
    assert [f.rel for f in files] == ["a.save", "b.save", "sync/deeper/x.save", "sync/z.save"]
    assert fsutil.dir_stats(tmp_path) == (True, 4, sum(f.size for f in files))
    assert fsutil.dir_stats(tmp_path / "missing") == (False, 0, 0)


def test_walk_files_skips_junctions(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.save").write_bytes(b"1")
    make_junction(tmp_path / "real" / "loop", tmp_path)
    files, errors = fsutil.walk_files(tmp_path / "real")
    assert [f.rel for f in files] == ["f.save"]
    assert [e.rel for e in errors] == ["loop"]
    assert fsutil.is_reparse_point(tmp_path / "real" / "loop")
    assert not fsutil.is_reparse_point(tmp_path / "real")


def test_iso_timestamps_round_trip() -> None:
    ns = 1_775_383_948_889_123_456
    text = fsutil.iso_from_ns(ns)
    assert text.endswith(".889Z")
    assert fsutil.ns_from_iso(text) == ns - 123_456
    assert fsutil.ns_from_iso("2026-01-01T00:00:00Z") % 1_000_000_000 == 0


@pytest.mark.parametrize(
    ("ns", "text"),
    [
        (-1, "1969-12-31T23:59:59.999Z"),
        (-50 * 365 * 86400 * 10**9 - 5, "1920-01-13T23:59:59.999Z"),
        (40_000_000_000 * 10**9, "3237-07-19T23:06:40.000Z"),
    ],
)
def test_iso_timestamps_outside_what_windows_formats(ns: int, text: str) -> None:
    """Computed, not asked of the OS: a file from before 1970 has a time too."""
    assert fsutil.iso_from_ns(ns) == text
    assert fsutil.ns_from_iso(text) // 1_000_000 == ns // 1_000_000
