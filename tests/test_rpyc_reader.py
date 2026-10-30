from __future__ import annotations

import os
import pickle
import subprocess
import zlib
from pathlib import Path

import pytest

from enginesave.engines.renpy import rpyc_reader
from enginesave.engines.renpy.literals import DynamicValue
from enginesave.engines.renpy.rpyc_reader import (
    StubBase,
    UnsupportedRpyc,
    extract_rpyc,
    read_rpyc,
    restricted_loads,
)
from fixtures.builder import (
    _AST,
    Define,
    PythonBlock,
    fake_renpy_modules,
    options_stmts,
    rpyc_bytes,
    rpyc_container,
    script_stmts,
)


def values(found: list) -> dict[str, object]:
    return {a.var: a.value for a in found}


@pytest.mark.parametrize("style", ["8.1", "8.4"])
def test_extracts_options_from_both_pickle_styles(style: str) -> None:
    stmts = options_stmts(
        name="Willow's Promise Beta",
        version="0.05.0",
        build_name="Willow'spromise",
        save_directory="Willowpromise-1637695238",
    )
    found = extract_rpyc(rpyc_bytes(stmts, "game/options.rpy", style), "game/options.rpyc")
    assert values(found) == {
        "config.name": "Willow's Promise Beta",
        "config.version": "0.05.0",
        "build.name": "Willow'spromise",
        "config.save_directory": "Willowpromise-1637695238",
    }
    assert {a.evidence.method for a in found} == {"rpyc_define"}
    sd = next(a for a in found if a.var == "config.save_directory")
    assert sd.evidence.file == "game/options.rpyc"
    assert sd.evidence.detail == 'define config.save_directory = "Willowpromise-1637695238"'


def test_tree_shape_uses_stubs() -> None:
    data, nodes = read_rpyc(rpyc_bytes(options_stmts(), "game/options.rpy"))
    assert data["version"] == 5003000
    init = nodes[0]
    assert isinstance(init, StubBase)
    assert type(init).__module__ == "renpy.ast"
    assert type(init).__name__ == "Init"
    define = init.get("block")[0]
    assert define.get("varname") == "name"
    assert define.get("code")._tuple_state[0] == 1


def test_python_early_block_marks_savedir_dynamic() -> None:
    stmts = options_stmts(
        extra=[
            PythonBlock(
                'import os\nconfig.savedir = os.path.join(config.basedir, "saves")', early=True
            )
        ]
    )
    found = extract_rpyc(rpyc_bytes(stmts, "game/options.rpy", "8.4"), "game/options.rpyc")
    savedir = next(a for a in found if a.var == "config.savedir")
    assert isinstance(savedir.value, DynamicValue)
    assert savedir.evidence.method == "python_assign"


def test_extra_savedirs_operator_is_kept() -> None:
    stmts = [Define("config", "extra_savedirs", '["mirror"]', op="+=")]
    for style in ("8.1", "8.4"):
        found = extract_rpyc(rpyc_bytes(stmts, "game/options.rpy", style), "x.rpyc")
        assert [(a.var, a.op, a.value) for a in found] == [
            ("config.extra_savedirs", "+=", ["mirror"])
        ]


def test_define_without_source_is_dynamic() -> None:
    node = _AST.Define(store="store.config", varname="save_directory", code=None)
    with fake_renpy_modules():
        raw = pickle.dumps(({}, [_AST.Init(block=[node])]), protocol=2)
    (found,) = extract_rpyc(rpyc_container(zlib.compress(raw)), "game/options.rpyc")
    assert found.evidence.method == "rpyc_define_without_source"
    assert isinstance(found.value, DynamicValue)


def test_indexed_define_is_ignored() -> None:
    node = _AST.Define(store="store.config", varname="name", code="x", index="k")
    with fake_renpy_modules():
        raw = pickle.dumps(({}, [node]), protocol=2)
    assert extract_rpyc(rpyc_container(zlib.compress(raw)), "f") == []


def test_legacy_headerless_format() -> None:
    tree_bytes = rpyc_bytes(options_stmts(save_directory="Legacy-1"), "game/options.rpy")
    slot = rpyc_reader.rpyc_payload(tree_bytes)
    found = extract_rpyc(zlib.compress(slot), "game/options.rpyc")
    assert values(found)["config.save_directory"] == "Legacy-1"


def test_prefilter_skips_scripts_without_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    data = rpyc_bytes(script_stmts(), "game/script.rpy")

    def fail(raw: bytes) -> object:
        raise AssertionError("should not unpickle")

    monkeypatch.setattr(rpyc_reader, "restricted_loads", fail)
    assert extract_rpyc(data, "game/script.rpyc") == []


@pytest.mark.parametrize(
    "data",
    [
        b"RENPY RPC2\x01\x00",
        b"RENPY RPC2" + (2).to_bytes(4, "little") * 3 + b"\x00" * 12,
        b"RENPY RPC2" + (1).to_bytes(4, "little") + (999).to_bytes(4, "little") * 2 + b"\0" * 12,
        b"RENPY RPC2" + (1).to_bytes(4, "little") + (46).to_bytes(4, "little") * 2 + b"\0" * 60,
        b"not zlib at all",
        zlib.compress(b"not a pickle"),
    ],
)
def test_malformed_files_raise_unsupported(data: bytes) -> None:
    with pytest.raises(UnsupportedRpyc):
        read_rpyc(data)


