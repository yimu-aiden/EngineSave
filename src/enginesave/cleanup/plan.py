"""``plan_cleanup`` and the read-only checks a folder must pass before it is removed (plan
section 19 M13, D26): snapshot, freshness, running game, shared global folder, whitelist,
recycle bin."""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from enginesave import layout, roots
from enginesave.appdirs import config_dir
from enginesave.cleanup import model, windows
from enginesave.cleanup.model import (
    CLEANED_RESULTS,
    ENLARGE_HINT,
    GAME_NOT_IN_BACKUP,
    GAME_ROOT,
    GLOBAL,
    MAX_LISTED,
    PERMANENT,
    RECYCLE,
    RECYCLE_HINT,
    RECYCLE_MARGIN_PERCENT,
    UNLINKED,
    CleanupPlan,
    CleanupTarget,
    RecycleBin,
    RecycleLedger,
    gigabytes,
)
from enginesave.cleanup.windows import DRIVE_FIXED
from enginesave.engines.renpy.paths import default_saves_root, game_saves_root, is_non_game_dir
from enginesave.errors import FatalError, UsageError
from enginesave.fsutil import (
    UnsafePathError,
    describe_error,
    is_reparse_point,
    is_within,
    long_path,
    path_key,
    safe_relative_component,
    sha256_file,
    source_files,
    walk_files,
)
from enginesave.models import DetectedGame, GameStatus, RootToken, ScanReport, SourceKind
from enginesave.repo import LINKED_FORMATS, Repo
from enginesave.restore import validate_manifest
from enginesave.scan import ScanOptions, run_scan
from enginesave.verify import verify_snapshot


def in_use(path: Path) -> bool:
    """True when ``path`` cannot be opened for writing because another process holds it.

    The file is opened for appending and closed again at once; nothing is written. A read-only
    file cannot be tested this way and counts as free (the process check still covers it).
    """
    try:
        if os.stat(long_path(path)).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:  # type: ignore[attr-defined]
            return False
    except (AttributeError, OSError):
        pass
    try:
        fd = os.open(long_path(path), os.O_WRONLY | os.O_APPEND)
    except PermissionError:
        return True
    except OSError:
        return False
    os.close(fd)
    return False


# ------------------------------------------------------------------------------ the checks


def parse_size(text: str) -> int:
    """``4G``, ``500MB``, ``1024`` → bytes (binary units)."""
    value = text.strip().upper().removesuffix("B")
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    factor = units.get(value[-1:], 1)
    number = value[:-1] if value[-1:] in units else value
    try:
        size = int(float(number) * factor)
    except ValueError:
        raise UsageError(f"not a size: {text!r} (examples: 4G, 500M, 1048576)") from None
    if size <= 0:
        raise UsageError(f"the size must be positive: {text!r}")
    return size


def same_path(a: Path | str, b: Path | str) -> bool:
    return path_key(a) == path_key(b)


def _limited(items: list[str], what: str) -> list[str]:
    if len(items) <= MAX_LISTED:
        return items
    return [*items[:MAX_LISTED], f"... and {len(items) - MAX_LISTED} more {what}"]


def _path_problems(
    path: Path,
    *,
    repo: Repo,
    scan_roots: Sequence[Path],
    saves_roots: Sequence[Path],
    profile_roots: Sequence[Path] = (),
) -> list[str]:
    """Whitelist checks shared by every kind of target (5)."""
    problems = []
    home = Path.home()
    if not path.is_absolute():
        return [f"not an absolute path: {path}"]
    if not path.is_dir():
        return [f"the folder does not exist: {path}"]
    if is_reparse_point(path):
        problems.append("the folder is a link (symlink or junction); remove it by hand")
    if same_path(path, Path(path.anchor)):
        problems.append("a drive root is never cleaned up")
    if is_within(home, path):
        problems.append("the home folder and its parents are never cleaned up")
    if is_within(path, repo.root) or is_within(repo.root, path):
        problems.append(f"overlaps the backup library {repo.root}")
    for root in scan_roots:
        if is_within(root, path):
            problems.append(f"is or contains the scan root {root}")
    for root in saves_roots:
        if is_within(root, path):
            problems.append(f"is or contains the Ren'Py saves root {root}")
    for root in profile_roots:  # 0.10.0 (D65 4): LocalLow, LOCALAPPDATA, APPDATA themselves
        if is_within(root, path):
            problems.append(f"is or contains the profile folder {root}")
    return problems


