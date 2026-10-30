"""Snapshot integrity checks (plan 9.1): every manifest file re-hashed; extras reported."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from enginesave import layout
from enginesave.errors import FatalError, StopCheck, Stopped, UsageError
from enginesave.fsutil import (
    UnsafePathError,
    describe_error,
    long_path,
    path_key,
    sha256_file,
    walk_files,
)
from enginesave.repo import DATA_DIR, OBJECT_RE, Repo


@dataclass
class SnapshotCheck:
    game_id: str
    display_name: str
    snapshot_id: str
    snapshot_status: str = "unknown"  # manifest "status": complete | partial
    reason: str = "unknown"
    ok: int = 0
    mismatched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.mismatched or self.missing or self.extra or self.problems)

    def to_dict(self) -> dict[str, Any]:
        return {**vars(self), "clean": self.clean}


def verify_snapshot(repo: Repo, game_id: str, snapshot_id: str, name: str = "") -> SnapshotCheck:
    check = SnapshotCheck(game_id, name or game_id, snapshot_id)
    snapshot_dir = repo.snapshot_dir(game_id, snapshot_id)
    try:
        manifest = repo.load_manifest(game_id, snapshot_id)
    except FatalError as exc:
        check.problems.append(str(exc))
        return check
    check.snapshot_status = str(manifest.get("status"))
    check.reason = str(manifest.get("reason"))
    data_dir = snapshot_dir / DATA_DIR
    expected: set[str] = set()
    for item in manifest.get("files", []):
        label = f"{item.get('source_id')}/{item.get('path')}"
        try:
            path = layout.snapshot_file_path(snapshot_dir, manifest, item)  # D67 3
        except (KeyError, UnsafePathError) as exc:
            check.problems.append(f"unsafe or malformed manifest entry {label}: {exc}")
            continue
        expected.add(path_key(path))
        if not os.path.isfile(long_path(path)):  # M46 review: snapshot paths are long now
            check.missing.append(label)
            continue
        try:
            digest, size = sha256_file(path)
        except OSError as exc:
            check.mismatched.append(f"{label} ({describe_error(exc)})")
            continue
        if size != item.get("size") or digest != item.get("sha256"):
            check.mismatched.append(label)
        else:
            check.ok += 1
    # a snapshot of no files may have no data folder at all: nothing to list then
    stored, errors = walk_files(data_dir) if os.path.lexists(long_path(data_dir)) else ([], [])
    check.extra = [f.rel for f in stored if path_key(f.path) not in expected]
    # 1.0.0 (D72 R13): what could not be listed is not known to be clean
    check.problems += [
        f"cannot list {getattr(e, 'rel', '') or DATA_DIR}: {getattr(e, 'error', e)}"
        for e in errors[:20]
    ]
    return check


@dataclass
class ObjectCheck:
    """``verify --objects``: every object's file name must equal its content hash (D18)."""

    ok: int = 0
    mismatched: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)  # not a digest, or in the wrong bucket

    @property
    def clean(self) -> bool:
        return not (self.mismatched or self.unexpected)

    def to_dict(self) -> dict[str, Any]:
        return {**vars(self), "clean": self.clean}


def verify_objects(repo: Repo, *, should_stop: StopCheck = None) -> ObjectCheck:
    """Re-hash every object of the store. Told to stop (D58), it raises :class:`Stopped`
    between objects with the check so far as ``partial``."""
    check = ObjectCheck()
    for name, path in repo.iter_objects():
        if should_stop is not None and should_stop():
            raise Stopped(check)
        if not OBJECT_RE.match(name) or path.parent.name != name[:2]:
            check.unexpected.append(f"{path.parent.name}/{name}")
            continue
        try:
            digest, _ = sha256_file(path)
        except OSError as exc:
            check.mismatched.append(f"{name} ({describe_error(exc)})")
            continue
        if digest == name:
            check.ok += 1
        else:
            check.mismatched.append(name)
    return check


def verify_repo(
    repo: Repo,
    catalog: dict,
    game_ref: str | None = None,
    snapshot_id: str | None = None,
    *,
    should_stop: StopCheck = None,
) -> list[SnapshotCheck]:
    """Check one snapshot, every snapshot of one game, or the whole repository. Told to stop
    (D58), it raises :class:`Stopped` between snapshots with the checks done as ``partial``."""
    if snapshot_id and not game_ref:
        raise UsageError("--snapshot requires --game")
    records = [repo.resolve_game(catalog, game_ref)] if game_ref else catalog["games"]
    checks: list[SnapshotCheck] = []
    for record in records:
        game_id = record["game_id"]
        name = record.get("display_name") or game_id
        ids = [snapshot_id] if snapshot_id else repo.snapshot_ids(game_id)
        if snapshot_id and snapshot_id not in repo.snapshot_ids(game_id):
            raise UsageError(f"no snapshot {snapshot_id} for {name}")
        for sid in ids:
            if should_stop is not None and should_stop():
                raise Stopped(partial=checks)
            checks.append(verify_snapshot(repo, game_id, sid, name))
    return checks
