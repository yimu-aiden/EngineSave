"""1.2.0 (D73 6, 7, 12, 13): what a scan reports per install folder -- every game's
``installs`` with the game version and the programs that start it, the install a local save
folder is in, and games that are a web page."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from enginesave.engines.common import plain_version
from enginesave.installs import launchable
from enginesave.scan import ScanOptions, run_scan
from fixtures.builder import build_f1, build_f4, write_text
from fixtures.engines import (
    DUMMY_EXE,
    build_electron,
    build_godot,
    build_kirikiri,
    build_mv,
    build_mz,
    build_rgss,
    build_unity,
    build_unreal,
    build_wolf,
)


def scan(root: Path, saves_root: Path | None = None):
    return run_scan(ScanOptions(roots=[root], saves_root_override=saves_root))


def one(report, name: str):
    return next(g for g in report.games if Path(g.game_root).name == name)


def paths(install: dict) -> list[str]:
    return [c["path"] for c in install["launch_candidates"]]


def test_a_renpy_install_reports_its_version_launcher_and_local_saves(
    games_root: Path, saves_root: Path
) -> None:
    built = build_f1(games_root, saves_root)
    (built.root / "ReadableGame-32.exe").write_bytes(DUMMY_EXE)
    (built.root / "UnityCrashHandler64.exe").write_bytes(DUMMY_EXE)  # a helper: never listed
    (built.root / "vcredist_x64.exe").write_bytes(DUMMY_EXE)
    game = one(scan(games_root, saves_root), built.root.name)
    (install,) = game.installs
    assert install["game_root"] == str(built.root)
    assert (install["game_version"], install["game_version_source"]) == ("1.0", "config.version")
    assert paths(install) == ["ReadableGame.exe", "ReadableGame-32.exe"]
    assert install["launch_candidates"][0]["reason"].startswith("Ren'Py launcher")
    local, global_ = game.source("local"), game.source("global")
    assert local.install_root == str(built.root)
    assert global_.install_root is None


def test_grouped_renpy_versions_keep_their_own_install_facts(
    games_root: Path, saves_root: Path
) -> None:
    """D9 groups the two versions into one game, as before; each install has its own version
    and launcher, and each local save folder names its install."""
    v1, v2 = build_f4(games_root, saves_root)
    (game,) = scan(games_root, saves_root).games
    by_root = {i["game_root"]: i for i in game.installs}
    assert set(by_root) == {str(v1.root), str(v2.root)}
    assert by_root[str(v1.root)]["game_version"] == "1.0"
    assert by_root[str(v2.root)]["game_version"] == "2.0"
    assert all(paths(i) == ["GameA.exe"] for i in game.installs)
    locals_ = {s.source_id: s.install_root for s in game.sources if s.kind.value == "local"}
    assert sorted(locals_.values()) == sorted([str(v1.root), str(v2.root)])
    assert game.source("global").install_root is None


@pytest.mark.parametrize(
    ("build", "folder", "expected"),
    [
        (lambda p: build_mv(p), "Fixture MV", ["Game.exe"]),
        (lambda p: build_mz(p), "Fixture MZ", ["Game.exe"]),
        (lambda p: build_rgss(p, "vxace"), "Fixture VXACE", ["Game.exe"]),
        (lambda p: build_rgss(p, "xp", exe="Hero"), "Fixture XP", ["Hero.exe"]),
        (lambda p: build_unity(p), "Fixture Unity", ["Fixture.exe"]),
        (lambda p: build_wolf(p, exe="Game"), "Fixture Wolf", ["Game.exe"]),
        (lambda p: build_kirikiri(p), "Fixture KiriKiri", ["fixture.exe"]),
        (lambda p: build_godot(p), "Fixture Godot", ["fixture_godot.exe"]),
        (lambda p: build_electron(p), "Fixture Electron", ["Fixture Electron.exe"]),
    ],
)
def test_launch_candidates_per_engine(games_root: Path, build, folder: str, expected) -> None:
    built = build(games_root)
    for helper in ("UnityCrashHandler64", "unins000", "DXSETUP", "UE4PrereqSetup_x64"):
        (built.root / f"{helper}.exe").write_bytes(DUMMY_EXE)
    game = one(scan(games_root), folder)
    (install,) = game.installs
    assert paths(install)[: len(expected)] == expected
    assert not any(not launchable(Path(p).stem) for p in paths(install))
    assert all(c["reason"] for c in install["launch_candidates"])


def test_unreal_prefers_the_launcher_then_the_shipping_build(games_root: Path) -> None:
    build_unreal(games_root)
    (games_root / "Fixture Unreal" / "UE4PrereqSetup_x64.exe").write_bytes(DUMMY_EXE)
    (install,) = one(scan(games_root), "Fixture Unreal").installs
    assert paths(install) == [
        "FixtureGame.exe",
        "FixtureGame/Binaries/Win64/FixtureGame-Win64-Shipping.exe",
    ]
    assert install["launch_candidates"][1]["reason"] == "Unreal shipping build"


def test_a_renamed_rpg_maker_player_with_its_ini_comes_first(games_root: Path) -> None:
    built = build_rgss(games_root, "vx", exe="Hero")
    (built.root / "Game.exe").write_bytes(DUMMY_EXE)  # no Game.ini: after Hero.exe + Hero.ini
    (install,) = one(scan(games_root), "Fixture VX").installs
    assert paths(install) == ["Hero.exe", "Game.exe"]
    assert install["launch_candidates"][0]["reason"] == "RPG Maker player with Hero.ini"


def test_emuera_and_unsupported_engines_get_an_install_too(games_root: Path) -> None:
    era = games_root / "Fixture Era"
    (era / "ERB").mkdir(parents=True)
    (era / "CSV").mkdir()
    (era / "Emuera1824.exe").write_bytes(DUMMY_EXE)
    (era / "Tool.exe").write_bytes(DUMMY_EXE)
    game = one(scan(games_root), "Fixture Era")
    assert game.engine == "emuera" and game.status.value == "UNSUPPORTED"
    (install,) = game.installs
    assert paths(install) == ["Emuera1824.exe", "Tool.exe"]
    assert install["game_version"] is None and install["game_version_source"] is None


@pytest.mark.parametrize(
    ("version", "expected"),
    [("2.3.1", "2.3.1"), ("1.0.0", None), ("0.0.0", None), (None, None), (3, None)],
)
def test_electron_takes_the_package_version_unless_it_is_a_default(
    games_root: Path, version, expected
) -> None:
    package = {"version": version} if version is not None else {}
    build_electron(games_root, package=package)
    (install,) = one(scan(games_root), "Fixture Electron").installs
    assert install["game_version"] == expected
    source = install["game_version_source"]
    assert (source == "resources/app.asar!package.json version") if expected else source is None


def test_an_rpg_maker_mv_package_version_is_read(games_root: Path) -> None:
    built = build_mv(games_root)
    manifest = json.loads((built.root / "package.json").read_text())
    (built.root / "package.json").write_text(json.dumps({**manifest, "version": "0.4.0"}))
    (install,) = one(scan(games_root), "Fixture MV").installs
    assert (install["game_version"], install["game_version_source"]) == (
        "0.4.0",
        "package.json version",
    )


def test_versions_are_never_read_from_folder_names(games_root: Path) -> None:
    build_unity(games_root, "Fixture Unity v1.2.3")
    (install,) = one(scan(games_root), "Fixture Unity v1.2.3").installs
    assert install["game_version"] is None


def test_plain_version() -> None:
    assert plain_version(" 1.2b ") == "1.2b"
    assert plain_version("1.0.0") is None and plain_version("") is None
    assert plain_version("x" * 65) is None and plain_version("1\n2") is None


TWINE = (
    "<!DOCTYPE html><html><head><title>Story</title><!-- SugarCube (v2.36.1): a story "
    'format --></head><body><tw-storydata name="Story"></tw-storydata>'
    '<script>/* engine */</script><img src="img/a.png"></body></html>'
)


def test_a_twine_game_is_recognised_as_html_and_not_backed_up(games_root: Path) -> None:
    root = games_root / "Fixture Twine"
    write_text(root / "Degrees of Fixture 0.4.html", TWINE)
    write_text(root / "readme.html", "<html><body>read me</body></html>")
    write_text(root / "img" / "a.png", "png")
    game = one(scan(games_root), "Fixture Twine")
    assert game.engine == "html" and game.status.value == "UNSUPPORTED"
    assert game.sources == [] and "browser" in game.warnings[0]
    (install,) = game.installs
    assert paths(install) == ["Degrees of Fixture 0.4.html"]
    assert install["launch_candidates"][0]["reason"].startswith("Twine story page")


def test_a_web_game_page_that_loads_its_folder_is_an_html_game(games_root: Path) -> None:
    root = games_root / "Fixture Web"
    page = '<html><canvas id="game"></canvas><script src="js/main.js"></script></html>'
    write_text(root / "index.html", page)
    write_text(root / "js" / "main.js", "//")
    game = one(scan(games_root), "Fixture Web")
    assert game.engine == "html" and paths(game.installs[0]) == ["index.html"]
    assert game.installs[0]["launch_candidates"][0]["reason"].startswith("web game page")


@pytest.mark.parametrize(
    "files",
    [
        {"manual.html": "<html><body>how to play</body></html>"},  # no script, no story
        # a game's manual, a web app's page: scripts and an image folder, no game engine
        {"startup.html": '<html><script src="img/menu.js"></script><img src="img/a.png"></html>'},
        {"index.html": TWINE, "Game.exe": "MZ"},  # a program: not a web game
    ],
)
def test_other_folders_with_pages_are_not_html_games(games_root: Path, files) -> None:
    root = games_root / "Docs"
    for name, text in files.items():
        write_text(root / name, text)
    write_text(root / "img" / "a.png", "png")
    assert all(g.engine != "html" for g in scan(games_root).games)


def test_the_scan_json_with_installs_conforms(games_root: Path, saves_root: Path) -> None:
    from test_schemas import conforms

    build_f4(games_root, saves_root)
    build_unreal(games_root)
    write_text(games_root / "Fixture Twine" / "story.html", TWINE)
    conforms("scan", json.loads(json.dumps(scan(games_root, saves_root).to_dict())))