def _files_by_path(manifest: Mapping[str, Any], source_id: str) -> dict[str, dict[str, Any]]:
    return {
        os.path.normcase(f["path"]): f
        for f in manifest.get("files", [])
        if f["source_id"] == source_id
    }


def _freshness(
    folder: Path,
    backed_up: Mapping[str, Mapping[str, Any]] | None,
    label: str,
    selection: tuple[Sequence[str], Sequence[str]] = ((), ()),
) -> list[str]:
    """Files in ``folder`` that are missing from, or differ from, the snapshot files of a
    source; of a source with ``patterns``/``excludes`` (D65 2) only its own files."""
    if not folder.is_dir():
        return []
    files, errors = source_files(folder, *selection)
    label = "" if label in ("", ".") else f"{label}/"  # the game folder itself: no prefix
    problems = [
        f"contains a link: {label}{e.rel} (remove it by hand)"
        if "reparse point" in e.error
        else f"cannot read {label}{e.rel}: {e.error}"
        for e in errors
    ]
    for entry in files:
        item = (backed_up or {}).get(os.path.normcase(entry.rel))
        if item is None:
            problems.append(f"not in the backup: {label}{entry.rel}")
            continue
        if item["size"] != entry.size:
            problems.append(f"changed since the backup: {label}{entry.rel}")
            continue
        try:
            digest, _ = sha256_file(entry.path)
        except OSError as exc:
            problems.append(f"cannot read {label}{entry.rel}: {describe_error(exc)}")
            continue
        if digest != item["sha256"]:
            problems.append(f"changed since the backup: {label}{entry.rel}")
    return problems


