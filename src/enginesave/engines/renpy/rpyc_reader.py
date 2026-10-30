"""Read compiled Ren'Py scripts (``.rpyc``) with a restricted unpickler.

Only whitelisted container types are ever instantiated for real. Every ``renpy.*`` global is
replaced by an inert :class:`StubBase` subclass that merely records constructor arguments and
state, so no ``REDUCE``/``NEWOBJ``/``BUILD`` can reach game or engine code. Any other global
is refused with :class:`pickle.UnpicklingError`.
"""

from __future__ import annotations

import builtins
import codecs
import io
import pickle
import struct
import zlib
from collections import OrderedDict, defaultdict
from typing import Any

from enginesave.engines.renpy.literals import DynamicValue, evaluate
from enginesave.engines.renpy.rpy_parser import TARGET_VARS, Assignment, clip, scan_python_source
from enginesave.models import Evidence

RPYC2_MAGIC = b"RENPY RPC2"
MAX_DECOMPRESSED = 256 * 1024 * 1024
MAX_SLOTS = 64
MAX_STUB_CLASSES = 10_000
# 1.0.0 (D72 R09): what bytes(N) and bytearray(N) in one pickle may allocate in all; a short
# pickle could otherwise ask for gigabytes (no script needs more than its own size)
MAX_ALLOCATED = 64 * 1024 * 1024
ALLOCATING = ("bytes", "bytearray")
# A compiled script can only contribute target values if one of these byte strings occurs in
# its pickle (strings are stored literally at least once even when memoized).
PREFILTER_TOKENS = (
    b"store.config",
    b"store.build",
    b"savedir",
    b"save_directory",
    b"extra_savedirs",
)

_BUILTIN_NAMES = (
    "set",
    "frozenset",
    "list",
    "dict",
    "tuple",
    "bytes",
    "bytearray",
    "str",
    "int",
    "float",
    "bool",
    "complex",
    "object",
)
_ALLOWED: dict[tuple[str, str], Any] = {
    **{("builtins", name): getattr(builtins, name) for name in _BUILTIN_NAMES},
    **{("__builtin__", name): getattr(builtins, name) for name in _BUILTIN_NAMES},
    ("__builtin__", "unicode"): str,
    ("__builtin__", "long"): int,
    ("collections", "defaultdict"): defaultdict,
    ("collections", "OrderedDict"): OrderedDict,
    ("_codecs", "encode"): codecs.encode,
}


class UnsupportedRpyc(Exception):
    """Not a readable compiled script (bad header, slot table, compression or pickle)."""


