"""Exporting one snapshot into a verified zip: the archive format, anonymized paths and the
README inside (plan section 16 M8, D27)."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from enginesave import __version__, layout
from enginesave.engines.renpy.paths import default_saves_root
from enginesave.engines.renpy.paths import saves_root_override as env_saves_root_override
from enginesave.errors import FatalError, UsageError, VerificationError
from enginesave.fsutil import (
    CHUNK_SIZE,
    describe_error,
    is_within,
    iso_utc,
    long_path,
    ns_from_iso,
    own_temp_file,
    utc_now,
)
from enginesave.identity import normalize_name
from enginesave.models import GameIdentity, RootToken, SourceKind
from enginesave.repo import DATA_DIR, Repo, dump_json
from enginesave.restore import validate_manifest
from enginesave.roots import profile_folders

EXPORT_FORMAT_VERSION = 2  # 0.11.0 (D67 6): data files where they belong; 1 = data/<source>
EXPORT_FORMATS = (1, 2)  # what an import reads
EXPORT_JSON = "EngineSave-export.json"
# exports made while EngineSave was called SaveTrail (up to 1.0.0): imported the same way
LEGACY_EXPORT_JSON = "SaveTrail-export.json"
TMP_SUFFIX = ".enginesave-tmp"
MAX_HEADER_BYTES = 64 * 1024 * 1024
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
IDENTITY_KEYS = tuple(f.name for f in dataclasses.fields(GameIdentity))
TOKENS_WARNING = (
    "data/machine_tokens/ contains Ren'Py's save-signing PRIVATE KEY (security_keys.txt). "
    "Do not share this archive."
)
PATHS_ANONYMIZED = "anonymized"
PATHS_ORIGINAL = "original"
EXCLUDED_KEYS = "private_keys"  # machine_tokens without include_tokens
EXCLUDED_ABSOLUTE = "absolute_path"  # ABSOLUTE sources in an anonymized export
# 0.10.0 (D65 1): where the profile root tokens are on a Windows machine, for the README
PROFILE_FOLDERS = {
    "UNITY_LOCALLOW": r"%USERPROFILE%\AppData\LocalLow",
    "LOCALAPPDATA": "%LOCALAPPDATA%",
    "APPDATA": "%APPDATA%",
    "DOCUMENTS": r"%USERPROFILE%\Documents",  # 0.11.0 (D69 3)
    "SAVED_GAMES": r"%USERPROFILE%\Saved Games",
    "VIRTUALSTORE": r"%LOCALAPPDATA%\VirtualStore",
}
# Drive (C:\, C:/) or UNC (\\server\) paths inside free text such as an OSError message: a
# quoted one is replaced up to its closing quote, a bare one up to the next blank.
QUOTED_PATH_RE = re.compile(r"""(['"])(?:[A-Za-z]:[\\/]|\\\\)[^'"]*\1""")
ABSOLUTE_PATH_RE = re.compile(r"(?:\b[A-Za-z]:[\\/]|\\\\[^\\\s'\"]+\\)[^\s'\"<>|]*")
PATH_PLACEHOLDER = "<path>"


@dataclass
class ExportResult:
    path: str
    game_id: str
    display_name: str
    snapshot_id: str
    file_count: int
    total_bytes: int
    archive_bytes: int
    includes_tokens: bool
    excluded_sources: list[str]
    paths: str = PATHS_ANONYMIZED
    excluded_reasons: dict[str, str] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def default_export_name(display_name: str, snapshot_id: str) -> str:
    """``EngineSave-<normalized display name>-<snapshot id>.zip``."""
    return f"EngineSave-{normalize_name(display_name) or 'game'}-{snapshot_id}.zip"


def member_name(item: Mapping[str, Any], manifest: Mapping[str, Any] | None = None) -> str:
    """A file's name in the archive: below its source's folder of ``manifest`` (the
    readable layout of export format 2, D67 6), ``data/<source>/...`` without one."""
    if manifest is None:
        return f"{DATA_DIR}/{item['source_id']}/{item['path']}"
    return f"{layout.source_data_rel(manifest, str(item['source_id']))}/{item['path']}"


def _zip_time(mtime: Any) -> tuple[int, int, int, int, int, int]:
    """Local date-time for a zip entry (zip has no time zone), clamped to the 1980 epoch."""
    try:
        moment = datetime.fromtimestamp(ns_from_iso(str(mtime)) / 1_000_000_000)
    except (ValueError, OverflowError, OSError):
        moment = datetime.now()
    stamp = (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)
    return max(ZIP_EPOCH, stamp)


def _without(manifest: dict[str, Any], excluded: Sequence[str]) -> dict[str, Any]:
    if not excluded:
        return manifest
    return {
        **manifest,
        "sources": [s for s in manifest.get("sources", []) if s["source_id"] not in excluded],
        "files": [f for f in manifest.get("files", []) if f["source_id"] not in excluded],
        "errors": [e for e in manifest.get("errors", []) if e.get("source_id") not in excluded],
    }


def token_location(source: Mapping[str, Any]) -> str:
    """``GAME_ROOT/game/saves`` or ``RENPY_SAVES_ROOT/<save dir>``: a source without its host."""
    relative = str(source["relative_root"]).replace("\\", "/")
    return f"{source['root_token']}/{relative}"


def _scrub(value: Any, known: Sequence[tuple[str, str]]) -> Any:
    """Strings with the known source folders in token form; any other absolute path ``<path>``."""
    if isinstance(value, dict):
        return {key: _scrub(item, known) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, known) for item in value]
    if not isinstance(value, str):
        return value
    for original, token in known:
        for form in (original, original.replace("\\", "\\\\")):  # repr() doubles backslashes
            value = re.sub(re.escape(form), lambda _m, t=token: t, value, flags=re.IGNORECASE)
    value = QUOTED_PATH_RE.sub(lambda m: f"{m.group(1)}{PATH_PLACEHOLDER}{m.group(1)}", value)
    return ABSOLUTE_PATH_RE.sub(PATH_PLACEHOLDER, value)


