"""0.2.0 scan semantics, D19: the CONFLICT boundary and games that only save locally."""

from __future__ import annotations

from pathlib import Path

from enginesave.engines.renpy.detector import LOCAL_ONLY_NOTE
from enginesave.models import DetectedGame, GameStatus, MetadataState, ScanReport, SourceStatus
from enginesave.report import render_scan
from enginesave.scan import ScanOptions, run_scan
from fixtures.builder import (
    GameSpec,
    build_f13,
    build_f14,
    build_game,
)


def scan(root: Path, saves_root: Path) -> ScanReport:
    return run_scan(ScanOptions(roots=[root], saves_root_override=saves_root))


def only_game(report: ScanReport) -> DetectedGame:
    assert len(report.games) == 1, [g.game_root for g in report.games]
    return report.games[0]


def test_f13a_options_without_save_directory_is_local_only(
    games_root: Path, saves_root: Path
) -> None:
    build_f13(games_root, saves_root, name_alike_dir=False)
    report = scan(games_root, saves_root)
    game = only_game(report)
    assert game.metadata_state is MetadataState.RESOLVED
    assert game.status is GameStatus.CONFIRMED
    assert [s.source_id for s in game.sources] == ["local"]
    assert LOCAL_ONLY_NOTE in game.sources[0].notes
    assert game.identity.renpy_save_directory is None
    assert "save_directory=<none> (local only)" in render_scan(report)


def test_f13b_name_alike_folder_is_still_a_candidate(games_root: Path, saves_root: Path) -> None:
    build_f13(games_root, saves_root, name_alike_dir=True)
    game = only_game(scan(games_root, saves_root))
    assert game.status is GameStatus.CANDIDATE
    candidate = game.source("global")
    assert candidate is not None
    assert (candidate.status, candidate.method) == (SourceStatus.CANDIDATE, "dir_name_match")
    assert candidate.relative_root == "LocalOnlyGame-123"
    assert game.source("local").status is SourceStatus.CONFIRMED


def test_unreadable_script_prevents_the_local_only_conclusion(
    games_root: Path, saves_root: Path
) -> None:
    built = build_f13(games_root, saves_root, name_alike_dir=False)
    (built.gamedir / "broken.rpyc").write_bytes(b"RENPY RPC2 truncated")
    game = only_game(scan(games_root, saves_root))
    assert game.metadata_state is MetadataState.MISSING
    assert game.status is GameStatus.CANDIDATE
    assert any("broken.rpyc: compiled script not readable" in w for w in game.warnings)


def test_f14_the_compiled_version_wins(games_root: Path, saves_root: Path) -> None:
    """F14: the compiled script wins. Since 0.9.0 (D52) the stale .rpy beside a readable
    .rpyc is not read at all, so there is nothing left to warn about."""
    build_f14(games_root, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert game.metadata_state is MetadataState.RESOLVED
    assert game.status is GameStatus.CONFIRMED
    assert game.identity.config_version == "v1.1", "the compiled script wins"
    assert not any(e.file == "game/options.rpy" for e in game.evidence)
    assert not any(w.startswith("conflicting values") for w in game.warnings)


def test_conflicting_save_directories_are_still_a_conflict(
    games_root: Path, saves_root: Path
) -> None:
    """Since 0.9.0 (D52) only on the full parse: options without a save directory, two
    other scripts that name different ones (F19). When options name one, they win."""
    from fixtures.builder import build_f19

    build_f19(games_root, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert game.metadata_state is MetadataState.CONFLICT
    assert game.status is GameStatus.CONFLICT
    assert any("conflicting values for config.save_directory" in w for w in game.warnings)


def test_build_info_alone_is_not_enough_for_local_only(games_root: Path, saves_root: Path) -> None:
    built = build_game(games_root, GameSpec("Bare", "Bare", options=False), saves_root)
    info = built.gamedir / "cache" / "build_info.json"
    info.parent.mkdir()
    info.write_text('{"name": "Bare Game", "version": "2.0"}', encoding="utf-8")
    game = only_game(scan(games_root, saves_root))
    assert game.identity.config_name == "Bare Game"
    assert game.metadata_state is MetadataState.MISSING


def test_f17_a_renamed_rpyc_header_is_confirmed(games_root: Path, saves_root: Path) -> None:
    """D50: every .rpyc has the renamed ``chengf 2`` header and there is no .rpy; the
    metadata is read as usual, one warning names the header, and the game is CONFIRMED
    (Sicae was a CANDIDATE found by its folder name until 0.7.0)."""
    from fixtures.builder import build_f17

    build_f17(games_root, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert game.metadata_state is MetadataState.RESOLVED
    assert game.status is GameStatus.CONFIRMED
    assert game.identity.renpy_save_directory == "RenamedHeader-1600407200"
    assert game.source("global").method != "dir_name_match"
    renamed = [w for w in game.warnings if "non-standard rpyc header" in w]
    assert len(renamed) == 1, game.warnings
    assert renamed[0].startswith(  # D52: only options.rpyc is read now
        "non-standard rpyc header 'chengf 2' (read as RPYC2): 1 compiled script(s), "
        "e.g. game/options.rpyc"
    )
    assert not any("not readable" in w for w in game.warnings)
