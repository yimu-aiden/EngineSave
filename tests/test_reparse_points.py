"""D20: only name-surrogate reparse points are skipped; cloud placeholders are read and noted."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from enginesave import fsutil
from enginesave.scan import CLOUD_WARNING, ScanOptions, run_scan
from fixtures.builder import build_f1, build_game, simple_spec

CLOUD_TAG = 0x9000601A  # IO_REPARSE_TAG_CLOUD_6: no name-surrogate bit
JUNCTION_TAG = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT: name surrogate


@pytest.fixture
def tagged(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """F15: pretend some folders carry a reparse tag (no real OneDrive needed)."""
    tags: dict[str, int] = {}
    real = fsutil.reparse_tag

    def fake(path: Path | os.DirEntry[str]) -> int | None:
        name = path.name if isinstance(path, os.DirEntry) else Path(path).name
        return tags[name] if name in tags else real(path)

    monkeypatch.setattr(fsutil, "reparse_tag", fake)
    return tags


def test_f15_cloud_placeholder_is_read_but_junction_is_skipped(
    games_root: Path, saves_root: Path, tagged: dict[str, int]
) -> None:
    build_game(games_root / "OneDriveGames", simple_spec("Cloud Game", "CloudGame", "C-1"), None)
    build_game(games_root / "Linked", simple_spec("Linked Game", "LinkedGame", "L-1"), None)
    tagged["OneDriveGames"] = CLOUD_TAG
    tagged["Linked"] = JUNCTION_TAG
    report = run_scan(ScanOptions(roots=[games_root], saves_root_override=saves_root))
    assert [g.display_name for g in report.games] == ["Cloud Game"]
    assert {"path": str(games_root / "Linked"), "reason": "symlink"} in report.skipped
    assert report.warnings.count(CLOUD_WARNING) == 1


def test_cloud_placeholder_files_are_copied_like_files(
    tmp_path: Path, tagged: dict[str, int]
) -> None:
    (tmp_path / "1-1-LT1.save").write_bytes(b"cloud")
    (tmp_path / "linked.save").write_bytes(b"link")
    tagged["1-1-LT1.save"] = CLOUD_TAG
    tagged["linked.save"] = JUNCTION_TAG
    with fsutil.track_cloud_placeholders() as seen:
        files, errors = fsutil.walk_files(tmp_path)
    assert [f.rel for f in files] == ["1-1-LT1.save"]
    assert [e.rel for e in errors] == ["linked.save"]
    assert seen == [str(tmp_path / "1-1-LT1.save")]


def test_no_cloud_warning_without_placeholders(games_root: Path, saves_root: Path) -> None:
    build_f1(games_root, saves_root)
    report = run_scan(ScanOptions(roots=[games_root], saves_root_override=saves_root))
    assert CLOUD_WARNING not in report.warnings


def test_reparse_tag_classification(tmp_path: Path) -> None:
    assert fsutil.reparse_tag(tmp_path) is None
    assert not fsutil.is_reparse_point(tmp_path)
    assert fsutil.is_cloud_tag(CLOUD_TAG) and fsutil.is_cloud_tag(0x9000001A)
    assert not fsutil.is_cloud_tag(JUNCTION_TAG)
    with fsutil.track_cloud_placeholders() as seen:
        assert not fsutil.is_reparse_point(tmp_path)
    assert seen == []
