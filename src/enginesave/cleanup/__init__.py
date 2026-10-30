"""Cleanup after a verified backup (D26, plan section 19 M13): check, then recycle or delete.

Nothing is ever removed automatically. The caller names each folder: a game's install folder,
its global save folder, or the folder of an orphaned (``unlinked``) record. ``plan_cleanup``
only reads; a folder passes when

1. the chosen snapshot is ``complete`` and verifies clean, its objects included;
2. every file now in the folder is in that snapshot with the same size and SHA-256 (freshness;
   for an install folder: its save folders, the game itself is not in any backup);
3. the game is not running (its ``.exe`` files and ``log.txt`` cannot be opened for writing,
   and no running process image lies inside an install folder);
4. a global folder is not used by another install that stays (shared);
5. the path is on the whitelist: exactly the recorded install, the resolved global folder or
   the orphaned folder; never a saves root, ``tokens``, ``persistent``, ``launcher-*``, the
   library, a scan root, a drive root or the home folder and its parents, never a link;
6. the recycle bin can take it when recycling was asked for: ``$RECYCLE.BIN`` on a fixed
   drive, the drive's recycle bin turned on, and room for the folder in it (its capacity
   minus what it holds minus 5 %; D35, 0.6.0). A folder is one item for Windows, which deletes
   an item too large for the bin at once and drops the oldest items when the bin overflows,
   so "can be restored" only holds with room to spare. When the size of the bin cannot be
   read the old fixed limit of 4 GB applies; ``--recycle-limit`` sets a limit explicitly.

``execute_cleanup`` runs every check again right before removing each folder, recycles it
(``SHFileOperationW``) or deletes it permanently (rename, then ``rmtree``), never falls back
from recycling to deleting, and records the outcome in ``game.json`` (``cleanups``,
``installs[].removed_at``) and the catalog (``cleaned_at``).

Modules (0.6.0, D38): ``model`` (targets, plan, result, recycle bin), ``plan`` (the checks and
``plan_cleanup``), ``execute`` (``execute_cleanup``), ``windows`` (the Windows calls; tests
replace them there).
"""

from __future__ import annotations

from enginesave.cleanup.execute import execute_cleanup, full_bins, recheck_request
from enginesave.cleanup.model import (
    CLEANED_RESULTS,
    DEFAULT_RECYCLE_LIMIT,
    DELETING_SUFFIX,
    ENLARGE_HINT,
    GAME_NOT_IN_BACKUP,
    GAME_ROOT,
    GLOBAL,
    GONE,
    KINDS,
    MAX_LISTED,
    PERMANENT,
    RECYCLE,
    RECYCLE_HINT,
    RECYCLE_MARGIN_PERCENT,
    SKIPPED_FULL_BIN,
    UNLINKED,
    CleanupPlan,
    CleanupResult,
    CleanupTarget,
    RecycleBin,
    RecycleLedger,
    gigabytes,
)
from enginesave.cleanup.plan import (
    cleaned_entry,
    has_global_folder,
    in_use,
    parse_size,
    plan_cleanup,
    recycle_bin_of,
    requested,
    same_path,
    save_folder,
)
from enginesave.cleanup.windows import (
    BITBUCKET_KEY,
    DRIVE_FIXED,
    FO_DELETE,
    FOF_ALLOWUNDO,
    FOF_NOCONFIRMATION,
    FOF_NOERRORUI,
    FOF_SILENT,
    RECYCLE_FLAGS,
    SHFILEOPSTRUCTW,
    SHQUERYRBINFO,
)

__all__ = [
    "BITBUCKET_KEY",
    "CLEANED_RESULTS",
    "DEFAULT_RECYCLE_LIMIT",
    "DELETING_SUFFIX",
    "DRIVE_FIXED",
    "ENLARGE_HINT",
    "FOF_ALLOWUNDO",
    "FOF_NOCONFIRMATION",
    "FOF_NOERRORUI",
    "FOF_SILENT",
    "FO_DELETE",
    "GAME_NOT_IN_BACKUP",
    "GAME_ROOT",
    "GLOBAL",
    "GONE",
    "KINDS",
    "MAX_LISTED",
    "PERMANENT",
    "RECYCLE",
    "RECYCLE_FLAGS",
    "RECYCLE_HINT",
    "RECYCLE_MARGIN_PERCENT",
    "SHFILEOPSTRUCTW",
    "SHQUERYRBINFO",
    "SKIPPED_FULL_BIN",
    "UNLINKED",
    "CleanupPlan",
    "CleanupResult",
    "CleanupTarget",
    "RecycleBin",
    "RecycleLedger",
    "cleaned_entry",
    "execute_cleanup",
    "full_bins",
    "gigabytes",
    "in_use",
    "parse_size",
    "plan_cleanup",
    "recheck_request",
    "recycle_bin_of",
    "requested",
    "same_path",
    "save_folder",
    "has_global_folder",
]
