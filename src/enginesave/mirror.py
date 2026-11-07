"""Mirroring a library into another folder (0.9.0, D57): a second, complete library, best on
another drive, in the same format and usable on its own (choose it as the library and verify
or restore from it).

The target is checked first (:func:`check_target`): not the library itself, nor inside it or
around it, nor in the Ren'Py saves root, a recorded game directory or save folder, or (in the
window) a scan root. It must be missing or empty, or a library whose every record is one of
this library's, or a mirror of it that still keeps records this library has deleted since: it
carries this library's ``catalog.json`` ``created_at`` and shares records with it (record ids
are random, so no other library does). Anything else is refused and never written into.

Then, in an order that keeps an interruption harmless and a second run cheap:

1. a new target gets a ``catalog.json`` without records at once (with this library's
   ``created_at``), so an interrupted first mirror is recognised as one;
2. the objects: one missing (or of another size) is copied into a temporary file in the
   mirror's ``objects/.tmp``, its SHA-256 compared with its name, and only then renamed into
   place; one already there with the same name and size is kept;
3. per record its ``game.json``, then every snapshot the mirror does not have yet, assembled in
   a ``.tmp-*`` folder (files linked to the mirror's objects, or copied where the drive has no
   hard links; format-1 snapshots copied and checked against their manifest) and renamed;
4. the catalog last, atomically: this library's records with the snapshot counts the mirror
   really holds, and the mirror's own records this library no longer has.

Since 0.11.0 (D67) the mirror has the library's format: a library of format 1 makes a mirror
of format 1, and a migrated library first migrates a mirror of format 1 (the same steps as
``enginesave migrate``); a mirror of format 2 is never written from a library of format 1.
A record's folder in the mirror has the library's name for it and follows a rename there by
the record's id; where that name is taken in the mirror the folder keeps (or gets) another.

Nothing is ever deleted from the mirror (D57 2): records and snapshots this library has
dropped stay there. Afterwards the mirror is verified (every snapshot re-hashed and
``verify --objects``) unless told not to. ``should_stop`` is checked between files (D58): the
mirror is then a smaller mirror, and the next run goes on from there.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import secrets
import shutil
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enginesave import layout
from enginesave.errors import (
    EngineSaveError,
    FatalError,
    StopCheck,
    Stopped,
    UsageError,
    stop_point,
)
from enginesave.fsutil import (
    CHUNK_SIZE,
    UnsafePathError,
    atomic_write_text,
    describe_error,
    is_reparse_point,
    is_within,
    iso_utc,
    long_path,
    ns_from_iso,
    path_key,
    sha256_file,
    utc_now,
)
from enginesave.lock import LOCK_NAME
from enginesave.repo import (
    CATALOG,
    GAME_FILE,
    GAME_ID_RE,
    GAMES_DIR,
    LINKED_FORMATS,
    MANIFEST,
    OBJECT_RE,
    Repo,
    dump_json,
    folder_name_for,
    present_installs,
)
from enginesave.store import storage_kind
from enginesave.verify import (
    ObjectCheck,
    SnapshotCheck,
    verify_objects,
    verify_repo,
    verify_snapshot,
)

Progress = Callable[[int, int, str], None]
MIRRORS_FILE = "mirrors.json"  # beside gui.json: when each library was last mirrored, where


@dataclass
class MirrorResult:
    source: Path
    target: Path
    new: bool = False  # the target was missing or empty
    records: int = 0
    objects_copied: int = 0
    objects_present: int = 0
    snapshots_copied: int = 0
    snapshots_present: int = 0
    bytes_copied: int = 0
    storages: set[str] = field(default_factory=set)  # "hardlink" and/or "copy"
    failed: list[str] = field(default_factory=list)  # what could not be copied, and why
    unreadable: list[str] = field(default_factory=list)  # snapshots of the library not mirrored
    kept_records: list[str] = field(default_factory=list)  # in the mirror, not the library
    checks: list[SnapshotCheck] | None = None
    objects: ObjectCheck | None = None
    objects_recopied: int = 0  # D59 3: damaged in the mirror, copied again and checked
    failed_objects: set[str] = field(default_factory=set)  # reported once (not in to_dict)
    recopied_names: set[str] = field(default_factory=set)  # re-copied while copying (not in it)
    files_replaced: int = 0  # snapshot files that did not match their manifest, placed again
    migrated: bool = False  # 0.11.0 (D67 8): the mirror was of format 1 and was migrated first
    folders_kept: list[str] = field(default_factory=list)  # named otherwise in the mirror
    cancelled: bool = False  # stopped while copying: a smaller mirror (D58)
    verify_stopped: bool = False  # D59 2: copied completely, stopped while verifying

    @property
    def verified(self) -> bool | None:
        if self.checks is None or self.objects is None:
            return None
        return all(c.clean for c in self.checks) and self.objects.clean

    @property
    def exit_code(self) -> int:
        return 1 if (self.failed or self.unreadable or self.verified is False) else 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": str(self.source),
            "target": str(self.target),
            "new": self.new,
            "records": self.records,
            "objects_copied": self.objects_copied,
            "objects_present": self.objects_present,
            "snapshots_copied": self.snapshots_copied,
            "snapshots_present": self.snapshots_present,
            "bytes_copied": self.bytes_copied,
            "storage": "mixed" if len(self.storages) > 1 else next(iter(self.storages), None),
            "failed": list(self.failed),
            "unreadable": list(self.unreadable),
            "kept_records": list(self.kept_records),
            "verified": self.verified,
            "checks": None if self.checks is None else [c.to_dict() for c in self.checks],
            "objects": None if self.objects is None else self.objects.to_dict(),
            "objects_recopied": self.objects_recopied,  # 0.10.0 (D59)
            "files_replaced": self.files_replaced,
        }
        if self.migrated:  # 0.11.0 (D67 8): only when it happened
            data["migrated"] = True
        if self.folders_kept:  # 0.11.0 (D67 1): only when there are some
            data["folders_kept"] = list(self.folders_kept)
        if self.cancelled:  # D58: only when it was stopped
            data["cancelled"] = True
        if self.verify_stopped:  # D59 2: only when the verification was stopped
            data["verify_stopped"] = True
        return data


# --------------------------------------------------------------------------------- target


class ProfileRoot(Path):
    """A folder of the user profile a root token stands for (APPDATA, 「文档」...): the mirror
    may not be it or around it, but may be a folder inside it that no record names (0.11.0,
    D69 3: a mirror in 「文档」\\Backups is fine)."""


def forbidden_places(
    repo: Repo,
    *,
    saves_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    also: Iterable[Path] = (),
) -> list[Path]:
    """Folders a mirror may not be in or around: the Ren'Py saves root, every recorded game
    directory that is on disk, every record's save folder and extra save folder
    (``config.extra_savedirs``, recorded with an absolute root), another engine's folders in
    the user profile (0.10.0, D65 1), and ``also`` (scan roots); and as :class:`ProfileRoot`
    the profile folders the root tokens stand for (0.11.0, D69 3)."""
    from enginesave import roots
    from enginesave.cleanup import save_folder
    from enginesave.engines.renpy.paths import default_saves_root

    env = dict(os.environ) if env is None else env
    places = [default_saves_root(env, saves_root)[0], *also]
    for record in repo.load_catalog()["games"]:
        game_id = str(record.get("game_id"))
        if not GAME_ID_RE.match(game_id):
            continue
        try:
            doc = repo.load_game(game_id)
        except EngineSaveError:  # a damaged record: its folders are unknown, the rest still count
            continue
        places.extend(present_installs(doc))
        folder = save_folder(doc, saves_root_override=saves_root, env=env)
        if folder is not None:
            places.append(folder)
        places.extend(
            Path(str(t["relative_root"]))
            for t in doc.get("source_templates") or []
            if isinstance(t, dict) and t.get("root_token") == "ABSOLUTE" and t.get("relative_root")
        )
        places.extend(roots.profile_folders(doc.get("source_templates") or [], env))
    found = [Path(p) for p in places]
    for token in roots.ENGINE_PROFILE_TOKENS:
        root, _how = roots.profile_root(token, env)
        if root is not None:
            found.append(ProfileRoot(root))
    return found


def _plain(path: Path) -> Path:
    """Without a ``\\\\?\\`` prefix (``\\\\?\\UNC\\`` becomes ``\\\\``), absolute."""
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(os.path.abspath(text))


def _existing_ancestors(path: Path) -> list[Path]:
    """``path`` and its parents that exist, nearest first."""
    return [p for p in (path, *path.parents) if p.exists()]


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _overlaps(a: Path, b: Path) -> bool:
    """``a`` is ``b``, inside it or around it: by name, once links and junctions are followed,
    and by file identity (an administrative share ``\\\\host\\C$``, a ``\\\\?\\`` path or a
    junction is the same folder under another name, D57 1)."""
    a, b = _plain(a), _plain(b)
    real_a, real_b = Path(os.path.realpath(a)), Path(os.path.realpath(b))
    for x, y in ((a, b), (real_a, real_b)):
        if is_within(x, y) or is_within(y, x):
            return True
    b_up, a_up = _existing_ancestors(b), _existing_ancestors(a)
    if b.exists() and any(_same(up, b) for up in a_up):  # a lies in b
        return True
    return a.exists() and any(_same(up, a) for up in b_up)  # a holds b


def _inside(a: Path, b: Path) -> bool:
    """``a`` lies below ``b`` (not ``b`` itself), by name and once links are followed."""
    a, b = _plain(a), _plain(b)
    real_a, real_b = Path(os.path.realpath(a)), Path(os.path.realpath(b))
    return any(
        is_within(x, y) and not _same_name(x, y) for x, y in ((a, b), (real_a, real_b))
    ) and not (a.exists() and b.exists() and _same(a, b))


def _same_name(a: Path, b: Path) -> bool:
    return is_within(a, b) and is_within(b, a)


def _deep_links(target: Path) -> list[str]:
    """The links and junctions a mirror would write through below its top folders (D72 R07):
    ``objects\\.tmp``, every ``objects\\sha256\\<bucket>``, and in each record folder its
    ``snapshots`` and every snapshot folder (its ``.tmp-*`` ones too). A bucket that leads back
    into the library would make the library's own objects the mirror's."""
    found: list[str] = []
    sha = target / "objects" / "sha256"

    def check(folder: Path) -> list[Path]:
        try:
            with os.scandir(long_path(folder)) as entries:
                children = [Path(folder, e.name) for e in entries]
        except OSError:
            return []
        found.extend(str(c.relative_to(target)) for c in children if is_reparse_point(c))
        return [c for c in children if c.is_dir() and not is_reparse_point(c)]

    tmp = target / "objects" / ".tmp"
    if os.path.lexists(tmp) and is_reparse_point(tmp):
        found.append(str(tmp.relative_to(target)))
    if sha.is_dir():
        check(sha)
    games = target / GAMES_DIR
    for record in check(games) if games.is_dir() else []:
        for child in check(record):
            if child.name == "snapshots":
                check(child)
    return found


def check_target(repo: Repo, target: Path, forbidden: Sequence[Path] = ()) -> str:
    """``"new"`` (missing or empty) or ``"mirror"`` (made from this library before); anything
    else raises :class:`UsageError` and nothing is written (D57 1)."""
    if _overlaps(target, repo.root):
        raise UsageError(f"the mirror must not be the library, inside it or around it: {target}")
    for place in forbidden:
        if isinstance(place, ProfileRoot):
            if _overlaps(target, place) and not _inside(target, place):
                raise UsageError(
                    f"the mirror must not be a folder of the user profile or around one "
                    f"({place}): {target}"
                )
            continue
        if _overlaps(target, place):
            raise UsageError(
                f"the mirror must not be inside or around a game, save or scanned folder "
                f"({place}): {target}"
            )
    if target.exists() and not target.is_dir():
        raise UsageError(f"the mirror's folder is a file: {target}")
    try:
        empty = not target.is_dir() or not any(  # D73 4: a lock taken for this mirror
            p.name != LOCK_NAME for p in target.iterdir()
        )
    except OSError as exc:
        raise UsageError(f"the mirror's folder cannot be read ({describe_error(exc)})") from exc
    if empty:
        return "new"
    if not (target / CATALOG).is_file():
        raise UsageError(
            f"{target} is not empty and is not an EngineSave library; choose an empty folder"
        )
    try:
        theirs = Repo.open(target).load_catalog()
    except EngineSaveError as exc:
        raise UsageError(f"{target} holds a library that cannot be read ({exc})") from exc
    ours = repo.load_catalog()
    known = {str(r.get("game_id")) for r in ours["games"]}
    listed = {str(r.get("game_id")) for r in theirs["games"]}
    games = target / GAMES_DIR
    # A link or junction where a library has folders would lead the copying (and a repair)
    # out of the mirror, into the library itself or anywhere: never followed, refused
    links = [
        p.name
        for p in (games, target / "objects", target / "objects" / "sha256")
        if os.path.lexists(p) and is_reparse_point(p)
    ]
    if games.is_dir() and not links:
        links = [p.name for p in games.iterdir() if is_reparse_point(p)]
    if not links:  # 1.0.0 (D72 R07): deeper too -- the object buckets, the temporary folder
        links = _deep_links(target)
    if links:
        raise UsageError(
            f"{target} holds links or junctions where a library has folders "
            f"({', '.join(sorted(links)[:3])}); it is not written into"
        )
    # D59 1: only folders are records; a desktop.ini or a sync tool's file is not one. D67 1:
    # a folder is a record's by the catalog's folder name, its id as its name or the id in
    # its game.json; one that is nobody's is named as it is
    owners = {path.name: owner for owner, path in Repo(target).record_folders()}
    # M45 review: a folder of nobody, without a game.json, that has the name this library
    # gives one of its records was left by an interrupted copy of that record: it is that one's
    ours_by_folder = {repo.folder_name(g).casefold(): g for g in known if GAME_ID_RE.match(g)}

    def owner(path: Path) -> str:
        found = owners.get(path.name)
        if found is None and not (path / GAME_FILE).exists():
            found = ours_by_folder.get(path.name.casefold())
        return found or path.name

    folders = {owner(p) for p in games.iterdir() if p.is_dir()} if games.is_dir() else set()
    if (listed | folders) <= known:
        return "mirror"  # nothing in it this library does not have
    # A mirror keeps the records this library has deleted since (D57 2). It carries this
    # library's created_at and shares records with it (ids are random, never shared by two
    # libraries); two libraries made in the same second share only the time.
    lineage = bool(ours.get("created_at")) and theirs.get("created_at") == ours.get("created_at")
    odd = {f for f in folders if not GAME_ID_RE.match(f)}  # a folder nobody's
    if lineage and listed & known and not odd:
        return "mirror"
    blocking = sorted(odd if lineage and listed & known else (listed | folders) - known)
    names = ", ".join(blocking[:3]) + (", ..." if len(blocking) > 3 else "")
    raise UsageError(
        f"{target} is another library ({len(blocking)} record(s) or folder(s) under "
        f"{GAMES_DIR} this library does not have: {names}); it is not written into. Choose "
        f"an empty folder or an earlier mirror of this library"
    )


# ---------------------------------------------------------------------------------- mirror


def mirror_repo(
    repo: Repo,
    target: Path,
    *,
    forbidden: Sequence[Path] = (),
    verify: bool = True,
    repair: bool = False,
    progress: Progress | None = None,
    should_stop: StopCheck = None,
) -> MirrorResult:
    """Mirror ``repo`` into ``target`` (see the module docstring). ``repair`` (D59 3) checks
    every object the mirror already has by its SHA-256 instead of its size and copies a
    damaged one again."""
    target = Path(os.path.abspath(target))
    state = check_target(repo, target, forbidden)
    result = MirrorResult(repo.root, target, new=state == "new")
    ours = repo.load_catalog()
    old = repo.old_format  # D67 8: the mirror has the library's format
    if state == "new":
        dest = Repo.open_or_create(target, format_version=repo.format_version)
        dest.allow_old_format = old
        start = dest.load_catalog()
        start["created_at"] = ours.get("created_at", start["created_at"])
        try:
            dest.save_catalog(start)
        except OSError as exc:
            raise FatalError(
                f"cannot write the mirror's {CATALOG} ({describe_error(exc)})"
            ) from exc
    else:
        dest = Repo.open(target)
        if old and not dest.old_format:
            raise UsageError(
                f"{target} is a mirror in the layout of SaveTrail 0.11.0 and this library is "
                f'not migrated yet; migrate it first: enginesave migrate --repo "{repo.root}"'
            )
        if not old and dest.old_format:
            from enginesave.migrate import migrate_repo

            try:
                migration = migrate_repo(dest, should_stop=should_stop)
            except UsageError as exc:
                raise UsageError(
                    f"the mirror {target} has the layout of SaveTrail 0.10.0 and earlier and "
                    f"is migrated before it is written into, but it cannot be now: {exc}"
                ) from exc
            if migration.cancelled:
                result.cancelled = True
                return result
            if migration.failed:
                raise FatalError(
                    f"the mirror {target} could not be migrated to the new layout first: "
                    + "; ".join(migration.failed)
                )
            dest = Repo.open(target)
            result.migrated = True
        dest.allow_old_format = old
    objects = [(name, path) for name, path in repo.iter_objects() if _is_object(name, path)]
    manifests, unreadable = _manifests(repo, ours)
    for game_id, sid, problem in unreadable:  # one the mirror already holds is no loss
        if dest.snapshot_dir(game_id, sid).is_dir():
            result.snapshots_present += 1
        else:
            result.unreadable.append(problem)
    missing = [
        (game_id, sid)
        for game_id, sids in manifests.items()
        for sid in sids
        if not dest.snapshot_dir(game_id, sid).is_dir()
    ]
    total = len(objects) + sum(len(manifests[g][s].get("files") or []) for g, s in missing)
    done = 0

    def step(name: str) -> None:
        nonlocal done
        if should_stop is not None and should_stop():
            raise Stopped
        done += 1
        if progress is not None:
            progress(done, total, name)

    try:
        for name, path in objects:
            step(name[:12])
            _mirror_object(dest, name, path, result, repair=repair)
        if not old:  # D67 1, M45 review: renames in the library followed, swaps too
            _follow_renames(repo, dest, ours, result)
        wanted = {
            repo.folder_name(str(r["game_id"])).casefold()
            for r in ours["games"]
            if GAME_ID_RE.match(str(r.get("game_id")))
        }
        for record in ours["games"]:
            game_id = str(record["game_id"])
            if not GAME_ID_RE.match(game_id):
                continue
            made = False
            try:  # a damaged game.json must not replace the mirror's good copy
                repo.load_game(game_id)
                if not old:
                    _place_folder(repo, dest, game_id, record, result, wanted)
                made = not os.path.isdir(long_path(dest.game_dir(game_id)))
                _copy_text(repo.game_dir(game_id) / GAME_FILE, dest.game_dir(game_id) / GAME_FILE)
            except (OSError, ValueError, EngineSaveError) as exc:  # the other records still go
                name = record.get("display_name") or game_id
                text = describe_error(exc) if isinstance(exc, OSError) else str(exc)
                result.failed.append(f"{name} {GAME_FILE}: {text}")
                if made:  # M45 review: no half-made record folder is left behind
                    _remove_unfinished(dest.game_dir(game_id))
                continue
            for sid, manifest in manifests.get(game_id, {}).items():
                if (game_id, sid) not in missing:
                    result.snapshots_present += 1
                    continue
                label = f"{record.get('display_name') or game_id} {sid}"
                if _mirror_snapshot(repo, dest, game_id, sid, manifest, result, label, step):
                    result.snapshots_copied += 1
    except Stopped:
        result.cancelled = True
    finally:
        try:  # a full or unwritable drive: reported, the mirror keeps its previous catalog
            result.records = _write_catalog(dest, ours, result)
        except (OSError, EngineSaveError) as exc:
            text = describe_error(exc) if isinstance(exc, OSError) else str(exc)
            result.failed.append(f"{CATALOG}: {text}")
        layout.write_index(dest)  # D67 5: the mirror has its own
    if repair and not result.cancelled:
        # D66 2: a re-copied object is a new file; the snapshot files still linked to the
        # damaged one are placed again now, with or without the check after mirroring
        try:
            _relink(repo, dest, result, should_stop)
        except Stopped:
            result.cancelled = True
        except EngineSaveError as exc:  # the mirror's catalog cannot be read back
            result.failed.append(f"{CATALOG}: {exc}")
    if verify and not result.cancelled:
        try:
            _verify_and_repair(repo, dest, result, should_stop)
        except Stopped:  # D59 2: the mirror is complete, only its check was stopped
            result.checks, result.objects, result.verify_stopped = None, None, True
        except EngineSaveError as exc:  # the mirror's catalog cannot be read back
            result.failed.append(f"{CATALOG}: {exc}")
    return result


def _verify_and_repair(
    repo: Repo, dest: Repo, result: MirrorResult, should_stop: StopCheck
) -> None:
    """The check after mirroring (D59 3), objects first: one that does not match its name is
    copied again from the library (when the library's own is sound) and hashed again; then
    every snapshot, and a file that does not match its manifest (or is missing) is placed
    again, its snapshot checked again. Only the mirror is written, and only a file found
    wrong, with content checked before it replaces it."""
    objects = verify_objects(dest, should_stop=should_stop)
    for entry in list(objects.mismatched):
        stop_point(should_stop)
        name = entry.split(" ", 1)[0]
        if name in result.failed_objects:  # the copying already said why (--repair)
            continue
        if _recopy_object(dest, name, repo.object_path(name), result):
            objects.mismatched.remove(entry)
            objects.ok += 1
            result.objects_recopied += 1
    checks = verify_repo(dest, dest.load_catalog(), should_stop=should_stop)
    for index, check in enumerate(checks):
        if not (check.mismatched or check.missing):
            continue
        stop_point(should_stop)
        replaced = _replace_files(repo, dest, check.game_id, check.snapshot_id, result)
        if replaced:
            result.files_replaced += replaced
            checks[index] = verify_snapshot(
                dest, check.game_id, check.snapshot_id, check.display_name
            )
    result.checks, result.objects = checks, objects


def _relink(repo: Repo, dest: Repo, result: MirrorResult, should_stop: StopCheck) -> None:
    """With ``--repair`` (D66 2, M44 review): every snapshot file of the mirror that should be
    a hard link to its object and is not -- the object was copied again by this run, by an
    interrupted one or by 0.10.0's repair -- is placed again, in the mirror's own records and
    snapshots too (those the library has dropped are kept there, D57 2)."""
    for record in dest.load_catalog()["games"]:
        game_id = str(record.get("game_id"))
        if not GAME_ID_RE.match(game_id):
            continue
        for sid in dest.snapshot_ids(game_id):
            stop_point(should_stop)
            detached = _detached(dest, game_id, sid)
            if detached:
                result.files_replaced += _replace_files(
                    repo, dest, game_id, sid, result, only=detached
                )


def _detached(dest: Repo, game_id: str, sid: str) -> set[str]:
    """The objects whose files in a linked snapshot are missing or not that object."""
    try:
        manifest = dest.load_manifest(game_id, sid)
    except FatalError:
        return set()
    if manifest.get("format_version") not in LINKED_FORMATS or manifest.get("storage") == "copy":
        return set()  # copies are checked by the verification, not here
    snapshot = dest.snapshot_dir(game_id, sid)
    found: set[str] = set()
    for item in manifest.get("files") or []:
        try:
            digest = str(item["sha256"])
            obj = dest.object_path(digest)
            target = layout.snapshot_file_path(snapshot, manifest, item)
            if not os.path.isfile(long_path(obj)):
                continue  # nothing to link to: the verification reports it
            if not os.path.isfile(long_path(target)) or not os.path.samefile(
                long_path(target), long_path(obj)
            ):
                found.add(digest)
        except (OSError, KeyError, TypeError, ValueError, UnsafePathError, FatalError):
            continue
    return found


def _place_folder(
    repo: Repo,
    dest: Repo,
    game_id: str,
    record: Mapping[str, Any],
    result: MirrorResult,
    wanted: Collection[str] = (),
) -> None:
    """D67 1: the record's folder in the mirror gets the library's name for it, renamed when
    the library's was (found by the id); where the name is taken in the mirror by another
    folder, the record keeps the folder it has there, or a new one gets a free name that no
    record of the library has (``wanted``). An empty folder of that name left by an
    interrupted copy is taken over."""
    want = repo.folder_name(game_id)
    games = dest.root / GAMES_DIR
    have = dest.folder_name(game_id)
    present = os.path.isdir(long_path(games / have))
    if present and have == want:
        dest.set_folder(game_id, have)
        return
    taken = dest.taken_folder_names(but=game_id if present else None)
    if want.casefold() in taken and not present and _unfinished(games / want, dest):
        taken.discard(want.casefold())  # M45 review: left by an interrupted copy of this one
    if want.casefold() in taken:
        if not present:
            have = folder_name_for(
                record.get("display_name"), None, game_id, taken | {want} | set(wanted)
            )
        dest.set_folder(game_id, have)
        result.folders_kept.append(f"{record.get('display_name') or game_id}: {have}")
        return
    if present:  # renamed in the library since the last mirror
        os.rename(long_path(games / have), long_path(games / want))
    dest.set_folder(game_id, want)


def _unfinished(folder: Path, dest: Repo) -> bool:
    """A folder of no record of the mirror holding nothing but an interrupted ``game.json``
    write (or nothing at all)."""
    if not os.path.isdir(long_path(folder)) or is_reparse_point(folder):
        return False
    if folder.name.casefold() in {dest.folder_name(g).casefold() for g in dest_ids(dest)}:
        return False  # a record's of the mirror
    try:
        entries = list(os.scandir(long_path(folder)))
    except OSError:
        return False
    return all(e.is_file() and e.name.startswith(f".{GAME_FILE}.tmp-") for e in entries)


def dest_ids(dest: Repo) -> list[str]:
    return [
        str(r.get("game_id"))
        for r in dest.load_catalog()["games"]
        if isinstance(r, dict) and GAME_ID_RE.match(str(r.get("game_id")))
    ]


def _remove_unfinished(folder: Path) -> None:
    """Remove a record folder the mirror has just made whose ``game.json`` did not arrive: its
    ``.game.json.tmp-*`` files, then the folder (only when it is empty)."""
    with contextlib.suppress(OSError):
        for entry in os.scandir(long_path(folder)):
            if entry.is_file() and entry.name.startswith(f".{GAME_FILE}.tmp-"):
                os.unlink(entry.path)
        os.rmdir(long_path(folder))


def _follow_renames(repo: Repo, dest: Repo, ours: Mapping[str, Any], result: MirrorResult) -> None:
    """D67 1 (M45 review): before the records are copied, the mirror's folders of records the
    library has renamed get the library's names -- in any order the names allow, and two
    records that swapped names through a temporary name. A name held by a folder the mirror
    keeps for a record the library deleted stays (``_place_folder`` says so)."""
    games = dest.root / GAMES_DIR
    moves: dict[str, tuple[str, str]] = {}
    for record in ours["games"]:
        game_id = str(record.get("game_id"))
        if not GAME_ID_RE.match(game_id) or not dest.has_game(game_id):
            continue
        have, want = dest.folder_name(game_id), repo.folder_name(game_id)
        if have != want:
            moves[game_id] = (have, want)
    while moves:
        progress = False
        for game_id, (have, want) in list(moves.items()):
            taken = dest.taken_folder_names(but=game_id)
            if want.casefold() in taken:
                continue
            os.rename(long_path(games / have), long_path(games / want))
            dest.set_folder(game_id, want)
            del moves[game_id]
            progress = True
        if progress:
            continue
        held = {have.casefold(): g for g, (have, _want) in moves.items()}
        cycle = next((g for g, (_have, want) in moves.items() if want.casefold() in held), None)
        if cycle is None:
            break  # held by folders the mirror keeps
        have, want = moves[cycle]
        spare = f".rename-{secrets.token_hex(4)}"
        os.rename(long_path(games / have), long_path(games / spare))
        dest.set_folder(cycle, spare)
        moves[cycle] = (spare, want)


def _is_object(name: str, path: Path) -> bool:
    return bool(OBJECT_RE.match(name)) and path.parent.name == name[:2]


def _manifests(
    repo: Repo, catalog: Mapping[str, Any]
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[tuple[str, str, str]]]:
    """Every readable manifest of the catalog's records, and the ones that are not (record,
    snapshot, why)."""
    found: dict[str, dict[str, dict[str, Any]]] = {}
    unreadable: list[tuple[str, str, str]] = []
    for record in catalog["games"]:
        game_id = str(record.get("game_id"))
        if not GAME_ID_RE.match(game_id):
            continue
        found[game_id] = {}
        for sid in repo.snapshot_ids(game_id):
            try:
                manifest = repo.load_manifest(game_id, sid)
                if not isinstance(manifest.get("files"), list):
                    raise FatalError(f"{MANIFEST} has no file list")
            except FatalError as exc:
                unreadable.append((game_id, sid, f"{game_id}/{sid}: {exc}"))
                continue
            found[game_id][sid] = manifest
    return found, unreadable


def _mirror_object(
    dest: Repo, name: str, path: Path, result: MirrorResult, *, repair: bool = False
) -> None:
    """Copy one object unless the mirror has it (same name and size; with ``repair`` also the
    same SHA-256, D59 3); a copy is checked by its hash before it is renamed into place."""
    target = dest.object_path(name)
    try:
        size = os.stat(long_path(path)).st_size
        damaged = False
        with contextlib.suppress(FileNotFoundError):
            if os.stat(long_path(target)).st_size == size:
                if not repair or sha256_file(target)[0] == name:
                    result.objects_present += 1
                    return
                damaged = True
        _copy_object(dest, name, path, size, target)
    except OSError as exc:
        result.failed.append(f"object {name}: {describe_error(exc)}")
        result.failed_objects.add(name)
        return
    if damaged:
        result.objects_recopied += 1
        result.recopied_names.add(name)
    else:
        result.objects_copied += 1
    result.bytes_copied += size


def _copy_object(dest: Repo, name: str, source: Path, size: int, target: Path) -> None:
    """A temporary file in the mirror's ``objects/.tmp``, its SHA-256 compared with the name,
    then renamed over ``target`` (a new file: hard links to a damaged one keep the old)."""
    tmp_dir = dest.object_tmp_dir()
    os.makedirs(long_path(tmp_dir), exist_ok=True)
    tmp = tmp_dir / secrets.token_hex(8)
    try:
        digest, copied = _copy_hashing(source, tmp)
        if digest != name or copied != size:
            raise OSError(f"the library's object does not match its name ({digest[:12]})")
        os.makedirs(long_path(target.parent), exist_ok=True)
        os.replace(long_path(tmp), long_path(target))
    finally:
        with contextlib.suppress(OSError):
            os.unlink(long_path(tmp))


def _recopy_object(dest: Repo, name: str, source: Path, result: MirrorResult) -> bool:
    """An object the check found damaged in the mirror, copied again and hashed again."""
    target = dest.object_path(name)
    try:
        if not OBJECT_RE.match(name):
            raise OSError("not an object name")
        size = os.stat(long_path(source)).st_size
        _copy_object(dest, name, source, size, target)
        if sha256_file(target) != (name, size):
            raise OSError("still does not match its name after copying it again")
    except OSError as exc:
        result.failed.append(f"object {name}: damaged in the mirror; {describe_error(exc)}")
        return False
    result.bytes_copied += size
    return True


def _replace_files(
    repo: Repo,
    dest: Repo,
    game_id: str,
    sid: str,
    result: MirrorResult,
    only: Collection[str] | None = None,
) -> int:
    """Place again the files of a mirrored snapshot that are missing or do not match its
    manifest (with ``only``, the files of those objects): linked to the mirror's object when
    that is sound, otherwise copied from the library's snapshot; each written in the mirror's
    ``objects/.tmp`` (where an interrupted run's leftovers are found and cleaned up, D55),
    checked, then renamed into place. Returns how many were placed."""
    try:
        manifest = dest.load_manifest(game_id, sid)
    except FatalError:
        return 0
    linked = manifest.get("format_version") in LINKED_FORMATS
    snapshot = dest.snapshot_dir(game_id, sid)
    source_snapshot = repo.snapshot_dir(game_id, sid)
    placed = 0
    for item in manifest.get("files") or []:
        if only is not None and str(item.get("sha256")) not in only:
            continue
        tmp: Path | None = None
        try:
            target = layout.snapshot_file_path(snapshot, manifest, item)  # D67 3
            digest, size = str(item["sha256"]), int(item["size"])
            if os.path.isfile(long_path(target)) and sha256_file(target) == (digest, size):
                continue
            os.makedirs(long_path(target.parent), exist_ok=True)
            os.makedirs(long_path(dest.object_tmp_dir()), exist_ok=True)
            tmp = dest.object_tmp_dir() / f"repair-{secrets.token_hex(8)}"
            obj = dest.object_path(digest) if OBJECT_RE.match(digest) else None
            if linked and obj is not None and os.path.isfile(long_path(obj)):
                if sha256_file(obj) != (digest, size):
                    raise OSError("the mirror's object is damaged too")
                _place(obj, tmp, digest, size)
            else:
                source = layout.snapshot_file_path(source_snapshot, manifest, item)
                if _copy_hashing(source, tmp) != (digest, size):
                    raise OSError("the library's copy does not match the manifest either")
            os.replace(long_path(tmp), long_path(target))
            tmp = None
            placed += 1
        except (OSError, KeyError, TypeError, ValueError, UnsafePathError) as exc:
            text = describe_error(exc) if isinstance(exc, OSError) else str(exc)
            where = f"{item.get('source_id')}/{item.get('path')}"
            result.failed.append(f"{game_id}/{sid} {where}: {text}")
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(long_path(tmp))
    return placed


def _copy_hashing(source: Path, target: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(long_path(source), "rb") as src, open(long_path(target), "xb") as out:
        while chunk := src.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    return digest.hexdigest(), size


def _copy_text(source: Path, target: Path) -> None:
    """A metadata file, byte for byte but written atomically."""
    with open(long_path(source), encoding="utf-8") as f:
        atomic_write_text(target, f.read())


def _mirror_snapshot(
    repo: Repo,
    dest: Repo,
    game_id: str,
    sid: str,
    manifest: Mapping[str, Any],
    result: MirrorResult,
    label: str,
    step: Callable[[str], None],
) -> bool:
    """Assemble one snapshot in a ``.tmp-*`` folder of the mirror and rename it; False (and
    nothing left behind) when a file cannot be placed. ``Stopped``, a second Ctrl+C or any
    other interruption also discards it."""
    linked = manifest.get("format_version") in LINKED_FORMATS
    source_snapshot = repo.snapshot_dir(game_id, sid)
    placed: set[str] = set()
    tmp: Path | None = None
    try:
        os.makedirs(long_path(dest.snapshots_dir(game_id)), exist_ok=True)
        tmp = dest.new_tmp_dir(game_id, sid)
        for item in manifest.get("files") or []:
            step(label)
            target = layout.snapshot_file_path(tmp, manifest, item)  # D67 3
            os.makedirs(long_path(target.parent), exist_ok=True)
            digest, size = str(item["sha256"]), int(item["size"])
            obj = dest.object_path(digest) if OBJECT_RE.match(digest) else None
            if linked and obj is not None and os.path.isfile(long_path(obj)):
                kind = _place(obj, target, digest, size)
            else:  # format 1 (or an object the library lacks): the snapshot's own file
                source = layout.snapshot_file_path(source_snapshot, manifest, item)
                if _copy_hashing(source, target) != (digest, size):
                    raise OSError(f"{item['path']} does not match the manifest")
                kind = "copy"
            placed.add(kind)
            if kind == "copy":
                result.bytes_copied += size
        _copy_manifest(
            repo.snapshot_dir(game_id, sid) / MANIFEST,
            tmp / MANIFEST,
            manifest,
            placed if linked else None,
        )
        readme = source_snapshot / layout.README  # D67 4: the mirror has it too
        if os.path.isfile(long_path(readme)):
            shutil.copyfile(long_path(readme), long_path(tmp / layout.README))
        os.replace(long_path(tmp), long_path(dest.snapshot_dir(game_id, sid)))
    except (OSError, KeyError, TypeError, ValueError, UnsafePathError, FatalError) as exc:
        if tmp is not None:
            dest.discard_tmp(tmp)
        result.failed.append(f"{label}: {describe_error(exc) if isinstance(exc, OSError) else exc}")
        return False
    except BaseException:
        if tmp is not None:
            dest.discard_tmp(tmp)
        raise
    result.storages |= placed
    return True


def _place(obj: Path, target: Path, digest: str, size: int) -> str:
    """A hard link to the mirror's object, or a copy (checked against the name) where the
    drive has none. Never over a file already there: a copy over a hard link would write into
    the object."""
    try:
        os.link(long_path(obj), long_path(target))
        return "hardlink"
    except FileExistsError:
        raise
    except OSError:
        if _copy_hashing(obj, target) != (digest, size):
            raise OSError(f"the mirror's object {digest[:12]} does not match its name") from None
        return "copy"


def _copy_manifest(
    source: Path, target: Path, manifest: Mapping[str, Any], placed: set[str] | None
) -> None:
    """The library's manifest byte for byte, except that a format-2 one says how the mirror
    really holds the files: ``storage`` differs where the mirror's drive has no hard links."""
    if placed is not None and manifest.get("storage") != storage_kind(placed):
        atomic_write_text(target, dump_json({**manifest, "storage": storage_kind(placed)}))
    else:
        shutil.copyfile(long_path(source), long_path(target))


def _write_catalog(dest: Repo, ours: Mapping[str, Any], result: MirrorResult) -> int:
    """The mirror's catalog, written last: this library's records (those whose ``game.json``
    reached the mirror) with the snapshots the mirror holds, then the mirror's own records
    this library does not have (kept, D57 2). Returns how many records it lists."""
    placed = {  # M45 review: before the catalog is read again (it forgets a case-only rename)
        str(r.get("game_id")): dest.folder_name(str(r.get("game_id")))
        for r in ours["games"]
        if GAME_ID_RE.match(str(r.get("game_id")))
    }
    theirs = dest.load_catalog()
    records = []
    for record in ours["games"]:
        game_id = str(record.get("game_id"))
        if not GAME_ID_RE.match(game_id):
            continue
        if not dest.old_format:
            dest.set_folder(game_id, placed[game_id])
        if not dest.has_game(game_id):
            continue
        entry = copy.deepcopy(dict(record))
        ids = dest.snapshot_ids(game_id)
        entry["snapshot_count"] = len(ids)
        entry["latest_snapshot"] = ids[-1] if ids else None
        if dest.old_format:
            entry.pop("folder", None)
        else:  # D67 1: the folder the record has in the mirror
            entry["folder"] = placed[game_id]
        records.append(entry)
    known = {r["game_id"] for r in records}
    kept = [r for r in theirs["games"] if r.get("game_id") not in known]
    result.kept_records = [str(r.get("display_name") or r.get("game_id")) for r in kept]
    catalog = copy.deepcopy(dict(ours))
    catalog["format_version"] = dest.format_version  # D67 8: the library's, checked above
    catalog["games"] = records + kept
    ours_overrides = list(ours.get("overrides", []))
    catalog["overrides"] = ours_overrides + [
        o for o in theirs.get("overrides", []) if o not in ours_overrides
    ]
    dest.save_catalog(catalog)
    return len(catalog["games"])


# ------------------------------------------------------------------- when, where (D56, D57)


def record_mirror(config: Path, source: Path, target: Path, when: str | None = None) -> None:
    """Remember the mirror in ``mirrors.json`` beside ``gui.json`` (shared with the command
    line, like ``scan-cache.json``; never in the library, whose format stays as it is). An
    unwritable file is only a lost note."""
    path = config / MIRRORS_FILE
    data = _mirrors(path)
    data[path_key(source)] = {"to": str(target), "at": when or iso_utc(utc_now())}
    with contextlib.suppress(OSError):
        _write(path, dump_json({"format": 1, "mirrors": data}))


def last_mirror(config: Path, source: Path) -> dict[str, str] | None:
    """``{"to": folder, "at": time}`` of this library's last mirror, or ``None`` (also for an
    entry someone edited into another shape)."""
    entry = _mirrors(config / MIRRORS_FILE).get(path_key(source))
    if not isinstance(entry, dict):
        return None
    at, to = entry.get("at"), entry.get("to")
    if not isinstance(at, str) or not isinstance(to, str):
        return None
    try:
        ns_from_iso(at)
    except ValueError:
        return None
    return {"to": to, "at": at}


SETTINGS_FILE = "gui.json"  # the window's settings, beside mirrors.json


def mirror_settings(config: Path) -> tuple[bool, bool]:
    """``(auto_mirror, mirror_verify)`` as the window's settings say (both on by default, as
    in the window; a damaged file or value counts as the default). Read only."""
    try:
        with open(long_path(config / SETTINGS_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):
        data = {}
    data = data if isinstance(data, dict) else {}
    auto, verify = data.get("auto_mirror", True), data.get("mirror_verify", True)
    return (auto if isinstance(auto, bool) else True, verify if isinstance(verify, bool) else True)


def mirror_after_backup(
    repo: Repo,
    config: Path,
    *,
    wanted: str,
    backed_up: str | None,
    forbidden: Sequence[Path] = (),
    progress: Progress | None = None,
    should_stop: StopCheck = None,
    command: str = "enginesave backup --mirror",
) -> dict[str, Any]:
    """1.2.0 (D73 16): after a backup, mirror the library to the folder of its last mirror
    that worked (``mirrors.json``), as the window does after a backup (D64).

    ``wanted`` is ``"mirror"`` (always) or ``"auto"`` (when the window's 「自动镜像」 setting
    is on); ``backed_up`` is ``None`` when the backup wrote something worth mirroring, else
    why not. Returns ``{"folder", "status": "ok" | "skipped" | "failed", "finished_at",
    "message"}``; nothing raised -- a mirror that fails never changes the backup's outcome."""
    from enginesave.lock import library_lock

    last = last_mirror(config, repo.root)
    folder = last["to"] if last else None
    outcome: dict[str, Any] = {
        "folder": folder,
        "status": "skipped",
        "finished_at": None,
        "message": "",
    }
    auto, verify = mirror_settings(config)
    if wanted == "auto" and not auto:
        outcome["message"] = "automatic mirroring is turned off in the window's settings"
        return outcome
    if last is None or folder is None:
        outcome["message"] = "this library has not been mirrored successfully before"
        return outcome
    if backed_up is not None:
        outcome["message"] = backed_up
        return outcome
    target = Path(folder)
    outcome["status"] = "failed"
    if not target.is_dir():  # never created here: a drive or a NAS not there now
        outcome["message"] = f"the mirror folder is not there: {target}"
        return outcome
    try:
        with library_lock(target, command):
            result = mirror_repo(
                repo,
                target,
                forbidden=forbidden,
                verify=verify,
                progress=progress,
                should_stop=should_stop,
            )
    except EngineSaveError as exc:
        outcome["message"] = str(exc)
        return outcome
    except OSError as exc:
        outcome["message"] = describe_error(exc)
        return outcome
    if result.cancelled or result.verify_stopped:
        outcome["message"] = "stopped at a safe point; the next mirror goes on from there"
        return outcome
    if result.exit_code != 0:
        problems = [*result.failed, *result.unreadable]
        outcome["message"] = "; ".join(problems[:3]) or "the mirror did not pass its verification"
        return outcome
    record_mirror(config, repo.root, result.target)
    outcome["status"] = "ok"
    outcome["finished_at"] = iso_utc(utc_now())
    copied = result.snapshots_copied
    outcome["message"] = f"{copied} new snapshot(s) copied" + (
        ", verified" if result.verified else ""
    )
    return outcome


def _mirrors(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_read(path))
    except (OSError, ValueError, RecursionError):
        return {}
    mirrors = data.get("mirrors") if isinstance(data, dict) else None
    return dict(mirrors) if isinstance(mirrors, dict) else {}


def _read(path: Path) -> str:  # the only read of mirrors.json (tests guard it)
    with open(long_path(path), encoding="utf-8") as f:
        return f.read()


def _write(path: Path, text: str) -> None:  # the only write of mirrors.json (tests guard it)
    atomic_write_text(path, text)
