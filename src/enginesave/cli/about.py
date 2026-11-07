"""``enginesave about`` (1.2.0, D73 11): this EngineSave's version and the versions of what it
writes, for a program that runs it to check what it can expect.

``formats`` are the versions of the ``--json`` outputs: one changes only when a field changes
meaning or goes away (2.0); fields that are only added (as in 1.2.0) keep it. ``library`` are
the versions of the library's own files (catalog and ``game.json``, snapshot manifests,
exported zips).
"""

from __future__ import annotations

import argparse
from typing import Any

from enginesave import __version__
from enginesave.cli.common import emit

# every --json output (docs/schemas): scan's carries its format_version, the others are 1
JSON_FORMATS = {
    "scan": 1,
    "list": 1,
    "backup": 1,
    "restore": 1,
    "cleanup": 1,
    "export": 1,
    "import": 1,
    "remove": 1,
    "mirror": 1,
}


def about() -> dict[str, Any]:
    from enginesave.export.archive import EXPORT_FORMAT_VERSION as EXPORT_FORMAT
    from enginesave.models import FORMAT_VERSION
    from enginesave.repo import FORMAT_VERSION as LIBRARY_FORMAT
    from enginesave.repo import MANIFEST_FORMAT

    return {
        "tool_version": __version__,
        "formats": {**JSON_FORMATS, "scan": FORMAT_VERSION},
        "library": {
            "catalog": LIBRARY_FORMAT,
            "manifest": MANIFEST_FORMAT,
            "export": EXPORT_FORMAT,
        },
    }


def cmd_about(args: argparse.Namespace) -> int:
    data = about()
    formats = ", ".join(f"{name} {version}" for name, version in data["formats"].items())
    human = f"EngineSave {data['tool_version']}\nJSON formats: {formats}"
    emit(args, human, data)
    return 0
