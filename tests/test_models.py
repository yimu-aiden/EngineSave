from __future__ import annotations

import json

from enginesave.models import (
    DetectedGame,
    Evidence,
    GameIdentity,
    GameStatus,
    MetadataState,
    RootToken,
    SaveSource,
    ScanReport,
    SourceKind,
    SourceStatus,
    UnlinkedSaveDir,
)


def sample_report() -> ScanReport:
    evidence = Evidence("game/options.rpy", "rpy_define", 'define config.save_directory = "X"', 9)
    source = SaveSource(
        source_id="global",
        kind=SourceKind.GLOBAL,
        path="C:\\RenPy\\X",
        root_token=RootToken.RENPY_SAVES_ROOT,
        relative_root="X",
        status=SourceStatus.CONFIRMED,
        method="path_to_saves:APPDATA",
        evidence=[evidence],
        exists=True,
        file_count=2,
        total_bytes=10,
        mirror_of=["local"],
        notes=["includes sync/"],
    )
    game = DetectedGame(
        game_root="D:\\Games\\X",
        engine="renpy",
        engine_version="8.4.1.25072401",
        display_name="X",
        identity=GameIdentity("X", "X", "X", "1.0", "X", "X"),
        metadata_state=MetadataState.RESOLVED,
        status=GameStatus.CONFIRMED,
        sources=[source],
        evidence=[evidence],
        installs=[{"game_root": "D:\\Games\\X", "config_version": "1.0"}],
    )
    orphan = UnlinkedSaveDir("Orphan", "C:\\RenPy\\Orphan", 1, 5, "2026-01-01T00:00:00Z", "0.7")
    return ScanReport(
        format_version=1,
        tool_version="0.1.0.dev0",
        generated_at="2026-10-23T12:00:00Z",
        roots=["D:\\Games"],
        renpy_saves_root="C:\\RenPy",
        games=[game],
        unlinked=[orphan],
        skipped=[{"path": "D:\\Games\\sdk", "reason": "renpy_sdk"}],
    )


def test_report_round_trips_through_json() -> None:
    report = sample_report()
    data = json.loads(json.dumps(report.to_dict()))
    assert data["games"][0]["status"] == "CONFIRMED"
    assert data["games"][0]["sources"][0]["kind"] == "global"
    assert data["games"][0]["metadata_state"] == "resolved"
    rebuilt = ScanReport.from_dict(data)
    assert rebuilt == report
    assert isinstance(rebuilt.games[0].sources[0].root_token, RootToken)


def test_from_dict_ignores_unknown_keys_and_uses_defaults() -> None:
    data = sample_report().games[0].sources[0].to_dict()
    data["future_field"] = 1
    del data["notes"]
    source = SaveSource.from_dict(data)
    assert source.notes == []
    assert source.status is SourceStatus.CONFIRMED


def test_summary_counts_every_status() -> None:
    summary = sample_report().summary()
    assert summary["CONFIRMED"] == 1
    assert summary["UNLINKED"] == 1
    assert set(summary) == {s.value for s in GameStatus}


def test_detected_game_source_lookup() -> None:
    game = sample_report().games[0]
    assert game.source("global") is game.sources[0]
    assert game.source("local") is None
