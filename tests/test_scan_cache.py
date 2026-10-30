"""The scan's parse cache (0.9.0, D52 3): a second scan reads no script; a changed file is
read again; a damaged cache is rebuilt; nothing is written inside a scanned folder; the
command line and the window keep it beside gui.json, and ``scan --no-cache`` leaves it be."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from enginesave import scan_cache
from enginesave.cli import main
from enginesave.engines.renpy import detector
from enginesave.scan import ScanOptions, run_scan
from enginesave.scan_cache import ScanCache
from fixtures.builder import build_f13, build_f17, build_game, build_many_scripts, simple_spec


def scan(root: Path, saves_root: Path, cache: ScanCache | None):  # noqa: ANN201
    return run_scan(ScanOptions(roots=[root], saves_root_override=saves_root, cache=cache))


def games(report) -> list[dict]:  # noqa: ANN001
    return [g.to_dict() for g in report.games]


@pytest.fixture
def mixed_games(games_root: Path, saves_root: Path) -> Path:
    """A full parse (F13a: options without save_directory), an archive-only game and the D50
    renamed headers (F17), so loose scripts, archive members and notes all go through it."""
    build_f13(games_root, saves_root, name_alike_dir=False)
    packed = simple_spec(
        "Packed", "Packed", "Packed-1", loose_rpy=False, loose_rpyc=False, rpa_scripts=True
    )
    build_game(games_root, packed, saves_root)
    build_f17(games_root, saves_root)
    return games_root


def test_a_second_scan_reads_no_script(
    mixed_games: Path, saves_root: Path, tmp_path: Path, reads: list[str]
) -> None:
    path = tmp_path / "config" / "scan-cache.json"
    first = scan(mixed_games, saves_root, ScanCache.load(path))
    assert reads and path.is_file()
    reads.clear()
    cache = ScanCache.load(path)  # from the file, as the next run would
    second = scan(mixed_games, saves_root, cache)
    assert reads == []
    assert cache.hits > 0 and cache.misses == 0
    assert games(second) == games(first), "the same games, sources, evidence and warnings"


def test_a_changed_script_is_read_again_and_only_it(
    games_root: Path, saves_root: Path, tmp_path: Path, reads: list[str]
) -> None:
    built = build_f13(games_root, saves_root, name_alike_dir=False)
    path = tmp_path / "scan-cache.json"
    scan(games_root, saves_root, ScanCache.load(path))
    reads.clear()
    script = built.gamedir / "script.rpyc"
    info = script.stat()
    os.utime(script, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    scan(games_root, saves_root, ScanCache.load(path))
    assert reads == ["script.rpyc"]


def test_another_parser_version_reads_everything_again(
    games_root: Path, saves_root: Path, tmp_path: Path, reads: list[str], monkeypatch
) -> None:
    build_f13(games_root, saves_root, name_alike_dir=False)
    path = tmp_path / "scan-cache.json"
    scan(games_root, saves_root, ScanCache.load(path))
    first = list(reads)
    reads.clear()
    monkeypatch.setattr(detector, "PARSER", "renpy-test")
    scan(games_root, saves_root, ScanCache.load(path))
    assert sorted(reads) == sorted(first)


@pytest.mark.parametrize("damage", ["{ not json", '{"format": 99, "entries": {}}', "[]"])
def test_a_damaged_cache_is_ignored_and_rebuilt(
    games_root: Path, saves_root: Path, tmp_path: Path, reads: list[str], damage: str
) -> None:
    build_f13(games_root, saves_root, name_alike_dir=False)
    path = tmp_path / "scan-cache.json"
    path.write_text(damage, encoding="utf-8")
    report = scan(games_root, saves_root, ScanCache.load(path))
    assert reads and report.games[0].identity.config_name == "Local Only Game"
    assert json.loads(path.read_text(encoding="utf-8"))["format"] == scan_cache.CACHE_FORMAT


def test_a_damaged_entry_is_parsed_again(
    mixed_games: Path, saves_root: Path, tmp_path: Path, reads: list[str]
) -> None:
    """Valid JSON, nonsense inside: every entry is dropped and its file read again; the scan
    gives the same result and the file is repaired."""
    path = tmp_path / "scan-cache.json"
    first = scan(mixed_games, saves_root, ScanCache.load(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, entry in data["entries"].items():
        bad = {"dict": "not a mapping"}
        entry["value"] = (
            "not a list"
            if key.endswith("|#index")
            else {
                "assignments": [["config.name", "=", bad, "f", "m", "d", 1]],
                "notes": [],
                "error": None,
            }
        )
    path.write_text(json.dumps(data), encoding="utf-8")
    count = len(reads)
    reads.clear()
    second = scan(mixed_games, saves_root, ScanCache.load(path))
    assert len(reads) == count and games(second) == games(first)
    reads.clear()
    scan(mixed_games, saves_root, ScanCache.load(path))
    assert reads == [], "the entries were written anew"


def test_a_cache_that_cannot_be_serialised_is_not_written(tmp_path: Path) -> None:
    cache = ScanCache(tmp_path / "scan-cache.json")
    cache.put(tmp_path / "x.rpyc", 1, 1, detector.PARSER, object())
    assert cache.save() is False and not cache.path.exists()


def test_a_lone_surrogate_does_not_block_the_file(tmp_path: Path) -> None:
    """A pickled name may hold half a surrogate pair; the file is still written (escaped)
    and reads back the same."""
    path = tmp_path / "scan-cache.json"
    cache = ScanCache(path)
    cache.put(tmp_path / "x.rpyc", 1, 1, detector.PARSER, {"name": "broken \ud800 name"})
    assert cache.save() is True
    again = ScanCache.load(path)
    assert again.get(tmp_path / "x.rpyc", 1, 1, detector.PARSER) == {"name": "broken \ud800 name"}


def test_a_huge_number_in_a_script_neither_fails_the_scan_nor_blocks_the_cache(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """``config.version = 0xfff…`` (3700 digits): the value is dynamic, the scan goes on and
    every entry is written."""
    from fixtures.builder import Define, options_stmts

    stmts = options_stmts(name="Huge", save_directory="Huge-1")
    stmts.append(Define("config", "version", "0x" + "f" * 3700))
    build_game(games_root, simple_spec("Huge", "Huge", "Huge-1", rpyc_stmts=stmts), saves_root)
    path = tmp_path / "scan-cache.json"
    report = scan(games_root, saves_root, ScanCache.load(path))
    assert report.games[0].identity.renpy_save_directory == "Huge-1"
    assert path.is_file() and json.loads(path.read_text(encoding="utf-8"))["entries"]


def test_a_deeply_nested_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "scan-cache.json"
    path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    assert ScanCache.load(path).entries == {}


def test_a_name_changed_only_in_case_is_parsed_again(
    games_root: Path, saves_root: Path, tmp_path: Path, reads: list[str]
) -> None:
    """The cache key ignores case (Windows), the evidence does not: after ``options.rpyc``
    becomes ``OPTIONS.RPYC`` the cached scan says what an uncached one says."""
    built = build_f13(games_root, saves_root, name_alike_dir=False)
    path = tmp_path / "scan-cache.json"
    scan(games_root, saves_root, ScanCache.load(path))
    (built.gamedir / "options.rpyc").rename(built.gamedir / "OPTIONS.RPYC")
    reads.clear()
    cached = scan(games_root, saves_root, ScanCache.load(path))
    assert reads == ["OPTIONS.RPYC"]
    assert games(cached) == games(scan(games_root, saves_root, None))


def test_old_entries_go_and_this_scans_stay(
    games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    build_f13(games_root, saves_root, name_alike_dir=False)
    path = tmp_path / "scan-cache.json"
    cache = ScanCache.load(path)
    cache.put(tmp_path / "gone.rpyc", 1, 1, detector.PARSER, {"assignments": []})
    cache.entries[cache.key(tmp_path / "gone.rpyc")]["used"] = 1  # 1970: long unused
    cache.used.clear()
    scan(games_root, saves_root, cache)
    kept = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert cache.key(tmp_path / "gone.rpyc") not in kept and kept


def test_entries_used_within_thirty_days_stay_and_older_ones_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D62 ④: both halves of the rule and its edge -- used yesterday stays, used exactly 30
    days ago stays, one second older goes; this scan's own entries always stay."""
    now = 2_000_000_000
    monkeypatch.setattr(scan_cache.time, "time", lambda: now)
    path = tmp_path / "scan-cache.json"
    cache = ScanCache.load(path)
    used = {
        "yesterday": now - 24 * 3600,
        "edge": now - scan_cache.KEEP_SECONDS,
        "older": now - scan_cache.KEEP_SECONDS - 1,
        "this-scan": 1,
    }
    for name, when in used.items():
        cache.put(tmp_path / f"{name}.rpyc", 1, 1, detector.PARSER, {"assignments": []})
        cache.entries[cache.key(tmp_path / f"{name}.rpyc")]["used"] = when
    cache.used = {cache.key(tmp_path / "this-scan.rpyc")}
    assert cache.save()
    kept = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert {name for name in used if cache.key(tmp_path / f"{name}.rpyc") in kept} == {
        "yesterday",
        "edge",
        "this-scan",
    }


