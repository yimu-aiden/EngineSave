"""Ren'Py detector: structure → metadata → save paths → sources and statuses (plan 6.1-6.4)."""

from __future__ import annotations

import contextlib
import json
import os
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from enginesave.engines.base import DetectionResult, Detector, ScanContext, game_status
from enginesave.engines.renpy import paths, structure
from enginesave.engines.renpy.literals import DynamicValue, printable_int
from enginesave.engines.renpy.rpa_reader import (
    MAX_MEMBER_BYTES,
    RpaEntry,
    UnsupportedRpa,
    read_index,
    read_member,
)
from enginesave.engines.renpy.rpy_parser import SAVE_LOCATION_VARS, Assignment, parse_rpy
from enginesave.engines.renpy.rpyc_reader import UnsupportedRpyc, extract_rpyc
from enginesave.fsutil import (
    FileEntry,
    UnsafePathError,
    describe_error,
    dir_stats,
    is_reparse_point,
    iter_files,
    long_path,
    path_key,
    safe_relative_component,
    same_path,
    walk_files,
)
from enginesave.identity import normalize_name, normalized_dir_name
from enginesave.models import (
    DetectedGame,
    Evidence,
    GameIdentity,
    GameStatus,
    MetadataState,
    RootToken,
    SaveSource,
    SourceKind,
    SourceStatus,
)

if TYPE_CHECKING:
    from enginesave.scan_cache import ScanCache

ENGINE = "renpy"
SKIP_TOP = frozenset({"tl", "saves", "cache"})
ROOT_LEVEL_SKIP = frozenset({"renpy", "lib"})
TEXT_PREFILTER = ("config.", "build.")
NAME_MATCH_MIN_LENGTH = 3
SCRIPTLESS = "renpy structure without readable scripts"  # D53: CANDIDATE, not UNSUPPORTED
_KIND_ORDER = {SourceKind.LOCAL: 0, SourceKind.GLOBAL: 1, SourceKind.EXTRA: 2}


# ---------------------------------------------------------------------- metadata collection

OPTIONS = ("options.rpyc", "options.rpy")  # the .rpy only without a readable .rpyc (D52 1)
SAVE_DIRECTORY = "config.save_directory"
# The scan cache's parser version (D52 3): bump when parsing changes. 0.10.0 (D60): an empty
# .rpyc is unreadable now, so what 0.9.0 cached for one ("nothing in it") must go.
PARSER = "renpy-2"
INDEX = "#index"  # the cache's member name for an archive's list of script members


def _read_bytes(path: Path) -> bytes:
    with open(long_path(path), "rb") as f:
        return f.read()


def _parse_text(data: bytes, label: str, method: str) -> list[Assignment]:
    text = data.decode("utf-8-sig", "replace")
    if not any(token in text for token in TEXT_PREFILTER):
        return []
    return parse_rpy(text, label, method)


@dataclass
class Parsed:
    """What one script said: its assignments, renamed-header notes (D50), or why it could not
    be read (a compiled script the reader refuses; not an I/O error, which may pass)."""

    assignments: list[Assignment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def _parse_script(data: bytes, label: str, method: str, compiled: bool) -> Parsed:
    if not compiled:
        return Parsed(_parse_text(data, label, method))
    notes: list[str] = []
    try:
        return Parsed(extract_rpyc(data, label, method, notes), notes)
    except UnsupportedRpyc as exc:
        return Parsed(error=str(exc))


class Uncacheable(Exception):
    """A value the scan cache does not hold; the script is then parsed again next time."""


def _encode(value: object) -> object:
    if isinstance(value, int) and not printable_int(value):
        raise Uncacheable("int too long to write out")  # one entry never blocks the file
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, DynamicValue):
        return {"dynamic": value.raw}
    if isinstance(value, list | tuple):
        return {type(value).__name__: [_encode(v) for v in value]}
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {"dict": {k: _encode(v) for k, v in value.items()}}
    raise Uncacheable(type(value).__name__)


def _decode(value: object) -> object:
    if not isinstance(value, dict):
        return value
    if "dynamic" in value:
        return DynamicValue(str(value["dynamic"]))
    if "list" in value:
        return [_decode(v) for v in value["list"]]
    if "tuple" in value:
        return tuple(_decode(v) for v in value["tuple"])
    if "dict" in value:
        return {k: _decode(v) for k, v in value["dict"].items()}
    raise ValueError("unknown cached value")


def encode_parsed(parsed: Parsed, label: str) -> dict[str, object]:
    """A parsed script as plain JSON for the scan cache (D52 3). ``label`` is the name the
    evidence carries: the cache key ignores case, the label does not."""
    rows = []
    for a in parsed.assignments:
        evidence = a.evidence
        row = [a.var, a.op, _encode(a.value), evidence.file, evidence.method, evidence.detail]
        rows.append([*row, evidence.line])
    return {
        "label": label,
        "assignments": rows,
        "notes": list(parsed.notes),
        "error": parsed.error,
    }


