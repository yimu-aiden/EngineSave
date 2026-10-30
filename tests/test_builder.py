"""Self-tests for the synthetic fixture builder (uses pickletools only, never unpickles)."""

from __future__ import annotations

import io
import json
import pickletools
import struct
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

from fixtures.builder import (
    RPA_KEY,
    build_f1,
    build_f2,
    build_f8,
    options_stmts,
    render_rpy,
    rpa_bytes,
    rpyc_bytes,
    save_bytes,
)


def _slots(data: bytes) -> dict[int, bytes]:
    assert data[:10] == b"RENPY RPC2"
    pos, slots = 10, {}
    while True:
        slot, start, length = struct.unpack_from("<III", data, pos)
        pos += 12
        if slot == 0:
            return slots
        slots[slot] = data[start : start + length]


def _ops(raw: bytes) -> list[tuple[str, object]]:
    return [(op.name, arg) for op, arg, _ in pickletools.genops(io.BytesIO(raw))]


def test_render_rpy_reports_statement_lines() -> None:
    text, lines = render_rpy(options_stmts(save_directory="Abc-1"))
    rendered = text.split("\n")
    assert rendered[lines[0] - 1].startswith('define config.name = _("Fixture Game")')
    assert 'define config.save_directory = "Abc-1"' in text


@pytest.mark.parametrize(
    ("style", "protocol", "expr_marker", "builds_expr_by"),
    [
        ("8.1", 2, "renpy.ast PyExpr", "NEWOBJ"),
        ("8.4", 5, "renpy.astsupport", "REDUCE"),
    ],
)
def test_rpyc_matches_real_pickle_shape(
    style: str, protocol: int, expr_marker: str, builds_expr_by: str
) -> None:
    data = rpyc_bytes(options_stmts(save_directory="Abc-1"), "game/options.rpy", style)
    slots = _slots(data)
    assert set(slots) == {1, 2}
    assert slots[1] == slots[2]
    ops = _ops(zlib.decompress(slots[1]))
    assert ops[0] == ("PROTO", protocol)
    strings = {arg for name, arg in ops if isinstance(arg, str)}
    assert '"Abc-1"' in strings
    assert "save_directory" in strings
    assert any(expr_marker in str(arg) for _, arg in ops)
    names = [name for name, _ in ops]
    assert "NEWOBJ" in names and "BUILD" in names
    assert builds_expr_by in names
    if style == "8.1":
        assert ("GLOBAL", "__builtin__ list") in ops


def test_fake_renpy_modules_do_not_leak() -> None:
    rpyc_bytes(options_stmts(), "game/options.rpy")
    assert "renpy" not in sys.modules
    assert "renpy.ast" not in sys.modules


def test_rpa_index_is_xor_obfuscated() -> None:
    data = rpa_bytes({"options.rpy": b"define config.name = 'x'\n"})
    header, rest = data.split(b"\n", 1)
    tag, offset_hex, key_hex = header.split()
    assert tag == b"RPA-3.0" and int(key_hex, 16) == RPA_KEY
    index = zlib.decompress(data[int(offset_hex, 16) :])
    ops = _ops(index)
    assert ("SHORT_BINUNICODE", "options.rpy") in ops or ("BINUNICODE", "options.rpy") in ops
    ints = [arg for name, arg in ops if name.startswith("BININT")]
    assert 34 ^ RPA_KEY in ints


def test_save_zip_carries_version() -> None:
    with zipfile.ZipFile(io.BytesIO(save_bytes("0.7.2", seed=1))) as zf:
        assert set(zf.namelist()) >= {"json", "log", "renpy_version", "screenshot.png"}
        assert json.loads(zf.read("json"))["_version"] == "0.7.2"
    assert save_bytes("1", seed=5) == save_bytes("1", seed=5)


def test_f1_layout_and_mirrors(games_root: Path, saves_root: Path) -> None:
    game = build_f1(games_root, saves_root)
    assert (game.root / "ReadableGame.exe").is_file()
    assert (game.root / "renpy" / "vc_version.py").read_text().count("8.4.1.25072401") == 1
    assert (game.gamedir / "options.rpy").is_file()
    assert (game.gamedir / "options.rpyc").is_file()
    assert sorted(p.name for p in game.local_dir.iterdir()) == sorted(game.local_files)
    assert game.global_dir == saves_root / "ReadableGame-1600000001"
    for name, data in game.local_files.items():
        assert (game.global_dir / name).read_bytes() == data
        assert (game.global_dir / "sync" / name).read_bytes() == data


def test_f2_layout(games_root: Path, saves_root: Path) -> None:
    game = build_f2(games_root, saves_root)
    wrapper = games_root / "InnerGame-0.2-pc"
    assert game.root.parent == wrapper
    assert not (wrapper / "renpy").exists()
    assert "'" in game.root.name
    assert not list(game.gamedir.glob("*.rpy"))
    assert (game.gamedir / "options.rpyc").is_file()
    assert (game.gamedir / "game" / "gui.rpyc").is_file()
    assert (game.gamedir / "archive.rpa").read_bytes().startswith(b"RPA-3.0 ")
    assert len(game.local_files) == 3


def test_f8_layout(saves_root: Path) -> None:
    misc = build_f8(saves_root)
    present = sorted(p.name for p in saves_root.iterdir())
    assert present == sorted([*misc.unlinked, *misc.excluded])