def _selection(source: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    return list(source.get("patterns") or []), list(source.get("excludes") or [])


def _origin_root(source: Mapping[str, Any]) -> Path | None:
    origin = Path(str(source.get("origin_path") or ""))
    if not origin.is_absolute():
        return None
    if str(source["relative_root"]) == roots.ROOT_ITSELF:  # D65 2: the game folder itself
        return origin
    parts = [p for p in str(source["relative_root"]).replace("\\", "/").split("/") if p]
    tail = [p.casefold() for p in origin.parts[-len(parts) :]] if parts else []
    if not parts or tail != [p.casefold() for p in parts]:
        return None
    return origin.parents[len(parts) - 1]


def _snapshot_problems(
    repo: Repo, game_id: str, snapshot_id: str, manifest: dict[str, Any], name: str
) -> tuple[list[str], int]:
    """(1) complete, verified, objects included; returns the problems and the verified count."""
    problems = []
    if manifest.get("status") != "complete":
        problems.append(
            f"snapshot {snapshot_id} is {manifest.get('status')}; back up again (game closed)"
        )
    check = verify_snapshot(repo, game_id, snapshot_id, name)
    if not check.clean:
        bad = len(check.mismatched) + len(check.missing) + len(check.extra) + len(check.problems)
        problems.append(f"snapshot {snapshot_id} does not verify ({bad} problems); run verify")
    if manifest.get("format_version") in LINKED_FORMATS:
        snapshot_dir = repo.snapshot_dir(game_id, snapshot_id)
        first: dict[str, dict[str, Any]] = {}
        for item in manifest.get("files", []):
            first.setdefault(item["sha256"], item)
        for digest, item in sorted(first.items()):
            obj = repo.object_path(digest)
            data = layout.snapshot_file_path(snapshot_dir, manifest, item)  # D67 3
            if not obj.is_file():
                problems.append(f"object {digest[:12]} is missing from the object store")
                continue
            with contextlib.suppress(OSError):
                if os.path.samefile(long_path(obj), long_path(data)):
                    continue  # the same file the verification above just hashed
            try:
                ok = sha256_file(obj) == (digest, item["size"])
            except OSError:
                ok = False
            if not ok:
                problems.append(f"object {digest[:12]} is damaged; run verify --objects")
    return problems, check.ok


def _running_problems(roots: Sequence[Path]) -> list[str]:
    """(3) the game must be closed: no exe or log.txt held open, no process image inside."""
    problems = []
    roots = [root for root in roots if root.is_dir()]
    images = (windows.list_process_images() or []) if roots else []
    for root in roots:
        candidates = [*root.glob("*.exe"), root / "log.txt"]
        if (root / "lib").is_dir():
            candidates += [p for p in (root / "lib").rglob("*.exe") if p.is_file()]
        busy = [p for p in candidates if p.is_file() and in_use(p)]
        if busy:
            names = ", ".join(_label(p, root) for p in busy[:5])
            problems.append(f"the game seems to be running ({names} in use); close it first")
        inside = [img for img in images if is_within(img, root)]
        if inside:
            problems.append(f"the game is running ({Path(inside[0]).name}); close it first")
    return problems


def _label(path: Path, folder: Path) -> str:
    """``path`` relative to ``folder`` with forward slashes (the full path if outside it)."""
    try:
        return str(path.relative_to(folder)).replace("\\", "/")
    except ValueError:
        return str(path)


def _resolve(
    kind: str,
    request: str | None,
    doc: Mapping[str, Any],
    saves_root: Path,
    default_root: Path,
    env: Mapping[str, str] | None = None,
) -> tuple[Path | None, str | None]:
    """The folder a request names, or ``(None, reason)`` when it is not on the whitelist."""
    identity = doc.get("identity") or {}
    record_kind = doc.get("kind", "game")
    if kind == GLOBAL and record_kind == "game" and (doc.get("engine") or "renpy") != "renpy":
        return _profile_folder(doc, os.environ if env is None else env)
    save_directory = identity.get("renpy_save_directory")
    if kind == GAME_ROOT:
        if record_kind != "game":
            return None, "an orphaned (unlinked) record has no game folder"
        recorded = [
            i
            for i in doc.get("installs", [])
            if i.get("game_root") and Path(i["game_root"]).is_absolute()
        ]
        installs = [Path(i["game_root"]) for i in recorded]
        if request:
            # A cleaned-up install is still one of the record's (D41): checking it again
            # says "already cleaned" instead of "not an install folder".
            match = [p for p in installs if same_path(p, request)]
            if not match:
                listed = "; ".join(str(p) for p in installs) or "none"
                return None, f"{request} is not an install folder of this record (known: {listed})"
            return match[0], None
        existing = [p for p in installs if p.is_dir()]
        if len(existing) == 1:
            return existing[0], None
        if not existing:
            removed = sorted(
                (i for i in recorded if i.get("removed_at")), key=lambda i: str(i["removed_at"])
            )
            if removed:  # the latest one cleaned up: the check reports it as cleaned
                return Path(removed[-1]["game_root"]), None
            return None, "no install folder of this game exists any more"
        return None, "several install folders; name one: " + "; ".join(map(str, existing))
    if kind in (GLOBAL, UNLINKED):
        if kind == GLOBAL and record_kind != "game":
            return None, "use the unlinked target for an orphaned record"
        if kind == UNLINKED and record_kind != "unlinked":
            return None, "only an orphaned (unlinked) record has an unlinked folder"
        if kind == UNLINKED and (
            not request or request.casefold() != str(save_directory).casefold()
        ):
            return None, f"{request!r} is not the folder of this record ({save_directory})"
        if not save_directory:
            return None, "the record has no global save folder (config.save_directory is unset)"
        try:
            name = safe_relative_component(str(save_directory))
        except UnsafePathError as exc:
            return None, f"unsafe save directory name: {exc}"
        if is_non_game_dir(name):
            return None, f"{name} belongs to Ren'Py itself and is never cleaned up"
        root = saves_root if kind == GLOBAL else default_root
        return root / name, None
    return None, f"unknown target {kind!r}"


def _profile_template(doc: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Another engine's "global" folder (D65 4): the record's one source in a profile folder
    (Unity's LocalLow, Unreal's LOCALAPPDATA, Electron's APPDATA...)."""
    found = [
        t
        for t in doc.get("source_templates") or []
        if isinstance(t, dict)
        and t.get("root_token") in roots.ENGINE_PROFILE_TOKENS
        and t.get("kind") == SourceKind.GLOBAL.value  # a folder, never the registry
    ]
    if not found:
        return None, "the record has no save folder outside its game folder"
    if len(found) > 1:
        names = ", ".join(f"{t['root_token']}/{t['relative_root']}" for t in found)
        return None, f"several save folders outside the game folder ({names}); clean up by hand"
    return found[0], None


def has_global_folder(doc: Mapping[str, Any]) -> bool:
    """A record has a save folder outside its game folders that a cleanup could check: Ren'Py's
    global folder, or another engine's in the user profile (D65 4)."""
    if (doc.get("identity") or {}).get("renpy_save_directory"):
        return True
    return any(
        isinstance(t, dict)
        and t.get("root_token") in roots.ENGINE_PROFILE_TOKENS
        and t.get("kind") == SourceKind.GLOBAL.value
        for t in doc.get("source_templates") or []
    )


def _profile_folder(
    doc: Mapping[str, Any], env: Mapping[str, str]
) -> tuple[Path | None, str | None]:
    template, why = _profile_template(doc)
    if template is None:
        return None, why
    try:
        path, how = roots.resolve(str(template["root_token"]), str(template["relative_root"]), env)
    except UnsafePathError as exc:
        return None, f"unsafe save folder: {exc}"
    return (path, None) if path is not None else (None, how)


def _weak_folder(doc: Mapping[str, Any]) -> str | None:
    """Why another engine's profile folder is not cleaned up although it was backed up: it was
    found by a name other games may share (D65 4, the most careful reading)."""
    identity = doc.get("identity") or {}
    template = _profile_template(doc)[0] or {}
    company = str(identity.get("company") or "")
    if (
        template.get("root_token") == roots.UNITY_LOCALLOW
        and company.casefold() == "defaultcompany"
    ):
        return (
            "the folder is under Unity's placeholder company DefaultCompany, which other games "
            "may use too; clean it up by hand"
        )
    if not identity.get("engine_key"):
        return (
            "the folder was found by a name other games may share (no identity of the game); "
            "clean it up by hand"
        )
    return None


def _profile_paths(record: Mapping[str, Any], env: Mapping[str, str]) -> list[Path]:
    """Every folder outside the game folder a record's sources name (D65 4)."""
    return roots.profile_folders(record.get("source_templates") or [], env)


def _protected_roots(env: Mapping[str, str]) -> list[Path]:
    """The profile folders themselves and EngineSave's own settings folder: never cleaned up,
    whatever a record names (D65 4)."""
    found = [config_dir(env)]
    for token in roots.ENGINE_PROFILE_TOKENS:
        path, _ = roots.profile_root(token, env)
        if path is not None:
            found.append(path)
    return sorted({path_key(p): p for p in found}.values())


def cleaned_entry(doc: Mapping[str, Any], folder: Path | str) -> dict[str, Any] | None:
    """How and when a cleanup of this record removed ``folder``: ``{"method", "at"}``, plus
    ``purged_at`` once it was purged from the recycle bin (D41); ``None`` if none did."""
    for entry in reversed(doc.get("cleanups") or []):
        for item in entry.get("targets") or []:
            if item.get("result") in CLEANED_RESULTS and same_path(item["path"], folder):
                found = {"method": entry.get("method"), "at": item.get("at") or entry["at"]}
                purged = item.get("purged_from_recycle_bin") or {}
                if purged.get("at"):
                    found["purged_at"] = purged["at"]
                return found
    return None


def _saves_roots(
    doc: Mapping[str, Any], env: Mapping[str, str], override: Path | None
) -> tuple[Path, Path]:
    """``(default saves root, the record's saves root)``: the latter follows the first install
    that still exists (a portable ``Ren'Py Data`` beside it, S-077)."""
    installs = [Path(i["game_root"]) for i in doc.get("installs", []) if i.get("game_root")]
    first = next((p for p in installs if p.is_absolute() and p.is_dir()), None)
    # A relative override (``--renpy-saves-root .local\x``) means the folder below the
    # current directory, as for scan and backup; the whitelist needs absolute paths.
    default_root = Path(os.path.abspath(default_saves_root(env, override)[0]))
    saves_root = Path(os.path.abspath(game_saves_root(first, env, override)[0]))
    return default_root, saves_root


def save_folder(
    doc: Mapping[str, Any],
    *,
    saves_root_override: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """A record's global save folder, or its own folder for an orphaned (``unlinked``) record,
    resolved as the cleanup resolves it; ``None`` when it has none. Nothing is checked or
    changed (the window opens it, D36)."""
    env = dict(os.environ) if env is None else dict(env)
    default_root, saves_root = _saves_roots(doc, env, saves_root_override)
    name = (doc.get("identity") or {}).get("renpy_save_directory")
    if doc.get("kind", "game") == "unlinked":
        folder, _ = _resolve(UNLINKED, str(name or ""), doc, saves_root, default_root)
    else:
        folder, _ = _resolve(GLOBAL, None, doc, saves_root, default_root, env)
    return folder


Selection = tuple[Sequence[str], Sequence[str]]  # a source's patterns and excludes (D65 2)


def _scan_save_folders(
    root: Path, env: Mapping[str, str], override: Path | None, engine: str = "renpy"
) -> list[tuple[Path, Selection]] | None:
    """Save folders inside an install (with which of their files are saves), as a read-only
    scan sees them; ``None`` if no game is recognised there."""
    report = run_scan(
        ScanOptions(roots=[root], include_unlinked=False, saves_root_override=override, env=env)
    )
    for game in report.games:
        found = {path_key(game.game_root), *(path_key(i["game_root"]) for i in game.installs)}
        if path_key(root) not in found:
            continue
        if engine != "renpy" and (  # 0.10.0: another engine, or one its rules cannot settle
            game.engine != engine or game.status is GameStatus.UNSUPPORTED
        ):
            return None
        return [
            (Path(s.path), (s.patterns, s.excludes))
            for s in game.sources
            if s.root_token in (RootToken.GAME_ROOT, RootToken.ABSOLUTE) and is_within(s.path, root)
        ]
    return None


def _overlap(theirs: Path, folder: Path) -> bool:
    """Another game's folder is the folder, inside it or around it (D65 4)."""
    return is_within(theirs, folder) or is_within(folder, theirs)


def _shared_problems(
    folder: Path,
    save_directory: str | None,
    removing: Sequence[Path],
    doc: Mapping[str, Any],
    known: Sequence[Mapping[str, Any]],
    report: ScanReport | None,
    env: Mapping[str, str],
) -> list[str]:
    """(4) another install that stays must not use the same global folder: a Ren'Py game by
    its save directory, another engine's (0.10.0, D65 4) by the folder's path, across the
    records and the games of the scan."""
    users: list[str] = []
    wanted = save_directory.casefold() if save_directory is not None else None

    def uses(record: Mapping[str, Any]) -> bool:
        if wanted is not None:
            theirs = (record.get("identity") or {}).get("renpy_save_directory")
            return str(theirs or "").casefold() == wanted
        return any(_overlap(p, folder) for p in _profile_paths(record, env))

    def scanned_uses(game: DetectedGame) -> bool:
        if wanted is not None:
            return str(game.identity.renpy_save_directory or "").casefold() == wanted
        return any(
            _overlap(Path(s.path), folder)
            for s in game.sources
            if s.root_token.value in roots.ENGINE_PROFILE_TOKENS
        )

    def stays(root: str) -> bool:
        return Path(root).is_dir() and not any(same_path(root, r) for r in removing)

    records = [doc, *[k for k in known if k.get("game_id") != doc.get("game_id")]]
    for record in records:
        if not uses(record):
            continue
        users += [
            i["game_root"]
            for i in record.get("installs", [])
            if i.get("game_root") and not i.get("removed_at") and stays(i["game_root"])
        ]
    for game in report.games if report is not None else []:
        if scanned_uses(game) and stays(game.game_root):
            users.append(game.game_root)
    unique = sorted({path_key(u): u for u in users}.values())
    if unique:
        return [
            "the global folder is still used by "
            + "; ".join(unique[:3])
            + " (clean up that install too, or keep the folder)"
        ]
    return []


def recycle_bin_of(ledger: RecycleLedger, anchor: str) -> RecycleBin:
    """The drive's bin as the run first saw it (read once per drive and run, D41)."""
    key = anchor.upper()
    if key not in ledger.bins:
        ledger.bins[key] = windows.recycle_bin_info(anchor)
    return ledger.bins[key]


def _recycle_problems(
    path: Path, size: int, limit: int | None, ledger: RecycleLedger
) -> tuple[list[str], str | None]:
    """(6) recycling must be real: a recycle bin on a fixed drive, turned on (D35).
    ``limit`` is ``--recycle-limit``. Returns ``(refusals, shortage note)``: a folder bigger
    than the room left is not refused but noted, room is made first when the cleanup runs
    (D41); with an unreadable bin size the 4 GB limit still refuses."""
    anchor = path.anchor
    if not anchor:
        return [], None  # a relative path is already refused by the whitelist
    problems = []
    if not windows.has_recycle_bin(anchor):
        problems.append(f"{anchor} has no recycle bin; {RECYCLE_HINT}")
    elif windows.drive_type(anchor) != DRIVE_FIXED:
        problems.append(f"{anchor} is not a fixed drive (no reliable recycle bin); {RECYCLE_HINT}")
    bin_ = recycle_bin_of(ledger, anchor)
    if bin_.disabled:  # 1.0.0 (D72 R04): whether it can recycle comes before any size limit
        problems.append(
            f"the recycle bin of {anchor} is turned off (Windows would delete at once); "
            f"{RECYCLE_HINT}"
        )
    if limit is not None:  # set explicitly with --recycle-limit: it only replaces the size
        if size > limit:
            problems.append(
                f"{gigabytes(size)} is above the --recycle-limit of {gigabytes(limit)}; "
                f"{RECYCLE_HINT} (or raise --recycle-limit)"
            )
        return problems, None
    room = ledger.room(anchor.upper())
    if bin_.disabled:
        pass  # said above
    elif bin_.room is None:
        fallback = model.DEFAULT_RECYCLE_LIMIT
        if size > fallback:
            problems.append(
                f"{gigabytes(size)} is above {gigabytes(fallback)}, the limit used "
                f"when the size of the recycle bin of {anchor} cannot be read; {RECYCLE_HINT} "
                "(or raise --recycle-limit)"
            )
    elif room is not None and size > room:
        return problems, (
            f"{gigabytes(size)} is more than the {gigabytes(max(room, 0))} left in the recycle "
            f"bin of {anchor} (capacity {gigabytes(bin_.capacity or 0)}, "
            f"{RECYCLE_MARGIN_PERCENT}% kept free); room is made first when it is cleaned up "
            "(--on-full-bin), or " + ENLARGE_HINT
        )
    return problems, None


def plan_cleanup(
    repo: Repo,
    record: Mapping[str, Any],
    targets: Sequence[tuple[str, str | None]],
    *,
    snapshot_id: str = "latest",
    method: str = RECYCLE,
    recycle_limit: int | None = None,
    scan_roots: Sequence[Path] = (),
    report: ScanReport | None = None,
    saves_root_override: Path | None = None,
    env: Mapping[str, str] | None = None,
    ledger: RecycleLedger | None = None,
) -> CleanupPlan:
    """Check every requested folder; nothing is changed. ``targets`` are ``(kind, path)``.

    ``path`` is an install folder for ``game_root`` (``None``: the record's only existing
    install), ``None`` for ``global`` and the folder name for ``unlinked``. Recycling is
    limited by the room in each drive's recycle bin unless ``recycle_limit`` is given (D35);
    a batch shares one ``ledger`` so that the room is counted across its records (D41).
    """
    if method not in (RECYCLE, PERMANENT):
        raise UsageError(f"unknown method {method!r}")
    if not targets:
        raise UsageError("choose at least one folder to clean up")
    env = dict(os.environ) if env is None else dict(env)
    game_id = record["game_id"]
    doc = repo.load_game(game_id)
    name = str(doc.get("display_name") or game_id)
    installs = [Path(i["game_root"]) for i in doc.get("installs", []) if i.get("game_root")]
    default_root, saves_root = _saves_roots(doc, env, saves_root_override)
    saves_roots = sorted({path_key(p): p for p in (default_root, saves_root)}.values())

    chosen = repo.latest_snapshot(game_id) if snapshot_id in ("latest", None) else snapshot_id
    plan = CleanupPlan(
        repo,
        game_id,
        name,
        doc.get("kind", "game"),
        chosen,
        None,
        method,
        recycle_limit,
        [],
        [(k, p) for k, p in targets],
        {
            "scan_roots": list(scan_roots),
            "report": report,
            "saves_root_override": saves_root_override,
            "env": env,
        },
        ledger=ledger if ledger is not None else RecycleLedger(),
    )
    common: list[str] = []
    manifest: dict[str, Any] = {"files": [], "sources": []}
    if chosen is None or chosen not in repo.snapshot_ids(game_id):
        common.append(f"no snapshot {snapshot_id} for {name}; back it up first")
    else:
        try:
            manifest = repo.load_manifest(game_id, chosen)
            validate_manifest(manifest)
            plan.snapshot_reason = manifest.get("reason")
            problems, plan.verified_files = _snapshot_problems(
                repo, game_id, chosen, manifest, name
            )
            common += problems
        except FatalError as exc:
            common.append(f"snapshot {chosen} is unreadable: {exc}")

    known = repo.known_games(repo.load_catalog())
    requested_roots: list[Path] = []
    for kind, request in targets:
        if kind == GAME_ROOT:
            folder, _ = _resolve(kind, request, doc, saves_root, default_root, env)
            if folder is not None:
                requested_roots.append(folder)
    protected = _protected_roots(env)

    for kind, request in targets:
        folder, why = _resolve(kind, request, doc, saves_root, default_root, env)
        target = CleanupTarget(kind, str(folder) if folder else str(request or ""))
        plan.targets.append(target)
        if folder is None:
            target.refuse(str(why))
            continue
        if not os.path.lexists(long_path(folder)):  # D41: not there is not refused
            target.already_cleaned = cleaned_entry(doc, folder)
            target.gone = target.already_cleaned is None
            continue
        for problem in [
            *common,
            *_path_problems(
                folder,
                repo=repo,
                scan_roots=scan_roots,
                saves_roots=saves_roots,
                profile_roots=protected,
            ),
        ]:
            target.refuse(problem)
        if not folder.is_dir():
            continue
        files, errors = walk_files(folder)
        target.files, target.bytes = len(files), sum(f.size for f in files)
        links = [e.rel for e in errors if "reparse point" in e.error]
        if links:
            target.refuse(f"contains links ({', '.join(links[:3])}); remove them by hand first")
        sources = manifest.get("sources", [])
        if kind == GAME_ROOT:
            target.notes.append(GAME_NOT_IN_BACKUP)
            checked: list[tuple[Path, str | None, Selection]] = []
            for source in sources:
                token = source["root_token"]
                if (
                    token == RootToken.GAME_ROOT.value
                    and (o := _origin_root(source))
                    and same_path(o, folder)
                ):
                    path = roots.join_relative(folder, str(source["relative_root"]))
                    checked.append((path, source["source_id"], _selection(source)))
                elif token == RootToken.ABSOLUTE.value and is_within(
                    source["relative_root"], folder
                ):
                    path = Path(source["relative_root"])
                    checked.append((path, source["source_id"], _selection(source)))
            engine = str(doc.get("engine") or "renpy")
            scanned = _scan_save_folders(folder, env, saves_root_override, engine)
            if scanned is None:
                target.refuse(
                    "no game is recognised in this folder any more; where its saves are "
                    "cannot be checked"
                )
            for path, selection in scanned or []:
                if not any(same_path(path, p) and s == selection for p, _, s in checked):
                    checked.append((path, None, selection))
            freshness: list[str] = []
            for path, source_id, selection in checked:
                entries = _files_by_path(manifest, source_id) if source_id else None
                freshness += _freshness(path, entries, _label(path, folder), selection)
            for problem in _limited(freshness, "files"):
                target.refuse(problem)
            if freshness:
                target.notes.append("back up again before cleaning up")
            for problem in _running_problems([folder]):
                target.refuse(problem)
        else:
            renpy = kind == UNLINKED or (doc.get("engine") or "renpy") == "renpy"
            if renpy:
                token = RootToken.RENPY_SAVES_ROOT.value
                relative = str((doc.get("identity") or {}).get("renpy_save_directory"))
            else:  # D65 4: the profile folder's source, by its token and relative root
                template = _profile_template(doc)[0] or {}
                token = str(template.get("root_token"))
                relative = str(template.get("relative_root"))
            source = next(
                (
                    s
                    for s in sources
                    if s["root_token"] == token
                    and path_key(str(s["relative_root"])) == path_key(relative)
                ),
                None,
            )
            entries = _files_by_path(manifest, source["source_id"]) if source else None
            if source is None and target.files:
                target.refuse(f"the snapshot has no copy of {folder.name}; back it up first")
            # D65 4: a profile folder goes as a whole: every file in it but the excluded
            # (logs, caches) must be in the backup, whatever the source's patterns took
            selection = ((), _selection(source)[1]) if source is not None else ((), ())
            freshness = (
                _freshness(folder, entries, folder.name, selection) if source is not None else []
            )
            for problem in _limited(freshness, "files"):
                target.refuse(problem)
            if freshness:
                target.notes.append("back up again before cleaning up")
            running_roots = [p for p in installs if p.is_absolute() and p.is_dir()]
            for problem in _running_problems(running_roots):
                target.refuse(problem)
            if kind == GLOBAL and not renpy and (weak := _weak_folder(doc)) is not None:
                target.refuse(weak)
            # 1.0.0 (D72 R05): an orphaned folder too -- "orphaned" was the record's state
            # when it was backed up; a game installed since may use the folder now
            if kind in (GLOBAL, UNLINKED):
                shared = _shared_problems(
                    folder, relative if renpy else None, requested_roots, doc, known, report, env
                )
                for problem in shared:
                    target.refuse(problem)
        if method == RECYCLE:
            problems, shortage = _recycle_problems(folder, target.bytes, recycle_limit, plan.ledger)
            for problem in problems:
                target.refuse(problem)
                target.recycle_refused = True
            if shortage:
                target.needs_room = True
                target.notes.append(shortage)
            drive = folder.anchor.upper()
            if drive in plan.ledger.bins:
                plan.recycle_bins[drive] = plan.ledger.bins[drive]
        target.ok = not target.reasons
    return plan


def requested(request: tuple[str, str | None], target: CleanupTarget) -> bool:
    kind, path = request
    if kind != target.kind:
        return False
    return kind != GAME_ROOT or path is None or same_path(path, target.path)