def decode_parsed(value: object, label: str) -> Parsed:
    """The cached script, when it was cached under the same ``label`` (a file renamed only in
    case is parsed again, so the evidence shows its name as it is now)."""
    if not isinstance(value, dict):
        raise ValueError("not a cached script")
    if value.get("label") != label:
        raise ValueError("cached under another name")
    assignments = []
    for var, op, raw, file, method, detail, line in value["assignments"]:
        evidence = Evidence(str(file), str(method), str(detail), line)
        assignments.append(Assignment(str(var), str(op), _decode(raw), evidence))
    error = value.get("error")
    return Parsed(
        assignments, [str(n) for n in value["notes"]], None if error is None else str(error)
    )


@dataclass
class Collected:
    """What the scripts of one game said (D52): the assignments, what could not be read, the
    renamed rpyc headers (D50) and other notes; ``cache`` spares parsing a script twice."""

    cache: ScanCache | None = None
    assignments: list[Assignment] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)  # scripts/archives not inspected
    renamed: dict[str, list[str]] = field(default_factory=dict)  # D50: header note -> files
    notes: list[str] = field(default_factory=list)  # warnings of the collection itself
    read: set[str] = field(default_factory=set)  # labels already taken in (or tried)
    scripts: bool = False  # a script or an archive was found at all
    failed: set[str] = field(default_factory=set)  # labels that could not be parsed
    archives: dict[str, list[str] | None] = field(default_factory=dict)  # label -> members
    indexes: dict[str, dict[str, RpaEntry]] = field(default_factory=dict)  # read this scan
    stats: dict[str, os.stat_result] = field(default_factory=dict)  # archive label -> stat
    streams: dict[str, BinaryIO | None] | None = None  # archives kept open (keeping_open)
    stack: contextlib.ExitStack | None = None

    def take(self, parsed: Parsed, label: str, unreadable: str) -> None:
        self.read.add(label)
        self.assignments.extend(parsed.assignments)
        for note in parsed.notes:
            self.renamed.setdefault(note, []).append(label)
        if parsed.error is not None:
            self.failed.add(label)
            self.unreadable.append(unreadable.format(label=label, error=parsed.error))

    def cached(
        self, path: Path, size: int, mtime_ns: int, label: str, member: str = ""
    ) -> Parsed | None:
        if self.cache is None:
            return None
        value = self.cache.get(path, size, mtime_ns, PARSER, member)
        if value is None:
            return None
        try:
            return decode_parsed(value, label)
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            self.cache.drop(path, member)  # damaged, or another name: parse again
            return None

    def remember(
        self, path: Path, size: int, mtime_ns: int, parsed: Parsed, label: str, member: str = ""
    ) -> None:
        if self.cache is None:
            return
        try:
            self.cache.put(path, size, mtime_ns, PARSER, encode_parsed(parsed, label), member)
        except Uncacheable:
            self.cache.drop(path, member)

    def read_file(self, path: Path, label: str) -> Parsed | None:
        """A loose ``.rpy``/``.rpyc``, from the cache while it has not changed (D52 3)."""
        compiled = path.name.lower().endswith(".rpyc")
        method = "rpyc_define" if compiled else "rpy_define"
        try:
            info = os.stat(long_path(path))
            parsed = self.cached(path, info.st_size, info.st_mtime_ns, label)
            if parsed is None:
                parsed = _parse_script(_read_bytes(path), label, method, compiled)
                self.remember(path, info.st_size, info.st_mtime_ns, parsed, label)
        except OSError as exc:
            self.read.add(label)
            self.failed.add(label)
            self.unreadable.append(f"{label}: {describe_error(exc)}")
            return None
        self.take(parsed, label, "{label}: compiled script not readable ({error})")
        return parsed

    def members(self, archive: Path, label: str) -> list[str] | None:
        """The script members of an archive (not ``tl/``), cached; ``None`` when unreadable.
        Each archive is looked at once per scan, whichever step asks first."""
        if label in self.archives:
            return self.archives[label]
        names: list[str] | None = None
        try:
            info = self.stats[label] = os.stat(long_path(archive))
            value = (
                self.cache.get(archive, info.st_size, info.st_mtime_ns, PARSER, INDEX)
                if self.cache is not None
                else None
            )
            if isinstance(value, list) and all(isinstance(n, str) for n in value):
                names = value
            else:
                index = read_index(archive)
                self.indexes[label] = {m.name: m for m in index}
                names = [
                    m.name
                    for m in index
                    if m.name.lower().endswith((".rpy", ".rpyc"))
                    and m.name.lower().split("/", 1)[0] != "tl"
                ]
                if self.cache is not None:
                    self.cache.put(archive, info.st_size, info.st_mtime_ns, PARSER, names, INDEX)
        except (UnsupportedRpa, OSError) as exc:
            self.read.add(label)
            self.unreadable.append(f"{label}: archive index not readable ({exc})")
        if names is not None:  # translations (tl/, at any depth) are never read (D60)
            names = [n for n in names if "tl" not in n.lower().split("/")[:-1]]
        self.archives[label] = names
        return names

    def read_members(self, archive: Path, label: str, names: Sequence[str]) -> None:
        """Script members of one archive, a ``.rpyc`` instead of its ``.rpy`` as for loose
        scripts (D52 2; the ``.rpy`` only when the ``.rpyc`` cannot be read). Each comes from
        the cache while the archive is unchanged; the archive is opened (once, D38) only for
        the members the cache does not hold. Members are taken in the order given, cached or
        not, so a cached scan reports exactly what an uncached one does."""
        compiled = {n.lower() for n in names if n.lower().endswith(".rpyc")}
        first = [n for n in names if n.lower().endswith(".rpyc") or f"{n.lower()}c" not in compiled]
        self._read_members(archive, label, first)
        failed = {n.lower() for n in first if f"{label}!{n}" in self.failed}
        self._read_members(
            archive, label, [n for n in names if n not in first and f"{n.lower()}c" in failed]
        )

    def _read_members(self, archive: Path, label: str, names: Sequence[str]) -> None:
        names = [n for n in names if f"{label}!{n}" not in self.read]
        if not names:
            return
        info = self.stats.get(label)
        if info is None:  # one stat of an archive per scan, however many scripts it gives
            try:
                info = self.stats[label] = os.stat(long_path(archive))
            except OSError as exc:
                self.unreadable.append(f"{label}: {describe_error(exc)}")
                return
        parsed: dict[str, Parsed | None] = {
            name: self.cached(archive, info.st_size, info.st_mtime_ns, f"{label}!{name}", name)
            for name in names
        }
        problems: dict[str, str] = {}
        todo = [name for name in names if parsed[name] is None]
        if todo:
            self._parse_members(archive, label, todo, info, parsed, problems)
        for name in names:
            member_label = f"{label}!{name}"
            if name in problems:
                self.read.add(member_label)
                self.failed.add(member_label)
                self.unreadable.append(problems[name])
            elif (found := parsed.get(name)) is not None:
                self.take(found, member_label, "{label}: not readable ({error})")

    def _parse_members(
        self,
        archive: Path,
        label: str,
        names: Sequence[str],
        info: os.stat_result,
        parsed: dict[str, Parsed | None],
        problems: dict[str, str],
    ) -> None:
        """Read and parse ``names`` from the archive (one open, D38) into ``parsed``, or the
        reason one could not be read into ``problems``; parsed members go into the cache."""
        index = self.indexes.get(label)
        if index is None:
            try:
                index = {m.name: m for m in read_index(archive)}
            except (UnsupportedRpa, OSError) as exc:
                self.unreadable.append(f"{label}: archive index not readable ({exc})")
                return
            self.indexes[label] = index
        with contextlib.ExitStack() as stack:
            stream = self._stream(archive, label, stack)
            for name in names:
                member_label = f"{label}!{name}"
                member = index.get(name)
                if member is None:
                    continue
                if member.length > MAX_MEMBER_BYTES:
                    problems[name] = (
                        f"{member_label}: skipped, larger than {MAX_MEMBER_BYTES} bytes"
                    )
                    continue
                compiled = name.lower().endswith(".rpyc")
                method = "rpa_rpyc_define" if compiled else "rpa_rpy_define"
                try:
                    data = read_member(archive, member, stream=stream)
                    result = _parse_script(data, member_label, method, compiled)
                except (UnsupportedRpa, OSError) as exc:
                    problems[name] = f"{member_label}: not readable ({exc})"
                    continue
                self.remember(archive, info.st_size, info.st_mtime_ns, result, member_label, name)
                parsed[name] = result

    def _stream(self, archive: Path, label: str, stack: contextlib.ExitStack) -> BinaryIO | None:
        """One open for every member (D38); while :meth:`keeping_open`, one for all the scripts
        the full parse reads from the archive one by one. Without it each member opens and
        reports alone."""
        if self.streams is not None and label in self.streams:
            return self.streams[label]
        try:
            stream = (self.stack or stack).enter_context(open(long_path(archive), "rb"))
        except OSError:
            stream = None
        if self.streams is not None:
            self.streams[label] = stream
        return stream

    @contextlib.contextmanager
    def keeping_open(self) -> Iterator[None]:
        """The archives opened in the block stay open until its end."""
        with contextlib.ExitStack() as stack:
            self.streams, self.stack = {}, stack
            try:
                yield
            finally:
                self.streams, self.stack = None, None

    def warnings(self) -> list[str]:
        """What could not be read, one line per renamed rpyc header (D50), other notes."""
        renamed = [
            f"{note}: {len(files)} compiled script(s), e.g. {files[0]}"
            for note, files in self.renamed.items()
        ]
        return [*self.unreadable, *renamed, *self.notes]


