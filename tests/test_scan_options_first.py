"""0.9.0 scan rules (D52, D53): options first and stop on a save directory, the three ways
back to the full parse, renamed archives, Ren'Py folders without scripts, Emuera."""

from __future__ import annotations

import os
import shutil
import zlib
from pathlib import Path

import pytest

from enginesave.backup import prepare_backup
from enginesave.engines.renpy import detector
from enginesave.engines.renpy.detector import Parsed
from enginesave.models import DetectedGame, GameStatus, MetadataState, ScanReport, SourceStatus
from enginesave.scan import ScanOptions, run_scan
from enginesave.scan_cache import ScanCache
from fixtures.builder import (
    GameSpec,
    build_engine_skeleton,
    build_f1,
    build_f2,
    build_f6,
    build_f13,
    build_f19,
    build_f19b,
    build_f20,
    build_f21,
    build_f22,
    build_game,
    build_many_scripts,
    options_stmts,
    render_rpy,
    rpa_bytes,
    rpyc_bytes,
    script_stmts,
    simple_spec,
)


def scan(root: Path, saves_root: Path) -> ScanReport:
    return run_scan(ScanOptions(roots=[root], saves_root_override=saves_root))


def only_game(report: ScanReport) -> DetectedGame:
    assert len(report.games) == 1, [g.game_root for g in report.games]
    return report.games[0]


def sd_files(game: DetectedGame) -> set[str]:
    return {e.file for e in game.evidence if "save_directory" in e.detail}


# --------------------------------------------------------------------------- options first


