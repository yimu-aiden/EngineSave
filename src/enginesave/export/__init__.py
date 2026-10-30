"""Snapshot export to a self-describing zip, and import back into a library (D24).

An export holds ``EngineSave-export.json`` (game record + manifest), readable copies of
``game.json`` and ``manifest.json``, a ``README.txt`` and ``data/<source_id>/<path>`` read from
the snapshot's ``data`` folder (0.1.0 copies and 0.2.0 hard links alike). It is written beside
its destination as ``<name>.enginesave-tmp``, verified (``testzip`` plus a SHA-256 of every data
member against the manifest) and only then moved into place; a failed check deletes it.

Since 0.3.1 an export does not reveal this computer's folders unless asked to (S-059): source
locations become ``GAME_ROOT/<relative_root>`` or ``RENPY_SAVES_ROOT/<relative_root>``, install
folders are left out, error texts lose their paths, and sources that only exist as an absolute
path (``config.extra_savedirs``) stay in the library. ``keep_paths`` writes everything as stored.

Import validates an archive the way restore validates a manifest (9.2: anything malformed or
path-unsafe → nothing is written), finds the record by the 6.6 rules and stores the files as a
new ``reason="import"`` snapshot through the object store.

Since 0.4.0 both work in batches (D27): ``export_records`` writes the latest snapshot of many
records into one folder, ``import_archives`` takes every zip of a folder; one failure does not
stop the others and each line of the result says what happened. Since 0.5.0 (D31) an exported
snapshot the library already holds as an import (same original record and snapshot id in
``imported_from``) is skipped unless asked to import it again.

Modules (0.6.0, D38): ``archive`` (one snapshot into a zip), ``importing`` (one zip into a
repository), ``batch`` (several of either).
"""

from __future__ import annotations

from enginesave.export.archive import (
    ABSOLUTE_PATH_RE,
    EXCLUDED_ABSOLUTE,
    EXCLUDED_KEYS,
    EXPORT_FORMAT_VERSION,
    EXPORT_FORMATS,
    EXPORT_JSON,
    IDENTITY_KEYS,
    LEGACY_EXPORT_JSON,
    MAX_HEADER_BYTES,
    PATH_PLACEHOLDER,
    PATHS_ANONYMIZED,
    PATHS_ORIGINAL,
    QUOTED_PATH_RE,
    TMP_SUFFIX,
    TOKENS_WARNING,
    ZIP_EPOCH,
    ExportResult,
    anonymized,
    check_export_target,
    default_export_name,
    export_forbidden_roots,
    export_readme,
    export_snapshot,
    member_name,
    token_location,
)
from enginesave.export.batch import (
    EXPORTED,
    FAILED,
    IMPORTED,
    SKIPPED,
    BatchResult,
    Progress,
    export_records,
    import_archives,
    preview_imports,
    zip_archives,
)
from enginesave.export.importing import (
    ALREADY_IMPORTED,
    ImportKey,
    ImportPlan,
    ImportResult,
    execute_import,
    import_archive,
    imported_snapshots,
    known_imports,
    plan_import,
    read_export,
)

__all__ = [
    "ABSOLUTE_PATH_RE",
    "ALREADY_IMPORTED",
    "BatchResult",
    "EXCLUDED_ABSOLUTE",
    "EXCLUDED_KEYS",
    "EXPORTED",
    "EXPORT_FORMAT_VERSION",
    "EXPORT_FORMATS",
    "EXPORT_JSON",
    "LEGACY_EXPORT_JSON",
    "ExportResult",
    "FAILED",
    "IDENTITY_KEYS",
    "IMPORTED",
    "ImportKey",
    "ImportPlan",
    "ImportResult",
    "MAX_HEADER_BYTES",
    "PATHS_ANONYMIZED",
    "PATHS_ORIGINAL",
    "PATH_PLACEHOLDER",
    "Progress",
    "QUOTED_PATH_RE",
    "SKIPPED",
    "TMP_SUFFIX",
    "TOKENS_WARNING",
    "ZIP_EPOCH",
    "anonymized",
    "check_export_target",
    "default_export_name",
    "execute_import",
    "export_forbidden_roots",
    "export_readme",
    "export_records",
    "export_snapshot",
    "import_archive",
    "import_archives",
    "imported_snapshots",
    "known_imports",
    "member_name",
    "plan_import",
    "preview_imports",
    "read_export",
    "token_location",
    "zip_archives",
]
