"""0.10.0 first tier (D65, M40): RPG Maker MV/MZ, XP/VX/VX Ace, Wolf RPG Editor and KiriKiri --
what the scan finds in synthetic games (tests/fixtures/engines.py), and a backup, deletion and
restore of each that gives back every save byte for byte."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from enginesave.backup import execute_backup, prepare_backup
from enginesave.cleanup import GAME_ROOT, plan_cleanup
from enginesave.cli import main
from enginesave.errors import UsageError
from enginesave.models import DetectedGame, GameStatus, RootToken, SourceStatus
from enginesave.repo import Repo
from enginesave.restore import execute_restore, plan_restore
from enginesave.scan import ScanOptions, run_scan
from fixtures.engines import (
    Built,
    build_kirikiri,
    build_mv,
    build_mz,
    build_rgss,
    build_rpg2000,
    build_wolf,
)


def scan(root: Path, repo: Path | None = None):  # noqa: ANN201
    return run_scan(ScanOptions(roots=[root], repo=repo))


def only(root: Path) -> DetectedGame:
    games = scan(root).games
    assert len(games) == 1, [(g.game_root, g.engine) for g in games]
    return games[0]


def back_up(root: Path, dest: Path) -> Repo:
    plan = prepare_backup([root], dest)
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    return repo


ROUND_TRIPS: list[tuple[str, Callable[[Path], Built], str]] = [
    ("mv", build_mv, "rpgmaker_mv"),
    ("mz", build_mz, "rpgmaker_mz"),
    ("xp", lambda p: build_rgss(p, "xp"), "rpgmaker_xp"),
    ("vx", lambda p: build_rgss(p, "vx"), "rpgmaker_vx"),
    ("vxace", lambda p: build_rgss(p, "vxace", encrypted=False), "rpgmaker_vxace"),
    ("wolf", build_wolf, "wolf_rpg"),
    ("kirikiri", build_kirikiri, "kirikiri"),
]


@pytest.mark.parametrize(("name", "build", "engine"), ROUND_TRIPS, ids=[r[0] for r in ROUND_TRIPS])
def test_scan_backup_delete_restore_gives_every_save_back(
    games_root: Path, tmp_path: Path, name: str, build: Callable[[Path], Built], engine: str
) -> None:
    built = build(games_root)
    game = only(games_root)
    assert (game.engine, game.status) == (engine, GameStatus.CONFIRMED), game.warnings
    assert sum(s.file_count for s in game.sources if s.status is SourceStatus.CONFIRMED) == len(
        built.saves
    )
    repo = back_up(games_root, tmp_path / "repo")
    game_id = repo.load_catalog()["games"][0]["game_id"]
    manifest = repo.load_manifest(game_id, repo.latest_snapshot(game_id))
    assert len(manifest["files"]) == len(built.saves), "the saves and nothing else"
    for path in built.saves:
        path.unlink()
    result = execute_restore(plan_restore(repo, repo.load_catalog(), game_id))
    assert result.failures == [] and result.written == len(built.saves)
    for path, data in {**built.saves, **built.others}.items():
        assert path.read_bytes() == data, path


def sandbox(token: str) -> Path:
    return Path(os.environ[f"ENGINESAVE_ROOT_{token}"])  # pointed into the sandbox (conftest)


def kirikiri_in(token: str, macro: str) -> Callable[[Path], Built]:
    def build(parent: Path) -> Built:
        built = build_kirikiri(parent, cf=f"$({macro})\\Maker\\Title", saves=False)
        moved = sandbox(token) / "Maker" / "Title"
        built.write(moved / "data0.kdt", b"slot zero", save=True)
        built.write(moved / "datasu.ksd", b"system", save=True)
        return built

    return build


def rgss_in_program_files(parent: Path) -> Built:
    program_files = parent / "Program Files"
    os.environ["ProgramFiles"] = str(program_files)  # restored by the test's monkeypatch
    built = build_rgss(program_files, "vx", saves=False)
    relative = Path(os.path.abspath(built.root)).parts[1:]
    built.write(sandbox("VIRTUALSTORE").joinpath(*relative) / "Save1.rvdata", b"vx", save=True)
    return built


def mkxp_game(parent: Path) -> Built:
    built = build_rgss(parent, "xp", saves=False)
    (built.root / "mkxp.json").write_bytes(b'{"dataPathOrg": "Team", "dataPathApp": "Mon"}')
    built.write(sandbox("APPDATA") / "Team" / "Mon" / "Game.rxdata", b"mon", save=True)
    return built


NEW_ROOTS: list[tuple[str, Callable[[Path], Built], str]] = [
    ("documents", kirikiri_in("DOCUMENTS", "personalpath"), "kirikiri"),
    ("saved-games", kirikiri_in("SAVED_GAMES", "savedgamespath"), "kirikiri"),
    ("virtualstore", rgss_in_program_files, "rpgmaker_vx"),
    ("mkxp", mkxp_game, "rpgmaker_xp"),
]


@pytest.mark.parametrize(("name", "build", "engine"), NEW_ROOTS, ids=[r[0] for r in NEW_ROOTS])
def test_saves_in_the_new_roots_go_back_where_they_were(
    games_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    build: Callable[[Path], Built],
    engine: str,
) -> None:
    """0.11.0 (D69 3, 4): 「文档」, 「保存的游戏」, VirtualStore and mkxp's folder, each in the
    sandbox: scanned, backed up, deleted, restored."""
    monkeypatch.setenv("ProgramFiles", os.environ.get("ProgramFiles", ""))
    test_scan_backup_delete_restore_gives_every_save_back(games_root, tmp_path, name, build, engine)


# ---------------------------------------------------------------------------- MV and MZ


def test_mv_title_version_and_the_save_folder_below_www(games_root: Path) -> None:
    build_mv(games_root, title="Hero's Tale", window_title="Window Name")
    game = only(games_root)
    assert (game.display_name, game.identity.engine_key, game.engine_version) == (
        "Hero's Tale",
        "Hero's Tale",
        "1.6.2",
    )
    (save, root_save) = game.sources
    assert (save.root_token, save.relative_root, save.file_count) == (
        RootToken.GAME_ROOT,
        "www/save",
        4,
    )
    assert (root_save.relative_root, root_save.status) == ("save", SourceStatus.EXPECTED_ABSENT)
    assert any(e.detail == "title 'Hero''s Tale'" or "Hero" in e.detail for e in game.evidence)


def test_mv_without_a_system_title_takes_the_window_title_and_a_default_title_is_no_key(
    games_root: Path,
) -> None:
    build_mv(games_root, "Untitled MV", title=None, window_title="From Window")
    assert only(games_root).display_name == "From Window"
    shutil.rmtree(games_root / "Untitled MV")
    build_mv(games_root, "Lazy MV", title="Project1", window_title="")
    game = only(games_root)
    assert (game.display_name, game.identity.engine_key) == ("Project1", None)


def test_mv_saves_in_both_folders_are_both_kept_with_a_warning(games_root: Path) -> None:
    build_mv(games_root, root_saves=True)
    game = only(games_root)
    assert [s.status for s in game.sources] == [SourceStatus.CONFIRMED, SourceStatus.CONFIRMED]
    assert any("saves in both" in w for w in game.warnings)


def test_mv_and_mz_without_saves_are_not_found(games_root: Path) -> None:
    build_mv(games_root, saves=False)
    build_mz(games_root, saves=False)
    games = scan(games_root).games
    assert sorted(g.engine for g in games) == ["rpgmaker_mv", "rpgmaker_mz"]
    assert all(g.status is GameStatus.NOT_FOUND for g in games)
    assert all(s.status is SourceStatus.EXPECTED_ABSENT for g in games for s in g.sources)


def test_mz_keeps_the_autosave_and_a_cut_write(games_root: Path) -> None:
    build_mz(games_root)
    game = only(games_root)
    (save,) = game.sources
    assert (save.relative_root, save.file_count, game.engine_version) == ("save", 5, "1.8.0")


def test_an_mv_folder_without_its_scripts_stays_unsupported(games_root: Path) -> None:
    built = build_mv(games_root)
    shutil.rmtree(built.root / "www" / "js")
    game = only(games_root)
    assert (game.engine, game.status) == ("rpgmaker_mv_mz", GameStatus.UNSUPPORTED)


def test_a_new_version_of_a_game_finds_its_record_by_title(
    games_root: Path, tmp_path: Path
) -> None:
    build_mv(games_root, "Hero 1.0", title="Hero")
    repo = back_up(games_root, tmp_path / "repo")
    game_id = repo.load_catalog()["games"][0]["game_id"]
    newer = tmp_path / "new"
    build_mv(newer, "Hero 1.1", title="Hero")
    assert scan(newer, repo.root).games[0].known_game_id == game_id


# ----------------------------------------------------------------------- XP / VX / VX Ace


def test_rgss_saves_are_the_save_files_at_the_top_only(games_root: Path) -> None:
    built = build_rgss(games_root, "vxace", encrypted=False)
    game = only(games_root)
    (source,) = game.sources
    assert (source.relative_root, source.patterns) == (".", ["Save*.rvdata2"])
    assert source.file_count == 2, "never Data\\Save9.rvdata2"
    assert (built.root / "Data" / "Save9.rvdata2").is_file()


def test_rgss_title_in_shift_jis_a_renamed_exe_and_an_empty_title(games_root: Path) -> None:
    build_rgss(games_root, "xp", "Japanese", title="テスト".encode("cp932"))
    build_rgss(games_root, "vxace", "Renamed", exe="Foo", title="Foo Quest")
    build_rgss(games_root, "vx", "No Title", title=b" ")
    games = {Path(g.game_root).name: g for g in scan(games_root).games}
    assert games["Japanese"].display_name == "テスト"
    assert (games["Renamed"].engine, games["Renamed"].display_name) == (
        "rpgmaker_vxace",
        "Foo Quest",
    )
    assert (games["No Title"].display_name, games["No Title"].identity.engine_key) == (
        "No Title",
        None,
    )


def test_rpg_maker_2000_is_not_taken_for_xp(games_root: Path) -> None:
    build_rpg2000(games_root)
    assert not any(g.engine.startswith("rpgmaker") for g in scan(games_root).games)


def test_a_cleanup_of_an_rgss_game_folder_checks_only_its_saves(
    games_root: Path, tmp_path: Path
) -> None:
    built = build_rgss(games_root, "vxace", encrypted=False)
    repo = back_up(games_root, tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    (built.root / "Data" / "Save9.rvdata2").write_bytes(b"\x04\x08changed database")
    request = [(GAME_ROOT, str(built.root))]
    (target,) = plan_cleanup(repo, record, request, recycle_limit=10**12).targets
    assert target.ok, target.reasons
    (built.root / "Save03.rvdata2").write_bytes(b"\x04\x08a new slot")
    (target,) = plan_cleanup(repo, record, request, recycle_limit=10**12).targets
    assert any("not in the backup: Save03.rvdata2" in r for r in target.reasons), target.reasons


# ----------------------------------------------------------------------------- Wolf RPG


@pytest.mark.parametrize("layout", ["data_wolf", "packed", "unpacked"])
def test_wolf_is_known_by_its_data_and_saves_in_save(games_root: Path, layout: str) -> None:
    build_wolf(games_root, layout=layout)
    game = only(games_root)
    (source,) = game.sources
    assert (game.engine, source.relative_root, source.file_count) == ("wolf_rpg", "Save", 3)
    assert (game.display_name, game.identity.engine_key) == ("Fixture Wolf", None), "Game.exe"


def test_a_wolf_exe_with_its_own_name_names_the_game(games_root: Path) -> None:
    build_wolf(games_root, "Folder", exe="SoulQuest")
    game = only(games_root)
    assert (game.display_name, game.identity.engine_key) == ("SoulQuest", "SoulQuest")


def test_a_data_folder_and_game_exe_alone_are_not_wolf(games_root: Path) -> None:
    root = games_root / "Not Wolf"
    (root / "data" / "system").mkdir(parents=True)
    (root / "data" / "startup.tjs").write_text("// KiriKiri", encoding="utf-8")
    (root / "Game.exe").write_bytes(b"MZ")
    assert not any(g.engine == "wolf_rpg" for g in scan(games_root).games)


# ----------------------------------------------------------------------------- KiriKiri


def test_kirikiri_savedata_without_its_log(games_root: Path) -> None:
    build_kirikiri(games_root)
    game = only(games_root)
    (source,) = game.sources
    assert (source.relative_root, source.excludes, source.file_count) == (
        "savedata",
        ["krkr.console.log"],
        5,
    )
    assert game.display_name == "fixture"


def test_kirikiri_with_only_the_engines_settings_is_not_found_with_a_hint(
    games_root: Path,
) -> None:
    build_kirikiri(games_root, only_settings=True)
    game = only(games_root)
    assert game.status is GameStatus.NOT_FOUND
    assert game.sources[0].status is SourceStatus.EXPECTED_ABSENT
    assert any("probably saves somewhere else" in w for w in game.warnings)


@pytest.mark.parametrize(
    ("value", "token", "relative"),
    [
        ("$(exepath)\\save2", RootToken.GAME_ROOT, "save2"),
        ('"$(appdatapath)\\\\Maker\\\\Title"', RootToken.APPDATA, "Maker/Title"),
        ("$(vistapath)/Maker/Title", RootToken.APPDATA, "Maker/Title"),
        ("$(personalpath)\\Maker\\Title", RootToken.DOCUMENTS, "Maker/Title"),  # D69 3
        ("$(savedgamespath)\\Title", RootToken.SAVED_GAMES, "Title"),
    ],
    ids=["exepath", "appdata-quoted", "vistapath", "documents", "saved-games"],
)
def test_a_kirikiri_cf_datapath_is_followed(
    games_root: Path, value: str, token: RootToken, relative: str
) -> None:
    built = build_kirikiri(games_root, cf=value)
    base = (
        built.root
        if token is RootToken.GAME_ROOT
        else Path(os.environ[f"ENGINESAVE_ROOT_{token.value}"])  # the sandbox (conftest)
    )
    target = base.joinpath(*relative.split("/"))
    target.mkdir(parents=True)
    (target / "data0.kdt").write_bytes(b"moved save")
    game = only(games_root)
    source, default = game.sources
    assert (source.root_token, source.relative_root, source.status) == (
        token,
        relative,
        SourceStatus.CONFIRMED,
    )
    # D66 4: the engine's own savedata is looked at too
    assert (default.relative_root, default.status) == ("savedata", SourceStatus.EXPECTED_ABSENT)


def test_a_kirikiri_datapath_in_documents_not_played_yet(games_root: Path) -> None:
    """0.11.0 (D69 3): the folder 「文档」 names, before the first save: expected there."""
    build_kirikiri(games_root, cf="$(personalpath)\\Maker\\Title")
    game = only(games_root)
    source = game.sources[0]
    assert (source.root_token, source.relative_root) == (RootToken.DOCUMENTS, "Maker/Title")
    assert source.status is SourceStatus.EXPECTED_ABSENT
    assert str(source.path).startswith(os.environ["ENGINESAVE_ROOT_DOCUMENTS"])
    assert not any("by hand" in w for w in game.warnings)


# ------------------------------------------------------------------------------- output


def test_the_window_names_the_engines(games_root: Path) -> None:
    from enginesave.gui.state.common import Selection
    from enginesave.gui.state.scanning import game_row

    build_mv(games_root)
    game = only(games_root)
    assert game_row(game, Selection()).engine == "RPG Maker MV 1.6.2"


def test_scan_json_of_every_engine_conforms(
    games_root: Path, capsys: pytest.CaptureFixture
) -> None:
    from test_schemas import SCHEMAS, validate

    for _name, build, _engine in ROUND_TRIPS:
        build(games_root)
    assert main(["scan", str(games_root), "--json", "--no-cache"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["games"]) == len(ROUND_TRIPS)
    schema = json.loads((SCHEMAS / "scan.schema.json").read_text(encoding="utf-8"))
    assert validate(schema, data, schema) == []


# ---------------------------------------------------------------------- the engine review


def test_a_helper_or_engine_exe_never_names_the_game(games_root: Path) -> None:
    wolf = build_wolf(games_root, "Alpha Quest")
    (wolf.root / "unins000.exe").write_bytes(b"MZ")
    kiri = build_kirikiri(games_root, "Gamma Story", exe="krkr", cf="$(exepath)\\sav")
    (kiri.root / "sav").mkdir()
    (kiri.root / "sav" / "data0.kdt").write_bytes(b"save")
    games = {Path(g.game_root).name: g for g in scan(games_root).games}
    assert games["Alpha Quest"].identity.engine_key is None
    assert games["Alpha Quest"].display_name == "Alpha Quest"
    assert games["Gamma Story"].identity.engine_key is None


@pytest.mark.parametrize("value", [".", ".\\", "$(exepath)\\.", "$(exepath)"])
def test_a_kirikiri_datapath_of_the_game_folder_itself_is_not_followed(
    games_root: Path, value: str
) -> None:
    build_kirikiri(games_root, cf=value)
    game = only(games_root)
    assert [s.source_id for s in game.sources] == ["savedata_default"]
    assert any("not followed" in w for w in game.warnings)


def test_mv_of_nwjs_0_12_without_nw_dll_saves_in_the_game_folder(games_root: Path) -> None:
    built = build_mv(games_root, "Old MV", saves=False, root_saves=True)
    (built.root / "nw.dll").unlink()
    (built.root / "nw.pak").write_bytes(b"pak")
    game = only(games_root)
    by_id = {s.source_id: s for s in game.sources}
    assert (game.engine, game.status) == ("rpgmaker_mv", GameStatus.CONFIRMED)
    assert by_id["save_root"].status is SourceStatus.CONFIRMED


def test_kirikiri_saves_in_a_subfolder_are_not_mistaken_for_settings(games_root: Path) -> None:
    built = build_kirikiri(games_root, only_settings=True)
    (built.root / "savedata" / "slots").mkdir()
    (built.root / "savedata" / "slots" / "data0.kdt").write_bytes(b"save")
    game = only(games_root)
    assert game.status is GameStatus.CONFIRMED and game.sources[0].file_count == 2


def test_an_rgss_game_in_program_files_is_looked_for_in_virtualstore(
    games_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.11.0 (D69 3): a game under Program Files saves into VirtualStore; both are looked at
    (the token points into the sandbox, conftest); a game elsewhere has no such source."""
    program_files = games_root / "Program Files (x86)"
    monkeypatch.setenv("ProgramFiles(x86)", str(program_files))
    built = build_rgss(program_files / "Maker", "vxace", saves=False)
    relative = "/".join(Path(os.path.abspath(built.root)).parts[1:])
    store = Path(os.environ["ENGINESAVE_ROOT_VIRTUALSTORE"]).joinpath(*relative.split("/"))
    game = only(games_root)
    local, virtual = game.sources
    assert local.source_id == "saves" and local.status is SourceStatus.EXPECTED_ABSENT
    assert (virtual.root_token, virtual.relative_root) == (RootToken.VIRTUALSTORE, relative)
    assert virtual.status is SourceStatus.EXPECTED_ABSENT and Path(virtual.path) == store
    store.mkdir(parents=True)
    (store / "Save01.rvdata2").write_bytes(b"redirected save")
    (store / "Game.ini").write_bytes(b"not a save")
    game = only(games_root)
    virtual = game.sources[1]
    assert virtual.status is SourceStatus.CONFIRMED
    assert virtual.file_count == 1  # Save*.rvdata2 only
    monkeypatch.delenv("ProgramFiles(x86)")
    assert [s.source_id for s in only(games_root).sources] == ["saves"]