def _sets_save_directory(assignments: Iterable[Assignment]) -> bool:
    return any(a.var == SAVE_DIRECTORY for a in assignments)


def _top_files(gamedir: Path) -> dict[str, Path]:
    """The regular files at the top of ``game/`` by lower-case name, with their real names;
    never through a symlink or a junction, as everywhere else (D20)."""
    found: dict[str, Path] = {}
    try:
        with os.scandir(long_path(gamedir)) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return found
    for entry in entries:
        try:
            if not is_reparse_point(entry) and entry.is_file(follow_symlinks=False):
                found.setdefault(entry.name.lower(), gamedir / entry.name)
        except OSError:
            continue
    return found


def _options_in_archives(root: Path, archives: Sequence[Path], collected: Collected) -> bool:
    """The options Ren'Py itself would load: it searches the archives by name in reverse
    order (``patch.rpa`` before ``archive.rpa``) for ``options.rpyc`` and never loads a
    ``.rpy`` from an archive, so the first archive with ``options.rpyc`` at its top level is
    the one (its own ``options.rpy`` only if that cannot be read). Without any, the first
    ``options.rpy`` is read, as the full parse would. True when they set
    ``config.save_directory``."""
    plain_only: tuple[Path, str, list[str]] | None = None
    for archive in sorted(archives, key=lambda path: path.name, reverse=True):
        collected.scripts = True
        label = structure.relative_label(archive, root)
        names = collected.members(archive, label) or []
        found = [n for option in OPTIONS for n in names if n.lower() == option]
        if any(n.lower() == OPTIONS[0] for n in found):
            collected.read_members(archive, label, found)
            return _sets_save_directory(collected.assignments)
        if found and plain_only is None:
            plain_only = (archive, label, found)
    if plain_only is not None:
        collected.read_members(*plain_only)
        return _sets_save_directory(collected.assignments)
    return False


