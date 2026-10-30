"""Importing one exported zip: read and check it, find the record it belongs to (D31), then
write the snapshot."""

from __future__ import annotations

import json
import os
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from enginesave import __version__
from enginesave.backup import source_template, update_catalog_entry
from enginesave.errors import EngineSaveError, FatalError, UsageError, VerificationError
from enginesave.export.archive import (
    EXPORT_FORMATS,
    EXPORT_JSON,
    IDENTITY_KEYS,
    LEGACY_EXPORT_JSON,
    MAX_HEADER_BYTES,
    member_name,
)
from enginesave.fsutil import UnsafePathError, describe_error, iso_utc, long_path, utc_now
from enginesave.identity import install_roots, match_key, match_record, match_unlinked
from enginesave.layout import (
    checked_source_id,
    readable_sources,
    snapshot_file_path,
    snapshot_readme,
    source_data_dir,
    write_index,
)
from enginesave.models import GameIdentity, SourceKind
from enginesave.repo import (
    DATA_DIR,
    FORMAT_VERSION,
    GAME_ID_RE,
    MANIFEST_FORMAT,
    MANIFEST_FORMATS,
    OBJECT_RE,
    SNAPSHOT_ID_RE,
    Repo,
    destination_state,
    record_version,
)
from enginesave.restore import validate_manifest
from enginesave.store import SourceReadError, VerifyMismatch, storage_kind, store_stream

# ------------------------------------------------------------------------------------ import

_SOURCE_FIELDS: dict[str, type] = {
    "origin_path": str,
    "file_count": int,
    "total_bytes": int,
    "consistency": str,
}
_FILE_FIELDS: dict[str, type] = {"source_id": str, "path": str, "sha256": str, "size": int}
_ERROR_FIELDS: dict[str, type] = {"source_id": str, "path": str, "error": str}


ImportKey = tuple[str, str]  # (original record id, original snapshot id) of an exported snapshot
ALREADY_IMPORTED = "already imported"


@dataclass
class ImportPlan:
    repo_root: Path
    zip_path: Path
    header: dict[str, Any]
    game_id: str
    matched_by: str | None  # "game_id", "identity", or None for a new record
    ignored_members: int = 0
    # D31: the import snapshot that already holds this exported snapshot, if any
    already_imported: dict[str, str] | None = None
    # 0.6.0 (S-098): the earlier archive of the same folder that holds the same exported
    # snapshot; importing that one makes this one already imported, so it is skipped too
    duplicate_of: str | None = None

    @property
    def source_key(self) -> ImportKey | None:
        """The exported snapshot's (original record id, original snapshot id)."""
        game_id = self.header["game"].get("game_id")
        if not isinstance(game_id, str) or not game_id:
            return None
        return game_id, str(self.header["snapshot_id"])

    @property
    def manifest(self) -> dict[str, Any]:
        return self.header["manifest"]

    @property
    def display_name(self) -> str:
        return str(self.header["game"]["display_name"])

    @property
    def existing_record(self) -> bool:
        return self.matched_by is not None

    @property
    def includes_tokens(self) -> bool:
        return any(s["kind"] == SourceKind.MACHINE_TOKENS.value for s in self.manifest["sources"])

    def to_dict(self) -> dict[str, Any]:
        files = self.manifest["files"]
        return {
            "repository": str(self.repo_root),
            "archive": str(self.zip_path),
            "display_name": self.display_name,
            "game_id": self.game_id,
            "matched_by": self.matched_by,
            "source_snapshot_id": self.header["snapshot_id"],
            "source_status": self.manifest["status"],
            "exported_at": self.header.get("exported_at"),
            "file_count": len(files),
            "total_bytes": sum(f["size"] for f in files),
            "includes_tokens": self.includes_tokens,
            "ignored_members": self.ignored_members,
            "already_imported": self.already_imported,
            "duplicate_of": self.duplicate_of,
        }


@dataclass
class ImportResult:
    game_id: str
    display_name: str
    snapshot_id: str
    created_record: bool
    file_count: int
    total_bytes: int
    storage: str

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _check_entries(manifest: dict[str, Any], key: str, fields: dict[str, type]) -> None:
    entries = manifest.get(key, [])
    if not isinstance(entries, list):
        raise ValueError(f"manifest {key} is not a list")
    for entry in entries:
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(name), kind) or isinstance(entry.get(name), bool)
            for name, kind in fields.items()
        ):
            raise ValueError(f"a manifest {key} entry is incomplete")