def test_backup_no_cache_neither_reads_nor_writes_it(
    games_root: Path, saves_root: Path, tmp_path: Path, isolated_env: Path
) -> None:
    """D62 ③: ``backup --no-cache`` as ``scan --no-cache``; ``--dry-run`` writes no library
    but still updates the cache, unless ``--no-cache``."""
    build_f13(games_root, saves_root, name_alike_dir=False)
    cache_file = isolated_env / "AppData" / "Roaming" / "EngineSave" / "scan-cache.json"
    base = ["backup", str(games_root), "--dest", str(tmp_path / "library")]
    base += ["--renpy-saves-root", str(saves_root), "--dry-run"]
    assert main([*base, "--no-cache"]) == 0
    assert not cache_file.exists() and not (tmp_path / "library").exists()
    assert main(base) == 0
    assert cache_file.is_file() and not (tmp_path / "library").exists()


def test_nothing_is_written_inside_a_scanned_folder(
    games_root: Path, saves_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """...and the log says so (D60 3), e.g. a portable copy kept inside a scan root."""
    build_f13(games_root, saves_root, name_alike_dir=False)
    inside = games_root / "config" / "scan-cache.json"
    with caplog.at_level("INFO", logger="enginesave.scan_cache"):
        scan(games_root, saves_root, ScanCache.load(inside))
    assert not inside.exists()
    assert any(
        "scan cache not written" in r.getMessage() and str(games_root) in r.getMessage()
        for r in caplog.records
    )


def test_a_cache_that_cannot_be_written_does_not_stop_the_scan(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    build_f13(games_root, saves_root, name_alike_dir=False)
    blocker = tmp_path / "a file"
    blocker.write_text("", encoding="utf-8")
    report = scan(games_root, saves_root, ScanCache.load(blocker / "scan-cache.json"))
    assert len(report.games) == 1


def test_the_command_line_keeps_it_beside_gui_json(
    games_root: Path, saves_root: Path, isolated_env: Path, reads: list[str], capsys
) -> None:
    """``scan`` and ``backup`` use ``%APPDATA%\\EngineSave\\scan-cache.json`` (the sandbox's
    here); ``scan --no-cache`` neither reads nor writes it; the exit codes stay the same."""
    build_many_scripts(games_root, saves_root, options=False)  # every script is read
    path = isolated_env / "AppData" / "Roaming" / "EngineSave" / "scan-cache.json"
    args = ["scan", str(games_root), "--renpy-saves-root", str(saves_root), "--json"]
    assert main([*args, "--no-cache"]) == 0 and not path.exists()
    assert len(reads) == 701, "options.rpyc, then the other 700 compiled scripts"
    reads.clear()
    assert main(args) == 0 and path.is_file()
    reads.clear()
    assert main(args) == 0 and reads == [], "the second scan reads no script"
    before = path.stat().st_mtime_ns
    assert main([*args, "--no-cache"]) == 0 and len(reads) == 701
    assert path.stat().st_mtime_ns == before, "--no-cache leaves the file alone"
    reads.clear()
    dest = isolated_env / "library"
    backup = ["backup", str(games_root), "--dest", str(dest), "--yes"]
    assert main([*backup, "--renpy-saves-root", str(saves_root), "--json"]) == 0
    assert reads == [], "the rescan before the backup comes from the cache"
    capsys.readouterr()


def test_confirm_source_works_without_a_saves_root_override(
    games_root: Path, saves_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """``game_at`` with no environment given takes the process's (0.8.0 passed None and a
    real ``confirm-source --game-root`` without an override failed)."""
    from enginesave.linking import game_at

    built = build_f13(games_root, saves_root, name_alike_dir=False)
    monkeypatch.delenv("ENGINESAVE_RENPY_SAVES_ROOT", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    game = game_at(built.root)
    assert game.game_root == str(built.root)
