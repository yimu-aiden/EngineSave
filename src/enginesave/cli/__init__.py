"""Command line entry point for ``enginesave`` (English output, exit codes per plan section 7).

One module per group of subcommands since 0.6.0 (D38); ``parser`` imports a subcommand's
module only when it runs.
"""

from __future__ import annotations

from enginesave.cli.common import confirm, emit, interactive
from enginesave.cli.parser import build_parser, main

__all__ = [
    "build_parser",
    "confirm",
    "emit",
    "interactive",
    "main",
]
