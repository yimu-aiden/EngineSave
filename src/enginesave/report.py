"""Console rendering (English, stable layout for snapshot-style tests) and JSON helpers."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enginesave import __version__
from enginesave.models import (
    LOAD_NOTE,
    DetectedGame,
    GameStatus,
    MetadataState,
    RootToken,
    SaveSource,
    ScanReport,
    SourceKind,
)

if TYPE_CHECKING:
    from enginesave.backup import BackupPlan, BackupResult
    from enginesave.cleanup import CleanupPlan, CleanupResult
    from enginesave.export import BatchResult, ExportResult, ImportPlan, ImportResult
    from enginesave.restore import RestorePlan, RestoreResult
    from enginesave.verify import ObjectCheck, SnapshotCheck


PATH_WIDTH = 44
SYNC_NOTE = "includes sync/ (engine mirror)"
HASH_NOTE_PREFIX = "identical by hash:"


def to_json(data: Any) -> str:
    """Pretty JSON for report files (UTF-8 text, non-ASCII kept)."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            break
    return f"{value:.1f} {unit}"


def abbreviate(path: str, width: int = PATH_WIDTH) -> str:
    """``C:\\Users\\u\\AppData\\Roaming\\RenPy\\X`` → ``C:\\...\\RenPy\\X`` when too long."""
    if len(path) <= width:
        return path
    sep = "\\" if "\\" in path else "/"
    prefix = path[: len(path) - len(path.lstrip(sep))]
    parts = [p for p in path.split(sep) if p]
    if len(parts) <= 3:
        return path
    return f"{prefix}{parts[0]}{sep}...{sep}{parts[-2]}{sep}{parts[-1]}"


def source_display_path(source: SaveSource) -> str:
    if source.root_token is RootToken.GAME_ROOT:
        return source.relative_root.replace("/", os.sep)
    return abbreviate(source.path)


def source_line(source: SaveSource) -> str:
    line = (
        f"  {source.source_id:<10} {source.status.value:<15} "
        f"{source_display_path(source):<{PATH_WIDTH}} {source.file_count:>5} files "
        f"{human_size(source.total_bytes):>9}"
    )
    if source.kind is SourceKind.GLOBAL and source.mirror_of:
        line += f"  mirror of: {', '.join(source.mirror_of)}"
        if SYNC_NOTE in source.notes:
            line += f" (incl. sync{os.sep})"
    for note in source.notes:
        if note.startswith(HASH_NOTE_PREFIX):
            line += f"  {note}"
    return line


def _save_directory_evidence(game: DetectedGame) -> str:
    labels: list[str] = []
    for e in game.evidence:
        if "config.save_directory" in e.detail:
            label = f"{e.file}:{e.line}" if e.line else e.file
            if label not in labels:
                labels.append(label)
    return ", ".join(labels)


def render_game(game: DetectedGame) -> list[str]:
    version = f" ({game.identity.config_version})" if game.identity.config_version else ""
    engine = f"{game.engine} {game.engine_version or '(version unknown)'}"
    lines = [
        f"[{game.status.value}]  {game.display_name}{version}   {engine}",
        f"  root:      {game.game_root}",
    ]
    if len(game.installs) > 1:
        lines.append(f"  installs:  {len(game.installs)}")
        for install in game.installs:
            suffix = f" ({install['config_version']})" if install.get("config_version") else ""
            lines.append(f"    - {install['game_root']}{suffix}")
    if game.engine == "renpy" and game.status is not GameStatus.UNSUPPORTED:
        if game.identity.renpy_save_directory:
            sd = game.identity.renpy_save_directory
        elif game.metadata_state is MetadataState.RESOLVED:
            sd = "<none> (local only)"
        else:
            sd = "<unresolved>"
        evidence = _save_directory_evidence(game)
        identity = f"  identity:  save_directory={sd}"
        if evidence:
            identity += f"  ({evidence})"
        if game.metadata_state is not MetadataState.RESOLVED:
            identity += f"  [metadata: {game.metadata_state.value}]"
        lines.append(identity)
    if game.known_game_id:
        lines.append(f"  known as:  {game.known_game_id}")
    for suggestion in game.link_suggestions:
        lines.append(f"  suggest:   link to {suggestion['game_id']} ({suggestion['reason']})")
    lines.extend(source_line(source) for source in game.sources)
    for source in game.sources:
        for note in source.notes:
            if note != SYNC_NOTE and not note.startswith(HASH_NOTE_PREFIX):
                lines.append(f"    note ({source.source_id}): {note}")
    lines.extend(f"  warning:   {w}" for w in game.warnings)
    return lines


def skipped_summary(skipped: list[dict], examples: int = 3) -> str:
    """One line for the skipped folders: how many per reason, and the first few (D34)."""
    counts = Counter(str(item["reason"]) for item in skipped)
    reasons = ", ".join(f"{reason} {count}" for reason, count in counts.most_common())
    shown = "; ".join(str(item["path"]) for item in skipped[:examples])
    first = f" (first {examples})" if len(skipped) > examples else ""
    return f"{reasons}; e.g. {shown}{first}"


