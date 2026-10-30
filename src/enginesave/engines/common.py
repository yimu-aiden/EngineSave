"""What the rule engines share (0.10.0, D65 3-5): a folder's own listing and engine
fingerprint, asked once per folder however many detectors look at it; small-file reads and
their text; save sources that follow from rules; and the game they make.

An engine module implements :class:`EngineDetector`: the fingerprint names the engine (the
same order of checks as before 0.10.0, so a folder never changes engine), the module finds
the title, the identity key and the save folders. A folder its rules cannot settle is left
to :class:`~enginesave.engines.fingerprints.FingerprintDetector`, which labels it
``UNSUPPORTED`` as before.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import locale
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from enginesave import roots
from enginesave.engines.base import DetectionResult, Detector, ScanContext, game_status
from enginesave.fsutil import UnsafePathError, dir_stats, is_reparse_point, is_within, long_path
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

SMALL_FILE_BYTES = 64 * 1024  # titles and settings come from small files only (M40)
# names an editor starts a project with or a tool fills in: no identity, since two games may
# well share them (6.6 rule 1 would merge them)
GENERIC_NAMES = frozenset(
    {
        "project", "project1", "project2", "myproject", "my project", "game", "new game",
        "untitled", "test", "rpg maker mv", "rpg maker mz", "rpg maker xp", "rpg maker vx",
        "rpg maker vx ace", "electron", "app", "electron-app", "my-app", "nw", "krkr",
        "krkrz", "tvpwin32", "tvpwin64", "gamepro", "start",
    }
)  # fmt: skip


@dataclass(frozen=True)
class Listing:
    """A folder's own files and folders by lower-case name, with their names on disk; never
    through a symlink or a junction (D20)."""

    files: dict[str, str]
    dirs: dict[str, str]

    def file(self, name: str) -> str | None:
        return self.files.get(name.lower())

    def dir(self, name: str) -> str | None:
        return self.dirs.get(name.lower())

    def exes(self) -> list[str]:
        return [name for lower, name in sorted(self.files.items()) if lower.endswith(".exe")]


def list_folder(directory: Path) -> Listing | None:
    try:
        with os.scandir(long_path(directory)) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return None
    files: dict[str, str] = {}
    dirs: dict[str, str] = {}
    for entry in entries:
        try:
            if is_reparse_point(entry):
                continue
            if entry.is_file(follow_symlinks=False):
                files.setdefault(entry.name.lower(), entry.name)
            elif entry.is_dir(follow_symlinks=False):
                dirs.setdefault(entry.name.lower(), entry.name)
        except OSError:
            continue
    return Listing(files, dirs)


def looked_at(directory: Path, ctx: ScanContext) -> tuple[Listing | None, Any]:
    """``(listing, fingerprint)`` of the folder the scan is asking about, worked out by the
    first detector that asks and kept for the others (only the last folder is kept)."""
    from enginesave.engines.fingerprints import fingerprint

    key = str(directory)
    memo = ctx.memo.get("folder")
    if memo is None or memo[0] != key:
        listing = list_folder(directory)
        found = fingerprint(directory, listing) if listing is not None else None
        memo = (key, listing, found)
        ctx.memo["folder"] = memo
    return memo[1], memo[2]


def read_small(path: Path, limit: int = SMALL_FILE_BYTES) -> bytes | None:
    """The bytes of a regular file (not a link) of at most ``limit`` bytes, else ``None``."""
    try:
        if is_reparse_point(path) or not path.is_file():
            return None
        with open(long_path(path), "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    return data if len(data) <= limit else None


def read_head(path: Path, limit: int = SMALL_FILE_BYTES) -> bytes | None:
    """The first ``limit`` bytes of a regular file (not a link), however big it is."""
    try:
        if is_reparse_point(path) or not path.is_file():
            return None
        with open(long_path(path), "rb") as handle:
            return handle.read(limit)
    except OSError:
        return None


def system_codepage() -> str:
    """The system's ANSI code page (``cp936`` on a Chinese Windows): what a tool that writes
    "ANSI" text writes there (0.11.0, D66 1)."""
    try:
        return locale.getencoding()
    except (AttributeError, ValueError):  # pragma: no cover - Python before 3.11
        return "cp1252"


def _looks_japanese(text: str) -> bool:
    """Kana and no half-width katakana: Shift-JIS read right. GBK bytes read as Shift-JIS
    give half-width katakana (``ﾓｶｱﾄﾃｰﾏﾕ``), Shift-JIS read as GBK gives no kana at all."""
    kana = any("\u3041" <= c <= "\u30ff" for c in text)
    return kana and not any("\uff61" <= c <= "\uff9f" for c in text)


# 0.11.0 (M44 review): what SaveTrail 0.10.0 read, so that its records keep matching
_LEGACY = contextvars.ContextVar("enginesave_legacy_decoding", default=False)
_DIFFERED: contextvars.ContextVar[list[bool] | None] = contextvars.ContextVar(
    "enginesave_decoding_differed", default=None
)


@contextlib.contextmanager
def legacy_decoding() -> Iterator[None]:
    """Read text and identity keys as 0.10.0 did (:func:`legacy_decode_text`, no rejected
    keys): only to find the key 0.10.0 gave a game."""
    token = _LEGACY.set(True)
    try:
        yield
    finally:
        _LEGACY.reset(token)


@contextlib.contextmanager
def noting_decoding() -> Iterator[list[bool]]:
    """A list that gets an item whenever :func:`decode_text` reads text otherwise than 0.10.0
    did (a GBK title, a Japanese one without kana on a Chinese system)."""
    noted: list[bool] = []
    token = _DIFFERED.set(noted)
    try:
        yield noted
    finally:
        _DIFFERED.reset(token)


def legacy_decode_text(data: bytes) -> str:
    """0.10.0's reading: by the BOM, else UTF-8, else Shift-JIS, else UTF-8 with replacement
    characters."""
    for bom, codec in (
        (b"\xef\xbb\xbf", "utf-8"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if data.startswith(bom):
            return data[len(bom) :].decode(codec, errors="replace")
    for codec in ("utf-8", "cp932"):
        with contextlib.suppress(UnicodeDecodeError):
            return data.decode(codec)
    return data.decode("utf-8", errors="replace")


def decode_text(data: bytes) -> str:
    """A small text file as its tool wrote it: by its BOM, else UTF-8, else the system's ANSI
    code page (GBK here: Chinese RPG Maker games), else Shift-JIS (CP932, Japanese tools) --
    Shift-JIS first when only it gives Japanese text --, else UTF-8 with replacement
    characters (which keep a title from being an identity key)."""
    if _LEGACY.get():
        return legacy_decode_text(data)
    text = _decode_text(data)
    noted = _DIFFERED.get()
    if noted is not None and not noted and text != legacy_decode_text(data):
        noted.append(True)
    return text


def _decode_text(data: bytes) -> str:
    for bom, codec in (
        (b"\xef\xbb\xbf", "utf-8"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if data.startswith(bom):
            return data[len(bom) :].decode(codec, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    found: dict[str, str] = {}
    for codec in dict.fromkeys((system_codepage(), "cp932")):
        try:
            found[codec] = data.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    japanese = found.get("cp932")
    if japanese is not None and _looks_japanese(japanese):
        return japanese
    if found:
        return next(iter(found.values()))
    return data.decode("utf-8", errors="replace")


def read_json(path: Path, limit: int = SMALL_FILE_BYTES) -> Any:
    """A small JSON file (a BOM allowed), or ``None`` when it is missing, too big or not JSON."""
    data = read_small(path, limit)
    if data is None:
        return None
    try:
        return json.loads(decode_text(data))
    except (ValueError, RecursionError):
        return None


def text_value(value: Any) -> str | None:
    """A non-empty string, stripped; anything else is ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def rule_source(
    source_id: str,
    kind: SourceKind,
    path: Path,
    token: str,
    relative: str,
    evidence: Sequence[Evidence],
    *,
    patterns: Sequence[str] = (),
    excludes: Sequence[str] = (),
    candidate: bool = False,
    method: str = "engine_rule",
) -> SaveSource:
    """A folder the engine's rules point at (D65 5): with files (as the source takes them)
    ``CONFIRMED`` -- ``CANDIDATE`` when only a name matched -- else ``EXPECTED_ABSENT``."""
    exists, count, size = dir_stats(path, patterns, excludes)
    found = SourceStatus.CANDIDATE if candidate else SourceStatus.CONFIRMED
    source = SaveSource(
        source_id,
        kind,
        str(path),
        RootToken(token),
        relative,
        found if count else SourceStatus.EXPECTED_ABSENT,
        method,
        list(evidence),
        patterns=list(patterns),
        excludes=list(excludes),
    )
    source.exists, source.file_count, source.total_bytes = exists, count, size
    return source


