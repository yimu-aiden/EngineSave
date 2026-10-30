"""Several at once (D27): the latest snapshot of several records into a folder, and every
zip in a folder into a repository."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from enginesave.engines.renpy.paths import default_saves_root
from enginesave.engines.renpy.paths import saves_root_override as env_saves_root_override
from enginesave.errors import EngineSaveError, StopCheck, UsageError
from enginesave.export.archive import check_export_target, default_export_name, export_snapshot
from enginesave.export.importing import (
    ALREADY_IMPORTED,
    ImportKey,
    ImportPlan,
    execute_import,
    known_imports,
    plan_import,
)
from enginesave.repo import Repo

# ---------------------------------------------------------------------------- batches (D27)

EXPORTED, IMPORTED, SKIPPED, FAILED = "exported", "imported", "skipped", "failed"
Progress = Callable[[int, int, str], None]


@dataclass
class BatchResult:
    """One item per record (export) or per archive (import), in the order they were done."""

    kind: str  # "export" | "import"
    location: str  # the export folder, or the folder (or archive) imported from
    items: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    cancelled: bool = False  # D58: stopped between archives; ``items`` are the ones done

    @property
    def outcomes(self) -> tuple[str, ...]:
        return (EXPORTED, SKIPPED, FAILED) if self.kind == "export" else (IMPORTED, SKIPPED, FAILED)

    def count(self, outcome: str) -> int:
        return sum(1 for item in self.items if item["result"] == outcome)

    @property
    def exit_code(self) -> int:
        return 1 if self.count(FAILED) else 0

    def to_dict(self) -> dict[str, Any]:
        counts = {outcome: self.count(outcome) for outcome in self.outcomes}
        data = {"kind": self.kind, "location": self.location, **counts, "items": self.items}
        if self.cancelled:  # 0.9.0: only when it was stopped
            data["cancelled"] = True
        return data


def export_records(
    repo: Repo,
    game_ids: Sequence[str],
    folder: Path,
    *,
    overwrite: bool = False,
    include_tokens: bool = False,
    keep_paths: bool = False,
    forbidden_roots: Sequence[Path] = (),
    saves_root_override: Path | None = None,
    env: Mapping[str, str] | None = None,
    progress: Progress | None = None,
    should_stop: StopCheck = None,
) -> BatchResult:
    """Export the ``latest`` snapshot of every record into ``folder``, one verified zip each.

    Files get the default name; an existing file is skipped and reported unless ``overwrite``,
    and two records that would get the same name in one batch are told apart by their game id.
    A record without a manual or imported snapshot is skipped. One failed export (removed, as
    for a single export) does not stop the others. The folder must not be inside the library,
    the Ren'Py saves root or ``forbidden_roots`` (UsageError before anything is written).
    """
    env = dict(os.environ) if env is None else env
    folder = Path(os.path.abspath(folder))
    if folder.exists() and not folder.is_dir():
        raise UsageError(f"{folder} is a file; give a folder for the exports")
    catalog = repo.load_catalog()
    chosen: dict[str, dict[str, Any]] = {}  # an unknown record fails before anything is written
    for wanted in game_ids:
        record = repo.resolve_game(catalog, wanted)
        chosen.setdefault(record["game_id"], record)
    records = list(chosen.values())
    if not records:
        raise UsageError("no records to export")
    common = [default_saves_root(env, None)[0], *forbidden_roots]
    if override := saves_root_override or env_saves_root_override(None, env):
        common.append(override)
    check_export_target(folder / "export.zip", repo, common)
    batch = BatchResult("export", str(folder))
    names: set[str] = set()
    for index, record in enumerate(records):
        if should_stop is not None and should_stop():  # D58: between archives
            batch.cancelled = True
            break
        game_id = str(record["game_id"])
        name = str(record.get("display_name") or game_id)
        if progress is not None:
            progress(index, len(records), name)
        item: dict[str, Any] = {
            "game_id": game_id,
            "display_name": name,
            "snapshot_id": None,
            "path": None,
            "result": FAILED,
            "reason": None,
            "export": None,
        }
        batch.items.append(item)
        snapshot_id = repo.latest_snapshot(game_id)
        if snapshot_id is None:
            item["result"], item["reason"] = SKIPPED, "no manual or imported snapshot"
            continue
        file_name = default_export_name(name, snapshot_id)
        if file_name.casefold() in names:
            file_name = f"{file_name.removesuffix('.zip')}-{game_id[:8]}.zip"
        names.add(file_name.casefold())
        dest = folder / file_name
        item["snapshot_id"], item["path"] = snapshot_id, str(dest)
        if dest.exists() and not overwrite:
            item["result"], item["reason"] = SKIPPED, "the file already exists"
            continue
        try:
            done = export_snapshot(
                repo,
                game_id,
                snapshot_id,
                dest,
                overwrite=overwrite,
                include_tokens=include_tokens,
                keep_paths=keep_paths,
                forbidden_roots=forbidden_roots,
                saves_root_override=saves_root_override,
                env=env,
            )
        except EngineSaveError as exc:
            item["reason"] = str(exc)
            continue
        item["result"], item["export"] = EXPORTED, done.to_dict()
    return batch


def zip_archives(folder: Path) -> list[Path]:
    """The ``*.zip`` files directly inside ``folder`` (not its subfolders), sorted by name."""
    folder = Path(os.path.abspath(folder))
    if not folder.is_dir():
        raise UsageError(f"not an existing folder: {folder}")
    found = [p for p in folder.iterdir() if p.suffix.lower() == ".zip" and p.is_file()]
    if not found:
        raise UsageError(f"no .zip files in {folder}")
    return sorted(found, key=lambda p: (p.name.casefold(), p.name))


def preview_imports(
    repo_root: Path, archives: Sequence[Path]
) -> list[tuple[Path, ImportPlan | None, str | None]]:
    """``(archive, plan, error)`` for each archive; reads only. A broken one has an error;
    a plan's ``already_imported`` says the library holds that exported snapshot (D31), its
    ``duplicate_of`` names an earlier archive of the batch with the same snapshot (S-098):
    both are skipped by :func:`import_archives`, so the preview counts what really happens."""
    try:
        imported = known_imports(repo_root)
    except EngineSaveError:
        imported = {}  # every plan below reports why the library cannot be used
    previews: list[tuple[Path, ImportPlan | None, str | None]] = []
    first_copy: dict[ImportKey, str] = {}
    for archive in archives:
        try:
            plan = plan_import(repo_root, archive, imported=imported)
        except EngineSaveError as exc:
            previews.append((archive, None, str(exc)))
            continue
        key = plan.source_key
        if key is not None and plan.already_imported is None:
            plan.duplicate_of = first_copy.setdefault(key, archive.name)
            if plan.duplicate_of == archive.name:
                plan.duplicate_of = None
        previews.append((archive, plan, None))
    return previews


def import_archives(
    repo_root: Path,
    archives: Sequence[Path],
    *,
    reimport: bool = False,
    location: Path | None = None,
    progress: Progress | None = None,
    should_stop: StopCheck = None,
) -> BatchResult:
    """Import each archive in turn; one that fails does not stop the others.

    Every archive is planned again right before it is imported, so one that an earlier archive
    in the batch gave a record joins that record by the usual rules. An exported snapshot the
    library already holds as an import is skipped and reported, not counted as a failure,
    unless ``reimport`` (D31) — this includes a second copy of an archive in the same batch.
    """
    where = location or (archives[0].parent if archives else repo_root)
    batch = BatchResult("import", str(where))
    imported: dict[ImportKey, dict[str, str]] | None = None
    for index, archive in enumerate(archives):
        if should_stop is not None and should_stop():  # D58: between archives
            batch.cancelled = True
            break
        if progress is not None:
            progress(index, len(archives), archive.name)
        item: dict[str, Any] = {
            "archive": str(archive),
            "result": FAILED,
            "reason": None,
            "error": None,
            "plan": None,
            "import": None,
        }
        batch.items.append(item)
        try:
            if imported is None:
                imported = known_imports(repo_root)
            plan = plan_import(repo_root, archive, imported=imported)
            item["plan"] = plan.to_dict()
            if plan.already_imported is not None and not reimport:
                item["result"], item["reason"] = SKIPPED, ALREADY_IMPORTED
                continue
            done = execute_import(plan)
        except EngineSaveError as exc:
            item["error"] = str(exc)
            continue
        item["result"], item["import"] = IMPORTED, done.to_dict()
        if plan.source_key is not None:
            imported[plan.source_key] = {"game_id": done.game_id, "snapshot_id": done.snapshot_id}
    return batch
