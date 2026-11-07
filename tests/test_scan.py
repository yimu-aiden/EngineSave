"""Scan scenarios over the synthetic fixture matrix (plan 10.2, F1-F11)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from enginesave.models import (
    DetectedGame,
    GameStatus,
    MetadataState,
    RootToken,
    ScanReport,
    SourceKind,
    SourceStatus,
)
from enginesave.scan import ScanOptions, run_scan
from fixtures.builder import (
    build_f1,
    build_f2,
    build_f3,
    build_f4,
    build_f4b,
    build_f5,
    build_f6,
    build_f7,
    build_f8,
    build_f9,
    build_f10,
    build_f16,
    build_f18,
    build_game,
    build_nwjs,
    make_junction,
    simple_spec,
)


def scan(*roots: Path, saves_root: Path | None, **kwargs: object) -> ScanReport:
    options = ScanOptions(roots=list(roots), saves_root_override=saves_root, **kwargs)
    return run_scan(options)


def only_game(report: ScanReport) -> DetectedGame:
    assert len(report.games) == 1, [g.game_root for g in report.games]
    return report.games[0]


def snapshot(*roots: Path) -> dict[str, tuple[int, int]]:
    state = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                path = os.path.join(dirpath, name)
                st = os.lstat(path)
                state[path] = (st.st_mtime_ns, st.st_size)
    return state


def test_f1_rpy_readable(games_root: Path, saves_root: Path) -> None:
    built = build_f1(games_root, saves_root)
    game = only_game(scan(games_root, saves_root=saves_root))
    assert game.game_root == str(built.root)
    assert game.status is GameStatus.CONFIRMED
    assert game.metadata_state is MetadataState.RESOLVED
    assert game.engine_version == "8.4.1.25072401"
    assert game.display_name == "Readable Game"
    assert game.identity.renpy_save_directory == "ReadableGame-1600000001"
    assert (game.identity.config_version, game.identity.build_name) == ("1.0", "ReadableGame")
    local, global_ = game.sources
    assert (local.source_id, local.status, local.relative_root) == (
        "local",
        SourceStatus.CONFIRMED,
        "game/saves",
    )
    assert local.file_count == len(built.local_files)
    assert (global_.source_id, global_.status, global_.root_token) == (
        "global",
        SourceStatus.CONFIRMED,
        RootToken.RENPY_SAVES_ROOT,
    )
    assert global_.path == str(built.global_dir)
    assert global_.file_count == len(built.global_files)
    assert global_.method == "override"
    assert local.mirror_of == ["global"] and global_.mirror_of == ["local"]
    assert "includes sync/ (engine mirror)" in global_.notes
    # 0.9.0 (D52 1): the readable options.rpyc names the folder; its .rpy is not read
    assert {(e.file, e.method) for e in global_.evidence} == {("game/options.rpyc", "rpyc_define")}
    old_keys = ("game_root", "config_version", "engine_version")
    assert [{k: i[k] for k in old_keys} for i in game.installs] == [
        {"game_root": str(built.root), "config_version": "1.0", "engine_version": "8.4.1.25072401"}
    ]
    # 1.2.0 (D73 6, 7, 13): what an install says besides
    assert game.installs[0]["game_version"] == "1.0"
    assert game.installs[0]["game_version_source"] == "config.version"
    assert local.install_root == str(built.root) and global_.install_root is None


def test_f2_wrapped_rpyc_only(games_root: Path, saves_root: Path) -> None:
    built = build_f2(games_root, saves_root)
    report = scan(games_root, saves_root=saves_root)
    game = only_game(report)
    assert game.game_root == str(built.root)
    assert "'" in Path(game.game_root).name
    assert game.identity.exe_basename == "Inner'sGame"
    assert game.status is GameStatus.CONFIRMED
    assert game.identity.config_name == "Inner's Game Beta"
    assert game.identity.renpy_save_directory == "InnerGame-1637695238"
    sd_files = {e.file for e in game.source("global").evidence}
    assert sd_files == {"game/options.rpyc"}, "D52: the loose options name it; no archive"
    methods = {e.method for e in game.source("global").evidence}
    assert methods == {"rpyc_define"}
    assert not any("WrongFromTranslation" in w for w in game.warnings)


def test_f3_no_saves_yet(games_root: Path, saves_root: Path) -> None:
    build_f3(games_root, saves_root)
    game = only_game(scan(games_root, saves_root=saves_root))
    assert game.status is GameStatus.NOT_FOUND
    assert [(s.source_id, s.status, s.exists) for s in game.sources] == [
        ("local", SourceStatus.EXPECTED_ABSENT, False),
        ("global", SourceStatus.EXPECTED_ABSENT, False),
    ]


def test_f4_two_installs_share_one_global(games_root: Path, saves_root: Path) -> None:
    v1, v2 = build_f4(games_root, saves_root)
    game = only_game(scan(games_root, saves_root=saves_root))
    assert game.status is GameStatus.CONFIRMED
    assert [i["game_root"] for i in game.installs] == [str(v1.root), str(v2.root)]
    assert [i["config_version"] for i in game.installs] == ["1.0", "2.0"]
    ids = [s.source_id for s in game.sources]
    assert ids == ["local", "local-2", "global"]
    assert game.source("local-2").path == str(v2.local_dir)
    assert f"install: {v2.root}" in game.source("local-2").notes
    assert sorted(game.source("global").mirror_of) == ["local", "local-2"]


def test_f4b_games_that_share_a_global_folder_stay_two_games(
    games_root: Path, saves_root: Path
) -> None:
    """D48 1: different games (different config.name) that use one global folder are two
    CONFIRMED games; each keeps the folder as a confirmed source, with a note and the other
    game's folder in ``shared_global_with``. Until 0.7.0 both were CONFLICT."""
    alpha, beta = build_f4b(games_root, saves_root)
    report = scan(games_root, saves_root=saves_root)
    assert [g.display_name for g in report.games] == ["Alpha Story", "Beta Story"]
    for game, other in zip(report.games, (beta, alpha), strict=True):
        assert game.status is GameStatus.CONFIRMED
        shared = game.source("global")
        assert shared.status is SourceStatus.CONFIRMED
        assert game.source("local").status is SourceStatus.CONFIRMED
        assert f"shared with a different game: {other.root}" in shared.notes
        assert game.shared_global_with == [str(other.root)]
        assert not any("claimed" in w for w in game.warnings)