def timing_lines(timing: dict) -> list[str]:
    """``Scan time: …`` per scan root, and the slowest folders of a scan that took minutes."""
    if not timing:
        return []
    roots = "; ".join(f"{r['root']} {r['seconds']:.1f} s" for r in timing.get("roots", []))
    lines = [f"Scan time: {timing['total_seconds']:.1f} s" + (f" ({roots})" if roots else "")]
    if timing.get("slowest"):
        lines.append("Slowest folders (listing and detection, not their subfolders):")
        lines.extend(f"  {s['seconds']:7.1f} s  {s['path']}" for s in timing["slowest"])
    return lines


def render_scan(report: ScanReport, verbose: bool = False) -> str:
    lines = [
        f"EngineSave {report.tool_version} - scan",
        f"Roots: {'; '.join(report.roots)}",
        f"Ren'Py saves root: {report.renpy_saves_root}",
        "",
    ]
    for game in report.games:
        lines.extend(render_game(game))
        lines.append("")
    if report.unlinked:
        lines.append(f"Unlinked global save directories ({len(report.unlinked)}):")
        width = min(48, max(34, *(len(item.name) for item in report.unlinked)))
        for item in report.unlinked:
            line = (
                f"  {item.name:<{width}} {item.file_count:>5} files "
                f"{human_size(item.total_bytes):>9}"
                f"   last modified {item.last_modified[:10]}"
            )
            if item.version_hint:
                line += f"   version hint: {item.version_hint}"
            if item.known_game_id:
                line += f"   known as: {item.known_game_id}"
            lines.append(line)
        lines.append("")
    warning_count = len(report.warnings) + sum(len(g.warnings) for g in report.games)
    lines.append(f"Skipped: {len(report.skipped)}   Warnings: {warning_count}")
    if verbose:
        lines.extend(f"  skipped:   {s['path']} ({s['reason']})" for s in report.skipped)
    elif report.skipped:  # one line, not one per folder (D34)
        lines.append(f"  skipped:   {skipped_summary(report.skipped)}  (--verbose lists all)")
    lines.extend(f"  warning:   {w}" for w in report.warnings)
    lines.extend(timing_lines(report.timing))
    counts = report.summary()
    lines.append(
        "Summary: "
        f"{counts['CONFIRMED']} confirmed, {counts['CANDIDATE']} candidate, "
        f"{counts['NOT_FOUND']} not found, {counts['CONFLICT']} conflict, "
        f"{counts['UNSUPPORTED']} unsupported, {counts['UNLINKED']} unlinked"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------------- backup & repository


def render_backup_plan(plan: BackupPlan) -> str:
    state = "new repository" if plan.repo_state == "new" else "existing repository"
    lines = [
        f"EngineSave {__version__} - backup plan",
        f"Destination: {plan.dest} ({state})",
        "",
    ]
    total_files = total_bytes = 0
    for game in plan.games:
        version = f" ({game.version})" if game.version else ""
        target = f"known game {game.game_id}" if game.game_id else "new game record"
        lines.append(f"[{game.kind}]  {game.display_name}{version}   -> {target}")
        for ps in game.sources:
            files, size = len(ps.files), ps.total_bytes
            total_files += files
            total_bytes += size
            lines.append(
                f"  {ps.source.source_id:<15} {ps.source.status.value:<15} "
                f"{files:>5} files {human_size(size):>9}  {ps.source.path}"
            )
            lines.extend(f"    note: {note}" for note in ps.source.notes)
            if ps.walk_errors:
                lines.append(f"    {len(ps.walk_errors)} entries cannot be copied (reparse points)")
        lines.append("")
    if plan.skipped:
        lines.append(f"Not backed up ({len(plan.skipped)}):")
        lines.extend(f"  {s['name']}: {s['reason']}" for s in plan.skipped)
        lines.append("")
    lines.append(
        f"Total: {len(plan.games)} to back up, {total_files} files, {human_size(total_bytes)}"
    )
    return "\n".join(lines)


def render_backup_result(result: BackupResult, repo_root: Path) -> str:
    lines = [f"Backup summary ({repo_root}):"]
    for r in result.results:
        label = {"ok": "OK", "partial": "PARTIAL", "failed": "FAILED"}.get(
            r.status, r.status.upper()
        )  # 1.2.0 (D73 2): UNCHANGED
        detail = (
            f"snapshot {r.snapshot_id}  {r.file_count} files  {human_size(r.total_bytes)}"
            if r.snapshot_id
            else "no snapshot written"
        )
        lines.append(f"  {label:<8} {r.display_name}  {detail}")
        if r.message:
            lines.append(f"           {r.message}")
        lines.extend(
            f"           error: {e['source_id']}/{e['path']}: {e['error']}" for e in r.errors
        )
    for s in result.skipped:
        lines.append(f"  {'SKIPPED':<8} {s['name']}  ({s['reason']})")
    counts = {
        k: sum(1 for r in result.results if r.status == k) for k in ("ok", "partial", "failed")
    }
    unchanged = sum(1 for r in result.results if r.status == "unchanged")
    lines.append(
        f"Result: {counts['ok']} ok, {counts['partial']} partial, {counts['failed']} failed, "
        f"{len(result.skipped)} skipped" + (f", {unchanged} unchanged" if unchanged else "")
    )
    if result.pruned is not None and result.pruned["removed"]:  # D73 3
        count = sum(len(item["snapshot_ids"]) for item in result.pruned["removed"])
        lines.append(
            f"Removed {count} older auto snapshot(s) (--keep-auto {result.pruned['keep']})"
        )
    return "\n".join(lines)


def render_list(data: dict[str, Any]) -> str:
    lines = [f"EngineSave {__version__} - list", f"Repository: {data['repository']}", ""]
    for row in data["games"]:
        count = row["snapshot_count"]
        noun = "snapshot " if count == 1 else "snapshots"
        latest = f"latest {row['latest_snapshot']}" if row["latest_snapshot"] else "no snapshots"
        removed = f"  game removed {row['cleaned_at'][:10]}" if row.get("cleaned_at") else ""
        group = "  installed" if row.get("install_present") else "  backup only"  # D43
        lines.append(
            f"  {row['game_id']}  {row['kind']:<8}  {row['display_name']:<34} "
            f"{count:>3} {noun}  {latest}{removed}{group}"
        )
    lines.extend(f"  warning: leftover temporary folder {p}" for p in data["stale_tmp_dirs"])
    lines.extend(
        f"  warning: record folder not in the catalog {p}"
        for p in data.get("orphan_record_dirs", [])
    )
    total = sum(row["snapshot_count"] for row in data["games"])
    lines.append("")
    lines.append(f"Records: {len(data['games'])}, snapshots: {total}")
    stats = data.get("stats")
    if stats:  # 0.9.0 (D56)
        lines.append(
            f"stored {human_size(stats['stored_bytes'])} "
            f"(logical {human_size(stats['logical_bytes'])})"
        )
    return "\n".join(lines)


def render_show_game(doc: dict[str, Any], snapshots: list[dict[str, Any]]) -> str:
    identity = doc.get("identity") or {}
    engine = f"{doc['engine']} {doc.get('engine_version') or ''}".rstrip()
    lines = [
        f"{doc['display_name']}  [{doc['kind']}]  {engine}",
        f"  game id:         {doc['game_id']}",
        f"  save directory:  {identity.get('renpy_save_directory')}",
        f"  config.name:     {identity.get('config_name')}",
        f"  config.version:  {identity.get('config_version')}",
    ]
    for install in doc.get("installs", []):
        lines.append(
            f"  install:         {install['game_root']}  ({install.get('config_version')}, "
            f"last seen {install.get('last_seen')})"
        )
    for t in doc.get("source_templates", []):
        lines.append(
            f"  source:          {t['source_id']:<15} {t['root_token']:<17} {t['relative_root']}"
        )
    lines.append("")
    lines.append(f"Snapshots ({len(snapshots)}):")
    for s in snapshots:
        if s.get("status") == "unreadable":
            lines.append(f"  {s['snapshot_id']}  unreadable manifest")
            continue
        size = human_size(s["total_bytes"])
        lines.append(
            f"  {s['snapshot_id']:<20} {s['reason']:<12} {s['status']:<9} "
            f"{s['file_count']:>5} files {size:>9}  errors: {s['error_count']}"
            + (f"  storage: {s['storage']}" if "storage" in s else "")
        )
    return "\n".join(lines)


def render_show_snapshot(doc: dict[str, Any], manifest: dict[str, Any]) -> str:
    lines = [
        f"{doc['display_name']}  snapshot {manifest['snapshot_id']}",
        f"  created:  {manifest.get('created_at')}  by EngineSave {manifest.get('tool_version')}",
        f"  reason:   {manifest.get('reason')}",
        f"  status:   {manifest.get('status')}",
        "",
        "Sources:",
    ]
    for s in manifest.get("sources", []):
        lines.append(
            f"  {s['source_id']:<15} {s['kind']:<15} {s['file_count']:>5} files "
            f"{human_size(s['total_bytes']):>9}  {s['consistency']}"
        )
        lines.append(f"    from {s['origin_path']}  ({s['root_token']}: {s['relative_root']})")
    errors = manifest.get("errors", [])
    lines.append("")
    lines.append(f"Errors ({len(errors)}):")
    lines.extend(f"  {e['source_id']}/{e['path']}: {e['error']}" for e in errors)
    return "\n".join(lines)


def render_restore_plan(plan: RestorePlan) -> str:
    manifest = plan.manifest
    lines = [
        f"EngineSave {__version__} - restore plan",
        f"Game: {plan.display_name}  ({plan.game_id})",
        f"Snapshot: {plan.snapshot_id}  ({manifest.get('reason')}, {manifest.get('status')})",
    ]
    if plan.game_root is not None:
        lines.append(f"Game folder: {plan.game_root}")
    lines += [f"Note: {note}" for note in plan.notes]
    lines.append("")
    for t in plan.targets:
        lines.append(f"  {t.source_id:<15} -> {t.target_dir}  ({t.resolved_by})")
        created = ", folder will be created" if t.creates_dir else ""
        lines.append(
            f"  {'':<15}    {len(t.files)} files to write, {t.overwrites} overwrite existing "
            f"files, {t.extra_in_target} other files in the target are kept{created}"
        )
    lines.append("")
    lines.append("Files that exist in a target but not in the snapshot are never deleted.")
    lines.append("Non-empty targets are backed up first (snapshot reason pre_restore).")
    if plan.includes_tokens:
        lines.append("WARNING: this restores tokens/ (Ren'Py's save-signing private keys).")
    return "\n".join(lines)


def render_restore_result(result: RestoreResult) -> str:
    lines = [
        "Restore summary:",
        f"  restored {result.written} files, {len(result.failures)} failed",
    ]
    lines.extend(f"  failed: {f['source_id']}/{f['path']}: {f['error']}" for f in result.failures)
    if result.safety_snapshot:
        lines.append(f"  safety snapshot: {result.safety_snapshot} (reason pre_restore)")
    else:
        lines.append("  safety snapshot: none needed (all targets were empty or absent)")
    if result.registered_install:  # D43: the record shows as installed again
        lines.append(f"  install recorded: {result.registered_install}")
    if result.failures:
        lines.append("  some files were not restored; the files listed above are unchanged")
    else:
        lines.append(f"  note: {LOAD_NOTE}")
    return "\n".join(lines)


def render_export_result(result: ExportResult) -> str:
    lines = [
        f"EngineSave {__version__} - export",
        f"Game: {result.display_name}  ({result.game_id})",
        f"Snapshot: {result.snapshot_id}",
        f"Wrote {result.path}",
        f"  {result.file_count} files, {human_size(result.total_bytes)} of saves; archive "
        f"{human_size(result.archive_bytes)}; every file verified against the manifest",
    ]
    if result.paths == "anonymized":
        lines.append(
            "  paths: anonymized (folders appear as GAME_ROOT/..., RENPY_SAVES_ROOT/... and the "
            "other root tokens; "
            "add --keep-paths to keep this computer's paths)"
        )
    else:
        lines.append("  paths: original (the archive contains this computer's folder paths)")
    keys = [s for s, why in result.excluded_reasons.items() if why == "private_keys"]
    absolute = [s for s, why in result.excluded_reasons.items() if why == "absolute_path"]
    if keys:
        lines.append(
            f"  not included: {', '.join(keys)} (Ren'Py's save-signing keys; "
            f"add --include-tokens to include them)"
        )
    if absolute:
        lines.append(
            f"  not included: {', '.join(absolute)} (save folders known only by an absolute "
            f"path; add --keep-paths to include them)"
        )
    if result.includes_tokens:
        lines.append(
            "  WARNING: the archive contains tokens/ (Ren'Py's save-signing private keys); "
            "do not share it."
        )
    return "\n".join(lines)


def already_imported_text(plan: ImportPlan) -> str:
    done = plan.already_imported or {}
    return f"already imported as snapshot {done.get('snapshot_id')} of {done.get('game_id')}"


def render_import_plan(plan: ImportPlan, reimport: bool = False) -> str:
    manifest = plan.manifest
    header = plan.header
    record = {
        "game_id": f"existing record {plan.game_id} (same game id)",
        "identity": f"existing record {plan.game_id} (same save directory or install folder)",
        None: f"new record {plan.game_id}",
    }[plan.matched_by]
    lines = [
        f"EngineSave {__version__} - import plan",
        f"Archive: {plan.zip_path}",
        f"Game: {plan.display_name}  (exported {header.get('exported_at')} by EngineSave "
        f"{header.get('tool_version')})",
        f"Snapshot: {header['snapshot_id']}  ({manifest.get('reason')}, {manifest['status']})",
        f"Repository: {plan.repo_root}",
        f"Record: {record}",
    ]
    if plan.already_imported is not None:
        then = "importing it again (--reimport)" if reimport else "skipped unless --reimport"
        lines.append(f"Note: {already_imported_text(plan)}; {then}")
    lines.append("")
    for source in manifest["sources"]:
        files = [f for f in manifest["files"] if f["source_id"] == source["source_id"]]
        size = human_size(sum(f["size"] for f in files))
        lines.append(
            f"  {source['source_id']:<15} {source['kind']:<15} {len(files):>5} files {size:>9}"
        )
    total = human_size(sum(f["size"] for f in manifest["files"]))
    lines.append("")
    lines.append(
        f"Total: {len(manifest['files'])} files, {total}; stored as a new snapshot "
        f"(reason import). Nothing outside the repository is written."
    )
    if plan.ignored_members:
        lines.append(
            f"note: {plan.ignored_members} data files are not in the manifest and are ignored"
        )
    if plan.includes_tokens:
        lines.append("WARNING: the archive contains tokens/ (Ren'Py's save-signing private keys).")
    return "\n".join(lines)


def render_import_skipped(plan: ImportPlan) -> str:
    return (
        f"Import summary ({plan.repo_root}):\n"
        f"  SKIPPED  {plan.display_name}  snapshot {plan.header['snapshot_id']}: "
        f"{already_imported_text(plan)}; nothing was written (--reimport imports it again)"
    )


def render_import_result(result: ImportResult, repo_root: Path) -> str:
    record = "new record" if result.created_record else "existing record"
    return "\n".join(
        [
            f"Import summary ({repo_root}):",
            f"  OK       {result.display_name}  snapshot {result.snapshot_id}  "
            f"{result.file_count} files  {human_size(result.total_bytes)}  ({record}, storage "
            f"{result.storage})",
            f'  to restore: enginesave restore --repo "{repo_root}" --game {result.game_id} '
            f"--snapshot {result.snapshot_id} --dry-run",
        ]
    )


BATCH_LABELS = {"exported": "OK", "imported": "OK", "skipped": "SKIPPED", "failed": "FAILED"}


def render_export_batch(result: BatchResult) -> str:
    """One line per record, then the counts and what the archives leave out by default."""
    lines = [f"Export summary ({result.location}):"]
    anonymized = tokens = 0
    for item in result.items:
        line = f"  {BATCH_LABELS[item['result']]:<8} {item['display_name']}"
        if item["snapshot_id"]:
            line += f"  snapshot {item['snapshot_id']}"
        if item["export"]:
            done = item["export"]
            line += f"  {done['file_count']} files  {human_size(done['archive_bytes'])}"
            anonymized += done["paths"] == "anonymized"
            tokens += bool(done["includes_tokens"])
        if item["path"]:
            line += f"  -> {Path(item['path']).name}"
        lines.append(line)
        if item["reason"]:
            lines.append(f"           {item['reason']}")
    lines.append(
        f"Exported {result.count('exported')}, skipped {result.count('skipped')}, "
        f"failed {result.count('failed')}; every archive was verified against its manifest."
    )
    if any(i["result"] == "skipped" and i["path"] for i in result.items):  # the file existed
        lines.append("  existing archives are kept; add --overwrite to replace them")
    if anonymized:
        lines.append(
            "  paths: anonymized (GAME_ROOT/... and RENPY_SAVES_ROOT/...; add --keep-paths "
            "to keep this computer's paths)"
        )
    if tokens:
        lines.append(
            f"  WARNING: {tokens} archive(s) contain Ren'Py's save-signing private keys; do "
            "not share them."
        )
    return "\n".join(lines)


def render_import_previews(
    previews: list[tuple[Path, ImportPlan | None, str | None]],
    repo_root: Path,
    reimport: bool = False,
) -> str:
    lines = [f"EngineSave {__version__} - import plan", f"Repository: {repo_root}", ""]
    usable = skipped = repeated = 0
    for archive, plan, error in previews:
        if plan is None:
            lines.append(f"  CANNOT   {archive.name}")
            lines.append(f"           {error}")
            continue
        files = plan.manifest["files"]
        record = "existing record" if plan.existing_record else "new record"
        line = (
            f"{archive.name}  {plan.display_name}  snapshot {plan.header['snapshot_id']}  "
            f"{len(files)} files  {human_size(sum(f['size'] for f in files))}"
        )
        if plan.already_imported is not None and not reimport:
            skipped += 1
            lines.append(f"  SKIP     {line}  ({already_imported_text(plan)})")
            continue
        if plan.duplicate_of is not None and not reimport:  # S-098
            repeated += 1
            lines.append(
                f"  SKIP     {line}  (same snapshot as {plan.duplicate_of} in this folder)"
            )
            continue
        usable += 1
        again = f"; {already_imported_text(plan)}" if plan.already_imported else ""
        lines.append(f"  OK       {line}  ({record}{again})")
    lines += [
        "",
        f"{usable} of {len(previews)} archives will be imported, in this order; each becomes a "
        "new snapshot (reason import) and is checked again right before it is imported. "
        "Nothing outside the repository is written.",
    ]
    if skipped:
        lines.append(
            f"{skipped} archive(s) are already in this repository and are skipped "
            "(--reimport imports them again)."
        )
    if repeated:
        lines.append(
            f"{repeated} archive(s) repeat a snapshot of an earlier archive in this folder and "
            "are skipped (--reimport imports every copy)."
        )
    return "\n".join(lines)


def render_import_batch(result: BatchResult, repo_root: Path) -> str:
    lines = [f"Import summary ({repo_root}):"]
    for item in result.items:
        name = Path(item["archive"]).name
        done = item["import"]
        if item["result"] == "skipped":
            earlier = (item["plan"] or {}).get("already_imported") or {}
            lines.append(
                f"  SKIPPED  {name}: {item['reason']} (snapshot {earlier.get('snapshot_id')} "
                f"of {earlier.get('game_id')})"
            )
            continue
        if done is None:
            lines.append(f"  {BATCH_LABELS[item['result']]:<8} {name}")
            lines.append(f"           {item['error']}")
            continue
        record = "new record" if done["created_record"] else "existing record"
        lines.append(
            f"  OK       {name}  {done['display_name']}  snapshot {done['snapshot_id']}  "
            f"{done['file_count']} files  {human_size(done['total_bytes'])}  ({record})"
        )
    lines.append(
        f"Imported {result.count('imported')}, skipped {result.count('skipped')}, "
        f"failed {result.count('failed')}."
    )
    if result.count("skipped"):
        lines.append("  skipped archives are already imported; --reimport imports them again")
    if result.count("imported"):
        lines.append(f'  to restore: enginesave restore --repo "{repo_root}" --game ID --dry-run')
    return "\n".join(lines)


CLEANUP_LABELS = {
    "game_root": "game folder",
    "global": "global saves",
    "unlinked": "orphaned saves",
}


def cleaned_text(cleaned: Mapping[str, Any]) -> str:
    """``recycled 2026-10-24 20:58`` (``, purged from the recycle bin …``) for a folder that a
    cleanup removed earlier (D41)."""
    how = "recycled" if cleaned.get("method") == "recycle" else "deleted permanently"
    text = f"{how} {str(cleaned['at'])[:16].replace('T', ' ')}"
    if cleaned.get("purged_at"):
        text += f", purged from the recycle bin {str(cleaned['purged_at'])[:16].replace('T', ' ')}"
    return text


def render_cleanup_plan(plan: CleanupPlan, *, skip_failed: bool = False) -> str:
    snapshot = (
        f"{plan.snapshot_id} ({plan.snapshot_reason}), {plan.verified_files} files verified"
        if plan.snapshot_id
        else "none"
    )
    if plan.method != "recycle":
        method = "delete permanently (no recycle bin)"
    elif plan.recycle_limit is not None:
        method = f"move to the recycle bin (folders up to {human_size(plan.recycle_limit)})"
    else:
        method = "move to the recycle bin"
    lines = [
        f"EngineSave {__version__} - cleanup plan",
        f"Game: {plan.display_name}  ({plan.game_id})",
        f"Snapshot: {snapshot}",
        f"Method: {method}",
    ]
    lines += [f"  {bin_.describe()}" for bin_ in plan.recycle_bins.values()]  # D35
    lines.append("")
    for t in plan.targets:
        label = CLEANUP_LABELS.get(t.kind, t.kind)
        if t.already_cleaned is not None:  # D41: neither passed nor refused
            lines.append(
                f"  {'CLEANED':<8} {label:<14} {t.path}  {cleaned_text(t.already_cleaned)}"
            )
            continue
        if t.gone:
            lines.append(f"  {'GONE':<8} {label:<14} {t.path}  not there (no cleanup recorded)")
            continue
        verdict = "OK" if t.ok else "REFUSED"
        size = f"{human_size(t.bytes)} in {t.files} files" if t.files else "empty"
        lines.append(f"  {verdict:<8} {label:<14} {t.path}  {size}")
        lines.extend(f"           - {reason}" for reason in t.reasons)
        lines.extend(f"           note: {note}" for note in t.notes)
    passed = len(plan.passed)
    lines.append("")
    if not plan.active:
        lines.append("Nothing to clean up: every folder is already cleaned up or gone.")
        return "\n".join(lines)
    others = len(plan.targets) - len(plan.active)
    extra = f"; {others} already cleaned up or gone" if others else ""
    lines.append(f"Checks: {passed} of {len(plan.active)} folders can be cleaned up{extra}.")
    if not plan.ok:
        lines.append(
            "--skip-failed: only the folders that passed are cleaned up."
            if skip_failed and passed
            else "Nothing is deleted unless every folder passes (or use --skip-failed)."
        )
    if plan.method == "recycle" and passed:
        lines.append("Folders in the recycle bin keep their space until you empty it.")
    return "\n".join(lines)


def render_cleanup_result(result: CleanupResult) -> str:
    lines = ["Cleanup summary:"]
    for t in result.targets:
        lines.append(
            f"  {t['result'].upper():<9} {CLEANUP_LABELS.get(t['kind'], t['kind']):<14} "
            f"{t['path']}  {human_size(t['bytes'])}"
        )
        if t.get("error"):
            lines.append(f"            {t['error']}")
        lines.extend(f"            left: {path}" for path in t.get("leftovers") or [])
    note = " (in the recycle bin: freed when you empty it)" if result.method == "recycle" else ""
    lines.append(f"Freed: {human_size(result.freed_bytes)}{note}")
    lines += [f"  now: {bin_.describe()}" for bin_ in result.recycle_bins]
    for full in result.full_bins:  # D41: left for later, not failed
        lines.append(
            f"Left for later: {len(result.skipped)} folder(s); the recycle bin of {full['drive']} "
            f"has {human_size(full['room'])} left, they need {human_size(full['needed'])} "
            f"(missing {human_size(full['missing'])})"
        )
    return "\n".join(lines)


def render_verify(
    checks: list[SnapshotCheck],
    stale: list[str],
    objects: ObjectCheck | None = None,
    orphans: Sequence[str] = (),
) -> str:
    lines = [f"EngineSave {__version__} - verify"]
    for c in checks:
        verdict = "OK" if c.clean else "PROBLEM"
        lines.append(
            f"  {verdict:<8} {c.display_name}  {c.snapshot_id}  ({c.reason}, {c.snapshot_status})"
            f"  ok {c.ok}, mismatched {len(c.mismatched)}, missing {len(c.missing)}, "
            f"extra {len(c.extra)}"
        )
        if c.snapshot_status == "partial":
            lines.append("           note: partial snapshot (see `show --snapshot` for errors)")
        for label, items in (
            ("mismatched", c.mismatched),
            ("missing", c.missing),
            ("extra", c.extra),
            ("problem", c.problems),
        ):
            lines.extend(f"           {label}: {item}" for item in items)
    lines.extend(f"  warning: leftover temporary folder {p}" for p in stale)
    lines.extend(f"  warning: record folder not in the catalog {p}" for p in orphans)
    if objects is not None:
        lines.append(
            f"Objects: {objects.ok} ok, {len(objects.mismatched)} mismatched, "
            f"{len(objects.unexpected)} unexpected"
        )
        lines.extend(f"  mismatched object: {name}" for name in objects.mismatched)
        lines.extend(f"  unexpected file: {name}" for name in objects.unexpected)
    bad = sum(1 for c in checks if not c.clean)
    lines.append(f"Result: {len(checks) - bad} clean, {bad} with problems")
    return "\n".join(lines)


# --------------------------------------------------------------------- remove (0.8.0, D49)


def render_remove_plan(data: dict[str, Any]) -> str:
    lines = [f"EngineSave {__version__} - remove", f"Repository: {data['repository']}", ""]
    for item in data["items"]:
        count = len(item["snapshot_ids"])
        what = "whole record" if item["whole_record"] else f"{count} of {item['snapshot_count']}"
        lines.append(
            f"  {item['display_name']}  ({item['game_id']})  {what}: {count} snapshot(s), "
            f"{item['files']} files, {human_size(item['bytes'])}"
        )
        lines.extend(f"    {snapshot_id}" for snapshot_id in item["snapshot_ids"])
    lines.append("")
    lines.append(
        f"Deletes {data['records']} record(s) and {data['snapshots']} snapshot(s) permanently "
        f"(not to the recycle bin), freeing about {human_size(data['freed_bytes'])}."
    )
    if data["games_not_installed"]:
        lines.append(
            f"Note: the game of {data['games_not_installed']} record(s) is not installed any "
            "more; once deleted, these saves cannot be recovered."
        )
    if data["gc_blocked"]:
        lines.append(
            f"Note: unused objects will not be removed ({data['gc_blocked']}); the snapshots "
            "are still deleted."
        )
    return "\n".join(lines)


def render_remove_result(data: dict[str, Any]) -> str:
    lines = [
        f"Deleted {data['records']} record(s) and {data['snapshots']} snapshot(s), freed "
        f"{human_size(data['freed_bytes'])} ({data['objects_removed']} unused object(s) removed)."
    ]
    if data["gc_skipped"]:
        lines.append(f"Unused objects were not removed: {data['gc_skipped']}.")
    if data["objects_kept_linked"]:
        lines.append(
            f"{data['objects_kept_linked']} unused object(s) kept: a file still links to them."
        )
    lines.extend(f"  could not delete {path}" for path in data["leftovers"])
    return "\n".join(lines)


def render_tmp_folders(data: dict[str, Any]) -> str:
    """``remove --tmp`` (0.9.0, D55): the leftovers of interrupted runs."""
    lines = [f"EngineSave {__version__} - remove --tmp", f"Repository: {data['repository']}", ""]
    if not data["tmp"]:
        lines.append("No temporary folders left by interrupted runs.")
    for item in data["tmp"]:
        if item.get("held_by_store"):
            busy = "  (a file is being written into the object store: may be in use, kept)"
        elif item["recent"]:
            busy = "  (changed in the last 10 minutes: may be in use, kept)"
        else:
            busy = ""
        lines.append(
            f"  {item['path']}  {item['files']} file(s), {human_size(item['bytes'])}, "
            f"modified {item['modified']}{busy}"
        )
    return "\n".join(lines)


def render_tmp_result(data: dict[str, Any]) -> str:
    lines = [
        f"Deleted {len(data['removed'])} temporary folder(s), freed "
        f"{human_size(data['freed_bytes'])} ({data['objects_removed']} unused object(s) removed)."
    ]
    if data["recent"]:
        lines.append(f"Kept {len(data['recent'])} folder(s) changed in the last 10 minutes.")
    if data.get("store_busy"):
        lines.append(
            "A file is being written into the object store: temporary snapshot folders are "
            "kept as well."
        )
    if data["gc_skipped"]:
        lines.append(f"Unused objects were not removed: {data['gc_skipped']}.")
    lines.extend(f"  could not delete {path}" for path in data["leftovers"])
    if data.get("cancelled"):
        lines.append("Stopped before the end.")
    return "\n".join(lines)


def render_migration_plan(data: dict[str, Any]) -> str:
    """``migrate --dry-run`` (0.11.0, D67 8)."""
    records = data["records"]
    renames = [r for r in records if r["state"] == "rename"]
    lines = [
        f"EngineSave {__version__} - migrate (dry run: nothing is changed)",
        f"Repository: {data['repo']}  (format {data['format_version']})",
        "",
    ]
    if not data["needed"]:
        lines.append("Already migrated: every record has its folder; nothing to do.")
    else:
        done = sum(r["state"] == "done" for r in records)
        lines.append(f"{len(renames)} record folder(s) to rename, {done} named already:")
        for record in records:
            if record["state"] == "rename":
                lines.append(f"  games\\{record['from']}  ->  games\\{record['to']}")
            elif record["state"] == "adopt":
                lines.append(f"  games\\{record['to']}  (renamed by an interrupted run)")
            elif record["state"] == "missing":
                lines.append(
                    f"  {record['display_name'] or record['game_id']}: no folder; its next "
                    f"backup uses games\\{record['to']}"
                )
        if data["format_version"] == 1:
            lines.append("Then format_version 2: SaveTrail 0.10.0 and earlier cannot open it.")
        if data.get("readmes_missing"):  # D67 4
            lines.append(f"{data['readmes_missing']} snapshot(s) get a README.txt; then INDEX.txt.")
    lines.extend(f"Refused: {reason}" for reason in data["refusals"])
    return "\n".join(lines)


def render_migration(data: dict[str, Any]) -> str:
    """``migrate`` (0.11.0, D67 8)."""
    lines = [f"EngineSave {__version__} - migrate", f"Repository: {data['repo']}", ""]
    if data["already_migrated"]:
        lines.append("Already migrated: nothing to do.")
        return "\n".join(lines)
    if data["catalog_backup"]:
        lines.append(f"Copy of the catalog: {data['catalog_backup']}")
    lines.append(
        f"{data['renamed']} record folder(s) renamed, {data['recorded']} catalog entr"
        f"{'y' if data['recorded'] == 1 else 'ies'} given their folder"
    )
    lines.append(  # D67 4, 5
        f"{data.get('readmes', 0)} README.txt written into snapshots"
        + ("; INDEX.txt written" if data.get("index") else "")
    )
    lines.extend(f"  no folder yet (made by its next backup): {name}" for name in data["missing"])
    lines.extend(f"  failed: {item}" for item in data["failed"])
    lines.extend(f"  note: {item}" for item in data["notes"])
    if data.get("cancelled"):
        lines.append("Stopped before the end: run the same command again to go on.")
    elif data["failed"]:
        lines.append("Not finished (format 1 still): fix the above and run it again.")
    else:
        lines.append(
            f"Migrated: format {data['format_version']}; SaveTrail 0.10.0 and earlier cannot "
            "open it any more."
        )
    return "\n".join(lines)


def render_mirror(data: dict[str, Any]) -> str:
    """``mirror`` (0.9.0, D57)."""
    lines = [
        f"EngineSave {__version__} - mirror",
        f"Repository: {data['source']}",
        f"Mirror:     {data['target']}{'  (new)' if data['new'] else ''}",
        "",
        f"Objects:   {data['objects_copied']} copied, {data['objects_present']} already there",
        f"Snapshots: {data['snapshots_copied']} copied, {data['snapshots_present']} already there",
        f"Copied {human_size(data['bytes_copied'])}; storage {data['storage'] or '-'}; "
        f"{data['records']} record(s) in the mirror",
    ]
    if data.get("migrated"):  # 0.11.0 (D67 8)
        lines.append("The mirror had the old layout: migrated first, as the repository was.")
    if data["kept_records"]:
        lines.append(
            f"Kept in the mirror, gone from the repository: {', '.join(data['kept_records'])}"
        )
    lines.extend(  # 0.11.0 (D67 1)
        f"  named otherwise in the mirror (the name is taken there): {item}"
        for item in data.get("folders_kept", [])
    )
    repaired = data.get("objects_recopied", 0), data.get("files_replaced", 0)
    if any(repaired):
        lines.append(
            f"Repaired: {repaired[0]} damaged object(s) copied again, "
            f"{repaired[1]} snapshot file(s) placed again"
        )
    lines.extend(f"  failed: {item}" for item in data["failed"])
    lines.extend(f"  not mirrored (unreadable): {item}" for item in data["unreadable"])
    if data.get("cancelled"):
        lines.append("Stopped before the end: run the same command again to go on.")
    elif data.get("verify_stopped"):
        lines.append("mirror complete; verification stopped")
    elif data["verified"] is None:
        lines.append("Not verified (--no-verify).")
    elif data["verified"]:
        lines.append("Verified: every snapshot and object of the mirror is intact.")
    else:
        bad = [c for c in data["checks"] if not c["clean"]]
        lines.append(
            f"Verification found problems: {len(bad)} snapshot(s), "
            f"{len(data['objects']['mismatched'])} object(s)."
        )
    return "\n".join(lines)