def _options_below(entries: Iterable[FileEntry]) -> tuple[Path | None, Path | None] | None:
    """D60 1: ``options.rpyc``/``options.rpy`` in a folder below ``game/`` (``tl/`` and
    whatever is below it left out), found by name in the listing alone; of several folders
    the least deep one, then the shortest path. ``(compiled, plain)`` or ``None``."""
    folders: dict[str, dict[str, FileEntry]] = {}
    for entry in entries:
        parts = entry.rel.split("/")
        name = parts[-1].lower()
        if len(parts) < 2 or name not in OPTIONS or any(p.lower() == "tl" for p in parts[:-1]):
            continue
        folders.setdefault("/".join(parts[:-1]).lower(), {})[name] = entry
    if not folders:
        return None
    best = folders[min(folders, key=lambda folder: (folder.count("/"), len(folder), folder))]
    compiled, plain = (best.get(name) for name in OPTIONS)
    return (compiled.path if compiled else None), (plain.path if plain else None)


def _options_first(
    root: Path, gamedir: Path, collected: Collected, listing: Callable[[], list[FileEntry]]
) -> bool:
    """D52 1: ``game/options.rpyc`` (``options.rpy`` only when there is no ``.rpyc`` or it
    cannot be read); without them the options in a folder below ``game/`` (D60 1, found in
    the ``listing``), else the options in the archives. True when they set
    ``config.save_directory``: nothing else needs reading then."""
    top = _top_files(gamedir)
    compiled, plain = (top.get(name) for name in OPTIONS)
    if compiled is None and plain is None:
        below = _options_below(listing())
        if below is None:
            archives = [path for name, path in top.items() if name.endswith(".rpa")]
            return _options_in_archives(root, archives, collected)
        compiled, plain = below
    collected.scripts = True
    parsed = None
    if compiled is not None:
        parsed = collected.read_file(compiled, structure.relative_label(compiled, root))
    if plain is not None and (parsed is None or parsed.error is not None):
        collected.read_file(plain, structure.relative_label(plain, root))
    return _sets_save_directory(collected.assignments)


def collect_assignments(
    root: Path, gamedir: Path, skip: frozenset[str], cache: ScanCache | None = None
) -> Collected:
    """D52: options first, and stop when they name the save directory; otherwise the other
    scripts (a ``.rpyc`` instead of its ``.rpy``, which is read only when the ``.rpyc``
    cannot be), the likely ones first, until one gives ``config.save_directory`` as a
    literal (D60 2), and the archives' scripts the same way only when the loose scripts did
    not -- in Ren'Py's order, the first copy of a name only; ``tl/`` is skipped. A ``game/``
    without any script is looked at for a renamed archive (D53 1), whose options come first
    as well."""
    collected = Collected(cache)
    listed: list[FileEntry] | None = None

    def listing() -> list[FileEntry]:  # game/ is listed once, and only when needed
        nonlocal listed
        if listed is None:
            listed = list(iter_files(gamedir, skip_top=skip))  # no symlink or junction (D20)
        return listed

    if _options_first(root, gamedir, collected, listing):
        return collected
    entries = listing()
    by_name = {e.rel.lower(): e for e in entries}
    scripts = [  # translations (tl/, at any depth) never name the save directory
        e
        for e in entries
        if e.rel.lower().endswith((".rpy", ".rpyc")) and "tl" not in e.rel.lower().split("/")[:-1]
    ]
    for entry in sorted(scripts, key=lambda e: _priority(e.rel)):
        lower = entry.rel.lower()
        collected.scripts = True
        label = structure.relative_label(entry.path, root)
        if label in collected.read or (lower.endswith(".rpy") and f"{lower}c" in by_name):
            continue  # read already, or its .rpyc is read instead (unless unreadable, below)
        parsed = collected.read_file(entry.path, label)
        plain = by_name.get(lower[:-1]) if lower.endswith(".rpyc") else None
        if plain is not None and (parsed is None or parsed.error is not None):
            plain_label = structure.relative_label(plain.path, root)
            if plain_label not in collected.read:
                collected.read_file(plain.path, plain_label)
        if _names_save_directory(collected.assignments):
            return collected  # D60 2: the first literal settles it
    archives = [e.path for e in entries if e.rel.lower().endswith(".rpa")]
    if not archives and not collected.scripts:
        archives = structure.renamed_archives(gamedir)
        for archive in archives:
            label = structure.relative_label(archive, root)
            collected.notes.append(f"{label}: an RPA archive under another name, read as one")
        if _options_in_archives(root, archives, collected):
            return collected
    if archives:
        collected.scripts = True
    if archives and not _sets_save_directory(collected.assignments):
        # Ren'Py's order: the archives by name in reverse (patch.rpa before archive.rpa), and
        # of a name it loads the first copy only -- the loose file, else the first archive's
        taken = {e.rel.lower() for e in entries}
        with collected.keeping_open():
            for archive in sorted(archives, key=lambda path: path.name, reverse=True):
                label = structure.relative_label(archive, root)
                members = collected.members(archive, label) or []
                names = [n for n in members if n.lower() not in taken]
                taken.update(n.lower() for n in names)
                for group in _member_groups(names):
                    collected.read_members(archive, label, group)
                    if _names_save_directory(collected.assignments):
                        return collected
    return collected