def test_f18_seasons_without_a_name_are_two_games(games_root: Path, saves_root: Path) -> None:
    """D48 1: two seasons share ``shared_seasons`` and neither has ``config.name`` (MILFs of
    Sunville): not merged (no name to say they are one game), not a conflict."""
    one, two = build_f18(games_root, saves_root)
    report = scan(games_root, saves_root=saves_root)
    assert [(g.display_name, g.identity.config_version) for g in report.games] == [
        ("SharedSeasons", "Season-1-Extra"),
        ("SharedSeasons", "Season2-v9.00"),
    ]
    assert [g.status for g in report.games] == [GameStatus.CONFIRMED] * 2
    assert [g.shared_global_with for g in report.games] == [[str(two.root)], [str(one.root)]]
    assert all(len(g.installs) == 1 for g in report.games)


def test_installs_of_one_game_merge_beside_a_game_that_shares_the_folder(
    games_root: Path, saves_root: Path
) -> None:
    """D9 still holds inside a shared folder: two installs with the same config.name become
    one game, which shares the folder with the third, differently named game."""
    for folder, name in (("A-v1", "Game A"), ("A-v2", "Game A"), ("B", "Game B")):
        build_game(games_root, simple_spec(folder, "Shared", "Shared-1", name=name), saves_root)
    report = scan(games_root, saves_root=saves_root)
    assert [(g.display_name, len(g.installs)) for g in report.games] == [
        ("Game A", 2),
        ("Game B", 1),
    ]
    assert report.games[0].shared_global_with == [str(games_root / "B")]
    assert report.games[1].shared_global_with == [str(games_root / "A-v1")]


def test_f5_dynamic_savedir(games_root: Path, saves_root: Path) -> None:
    build_f5(games_root, saves_root)
    game = only_game(scan(games_root, saves_root=saves_root))
    assert game.metadata_state is MetadataState.DYNAMIC
    assert game.status is GameStatus.CANDIDATE
    assert game.source("global").status is SourceStatus.CANDIDATE
    assert game.source("local").status is SourceStatus.CONFIRMED
    assert any("runtime code may change save location" in w for w in game.warnings)


def test_f6_name_match_candidate(games_root: Path, saves_root: Path) -> None:
    build_f6(games_root, saves_root)
    game = only_game(scan(games_root, saves_root=saves_root))
    assert game.metadata_state is MetadataState.MISSING
    assert game.status is GameStatus.CANDIDATE
    candidate = game.source("global")
    assert candidate.status is SourceStatus.CANDIDATE
    assert candidate.method == "dir_name_match"
    assert candidate.relative_root == "NamelessQuest-123456"
    assert candidate.evidence[0].method == "dir_name_match"
    assert game.source("local").status is SourceStatus.CONFIRMED


def test_f7_unity_is_unsupported_and_not_descended(games_root: Path) -> None:
    root = build_f7(games_root)
    game = only_game(scan(games_root, saves_root=None))
    assert game.game_root == str(root)
    assert (game.engine, game.status) == ("unity", GameStatus.UNSUPPORTED)
    assert game.sources == []


