"""Backup repository I/O (plan 5.3): ``catalog.json``, ``game.json`` and snapshot folders.

Every metadata write is atomic (temp file + ``os.replace``); snapshots are assembled in a
``.tmp-*`` folder and committed with one ``os.replace`` so a failure never leaves a half
snapshot behind.

Since 0.11.0 (D67, format 2) a record's folder is named after the game (``games/<name>``, the
catalog entry's ``folder``; ``games/<id>`` for a record without one) and a new snapshot's
folder after the local time it was made (``YYYY-MM-DD HH-MM-SS``); snapshots are ordered by
their manifest's ``created_at``. A library of format 1 (0.1.0-0.10.0) is read as it is and
written only by ``migrate`` (and by a mirror of such a library), see :mod:`enginesave.migrate`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from enginesave import layout
from enginesave.errors import FatalError, UsageError
from enginesave.fsutil import (
    atomic_write_text,
    iso_from_ns,
    iso_utc,
    long_path,
    ns_from_iso,
    path_key,
    utc_now,
)

FORMAT_VERSION = 2  # catalog.json and game.json; 2 = folders named after the games (D67)
OLD_FORMAT = 1  # the layout of 0.1.0-0.10.0: read, written only by migrate and a mirror of it
FORMATS = (OLD_FORMAT, FORMAT_VERSION)
# new snapshots: data files are hard links into the object store (D18), laid out by where they
# belong (0.11.0, D67 3: format 3; 2 had data/<source id>)
MANIFEST_FORMAT = 3
MANIFEST_FORMATS = (1, 2, 3)  # readable manifest versions; 1 = plain copies (0.1.0)
LINKED_FORMATS = (2, 3)  # files are hard links into the object store (or copies of objects)
log = logging.getLogger("enginesave.repo")
CATALOG = "catalog.json"
GAMES_DIR = "games"
GAME_FILE = "game.json"
SNAPSHOTS_DIR = "snapshots"
MANIFEST = "manifest.json"
DATA_DIR = "data"
OBJECTS_DIR = "objects"
OBJECT_ALGO = "sha256"
TMP_PREFIX = ".tmp-"
USER_REASONS = ("manual", "import")  # snapshots `latest` may pick; pre_restore is automatic
# A snapshot id: 0.1.0-0.10.0 UTC ``YYYYMMDDTHHMMSSZ`` with ``-1``, ``-2``... (D11); 0.11.0 the
# local time ``YYYY-MM-DD HH-MM-SS`` with `` (2)``, `` (3)``... (D67 2). Both stay valid.
OLD_SNAPSHOT_ID = r"(\d{8}T\d{6}Z)(?:-(\d+))?"
NEW_SNAPSHOT_ID = r"(\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2})(?: \((\d+)\))?"
SNAPSHOT_ID_RE = re.compile(rf"^(?:{OLD_SNAPSHOT_ID}|{NEW_SNAPSHOT_ID})$")
GAME_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
OBJECT_RE = re.compile(r"^[0-9a-f]{64}$")
# a manifest's created_at is its third key: read from its first kilobytes when it is there
_CREATED_RE = re.compile(
    r'^\s*\{\s*"format_version"\s*:\s*\d+\s*,\s*"snapshot_id"\s*:\s*"[^"\\]*"\s*,'
    r'\s*"created_at"\s*:\s*"([^"\\]+)"'
)
# M45 review: kept across Repo objects (the window reads the library with a new one each time)
_CREATED: dict[str, tuple[int, int, int | None]] = {}
_SHORT_NAME = re.compile(r"[^.~]{1,6}~\d{1,6}(?:\.[^.]{0,3})?")  # an 8.3 alias, e.g. READAB~1
MIGRATE_HINT = (
    "this library has the layout of SaveTrail 0.10.0 and earlier (format 1): it can be read, "
    'but not written, until it is migrated: enginesave migrate --repo "{root}"'
)


def parse_snapshot_id(snapshot_id: str) -> tuple[str, int, bool] | None:
    """``(base, number, utc)`` of a snapshot id: an old id is UTC and numbered ``-n`` from 1, a
    new one local time and numbered `` (n)`` from 2 (its first is 1); None when it is no id."""
    match = SNAPSHOT_ID_RE.match(snapshot_id)
    if not match:
        return None
    if match.group(1):
        return match.group(1), int(match.group(2) or 0), True
    return match.group(3), int(match.group(4) or 1), False


def snapshot_ns(snapshot_id: str) -> int | None:
    """When a snapshot id says it was made (ns since 1970); a new id is read as this machine's
    local time. None for anything else."""
    parsed = parse_snapshot_id(snapshot_id)
    if parsed is None:
        return None
    base, _n, utc = parsed
    try:
        if utc:
            moment = datetime.strptime(base, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        else:
            moment = datetime.strptime(base, "%Y-%m-%d %H-%M-%S").astimezone()
        return int(moment.timestamp()) * 1_000_000_000
    except (ValueError, OverflowError, OSError):
        return None


def snapshot_sort_key(snapshot_id: str) -> tuple[int, int, str]:
    """Order of snapshot ids by the time they name (a fallback: :meth:`Repo.snapshot_ids`
    orders by the manifests' ``created_at``)."""
    parsed = parse_snapshot_id(snapshot_id)
    ns = snapshot_ns(snapshot_id)
    return (-1 if ns is None else ns, parsed[1] if parsed else -1, snapshot_id)


def numbered_snapshot_id(base: str, number: int) -> str:
    """The ``number``-th id of a second: ``base`` itself for the first."""
    if "T" in base:  # an old id (only a repair of an old library ever numbers one)
        return base if number <= 0 else f"{base}-{number}"
    return base if number <= 1 else f"{base} ({number})"


def base_snapshot_id(moment: datetime) -> str:
    """D67 2: local ``YYYY-MM-DD HH-MM-SS`` (0.1.0-0.10.0: UTC ``YYYYMMDDTHHMMSSZ``, D11)."""
    return moment.astimezone().strftime("%Y-%m-%d %H-%M-%S")


# ------------------------------------------------------------------- folder names (D67 1)

FOLDER_MAX = 80  # characters of a record folder's name taken from the game's name
_FOLDER_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{device}{n}" for device in ("COM", "LPT") for n in range(1, 10)}
)