def test_a_virtualstore_save_follows_the_install_it_is_restored_into(
    games_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M48 review: restored into another install under Program Files, the VirtualStore save
    goes to that install's VirtualStore folder; into one outside Program Files it is left out
    (said) -- never into the old install's folder, which the new one does not read."""
    program_files = games_root / "Program Files (x86)"
    monkeypatch.setenv("ProgramFiles(x86)", str(program_files))
    built = build_rgss(program_files / "Old", "vxace", saves=False)
    store = Path(os.environ["ENGINESAVE_ROOT_VIRTUALSTORE"])

    def redirected(root: Path) -> Path:
        return store.joinpath(*Path(os.path.abspath(root)).parts[1:])

    (redirected(built.root)).mkdir(parents=True)
    (redirected(built.root) / "Save01.rvdata2").write_bytes(b"redirected")
    (built.root / "Save02.rvdata2").write_bytes(b"local")  # a save of its own too
    repo = back_up(games_root, tmp_path / "repo")
    game_id = repo.load_catalog()["games"][0]["game_id"]
    moved = program_files / "New" / built.root.name
    shutil.copytree(built.root, moved)
    plan = plan_restore(repo, repo.load_catalog(), game_id, game_root=str(moved))
    target = next(t for t in plan.targets if t.source["source_id"] == "virtualstore")
    assert target.target_dir == redirected(moved)
    assert any("not of the install it was backed up from" in n for n in plan.notes)
    outside = tmp_path / "elsewhere" / built.root.name
    shutil.copytree(built.root, outside)
    plan = plan_restore(repo, repo.load_catalog(), game_id, game_root=str(outside))
    assert [t.source["source_id"] for t in plan.targets] == ["saves"]
    assert any("is not restored" in n for n in plan.notes)
    with pytest.raises(UsageError, match="does not redirect"):
        plan_restore(
            repo, repo.load_catalog(), game_id, game_root=str(outside), sources=["virtualstore"]
        )


MKXP_JSON = b"""{
    // mkxp-z's own comments
    "windowTitle": "Fixture // not a comment",
    /* where the game writes */
    "dataPathOrg": "Fixture Team",
    "dataPathApp": "Fixture Essentials"
}"""


def test_an_mkxp_game_writes_where_mkxp_json_says(games_root: Path, tmp_path: Path) -> None:
    """0.11.0 (D69 4): ``dataPathOrg``/``dataPathApp`` → ``APPDATA\\<org>\\<app>`` (in the
    sandbox), comments and all; the backup takes it."""
    built = build_rgss(games_root, "xp", saves=False)
    (built.root / "mkxp.json").write_bytes(MKXP_JSON)
    data = Path(os.environ["ENGINESAVE_ROOT_APPDATA"]) / "Fixture Team" / "Fixture Essentials"
    built.write(data / "Game.rxdata", b"essentials save", save=True)
    game = only(games_root)
    source = game.sources[-1]
    assert (source.source_id, source.root_token, source.relative_root) == (
        "mkxp_data",
        RootToken.APPDATA,
        "Fixture Team/Fixture Essentials",
    )
    assert source.status is SourceStatus.CONFIRMED and source.file_count == 1
    repo = back_up(games_root, tmp_path / "repo")
    assert repo.load_catalog()["games"]


@pytest.mark.parametrize(
    ("config", "relative", "hint"),
    [
        (b'{"customDataPath": "Saves"}', "Saves", None),
        (b'{"dataPathApp": "Solo"}', "Solo", None),
        (b'{"customDataPath": "C:\\\\Saves"}', None, "not followed"),
        (b'{"customDataPath": "/Saves"}', None, "not followed"),
        (b'{"customDataPath": "\\\\Saves"}', None, "not followed"),
        (b'{"dataPathOrg": "Team", "dataPathApp": "Mon",}', "Team/Mon", None),
        (b'{"dataPathOrg": "Team"}', "Team/Fixture XP", None),
        (b"{not json", None, "could not be read"),
    ],
    ids=[
        "custom-relative",
        "app-only",
        "custom-absolute",
        "custom-drive-root",
        "custom-backslash-root",
        "trailing-comma",
        "org-only-title",
        "damaged",
    ],  # fmt: skip
)
def test_mkxp_json_variants(
    games_root: Path, config: bytes, relative: str | None, hint: str | None
) -> None:
    built = build_rgss(games_root, "xp", saves=False)
    (built.root / "mkxp.json").write_bytes(config)
    game = only(games_root)
    found = [s for s in game.sources if s.source_id == "mkxp_data"]
    assert [s.relative_root for s in found] == ([relative] if relative else [])
    assert (hint is None) or any(hint in w for w in game.warnings)


def test_an_rgss_ini_with_odd_lines_is_still_read(games_root: Path) -> None:
    built = build_rgss(games_root, "vxace", title="Odd % Title; yes")
    ini = built.root / "Game.ini"
    ini.write_bytes(b"just a line\r\n" + ini.read_bytes() + b"another line\r\n")
    game = only(games_root)
    assert (game.engine, game.display_name) == ("rpgmaker_vxace", "Odd % Title; yes")


def test_a_renamed_rgss_game_by_its_ini_or_by_its_archive(games_root: Path) -> None:
    build_rgss(games_root, "vxace", "By Ini", exe="Foo", encrypted=False)
    by_archive = build_rgss(games_root, "vx", "By Archive", exe="Bar")
    (by_archive.root / "Bar.ini").unlink()
    games = {Path(g.game_root).name: g for g in scan(games_root).games}
    assert games["By Ini"].engine == "rpgmaker_vxace"
    assert (games["By Archive"].engine, games["By Archive"].identity.engine_key) == (
        "rpgmaker_vx",
        None,
    )


def test_mv_without_any_title_is_its_folder_and_no_identity(games_root: Path) -> None:
    build_mv(games_root, "Nameless MV", title=None, window_title="")
    game = only(games_root)
    assert (game.display_name, game.identity.engine_key) == ("Nameless MV", None)


def test_a_large_system_json_still_gives_its_title(games_root: Path) -> None:
    built = build_mv(games_root, "Big MV", title="Big Title")
    system = built.root / "www" / "data" / "System.json"
    data = json.loads(system.read_bytes())
    data["variables"] = ["v" * 50] * 4000  # well over 64 KiB, after the title
    system.write_text(json.dumps(data), encoding="utf-8")
    assert system.stat().st_size > 64 * 1024
    assert only(games_root).display_name == "Big Title"


def test_a_kirikiri_appdata_datapath_leaves_out_the_log(games_root: Path) -> None:
    build_kirikiri(games_root, cf="$(appdatapath)\\Maker\\Title")
    target = Path(os.environ["ENGINESAVE_ROOT_APPDATA"]) / "Maker" / "Title"
    target.mkdir(parents=True)
    (target / "data0.kdt").write_bytes(b"save")
    (target / "krkr.console.log").write_bytes(b"a log")
    source = only(games_root).sources[0]
    assert source.file_count == 1


def test_kirikiri_saves_both_where_the_cf_says_and_in_savedata(games_root: Path) -> None:
    """D66 4: both places are backed up, with a warning, as MV's two save folders."""
    built = build_kirikiri(games_root, cf="$(exepath)\\sav")
    for folder in ("sav", "savedata"):
        (built.root / folder).mkdir(exist_ok=True)
        (built.root / folder / "data0.kdt").write_bytes(folder.encode())
    game = only(games_root)
    assert [s.status for s in game.sources] == [SourceStatus.CONFIRMED] * 2
    assert any("both are backed up" in w for w in game.warnings)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("$(exepath)savedata", [("savedata", RootToken.GAME_ROOT, "savedata")]),
        ("savedata\\saves", [("savedata", RootToken.GAME_ROOT, "savedata/saves")]),
        ("$(exepath)savedata\\slot", [("savedata", RootToken.GAME_ROOT, "savedata/slot")]),
        (
            "$(appdatapath)\\savedata",
            [
                ("savedata", RootToken.APPDATA, "savedata"),
                ("savedata_default", RootToken.GAME_ROOT, "savedata"),
            ],
        ),
    ],
    ids=["savedata", "below-savedata", "exepath-below-savedata", "appdata-named-savedata"],
)
def test_kirikiri_second_source_only_where_it_adds_a_folder(
    games_root: Path, value: str, expected: list
) -> None:
    """M44 review: the game folder's savedata is a second source unless the .cf names it or a
    folder in it (the same files twice); the root token counts, not only the name."""
    built = build_kirikiri(games_root, cf=value)
    (built.root / "savedata" / "saves").mkdir(parents=True)
    (built.root / "savedata" / "slot").mkdir()
    (built.root / "savedata" / "saves" / "data0.kdt").write_bytes(b"save")
    (built.root / "savedata" / "slot" / "data0.kdt").write_bytes(b"save")
    game = only(games_root)
    assert [(s.source_id, s.root_token, s.relative_root) for s in game.sources] == expected
    assert not any("both are backed up" in w for w in game.warnings)