def test_f16_electron_and_nwjs_are_unsupported_and_not_descended(games_root: Path) -> None:
    """D34: Electron and NW.js games are labelled, and their deep web-app trees are not walked
    (no depth-limit skips from inside them, no Ren'Py game found inside them)."""
    electron = build_f16(games_root)
    nwjs = build_nwjs(games_root)
    report = scan(games_root, saves_root=None)
    found = {g.game_root: (g.engine, g.status) for g in report.games}
    assert found == {
        str(electron): ("electron", GameStatus.UNSUPPORTED),
        str(nwjs): ("nwjs", GameStatus.UNSUPPORTED),
    }
    assert report.skipped == [], "nothing inside them was visited"
    (game,) = [g for g in report.games if g.engine == "electron"]
    assert game.evidence[0].file == "resources/app" and "Electron" in game.warnings[0]
    assert next(g for g in report.games if g.engine == "nwjs").evidence[0].file == "package.json"


def deep_tree(games_root: Path, count: int) -> list[Path]:
    """``count`` folders nested deeper than the scan depth: each one is skipped (depth_limit)."""
    leaves = []
    for index in range(count):
        leaf = games_root.joinpath(f"deep{index}", *(f"l{n}" for n in range(8)))
        leaf.mkdir(parents=True)
        leaves.append(leaf)
    return leaves