def test_options_with_a_save_directory_are_all_that_is_read(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """D52 1: ``options.rpyc`` alone (its ``.rpy`` is not read while it is readable)."""
    build_f1(games_root, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert reads == ["options.rpyc"]
    assert game.metadata_state is MetadataState.RESOLVED and game.status is GameStatus.CONFIRMED
    assert game.identity.renpy_save_directory == "ReadableGame-1600000001"
    assert sd_files(game) == {"game/options.rpyc"}


def test_options_in_another_case_are_read_once_under_their_own_name(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """``OPTIONS.RPYC``: found whatever its case, and labelled with its real name, so the full
    parse (options without a save directory) does not read it again."""
    built = build_f13(games_root, saves_root, name_alike_dir=False)
    for name in ("options.rpyc", "options.rpy"):
        (built.gamedir / name).rename(built.gamedir / name.upper())
    game = only_game(scan(games_root, saves_root))
    assert [r for r in reads if r.lower().startswith("options.")] == ["OPTIONS.RPYC"], reads
    found = [(e.file, e.detail) for e in game.evidence]
    options = {f for f, _ in found if "options" in f.lower()}
    assert options == {"game/OPTIONS.RPYC"} and len(found) == len(set(found)), found


def test_options_through_a_symlink_are_not_followed(
    games_root: Path, saves_root: Path, reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D20: an ``options.rpyc`` that is a symlink (or junction) is skipped, as the full parse
    skips it; the check is the one every walk uses (a real file symlink needs privileges)."""
    build_f1(games_root, saves_root)
    real = detector.is_reparse_point
    monkeypatch.setattr(
        detector,
        "is_reparse_point",
        lambda entry: getattr(entry, "name", "") == "options.rpyc" or real(entry),
    )
    game = only_game(scan(games_root, saves_root))
    assert reads == ["options.rpy"]
    assert game.identity.renpy_save_directory == "ReadableGame-1600000001", "from options.rpy"


def test_options_inside_an_archive_are_found_in_renpys_archive_order(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """No loose options: the archives in the order Ren'Py searches them (names in reverse),
    each index read once; ``tl/`` translations are never taken for options (``zz_tl.rpa``
    comes first and only has one). The options here are only an ``options.rpy``, so every
    index is looked at for an ``options.rpyc`` before that is read."""
    spec = simple_spec(
        "Packed", "Packed", "Packed-1", loose_rpy=False, loose_rpyc=False, rpa_scripts=True,
        rpa_name="scripts.rpa",
    )  # fmt: skip
    built = build_game(games_root, spec, saves_root)
    images = {f"images/{i}.png": b"\x89PNG" for i in range(3)}
    (built.gamedir / "images.rpa").write_bytes(rpa_bytes(images))
    translated = {"tl/de/options.rpy": render_rpy(options_stmts(save_directory=None))[0].encode()}
    (built.gamedir / "zz_tl.rpa").write_bytes(rpa_bytes(translated))
    game = only_game(scan(games_root, saves_root))
    assert reads == [
        "zz_tl.rpa#index",
        "scripts.rpa#index",
        "images.rpa#index",
        "scripts.rpa!options.rpy",
    ]
    assert game.identity.renpy_save_directory == "Packed-1"
    assert sd_files(game) == {"game/scripts.rpa!options.rpy"}


def test_a_compiled_options_in_a_lower_archive_beats_a_plain_one_above(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """Ren'Py loads ``options.rpyc`` from the first archive that has it and never a ``.rpy``
    from an archive: ``zz_src.rpa`` with only ``options.rpy`` does not shadow it."""
    built = build_game(games_root, no_folder_spec("Shadowed"), saves_root)
    for name in ("options.rpy", "options.rpyc", "script.rpy", "script.rpyc"):
        (built.gamedir / name).unlink()
    right = options_stmts(name="Shadowed", save_directory="Right-1")
    compiled = {"options.rpyc": rpyc_bytes(right, "game/options.rpy")}
    (built.gamedir / "archive.rpa").write_bytes(rpa_bytes(compiled))
    wrong = render_rpy(options_stmts(name="Shadowed", save_directory="Wrong-1"))[0].encode()
    (built.gamedir / "zz_src.rpa").write_bytes(rpa_bytes({"options.rpy": wrong}))
    game = only_game(scan(games_root, saves_root))
    assert reads == ["zz_src.rpa#index", "archive.rpa#index", "archive.rpa!options.rpyc"]
    assert game.identity.renpy_save_directory == "Right-1"


def test_a_patch_archive_wins_as_it_does_in_renpy(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """``patch.rpa`` and ``archive.rpa`` both hold options: Ren'Py loads the one in
    ``patch.rpa`` (it searches names in reverse), so its save folder is the game's."""
    built = build_game(games_root, no_folder_spec("Modded"), saves_root)
    for name in ("options.rpy", "options.rpyc", "script.rpy", "script.rpyc"):
        (built.gamedir / name).unlink()
    for archive, folder in (("archive.rpa", "Modded-1"), ("patch.rpa", "Modded-Mod")):
        stmts = options_stmts(name="Modded", save_directory=folder)
        members = {"options.rpyc": rpyc_bytes(stmts, "game/options.rpy")}
        (built.gamedir / archive).write_bytes(rpa_bytes(members))
    game = only_game(scan(games_root, saves_root))
    assert reads == ["patch.rpa#index", "patch.rpa!options.rpyc"]
    assert game.identity.renpy_save_directory == "Modded-Mod"


def test_a_loose_options_file_means_the_archives_are_not_opened(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    build_f2(games_root, saves_root)  # options.rpyc loose, more scripts in archive.rpa
    game = only_game(scan(games_root, saves_root))
    assert not any("#index" in r or "!" in r for r in reads), reads
    assert game.status is GameStatus.CONFIRMED


def test_options_win_over_another_script(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """F19b: options.rpyc says Split-B, the stale options.rpy Split-A, elsewhere.rpy Split-C.
    The compiled options win, no CONFLICT and no warning: neither the stale .rpy nor the
    other script is read (S-148)."""
    build_f19b(games_root, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert reads == ["options.rpyc"]
    assert game.metadata_state is MetadataState.RESOLVED
    assert game.identity.renpy_save_directory == "Split-B"
    assert game.status is GameStatus.CONFIRMED  # the local saves; Split-B has no folder
    assert game.source("global").status is SourceStatus.EXPECTED_ABSENT
    assert game.warnings == []


def test_a_stale_rpy_does_not_name_the_folder_when_the_rpyc_does_not(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """A readable options.rpyc without a save directory and a stale options.rpy with one:
    the options name no folder (the .rpyc is what runs), so every other script is read, as
    the full parse would."""
    rpyc = options_stmts(name="Stale", build_name="Stale", save_directory=None)
    rpy = options_stmts(name="Stale", build_name="Stale", save_directory="Stale-1")
    extra = {"other": [detector_define("Other-2")]}
    spec = GameSpec("Stale", "Stale", rpy, "Other-2", rpyc_stmts=rpyc, extra_scripts=extra)
    build_game(games_root, spec, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert "options.rpy" not in reads and "other.rpyc" in reads, reads
    assert game.metadata_state is MetadataState.RESOLVED
    assert game.identity.renpy_save_directory == "Other-2"
    assert "game/options.rpy" not in {e.file for e in game.evidence}


# ----------------------------------------------------------- the ways back to the full parse


def test_no_options_reads_every_compiled_script_and_skips_their_rpy(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """D52 2 (no options): F6 has script.rpy and script.rpyc; only the .rpyc is read."""
    build_f6(games_root, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert reads == ["script.rpyc"]
    assert game.metadata_state is MetadataState.MISSING


def test_unreadable_options_fall_back_to_the_full_parse(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """D52 2 (options cannot be read, and there is no options.rpy): every other script."""
    spec = simple_spec("Broken Options", "BrokenOptions", "BrokenOptions-1", loose_rpy=False)
    built = build_game(games_root, spec, saves_root)
    (built.gamedir / "options.rpyc").write_bytes(b"RENPY RPC2 broken beyond repair")
    (built.gamedir / "later.rpy").write_text(
        render_rpy(options_stmts(save_directory="BrokenOptions-1"))[0], encoding="utf-8"
    )
    game = only_game(scan(games_root, saves_root))
    assert reads.count("options.rpyc") == 1, "read once, not again by the full parse"
    assert {"script.rpyc", "later.rpy"} <= set(reads)
    assert game.identity.renpy_save_directory == "BrokenOptions-1"
    assert any("options.rpyc: compiled script not readable" in w for w in game.warnings)


def test_an_unreadable_rpyc_is_replaced_by_its_rpy(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    built = build_f1(games_root, saves_root)
    (built.gamedir / "options.rpyc").write_bytes(b"RENPY RPC2 broken")
    game = only_game(scan(games_root, saves_root))
    assert game.identity.renpy_save_directory == "ReadableGame-1600000001", "from options.rpy"
    assert "script.rpyc" not in reads, "options.rpy named the save directory: stop"


def test_an_options_rpyc_that_cannot_be_opened_is_replaced_by_its_rpy(
    games_root: Path, saves_root: Path, reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The I/O-error half: ``options.rpyc`` is locked; ``options.rpy`` is read instead."""
    build_f1(games_root, saves_root)
    counted = detector._read_bytes

    def locked(path: Path) -> bytes:
        if Path(path).name == "options.rpyc":
            raise PermissionError(13, "locked", str(path))
        return counted(path)

    monkeypatch.setattr(detector, "_read_bytes", locked)
    game = only_game(scan(games_root, saves_root))
    assert reads == ["options.rpy"]
    assert game.identity.renpy_save_directory == "ReadableGame-1600000001"
    assert any(w.startswith("game/options.rpyc: ") for w in game.warnings), game.warnings


def test_the_full_parse_reads_the_rpy_of_an_unreadable_rpyc(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """D52 2: a .rpyc that cannot be read gives way to its .rpy; a readable one does not."""
    spec = simple_spec(
        "Fallback", "Fallback", None, extra_scripts={"zz": [detector_define("Fallback-1")]}
    )
    built = build_game(games_root, spec, saves_root)
    (built.gamedir / "zz.rpyc").write_bytes(b"RENPY RPC2 broken beyond repair")
    game = only_game(scan(games_root, saves_root))
    assert {"zz.rpyc", "zz.rpy", "script.rpyc"} <= set(reads), reads
    assert "script.rpy" not in reads and "options.rpy" not in reads
    assert game.identity.renpy_save_directory == "Fallback-1"
    assert sd_files(game) == {"game/zz.rpy"}
    assert any(w.startswith("game/zz.rpyc: compiled script not readable") for w in game.warnings)


def test_the_rpy_fallback_takes_the_file_as_it_is_named_and_never_a_link(
    games_root: Path, saves_root: Path, reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ZZ.rpyc`` cannot be read: its ``zz.rpy`` is labelled ``game/zz.rpy``; and a
    ``.rpy`` that is a symlink (or junction) is not followed, as nowhere else (D20)."""
    spec = simple_spec(
        "Fallback", "Fallback", None, extra_scripts={"zz": [detector_define("Fallback-1")]}
    )
    built = build_game(games_root, spec, saves_root)
    (built.gamedir / "zz.rpyc").unlink()
    (built.gamedir / "ZZ.rpyc").write_bytes(b"RENPY RPC2 broken beyond repair")
    game = only_game(scan(games_root, saves_root))
    assert sd_files(game) == {"game/zz.rpy"}
    reads.clear()
    from enginesave import fsutil

    real = fsutil.is_reparse_point
    monkeypatch.setattr(
        fsutil, "is_reparse_point", lambda e: getattr(e, "name", "") == "zz.rpy" or real(e)
    )
    game = only_game(scan(games_root, saves_root))
    assert "zz.rpy" not in reads and game.identity.renpy_save_directory is None


def test_options_without_a_save_directory_still_check_every_script(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """D52 2 (options without save_directory): D19's local-only conclusion needs the other
    scripts read too (F13a)."""
    build_f13(games_root, saves_root, name_alike_dir=False)
    game = only_game(scan(games_root, saves_root))
    assert "script.rpyc" in reads
    assert (
        game.metadata_state is MetadataState.RESOLVED and game.identity.renpy_save_directory is None
    )


@pytest.mark.parametrize("folder", ["01_Resolution_Config", "ZZ_Config"])
def test_options_in_a_folder_below_game_are_read_first(
    games_root: Path, saves_root: Path, reads: list[str], folder: str
) -> None:
    """D60 1 (MILFs of Sunville S2): options in ``game/01_Resolution_Config/`` are found in
    the listing and read first; with 700 scripts beside them nothing else is read. A folder
    after ``script.rpyc`` by name too: the full parse would read that first."""
    build_many_scripts(games_root, saves_root, options_dir=folder)
    game = only_game(scan(games_root, saves_root))
    assert reads == ["options.rpyc"]
    assert game.status is GameStatus.CONFIRMED
    assert game.identity.renpy_save_directory == "ManyScripts-1600000700"
    assert sd_files(game) == {f"game/{folder}/options.rpyc"}


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [("aa/deep", "zz", "B"), ("aaa", "zz", "B"), ("b", "a", "B")],
    ids=["shallower", "shorter", "by-name"],
)
def test_of_two_options_folders_the_shallowest_then_the_shortest_wins(
    games_root: Path, saves_root: Path, first: str, second: str, expected: str
) -> None:
    other = options_stmts(name="Two Options", save_directory="Two-B")
    spec = GameSpec(
        "Two Options",
        "TwoOptions",
        options_stmts(name="Two Options", save_directory="Two-A"),
        "Two-A",
        options_dir=first,
        extra_scripts={f"{second}/options": other},
    )
    build_game(games_root, spec, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert game.identity.renpy_save_directory == f"Two-{expected}"


def test_options_and_scripts_in_tl_are_left_out(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """Translations (``tl/``, at any depth) are never read for the save directory."""
    wrong = options_stmts(name="Translated", save_directory="Wrong-1")
    spec = GameSpec(
        "Translated",
        "Translated",
        options_stmts(name="Translated", save_directory=None),
        "Right-1",
        extra_scripts={
            "tl/chinese/options": wrong,
            "story/tl/french/options": wrong,
            "zz_saves": [detector_define("Right-1")],
        },
    )
    build_game(games_root, spec, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert game.identity.renpy_save_directory == "Right-1"
    assert not any("/tl/" in f"/{e.file}" for e in game.evidence), game.evidence


def test_options_below_game_but_in_tl_are_left_out(games_root: Path, saves_root: Path) -> None:
    """D60 1 looks below ``game/`` only outside ``tl/``: a translation's options are not
    the game's, the full parse finds the folder."""
    spec = GameSpec(
        "Translated", "Translated", options_stmts(name="Translated", save_directory="Wrong-1"),
        "Right-1", options_dir="story/tl/french",
        extra_scripts={"zz_saves": [detector_define("Right-1")]},
    )  # fmt: skip
    build_game(games_root, spec, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert game.identity.renpy_save_directory == "Right-1"


@pytest.mark.parametrize(
    "content",
    [b"", b"x\x9c\x03\x00\x00\x00\x00\x01", zlib.compress(b"garbage, not a pickle")],
    ids=["empty", "zero", "not-a-pickle"],
)
def test_an_empty_options_rpyc_gives_way_to_its_rpy(
    games_root: Path, saves_root: Path, reads: list[str], content: bytes
) -> None:
    """D60: a 0-byte ``options.rpyc`` (a broken download, a quarantined stub) or one whose
    content is no pickle is unreadable, so the ``options.rpy`` beside it is read."""
    built = build_f1(games_root, saves_root)
    (built.gamedir / "options.rpyc").write_bytes(content)
    game = only_game(scan(games_root, saves_root))
    assert reads[:2] == ["options.rpyc", "options.rpy"]
    assert game.identity.renpy_save_directory == built.spec.save_directory
    assert game.status is GameStatus.CONFIRMED


def test_an_entry_from_0_9_0_for_an_empty_options_rpyc_is_not_used(
    games_root: Path, saves_root: Path, reads: list[str], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """0.9.0 (parser ``renpy-1``) cached an empty ``options.rpyc`` as "nothing in it";
    since D60 it is unreadable, so the cache's parser version changed and it is read again."""
    built = build_f1(games_root, saves_root)
    (built.gamedir / "options.rpyc").write_bytes(b"")
    path = tmp_path / "scan-cache.json"
    parser, parse = detector.PARSER, detector._parse_script
    monkeypatch.setattr(detector, "PARSER", "renpy-1")
    monkeypatch.setattr(detector, "_parse_script", lambda *args: Parsed([]))  # as 0.9.0 read it
    run_scan(
        ScanOptions(roots=[games_root], saves_root_override=saves_root, cache=ScanCache.load(path))
    )
    monkeypatch.setattr(detector, "PARSER", parser)  # not undo(): the reads fixture stays
    monkeypatch.setattr(detector, "_parse_script", parse)
    reads.clear()
    report = run_scan(
        ScanOptions(roots=[games_root], saves_root_override=saves_root, cache=ScanCache.load(path))
    )
    assert "options.rpy" in reads
    assert only_game(report).identity.renpy_save_directory == built.spec.save_directory


def test_the_full_parse_stops_after_the_second_script(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """D60 2 (LabRats2 Reformulate): options without a save directory, 700 scripts, the
    save directory in the one read second (``00_define``): two files are read."""
    build_many_scripts(games_root, saves_root, second=True)
    game = only_game(scan(games_root, saves_root))
    assert reads == ["options.rpyc", "00_define.rpyc"]
    assert game.identity.renpy_save_directory == "ManyScripts-1600000700"


def test_the_full_parse_in_an_archive_stops_at_the_first_script_too(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """Members in the same order as loose scripts: ``zz_config`` (a settings name) before
    the 50 ``chapter_*``, though after them by name."""
    options = options_stmts(name="Packed", save_directory=None)
    spec = GameSpec(
        "Packed", "Packed", options, "Packed-1", loose_rpy=False, loose_rpyc=False,
        rpa_scripts=True, script_count=50,
        extra_scripts={"zz_config": [detector_define("Packed-1")], "zz": [detector_define("X")]},
    )  # fmt: skip
    build_game(games_root, spec, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert game.identity.renpy_save_directory == "Packed-1" and game.status is GameStatus.CONFIRMED
    members = [r for r in reads if "!" in r]
    assert members[0] == "archive.rpa!options.rpy", members
    assert members[-1] == "archive.rpa!zz_config.rpy", members
    assert not any("chapter" in m for m in members), members


def test_00_comes_before_01_when_no_name_says_settings(games_root: Path, saves_root: Path) -> None:
    """D60 2: of two scripts without a settings word, ``00*`` is read before ``01*``."""
    spec = GameSpec(
        "Ranks", "Ranks", options_stmts(name="Ranks", save_directory=None), "Ranks-B",
        extra_scripts={"01_a": [detector_define("Ranks-A")], "00_b": [detector_define("Ranks-B")]},
    )  # fmt: skip
    build_game(games_root, spec, saves_root)
    assert only_game(scan(games_root, saves_root)).identity.renpy_save_directory == "Ranks-B"


def archives_with(gamedir: Path, archives: dict[str, dict[str, list]]) -> None:
    for archive, scripts in archives.items():
        members = {
            f"{name}.rpyc": rpyc_bytes(stmts, f"game/{name}.rpy") for name, stmts in scripts.items()
        }
        (gamedir / archive).write_bytes(rpa_bytes(members))


def test_the_full_parse_reads_patch_rpa_before_archive_rpa(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """The same script in ``archive.rpa`` and ``patch.rpa``: Ren'Py searches archive names
    in reverse and loads the first copy of a name, so ``patch.rpa``'s copy is the game's."""
    built = build_game(games_root, no_folder_spec("Patched"), saves_root)
    archives_with(
        built.gamedir,
        {
            "archive.rpa": {"definitions": [detector_define("Patched-Old")]},
            "patch.rpa": {"definitions": [detector_define("Patched-New")]},
        },
    )
    game = only_game(scan(games_root, saves_root))
    assert game.identity.renpy_save_directory == "Patched-New"
    assert [r for r in reads if ".rpa" in r] == ["patch.rpa#index", "patch.rpa!definitions.rpyc"]


def test_a_script_shadowed_by_an_earlier_copy_is_not_read(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """Ren'Py loads one copy of a name: ``archive.rpa``'s ``definitions`` is not, after the
    one in ``patch.rpa`` (which names no folder), nor ``script`` after the loose one."""
    built = build_game(games_root, no_folder_spec("Shadowed"), saves_root)
    archives_with(
        built.gamedir,
        {
            "archive.rpa": {
                "definitions": [detector_define("Shadowed-Old")],
                "script": [detector_define("Shadowed-Script")],
                "zz": [detector_define("Shadowed-1")],
            },
            "patch.rpa": {"definitions": script_stmts()},
        },
    )
    game = only_game(scan(games_root, saves_root))
    assert game.identity.renpy_save_directory == "Shadowed-1"
    assert "archive.rpa!definitions.rpyc" not in reads and "archive.rpa!script.rpyc" not in reads


def test_the_full_parse_opens_an_archive_once(
    games_root: Path, saves_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D38 with D60 2: reading an archive script by script (and stopping after any) still
    opens and stats it once, not once per script."""
    built = build_game(games_root, no_folder_spec("Opened"), saves_root)
    scripts: dict[str, list] = {f"chapter_{i:02}": script_stmts() for i in range(20)}
    scripts["zz_late"] = [detector_define("Opened-1")]
    archives_with(built.gamedir, {"archive.rpa": scripts})
    opened: list[str] = []
    stats: list[str] = []
    real_stat = os.stat

    def counting_open(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        opened.append(str(path))
        return open(path, *args, **kwargs)

    def counting_stat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if str(path).endswith(".rpa"):
            stats.append(str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(detector, "open", counting_open, raising=False)
    monkeypatch.setattr(detector.os, "stat", counting_stat)
    game = only_game(scan(games_root, saves_root))
    monkeypatch.undo()
    assert game.identity.renpy_save_directory == "Opened-1"
    assert len([p for p in opened if p.endswith(".rpa")]) == 1, opened
    assert len(stats) == 1, stats


def test_translations_inside_an_archive_are_left_out_at_any_depth(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    built = build_game(games_root, no_folder_spec("Nested"), saves_root)
    archives_with(
        built.gamedir,
        {
            "archive.rpa": {
                "story/tl/french/a": [detector_define("WrongNested")],
                "zz": [detector_define("Nested-1")],
            }
        },
    )
    game = only_game(scan(games_root, saves_root))
    assert game.identity.renpy_save_directory == "Nested-1"
    assert not any("/tl/" in r for r in reads), reads


def test_reading_everything_without_a_literal_is_still_dynamic(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """D60 2: a save directory set by code does not stop the parse; with no literal anywhere
    every script is read and the game is DYNAMIC (a CANDIDATE), as before. ``config_code``
    (read first of the others, a settings name) sets ``config.save_directory`` by code."""
    from fixtures.builder import DYNAMIC_SAVEDIR, PythonBlock

    coded = 'import os\nconfig.save_directory = os.environ.get("CODED", "Coded-1")'
    options = options_stmts(name="Coded", save_directory=None)
    spec = GameSpec(
        "Coded", "Coded", options, None, script_count=5,
        extra_scripts={
            "config_code": [PythonBlock(coded, early=True)],
            "zz_code": [PythonBlock(DYNAMIC_SAVEDIR, early=True)],
        },
    )  # fmt: skip
    build_game(games_root, spec, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert game.metadata_state is MetadataState.DYNAMIC and game.status is GameStatus.CANDIDATE
    assert len([r for r in reads if r.endswith(".rpyc")]) == 9, "options, script, 5, two coded"


def test_a_script_that_names_two_save_directories_is_a_conflict(
    games_root: Path, saves_root: Path
) -> None:
    """F19: options without a save directory, another script that names two: the one
    CONFLICT left, reached only by the full parse."""
    build_f19(games_root, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert game.metadata_state is MetadataState.CONFLICT and game.status is GameStatus.CONFLICT
    assert any("conflicting values for config.save_directory" in w for w in game.warnings)


def test_the_full_parse_stops_at_the_first_script_that_names_one(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """D60 2: of two scripts that disagree the first (by the priority order, then by name)
    is the one; the other is not read, so there is no conflict to see (README)."""
    build_f19(games_root, saves_root, one_script=False)
    game = only_game(scan(games_root, saves_root))
    assert game.status is GameStatus.CONFIRMED and game.identity.renpy_save_directory == "Split-A"
    assert "saves_b.rpyc" not in reads and "saves_a.rpyc" in reads


def test_archives_are_read_only_when_loose_scripts_did_not_name_the_folder(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    options = options_stmts(name="Mixed", save_directory=None)
    spec = GameSpec(
        "Mixed", "Mixed", options, "Mixed-1", rpa_scripts=True,
        extra_scripts={"zz_saves": [detector_define("Mixed-1")]},
    )  # fmt: skip
    build_game(games_root, spec, saves_root)
    game = only_game(scan(games_root, saves_root))
    assert game.identity.renpy_save_directory == "Mixed-1"
    assert not any("#index" in r for r in reads), "zz_saves.rpyc named it: no archive"


def no_folder_spec(name: str) -> GameSpec:
    """Loose options and script, neither naming a save folder: the full parse goes on."""
    return GameSpec(name, name, options_stmts(name=name, save_directory=None), None)


def test_archives_are_read_when_the_loose_scripts_named_no_folder(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """D52 2, the other half: nothing loose names the folder, so the archive is read (its
    index once), a .rpyc member instead of its .rpy, the .rpy of a broken .rpyc member, and
    never ``tl/``."""
    built = build_game(games_root, no_folder_spec("Archived"), saves_root)
    wrong = render_rpy([detector_define("WrongFromTranslation")])[0].encode()
    members = {
        "chapter.rpy": render_rpy(script_stmts())[0].encode(),
        "chapter.rpyc": rpyc_bytes(script_stmts(), "game/chapter.rpy"),
        "late.rpy": render_rpy([detector_define("Archived-1")])[0].encode(),
        "late.rpyc": b"RENPY RPC2 broken beyond repair",
        "tl/RU/options.rpy": wrong,
    }
    (built.gamedir / "data.rpa").write_bytes(rpa_bytes(members))
    game = only_game(scan(games_root, saves_root))
    archived = [r for r in reads if r.startswith("data.rpa")]
    assert archived == [
        "data.rpa#index",
        "data.rpa!chapter.rpyc",
        "data.rpa!late.rpyc",
        "data.rpa!late.rpy",
    ]
    assert game.identity.renpy_save_directory == "Archived-1"
    assert sd_files(game) == {"game/data.rpa!late.rpy"}
    assert not any("WrongFromTranslation" in e.detail for e in game.evidence)


def test_an_unreadable_archive_is_reported_once(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """Options first looks into the archives, the full parse again: one index read, one
    warning."""
    spec = GameSpec("Damaged", "Damaged", save_directory=None, options=False)
    built = build_game(games_root, spec, saves_root)
    (built.gamedir / "bad.rpa").write_bytes(b"RPA-3.0 not hexadecimal at all\n")
    game = only_game(scan(games_root, saves_root))
    assert reads.count("bad.rpa#index") == 1
    assert sum("archive index not readable" in w for w in game.warnings) == 1, game.warnings


def detector_define(save_directory: str):  # noqa: ANN201 - a fixture statement
    from fixtures.builder import Define

    return Define("config", "save_directory", f'"{save_directory}"')


# -------------------------------------------------------------------------- 700 scripts


def test_seven_hundred_scripts_read_only_the_options_and_agree_with_the_full_parse(
    games_root: Path, saves_root: Path, reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D52 acceptance (by count; the timings are in EXECUTION_LOG): the performance game is
    read through its options.rpyc, with the same result as reading all 700 compiled ones."""
    build_many_scripts(games_root, saves_root)
    fast = only_game(scan(games_root, saves_root))
    assert reads == ["options.rpyc"]
    reads.clear()
    monkeypatch.setattr(detector, "_options_first", lambda *_a: False)
    monkeypatch.setattr(detector, "_names_save_directory", lambda _a: False)  # read them all
    full = only_game(scan(games_root, saves_root))
    assert len(reads) == 700, "every .rpyc, no .rpy"
    assert fast.identity == full.identity and fast.status is full.status

    def where(game: DetectedGame) -> list[tuple[object, ...]]:  # evidence differs by design
        return [(s.source_id, s.path, s.status, s.method, s.file_count) for s in game.sources]

    assert where(fast) == where(full)


# ---------------------------------------------------------------------- D53 and Emuera


@pytest.mark.parametrize("version", ["3.0", "2.0"])
def test_a_renamed_archive_is_read_by_its_header(
    games_root: Path, saves_root: Path, version: str
) -> None:
    """F20 (D53 1): ``game/archive.liu`` is an RPA archive; nothing has a script suffix."""
    build_f20(games_root, saves_root, version=version)
    game = only_game(scan(games_root, saves_root))
    assert game.metadata_state is MetadataState.RESOLVED and game.status is GameStatus.CONFIRMED
    assert sd_files(game) == {"game/archive.liu!options.rpy"}
    assert "game/archive.liu: an RPA archive under another name, read as one" in game.warnings


def test_a_renamed_archive_is_read_options_first(
    games_root: Path, saves_root: Path, reads: list[str]
) -> None:
    """Its options name the folder: the other members are not read."""
    build_f20(games_root, saves_root)
    only_game(scan(games_root, saves_root))
    assert reads == ["archive.liu#index", "archive.liu!options.rpy"]


def test_a_renpy_folder_without_scripts_but_with_saves_is_a_candidate(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """F21 (D53 2): the scripts are packed in a format of the game's own. The local saves are
    confirmed (and backed up), the global folder is a candidate by name, nothing UNSUPPORTED."""
    built = build_f21(games_root, saves_root)
    report = scan(games_root, saves_root)
    game = only_game(report)
    assert game.engine == "renpy" and game.metadata_state is MetadataState.MISSING
    assert game.status is GameStatus.CANDIDATE
    assert game.warnings[0] == detector.SCRIPTLESS
    local, global_ = game.source("local"), game.source("global")
    assert local.status is SourceStatus.CONFIRMED and local.file_count == len(built.local_files)
    assert global_.status is SourceStatus.CANDIDATE and global_.method == "dir_name_match"
    assert global_.relative_root == "CityRestart-1600000021"
    assert "CityRestart-1600000021" not in [u.name for u in report.unlinked]
    plan = prepare_backup([games_root], tmp_path / "repo", saves_root_override=saves_root)
    assert [[s.source.source_id for s in g.sources] for g in plan.games] == [["local"]]


@pytest.mark.parametrize(
    ("game_dir", "saves", "launcher", "kept"),
    [
        (False, False, False, False),  # neither game\ nor a launcher (D53 3)
        (True, False, True, True),  # game\ beside a launcher (D53 2)
        (True, True, False, True),  # game\saves alone (D53 2)
        (False, False, True, False),  # a launcher alone: no game\ (S-150)
        (True, False, False, False),  # an empty game\ alone (S-150)
    ],
)
def test_a_renpy_folder_without_scripts(
    games_root: Path, saves_root: Path, game_dir: bool, saves: bool, launcher: bool, kept: bool
) -> None:
    """D53: no script anywhere. With ``game\\saves``, or ``game\\`` beside a launcher, the
    game is kept (its saves are backed up); otherwise it stays UNSUPPORTED."""
    root = games_root / "Shell"
    build_engine_skeleton(root, "Shell", "8.1.3.23091805")
    if not launcher:
        for stub in ("Shell.exe", "Shell.py", "Shell.sh"):
            (root / stub).unlink()
    if game_dir:
        (root / "game").mkdir()
    if saves:
        (root / "game" / "saves").mkdir()
        (root / "game" / "saves" / "1-1-LT1.save").write_bytes(b"PK synthetic save")
    game = only_game(scan(games_root, saves_root))
    if kept:
        assert game.status is not GameStatus.UNSUPPORTED
        assert game.warnings[0] == detector.SCRIPTLESS
    else:
        assert game.status is GameStatus.UNSUPPORTED
        assert game.warnings == ["renpy structure without scripts"]


@pytest.mark.parametrize("exe", [True, False])
def test_emuera_is_recognised_and_not_entered(
    games_root: Path, saves_root: Path, exe: bool
) -> None:
    """F22 (D52 4): ``Emuera*.exe`` (any case), or ``ERB/`` with ``CSV/``."""
    root = build_f22(games_root, exe=exe)
    report = scan(games_root, saves_root)
    game = only_game(report)
    assert (game.engine, game.status, game.game_root) == (
        "emuera",
        GameStatus.UNSUPPORTED,
        str(root),
    )
    assert game.warnings[0].startswith("Emuera is not supported")
    assert report.skipped == []


def test_an_emuera_launcher_alone_is_enough(games_root: Path, saves_root: Path) -> None:
    """``emuera.EXE`` without ``ERB/`` or ``CSV/`` (the launcher rule on its own, any case)."""
    root = build_f22(games_root, exe=True)
    shutil.rmtree(root / "ERB")
    shutil.rmtree(root / "CSV")
    (root / "EMUERA1824+v13.exe").rename(root / "emuera.EXE")
    game = only_game(scan(games_root, saves_root))
    assert (game.engine, game.status) == ("emuera", GameStatus.UNSUPPORTED)


def test_erb_alone_is_not_emuera(games_root: Path, saves_root: Path) -> None:
    root = build_f22(games_root, exe=False)
    (root / "CSV" / "Chara0.csv").unlink()
    (root / "CSV").rmdir()
    report = scan(games_root, saves_root)
    assert [g.engine for g in report.games] == ["renpy"], "the game inside ERB/ is found"
    assert script_stmts()  # the hidden game is a real fixture game