def test_kirikiri_settings_where_the_cf_says_and_saves_in_savedata(games_root: Path) -> None:
    """M44 review: the .cf's folder holds only the engine's settings, savedata the saves: one
    place is backed up, and the warning says the saves were found."""
    built = build_kirikiri(games_root, cf="$(exepath)\\sav")
    (built.root / "sav").mkdir()
    (built.root / "sav" / "fixture.cfu").write_bytes(b"settings")
    (built.root / "savedata").mkdir()
    (built.root / "savedata" / "data0.kdt").write_bytes(b"save")
    game = only(games_root)
    assert [s.status for s in game.sources] == [
        SourceStatus.EXPECTED_ABSENT,
        SourceStatus.CONFIRMED,
    ]
    assert game.status is GameStatus.CONFIRMED
    assert any("the saves were found in savedata" in w for w in game.warnings)
    assert not any("both are backed up" in w or "cannot find" in w for w in game.warnings)


def test_a_gbk_title_is_read_in_the_system_code_page(
    games_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D66 1: a Chinese Game.ini (GBK, as a Chinese Windows writes ANSI text) gives its title;
    Shift-JIS with kana still reads as Japanese; bytes no code page reads are no identity."""
    from enginesave.engines import common

    monkeypatch.setattr(common, "system_codepage", lambda: "cp936")
    build_rgss(games_root, "xp", "Chinese", title="仙剑奇侠传".encode("gbk"))
    build_rgss(games_root, "vx", "Japanese", title="テスト".encode("cp932"))
    build_rgss(games_root, "vxace", "Broken", title=b"\xff\xfe\xff")
    games = {Path(g.game_root).name: g for g in scan(games_root).games}
    assert (games["Chinese"].display_name, games["Chinese"].identity.engine_key) == (
        "仙剑奇侠传",
        "仙剑奇侠传",
    )
    assert games["Japanese"].display_name == "テスト"
    assert games["Broken"].identity.engine_key is None, games["Broken"].display_name


def test_bytes_no_code_page_reads_and_gbk_read_as_half_width_katakana(
    games_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M44 review: a title no code page reads (a replacement character) is no identity; GBK
    bytes whose Shift-JIS reading has kana but also half-width katakana are read as GBK."""
    from enginesave.engines import common

    monkeypatch.setattr(common, "system_codepage", lambda: "cp936")
    assert common.decode_text(b"\x83\x41\xb0\xa1") == b"\x83\x41\xb0\xa1".decode("gbk")
    build_rgss(games_root, "xp", "Undecodable", title=b"\x81 ")
    (game,) = scan(games_root).games
    assert "\ufffd" in game.display_name and game.identity.engine_key is None


@pytest.mark.parametrize(
    ("title", "codec", "rekeyed"),
    [("仙剑奇侠传", "gbk", True), ("幻想三国志外传", "gbk", True), ("東方紅魔郷", "cp932", False)],
    ids=["gbk-as-shift-jis", "gbk-replaced", "kanji-only-japanese"],
)
def test_a_record_of_0_10_0_keeps_its_game_when_the_title_is_read_otherwise(
    games_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    title: str,
    codec: str,
    rekeyed: bool,
) -> None:
    """M44 review: 0.10.0 read a GBK title as Shift-JIS (or with replacement characters) and
    keyed its record by that. Scanned now, the game still finds that record (its
    ``legacy_key``), the next backup adds to it instead of making a second one, and a garbled
    key, title and name take the reading of now; a title 0.10.0 read right is kept."""
    from enginesave.engines import common

    monkeypatch.setattr(common, "system_codepage", lambda: "cp936")
    build_rgss(games_root, "xp", "Old Game", title=title.encode(codec))
    with common.legacy_decoding():  # as 0.10.0 read it
        repo = back_up(games_root, tmp_path / "repo")
    (record,) = repo.load_catalog()["games"]
    stored = repo.load_game(record["game_id"])["identity"]["engine_key"]
    (game,) = scan(games_root, repo.root).games
    assert stored and game.identity.legacy_key == stored != game.identity.engine_key
    assert game.known_game_id == record["game_id"]
    back_up(games_root, repo.root)
    catalog = Repo.open(repo.root).load_catalog()
    assert [r["game_id"] for r in catalog["games"]] == [record["game_id"]]
    assert catalog["games"][0]["snapshot_count"] == 2
    doc = Repo.open(repo.root).load_game(record["game_id"])
    assert "legacy_key" not in doc["identity"]
    folder = Repo.open(repo.root).folder_name(record["game_id"])
    if rekeyed:
        assert doc["identity"]["engine_key"] == game.identity.engine_key == title
        assert doc["display_name"] == title == catalog["games"][0]["display_name"]
        # 1.0.0 (D70 9): the folder named after the garbled title follows the right one
        assert folder == title and (repo.root / "games" / title / "game.json").is_file()
    else:
        assert doc["identity"]["engine_key"] == stored == title == doc["display_name"]
        assert folder == title  # read right in 0.10.0 too: named so from the start
    (again,) = scan(games_root, repo.root).games
    assert again.known_game_id == record["game_id"]


def test_link_suggestions_look_past_version_tails(games_root: Path, tmp_path: Path) -> None:
    """D66 5: "Hero v0.3" and "Hero v0.4" are suggested for each other; the key differs."""
    build_rgss(games_root, "vxace", "Hero v0.3", title="Hero v0.3")
    repo = back_up(games_root, tmp_path / "repo")
    newer = tmp_path / "newer"
    build_rgss(newer, "vxace", "Hero v0.4", title="Hero v0.4")
    (game,) = scan(newer, repo.root).games
    assert game.known_game_id is None
    assert [s["reason"] for s in game.link_suggestions] == ["config_name_match"]


def test_link_suggestions_by_a_folder_name_with_a_version(games_root: Path, tmp_path: Path) -> None:
    """D66 5 (M44 review): without a title, the folder's name past its version is suggested;
    the engine's own exe name (``Game``) is not."""
    build_rgss(games_root, "vxace", "Quest-1.0", title=b"")
    repo = back_up(games_root, tmp_path / "repo")
    newer = tmp_path / "newer"
    build_rgss(newer, "vxace", "Quest-1.1", title=b"")
    (game,) = scan(newer, repo.root).games
    assert game.known_game_id is None
    assert [s["reason"] for s in game.link_suggestions] == ["dir_name_match"]


@pytest.mark.parametrize(
    "build",
    [
        lambda p: build_kirikiri(p, saves=False),
        lambda p: build_rgss(p, "xp", saves=False),
        lambda p: build_wolf(p, saves=False),
    ],
    ids=["kirikiri", "rgss", "wolf"],
)
def test_without_saves_the_game_is_not_found(games_root: Path, build: Callable) -> None:
    build(games_root)
    game = only(games_root)
    assert game.status is GameStatus.NOT_FOUND
    assert all(s.status is SourceStatus.EXPECTED_ABSENT for s in game.sources)


# ------------------------------------------------------------------- 1.0.0 (D70 7) details


def test_rgss_saves_in_the_game_folder_and_in_virtualstore_are_both_said(
    games_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program_files = games_root / "Program Files"
    monkeypatch.setenv("ProgramFiles", str(program_files))
    built = build_rgss(program_files / "Maker", "vx")  # saves in the game folder
    relative = Path(os.path.abspath(built.root)).parts[1:]
    store = Path(os.environ["ENGINESAVE_ROOT_VIRTUALSTORE"]).joinpath(*relative)
    store.mkdir(parents=True)
    (store / "Save1.rvdata").write_bytes(b"redirected")
    game = only(games_root)
    assert all(s.file_count for s in game.sources)
    assert any("both the game folder and VirtualStore" in w for w in game.warnings)


@pytest.mark.parametrize("text", [b"", b"  \r\n", b"// only a comment\n", b"/* c */ {}", b"{}"])
def test_an_mkxp_json_that_says_nothing_is_not_damage(games_root: Path, text: bytes) -> None:
    built = build_rgss(games_root, "xp", saves=False)
    (built.root / "mkxp.json").write_bytes(text)
    game = only(games_root)
    assert [s.source_id for s in game.sources] == ["saves"]
    assert not any("mkxp.json" in w for w in game.warnings)


def test_the_known_folders_fall_back_to_the_profiles_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D70 7: when SHGetKnownFolderPath says nothing, %USERPROFILE%\\Documents and Saved Games
    are used -- only when they are there."""
    from enginesave import roots

    monkeypatch.setattr(roots, "known_folder", lambda folder_id: None)
    profile = tmp_path / "profile"
    (profile / "Documents").mkdir(parents=True)
    env = {"USERPROFILE": str(profile)}
    assert roots.profile_root(roots.DOCUMENTS, env) == (
        profile / "Documents",
        "default:USERPROFILE",
    )
    assert roots.profile_root(roots.SAVED_GAMES, env)[0] is None  # not there: not used