def clean_folder_name(text: str) -> str:
    """``text`` as a folder name: the characters Windows forbids and control characters become
    spaces, white space runs become one space, trailing dots and spaces go, cut to
    :data:`FOLDER_MAX` characters; empty when nothing is left or it is a device name."""
    text = " ".join(_FOLDER_BAD.sub(" ", str(text or "")).split())
    text = text.rstrip(" .")[:FOLDER_MAX].rstrip(" .")
    if not text or text.split(".")[0].strip().upper() in RESERVED_NAMES or short_name(text):
        return ""
    return text


def short_name(name: str) -> bool:
    """An 8.3 alias (``READAB~1``): Windows may already give that name to another folder,
    which would then be two records' (M45 review)."""
    return bool(_SHORT_NAME.fullmatch(name))


def valid_folder(name: object) -> bool:
    """A name a catalog entry's ``folder`` may have: one folder, no forbidden character."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= 255
        and not _FOLDER_BAD.search(name)
        and name not in (".", "..")
        and not name.endswith((" ", "."))
        and name.split(".")[0].strip().upper() not in RESERVED_NAMES
        and not short_name(name)
    )


def folder_name_for(
    display_name: str | None, version: str | None, game_id: str, taken: Iterable[str]
) -> str:
    """D67 1: the game's name (or ``game <first 8 of the id>``), then ``<name> (<version>)``,
    then ``<name> (<first 8 of the id>)``: the first that is not ``taken`` (compared without
    case, as Windows does)."""
    used = {t.casefold() for t in taken}
    name = clean_folder_name(display_name or "") or f"game {game_id[:8]}"
    candidates = [name]
    if cleaned := clean_folder_name(version or "")[:40].rstrip(" ."):
        candidates.append(f"{name} ({cleaned})")
    candidates += [f"{name} ({game_id[:8]})", f"{name} ({game_id})", game_id]
    return next((c for c in candidates if c.casefold() not in used and not short_name(c)), game_id)


def record_version(doc: Mapping[str, Any]) -> str | None:
    """The version a record's folder name may carry: its ``config_version``, else the newest
    recorded install's."""
    identity = doc.get("identity") if isinstance(doc.get("identity"), dict) else {}
    version = identity.get("config_version") if identity else None
    if isinstance(version, str) and version.strip():
        return version
    installs = [i for i in doc.get("installs") or [] if isinstance(i, dict)]
    for install in sorted(installs, key=lambda i: str(i.get("last_seen") or ""), reverse=True):
        if isinstance(install.get("config_version"), str) and install["config_version"].strip():
            return install["config_version"]
    return None


def dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _read_json(path: Path, what: str) -> Any:
    try:
        with open(long_path(path), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise FatalError(f"{what} is missing: {path}") from None
    except (OSError, ValueError) as exc:
        raise FatalError(f"{what} is unreadable ({exc}): {path}") from exc


def _check_format(data: Any, what: str, path: Path, allowed: tuple[int, ...] = FORMATS) -> dict:
    if not isinstance(data, dict):
        raise FatalError(f"{what} is not a JSON object: {path}")
    version = data.get("format_version")
    if version not in allowed:
        newer = type(version) is int and version > max(allowed)
        raise FatalError(
            f"{what} has unsupported format_version {version!r}"
            + (" (written by a newer EngineSave)" if newer else "")
            + f": {path}"
        )
    return data


def read_manifest(path: Path) -> dict:
    """A snapshot's ``manifest.json`` of a readable format."""
    return _check_format(_read_json(path, MANIFEST), MANIFEST, path, MANIFEST_FORMATS)


def game_removed(doc: Mapping[str, Any]) -> bool:
    """Every recorded install of the game was cleaned up (D26)."""
    installs = [i for i in doc.get("installs", []) if isinstance(i, dict) and i.get("game_root")]
    return bool(installs) and all(i.get("removed_at") for i in installs)


def cleaned_at(doc: Mapping[str, Any]) -> str | None:
    """When the game was cleaned up, while none of its installs is back (catalog field)."""
    if not game_removed(doc):
        return None
    return max(str(i["removed_at"]) for i in doc["installs"] if i.get("removed_at"))


def present_installs(doc: Mapping[str, Any]) -> list[Path]:
    """The recorded install folders that are on disk now (D43); ``removed_at`` does not count,
    the folder does."""
    roots = [Path(str(i["game_root"])) for i in doc.get("installs") or [] if i.get("game_root")]
    return [p for p in roots if p.is_absolute() and p.is_dir()]


def install_present(doc: Mapping[str, Any]) -> bool:
    """「本体存在」 rather than 「本体不在」 (D43, D51): at least one install folder is there."""
    return bool(present_installs(doc))


def destination_state(dest: Path) -> str:
    """``"existing"`` (has a catalog), ``"new"`` (absent or empty); refuses anything else."""
    if (dest / CATALOG).is_file():
        return "existing"
    if dest.exists() and not dest.is_dir():
        raise UsageError(f"destination is not a directory: {dest}")
    if dest.is_dir() and any(dest.iterdir()):
        raise UsageError(
            f"destination exists, is not empty and has no {CATALOG}; refusing to write "
            f"into it: {dest}"
        )
    return "new"


def remove_tree(path: Path) -> list[str]:
    """Remove ``path`` and everything in it (D49); returns what could not be removed. A link or
    junction is never followed: it is reported instead."""
    if os.path.islink(path) or os.path.isjunction(path):
        return [str(path)]
    leftovers: list[str] = []

    def retry(function: Callable[..., Any], failed: str, _exc: BaseException) -> None:
        try:
            os.chmod(failed, stat.S_IWRITE)  # a read-only file
            function(failed)
        except OSError:
            leftovers.append(failed)

    if os.path.lexists(long_path(path)):
        shutil.rmtree(long_path(path), onexc=retry)
    return leftovers


def _remove_file(path: Path) -> bool:
    """Remove one file (a read-only one too) or link; False when it stays."""
    try:
        os.remove(long_path(path))
        return True
    except PermissionError:
        pass
    except OSError:
        return False
    if os.path.islink(path):  # chmod would act on what the link points to
        return False
    try:
        os.chmod(long_path(path), stat.S_IWRITE)  # a read-only file
        os.remove(long_path(path))
    except OSError:
        return False
    return True


RECENT_SECONDS = 600  # D55: changed in the last ten minutes means "a backup may be writing it"


@dataclass(frozen=True)
class TmpFolder:
    """A leftover of an interrupted run (D55): its size, files and newest modification time
    (of the folder or anything in it)."""

    path: Path
    bytes: int
    files: int
    modified_ns: int
    own_bytes: int = 0  # files with no other link: what deleting it frees by itself
    # D59 4: for a snapshot's temporary folder, the newest change in objects/.tmp -- a large
    # file of the same backup may be being written into the object store for a long time
    store_ns: int = 0

    def recent(self, now: float | None = None, seconds: float = RECENT_SECONDS) -> bool:
        """Changed in the last ten minutes, or (a snapshot's temporary folder) while anything
        in ``objects/.tmp`` was: may be in use."""
        now = time.time() if now is None else now
        return now - max(self.modified_ns, self.store_ns) / 1e9 < seconds

    def held_by_store(self, now: float | None = None, seconds: float = RECENT_SECONDS) -> bool:
        """Old itself, kept only because something is being written into the store."""
        now = time.time() if now is None else now
        return self.recent(now, seconds) and now - self.modified_ns / 1e9 >= seconds

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "bytes": self.bytes,
            "files": self.files,
            "modified": iso_from_ns(self.modified_ns),
            "recent": self.recent(now),
        } | ({"held_by_store": True} if self.held_by_store(now) else {})  # 0.10.0, optional