def _check_header(header: Any) -> None:
    """Structure checks on top of :func:`validate_manifest`; raises ValueError."""
    if not isinstance(header, dict):
        raise ValueError("the description is not a JSON object")
    version = header.get("export_format_version")
    if version not in EXPORT_FORMATS:
        raise ValueError(f"unsupported export format {version!r}")
    snapshot_id = header.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_RE.match(snapshot_id):
        raise ValueError("invalid snapshot id")
    game, manifest = header.get("game"), header.get("manifest")
    if not isinstance(game, dict) or not isinstance(manifest, dict):
        raise ValueError("the game record or the manifest is missing")
    identity = game.get("identity", {})
    installs = game.get("installs", [])
    if (
        not _text(game.get("display_name"))
        or game.get("kind", "game") not in ("game", "unlinked")
        or not isinstance(identity, dict)
        or any(v is not None and not isinstance(v, str) for v in identity.values())
        or not isinstance(installs, list)
        or any(not isinstance(i, dict) or not _text(i.get("game_root")) for i in installs)
    ):
        raise ValueError("the game record is malformed")
    if manifest.get("format_version") not in MANIFEST_FORMATS:
        raise ValueError(f"unsupported manifest format {manifest.get('format_version')!r}")
    if manifest.get("status") not in ("complete", "partial"):
        raise ValueError("the manifest status is invalid")
    _check_entries(manifest, "sources", _SOURCE_FIELDS)
    _check_entries(manifest, "files", _FILE_FIELDS)
    _check_entries(manifest, "errors", _ERROR_FIELDS)
    ids = [s.get("source_id") for s in manifest.get("sources", [])]
    for source_id in ids:  # D72 R08 R10: before they are compared or laid out
        try:
            checked_source_id(source_id)
        except UnsafePathError as exc:
            raise ValueError(str(exc)) from exc
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source ids")
    seen: set[tuple[str, str]] = set()
    for item in manifest.get("files", []):
        if not OBJECT_RE.match(item["sha256"]) or item["size"] < 0:
            raise ValueError(f"invalid hash or size for {item['path']!r}")
        key = (item["source_id"], os.path.normcase(item["path"].replace("\\", "/")))
        if key in seen:
            raise ValueError(f"duplicate file {item['path']!r}")
        seen.add(key)


def read_export(zip_path: Path) -> tuple[dict[str, Any], int]:
    """Validated ``EngineSave-export.json`` and the number of unlisted ``data/`` members.

    Anything malformed or path-unsafe raises FatalError (exit 3), as for restore (9.2).
    """
    try:
        with zipfile.ZipFile(long_path(zip_path)) as zf:
            names = set(zf.namelist())
            # an export of SaveTrail (EngineSave's name up to 1.0.0) has the same header
            found = next((n for n in (EXPORT_JSON, LEGACY_EXPORT_JSON) if n in names), None)
            if found is None:
                raise FatalError(f"{zip_path} is not an EngineSave export")
            info = zf.getinfo(found)
            if info.file_size > MAX_HEADER_BYTES:
                raise FatalError(f"{found} in {zip_path} is unreasonably large")
            header = json.loads(zf.read(info))
    except (OSError, ValueError, RecursionError, zipfile.BadZipFile) as exc:
        raise FatalError(f"cannot read {zip_path}: {describe_error(exc)}") from exc
    try:
        _check_header(header)
    except ValueError as exc:
        raise FatalError(f"{zip_path}: {exc}") from exc
    manifest = header["manifest"]
    for key in ("sources", "files", "errors"):
        manifest.setdefault(key, [])
    validate_manifest(manifest)
    try:  # D67 6: the files below their sources' folders (export format 2) or data/<source>
        wanted = {member_name(f, manifest) for f in manifest["files"]}
    except (UnsafePathError, KeyError) as exc:
        raise FatalError(f"{zip_path}: {exc}") from exc
    missing = sorted(wanted - names)
    if missing:
        raise FatalError(f"{zip_path}: {len(missing)} data files are missing, e.g. {missing[0]}")
    extra = sum(1 for n in names - wanted if n.startswith(f"{DATA_DIR}/") and not n.endswith("/"))
    return header, extra


def _match_record(
    game: dict[str, Any], known: Sequence[dict[str, Any]], overrides: Sequence[dict[str, Any]]
) -> str | None:
    """6.6 for an imported record; ``unlinked`` ones follow the orphaned-folder rule (S-021)."""
    identity = game.get("identity") or {}
    save_directory = _text(identity.get("renpy_save_directory"))
    if game.get("kind") == "unlinked":
        return match_unlinked(save_directory, known) if save_directory else None
    key = match_key(_text(game.get("engine")), identity)  # D65 3: by engine
    engine = _text(game.get("engine")) or "renpy"
    return match_record(key, install_roots(game), known, overrides, engine=engine)