PRIORITY_WORDS = ("option", "config", "setting", "define", "init", "script")


def _priority(rel: str) -> tuple[int, str]:
    """D60 2: the order of the full parse -- a file named like options, config, settings,
    defines, init or script first, then ``00*``, then ``01*``, then the rest, each by path."""
    name = rel.rsplit("/", 1)[-1].lower()
    if any(word in name for word in PRIORITY_WORDS):
        rank = 0
    elif name.startswith("00"):
        rank = 1
    elif name.startswith("01"):
        rank = 2
    else:
        rank = 3
    return rank, rel.lower()


def _member_groups(names: Sequence[str]) -> list[list[str]]:
    """An archive's script members by script, in :func:`_priority` order: each script's
    ``.rpyc`` with its ``.rpy`` (read only when the ``.rpyc`` cannot be), so that reading
    can stop after any script."""
    groups: dict[str, list[str]] = {}
    for name in names:
        lower = name.lower()
        stem = lower[:-1] if lower.endswith(".rpyc") else lower
        groups.setdefault(stem, []).append(name)
    return [
        sorted(group, key=lambda n: not n.lower().endswith(".rpyc"))
        for _stem, group in sorted(groups.items(), key=lambda item: _priority(item[0]))
    ]


def _names_save_directory(assignments: Iterable[Assignment]) -> bool:
    """A plain ``config.save_directory = <literal>`` (``None`` too) is among them: the full
    parse can stop (D60 2); one set by code is not, the other scripts may still name it."""
    return any(
        a.var == SAVE_DIRECTORY and a.op == "=" and not isinstance(a.value, DynamicValue)
        for a in assignments
    )


def build_info_assignments(gamedir: Path, root: Path) -> list[Assignment]:
    """Name/version from ``cache/build_info.json`` (never a save directory)."""
    path = gamedir / "cache" / "build_info.json"
    text = structure.read_small_text(path)
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    label = structure.relative_label(path, root)
    found = []
    for key, var in (("name", "config.name"), ("version", "config.version")):
        value = data.get(key)
        if isinstance(value, str) and value:
            evidence = Evidence(label, "build_info_json", f"{key} = {json.dumps(value)}")
            found.append(Assignment(var, "=", value, evidence))
    return found


# --------------------------------------------------------------------------- metadata merge


COMPILED_METHODS = ("rpyc_define", "rpa_rpyc_define")
NAME_VARS = ("config.name", "build.name")
LOCAL_ONLY_NOTE = "config.save_directory not defined; engine default keeps saves in game/saves"