class StubBase:
    """Inert placeholder for any ``renpy.*`` class. Records args, state and list/dict payloads."""

    __slots__ = ("_args", "_kwargs", "_state", "_tuple_state", "_items", "_mapping")

    def __new__(cls, *args: Any, **kwargs: Any) -> StubBase:
        self = object.__new__(cls)
        self._args = args
        self._kwargs = kwargs
        self._state: dict[Any, Any] = {}
        self._tuple_state: Any = None
        self._items: list[Any] = []
        self._mapping: dict[Any, Any] = {}
        return self

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __setstate__(self, state: Any) -> None:
        if isinstance(state, dict):
            self._state.update(state)
        elif (
            isinstance(state, tuple)
            and len(state) == 2
            and all(part is None or isinstance(part, dict) for part in state)
        ):
            for part in state:
                if part:
                    self._state.update(part)
        else:
            self._tuple_state = state

    def append(self, item: Any) -> None:
        self._items.append(item)

    def extend(self, items: Any) -> None:
        self._items.extend(items)

    def add(self, item: Any) -> None:
        self._items.append(item)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._mapping[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def __repr__(self) -> str:
        cls = type(self)
        return f"<stub {cls.__module__}.{cls.__qualname__}>"


class _RestrictedUnpickler(pickle.Unpickler):
    def __init__(self, file: io.BytesIO, budget: int | None = None) -> None:
        super().__init__(file, encoding="utf-8", errors="replace")
        self._stubs: dict[tuple[str, str], type] = {}
        self._left = MAX_ALLOCATED if budget is None else budget

    def _budgeted(self, kind: type) -> Any:
        """``kind`` (bytes or bytearray) that asks before it allocates (D72 R09): a size
        given as a number counts before the object is made, anything else (bytes from a
        string, a list of numbers) by what it made -- both against the pickle's budget."""

        def make(*args: Any) -> Any:
            first = args[0] if args else None
            if isinstance(first, int) and not isinstance(first, bool):
                self._spend(first)
                return kind(*args)
            made = kind(*args)
            self._spend(len(made))
            return made

        return make

    def _spend(self, size: int) -> None:
        if size > self._left:
            raise pickle.UnpicklingError(f"refused an allocation of {size} bytes")
        self._left -= max(size, 0)

    def find_class(self, module: str, name: str) -> Any:
        allowed = _ALLOWED.get((module, name))
        if allowed is not None:
            if name in ALLOCATING or allowed in (bytes, bytearray):
                return self._budgeted(allowed)
            return allowed
        if module == "renpy" or module.startswith("renpy."):
            stub = self._stubs.get((module, name))
            if stub is None:
                if len(self._stubs) >= MAX_STUB_CLASSES:
                    raise pickle.UnpicklingError("too many distinct classes")
                stub = type(name, (StubBase,), {"__module__": module, "__qualname__": name})
                self._stubs[(module, name)] = stub
            return stub
        raise pickle.UnpicklingError(f"refused global {module}.{name}")

    def persistent_load(self, pid: Any) -> Any:
        raise pickle.UnpicklingError("persistent IDs are not supported")


def restricted_loads(raw: bytes, budget: int | None = None) -> Any:
    """Unpickle ``raw`` allowing only containers, primitives and ``renpy.*`` stubs; ``bytes``
    and ``bytearray`` allocate at most ``budget`` bytes in all (MAX_ALLOCATED)."""
    return _RestrictedUnpickler(io.BytesIO(raw), budget).load()


def decompress_limited(blob: bytes, limit: int | None = None) -> bytes:
    """zlib-decompress with an output cap; raises ``ValueError`` on bad or oversized data."""
    cap = MAX_DECOMPRESSED if limit is None else limit
    decomp = zlib.decompressobj()
    try:
        out = decomp.decompress(blob, cap)
    except zlib.error as exc:
        raise ValueError(f"zlib error: {exc}") from exc
    if decomp.unconsumed_tail:
        raise ValueError(f"decompressed data exceeds {cap} bytes")
    return out


def _slot_one(data: bytes, pos: int, *, strict: bool = False) -> tuple[int, int]:
    """``(start, length)`` of slot 1 in the slot table that begins at ``pos``; ``strict`` also
    wants slot 1 after the table (for a renamed header, D50)."""
    slots: dict[int, tuple[int, int]] = {}
    while True:
        if pos + 12 > len(data):
            raise UnsupportedRpyc("truncated slot table")
        slot, start, length = struct.unpack_from("<III", data, pos)
        pos += 12
        if slot == 0:
            break
        slots[slot] = (start, length)
        if len(slots) > MAX_SLOTS:
            raise UnsupportedRpyc("slot table too long")
    if 1 not in slots:
        raise UnsupportedRpyc("slot 1 missing")
    start, length = slots[1]
    if start + length > len(data) or (strict and start < pos):
        raise UnsupportedRpyc("slot 1 out of range")
    return start, length


def _zlib_header(blob: bytes) -> bool:
    """RFC 1950: compression method 8 and a CMF/FLG pair that is a multiple of 31."""
    return len(blob) >= 2 and blob[0] & 0x0F == 8 and (blob[0] * 256 + blob[1]) % 31 == 0


def _renamed_header(data: bytes) -> tuple[bytes, str] | None:
    """D50: an RPYC2 file whose magic a packing tool replaced (``chengf 2`` instead of
    ``RENPY RPC2``). A slot table is tried right after 10 and after 8 bytes; it is accepted
    only when it is well formed, slot 1 starts with a valid zlib header and decompresses
    within the size limit into a pickle. Returns ``(payload, header)``."""
    for offset in (10, 8):
        try:
            start, length = _slot_one(data, offset, strict=True)
            blob = data[start : start + length]
            if not _zlib_header(blob):
                continue
            raw = decompress_limited(blob)
        except (UnsupportedRpyc, ValueError):
            continue
        if raw[:1] == b"\x80":  # pickle protocol 2 and later begin with PROTO
            return raw, data[:offset].decode("latin-1")
    return None


def rpyc_payload(data: bytes, notes: list[str] | None = None) -> bytes:
    """Decompressed pickle of slot 1 (or of the whole file for the legacy, header-less format).

    A renamed RPYC2 header (D50) is read as RPYC2 and noted in ``notes``. An empty result, or
    one that is not a pickle (it begins with PROTO), is not a compiled script (0.10.0, D60):
    a 0-byte file left by a broken download or a quarantined stub is unreadable, so the
    ``.rpy`` beside it is read instead.
    """
    try:
        if not data.startswith(RPYC2_MAGIC):
            try:
                raw = decompress_limited(data)
            except ValueError:
                renamed = _renamed_header(data)
                if renamed is None:
                    raise
                if notes is not None:
                    notes.append(f"non-standard rpyc header {renamed[1]!r} (read as RPYC2)")
                return renamed[0]
        else:
            start, length = _slot_one(data, len(RPYC2_MAGIC))
            raw = decompress_limited(data[start : start + length])
    except ValueError as exc:
        raise UnsupportedRpyc(str(exc)) from exc
    if raw[:1] != b"\x80":  # pickle protocol 2 and later begin with PROTO
        raise UnsupportedRpyc("not a pickle" if raw else "empty")
    return raw


def read_rpyc(data: bytes) -> Any:
    """Return the restricted-unpickled ``(data_dict, statements)`` tree of a compiled script."""
    return _load_tree(rpyc_payload(data))


def _load_tree(raw: bytes) -> Any:
    try:
        return restricted_loads(raw)
    except Exception as exc:  # any malformed or refused pickle
        raise UnsupportedRpyc(f"{type(exc).__name__}: {exc}") from exc


# --------------------------------------------------------------------------------- AST walk


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return str(value)
    if isinstance(value, StubBase) and value._args and isinstance(value._args[0], str):
        return str(value._args[0])  # PyExpr(source, filename, line, ...)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return None


def code_source(code: Any) -> str | None:
    """Source text of a ``PyCode`` stub: tuple state ``(1, source, ...)`` or dict ``source``."""
    if isinstance(code, str):
        return str(code)
    if not isinstance(code, StubBase):
        return None
    candidate = None
    state = code._tuple_state
    if isinstance(state, tuple) and len(state) >= 2 and isinstance(state[0], int):
        candidate = state[1]
    if candidate is None:
        candidate = code._state.get("source")
    return _text(candidate)


def _children(obj: StubBase) -> list[Any]:
    children = list(obj._state.values())
    children.extend(obj._items)
    children.extend(obj._mapping.values())
    children.extend(obj._args)
    if isinstance(obj._tuple_state, tuple):
        children.extend(obj._tuple_state)
    return children


def _define(node: StubBase, keyword: str, file_label: str, method: str) -> list[Assignment]:
    store, varname = node.get("store"), node.get("varname")
    if not isinstance(store, str) or not isinstance(varname, str):
        return []
    if node.get("index") is not None:
        return []
    short_store = store.removeprefix("store.")
    var = f"{short_store}.{varname}"
    if var not in TARGET_VARS:
        return []
    op = node.get("operator")
    op = op if isinstance(op, str) and op else "="
    source = code_source(node.get("code"))
    if source is None:
        evidence = Evidence(file_label, "rpyc_define_without_source", f"{keyword} {var}")
        return [Assignment(var, op, DynamicValue(""), evidence)]
    evidence = Evidence(file_label, method, clip(f"{keyword} {var} {op} {source}"))
    return [Assignment(var, op, evaluate(source), evidence)]


def walk_assignments(
    tree: Any, file_label: str, define_method: str = "rpyc_define"
) -> list[Assignment]:
    """Find target ``Define``/``Default`` nodes and python save-location assignments."""
    found: list[Assignment] = []
    stack: list[Any] = [tree]
    seen: set[int] = set()
    while stack:
        obj = stack.pop()
        if obj is None or isinstance(obj, str | bytes | bytearray | int | float | complex):
            continue
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, StubBase):
            kind = type(obj).__name__
            if kind in ("Define", "Default"):
                found.extend(_define(obj, kind.lower(), file_label, define_method))
            elif kind in ("Python", "EarlyPython"):
                source = code_source(obj.get("code"))
                if source:
                    found.extend(scan_python_source(source, file_label))
            stack.extend(reversed(_children(obj)))
        elif isinstance(obj, dict):
            stack.extend(reversed(list(obj.values())))
        elif isinstance(obj, list | tuple | set | frozenset):
            stack.extend(reversed(list(obj)))
    return found


def extract_rpyc(
    data: bytes,
    file_label: str,
    define_method: str = "rpyc_define",
    notes: list[str] | None = None,
) -> list[Assignment]:
    """Target assignments from one ``.rpyc``; raises :class:`UnsupportedRpyc` when unreadable.
    A renamed header (D50) adds ``non-standard rpyc header ...`` to ``notes``."""
    raw = rpyc_payload(data, notes)
    if not any(token in raw for token in PREFILTER_TOKENS):
        return []
    return walk_assignments(_load_tree(raw), file_label, define_method)
