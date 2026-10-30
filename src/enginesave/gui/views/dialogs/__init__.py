"""Modal dialogs: settings, game details, backup preview and result, restore, export, import,
cleanup (one module each since 0.6.0, D38).

Confirming a dialog is the window's ``--yes``. Every rule (destination overlap, manifest
validation, the safety snapshot, refusing export targets, the cleanup checks) is enforced by
the library call the dialog makes through the window's task runner; the dialogs only collect
choices and show results.

Since 0.5.0 (D32) a dialog does its read-only preparation by itself as soon as it opens (the
cleanup check, the restore preview, the import check), pre-selects what passed, and its primary
button is the recommended action: focused, pressed by Enter, and named after what it does.
Only irreversible steps still ask: restoring Ren'Py's private keys asks once more, and
overwriting an existing export file asks first (unless "overwrite" is ticked); a permanent
delete is one click with a red warning (D40).
"""

from __future__ import annotations

from enginesave.gui.views.dialogs.backup import (
    BackupPreviewDialog,
    BackupResultDialog,
)
from enginesave.gui.views.dialogs.base import (
    LINK_COLOR,
    LINK_COLUMN,
    OPEN_LINK,
    PAD,
    WARNING_COLOR,
    Dialog,
    TextDialog,
    game_details_dialog,
    scrolled_tree,
)
from enginesave.gui.views.dialogs.cleanup import (
    CleanupDialog,
)
from enginesave.gui.views.dialogs.export import (
    ExportDialog,
)
from enginesave.gui.views.dialogs.importing import (
    ImportDialog,
)
from enginesave.gui.views.dialogs.linking import ConfirmLinkDialog
from enginesave.gui.views.dialogs.maintenance import MigrateDialog, MirrorDialog, TmpDialog
from enginesave.gui.views.dialogs.removing import RemoveDialog
from enginesave.gui.views.dialogs.restore import (
    RestoreDialog,
)
from enginesave.gui.views.dialogs.settings import SettingsDialog
from enginesave.gui.views.dialogs.unlinked import UnlinkedDialog

__all__ = [
    "BackupPreviewDialog",
    "BackupResultDialog",
    "CleanupDialog",
    "ConfirmLinkDialog",
    "Dialog",
    "ExportDialog",
    "ImportDialog",
    "LINK_COLOR",
    "LINK_COLUMN",
    "MigrateDialog",
    "MirrorDialog",
    "OPEN_LINK",
    "PAD",
    "RemoveDialog",
    "RestoreDialog",
    "SettingsDialog",
    "TextDialog",
    "TmpDialog",
    "UnlinkedDialog",
    "WARNING_COLOR",
    "game_details_dialog",
    "scrolled_tree",
]
