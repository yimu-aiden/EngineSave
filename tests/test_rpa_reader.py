from __future__ import annotations

import pickle
import zlib
from pathlib import Path

import pytest

from enginesave.engines.renpy import rpa_reader
from enginesave.engines.renpy.rpa_reader import (
    RpaEntry,
    UnsupportedRpa,
    read_index,
    read_member,
)
from fixtures.builder import RPA_KEY, rpa_bytes

MEMBERS = {
    "options.rpy": b'define config.save_directory = "X-1"\n',
    "images/bg.png": b"\x89PNG" + bytes(range(256)),
    "tl/RU/options.rpy": b"# translation\n",
}


def write(tmp_path: Path, data: bytes, name: str = "archive.rpa") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("version", ["3.0", "2.0"])
def test_index_and_members(tmp_path: Path, version: str) -> None:
    archive = write(tmp_path, rpa_bytes(MEMBERS, version=version))
    entries = read_index(archive)
    assert [e.name for e in entries] == sorted(MEMBERS)
    for entry in entries:
        assert read_member(archive, entry) == MEMBERS[entry.name]
    with archive.open("rb") as stream:  # one open for every member, in any order (D38)
        for entry in reversed(entries):
            assert read_member(archive, entry, stream=stream) == MEMBERS[entry.name]
        with pytest.raises(UnsupportedRpa, match="out of range"):
            read_member(archive, RpaEntry("x", 10**9, 4, b""), stream=stream)


def test_bytes_names_and_prefix(tmp_path: Path) -> None:
    body = b"HEADER-define config.name = 'x'\n"
    header = b"RPA-3.0 %016x %08x\n" % (34 + len(body), RPA_KEY)
    index = {b"game/options.rpy": [(34 ^ RPA_KEY, len(body) ^ RPA_KEY, b"")]}
    archive = write(tmp_path, header + body + zlib.compress(pickle.dumps(index, protocol=2)))
    (entry,) = read_index(archive)
    assert entry.name == "game/options.rpy"
    assert read_member(archive, entry) == body
    prefixed = RpaEntry(entry.name, entry.offset + 7, entry.length, b"HEADER-")
    assert read_member(archive, prefixed) == body


@pytest.mark.parametrize(
    "data",
    [
        b"RPA-1.0 0000000000000000\n",
        b"RPA-4.0 0000000000000010 42424242\n",
        b"PK\x03\x04 zip file\n",
        b"RPA-3.0 zzzz 42424242\n",
        b"RPA-3.0 00000000000fffff 42424242\n",
        b"RPA-2.0 0000000000000010\n" + zlib.compress(b"junk"),
        b"RPA-2.0 0000000000000019\n" + zlib.compress(pickle.dumps(["not", "a", "dict"])),
    ],
)
def test_unsupported_or_malformed(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(UnsupportedRpa):
        read_index(write(tmp_path, data))


def test_malicious_index_is_refused(tmp_path: Path) -> None:
    marker = tmp_path / "pwned.txt"
    evil = b"cos\nsystem\n(S'echo pwned > \"" + str(marker).encode() + b"\"'\ntR."
    data = b"RPA-2.0 %016x\n" % 25 + zlib.compress(evil)
    with pytest.raises(UnsupportedRpa, match="refused global"):
        read_index(write(tmp_path, data))
    assert not marker.exists()


def test_index_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = write(tmp_path, rpa_bytes(MEMBERS))
    monkeypatch.setattr(rpa_reader, "MAX_INDEX_BYTES", 8)
    with pytest.raises(UnsupportedRpa, match="too large"):
        read_index(archive)


def test_member_limits(tmp_path: Path) -> None:
    archive = write(tmp_path, rpa_bytes(MEMBERS))
    entry = next(e for e in read_index(archive) if e.name == "images/bg.png")
    with pytest.raises(UnsupportedRpa, match="larger than"):
        read_member(archive, entry, max_bytes=10)
    with pytest.raises(UnsupportedRpa, match="out of range"):
        read_member(archive, RpaEntry("x", 10**9, 4, b""))
    with pytest.raises(UnsupportedRpa, match="invalid"):
        read_member(archive, RpaEntry("x", 10, 2, b"long prefix"))
