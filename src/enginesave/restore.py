"""Restore (plan 9.2): validate → resolve targets → preview → safety snapshot → write → verify.

Every file is written to ``<name>.<random>.enginesave-tmp`` beside its target (created anew,
never an existing file: 1.0.0, D72 R02), checked against the
manifest hash, then moved into place with ``os.replace`` and hashed again. Files present in a
target but absent from the snapshot are never deleted.

Since 0.6.0 the install that receives local saves is chosen with care (D39, plan section 21):
a ``--game-root`` must be a Ren'Py game of the same save directory (the folder around it, as
an archive unpacks, is replaced by the game inside); without one, the candidates are the
record's installs that still exist plus the installs a scan recognised as this record, and
only a single candidate is used by itself. A different game version is noted, never refused.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enginesave import layout, roots
from enginesave.backup import (
    PlannedGame,
    PlannedSource,
    backup_game,
    touch_install,
    update_catalog_entry,
)
from enginesave.engines.renpy.paths import game_saves_root
from enginesave.errors import EngineSaveError, FatalError, UsageError
from enginesave.fsutil import (
    CHUNK_SIZE,
    describe_error,
    is_reparse_point,
    is_within,
    iso_utc,
    long_path,
    matches_patterns,
    ns_from_iso,
    own_temp_file,
    path_key,
    safe_join,
    safe_relative_component,
    sha256_file,
    source_files,
    split_relative,
    utc_now,
)
from enginesave.models import (
    LOAD_NOTE,
    DetectedGame,
    GameStatus,
    RootToken,
    SaveSource,
    ScanReport,
    SourceKind,
    SourceStatus,
)
from enginesave.repo import Repo
from enginesave.scan import ScanOptions, run_scan

TMP_SUFFIX = ".enginesave-tmp"

# Post-write verification hash; a module attribute so tests can inject failures.
hash_file = sha256_file


class RestoreError(Exception):
    """One file could not be restored (recorded; the restore continues with other files)."""


@dataclass
class RestoreTarget:
    source: dict[str, Any]  # manifest source entry
    target_dir: Path
    resolved_by: str
    files: list[dict[str, Any]]
    overwrites: int = 0
    extra_in_target: int = 0
    creates_dir: bool = False

    @property
    def source_id(self) -> str:
        return str(self.source["source_id"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.source["kind"],
            "target_dir": str(self.target_dir),
            "resolved_by": self.resolved_by,
            "files": len(self.files),
            "overwrites": self.overwrites,
            "extra_in_target": self.extra_in_target,
            "creates_dir": self.creates_dir,
        }


@dataclass
class RestorePlan:
    repo: Repo
    game_id: str
    display_name: str
    kind: str
    snapshot_id: str
    manifest: dict[str, Any]
    targets: list[RestoreTarget]
    game_root: Path | None = None  # the install local saves go into, when there is one
    notes: list[str] = field(default_factory=list)  # a folder replaced, a version that differs
    given_root: Path | None = None  # the --game-root given, when the game inside it is used
    saved_version: str | None = None  # the version the saves were made with, when known
    target_version: str | None = None  # the version found in game_root
    # 0.8.0 (D48 1): the other games that use the global save directory restored into
    shared_with: list[str] = field(default_factory=list)
    # 1.2.0 (D73 15): the install whose local saves were chosen among several, if so
    from_install: str | None = None

    @property
    def includes_tokens(self) -> bool:
        return any(t.source["kind"] == SourceKind.MACHINE_TOKENS.value for t in self.targets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": str(self.repo.root),
            "game_id": self.game_id,
            "display_name": self.display_name,
            "snapshot_id": self.snapshot_id,
            "snapshot_reason": self.manifest.get("reason"),
            "snapshot_status": self.manifest.get("status"),
            "game_root": str(self.game_root) if self.game_root else None,
            "notes": list(self.notes),
            "shared_with": list(self.shared_with),
            "targets": [t.to_dict() for t in self.targets],
            "from_install": self.from_install,  # 1.2.0 (D73 15)
        }


@dataclass
class RestoreResult:
    written: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    safety_snapshot: str | None = None
    registered_install: str | None = None  # 0.7.0 (D43): the game folder now an install

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "failures": self.failures,
            "safety_snapshot": self.safety_snapshot,
            "note": None if self.failures else LOAD_NOTE,
            "registered_install": self.registered_install,
        }


# ---------------------------------------------------------------------------------- planning


def _outside_selection(source: Mapping[str, Any], rel: str) -> bool:
    """0.10.0 (D65 2): a file a source with ``patterns``/``excludes`` would never take -- one
    below its folder or not matching its patterns, or under an excluded name. A manifest that
    lists one is refused: the restore would write where the safety snapshot does not look."""
    patterns, excludes = _selection(source)
    parts = rel.replace("\\", "/").split("/")
    if patterns and (len(parts) != 1 or not matches_patterns(parts[0], patterns)):
        return True
    return bool(excludes) and matches_patterns(parts[0], excludes)


def validate_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every source and file entry must be well-formed and path-safe, or nothing is restored."""
    sources: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("sources", []):
        try:
            source_id = safe_relative_component(str(entry["source_id"]))
            relative = str(entry["relative_root"])
            # 0.10.0 (D65 1): every token of the registry; one this EngineSave does not know
            # (a later version's) is refused with that reason
            roots.check_relative(str(entry["root_token"]), relative)
            if relative == roots.ROOT_ITSELF and not entry.get("patterns"):
                raise ValueError("the game folder itself needs file patterns")
            for key in ("patterns", "excludes"):
                names = entry.get(key, [])
                if not isinstance(names, list) or not all(
                    isinstance(p, str) and p and "/" not in p and "\\" not in p for p in names
                ):
                    raise ValueError(f"{key} must be file name patterns")
            SourceKind(entry["kind"])
        except roots.UnknownToken as exc:
            raise FatalError(str(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise FatalError(f"manifest source entry is invalid or unsafe ({exc})") from exc
        sources[source_id] = entry
    if manifest.get("format_version") == layout.READABLE_FORMAT:  # M46 review: D67 3
        dirs = layout.data_dirs(manifest)
        missing = sorted(set(sources) - set(dirs))
        if missing:
            raise FatalError(f"manifest source {missing[0]!r} has no safe data folder")
        placed = list(dirs.values())
        for i, folder in enumerate(placed):
            if any(layout.overlaps(folder, other) for other in placed[i + 1 :]):
                raise FatalError(f"two sources of the manifest share the data folder {folder!r}")
    for item in manifest.get("files", []):
        try:
            if item["source_id"] not in sources:
                raise ValueError(f"unknown source {item['source_id']!r}")
            split_relative(str(item["path"]))
            if _outside_selection(sources[item["source_id"]], str(item["path"])):
                raise ValueError("not one of the files its source takes")
            if not isinstance(item["sha256"], str) or not isinstance(item["size"], int):
                raise ValueError("missing hash or size")
        except (KeyError, TypeError, ValueError) as exc:
            raise FatalError(
                f"manifest file entry {item.get('path')!r} is invalid or unsafe ({exc})"
            ) from exc
    return sources


RECORDED_INSTALL = "recorded install"
SCANNED_INSTALL = "install found by the scan"


def install_candidates(
    doc: Mapping[str, Any], report: ScanReport | None = None
) -> list[tuple[Path, str]]:
    """Where local saves could go back without a ``--game-root`` (D39): the record's installs
    that still exist (most recently seen first), then the installs a scan recognised as this
    record (``known as``) that the record does not list yet, e.g. a newer version unpacked
    elsewhere and not backed up so far. ``(folder, how it was found)``."""
    found: dict[str, tuple[Path, str]] = {}
    installs = sorted(doc.get("installs", []), key=lambda i: i.get("last_seen") or "", reverse=True)
    for install in installs:
        path = Path(install.get("game_root") or "")
        if path.is_absolute() and path.is_dir():
            found.setdefault(path_key(path), (path, RECORDED_INSTALL))
    for game in report.games if report is not None else []:
        if game.known_game_id != doc.get("game_id"):
            continue
        for path, _version in _install_roots(game):
            if path.is_dir():
                found.setdefault(path_key(path), (path, SCANNED_INSTALL))
    return list(found.values())


def game_label(name: str, version: str | None) -> str:
    return f"{name} ({version})" if version else name


def profile_sharers(
    repo: Repo,
    catalog: Mapping[str, Any],
    doc: Mapping[str, Any],
    folder: Path,
    report: ScanReport | None,
    env: Mapping[str, str],
) -> list[str]:
    """0.10.0 (D65 4): the other records and scanned games whose folder in the user profile
    is ``folder``, inside it or around it."""

    def overlaps(path: Path) -> bool:
        return is_within(path, folder) or is_within(folder, path)

    labels: list[str] = []
    for record in catalog.get("games", []):
        if record.get("kind", "game") != "game" or record.get("game_id") == doc.get("game_id"):
            continue
        try:
            other = repo.load_game(str(record["game_id"]))
        except (EngineSaveError, OSError, KeyError):
            continue
        if any(
            overlaps(p) for p in roots.profile_folders(other.get("source_templates") or [], env)
        ):
            labels.append(str(record.get("display_name") or record["game_id"]))
    for game in report.games if report is not None else []:
        if game.known_game_id == doc.get("game_id") or game.display_name in labels:
            continue
        if any(
            s.root_token.value in roots.ENGINE_PROFILE_TOKENS and overlaps(Path(s.path))
            for s in game.sources
        ):
            labels.append(game.display_name)
    return labels


def sharing_games(
    repo: Repo,
    catalog: Mapping[str, Any],
    doc: Mapping[str, Any],
    save_directory: str,
    report: ScanReport | None = None,
) -> list[str]:
    """D48 1: the other games whose saves are in the same global save directory, ``name
    (version)``: other records with that save directory, and games of the window's last scan
    that are neither this record nor one of those."""
    wanted = save_directory.casefold()
    labels: list[str] = []
    counted = {doc.get("game_id")}
    for record in catalog.get("games", []):
        identity = record.get("identity") or {}
        if record.get("kind", "game") != "game" or record.get("game_id") in counted:
            continue
        if str(identity.get("renpy_save_directory") or "").casefold() != wanted:
            continue
        counted.add(record.get("game_id"))
        try:
            version = (repo.load_game(record["game_id"]).get("identity") or {}).get(
                "config_version"
            )
        except (EngineSaveError, OSError):  # a damaged record still counts, without a version
            version = None
        labels.append(game_label(str(record.get("display_name") or record["game_id"]), version))
    ours = {path_key(i["game_root"]) for i in doc.get("installs", []) if i.get("game_root")}
    for game in report.games if report is not None else []:
        roots = {path_key(path) for path, _version in _install_roots(game)}
        if game.known_game_id in counted or roots & ours:
            continue
        if str(game.identity.renpy_save_directory or "").casefold() == wanted:
            labels.append(game_label(game.display_name, game.identity.config_version))
    return labels


def _install_roots(game: DetectedGame) -> list[tuple[Path, str | None]]:
    """Every folder a scanned game stands for, with its version: a game found in several
    folders (same name and save directory, D9) is one entry with several installs."""
    roots = {path_key(game.game_root): (Path(game.game_root), game.identity.config_version)}
    for install in game.installs:
        if install.get("game_root"):
            roots.setdefault(
                path_key(install["game_root"]),
                (Path(install["game_root"]), install.get("config_version")),
            )
    return list(roots.values())


def detect_games(
    folder: Path, *, saves_root_override: Path | None = None, env: Mapping[str, str] | None = None
) -> list[DetectedGame]:
    """The games in ``folder`` itself or directly inside it (a read-only scan, one level)."""
    report = run_scan(
        ScanOptions(
            roots=[folder],
            max_depth=1,
            include_unlinked=False,
            saves_root_override=saves_root_override,
            env=env,
        )
    )
    return report.games


def find_install(
    folder: Path, *, saves_root_override: Path | None = None, env: Mapping[str, str] | None = None
) -> tuple[DetectedGame, str | None] | None:
    """The game installed exactly at ``folder`` and that install's version, if any."""
    for game in detect_games(folder, saves_root_override=saves_root_override, env=env):
        for path, version in _install_roots(game):
            if path_key(path) == path_key(folder):
                return game, version
    return None


def check_game_root(
    folder: Path,
    doc: Mapping[str, Any],
    *,
    saves_root_override: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, str | None, str | None]:
    """What a ``--game-root`` names (plan section 21 M21): the Ren'Py game at ``folder``, or
    the only game directly inside it (the folder an archive unpacks into), never another game.

    Returns ``(install, its game version, note)``; the note says when the folder was replaced.
    Refused (exit code 2) when there is no game or several, when it is not a Ren'Py game, or
    when its save directory differs from the record's (restoring into another game).
    """
    name = doc.get("display_name") or doc.get("game_id")
    games = detect_games(folder, saves_root_override=saves_root_override, env=env)
    installs = [(path, version, game) for game in games for path, version in _install_roots(game)]
    found = "; ".join(str(path) for path, _version, _game in installs) or "none"
    exact = [item for item in installs if path_key(item[0]) == path_key(folder)]
    note = None
    if exact:
        root, version, game = exact[0]
    else:
        inside = [item for item in installs if path_key(item[0].parent) == path_key(folder)]
        if len(inside) != 1:
            raise UsageError(
                f"--game-root {folder} is not the folder of a game (games in it: {found}); "
                "give the folder that holds the game's .exe and game folder"
            )
        root, version, game = inside[0]
        note = f"{folder} holds the game in {root}; restoring into that folder"
    from enginesave.engines.labels import engine_label
    from enginesave.identity import game_match_key, match_key

    engine = str(doc.get("engine") or "renpy")
    if engine != "renpy":  # 0.10.0 (D65 3): the same engine and, when both have one, key
        if game.status is GameStatus.UNSUPPORTED or game.engine != engine:
            raise UsageError(
                f"{root} is not a {engine_label(engine)} game ({engine_label(game.engine)}); "
                "choose that game's folder"
            )
        wanted_key = match_key(engine, doc.get("identity") or {})
        found_key = game_match_key(game)
        if wanted_key and found_key and wanted_key != found_key:
            raise UsageError(
                f"{root} is {game.display_name}, not {name}; choose that game's folder"
            )
        return root, version, note
    if game.status is GameStatus.UNSUPPORTED:
        raise UsageError(f"{root} is not a Ren'Py game ({game.engine})")
    wanted = str((doc.get("identity") or {}).get("renpy_save_directory") or "")
    theirs = str(game.identity.renpy_save_directory or "")
    if wanted.casefold() != theirs.casefold():
        raise UsageError(
            f"{root} is {game.display_name} (save directory {theirs or 'none'}), "
            f"not {name} (save directory {wanted or 'none'}); choose that game's folder"
        )
    return root, version, note


def pick_local(
    local: Sequence[dict[str, Any]], manifest: Mapping[str, Any], wanted: Path | None
) -> tuple[dict[str, Any] | None, str]:
    """D73 15: the local source of the install ``wanted`` (``--from-install``), else of the
    snapshot's ``from_install``; ``(None, "")`` when the snapshot does not name one. A
    ``wanted`` install the snapshot has no local saves of is refused (exit code 2)."""
    from enginesave.repo import from_install_of

    def of(folder: Path) -> dict[str, Any] | None:
        found = [s for s in local if (o := _origin_root(s)) is not None and same(o, folder)]
        return found[0] if len(found) == 1 else None

    def same(a: Path, b: Path) -> bool:
        return path_key(a) == path_key(b)

    if wanted is not None:
        folder = Path(os.path.abspath(wanted))
        pick = of(folder)
        if pick is None:
            have = "; ".join(f"{s['source_id']}: {_origin_root(s) or '?'}" for s in local)
            raise UsageError(
                f"--from-install {folder}: the snapshot has no local saves of that install "
                f"(it has {have})"
            )
        return pick, "--from-install"
    recorded = from_install_of(manifest)
    if recorded is not None:
        pick = of(Path(recorded["game_root"]))
        if pick is not None:
            return pick, "the install the snapshot came from"
    return None, ""


def _snapshot_version(doc: Mapping[str, Any], manifest: Mapping[str, Any]) -> str | None:
    """The game version a snapshot's local saves were made with: the recorded install they
    were backed up from (``None`` when the snapshot names no install, e.g. an anonymous
    import or global saves only)."""
    for source in manifest.get("sources", []):
        if source.get("root_token") != RootToken.GAME_ROOT.value:
            continue
        origin = _origin_root(dict(source))
        for install in doc.get("installs", []) if origin is not None else []:
            if install.get("game_root") and path_key(install["game_root"]) == path_key(origin):
                return install.get("config_version") or None
    return None


def version_note(saved: str | None, target: str | None) -> str | None:
    """D39 (b): a different version is said, never refused."""
    if saved and target and saved != target:
        return (
            f"the saves come from version {saved}, the target is version {target}; "
            "whether the game loads them depends on the game"
        )
    return None


def _origin_root(source: dict[str, Any]) -> Path | None:
    """``origin_path`` minus ``relative_root`` (the install a local source came from).

    ``None`` for an anonymized export's ``GAME_ROOT/...`` form, which names no folder here.
    """
    origin = Path(source["origin_path"])
    if not origin.is_absolute():
        return None
    if source["relative_root"] == roots.ROOT_ITSELF:  # D65 2: the game folder itself
        return origin
    parts = split_relative(source["relative_root"])
    if [p.casefold() for p in origin.parts[-len(parts) :]] != [p.casefold() for p in parts]:
        return None
    return origin.parents[len(parts) - 1]


def _selection(source: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """A manifest source's ``patterns`` and ``excludes`` (0.10.0): which files of its folder
    are the source's."""
    return list(source.get("patterns") or []), list(source.get("excludes") or [])


def _key(rel: str) -> str:
    return os.path.normcase(rel.replace("\\", "/"))


def _preview(target: RestoreTarget) -> None:
    target.creates_dir = not target.target_dir.is_dir()
    present = {}
    if not target.creates_dir:
        files, _ = source_files(target.target_dir, *_selection(target.source))
        present = {_key(f.rel): f for f in files}
    wanted = {_key(f["path"]) for f in target.files}
    target.overwrites = sum(1 for key in wanted if key in present)
    target.extra_in_target = sum(1 for key in present if key not in wanted)


def plan_restore(
    repo: Repo,
    catalog: dict[str, Any],
    game_ref: str,
    *,
    snapshot: str | None = "latest",
    game_root: Path | None = None,
    sources: Sequence[str] | None = None,
    saves_root_override: Path | None = None,
    env: Mapping[str, str] | None = None,
    report: ScanReport | None = None,
    from_install: Path | None = None,
) -> RestorePlan:
    """Work out where every selected source would be written. Nothing is written.

    ``report`` (a scan with this library) adds installs the record does not list yet to the
    candidates (D39); the window passes its last scan, the command line has none.

    1.2.0 (D73 15): with ``game_root``, a snapshot that holds the local saves of several
    installs (``local``, ``local-2``...) restores those of ``from_install``, else of the
    install the snapshot came from (its manifest's ``from_install``); only without either is
    ``--sources`` needed. ``game_root`` may be any folder of this game -- one the record does
    not know, a version installed again elsewhere -- and is checked as before.
    """
    env = dict(os.environ) if env is None else env
    record = repo.resolve_game(catalog, game_ref)
    game_id = record["game_id"]
    doc = repo.load_game(game_id)
    name = doc.get("display_name") or game_id
    kind = doc.get("kind", "game")
    if snapshot in (None, "latest"):
        snapshot_id = repo.latest_snapshot(game_id)
        if snapshot_id is None:
            raise UsageError(f"{name} has no snapshots")
    elif snapshot in repo.snapshot_ids(game_id):
        snapshot_id = snapshot
    else:
        raise UsageError(f"no snapshot {snapshot} for {name}")
    manifest = repo.load_manifest(game_id, snapshot_id)
    by_id = validate_manifest(manifest)

    if sources:
        unknown = [s for s in sources if s not in by_id]
        if unknown:
            raise UsageError(
                f"--sources not in snapshot {snapshot_id}: {', '.join(unknown)} "
                f"(available: {', '.join(by_id)})"
            )
        chosen = [by_id[s] for s in dict.fromkeys(sources)]
    else:
        chosen = [s for s in by_id.values() if s["kind"] != SourceKind.MACHINE_TOKENS.value]
    if not chosen:
        raise UsageError(f"snapshot {snapshot_id} has nothing to restore")

    explicit = Path(os.path.abspath(game_root)) if game_root else None
    local = [s for s in chosen if s["root_token"] == RootToken.GAME_ROOT.value]
    game_bases: dict[str, tuple[Path, str]] = {}
    notes: list[str] = []
    target_version: str | None = None
    checked = False  # the target's version was read while checking --game-root
    candidates = install_candidates(doc, report)
    install: Path | None = None  # the one install local saves go into
    picked_from: str | None = None
    if from_install is not None and explicit is None:
        raise UsageError("--from-install goes with --game-root (the folder to restore into)")
    if explicit is not None:
        if len(local) > 1 and (not sources or from_install is not None):
            # 1.2.0 (D73 15): the local saves of one install -- the one asked for, else the
            # one the snapshot came from -- go into the folder given
            pick, how = pick_local(local, manifest, from_install)
            if pick is not None:
                chosen = [s for s in chosen if s is pick or all(s is not o for o in local)]
                local = [pick]
                origin = _origin_root(pick)
                picked_from = str(origin) if origin is not None else None
                notes.append(
                    f"the snapshot holds the local saves of several installs; restoring "
                    f"{pick['source_id']} ({picked_from}, {how})"
                )
        if len(local) > 1:
            ids = ", ".join(s["source_id"] for s in local)
            raise UsageError(
                f"several game-root sources ({ids}); restore one at a time with --sources, "
                "or name the install they come from with --from-install"
            )
        if is_within(explicit, repo.root) or is_within(repo.root, explicit):
            raise UsageError(f"--game-root {explicit} overlaps the backup repository")
        install, target_version, note = check_game_root(
            explicit, doc, saves_root_override=saves_root_override, env=env
        )
        checked = True
        notes += [note] if note else []
        for s in local:
            game_bases[s["source_id"]] = (install, "--game-root")
        if picked_from is None and len(local) == 1:  # D73 15: where these saves came from
            origin = _origin_root(local[0])
            picked_from = str(origin) if origin is not None else None
    elif len(local) == 1:
        if len(candidates) > 1:  # D39: never pick one of several installs by itself
            listed = "; ".join(str(path) for path, _how in candidates)
            raise UsageError(
                f"{name} is installed in several places; choose one with --game-root "
                f"(candidates: {listed})"
            )
        if not candidates:
            raise UsageError("the game's install folder is unknown or gone; pass --game-root")
        install, how = candidates[0]
        game_bases[local[0]["source_id"]] = (install, how)
    else:
        for s in local:
            origin = _origin_root(s)
            if origin is None or not origin.is_dir():
                raise UsageError(f"install folder of {s['source_id']} is gone; pass --game-root")
            game_bases[s["source_id"]] = (origin, "original install")

    saved_version = None
    if install is not None and local:
        if not checked:
            found = find_install(install, saves_root_override=saves_root_override, env=env)
            target_version = found[1] if found is not None else None
        saved_version = _snapshot_version(doc, {"sources": local} if picked_from else manifest)
        note = version_note(saved_version, target_version)
        notes += [note] if note else []
    fallback = install or (candidates[0][0] if candidates else None)
    saves_root, saves_method = game_saves_root(fallback, env, saves_root_override)
    targets = []
    for entry in list(chosen):
        token = RootToken(entry["root_token"])
        if token is RootToken.VIRTUALSTORE and install is not None:
            # M48 review: the folder Windows redirects to follows the install chosen (without
            # one, the folder recorded at the backup)
            from enginesave.engines.common import virtualstore_relative

            moved = virtualstore_relative(install, env)
            if moved is None:
                if len(chosen) == 1:
                    raise UsageError(
                        f"{entry['source_id']} is the VirtualStore folder of a game installed in "
                        f"Program Files, but {install} is not there: Windows does not redirect "
                        "its saves; copy them from the snapshot by hand if needed"
                    )
                notes.append(
                    f"{entry['source_id']} (the VirtualStore folder of an install in Program "
                    f"Files) is not restored: {install} is not in Program Files, so the game "
                    "does not read it; its files stay in the snapshot"
                )
                chosen = [c for c in chosen if c is not entry]
                continue
            if moved != entry["relative_root"]:
                notes.append(
                    f"{entry['source_id']} goes to the VirtualStore folder of {install}, not "
                    "of the install it was backed up from"
                )
                entry = {**entry, "relative_root": moved}
        if token is RootToken.GAME_ROOT:
            base, how = game_bases[entry["source_id"]]
            target_dir = roots.join_relative(base, entry["relative_root"])
        elif token is RootToken.RENPY_SAVES_ROOT:
            target_dir, how = saves_root / entry["relative_root"], saves_method
        else:  # ABSOLUTE, or a profile folder of another engine (D65 1)
            found, how = roots.resolve(token.value, entry["relative_root"], env)
            if found is None:
                raise UsageError(f"cannot restore {entry['source_id']}: {how}")
            target_dir = found
        if is_within(target_dir, repo.root) or is_within(repo.root, target_dir):
            raise UsageError(f"restore target {target_dir} overlaps the backup repository")
        files = [f for f in manifest.get("files", []) if f["source_id"] == entry["source_id"]]
        target = RestoreTarget(entry, target_dir, how, files)
        _preview(target)
        targets.append(target)
    shared: list[str] = []
    for target in targets:
        if target.source["kind"] == SourceKind.GLOBAL.value and kind == "game":
            if target.source["root_token"] == RootToken.RENPY_SAVES_ROOT.value:
                others = sharing_games(repo, catalog, doc, target.source["relative_root"], report)
            else:  # 0.10.0 (D65 4): another engine's folder, by its path
                others = profile_sharers(repo, catalog, doc, target.target_dir, report, env)
            shared += [o for o in others if o not in shared]
            if others:
                notes.append(
                    f"the global save directory {target.target_dir} is shared with "
                    f"{', '.join(others)}; restoring also overwrites their saves in it"
                )
    if any(t.source["kind"] == SourceKind.REGISTRY.value for t in targets):  # D65: PlayerPrefs
        notes.append(
            "PlayerPrefs.reg goes into the game folder; EngineSave never writes the registry: "
            "double-click the file to put the game's settings back"
        )
    used = install if local and install is not None else None
    replaced = explicit if explicit is not None and install is not None else None
    if replaced is not None and path_key(replaced) == path_key(install):
        replaced = None
    return RestorePlan(
        repo,
        game_id,
        name,
        kind,
        snapshot_id,
        manifest,
        targets,
        used,
        notes,
        replaced,
        saved_version,
        target_version,
        shared,
        picked_from,
    )


# --------------------------------------------------------------------------------- execution


def _safety_source(target: RestoreTarget) -> SaveSource:
    return SaveSource(
        target.source_id,
        SourceKind(target.source["kind"]),
        str(target.target_dir),
        RootToken(target.source["root_token"]),
        target.source["relative_root"],
        SourceStatus.USER_CONFIRMED,
        "pre_restore",
        patterns=list(target.source.get("patterns") or []),  # D65 2: only those files
        excludes=list(target.source.get("excludes") or []),
    )


def take_safety_snapshot(plan: RestorePlan) -> str | None:
    """D10: back up every non-empty target first; any failure aborts the restore (exit 3).
    A target that cannot be listed whole -- a folder that cannot be read, a link or junction
    inside it -- aborts it too (1.0.0, D72 R03): it is not an empty folder, and what is behind
    the link would be overwritten with no safety snapshot."""
    protect = []
    for t in plan.targets:
        if not t.target_dir.is_dir():
            continue
        files, errors = source_files(t.target_dir, *_selection(t.source))
        if errors:
            said = "; ".join(f"{e.rel}: {e.error}" if hasattr(e, "rel") else str(e) for e in errors)
            raise FatalError(
                f"{t.target_dir} cannot be listed whole ({said[:300]}); nothing was restored"
            )
        if files:
            protect.append(t)
    if not protect:
        return None
    planned = PlannedGame(
        plan.kind,
        plan.display_name,
        None,
        [PlannedSource(_safety_source(t), []) for t in protect],
        game_id=plan.game_id,
    )
    outcome = backup_game(plan.repo, plan.repo.load_catalog(), planned, reason="pre_restore")
    if outcome.status != "ok":
        detail = outcome.message or f"{len(outcome.errors)} file errors"
        raise FatalError(f"safety snapshot failed ({detail}); nothing was restored")
    return outcome.snapshot_id


def link_inside(target_dir: Path, path: Path) -> Path | None:
    """The first folder or file between ``target_dir`` (not itself) and ``path`` that exists
    and is a link or junction (a name-surrogate reparse point), or ``None`` (D72 R03: a write
    goes into the target, never through a link inside it to somewhere else; a cloud
    placeholder is an ordinary file)."""
    current = target_dir
    for part in path.relative_to(target_dir).parts:
        current = current / part
        if not os.path.lexists(long_path(current)):
            return None
        if is_reparse_point(current):
            return current
    return None


def restore_file(data_dir: Path, target_dir: Path, item: dict[str, Any]) -> None:
    source = safe_join(data_dir, item["path"])
    target = safe_join(target_dir, item["path"])
    os.makedirs(long_path(target.parent), exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    tmp, out = own_temp_file(target, TMP_SUFFIX)  # D72 R02: ours alone, never a stray file
    try:
        with out as fout, open(long_path(source), "rb") as fin:  # ours closes either way
            while chunk := fin.read(CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        if (digest.hexdigest(), size) != (item["sha256"], item["size"]):
            raise RestoreError("stored copy does not match the manifest (run verify)")
        with contextlib.suppress(ValueError):
            mtime = ns_from_iso(str(item.get("mtime")))
            os.utime(long_path(tmp), ns=(mtime, mtime))
        os.replace(long_path(tmp), long_path(target))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(long_path(tmp))
        raise
    if hash_file(target) != (item["sha256"], item["size"]):
        raise RestoreError("restored file failed verification")


def execute_restore(plan: RestorePlan, *, safety_snapshot: bool = True) -> RestoreResult:
    plan.repo.require_writable()  # D67 8: it records the restore in the library
    result = RestoreResult()
    if safety_snapshot:
        result.safety_snapshot = take_safety_snapshot(plan)
    snapshot_dir = plan.repo.snapshot_dir(plan.game_id, plan.snapshot_id)
    manifest = plan.repo.load_manifest(plan.game_id, plan.snapshot_id)
    for target in plan.targets:
        data_dir = layout.source_data_dir(snapshot_dir, manifest, target.source_id)  # D67 3
        links = {
            str(found)
            for item in target.files
            if (found := link_inside(target.target_dir, safe_join(target.target_dir, item["path"])))
        }
        if links:  # D72 R03: the whole source is refused before its first write
            error = f"a link or junction inside the target is not followed: {sorted(links)[0]}"
            result.failures += [
                {"source_id": target.source_id, "path": item["path"], "error": error}
                for item in target.files
            ]
            continue
        for item in target.files:
            try:
                restore_file(data_dir, target.target_dir, item)
                result.written += 1
            except (RestoreError, OSError) as exc:
                error = str(exc) if isinstance(exc, RestoreError) else describe_error(exc)
                result.failures.append(
                    {"source_id": target.source_id, "path": item["path"], "error": error}
                )
    result.registered_install = register_install(plan, result)
    layout.write_index(plan.repo)  # D67 5: the game folder is there again
    return result


def register_install(plan: RestorePlan, result: RestoreResult) -> str | None:
    """Once the local saves went into ``plan.game_root`` without a failure, that folder is an
    install of the record (D43): added to ``installs``, or its ``removed_at`` cleared, its
    ``last_seen`` and version updated, so the record shows as installed again. A restore of
    global saves only registers nothing."""
    local = [t for t in plan.targets if t.source.get("root_token") == RootToken.GAME_ROOT.value]
    failed = {f["source_id"] for f in result.failures}
    if plan.game_root is None or not local or any(t.source_id in failed for t in local):
        return None
    now = iso_utc(utc_now())
    doc = plan.repo.load_game(plan.game_id)
    install = {"game_root": str(plan.game_root), "config_version": plan.target_version}
    touch_install(doc.setdefault("installs", []), install, now)
    doc["updated_at"] = now
    plan.repo.save_game(doc)
    update_catalog_entry(plan.repo, plan.repo.load_catalog(), doc)
    return str(plan.game_root)