def imported_snapshots(repo: Repo) -> dict[ImportKey, dict[str, str]]:
    """Where exported snapshots already went (D31): ``(original record id, original snapshot
    id)`` → ``{"game_id", "snapshot_id"}`` of the ``import`` snapshot holding it. Only the IDs
    in ``imported_from`` count, not the content (equal content is deduplicated by the object
    store anyway); other snapshots and unreadable manifests are left out."""
    found: dict[ImportKey, dict[str, str]] = {}
    for record in repo.load_catalog()["games"]:
        game_id = record["game_id"]
        for snapshot_id in repo.snapshot_ids(game_id):
            try:
                manifest = repo.load_manifest(game_id, snapshot_id)
            except EngineSaveError:
                continue
            origin = manifest.get("imported_from")
            if manifest.get("reason") != "import" or not isinstance(origin, dict):
                continue
            key = (origin.get("game_id"), origin.get("snapshot_id"))
            if all(isinstance(part, str) and part for part in key):
                found.setdefault(key, {"game_id": game_id, "snapshot_id": snapshot_id})
    return found


def known_imports(repo_root: Path) -> dict[ImportKey, dict[str, str]]:
    """``imported_snapshots`` of the library at ``repo_root`` (empty for a new library)."""
    repo_root = Path(os.path.abspath(repo_root))
    if destination_state(repo_root) != "existing":
        return {}
    return imported_snapshots(Repo.open(repo_root))


def plan_import(
    repo_root: Path,
    zip_path: Path,
    *,
    imported: Mapping[ImportKey, dict[str, str]] | None = None,
) -> ImportPlan:
    """Validate the archive and decide which record receives it. Nothing is written.

    ``already_imported`` names the import snapshot that already holds the same exported
    snapshot; ``imported`` (from :func:`known_imports`) saves reading it again per archive.
    """
    repo_root = Path(os.path.abspath(repo_root))
    zip_path = Path(os.path.abspath(zip_path))
    if not zip_path.is_file():
        raise UsageError(f"--from is not an existing file: {zip_path}")
    state = destination_state(repo_root)
    header, extra = read_export(zip_path)
    game = header["game"]
    known: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    if state == "existing":
        repo = Repo.open(repo_root)
        catalog = repo.load_catalog()
        known, overrides = repo.known_games(catalog), catalog["overrides"]
        if imported is None:
            imported = imported_snapshots(repo)
    zip_id = game.get("game_id")
    if any(r["game_id"] == zip_id for r in known):
        plan = ImportPlan(repo_root, zip_path, header, str(zip_id), "game_id", extra)
    elif (matched := _match_record(game, known, overrides)) is not None:
        plan = ImportPlan(repo_root, zip_path, header, matched, "identity", extra)
    else:
        fresh = (
            zip_id if isinstance(zip_id, str) and GAME_ID_RE.match(zip_id) else str(uuid.uuid4())
        )
        plan = ImportPlan(repo_root, zip_path, header, fresh, None, extra)
    key = plan.source_key
    if key is not None and imported:
        plan.already_imported = imported.get(key)
    return plan