def tmp_folder(path: Path) -> TmpFolder:
    """Size and newest time of ``path`` and everything below it; links and junctions are not
    followed. A hard link into the object store counts in ``bytes``, not in ``own_bytes``."""
    info = os.lstat(long_path(path))
    if not stat.S_ISDIR(info.st_mode) or os.path.isjunction(long_path(path)):
        own = info.st_size if info.st_nlink <= 1 and stat.S_ISREG(info.st_mode) else 0
        return TmpFolder(path, info.st_size, 1, info.st_mtime_ns, own)
    total = files = own = 0
    newest = info.st_mtime_ns
    for folder, dirs, names in os.walk(long_path(path), followlinks=False):
        for name in [*dirs, *names]:
            full = os.path.join(folder, name)
            try:
                entry = os.lstat(full)
            except OSError:
                continue
            newest = max(newest, entry.st_mtime_ns)
            if not stat.S_ISDIR(entry.st_mode):
                total += entry.st_size
                files += 1
                own += entry.st_size if entry.st_nlink <= 1 else 0
        dirs[:] = [d for d in dirs if not os.path.isjunction(os.path.join(folder, d))]
    return TmpFolder(path, total, files, newest, own)


@dataclass
class TmpRemoval:
    """What ``remove_tmp_dirs`` did (D55)."""

    removed: list[str] = field(default_factory=list)
    freed_bytes: int = 0  # the leftovers' files and the objects collected afterwards
    recent: list[str] = field(default_factory=list)  # changed in the last ten minutes: kept
    leftovers: list[str] = field(default_factory=list)  # could not be deleted
    garbage: Garbage | None = None
    cancelled: bool = False
    store_busy: bool = False  # D59 4: snapshot folders kept while objects/.tmp was written

    @property
    def exit_code(self) -> int:
        garbage = self.garbage
        blocked = garbage is None or bool(garbage.skipped or garbage.failed)
        return 1 if (self.recent or self.leftovers or blocked) else 0

    def to_dict(self) -> dict[str, Any]:
        garbage = self.garbage or Garbage(skipped="stopped before collecting")
        data: dict[str, Any] = {
            "removed": list(self.removed),
            "freed_bytes": self.freed_bytes,
            "recent": list(self.recent),
            "leftovers": list(self.leftovers),
            "objects_removed": garbage.removed,
            "objects_kept_linked": garbage.kept_linked,
            "gc_skipped": garbage.skipped,
        }
        if self.cancelled:  # D58: only when it was stopped
            data["cancelled"] = True
        if self.store_busy:  # 0.10.0 (D59 4): only when it happened
            data["store_busy"] = True
        return data