def game_source(
    root: Path,
    source_id: str,
    relative: str,
    evidence: Sequence[Evidence],
    **options: Any,
) -> SaveSource:
    """A save folder inside the game folder (``relative`` ``.`` is the folder itself, with
    ``patterns``)."""
    path = roots.join_relative(root, relative)
    return rule_source(
        source_id, SourceKind.LOCAL, path, roots.GAME_ROOT, relative, evidence, **options
    )


def profile_source(
    source_id: str,
    token: str,
    relative: str,
    ctx: ScanContext,
    evidence: Sequence[Evidence],
    warnings: list[str],
    **options: Any,
) -> SaveSource | None:
    """A save folder in the user profile (LocalLow, LOCALAPPDATA, APPDATA; D65 1), found as
    ``--root-override``, ``ENGINESAVE_ROOT_<TOKEN>`` or the system say; ``None`` (with a
    warning) when that folder cannot be found here or the name is not a safe one."""
    try:
        path, how = roots.resolve(token, relative, ctx.env)
    except UnsafePathError as exc:
        warnings.append(f"save folder {token}/{relative} not used: {exc}")
        return None
    if path is None:
        warnings.append(f"save folder {token}/{relative} not looked at: {how}")
        return None
    return rule_source(source_id, SourceKind.GLOBAL, path, token, relative, evidence, **options)


