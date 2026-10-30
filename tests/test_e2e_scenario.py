"""The end-to-end scenario script's safety (plan section 21 M22): it only ever writes into a
sandbox below the repository's ``.local`` folder and only empties that sandbox. The scenario
itself runs on real copies outside the test suite (``tools/e2e_restore_scenario.ps1``)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"


@pytest.fixture(scope="module")
def e2e():
    sys.path.insert(0, str(TOOLS))
    try:
        import e2e_restore_scenario
    finally:
        sys.path.remove(str(TOOLS))
    return e2e_restore_scenario


def test_the_sandbox_must_be_below_dot_local(e2e, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".local").mkdir(parents=True)
    sample = repo / "Test" / "Game"
    sample.mkdir(parents=True)
    assert e2e.check_sandbox(repo / ".local" / "e2e", repo) == (repo / ".local" / "e2e").resolve()
    for outside in (
        repo / ".local",  # the folder itself
        repo,
        repo / "Test",
        tmp_path / "elsewhere",
        repo / ".local" / ".." / "src",
        Path(repo.anchor),
    ):
        with pytest.raises(e2e.Refused):
            e2e.check_sandbox(outside, repo)
    inside_sample = repo / ".local" / "e2e"
    with pytest.raises(e2e.Refused, match="only read"):
        e2e.check_sandbox(inside_sample, repo, keep_out=[repo / ".local"])


def test_emptying_the_sandbox_touches_nothing_else(e2e, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sandbox = repo / ".local" / "e2e"
    (sandbox / "old" / "deep").mkdir(parents=True)
    locked = sandbox / "old" / "deep" / "read-only.bin"
    locked.write_bytes(b"x")
    os.chmod(locked, stat.S_IREAD)
    neighbour = repo / ".local" / "keep.txt"
    neighbour.write_text("stay", encoding="utf-8")
    e2e.clear_sandbox(sandbox, repo)
    assert sandbox.is_dir() and list(sandbox.iterdir()) == []
    assert neighbour.read_text(encoding="utf-8") == "stay"
    with pytest.raises(e2e.Refused):
        e2e.clear_sandbox(repo, repo)
    assert neighbour.exists(), "a refused sandbox empties nothing"


def test_copy_leaves_out_and_compare_finds_every_difference(e2e, tmp_path: Path) -> None:
    source = tmp_path / "game"
    for rel, data in {
        "game.exe": b"MZ",
        "game/scripts.rpa": b"rpa",
        "game/images_1.rpa": b"big",
        "game/saves/1-1-LT1.save": b"save",
    }.items():
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_bytes(data)
    target = tmp_path / "copy"
    files, size = e2e.copy_tree(source, target, leave_out=["game/images_*.rpa", "game/saves"])
    assert (files, size) == (2, 5)
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()) == [
        "game.exe",
        "game/scripts.rpa",
    ]
    original = e2e.manifest(source / "game" / "saves")
    copy = tmp_path / "saves"
    e2e.copy_tree(source / "game" / "saves", copy)
    assert e2e.differences(original, e2e.manifest(copy)) == [], "times are kept"
    (copy / "1-1-LT1.save").write_bytes(b"other")
    (copy / "extra.save").write_bytes(b"new")
    found = e2e.differences(original, e2e.manifest(copy))
    assert found[0].startswith("size, sha256") and found[1] == "extra: extra.save"