def test_scan_timing_and_the_slowest_folders(
    games_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D34: every scan root's time; the ten slowest folders only past a minute."""
    from enginesave import scan as scan_module

    build_f1(games_root, games_root.parent / "saves")
    deep_tree(games_root, 2)
    report = scan(games_root, saves_root=None)
    timing = report.timing
    assert [r["root"] for r in timing["roots"]] == [str(games_root)]
    assert timing["total_seconds"] >= timing["roots"][0]["seconds"] >= 0
    assert timing["slowest"] == [], "a quick scan names no slow folders"
    monkeypatch.setattr(scan_module, "SLOW_SCAN_SECONDS", -1)  # as if it took minutes
    slowest = scan(games_root, saves_root=None).timing["slowest"]
    assert 0 < len(slowest) <= 10
    seconds = [s["seconds"] for s in slowest]
    assert seconds == sorted(seconds, reverse=True)
    assert all(Path(s["path"]).is_dir() for s in slowest)


def test_skipped_folders_are_summarised(games_root: Path, capsys) -> None:
    """D34: one line per scan by reason; --verbose lists each folder."""
    from enginesave.cli import main

    deep_tree(games_root, 5)
    assert main(["scan", str(games_root), "--renpy-saves-root", str(games_root)]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("  skipped:")]
    assert len(lines) == 1 and "depth_limit 5; e.g." in lines[0] and "(first 3)" in lines[0]
    assert "--verbose lists all" in lines[0] and "Scan time: " in out
    assert main(["scan", str(games_root), "--renpy-saves-root", str(games_root), "--verbose"]) == 0
    out = capsys.readouterr().out
    assert sum(line.startswith("  skipped:") for line in out.splitlines()) == 5


def test_electron_needs_its_runtime_files(games_root: Path) -> None:
    """A folder with resources/app but no Chromium runtime beside an .exe is not Electron."""
    root = build_f16(games_root)
    (root / "ffmpeg.dll").unlink()
    (root / "v8_context_snapshot.bin").unlink()
    report = scan(games_root, saves_root=None)
    assert not any(g.engine == "electron" for g in report.games)


def test_f8_unlinked_and_engine_folders(games_root: Path, saves_root: Path) -> None:
    built = build_f1(games_root, saves_root)
    misc = build_f8(saves_root)
    report = scan(games_root, saves_root=saves_root)
    names = {u.name: u for u in report.unlinked}
    assert set(names) == set(misc.unlinked)
    for name, hint in misc.unlinked.items():
        assert names[name].version_hint == hint
    assert names["Orphan-1500000000"].file_count == 2
    assert built.global_dir is not None and built.global_dir.name not in names
    assert report.summary()["UNLINKED"] == 2
    assert not scan(games_root, saves_root=saves_root, include_unlinked=False).unlinked


def test_f9_portable_renpy_data(games_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENGINESAVE_RENPY_SAVES_ROOT")
    built = build_f9(games_root)
    game = only_game(scan(games_root, saves_root=None))
    source = game.source("global")
    assert source.method == "portable:RenPy Data"
    assert source.path == str(built.global_dir)
    assert source.status is SourceStatus.CONFIRMED


def test_f10_sdk_is_skipped(games_root: Path) -> None:
    sdk = build_f10(games_root)
    report = scan(games_root, saves_root=None)
    assert report.games == []
    assert report.skipped == [{"path": str(sdk), "reason": "renpy_sdk"}]


def test_f11_junction_is_not_followed(games_root: Path, saves_root: Path) -> None:
    build_f1(games_root, saves_root)
    if not make_junction(games_root / "loop", games_root):
        pytest.skip("cannot create a junction or symlink here")
    report = scan(games_root, saves_root=saves_root)
    assert len(report.games) == 1
    assert {"path": str(games_root / "loop"), "reason": "symlink"} in report.skipped


def test_scan_is_read_only(games_root: Path, saves_root: Path) -> None:
    build_f1(games_root, saves_root)
    build_f2(games_root, saves_root)
    build_f5(games_root, saves_root)
    build_f8(saves_root)
    before = snapshot(games_root, saves_root)
    scan(games_root, saves_root=saves_root, compute_hashes=True)
    assert snapshot(games_root, saves_root) == before


def test_hash_mode_reports_identical_mirrors(games_root: Path, saves_root: Path) -> None:
    build_f1(games_root, saves_root)
    game = only_game(scan(games_root, saves_root=saves_root, compute_hashes=True))
    assert "identical by hash: 4/4 (local)" in game.source("global").notes


def test_excludes_depth_and_backup_repos(games_root: Path, saves_root: Path) -> None:
    deep = games_root / "a" / "b" / "c"
    build_game(deep, simple_spec("Deep Game", "DeepGame", "Deep-1"), saves_root)
    build_f1(games_root, saves_root)
    (games_root / "MyBackups" / "games").mkdir(parents=True)
    (games_root / "MyBackups" / "catalog.json").write_text("{}")
    report = scan(games_root, saves_root=saves_root)
    assert len(report.games) == 2
    assert {"path": str(games_root / "MyBackups"), "reason": "backup_repo"} in report.skipped
    shallow = scan(games_root, saves_root=saves_root, max_depth=2)
    assert [g.display_name for g in shallow.games] == ["Readable Game"]
    assert {"path": str(games_root / "a" / "b"), "reason": "depth_limit"} in shallow.skipped
    excluded = scan(games_root, saves_root=saves_root, excludes=["readable*"])
    assert [g.display_name for g in excluded.games] == ["Deep Game"]
    assert {"path": str(games_root / "Readable Game 1.0"), "reason": "excluded"} in excluded.skipped


def test_an_excluded_folder_by_its_path_or_a_pattern_is_not_entered(
    games_root: Path, saves_root: Path
) -> None:
    """D63: a folder chosen in the window is kept as its path -- matched as it is, even with
    ``[`` in its name (a glob set) -- and a typed pattern matches anywhere below a root."""
    from enginesave.discovery import is_excluded

    novels = games_root / "Novels [2026]" / "Saga"
    build_game(novels, simple_spec("Hidden Game", "HiddenGame", "Hidden-1"), saves_root)
    build_f1(games_root, saves_root)
    report = scan(games_root, saves_root=saves_root, excludes=[str(games_root / "Novels [2026]")])
    assert [g.display_name for g in report.games] == ["Readable Game"]
    assert {"path": str(games_root / "Novels [2026]"), "reason": "excluded"} in report.skipped
    by_pattern = scan(games_root, saves_root=saves_root, excludes=["*\\novels*"])
    assert [g.display_name for g in by_pattern.games] == ["Readable Game"]
    assert is_excluded(games_root / "Novels [2026]", [str(games_root / "novels [2026]") + "\\"])
    assert not is_excluded(games_root / "Novels [2026]" / "Saga", [str(games_root / "Novels")])
    chosen = str(games_root / "[2024] Novels")  # a full path: its brackets are its name
    assert is_excluded(games_root / "[2024] Novels", [chosen])
    assert not any(is_excluded(games_root / n, [chosen]) for n in ("2 Novels", "4 novels"))
    assert is_excluded(games_root / "2 Novels", [str(games_root / "[0-9]*")]), "with * a glob"


def test_report_is_sorted_and_stable(games_root: Path, saves_root: Path) -> None:
    build_f4b(games_root, saves_root)
    build_f3(games_root, saves_root)
    first = scan(games_root, saves_root=saves_root).to_dict()
    second = scan(games_root, saves_root=saves_root).to_dict()
    for when in ("generated_at", "timing"):  # the moment and the time the scan took vary
        first.pop(when), second.pop(when)
    assert first == second
    roots = [g["game_root"] for g in first["games"]]
    assert roots == sorted(roots, key=str.casefold)


def test_missing_root_is_a_warning(tmp_path: Path, saves_root: Path) -> None:
    report = scan(tmp_path / "nope", saves_root=saves_root)
    assert report.games == []
    assert any("not a directory" in w for w in report.warnings)
    assert report.games == [] and SourceKind.LOCAL.value == "local"