PROGRAM_FILES = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")


def virtualstore_relative(directory: Path, env: Mapping[str, str]) -> str | None:
    """0.11.0 (D69 3): where Windows redirects the writes of an old game (no manifest) that
    is installed in Program Files -- ``%LOCALAPPDATA%\\VirtualStore\\<the path without the
    drive>`` -- as a VIRTUALSTORE relative path; ``None`` for a game elsewhere."""
    folded = {k.casefold(): v for k, v in env.items()}  # dict(os.environ) is upper case
    for name in PROGRAM_FILES:
        base = folded.get(name.casefold())
        if base and Path(base).is_absolute() and is_within(directory, base):
            parts = Path(os.path.abspath(directory)).parts[1:]
            return "/".join(parts) if parts else None
    return None


# programs beside a game that are not the game (they would name every game alike)
HELPER_EXES = ("unitycrashhandler", "crashpad", "notification_helper", "uninst", "unins0")
HELPER_NAMES = frozenset({"setup", "editor", "launcher", "updater", "patcher", "config"})


def is_helper_exe(stem: str) -> bool:
    lower = stem.casefold()
    return lower in HELPER_NAMES or lower.startswith(HELPER_EXES)


def identity_key(name: str | None) -> str | None:
    """A name as an engine's identity key (D65 3), or ``None`` for one many games share (a
    default title, a helper program's name)."""
    if not name or " ".join(name.casefold().split()) in GENERIC_NAMES or is_helper_exe(name):
        return None
    # D66 1: read with the wrong code page (a replacement character, or the private-use
    # characters GBK gives undefined bytes): two games may read alike
    if not _LEGACY.get() and misread(name):
        return None
    return name


def garbled(text: str) -> bool:
    """Read with the wrong code page for sure: :func:`misread`, or half-width katakana (what
    GBK bytes read as Shift-JIS give, as 0.10.0 read Chinese titles)."""
    return misread(text) or any("\uff61" <= c <= "\uff9f" for c in text)


def misread(name: str) -> bool:
    """A replacement or private-use character: text read with the wrong code page."""
    return any(c == "\ufffd" or "\ue000" <= c <= "\uf8ff" for c in name)


def rule_game(
    directory: Path,
    engine: str,
    *,
    title: str | None,
    engine_key: str | None,
    sources: Sequence[SaveSource],
    evidence: Sequence[Evidence],
    warnings: Sequence[str] = (),
    engine_version: str | None = None,
    company: str | None = None,
    product: str | None = None,
    project: str | None = None,
    unsupported: bool = False,
) -> DetectedGame:
    """A game of a rule engine: its metadata is what the rules read (``RESOLVED``), its
    status follows from its sources (6.4, D65 5)."""
    exes = sorted(
        (p.stem for p in directory.glob("*.exe") if not is_helper_exe(p.stem)),
        key=str.casefold,
    )
    identity = GameIdentity(
        None,
        None,
        None,
        None,
        exes[0] if exes else None,
        directory.name,
        engine_key=identity_key(engine_key),
        title=title,
        company=company,
        product=product,
        project=project,
    )
    game = DetectedGame(
        game_root=str(directory),
        engine=engine,
        engine_version=engine_version,
        display_name=title or directory.name,
        identity=identity,
        metadata_state=MetadataState.RESOLVED if not unsupported else MetadataState.MISSING,
        status=GameStatus.UNSUPPORTED if unsupported else GameStatus.NOT_FOUND,
        sources=list(sources),
        evidence=list(evidence),
        warnings=list(warnings),
        installs=[
            {"game_root": str(directory), "config_version": None, "engine_version": engine_version}
        ],
    )
    game.status = game_status(game)
    return game


class EngineDetector(Detector):
    """A rule engine's detector: ``engines`` are the fingerprints it takes; ``recognise``
    returns the game, or ``None`` to leave the folder to the fingerprint detector."""

    engines: tuple[str, ...] = ()

    def detect(self, directory: Path, ctx: ScanContext) -> DetectionResult | None:
        listing, found = looked_at(directory, ctx)
        if listing is None or found is None or found.engine not in self.engines:
            return None
        try:
            game = self.recognise(directory, listing, found, ctx)
        except UnsafePathError:  # a folder named by the game that is not a safe one
            return None  # left to the fingerprint detector: unsupported
        return DetectionResult(game=game) if game is not None else None

    def recognise(
        self, directory: Path, listing: Listing, found: Any, ctx: ScanContext
    ) -> DetectedGame | None:
        raise NotImplementedError
