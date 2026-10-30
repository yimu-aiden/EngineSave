"""Repository hygiene (plan 12.11, S-038): no tracked text file mentions the local user name."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def tracked_files() -> list[Path]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    return [ROOT / name.decode("utf-8") for name in listing.split(b"\0") if name]


def test_tracked_text_files_do_not_contain_the_user_name() -> None:
    user = os.environ.get("USERNAME", "")
    if len(user) < 4:
        pytest.skip("USERNAME unset or too short to search for reliably")
    needle = user.casefold().encode("utf-8")
    offenders = []
    for path in tracked_files():
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue  # binary (e.g. the icon)
        if needle in data.decode("utf-8", "replace").casefold().encode("utf-8"):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
