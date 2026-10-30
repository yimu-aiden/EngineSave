"""``enginesave mirror`` (0.9.0, D57): copy the repository into another folder, best on another
drive, as a library of its own; again later, only what is new. Nothing in the mirror is ever
deleted, and another library is never written into."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from enginesave.appdirs import config_dir
from enginesave.cli.common import STOPPED_NOTE, emit, override_from, should_stop
from enginesave.errors import STOPPED_EXIT_CODE
from enginesave.mirror import forbidden_places, mirror_repo, record_mirror
from enginesave.repo import Repo
from enginesave.report import render_mirror


def cmd_mirror(args: argparse.Namespace) -> int:
    repo = Repo.open(Path(args.repo))
    target = Path(os.path.abspath(args.to))
    forbidden = forbidden_places(repo, saves_root=override_from(args))
    result = mirror_repo(
        repo,
        target,
        forbidden=forbidden,
        verify=not args.no_verify,
        repair=args.repair,
        should_stop=should_stop,
    )
    if result.exit_code == 0 and not result.cancelled:  # list's stats show when (D56)
        record_mirror(config_dir(), repo.root, target)
    data = result.to_dict()
    human = render_mirror(data)
    if result.cancelled:  # D58: Ctrl+C between files; running it again goes on
        emit(args, f"{human}\n{STOPPED_NOTE}", data)
        return STOPPED_EXIT_CODE
    emit(args, human, data)
    return result.exit_code