def test_decompression_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpyc_reader, "MAX_DECOMPRESSED", 64)
    with pytest.raises(UnsupportedRpyc, match="exceeds"):
        read_rpyc(rpyc_bytes(options_stmts(), "game/options.rpy"))


# ------------------------------------------------------------------------ F12 malicious pickles


class Gadget:
    def __init__(self, func: object, args: tuple[object, ...]) -> None:
        self.func, self.args = func, args

    def __reduce__(self) -> tuple[object, ...]:
        return (self.func, self.args)


class Plain:
    def __init__(self) -> None:
        self.x = 1


def malicious_pickles(marker: Path) -> dict[str, bytes]:
    cmd = f'echo pwned > "{marker}"'
    write_marker = f"__import__('pathlib').Path(r'{marker}').write_text('pwned')"
    return {
        "GLOBAL os system": b"cos\nsystem\n(S'" + cmd.encode() + b"'\ntR.",
        "GLOBAL builtins eval": b"cbuiltins\neval\n(S" + repr(write_marker).encode() + b"\ntR.",
        "__builtin__ eval (protocol 2)": pickle.dumps(Gadget(eval, (write_marker,)), protocol=2),
        "builtins exec (protocol 4)": pickle.dumps(Gadget(exec, (write_marker,)), protocol=4),
        "nt/posix system": pickle.dumps(Gadget(os.system, (cmd,)), protocol=2),
        "subprocess Popen": pickle.dumps(Gadget(subprocess.Popen, (cmd,)), protocol=2),
        "copyreg _reconstructor": b"ccopyreg\n_reconstructor\n(c__builtin__\nobject\nc__builtin__"
        b"\nobject\nNtR.",
        "copy_reg _reconstructor (protocol 0)": pickle.dumps(Plain(), protocol=0),
        "renpy-prefixed lookalike": b"crenpyx.evil\nThing\n)R.",
        "persistent id": b"Pfoo\n.",
        "nested in renpy tree": b"\x80\x02crenpy.ast\nDefine\n)\x81}(X\x04\x00\x00\x00code"
        b"cos\nsystem\nX\x02\x00\x00\x00hiq\x85Rub.",
    }


@pytest.mark.parametrize("label", list(malicious_pickles(Path("m")).keys()))
def test_malicious_pickles_are_refused(tmp_path: Path, label: str) -> None:
    marker = tmp_path / "pwned.txt"
    payload = malicious_pickles(marker)[label]
    with pytest.raises(pickle.UnpicklingError):
        restricted_loads(payload)
    with pytest.raises(UnsupportedRpyc):
        read_rpyc(rpyc_container(zlib.compress(payload)))
    assert not marker.exists()


def test_whitelisted_containers_and_stubs_only() -> None:
    raw = pickle.dumps({"a": [1, 2], "b": (b"x", frozenset({"y"}))}, protocol=2)
    assert restricted_loads(raw) == {"a": [1, 2], "b": (b"x", frozenset({"y"}))}
    stub = restricted_loads(b"crenpy.ast\nAnything\n(S'arg'\ntR.")
    assert isinstance(stub, StubBase)
    assert stub._args == ("arg",)


# --------------------------------------------------------------- a renamed header (D50, 0.8.0)


@pytest.mark.parametrize("header", [b"chengf 2", b"PACKED RPC"])
def test_a_renamed_header_is_read_as_rpyc2(header: bytes) -> None:
    """D50: a packing tool replaced ``RENPY RPC2`` (8 bytes ``chengf 2`` in Sicae, or another
    10-byte word); the slot table and the zlib stream are standard: read, with a note."""
    stmts = options_stmts(save_directory="Renamed-1600407200")
    data = rpyc_bytes(stmts, "game/options.rpy", header=header)
    assert data.startswith(header) and not data.startswith(rpyc_reader.RPYC2_MAGIC)
    notes: list[str] = []
    found = extract_rpyc(data, "game/options.rpyc", notes=notes)
    assert values(found)["config.save_directory"] == "Renamed-1600407200"
    assert notes == [f"non-standard rpyc header {header.decode()!r} (read as RPYC2)"]
    standard = rpyc_bytes(stmts, "game/options.rpy")
    assert rpyc_reader.rpyc_payload(data) == rpyc_reader.rpyc_payload(standard)


def test_a_renamed_header_needs_a_sound_table_and_zlib_stream() -> None:
    """Anything else stays unreadable: slot 1 that is not a zlib stream, a slot past the end,
    a table that never ends, or no table at all; no note is made then."""
    good = rpyc_bytes(options_stmts(), "game/options.rpy", header=b"chengf 2")
    start = int.from_bytes(good[12:16], "little")
    not_zlib = good[:start] + b"\x00\x00" + good[start + 2 :]
    past_end = good[:12] + len(good).to_bytes(4, "little") + good[16:]
    endless = b"chengf 2" + (1).to_bytes(4, "little") * 3 * 80
    for data in (not_zlib, past_end, endless, b"chengf 2 and then nothing"):
        notes: list[str] = []
        with pytest.raises(UnsupportedRpyc):
            extract_rpyc(data, "game/options.rpyc", notes=notes)
        assert notes == []
    with pytest.raises(UnsupportedRpyc, match="incorrect header check"):
        read_rpyc(not_zlib)