def _import_member(
    repo: Repo,
    zf: zipfile.ZipFile,
    item: dict[str, Any],
    target: Path,
    manifest: Mapping[str, Any],
) -> str:
    name = member_name(item, manifest)
    try:
        member = zf.open(name)
    except (KeyError, OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        raise SourceReadError(f"{name}: {describe_error(exc)}") from exc
    with member:
        try:
            return store_stream(repo, member, target, item["sha256"], item["size"])
        except VerifyMismatch as exc:
            raise VerifyMismatch(f"{name}: {exc}") from exc
        except SourceReadError as exc:
            raise SourceReadError(f"{name}: {exc}") from exc


def _new_doc(plan: ImportPlan, stamp: str) -> dict[str, Any]:
    game = plan.header["game"]
    identity = game.get("identity") or {}
    installs = [  # only real folders: a bare name would be looked up in the current directory
        {
            "game_root": i["game_root"],
            "config_version": _text(i.get("config_version")),
            "first_seen": _text(i.get("first_seen")),
            "last_seen": _text(i.get("last_seen")),
        }
        for i in game.get("installs", [])
        if Path(i["game_root"]).is_absolute()
    ]
    return {
        "format_version": FORMAT_VERSION,
        "game_id": plan.game_id,
        "kind": game.get("kind", "game"),
        "engine": _text(game.get("engine")) or "renpy",
        "engine_version": _text(game.get("engine_version")),
        "display_name": plan.display_name,
        "identity": {  # 0.10.0's optional keys (another engine's) only when they are set
            key: _text(identity.get(key))
            for key in IDENTITY_KEYS
            if key not in GameIdentity.OPTIONAL_FIELDS or _text(identity.get(key))
        },
        "installs": installs,
        "source_templates": [],
        "created_at": stamp,
        "updated_at": stamp,
    }


def execute_import(plan: ImportPlan) -> ImportResult:
    """Store the archive as a new ``reason="import"`` snapshot (object store, format 2).

    A file that does not match its manifest hash aborts the whole import; nothing is committed
    (VerificationError, exit 1).
    """
    repo = Repo.open_or_create(plan.repo_root)
    repo.require_writable()  # D67 8
    catalog = repo.load_catalog()
    manifest = plan.manifest
    now = utc_now()
    stamp = iso_utc(now)
    is_new = not repo.has_game(plan.game_id)
    if is_new:  # D67 1: a new record's folder is named after the game
        repo.assign_folder(plan.game_id, plan.display_name, record_version(plan.header["game"]))
    snapshot_id = repo.next_snapshot_id(plan.game_id, now)
    tmp = repo.new_tmp_dir(plan.game_id, snapshot_id)
    storages: set[str] = set()
    # D67 3: the new snapshot lays the files out where they belong, whatever the zip did
    try:
        placed = {
            "format_version": MANIFEST_FORMAT,
            "sources": readable_sources(manifest["sources"]),
        }
        for source in placed["sources"]:
            os.makedirs(long_path(source_data_dir(tmp, placed, source["source_id"])), exist_ok=True)
        with zipfile.ZipFile(long_path(plan.zip_path)) as zf:
            for item in manifest["files"]:
                target = snapshot_file_path(tmp, placed, item)
                storages.add(_import_member(repo, zf, item, target, manifest))
        imported = {
            **manifest,
            "sources": placed["sources"],
            "format_version": MANIFEST_FORMAT,
            "snapshot_id": snapshot_id,
            "created_at": stamp,
            "tool_version": __version__,
            "game_id": plan.game_id,
            "reason": "import",
            "storage": storage_kind(storages),
            "imported_from": {
                "archive": plan.zip_path.name,
                "game_id": plan.header["game"].get("game_id"),
                "snapshot_id": plan.header["snapshot_id"],
                "snapshot_created_at": manifest.get("created_at"),
                "exported_at": plan.header.get("exported_at"),
                "exported_by": plan.header.get("tool_version"),
            },
        }
        # M45 review: the three keys first, as a backup writes them (its order is read)
        head = ("format_version", "snapshot_id", "created_at")
        imported = {key: imported[key] for key in head} | imported
        game = {**plan.header["game"], "game_id": plan.game_id, "display_name": plan.display_name}
        if repo.has_game(plan.game_id):
            game = repo.load_game(plan.game_id)
        snapshot_id = repo.commit_snapshot(
            tmp, plan.game_id, imported, readme=lambda final: snapshot_readme(game, final)
        )
    except BaseException as exc:
        repo.discard_tmp(tmp)
        if is_new:
            repo.remove_if_empty(plan.game_id)
        if isinstance(exc, (VerifyMismatch, SourceReadError)):
            raise VerificationError(f"nothing was imported: {exc}") from exc
        if isinstance(exc, (OSError, zipfile.BadZipFile)):
            raise FatalError(f"import failed, nothing was imported: {describe_error(exc)}") from exc
        raise
    if is_new:
        doc = _new_doc(plan, stamp)
    else:
        doc = repo.load_game(plan.game_id)
        identity = plan.header["game"].get("identity") or {}
        for key in IDENTITY_KEYS:
            if _text(identity.get(key)) and not doc["identity"].get(key):
                doc["identity"][key] = identity[key]
        if not doc.get("engine_version"):
            doc["engine_version"] = _text(plan.header["game"].get("engine_version"))
    templates = {t["source_id"] for t in doc["source_templates"]}
    for source in manifest["sources"]:
        if source["source_id"] not in templates:
            doc["source_templates"].append(source_template(source))
    doc["updated_at"] = stamp
    repo.save_game(doc)
    update_catalog_entry(repo, catalog, doc)
    write_index(repo)  # D67 5
    files = manifest["files"]
    return ImportResult(
        game_id=plan.game_id,
        display_name=str(doc["display_name"]),
        snapshot_id=snapshot_id,
        created_record=is_new,
        file_count=len(files),
        total_bytes=sum(f["size"] for f in files),
        storage=storage_kind(storages),
    )


def import_archive(repo_root: Path, zip_path: Path) -> ImportResult:
    """Plan and execute an import in one call (plan section 17, M9)."""
    return execute_import(plan_import(repo_root, zip_path))