@dataclass
class Garbage:
    """What ``collect_garbage`` did: objects removed and their size, unreferenced objects
    left because something still links to them, or why nothing was collected."""

    removed: int = 0
    freed_bytes: int = 0
    kept_linked: int = 0
    failed: int = 0
    skipped: str | None = None


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        # D67 8: a library of format 1 is written only by migrate and a mirror of it, which
        # set this; everything else refuses (MIGRATE_HINT)
        self.allow_old_format = False
        self._format: int | None = None  # of the catalog read last
        self._folders: dict[str, str] | None = None  # record id -> folder, from the catalog
        self._pending: dict[str, str] = {}  # folders given to records not in the catalog yet
        self._resolved: dict[str, str] = {}
        self._strays: dict[str, str] | None = None  # record id -> folder no catalog entry names

    # ------------------------------------------------------------------------- open/create

    @classmethod
    def open(cls, root: Path) -> Repo:
        if not (root / CATALOG).is_file():
            raise UsageError(f"not an EngineSave repository (no {CATALOG}): {root}")
        repo = cls(root)
        repo.load_catalog()
        return repo

    @classmethod
    def open_or_create(cls, root: Path, *, format_version: int = FORMAT_VERSION) -> Repo:
        """An existing library, or a new empty one (of ``format_version``: only a mirror of a
        format-1 library makes one of format 1)."""
        if destination_state(root) == "existing":
            return cls.open(root)
        now = iso_utc(utc_now())
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FatalError(f"cannot create repository folder {root}: {exc}") from exc
        catalog = {
            "format_version": format_version,
            "created_at": now,
            "updated_at": now,
            "games": [],
            "overrides": [],
        }
        atomic_write_text(root / CATALOG, dump_json(catalog))
        repo = cls(root)
        repo._remember(catalog)
        return repo

    # ----------------------------------------------------------------------------- catalog

    @property
    def catalog_path(self) -> Path:
        return self.root / CATALOG

    @property
    def format_version(self) -> int:
        """The library's format (D67 8): 2, or 1 for a library not migrated yet."""
        if self._format is None:
            if not self.catalog_path.is_file():
                return FORMAT_VERSION
            self.load_catalog()
        assert self._format is not None
        return self._format

    @property
    def old_format(self) -> bool:
        return self.format_version != FORMAT_VERSION

    def require_writable(self) -> None:
        """Raise :class:`UsageError` (exit 2) for a library of format 1 (D67 8)."""
        if self.old_format and not self.allow_old_format:
            raise UsageError(MIGRATE_HINT.format(root=self.root))

    def load_catalog(self) -> dict:
        data = _check_format(_read_json(self.catalog_path, CATALOG), CATALOG, self.catalog_path)
        if not isinstance(data.get("games"), list) or not isinstance(
            data.get("overrides", []), list
        ):
            raise FatalError(f"{CATALOG} is malformed: {self.catalog_path}")
        data.setdefault("overrides", [])
        self._remember(data)
        return data

    def save_catalog(self, catalog: dict) -> None:
        """Write the catalog; an entry without ``folder`` gets the one its record has here."""
        self.require_writable()
        if catalog.get("format_version") == FORMAT_VERSION:
            known = self._known_folders()
            for entry in catalog["games"]:
                if isinstance(entry, dict) and "folder" not in entry:
                    game_id = entry.get("game_id")
                    folder = self._pending.get(game_id) or known.get(game_id)  # type: ignore[arg-type]
                    if folder:
                        entry["folder"] = folder
        catalog["updated_at"] = iso_utc(utc_now())
        atomic_write_text(self.catalog_path, dump_json(catalog))
        self._remember(catalog)

    def _remember(self, catalog: Mapping[str, Any]) -> None:
        self._format = catalog.get("format_version")  # type: ignore[assignment]
        folders: dict[str, str] = {}
        used: set[str] = set()
        for entry in catalog.get("games") or []:
            if not isinstance(entry, dict):
                continue
            game_id, folder = entry.get("game_id"), entry.get("folder")
            if isinstance(game_id, str) and valid_folder(folder):
                if str(folder).casefold() not in used:  # two entries, one folder: the first
                    folders[game_id] = str(folder)
                    used.add(str(folder).casefold())
        self._folders = folders
        self._pending = {k: v for k, v in self._pending.items() if k not in folders}
        self._resolved.clear()
        self._strays = None

    # ------------------------------------------------------------------- record folders

    def _known_folders(self) -> dict[str, str]:
        if self._folders is None:
            try:
                if self.catalog_path.is_file():
                    self.load_catalog()
            except FatalError:  # an unreadable catalog: every record at games/<id>
                pass
        return self._folders or {}

    def _stray_folders(self) -> dict[str, str]:
        """Folders under ``games/`` no catalog entry names, by the record id their
        ``game.json`` holds: an interrupted migration's, rename's or mirror's (D67 8)."""
        if self._strays is None:
            strays: dict[str, str] = {}
            named = {f.casefold() for f in self._known_folders().values()}
            games = self.root / GAMES_DIR
            with contextlib.suppress(OSError):
                for path in sorted(games.iterdir()) if games.is_dir() else []:
                    if path.name.casefold() in named:
                        continue
                    with contextlib.suppress(OSError, ValueError, FatalError):
                        game_id = _read_json(path / GAME_FILE, GAME_FILE).get("game_id")
                        # 1.0.0 (D72 R14): a folder named like an id is a stray too when its
                        # game.json holds another record's id
                        if (
                            isinstance(game_id, str)
                            and GAME_ID_RE.match(game_id)
                            and game_id.casefold() != path.name.casefold()
                        ):
                            strays.setdefault(game_id, path.name)
            self._strays = strays
        return self._strays

    def folder_name(self, game_id: str) -> str:
        """The name of a record's folder under ``games/`` (D67 1): the one given to it here,
        else the catalog's ``folder`` when it is there, else ``<id>`` when that is there, else
        a folder whose ``game.json`` holds the id; otherwise the catalog's or ``<id>``."""
        if game_id in self._pending:
            return self._pending[game_id]
        if game_id in self._resolved:
            return self._resolved[game_id]
        games = self.root / GAMES_DIR
        named = self._known_folders().get(game_id)
        if named and os.path.isdir(long_path(games / named)):
            found = named
        elif os.path.isdir(long_path(games / game_id)):
            found = game_id
        else:
            found = self._stray_folders().get(game_id) or named or game_id
        self._resolved[game_id] = found
        return found

    def taken_folder_names(self, but: str | None = None) -> set[str]:
        """Every name under ``games/`` and every folder a record has or was given (case-folded),
        except the folder of record ``but``."""
        folders = {**self._known_folders(), **self._pending}  # a pending one replaces its own
        names = {f.casefold() for f in folders.values()}
        games = self.root / GAMES_DIR
        with contextlib.suppress(OSError):
            names |= {p.name.casefold() for p in games.iterdir()} if games.is_dir() else set()
        if but is not None:
            names.discard(self.folder_name(but).casefold())
        return names

    def assign_folder(self, game_id: str, display_name: str | None, version: str | None) -> str:
        """The folder of a new record (D67 1), named once from its name; a record that has a
        folder keeps it. A library of format 1 keeps ``games/<id>``."""
        if not GAME_ID_RE.match(game_id):
            raise FatalError(f"invalid game id {game_id!r}")
        current = self.folder_name(game_id)
        if self.old_format or os.path.isdir(long_path(self.root / GAMES_DIR / current)):
            return current
        if game_id in self._known_folders():
            return self._known_folders()[game_id]
        name = folder_name_for(display_name, version, game_id, self.taken_folder_names())
        self.set_folder(game_id, name)
        return name

    def set_folder(self, game_id: str, name: str) -> None:
        """Give a record the folder ``name`` until the catalog says otherwise."""
        if not GAME_ID_RE.match(game_id) or not valid_folder(name):
            raise FatalError(f"invalid record folder {name!r} for {game_id!r}")
        self._pending[game_id] = name
        self._resolved.pop(game_id, None)
        self._strays = None

    def record_folders(self) -> list[tuple[str | None, Path]]:
        """Every folder under ``games/`` with the record id it is the folder of (``None`` for
        a folder no record resolves to, e.g. an orphan of an interrupted removal)."""
        games = self.root / GAMES_DIR
        if not games.is_dir():
            return []
        ids = {r.get("game_id") for r in self.load_catalog()["games"] if isinstance(r, dict)}
        owners = {
            self.folder_name(game_id).casefold(): game_id
            for game_id in ids
            if isinstance(game_id, str) and GAME_ID_RE.match(game_id)
        }
        strays = {name.casefold(): game_id for game_id, name in self._stray_folders().items()}
        found: list[tuple[str | None, Path]] = []
        for path in sorted(games.iterdir()):
            if not path.is_dir():
                continue
            key = path.name.casefold()
            owner = owners.get(key)
            if owner is None and GAME_ID_RE.match(path.name) and path.name not in ids:
                owner = path.name  # a record folder of 0.10.0 the catalog does not name
            if owner is None and strays.get(key) not in ids:
                owner = strays.get(key)
            found.append((owner, path))
        return found

    def folder_rename(self, game_id: str) -> tuple[str, str]:
        """``(current, new)`` folder name of a record named after its current name (D67 1,
        「按当前名字重命名文件夹…」); the same twice when nothing would change."""
        doc = self.load_game(game_id)
        current = self.folder_name(game_id)
        taken = self.taken_folder_names(but=game_id)
        name = doc.get("display_name")
        return current, folder_name_for(name, record_version(doc), game_id, taken)

    def rename_folder(self, game_id: str) -> tuple[str, str]:
        """Rename a record's folder after its current name, then record it in the catalog
        (an interruption in between is found again by the id in the folder's ``game.json``).
        Returns ``(old, new)``; nothing happens when they are the same."""
        self.require_writable()
        old, new = self.folder_rename(game_id)
        if old == new:
            return old, new
        games = self.root / GAMES_DIR
        try:
            os.rename(long_path(games / old), long_path(games / new))
        except OSError as exc:
            raise FatalError(f"cannot rename the record folder {old!r} to {new!r}: {exc}") from exc
        catalog = self.load_catalog()
        for entry in catalog["games"]:
            if isinstance(entry, dict) and entry.get("game_id") == game_id:
                entry["folder"] = new
        self.set_folder(game_id, new)
        self.save_catalog(catalog)
        layout.write_index(self)  # D67 5
        return old, new

    # ------------------------------------------------------------------------------- games

    def game_dir(self, game_id: str) -> Path:
        if not GAME_ID_RE.match(game_id):
            raise FatalError(f"invalid game id {game_id!r}")
        return self.root / GAMES_DIR / self.folder_name(game_id)

    def has_game(self, game_id: str) -> bool:
        return (self.game_dir(game_id) / GAME_FILE).is_file()

    def load_game(self, game_id: str) -> dict:
        path = self.game_dir(game_id) / GAME_FILE
        return _check_format(_read_json(path, GAME_FILE), GAME_FILE, path)

    def save_game(self, game: dict) -> None:
        self.require_writable()
        if not self.old_format:
            game["format_version"] = FORMAT_VERSION
        atomic_write_text(self.game_dir(game["game_id"]) / GAME_FILE, dump_json(game))

    def known_games(self, catalog: dict) -> list[dict]:
        """``game.json`` documents of every catalog record that still has one. One that
        cannot be read is left out and logged (1.0.0, D70 6): it matches no game until it is
        repaired, and every other record goes on being backed up."""
        docs = []
        for record in catalog.get("games", []):
            game_id = record.get("game_id")
            if isinstance(game_id, str) and GAME_ID_RE.match(game_id) and self.has_game(game_id):
                try:
                    docs.append(self.load_game(game_id))
                except FatalError as exc:
                    log.warning("record %s left out: %s", game_id, exc)
        return docs

    def set_override(self, catalog: dict, override: dict, replaces: Callable[[dict], bool]) -> bool:
        """Persist ``override`` (D12), dropping earlier ones matched by ``replaces``."""
        kept = [o for o in catalog["overrides"] if not replaces(o)]
        replaced = len(kept) != len(catalog["overrides"])
        catalog["overrides"] = [*kept, override]
        self.save_catalog(catalog)
        return replaced

    # --------------------------------------------------------------------------- snapshots

    def snapshots_dir(self, game_id: str) -> Path:
        return self.game_dir(game_id) / SNAPSHOTS_DIR

    def snapshot_ids(self, game_id: str) -> list[str]:
        return self.folder_snapshot_ids(self.game_dir(game_id))

    def folder_snapshot_ids(self, record: Path) -> list[str]:
        """The snapshots in a record folder, oldest first: by their manifests' ``created_at``
        (D67 2), by the time the id names where a manifest cannot be read."""
        folder = record / SNAPSHOTS_DIR
        if not folder.is_dir():
            return []
        ids = [p.name for p in folder.iterdir() if p.is_dir() and SNAPSHOT_ID_RE.match(p.name)]
        return sorted(ids, key=lambda sid: self._order(folder / sid / MANIFEST, sid))

    def _order(self, manifest: Path, snapshot_id: str) -> tuple[int, int, str]:
        fallback = snapshot_sort_key(snapshot_id)
        created = self.created_ns(manifest)
        return fallback if created is None else (created, fallback[1], snapshot_id)

    def created_ns(self, manifest: Path) -> int | None:
        """A manifest's ``created_at`` (ns since 1970), read from its first kilobytes when it
        is there (it is the third key) and remembered while the file stays the same."""
        try:
            info = os.stat(long_path(manifest))
        except OSError:
            return None
        key = path_key(manifest)
        cached = _CREATED.get(key)
        if cached is not None and cached[:2] == (info.st_mtime_ns, info.st_size):
            return cached[2]
        value: int | None = None
        try:
            with open(long_path(manifest), "rb") as f:
                head = f.read(4096).decode("utf-8", "replace")
            found = _CREATED_RE.match(head)
            text = found.group(1) if found else _read_json(manifest, MANIFEST).get("created_at")
            value = ns_from_iso(str(text))
        except (OSError, ValueError, AttributeError, FatalError):
            value = None
        _CREATED[key] = (info.st_mtime_ns, info.st_size, value)
        return value

    def snapshot_created(self, game_id: str, snapshot_id: str) -> str | None:
        """When a snapshot was made (UTC ISO): its manifest's ``created_at``, else the time
        its id names."""
        ns = self.created_ns(self.snapshot_dir(game_id, snapshot_id) / MANIFEST)
        ns = snapshot_ns(snapshot_id) if ns is None else ns
        return None if ns is None else iso_utc(datetime.fromtimestamp(ns / 1e9, UTC))

    def snapshot_dir(self, game_id: str, snapshot_id: str) -> Path:
        if not SNAPSHOT_ID_RE.match(snapshot_id):
            raise UsageError(f"invalid snapshot id {snapshot_id!r}")
        return self.snapshots_dir(game_id) / snapshot_id

    def load_manifest(self, game_id: str, snapshot_id: str) -> dict:
        return read_manifest(self.snapshot_dir(game_id, snapshot_id) / MANIFEST)

    # ------------------------------------------------------------------------- object store

    def object_path(self, digest: str) -> Path:
        """``objects/sha256/<aa>/<digest>``: written once, never modified (D18)."""
        if not OBJECT_RE.match(digest):
            raise FatalError(f"invalid object name {digest!r}")
        return self.root / OBJECTS_DIR / OBJECT_ALGO / digest[:2] / digest

    def object_tmp_dir(self) -> Path:
        return self.root / OBJECTS_DIR / ".tmp"

    def iter_objects(self) -> Iterator[tuple[str, Path]]:
        """``(file name, path)`` of every file under ``objects/sha256/`` (names unchecked)."""
        store = self.root / OBJECTS_DIR / OBJECT_ALGO
        if not store.is_dir():
            return
        for bucket in sorted(store.iterdir()):
            if bucket.is_dir():
                for path in sorted(bucket.iterdir()):
                    yield path.name, path

    def new_tmp_dir(self, game_id: str, snapshot_id: str) -> Path:
        self.require_writable()
        tmp = self.snapshots_dir(game_id) / f"{TMP_PREFIX}{snapshot_id}-{secrets.token_hex(3)}"
        try:
            os.makedirs(long_path(tmp))
        except OSError as exc:
            raise FatalError(f"cannot create snapshot folder {tmp}: {exc}") from exc
        return tmp

    def next_snapshot_id(self, game_id: str, moment: datetime) -> str:
        """D67 2: the local time, then `` (2)``, `` (3)``... when a folder with that id (in any
        case) already exists."""
        base = base_snapshot_id(moment)
        existing = {s.casefold() for s in self.snapshot_ids(game_id)}
        candidate, n = base, 1
        while candidate.casefold() in existing or self.snapshot_dir(game_id, candidate).exists():
            n += 1
            candidate = numbered_snapshot_id(base, n)
        return candidate

    def commit_snapshot(
        self,
        tmp: Path,
        game_id: str,
        manifest: dict,
        readme: Callable[[dict], str] | None = None,
    ) -> str:
        """Write ``manifest.json`` (and the ``README.txt`` that ``readme`` makes of it, D67 4)
        into ``tmp`` and rename ``tmp`` to its final id atomically.

        If the id was taken meanwhile, the next suffix is used and the manifest rewritten.
        Every file the manifest lists must be in ``tmp`` with its size, or nothing is
        committed (0.10.0: a temporary folder cleaned up by another window meanwhile).
        """
        for item in manifest.get("files", []):
            try:
                placed = layout.snapshot_file_path(tmp, manifest, item)
                size = os.stat(long_path(placed)).st_size
            except (OSError, ValueError):
                size = None
            if size != item["size"]:
                raise FatalError(
                    f"the snapshot being assembled lost {item['source_id']}/{item['path']} "
                    "(was its temporary folder cleaned up?); nothing was committed"
                )
        parsed = parse_snapshot_id(manifest["snapshot_id"])
        assert parsed is not None
        snapshot_id, (base, n, _utc) = manifest["snapshot_id"], parsed
        for _ in range(1000):
            final = self.snapshot_dir(game_id, snapshot_id)
            if not final.exists():
                manifest["snapshot_id"] = snapshot_id
                atomic_write_text(tmp / MANIFEST, dump_json(manifest))
                if readme is not None:
                    try:  # D70 6: an explanation never costs the snapshot (migrate adds it)
                        layout.write_readme(tmp, readme(manifest))
                    except Exception as exc:  # noqa: BLE001 - any fault of the README alone
                        log.warning(
                            "README.txt of %s %s not written: %s", game_id, snapshot_id, exc
                        )
                try:
                    os.replace(long_path(tmp), long_path(final))
                    return snapshot_id
                except OSError:
                    if not final.exists():
                        raise
            n += 1
            snapshot_id = numbered_snapshot_id(base, n)
        raise FatalError(f"could not allocate a snapshot id for {game_id}")

    def discard_tmp(self, tmp: Path) -> None:
        shutil.rmtree(long_path(tmp), ignore_errors=True)

    def remove_if_empty(self, game_id: str) -> None:
        """Remove ``games/<id>`` left behind by a failed first snapshot (only when empty)."""
        with contextlib.suppress(OSError):
            os.rmdir(long_path(self.snapshots_dir(game_id)))
        with contextlib.suppress(OSError):
            os.rmdir(long_path(self.game_dir(game_id)))

    def stale_tmp_dirs(self) -> list[Path]:
        """Leftovers of interrupted runs: ``snapshots/.tmp-*`` folders and ``objects/.tmp/*``."""
        stale: list[Path] = []
        games = self.root / GAMES_DIR
        if games.is_dir():
            stale.extend(
                p
                for game in games.iterdir()
                if (game / SNAPSHOTS_DIR).is_dir()
                for p in (game / SNAPSHOTS_DIR).iterdir()
                if p.name.startswith(TMP_PREFIX)
            )
        tmp = self.object_tmp_dir()
        if tmp.is_dir():
            stale.extend(tmp.iterdir())
        return sorted(stale)

    def tmp_folders(self) -> list[TmpFolder]:
        """The leftovers of interrupted runs with sizes and times (D55); a snapshot's
        temporary folder carries the newest change in ``objects/.tmp`` (D59 4)."""
        store = self.store_tmp_ns()
        found = []
        for path in self.stale_tmp_dirs():
            with contextlib.suppress(OSError):  # gone meanwhile
                folder = tmp_folder(path)
                if self._snapshot_tmp(path):
                    folder = replace(folder, store_ns=store)
                found.append(folder)
        return found

    def store_tmp_ns(self) -> int:
        """The newest change of anything in ``objects/.tmp`` (0 when there is nothing)."""
        tmp = self.object_tmp_dir()
        newest = 0
        with contextlib.suppress(OSError):
            for path in tmp.iterdir():
                with contextlib.suppress(OSError):
                    newest = max(newest, tmp_folder(path).modified_ns)
        return newest

    def _snapshot_tmp(self, path: Path) -> bool:
        return path.name.startswith(TMP_PREFIX) and path.parent.name == SNAPSHOTS_DIR

    def remove_tmp_dirs(
        self,
        chosen: Iterable[Path] | None = None,
        *,
        older_than: float = RECENT_SECONDS,
        should_stop: Callable[[], bool] | None = None,
    ) -> TmpRemoval:
        """Delete the leftovers (the ``chosen`` ones, default all) that nothing changed in the
        last ``older_than`` seconds, each looked at again right before it goes, then collect
        the object store (D55). Only ``snapshots/.tmp-*`` folders and entries of
        ``objects/.tmp`` are ever touched; a link or junction is reported, not followed."""
        allowed = {path_key(p): p for p in self.stale_tmp_dirs()}
        wanted = (
            list(allowed.values())
            if chosen is None
            else [allowed[path_key(p)] for p in chosen if path_key(p) in allowed]
        )
        result = TmpRemoval()
        for path in wanted:
            if should_stop is not None and should_stop():
                result.cancelled = True
                return result
            try:
                snapshot_tmp = self._snapshot_tmp(path)
                store = self.store_tmp_ns() if snapshot_tmp else 0  # D59 4: the store first,
                folder = replace(tmp_folder(path), store_ns=store)  # then the folder itself
            except OSError:
                continue  # gone meanwhile
            if folder.recent(seconds=older_than):
                result.recent.append(str(path))
                result.store_busy |= folder.held_by_store(seconds=older_than)
                continue
            if snapshot_tmp and not os.path.islink(path) and not os.path.isjunction(path):
                # Renamed first: Windows refuses while any file in it is open, i.e. while a
                # backup still reads one back (a long check, nothing written meanwhile), and
                # such a folder is kept as in use. The new name is a leftover's too.
                doomed = path.with_name(f"{TMP_PREFIX}deleting-{secrets.token_hex(4)}")
                try:
                    os.rename(long_path(path), long_path(doomed))
                except OSError:
                    result.recent.append(str(path))
                    continue
                failed = remove_tree(doomed)
            elif os.path.isdir(long_path(path)) and not os.path.islink(path):
                failed = remove_tree(path)
            else:
                failed = [] if _remove_file(path) else [str(path)]
            if failed:
                result.leftovers += failed
            else:  # hard links into the object store free nothing until the object goes
                result.removed.append(str(path))
                result.freed_bytes += folder.own_bytes
        result.garbage = self.collect_garbage()
        result.freed_bytes += result.garbage.freed_bytes
        return result

    # ---------------------------------------------------------------- removal (0.8.0, D49)

    def orphan_game_dirs(self) -> list[Path]:
        """Folders under ``games/`` that no catalog record names, e.g. left by a removal that
        was interrupted after the catalog was written (D49): reported, never removed."""
        ids = {r.get("game_id") for r in self.load_catalog()["games"] if isinstance(r, dict)}
        return [path for owner, path in self.record_folders() if owner not in ids]

    def remove_game(self, game_id: str) -> list[str]:
        """Remove a whole record (D49 2): its catalog entry and every override of it first,
        then its snapshot folders, then its folder. Returns what could not be removed."""
        self.require_writable()
        folder = self.game_dir(game_id)
        snapshots = [folder / SNAPSHOTS_DIR / sid for sid in self.folder_snapshot_ids(folder)]
        catalog = self.load_catalog()
        catalog["games"] = [r for r in catalog["games"] if r.get("game_id") != game_id]
        catalog["overrides"] = [o for o in catalog["overrides"] if o.get("game_id") != game_id]
        self.save_catalog(catalog)
        leftovers: list[str] = []
        for snapshot in snapshots:
            leftovers += remove_tree(snapshot)
        leftovers += remove_tree(folder)
        return leftovers

    def remove_snapshots(self, game_id: str, snapshot_ids: Iterable[str]) -> list[str]:
        """Remove some snapshots of a record: the catalog entry is updated first, then the
        snapshot folders go; all of them means the whole record (``remove_game``)."""
        self.require_writable()
        existing = self.snapshot_ids(game_id)
        wanted = set(snapshot_ids)
        unknown = sorted(wanted - set(existing))
        if unknown:
            raise UsageError(f"no snapshot {', '.join(unknown)} for {game_id}")
        remaining = [s for s in existing if s not in wanted]
        if not remaining:
            return self.remove_game(game_id)
        catalog = self.load_catalog()
        for entry in catalog["games"]:
            if entry.get("game_id") == game_id:
                entry["latest_snapshot"] = remaining[-1]
                entry["snapshot_count"] = len(remaining)
        self.save_catalog(catalog)
        leftovers: list[str] = []
        for snapshot_id in (s for s in existing if s in wanted):
            leftovers += remove_tree(self.snapshot_dir(game_id, snapshot_id))
        return leftovers

    def referenced_objects(self) -> tuple[set[str], list[str]]:
        """Every SHA-256 a manifest on disk names (also in folders the catalog does not name),
        and the manifests that could not be read (``<game folder>/<snapshot>``)."""
        refs: set[str] = set()
        unreadable: list[str] = []
        games = self.root / GAMES_DIR
        for game in sorted(games.iterdir()) if games.is_dir() else []:
            folder = game / SNAPSHOTS_DIR
            if not game.is_dir() or not folder.is_dir():
                continue
            for snapshot in sorted(folder.iterdir()):
                if not snapshot.is_dir() or snapshot.name.startswith(TMP_PREFIX):
                    continue
                path = snapshot / MANIFEST
                try:
                    manifest = _check_format(
                        _read_json(path, MANIFEST), MANIFEST, path, MANIFEST_FORMATS
                    )
                    files = manifest.get("files")
                    if not isinstance(files, list):
                        raise FatalError(f"{MANIFEST} has no file list: {path}")
                    refs.update(
                        f["sha256"]
                        for f in files
                        if isinstance(f, dict) and isinstance(f.get("sha256"), str)
                    )
                except FatalError:
                    unreadable.append(f"{game.name}/{snapshot.name}")
        return refs, unreadable

    def collect_garbage(self) -> Garbage:
        """Remove the objects no snapshot uses any more (D49 2). Nothing is removed while an
        interrupted or running backup left temporary folders, or when any manifest cannot be
        read; an object goes only when no manifest names it and no file links to it."""
        result = Garbage()
        stale = self.stale_tmp_dirs()
        if stale:
            result.skipped = f"{len(stale)} temporary folder(s) of an interrupted backup"
            return result
        referenced, unreadable = self.referenced_objects()
        if unreadable:
            result.skipped = f"{len(unreadable)} snapshot manifest(s) could not be read"
            return result
        buckets: set[Path] = set()
        for name, path in list(self.iter_objects()):
            if not OBJECT_RE.match(name) or path.parent.name != name[:2] or name in referenced:
                continue
            try:
                info = os.stat(long_path(path))
                if info.st_nlink > 1:  # a file still links to it although no manifest names
                    result.kept_linked += 1  # it: left alone
                    continue
                os.remove(long_path(path))
            except OSError:
                result.failed += 1
                continue
            result.removed += 1
            result.freed_bytes += info.st_size
            buckets.add(path.parent)
        for bucket in buckets:
            with contextlib.suppress(OSError):
                os.rmdir(long_path(bucket))  # only when it is empty now
        return result

    # ------------------------------------------------------------------------- references

    def resolve_game(self, catalog: dict, ref: str) -> dict:
        """Catalog record for ``ref``: game id, unique id prefix (>= 6), save directory or name."""
        records = [r for r in catalog["games"] if isinstance(r, dict) and "game_id" in r]
        exact = [r for r in records if r["game_id"] == ref]
        if exact:
            return exact[0]
        lowered = ref.casefold()
        tests = [
            lambda r: len(ref) >= 6 and r["game_id"].startswith(lowered),
            lambda r: (
                str((r.get("identity") or {}).get("renpy_save_directory") or "").casefold()
                == lowered
            ),
            lambda r: str(r.get("display_name") or "").casefold() == lowered,
        ]
        for test in tests:
            matches = [r for r in records if test(r)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                ids = ", ".join(r["game_id"] for r in matches)
                raise UsageError(f"--game {ref!r} is ambiguous; use one of: {ids}")
        raise UsageError(f"no game matches {ref!r} in {self.root}")

    def snapshot_summary(self, game_id: str, snapshot_id: str) -> dict[str, Any]:
        """One line of ``show``: reason, status, counts; ``status="unreadable"`` on damage."""
        try:
            manifest = self.load_manifest(game_id, snapshot_id)
        except FatalError:
            return {"snapshot_id": snapshot_id, "status": "unreadable"}
        summary = {
            "snapshot_id": snapshot_id,
            "created_at": manifest.get("created_at"),
            "reason": manifest.get("reason"),
            "status": manifest.get("status"),
            "file_count": len(manifest.get("files", [])),
            "total_bytes": sum(f.get("size", 0) for f in manifest.get("files", [])),
            "error_count": len(manifest.get("errors", [])),
        }
        if "storage" in manifest:  # format 2 (D18); format-1 output stays exactly as in 0.1.0
            summary["storage"] = manifest["storage"]
        return summary

    def latest_snapshot(self, game_id: str) -> str | None:
        """Newest ``manual``/``import`` snapshot; newest of any reason when there is none."""
        ids = self.snapshot_ids(game_id)
        for snapshot_id in reversed(ids):
            try:
                if self.load_manifest(game_id, snapshot_id).get("reason") in USER_REASONS:
                    return snapshot_id
            except FatalError:
                continue
        return ids[-1] if ids else None