def anonymized(
    doc: dict[str, Any], manifest: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Game record and manifest without this computer's folders (S-059).

    ``manifest`` must no longer contain ABSOLUTE sources (they have no token form). Source
    locations become :func:`token_location`, installs are dropped, and every remaining string is
    scrubbed of absolute paths, so error messages and unknown fields cannot leak one either.
    """
    known = [
        (str(s["origin_path"]), token_location(s))
        for s in manifest.get("sources", [])
        if s.get("origin_path")
    ]
    sources = [{**s, "origin_path": token_location(s)} for s in manifest.get("sources", [])]
    clean_manifest = _scrub({**manifest, "sources": sources}, known)
    templates = [
        t
        for t in doc.get("source_templates", [])
        if t.get("root_token") != RootToken.ABSOLUTE.value
    ]
    clean_doc = _scrub({**doc, "installs": [], "source_templates": templates}, known)
    return clean_doc, clean_manifest


def export_readme(
    doc: dict[str, Any],
    manifest: dict[str, Any],
    exported_at: str,
    includes_tokens: bool,
    *,
    paths: str = PATHS_ORIGINAL,
    excluded_reasons: Mapping[str, str] | None = None,
) -> str:
    """Plain-text guide inside the archive: what is in it and how to put it back by hand."""
    identity = doc.get("identity") or {}
    engine = f"{doc.get('engine')} {doc.get('engine_version') or ''}".rstrip()
    where = (
        "anonymized: folders are shown as GAME_ROOT/... (the game folder), "
        "RENPY_SAVES_ROOT/... (%APPDATA%\\RenPy) and, for other engines, UNITY_LOCALLOW/..., "
        "LOCALAPPDATA/..., APPDATA/..., DOCUMENTS/..., SAVED_GAMES/... and VIRTUALSTORE/..."
        if paths == PATHS_ANONYMIZED
        else "original: the folders of the computer that made the backup"
    )
    lines = [
        "EngineSave export",
        "================",
        "",
        f"Game:      {doc.get('display_name')}",
        f"Version:   {identity.get('config_version') or 'unknown'}",
        f"Engine:    {engine}",
        f"Snapshot:  {manifest['snapshot_id']} ({manifest.get('reason')}, "
        f"{manifest.get('status')}), taken {manifest.get('created_at')}",
        f"Exported:  {exported_at} by EngineSave {__version__}",
        f"Paths:     {where}",
        "",
        "Contents",
        "--------",
        "EngineSave-export.json     description for EngineSave (game record and manifest)",
        "game.json, manifest.json  the same records, for reading",
        "data/<place>/...          the saved files as backed up, where they belong (SHA-256 of",
        "                          each in manifest.json; see the folders above)",
        "",
        "Sources",
        "-------",
    ]
    folders = layout.data_dirs(manifest)
    for source in manifest.get("sources", []):
        count = sum(1 for f in manifest.get("files", []) if f["source_id"] == source["source_id"])
        folder = folders.get(source["source_id"], f"{DATA_DIR}/{source['source_id']}")
        lines += [
            f"{folder}/  ({count} files)",
            f"  kind: {source['kind']}   root: {source['root_token']}   "
            f"relative root: {source['relative_root']}",
            f"  backed up from: {source.get('origin_path')}",
        ]
    if excluded_reasons:
        lines += ["", "Not included", "------------"]
        for source_id, reason in sorted(excluded_reasons.items()):
            why = (
                "Ren'Py's save-signing private keys (export with --include-tokens to add them)"
                if reason == EXCLUDED_KEYS
                else "a save folder known only by its absolute path (config.extra_savedirs); "
                "export with --keep-paths to add it"
            )
            lines.append(f"{source_id}: {why}")
    lines += [
        "",
        "Restoring by hand",
        "-----------------",
        "1. Quit the game, and copy the saves that are there now somewhere safe first.",
        "2. Copy the contents of each data folder to its place:",
    ]
    for source in manifest.get("sources", []):
        relative = str(source["relative_root"]).replace("/", "\\")
        token = source["root_token"]
        if token == RootToken.GAME_ROOT.value:
            where = "<game folder>" if relative == "." else f"<game folder>\\{relative}"
        elif token == RootToken.RENPY_SAVES_ROOT.value:
            where = f"%APPDATA%\\RenPy\\{relative}"
        elif token in PROFILE_FOLDERS:  # 0.10.0 (D65 1)
            where = f"{PROFILE_FOLDERS[token]}\\{relative}"
        else:
            where = relative
        if source.get("kind") == "registry":  # 0.10.0 (D65): Unity's PlayerPrefs
            where += "  (a .reg file: double-click it to import the settings)"
        folder = folders.get(source["source_id"], f"{DATA_DIR}/{source['source_id']}")
        lines.append(f"   {folder}/  ->  {where}")
    lines += [
        "3. Start the game. Whether it loads these saves depends on the game version.",
        "",
        "Restoring with EngineSave",
        "------------------------",
        "enginesave import --repo <backup library> --from <this zip>",
        "then run the restore command it prints (try it with --dry-run first).",
    ]
    if includes_tokens:
        lines += ["", "WARNING", "-------", TOKENS_WARNING]
    # D67 6: the snapshot's README first (Chinese, then English), this export's notes after it
    readme = layout.snapshot_readme(doc, manifest, resolve=paths != PATHS_ANONYMIZED)
    return readme + "\r\n\r\n" + "\r\n".join(lines) + "\r\n"


def export_forbidden_roots(
    doc: dict[str, Any],
    manifest: dict[str, Any],
    *,
    saves_root_override: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[Path]:
    """Folders an export must stay out of: installs, backed-up sources, the Ren'Py saves roots
    (the machine default and, when set, the override)."""
    env = dict(os.environ) if env is None else env
    places = [i.get("game_root") for i in doc.get("installs", [])]
    places += [s.get("origin_path") for s in manifest.get("sources", [])]
    # Imported anonymized snapshots carry GAME_ROOT/... forms, which name no folder here.
    roots = [Path(p) for p in places if p and Path(p).is_absolute()]
    # 0.10.0 (D65 1): another engine's folders in the user profile, as this machine has them
    # (an imported snapshot knows them only by their tokens)
    roots += profile_folders([*manifest.get("sources", []), *doc.get("source_templates", [])], env)
    roots.append(default_saves_root(env, None)[0])
    override = saves_root_override or env_saves_root_override(None, env)
    if override is not None:
        roots.append(override)
    return roots


def check_export_target(dest: Path, repo: Repo, forbidden_roots: Sequence[Path]) -> None:
    """The archive may not be written inside the library, a scan root or a save location."""
    if is_within(dest, repo.root):
        raise UsageError(f"the export file must not be inside the backup library {repo.root}")
    for root in forbidden_roots:
        if is_within(dest, root):
            raise UsageError(f"the export file must not be inside {root}")


def _verify_archive(path: Path, manifest: Mapping[str, Any]) -> None:
    """``testzip``, data member count and a streamed SHA-256 of every member vs the manifest."""
    files = list(manifest.get("files") or [])
    try:
        with zipfile.ZipFile(long_path(path)) as zf:
            broken = zf.testzip()
            if broken is not None:
                raise VerificationError(f"archive member {broken} is corrupt")
            data_members = [n for n in zf.namelist() if n.startswith(f"{DATA_DIR}/")]
            if len(data_members) != len(files):
                raise VerificationError(
                    f"the archive holds {len(data_members)} data files, the manifest lists "
                    f"{len(files)}"
                )
            for item in files:
                digest = hashlib.sha256()
                size = 0
                with zf.open(member_name(item, manifest)) as member:
                    while chunk := member.read(CHUNK_SIZE):
                        digest.update(chunk)
                        size += len(chunk)
                if (digest.hexdigest(), size) != (item["sha256"], item["size"]):
                    raise VerificationError(
                        f"{member_name(item, manifest)} does not match the manifest "
                        f"(run verify on the backup library)"
                    )
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        detail = describe_error(exc)
        raise VerificationError(f"the archive could not be read back: {detail}") from exc


def _write_archive(
    tmp: Path | BinaryIO,
    header: dict[str, Any],
    readme: str,
    snapshot_dir: Path,
    stored: Mapping[str, Any],
) -> None:
    """The archive: each file read where the snapshot keeps it (``stored``, its manifest)
    and written where the exported manifest places it."""
    exported = header["manifest"]
    target = tmp if not isinstance(tmp, Path) else long_path(tmp)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(EXPORT_JSON, json.dumps(header, indent=2, ensure_ascii=False))
        zf.writestr("game.json", dump_json(header["game"]))
        zf.writestr("manifest.json", dump_json(exported))
        zf.writestr("README.txt", "\ufeff" + readme)  # M46 review: as in the library
        for item in exported.get("files", []):
            source = layout.snapshot_file_path(snapshot_dir, stored, item)
            try:
                fin = open(long_path(source), "rb")
            except OSError as exc:
                raise FatalError(
                    f"snapshot file {item['source_id']}/{item['path']} is unreadable "
                    f"({describe_error(exc)}); run verify on the backup library"
                ) from exc
            info = zipfile.ZipInfo(
                member_name(item, exported), date_time=_zip_time(item.get("mtime"))
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            with fin, zf.open(info, "w", force_zip64=item["size"] >= 2**31) as fout:
                shutil.copyfileobj(fin, fout, CHUNK_SIZE)


def export_snapshot(
    repo: Repo,
    game_id: str,
    snapshot_id: str,
    dest: Path,
    *,
    overwrite: bool = False,
    include_tokens: bool = False,
    keep_paths: bool = False,
    forbidden_roots: Sequence[Path] = (),
    saves_root_override: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ExportResult:
    """Write one snapshot as a verified zip at ``dest`` (a file path).

    ``machine_tokens`` sources are left out unless ``include_tokens``. Paths are anonymized
    (:func:`anonymized`; ABSOLUTE sources are left out) unless ``keep_paths``. ``dest`` must not
    be inside the library, the game's installs, a backed-up source folder, the Ren'Py saves root
    or any of ``forbidden_roots`` (the caller's scan roots) → UsageError; an existing file
    needs ``overwrite``. A failed verification removes the archive (VerificationError).
    """
    doc = repo.load_game(game_id)
    if snapshot_id not in repo.snapshot_ids(game_id):
        raise UsageError(f"no snapshot {snapshot_id} for {doc.get('display_name')}")
    manifest = repo.load_manifest(game_id, snapshot_id)
    validate_manifest(manifest)
    dest = Path(os.path.abspath(dest))
    if dest.is_dir():
        raise UsageError(f"{dest} is a folder; give the export a file name")
    inherent = export_forbidden_roots(
        doc, manifest, saves_root_override=saves_root_override, env=env
    )
    check_export_target(dest, repo, [*inherent, *forbidden_roots])
    if dest.exists() and not overwrite:
        raise UsageError(f"{dest} already exists (use --overwrite to replace it)")
    reasons: dict[str, str] = {}
    for source in manifest.get("sources", []):
        if source["kind"] == SourceKind.MACHINE_TOKENS.value and not include_tokens:
            reasons[source["source_id"]] = EXCLUDED_KEYS
        elif source["root_token"] == RootToken.ABSOLUTE.value and not keep_paths:
            reasons[source["source_id"]] = EXCLUDED_ABSOLUTE
    excluded = sorted(reasons)
    exported = _without(manifest, excluded)
    game = doc
    paths = PATHS_ORIGINAL if keep_paths else PATHS_ANONYMIZED
    if not keep_paths:
        game, exported = anonymized(doc, exported)
    exported = {  # D67 6: the files where they belong, as in a new snapshot
        **exported,
        "format_version": layout.READABLE_FORMAT,
        "sources": layout.readable_sources(exported.get("sources", [])),
    }
    files = exported.get("files", [])
    tokens = any(s["kind"] == SourceKind.MACHINE_TOKENS.value for s in exported["sources"])
    exported_at = iso_utc(utc_now())
    header = {
        "export_format_version": EXPORT_FORMAT_VERSION,
        "tool_version": __version__,
        "exported_at": exported_at,
        "game": game,
        "snapshot_id": snapshot_id,
        "manifest": exported,
        "includes_tokens": tokens,
        "excluded_sources": excluded,
        "excluded_reasons": {sid: reasons[sid] for sid in excluded},
        "paths": paths,
    }
    readme = export_readme(
        game, exported, exported_at, tokens, paths=paths, excluded_reasons=reasons
    )
    snapshot_dir = repo.snapshot_dir(game_id, snapshot_id)
    try:
        os.makedirs(long_path(dest.parent), exist_ok=True)
        tmp, out = own_temp_file(dest, TMP_SUFFIX)  # D72 R02: a file of our own, made anew
    except OSError as exc:
        raise FatalError(f"export failed: {describe_error(exc)}") from exc
    try:
        with out:
            _write_archive(out, header, readme, snapshot_dir, manifest)
        _verify_archive(tmp, exported)
        os.replace(long_path(tmp), long_path(dest))
    except BaseException as exc:
        with contextlib.suppress(OSError):
            os.unlink(long_path(tmp))
        if isinstance(exc, OSError):
            raise FatalError(f"export failed and was removed: {describe_error(exc)}") from exc
        raise
    return ExportResult(
        path=str(dest),
        game_id=game_id,
        display_name=str(doc.get("display_name")),
        snapshot_id=snapshot_id,
        file_count=len(files),
        total_bytes=sum(f["size"] for f in files),
        archive_bytes=os.path.getsize(long_path(dest)),
        includes_tokens=tokens,
        excluded_sources=excluded,
        paths=paths,
        excluded_reasons={sid: reasons[sid] for sid in excluded},
    )
