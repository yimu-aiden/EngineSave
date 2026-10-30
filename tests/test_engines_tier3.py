"""0.10.0 third tier (D65, M42): Electron (``package.json`` loose or inside ``app.asar``, the
data folder in APPDATA without Chromium's caches), Unreal (``Saved\\SaveGames`` below
LOCALAPPDATA or the game folder) and Godot (a candidate folder in ``app_userdata``). The
profile folders are sandbox folders (conftest)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from enginesave.backup import execute_backup, prepare_backup
from enginesave.engines.electron import asar_package
from enginesave.models import GameStatus, RootToken, SourceStatus
from enginesave.repo import Repo
from enginesave.restore import execute_restore, plan_restore
from enginesave.scan import ScanOptions, run_scan
from fixtures.engines import asar_bytes, build_electron, build_godot, build_unreal


def only(root: Path):  # noqa: ANN201
    games = run_scan(ScanOptions(roots=[root])).games
    assert len(games) == 1, [(g.game_root, g.engine) for g in games]
    return games[0]


def round_trip(root: Path, dest: Path, saves: dict[Path, bytes], **options: object) -> int:
    """Back up, delete the saves, restore: how many files the restore wrote."""
    plan = prepare_backup([root], dest, **options)
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    game_id = repo.load_catalog()["games"][0]["game_id"]
    manifest = repo.load_manifest(game_id, repo.latest_snapshot(game_id))
    assert len(manifest["files"]) == len(saves), "the saves and nothing else"
    for path in saves:
        path.unlink()
    result = execute_restore(plan_restore(repo, repo.load_catalog(), game_id))
    assert result.failures == []
    for path, data in saves.items():
        assert path.read_bytes() == data, path
    return result.written


# ----------------------------------------------------------------------------- Electron


@pytest.mark.parametrize("packed", [True, False], ids=["asar", "loose"])
def test_electron_data_folder_goes_back_without_its_caches(
    games_root: Path, tmp_path: Path, packed: bool
) -> None:
    built = build_electron(games_root, product="  Star Quest ", packed=packed)
    game = only(games_root)
    assert (game.engine, game.status, game.display_name) == (
        "electron",
        GameStatus.CONFIRMED,
        "Star Quest",
    )
    (source,) = game.sources
    assert (source.root_token, source.relative_root, source.file_count) == (
        RootToken.APPDATA,
        "Star Quest",
        3,
    )
    assert "Cache" in source.excludes and "GPUCache" in source.excludes
    assert round_trip(games_root, tmp_path / "repo", built.saves) == len(built.saves)


SYSTEM_JSON = b'{"airship": {}, "gameTitle": "Maker Quest", "switches": ["", "a"]}'


@pytest.mark.parametrize("packed", [True, False], ids=["asar", "loose"])
def test_rpg_maker_mv_in_electron_takes_its_title_and_its_save_folder(
    games_root: Path, tmp_path: Path, packed: bool
) -> None:
    """0.11.0 (D69 2): the MV core and System.json in the app, loose or packed; the save
    folder there is (in the loose app, or beside the archive) is added to the APPDATA data;
    the title is gameTitle; the engine and the identity key stay Electron's, so the record
    0.10.0 made is the same one."""
    built = build_electron(
        games_root,
        product="maker-quest",
        packed=packed,
        game=False,  # the package says nothing of a game: the MV core is enough
        extra={
            "www/index.html": b"<html>",
            "www/js/rpg_core.js": b"// RPGMAKER_VERSION = '1.6.2'",
            "www/data/System.json": SYSTEM_JSON,
        },
    )
    save = built.root / "resources" / ("save" if packed else "app/www/save")
    built.write(save / "file1.rpgsave", b"slot one", save=True)
    built.write(save / "global.rpgsave", b"global", save=True)
    game = only(games_root)
    assert (game.engine, game.display_name, game.identity.engine_key) == (
        "electron",
        "Maker Quest",
        "maker-quest",
    )
    assert game.status is GameStatus.CONFIRMED
    userdata, files = game.sources
    assert userdata.status is SourceStatus.CONFIRMED and userdata.relative_root == "maker-quest"
    assert (files.root_token, files.relative_root, files.file_count) == (
        RootToken.GAME_ROOT,
        "resources/save" if packed else "resources/app/www/save",
        2,
    )
    assert any(e.detail == "RPG Maker MV core" for e in game.evidence)
    assert not any("only a candidate" in w for w in game.warnings)
    assert round_trip(games_root, tmp_path / "repo", built.saves) == len(built.saves)


def test_rpg_maker_mz_in_electron_without_a_save_folder_keeps_the_appdata_one(
    games_root: Path,
) -> None:
    """MZ (``js/rmmz_core.js`` at the app's top) whose saves are in Local Storage: only the
    APPDATA source; a plain Electron app next to it is not taken for RPG Maker."""
    build_electron(
        games_root,
        "Maker Z",
        product="Maker Z",
        game=False,
        extra={"js/rmmz_core.js": b"// MZ", "data/System.json": b"{}"},
    )
    game = only(games_root)
    (source,) = game.sources
    assert source.relative_root == "Maker Z" and source.status is SourceStatus.CONFIRMED
    assert game.display_name == "Maker Z"  # no gameTitle: the package's name
    assert any(e.detail == "RPG Maker MZ core" for e in game.evidence)


def test_rpg_maker_in_an_electron_app_with_a_name_many_apps_keep(games_root: Path) -> None:
    """M48 review: the shared-looking APPDATA folder stays a candidate (and the name no key);
    the game's own save folder is confirmed; no warning says the opposite of what is done."""
    built = build_electron(
        games_root,
        product="electron-quick-start",
        packed=False,
        extra={"www/js/rpg_core.js": b"//", "www/data/System.json": SYSTEM_JSON},
    )
    built.write(built.root / "resources/app/www/save/file1.rpgsave", b"s", save=True)
    game = only(games_root)
    userdata, files = game.sources
    assert userdata.status is SourceStatus.CANDIDATE and files.status is SourceStatus.CONFIRMED
    assert game.identity.engine_key is None
    assert [w for w in game.warnings if "only a candidate" in w] == [
        "'electron-quick-start' is a name many apps keep: "
        "%APPDATA%\\electron-quick-start is only a candidate"
    ]


def test_a_plain_electron_app_is_not_rpg_maker(games_root: Path) -> None:
    built = build_electron(games_root, product="Plain Game", packed=False)
    (built.root / "save").mkdir()  # a folder named save beside it is not taken
    game = only(games_root)
    assert [s.source_id for s in game.sources] == ["userdata"]
    assert not any("RPG Maker" in e.detail for e in game.evidence)


def test_electron_without_product_name_uses_the_name_and_a_generic_one_is_a_candidate(
    games_root: Path,
) -> None:
    build_electron(games_root, "Named", product=None, name="named-app")
    assert only(games_root).sources[0].relative_root == "named-app"
    build_electron(games_root, "Generic", product="Electron")
    games = {Path(g.game_root).name: g for g in run_scan(ScanOptions(roots=[games_root])).games}
    generic = games["Generic"]
    assert generic.sources[0].status is SourceStatus.CANDIDATE
    assert generic.status is GameStatus.CANDIDATE and generic.identity.engine_key is None


def test_a_damaged_asar_is_read_as_nothing(tmp_path: Path) -> None:
    good = asar_bytes({"package.json": b'{"name": "x"}'})
    cases = {
        "short": good[:10],
        "not four": b"\x05" + good[1:],
        "header past the end": good[:4] + (10**9).to_bytes(4, "little") + good[8:],
        "no package": asar_bytes({"main.js": b"//"}),
        "not json": asar_bytes({"package.json": b"{not json"}),
    }
    for name, data in cases.items():
        path = tmp_path / f"{name}.asar"
        path.write_bytes(data)
        assert asar_package(path) is None, name
    (tmp_path / "good.asar").write_bytes(good)
    assert asar_package(tmp_path / "good.asar") == {"name": "x"}


def test_an_electron_app_without_a_name_is_unsupported(games_root: Path) -> None:
    built = build_electron(games_root, product=None, name="", saves=False)
    assert built.root.is_dir()
    game = only(games_root)
    assert (game.engine, game.status) == ("electron", GameStatus.UNSUPPORTED)


# ------------------------------------------------------------------------------- Unreal


@pytest.mark.parametrize("where", ["installed", "local"])
def test_unreal_save_games_go_back(games_root: Path, tmp_path: Path, where: str) -> None:
    built = build_unreal(games_root, saves=where)
    game = only(games_root)
    assert (game.engine, game.status, game.identity.project) == (
        "unreal",
        GameStatus.CONFIRMED,
        "FixtureGame",
    )
    by_id = {s.source_id: s for s in game.sources}
    confirmed = "savegames" if where == "installed" else "savegames_local"
    assert by_id[confirmed].status is SourceStatus.CONFIRMED and by_id[confirmed].file_count == 2
    assert by_id[confirmed].relative_root == "FixtureGame/Saved/SaveGames"
    assert round_trip(games_root, tmp_path / "repo", built.saves) == 2


def test_unreal_without_saves_is_not_found(games_root: Path) -> None:
    build_unreal(games_root, saves=None)
    game = only(games_root)
    assert game.status is GameStatus.NOT_FOUND


# -------------------------------------------------------------------------------- Godot


def test_godot_userdata_is_a_candidate_backed_up_when_asked(
    games_root: Path, tmp_path: Path
) -> None:
    built = build_godot(games_root)
    game = only(games_root)
    (source,) = game.sources
    assert (game.engine, game.status, source.status) == (
        "godot",
        GameStatus.CANDIDATE,
        SourceStatus.CANDIDATE,
    )
    assert (source.relative_root, source.file_count) == ("Godot/app_userdata/fixture_godot", 2)
    assert game.identity.engine_key is None
    assert round_trip(games_root, tmp_path / "repo", built.saves, include_candidates=True) == 2


def test_godot_without_a_userdata_folder_is_a_hint(games_root: Path) -> None:
    build_godot(games_root, saves=False)
    game = only(games_root)
    assert game.sources == [] and game.status is GameStatus.NOT_FOUND
    assert any("app_userdata" in w for w in game.warnings)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_godot_reads_its_project_name_from_the_pck(
    games_root: Path, tmp_path: Path, version: int
) -> None:
    """D69 1: the pack's project.binary names the project: its app_userdata folder is
    CONFIRMED, the name is the identity, a backup and a restore give the saves back."""
    built = build_godot(games_root, project="Star Fixture", pck_version=version)
    game = only(games_root)
    (source,) = game.sources
    assert (game.status, source.status, game.display_name) == (
        GameStatus.CONFIRMED,
        SourceStatus.CONFIRMED,
        "Star Fixture",
    )
    assert (source.root_token, source.relative_root) == (
        RootToken.APPDATA,
        "Godot/app_userdata/Star Fixture",
    )
    assert source.file_count == 2 and game.identity.engine_key == "Star Fixture"
    assert round_trip(games_root, tmp_path / "repo", built.saves) == 2


def test_godot_custom_user_folder_and_a_project_not_played_yet(games_root: Path) -> None:
    """D69 1: use_custom_user_dir gives %APPDATA%\\<folder>; no saves yet is EXPECTED_ABSENT."""
    build_godot(games_root, "Custom", project="Custom Game", custom_dir="Studio/Custom Game")
    build_godot(games_root, "Fresh", exe="fresh", project="Fresh Game", saves=False)
    games = {Path(g.game_root).name: g for g in run_scan(ScanOptions(roots=[games_root])).games}
    assert games["Custom"].sources[0].relative_root == "Studio/Custom Game"
    assert games["Custom"].status is GameStatus.CONFIRMED
    assert games["Fresh"].sources[0].status is SourceStatus.EXPECTED_ABSENT
    assert games["Fresh"].status is GameStatus.NOT_FOUND


def test_a_damaged_pck_falls_back_to_the_candidate(games_root: Path) -> None:
    """D69 1: a pack whose table cannot be read is treated as in 0.10.0 (a candidate by the
    exe's name)."""
    built = build_godot(games_root, project="Broken")
    pck = built.root / "fixture_godot.pck"
    pck.write_bytes(pck.read_bytes()[:40])
    userdata = Path(os.environ["ENGINESAVE_ROOT_APPDATA"]) / "Godot" / "app_userdata"
    (userdata / "fixture_godot").mkdir(parents=True)
    (userdata / "fixture_godot" / "save.dat").write_bytes(b"x")
    game = only(games_root)
    assert game.sources[0].status is SourceStatus.CANDIDATE and game.identity.engine_key is None


@pytest.mark.parametrize(
    ("settings", "folder"),
    [
        ({"name": "AC/DC Tour"}, "Godot/app_userdata/AC-DC Tour"),
        ({"name": " Padded "}, "Godot/app_userdata/Padded"),
        ({"name": "Game: Sub..."}, "Godot/app_userdata/Game- Sub"),
        ({"name": "Star", "use_custom_user_dir": True}, "Star"),  # no custom name: the project's
        ({"name": "Star", "use_custom_user_dir": True, "custom_user_dir_name": "  "}, "Star"),
        (
            {"name": "Star", "use_custom_user_dir": True, "custom_user_dir_name": "..\\Studio//S?"},
            "-/Studio/S-",
        ),
        ({"name": "Star", "use_custom_user_dir": False, "custom_user_dir_name": "X"},
         "Godot/app_userdata/Star"),
    ],
)  # fmt: skip
def test_godot_folder_names_are_made_safe_as_godot_makes_them(
    settings: dict[str, object], folder: str
) -> None:
    """M48 review: Godot's ``OS::get_user_data_dir`` and ``get_safe_dir_name``."""
    from enginesave.engines.godot import user_folder

    found = user_folder({f"application/config/{k}": v for k, v in settings.items()})
    assert found is not None and found[1] == folder


def test_a_pck_without_its_mark_is_not_godot(games_root: Path) -> None:
    built = build_godot(games_root, saves=False)
    (built.root / "fixture_godot.pck").write_bytes(b"not a pack")
    game = only(games_root)
    assert game.status is GameStatus.UNSUPPORTED, "the fingerprint keeps it, unsupported"


# ---------------------------------------------------------------------- the engine review


def test_an_asar_index_of_the_wrong_shape_is_read_as_nothing(tmp_path: Path) -> None:
    import json as json_module
    import struct

    for index in ({"files": []}, {"files": "x"}, [1, 2]):
        text = json_module.dumps(index).encode()
        pad = (4 - len(text) % 4) % 4
        payload = 4 + len(text) + pad
        data = struct.pack("<IIII", 4, 4 + payload, payload, len(text)) + text + b"\x00" * pad
        path = tmp_path / "odd.asar"
        path.write_bytes(data)
        assert asar_package(path) is None


@pytest.mark.parametrize("name", ["nw", "electron-quick-start"])
def test_names_many_apps_keep_are_no_identity(games_root: Path, name: str) -> None:
    build_electron(games_root, product=name)
    game = only(games_root)
    assert game.identity.engine_key is None and game.sources[0].status is SourceStatus.CANDIDATE


@pytest.mark.parametrize(
    ("name", "relative"),
    [
        ("Studio Inc.", "Studio Inc"),
        ("Wait... What", "Wait... What"),
        ("@studio/mygame", "@studio/mygame"),
    ],
)
def test_an_electron_name_becomes_the_folder_windows_makes(
    games_root: Path, name: str, relative: str
) -> None:
    build_electron(games_root, product=name, saves=False)
    folder = Path(os.environ["ENGINESAVE_ROOT_APPDATA"]).joinpath(*relative.split("/"))
    (folder / "Local Storage").mkdir(parents=True)
    (folder / "Local Storage" / "x.log").write_bytes(b"save")
    (source,) = only(games_root).sources
    assert (source.relative_root, source.status) == (relative, SourceStatus.CONFIRMED)


def test_folders_found_by_a_shared_name_are_not_cleaned_up(
    games_root: Path, tmp_path: Path
) -> None:
    from enginesave.cleanup import GAME_ROOT, GLOBAL, plan_cleanup

    for build, options in (
        (lambda p: build_electron(p, product="Electron"), {"include_candidates": True}),
        (build_godot, {"include_candidates": True}),
    ):
        root = tmp_path / f"games-{len(list(tmp_path.iterdir()))}"
        built = build(root)
        plan = prepare_backup([root], tmp_path / f"repo-{root.name}", **options)
        repo = Repo.open_or_create(plan.dest)
        assert execute_backup(plan, repo).exit_code == 0
        record = repo.load_catalog()["games"][0]
        requests = [(GAME_ROOT, str(built.root)), (GLOBAL, None)]
        target = plan_cleanup(repo, record, requests, recycle_limit=10**12).targets[1]
        assert any("clean it up by hand" in r for r in target.reasons), target.reasons


def test_an_unreal_project_whose_exe_is_named_otherwise(games_root: Path) -> None:
    built = build_unreal(games_root, project="Proj")
    win64 = built.root / "Proj" / "Binaries" / "Win64"
    (win64 / "Proj-Win64-Shipping.exe").rename(win64 / "Target-Win64-Shipping.exe")
    assert only(games_root).identity.project == "Proj"


def test_an_unpacked_asar_package_is_read(games_root: Path) -> None:
    built = build_electron(games_root, product="Packed Name")
    unpacked = built.root / "resources" / "app.asar.unpacked"
    unpacked.mkdir()
    (unpacked / "package.json").write_text('{"productName": "Unpacked App"}', encoding="utf-8")
    assert only(games_root).display_name == "Unpacked App"


@pytest.mark.parametrize(
    ("options", "candidate"),
    [
        ({"game": False}, True),
        ({"game": False, "product": "Studio Launcher"}, True),
        ({"game": True, "product": "Game Updater"}, True),
        ({"game": False, "extra": {"www/index.html": b"<title>"}}, False),
        ({"game": False, "extra": {"tyrano/libs.js": b"//"}}, False),
        ({"game": True}, False),
        ({"game": False, "package": {"save": {"slots": 3}}}, False),
        ({"game": False, "package": {"description": "A puzzle game"}}, False),
        ({"game": True, "product": "Space Dispatcher"}, False),
        ({"game": True, "product": "Mindsetup"}, False),
        ({"game": True, "product": "GameSetup"}, True),
        ({"game": True, "product": "my-updater"}, True),
    ],
    ids=[
        "tool", "launcher", "updater", "www", "tyrano", "keywords", "save-key", "description",
        "dispatcher", "a-word-with-setup", "camel-case-setup", "dashed-updater",
    ],
)  # fmt: skip
def test_an_electron_app_that_shows_no_game_is_only_a_candidate(
    games_root: Path, options: dict, candidate: bool
) -> None:
    """D66 6: without a game engine's files or a game in its package.json, or named like a
    launcher (a word of the name, M44 review), its APPDATA folder is a candidate and its name
    no identity."""
    build_electron(games_root, **options)
    (source,) = only(games_root).sources
    game = only(games_root)
    assert (source.status is SourceStatus.CANDIDATE) is candidate
    assert (game.identity.engine_key is None) is candidate


# ----------------------------------------------------- 1.0.0 (D70 3, D72 R12): Godot's pack


def godot_pack(folder: Path, files: dict[str, bytes], version: int = 3) -> Path:
    """``moon.exe`` beside ``moon.pck`` holding ``files``, in a game folder of its own."""
    from fixtures.engines import pck_bytes

    folder.mkdir(parents=True)
    (folder / "moon.exe").write_bytes(b"MZ")
    pck = folder / "moon.pck"
    pck.write_bytes(pck_bytes(files, version))
    return pck


@pytest.mark.parametrize("version", [1, 2, 3])
def test_a_big_pack_and_a_long_path_do_not_hide_the_project(games_root: Path, version: int) -> None:
    """D70 3: 25000 entries, or a path longer than 1 KiB before ``project.binary``, made the
    whole pack unreadable (back to the exe's name, "maybe encrypted"); now the table is read
    entry by entry and the project's folder is found."""
    from enginesave.engines.godot import read_project
    from fixtures.engines import project_binary

    many = {f"res://assets/{n:05d}.png": b"" for n in range(25000)}
    long_name = {"res://" + "d/" * 700 + "deep.txt": b"x"}
    settings = project_binary({"application/config/name": "Moon Garden"})
    for name, files in (("many", many), ("long", long_name)):
        pck = godot_pack(games_root / name, {**files, "res://project.binary": settings}, version)
        found, why = read_project(pck)
        assert found == {"application/config/name": "Moon Garden"} and why == "", name
    game = next(
        g for g in run_scan(ScanOptions(roots=[games_root])).games if g.game_root.endswith("many")
    )
    assert game.identity.engine_key == "Moon Garden"
    assert game.sources[0].relative_root == "Godot/app_userdata/Moon Garden"


def test_r12_a_project_name_cut_short_is_damage_not_a_name(games_root: Path) -> None:
    """D72 R12: a string that says 999 bytes but has 9 was taken as the project's name (and
    the game CONFIRMED by it). Now it is a damaged project.binary: the 0.10.0 way (the exe's
    name, a candidate), and the hint says why."""
    import struct

    from enginesave.engines.godot import DAMAGED_PROJECT, read_project

    key = b"application/config/name"
    cut = struct.pack("<II", 4, 999) + b"Truncated"
    wrong_size = struct.pack("<II", 4, 4) + b"Moon"

    def ecfg(variant: bytes, declared: int | None = None) -> bytes:
        size = len(variant) if declared is None else declared
        return (
            b"ECFG"
            + struct.pack("<I", 1)
            + struct.pack("<I", len(key))
            + key
            + struct.pack("<I", size)
            + variant
        )

    cases = {
        "cut": ecfg(cut),
        "variant size": ecfg(wrong_size, declared=400),
        "key": b"ECFG" + struct.pack("<II", 1, 500) + key,
    }
    for name, blob in cases.items():
        pck = godot_pack(games_root / name, {"project.binary": blob})
        assert read_project(pck) == (None, DAMAGED_PROJECT), name
    short = godot_pack(games_root / "short", {"project.binary": ecfg(wrong_size)})
    data = short.read_bytes()
    short.write_bytes(data[: len(data) - 12])  # the member's bytes end before its size
    assert read_project(short)[0] is None
    games = run_scan(ScanOptions(roots=[games_root])).games
    for game in games:
        assert game.identity.engine_key is None and game.display_name == "moon"
    cut_game = next(g for g in games if g.game_root.endswith("cut"))
    assert any("project.binary in the .pck is damaged" in w for w in cut_game.warnings)


def test_the_godot_hint_says_why_the_folder_is_not_known(games_root: Path) -> None:
    """D70 3: the hint gives the real reason: no project.binary, an encrypted table."""
    from enginesave.engines.godot import ENCRYPTED, NO_PROJECT, read_project

    plain = godot_pack(games_root / "plain", {"res://icon.png": b"png"}, version=2)
    assert read_project(plain) == (None, NO_PROJECT)
    locked = godot_pack(games_root / "locked", {"res://icon.png": b"png"}, version=2)
    data = bytearray(locked.read_bytes())
    data[20:24] = (1).to_bytes(4, "little")  # the pack's flags: its directory is encrypted
    locked.write_bytes(bytes(data))
    assert read_project(locked) == (None, ENCRYPTED)
    games = {Path(g.game_root).name: g for g in run_scan(ScanOptions(roots=[games_root])).games}
    assert any("holds no project.binary" in w for w in games["plain"].warnings)
    assert any("file table is encrypted" in w for w in games["locked"].warnings)


def test_rpg_maker_in_electron_finds_the_record_of_its_nwjs_version(
    games_root: Path, tmp_path: Path
) -> None:
    """D70 7: the NW.js version backed up (record engine rpgmaker_mv, key the title); the
    Electron version of the same game is matched to that record by ``rpgmaker_mv:<title>``
    -- and an NW.js game is offered an Electron record of the same title."""
    from enginesave.identity import link_suggestions, match_known_game
    from fixtures.engines import build_mv

    nwjs = games_root / "nwjs"
    nwjs.mkdir()
    build_mv(nwjs, title="Maker Quest")
    plan = prepare_backup([nwjs], tmp_path / "repo")
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    record = repo.load_catalog()["games"][0]
    electron = games_root / "electron"
    electron.mkdir()
    build_electron(
        electron, "Maker Quest Electron", product="maker-quest", game=False,
        extra={"www/js/rpg_core.js": b"//", "www/data/System.json": SYSTEM_JSON},
    )  # fmt: skip
    game = only(electron)
    known = repo.known_games(repo.load_catalog())
    assert match_known_game(game, known) == record["game_id"]
    report = run_scan(ScanOptions(roots=[electron], repo=repo.root))
    assert report.games[0].known_game_id == record["game_id"]
    # the other way: a record of the Electron version, an NW.js game of the same title
    electron_record = {**game.to_dict(), "game_id": "e1e1e1e1-0000-4000-8000-000000000000"}
    electron_record["identity"] = game.identity.to_dict()
    nw_game = only(nwjs)
    found = link_suggestions(nw_game, [{**electron_record, "kind": "game", "engine": "electron"}])
    assert [s["reason"] for s in found] == ["config_name_match"]
