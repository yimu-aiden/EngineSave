"""``enginesave scan``: the read-only discovery report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from enginesave.cli.common import (
    checked_report_path,
    emit,
    override_from,
    should_stop,
    write_report,
)
from enginesave.discovery import DEFAULT_MAX_DEPTH
from enginesave.models import GameStatus
from enginesave.report import render_scan
from enginesave.scan import ScanOptions, run_scan
from enginesave.scan_cache import ScanCache, default_path

# ---------------------------------------------------------------------------------- commands


def cmd_scan(args: argparse.Namespace) -> int:
    roots = [Path(r) for r in args.roots]
    libraries = [Path(args.repo)] if args.repo else []
    report_path = checked_report_path(args, roots, libraries)  # D72 R01
    options = ScanOptions(
        roots=roots,
        max_depth=DEFAULT_MAX_DEPTH if args.max_depth is None else args.max_depth,
        excludes=args.exclude or [],
        compute_hashes=args.hash,
        include_unlinked=not args.no_unlinked,
        saves_root_override=override_from(args),
        repo=Path(args.repo) if args.repo else None,
        cache=None if args.no_cache else ScanCache.load(default_path()),  # D52 3
        should_stop=should_stop,  # D58: Ctrl+C, between folders
    )
    report = run_scan(options)
    data = report.to_dict()
    problem = write_report(report_path, data) if report_path else None
    emit(args, render_scan(report, args.verbose), data)
    if problem:  # D72 R15
        print(f"error: {problem}", file=sys.stderr)
        return 3
    conflicts = any(g.status is GameStatus.CONFLICT for g in report.games)
    denied = any(s["reason"] == "permission_denied" for s in report.skipped)
    return 1 if conflicts or denied else 0