@dataclass
class Metadata:
    state: MetadataState
    # var -> [(literal value, evidence method)] for plain ``=`` assignments, in collection order
    values: dict[str, list[tuple[object, str]]] = field(default_factory=dict)
    save_directories: list[str] = field(default_factory=list)  # distinct safe literal values
    explicit_no_global: bool = False  # ``config.save_directory = None`` (or "")
    implicit_local_only: bool = False  # options readable but no save_directory at all (D19)
    extra_savedirs: list[str] = field(default_factory=list)
    evidence_by_var: dict[str, list[Evidence]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def distinct(self, var: str) -> list[object]:
        seen: list[object] = []
        for value, _ in self.values.get(var, []):
            if value not in seen:
                seen.append(value)
        return seen

    def text(self, var: str) -> str | None:
        """Identity value: the compiled (``.rpyc``) one first, else the first seen (D19)."""
        ordered = sorted(self.values.get(var, []), key=lambda vm: vm[1] not in COMPILED_METHODS)
        for value, _ in ordered:
            if isinstance(value, str) and value:
                return value
            if type(value) is int:
                return str(value)
        return None

    def has_script_name(self) -> bool:
        return any(
            isinstance(value, str) and value and method != "build_info_json"
            for var in NAME_VARS
            for value, method in self.values.get(var, [])
        )


def merge_metadata(assignments: list[Assignment], *, unreadable: int = 0) -> Metadata:
    """Plan 6.2 merge rules with the 0.2.0 boundaries (D19).

    Only the save-location variables can make the metadata ``conflict``; differing names or
    versions are warnings. Readable options without any ``save_directory`` mean "local only",
    unless some script could not be inspected.
    """
    md = Metadata(state=MetadataState.MISSING)
    dynamic: dict[str, list[Evidence]] = {}
    extra_lists: list[list[str]] = []  # distinct plain ``=`` assignments of extra_savedirs
    for a in assignments:
        md.evidence_by_var.setdefault(a.var, []).append(a.evidence)
        if a.var == "config.extra_savedirs":
            items = [a.value] if isinstance(a.value, str) else a.value
            if isinstance(items, list) and all(isinstance(i, str) for i in items):
                if a.op == "=" and items not in extra_lists:
                    extra_lists.append(list(items))
                for item in items:
                    if item not in md.extra_savedirs:
                        md.extra_savedirs.append(item)
            else:
                dynamic.setdefault(a.var, []).append(a.evidence)
            continue
        if a.var == "config.savedir" or isinstance(a.value, DynamicValue) or a.op != "=":
            dynamic.setdefault(a.var, []).append(a.evidence)
            continue
        value = None if a.var == "config.save_directory" and a.value == "" else a.value
        md.values.setdefault(a.var, []).append((value, a.evidence.method))

    for value in md.distinct("config.save_directory"):
        if value is None:
            md.explicit_no_global = True
            continue
        try:
            if not isinstance(value, str):
                raise UnsafePathError(f"not a string: {value!r}")
            md.save_directories.append(safe_relative_component(value))
        except UnsafePathError as exc:
            md.warnings.append(f"config.save_directory ignored: {exc}")
            dynamic.setdefault("config.save_directory", [])

    conflicts: dict[str, list[object]] = {}
    if len(md.distinct("config.save_directory")) > 1:
        conflicts["config.save_directory"] = md.distinct("config.save_directory")
    if len(extra_lists) > 1:
        conflicts["config.extra_savedirs"] = list(extra_lists)
    for var, values in conflicts.items():
        md.warnings.append(f"conflicting values for {var}: {', '.join(map(repr, values))}")
    for var in md.values:
        values = md.distinct(var)
        if var not in SAVE_LOCATION_VARS and len(values) > 1:
            md.warnings.append(
                f"conflicting values for {var}: {', '.join(map(repr, values))}; "
                f"using {md.text(var)!r}"
            )
    for var, evidences in dynamic.items():
        files = ", ".join(sorted({e.file for e in evidences})) or "invalid literal"
        if var in SAVE_LOCATION_VARS:
            md.warnings.append(
                f"runtime code may change save location: {var} is set by code that cannot "
                f"be evaluated statically ({files})"
            )
        else:
            md.warnings.append(f"{var} is not a plain literal ({files}); ignored")

    if conflicts:
        md.state = MetadataState.CONFLICT
    elif dynamic.keys() & SAVE_LOCATION_VARS:
        md.state = MetadataState.DYNAMIC
    elif "config.save_directory" in md.values:
        md.state = MetadataState.RESOLVED
    elif md.has_script_name() and not unreadable:
        md.state = MetadataState.RESOLVED
        md.implicit_local_only = True
    return md


# ---------------------------------------------------------------------------------- sources


def _source(
    source_id: str,
    kind: SourceKind,
    path: Path,
    token: RootToken,
    relative_root: str,
    method: str,
    evidence: list[Evidence],
    status_if_files: SourceStatus,
) -> SaveSource:
    exists, count, size = dir_stats(path)
    status = status_if_files if count else SourceStatus.EXPECTED_ABSENT
    source = SaveSource(
        source_id, kind, str(path), token, relative_root, status, method, list(evidence)
    )
    source.exists, source.file_count, source.total_bytes = exists, count, size
    if kind is SourceKind.GLOBAL and (path / "sync").is_dir():
        source.notes.append("includes sync/ (engine mirror)")
    return source


def _saves_root_dirs(saves_root: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(saves_root) as it:
            entries = sorted(it, key=lambda e: e.name.casefold())
    except OSError:
        return []
    return [
        e
        for e in entries
        if e.is_dir(follow_symlinks=False)
        and not is_reparse_point(e)
        and not paths.is_non_game_dir(e.name)
    ]


def build_sources(
    root: Path, gamedir: Path, identity: GameIdentity, md: Metadata, ctx: ScanContext
) -> list[SaveSource]:
    rel_gamedir = structure.relative_label(gamedir, root)
    local_rel = "saves" if rel_gamedir == "." else f"{rel_gamedir}/saves"
    local_evidence = Evidence(
        rel_gamedir, "path_to_gamedir", f"gamedir for exe_basename {identity.exe_basename!r}"
    )
    local = _source(
        "local",
        SourceKind.LOCAL,
        gamedir / "saves",
        RootToken.GAME_ROOT,
        local_rel,
        "renpy:gamedir/saves",
        [local_evidence],
        SourceStatus.CONFIRMED,
    )
    if md.explicit_no_global:
        local.notes.append("config.save_directory is None: the engine keeps saves only here")
    if md.implicit_local_only:
        local.notes.append(LOCAL_ONLY_NOTE)
    sources = [local]
    taken = {path_key(local.path)}

    def next_global_id() -> str:
        count = sum(1 for s in sources if s.kind is SourceKind.GLOBAL)
        return "global" if count == 0 else f"global-{count + 1}"

    saves_root, root_method = paths.game_saves_root(root, ctx.env, ctx.saves_root_override)
    resolved = md.state is MetadataState.RESOLVED
    sd_evidence = md.evidence_by_var.get("config.save_directory", [])
    for value in md.save_directories:
        path = saves_root / value
        if path_key(path) in taken:
            continue
        status = SourceStatus.CONFIRMED if resolved else SourceStatus.CANDIDATE
        evidence = [e for e in sd_evidence if value in e.detail] or sd_evidence
        source = _source(
            next_global_id(),
            SourceKind.GLOBAL,
            path,
            RootToken.RENPY_SAVES_ROOT,
            value,
            root_method,
            evidence,
            status,
        )
        if md.state is MetadataState.CONFLICT and not source.file_count:
            continue
        sources.append(source)
        taken.add(path_key(path))

    if not resolved or md.implicit_local_only:
        keys = {
            label: normalize_name(value)
            for label, value in (
                ("config_name", identity.config_name),
                ("build_name", identity.build_name),
                ("exe_basename", identity.exe_basename),
                ("root_dir_name", identity.root_dir_name),
            )
            if len(normalize_name(value)) >= NAME_MATCH_MIN_LENGTH
        }
        for entry in _saves_root_dirs(saves_root):
            normalized = normalized_dir_name(entry.name)
            matched = [label for label, key in keys.items() if key == normalized]
            path = Path(entry.path)
            if not matched or path_key(path) in taken:
                continue
            detail = f"normalized {normalized!r} matches {', '.join(matched)}"
            source = _source(
                next_global_id(),
                SourceKind.GLOBAL,
                path,
                RootToken.RENPY_SAVES_ROOT,
                entry.name,
                "dir_name_match",
                [Evidence(entry.name, "dir_name_match", detail)],
                SourceStatus.CANDIDATE,
            )
            if source.file_count:
                sources.append(source)
                taken.add(path_key(path))

    extra_evidence = md.evidence_by_var.get("config.extra_savedirs", [])
    for value in md.extra_savedirs:
        path = Path(value) if Path(value).is_absolute() else root / value
        if path_key(path) in taken:
            continue
        files, _ = walk_files(path) if path.is_dir() else ([], [])
        has_saves = any(f.rel.lower().endswith(".save") for f in files)
        status = SourceStatus.CONFIRMED if has_saves else SourceStatus.CANDIDATE
        extra_id = f"extra-{sum(1 for s in sources if s.kind is SourceKind.EXTRA) + 1}"
        source = _source(
            extra_id,
            SourceKind.EXTRA,
            path,
            RootToken.ABSOLUTE,
            str(path),
            "config.extra_savedirs",
            extra_evidence,
            status,
        )
        sources.append(source)
        taken.add(path_key(path))
    return sources


# ------------------------------------------------------------------------------------ games


def analyze_game(root: Path, ctx: ScanContext) -> DetectedGame:
    exe = structure.exe_basename(root)
    gamedir = structure.path_to_gamedir(root, exe)
    version, version_evidence = structure.engine_version(root, gamedir)
    rel_gamedir = structure.relative_label(gamedir, root)
    evidence = [Evidence(rel_gamedir, "path_to_gamedir", f"gamedir for exe_basename {exe!r}")]
    if version_evidence:
        evidence.append(version_evidence)
    install = {"game_root": str(root), "config_version": None, "engine_version": version}
    skip = SKIP_TOP | (ROOT_LEVEL_SKIP if same_path(gamedir, root) else frozenset())
    collected = collect_assignments(root, gamedir, skip, ctx.cache)
    scriptless = not collected.scripts
    has_game = (gamedir / "saves").is_dir() or (not same_path(gamedir, root) and exe is not None)
    if scriptless and not has_game:
        return DetectedGame(
            game_root=str(root),
            engine=ENGINE,
            engine_version=version,
            display_name=root.name,
            identity=GameIdentity(None, None, None, None, exe, root.name),
            metadata_state=MetadataState.MISSING,
            status=GameStatus.UNSUPPORTED,
            evidence=evidence,
            warnings=["renpy structure without scripts"],
            installs=[install],
        )

    # D53: no script anywhere, yet a game\saves (or a game\ beside a launcher): its local
    # saves are taken as usual, a global folder only by name, as a candidate
    warnings = [SCRIPTLESS, *collected.warnings()] if scriptless else collected.warnings()
    assignments = collected.assignments or build_info_assignments(gamedir, root)
    md = merge_metadata(assignments, unreadable=len(collected.unreadable))
    warnings.extend(md.warnings)
    identity = GameIdentity(
        renpy_save_directory=md.save_directories[0] if len(md.save_directories) == 1 else None,
        config_name=md.text("config.name"),
        build_name=md.text("build.name"),
        config_version=md.text("config.version"),
        exe_basename=exe,
        root_dir_name=root.name,
    )
    install["config_version"] = identity.config_version
    evidence.extend(a.evidence for a in assignments)
    game = DetectedGame(
        game_root=str(root),
        engine=ENGINE,
        engine_version=version,
        display_name=identity.config_name or identity.build_name or root.name,
        identity=identity,
        metadata_state=md.state,
        status=GameStatus.NOT_FOUND,
        sources=build_sources(root, gamedir, identity, md, ctx),
        evidence=evidence,
        warnings=warnings,
        installs=[install],
    )
    game.status = game_status(game)
    return game


def _absorb_install(primary: DetectedGame, other: DetectedGame) -> None:
    """D9: fold another install of the same game into ``primary`` (global appears once)."""
    primary.installs.extend(other.installs)
    local_count = sum(1 for s in primary.sources if s.kind is SourceKind.LOCAL)
    extra_paths = {path_key(s.path) for s in primary.sources if s.kind is SourceKind.EXTRA}
    for source in other.sources:
        if source.kind is SourceKind.LOCAL:
            local_count += 1
            source.source_id = f"local-{local_count}"
            source.notes.append(f"install: {other.game_root}")
            primary.sources.append(source)
        elif source.kind is SourceKind.EXTRA and path_key(source.path) not in extra_paths:
            extra_paths.add(path_key(source.path))
            source.source_id = f"extra-{len(extra_paths)}"
            primary.sources.append(source)
    primary.evidence.extend(e for e in other.evidence if e not in primary.evidence)
    primary.warnings.extend(w for w in other.warnings if w not in primary.warnings)


def _order_sources(game: DetectedGame) -> None:
    game.sources.sort(key=lambda s: (_KIND_ORDER.get(s.kind, 9), len(s.source_id), s.source_id))


def _assign_mirrors(game: DetectedGame) -> None:
    confirmed = [
        s for s in game.sources if s.status is SourceStatus.CONFIRMED and s.kind in _KIND_ORDER
    ]
    kinds = {s.kind for s in confirmed}
    if not {SourceKind.LOCAL, SourceKind.GLOBAL} <= kinds:
        return
    for source in confirmed:
        source.mirror_of = [o.source_id for o in confirmed if o is not source]


class RenPyDetector(Detector):
    engine = ENGINE

    def detect(self, directory: Path, ctx: ScanContext) -> DetectionResult | None:
        if not structure.has_renpy_package(directory):
            return None
        if structure.is_renpy_sdk(directory):
            return DetectionResult(skipped_reason="renpy_sdk")
        if not structure.is_renpy_root(directory):
            return None
        return DetectionResult(game=analyze_game(directory, ctx))

    def finalize(self, games: list[DetectedGame], ctx: ScanContext) -> list[DetectedGame]:
        mine = [g for g in games if g.engine == ENGINE and g.status is not GameStatus.UNSUPPORTED]
        groups: dict[str, list[DetectedGame]] = defaultdict(list)
        for game in mine:
            source = game.source("global")
            resolved = game.metadata_state is MetadataState.RESOLVED
            if resolved and source is not None and source.method != "dir_name_match":
                groups[path_key(source.path)].append(game)
        absorbed: set[int] = set()
        for members in groups.values():
            if len(members) < 2:
                continue
            members.sort(key=lambda g: path_key(g.game_root))
            # D9: installs of one game (the same non-empty config.name) become one game ...
            by_name: dict[str, list[DetectedGame]] = defaultdict(list)
            for member in members:
                key = normalize_name(member.identity.config_name)
                by_name[key or f"#{path_key(member.game_root)}"].append(member)
            survivors = []
            for same in by_name.values():
                for other in same[1:]:
                    _absorb_install(same[0], other)
                    absorbed.add(id(other))
                survivors.append(same[0])
            # ... and different games that share the directory stay different games (D48 1):
            # each keeps the folder as a confirmed source and backs it up into its own record.
            for member in survivors:
                others = [o.game_root for o in survivors if o is not member]
                if not others:
                    continue
                source = member.source("global")
                assert source is not None
                source.notes.append(f"shared with a different game: {'; '.join(others)}")
                member.shared_global_with = others
        games = [g for g in games if id(g) not in absorbed]

        owners = {
            path_key(s.path): g
            for g in mine
            if id(g) not in absorbed and g.metadata_state is MetadataState.RESOLVED
            for s in g.sources
            if s.kind is SourceKind.GLOBAL and s.method != "dir_name_match"
        }
        for game in games:
            if game.engine != ENGINE:
                continue
            for source in game.sources:
                owner = owners.get(path_key(source.path))
                if source.method == "dir_name_match" and owner is not None and owner is not game:
                    source.notes.append(f"also the save directory of {owner.display_name}")
            _order_sources(game)
            _assign_mirrors(game)
            game.status = game_status(game)
        return games
