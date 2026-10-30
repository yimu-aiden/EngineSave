"""0.10.0 engine framework (D65 1-5): the root token registry and ``--root-override``, sources
that take some files of a folder (``patterns``, ``excludes``), identity keys of other engines,
cleanup of their profile folders and the status rules -- with a test-only engine, so that the
framework is checked apart from the real engines (M40-M42)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from enginesave import layout, roots
from enginesave.backup import execute_backup, prepare_backup
from enginesave.cleanup import GLOBAL, plan_cleanup, save_folder
from enginesave.cleanup.model import GAME_ROOT
from enginesave.cli import main
from enginesave.engines import common, default_detectors
from enginesave.engines.base import DetectionResult, Detector, ScanContext
from enginesave.errors import FatalError, UsageError
from enginesave.identity import match_key, match_record
from enginesave.models import Evidence, GameStatus, RootToken, SourceKind, SourceStatus
from enginesave.repo import Repo
from enginesave.restore import execute_restore, plan_restore, validate_manifest
from enginesave.scan import ScanOptions, run_scan

MARKER = "pattern.engine"


class PatternDetector(Detector):
    """A test engine: ``pattern.engine`` names the game (first line) and the profile folder
    below LOCALAPPDATA (second line, the title when empty); saves are ``Save*.dat`` beside the
    game and ``<profile>/Saves`` without ``log.txt``."""

    engine = "testengine"

    def detect(self, directory: Path, ctx: ScanContext) -> DetectionResult | None:
        marker = directory / MARKER
        if not marker.is_file():
            return None
        lines = [*marker.read_text(encoding="utf-8").splitlines(), "", ""]
        title = lines[0].strip() or directory.name
        profile = lines[1].strip() or title
        evidence = [Evidence(MARKER, "test_marker", "test engine")]
        warnings: list[str] = []
        sources = [
            common.game_source(
                directory, "root", roots.ROOT_ITSELF, evidence, patterns=["Save*.dat"]
            )
        ]
        found = common.profile_source(
            "profile",
            roots.LOCALAPPDATA,
            f"{profile}/Saves",
            ctx,
            evidence,
            warnings,
            excludes=["log.txt"],
        )
        sources += [found] if found is not None else []
        game = common.rule_game(
            directory,
            self.engine,
            title=title,
            engine_key=title,
            sources=sources,
            evidence=evidence,
            warnings=warnings,
        )
        return DetectionResult(game=game)


def detectors() -> list[Detector]:
    return [PatternDetector(), *default_detectors()]


@pytest.fixture(autouse=True)
def test_engine_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test engine is one of the defaults here, so that every scan sees it -- the
    cleanup's own scan of a game folder too."""
    import enginesave.scan

    monkeypatch.setattr(enginesave.scan, "default_detectors", detectors)


def local_appdata() -> Path:
    return Path(os.environ[roots.env_name(roots.LOCALAPPDATA)])


def build_pattern_game(
    parent: Path, folder: str, title: str = "", profile: str = ""
) -> tuple[Path, Path]:
    """The game folder (two saves at its top, a ``Save`` file one level down that is not one,
    a settings file) and its profile folder (two saves, one in a subfolder, and a log)."""
    root = parent / folder
    (root / "Data").mkdir(parents=True)
    (root / MARKER).write_text(f"{title}\n{profile}\n", encoding="utf-8")
    (root / "Game.exe").write_bytes(b"MZ not a program")
    (root / "Save01.dat").write_bytes(b"slot one")
    (root / "save02.DAT").write_bytes(b"slot two")
    (root / "Data" / "Save03.dat").write_bytes(b"game data, not a save")
    (root / "config.ini").write_text("volume=3\n", encoding="utf-8")
    saves = local_appdata() / (profile or title or folder) / "Saves"
    (saves / "sub").mkdir(parents=True, exist_ok=True)
    (saves / "slot1.sav").write_bytes(b"profile one")
    (saves / "sub" / "slot2.sav").write_bytes(b"profile two")
    (saves / "log.txt").write_text("a log\n", encoding="utf-8")
    return root, saves


def scan(*paths: Path):  # noqa: ANN201
    return run_scan(ScanOptions(roots=list(paths), detectors=detectors()))


def back_up(paths: list[Path], dest: Path) -> Repo:
    plan = prepare_backup(paths, dest, detectors=detectors())
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    return repo


# ------------------------------------------------------------------------------ root tokens


def test_a_root_token_comes_from_the_override_then_the_variable_then_the_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(roots.env_name(roots.LOCALAPPDATA))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "system"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "me"))
    monkeypatch.delenv(roots.env_name(roots.UNITY_LOCALLOW))
    assert roots.profile_root(roots.LOCALAPPDATA, os.environ) == (
        tmp_path / "system",
        "default:LOCALAPPDATA",
    )
    assert roots.profile_root(roots.UNITY_LOCALLOW, os.environ)[0] == (
        tmp_path / "me" / "AppData" / "LocalLow"
    )
    monkeypatch.setenv(roots.env_name(roots.LOCALAPPDATA), str(tmp_path / "variable"))
    assert roots.profile_root(roots.LOCALAPPDATA, os.environ)[0] == tmp_path / "variable"
    with roots.overriding([f"localappdata={tmp_path / 'option'}"]):
        assert roots.profile_root(roots.LOCALAPPDATA, os.environ)[0] == tmp_path / "option"
    assert roots.profile_root(roots.LOCALAPPDATA, os.environ)[0] == tmp_path / "variable"
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.delenv(roots.env_name(roots.LOCALAPPDATA))
    assert roots.profile_root(roots.LOCALAPPDATA, os.environ) == (None, "LOCALAPPDATA is not set")


def test_the_renpy_saves_root_keeps_its_own_options_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--renpy-saves-root`` before ``--root-override RENPY_SAVES_ROOT=`` before
    ``ENGINESAVE_RENPY_SAVES_ROOT`` (which the tests always set)."""
    from enginesave.engines.renpy.paths import saves_root_override

    assert saves_root_override(None, os.environ) == Path(os.environ["ENGINESAVE_RENPY_SAVES_ROOT"])
    with roots.overriding([f"RENPY_SAVES_ROOT={tmp_path / 'option'}"]):
        assert saves_root_override(None, os.environ) == tmp_path / "option"
        assert roots.profile_root(roots.RENPY_SAVES_ROOT, os.environ)[0] == tmp_path / "option"
        assert saves_root_override(tmp_path / "first", os.environ) == tmp_path / "first"


@pytest.mark.parametrize("token", ["DOCUMENTS", "SAVED_GAMES", "VIRTUALSTORE"])
def test_the_new_roots_take_an_override(token: str, tmp_path: Path) -> None:
    """0.11.0 (D69 3): ``--root-override`` and ``ENGINESAVE_ROOT_*`` for the new tokens."""
    assert roots.parse_overrides([f"{token}={tmp_path}"]) == {token: str(tmp_path)}
    with roots.overriding([f"{token}={tmp_path / 'option'}"]):
        assert roots.profile_root(token, os.environ)[0] == tmp_path / "option"
    assert roots.profile_root(token, {f"ENGINESAVE_ROOT_{token}": str(tmp_path)})[0] == tmp_path


@pytest.mark.parametrize(
    "value",
    ["LOCALAPPDATA", "LOCALAPPDATA=", "GAME_ROOT=C:\\x", "ONEDRIVE=C:\\x", "APPDATA=relative"],
)
def test_a_bad_root_override_is_a_usage_error(value: str, capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(UsageError):
        roots.parse_overrides([value])
    assert main(["list", "--repo", "nowhere", "--root-override", value]) == 2
    assert "--root-override" in capsys.readouterr().err


def test_a_token_this_version_does_not_know_is_refused_with_the_reason() -> None:
    manifest = {
        "sources": [
            {"source_id": "docs", "kind": "global", "root_token": "ONEDRIVE",
             "relative_root": "My Games/X"},
        ],
        "files": [],
    }  # fmt: skip
    with pytest.raises(FatalError, match="later EngineSave"):
        validate_manifest(manifest)
    manifest["sources"][0].update(root_token="GAME_ROOT", relative_root=".")
    with pytest.raises(FatalError, match="patterns"):
        validate_manifest(manifest)
    for unsafe in ("../x", "C:\\x", ""):
        manifest["sources"][0].update(root_token="LOCALAPPDATA", relative_root=unsafe)
        with pytest.raises(FatalError):
            validate_manifest(manifest)


# ------------------------------------------------------------------- sources and statuses


def test_the_rules_give_confirmed_expected_absent_and_candidate(tmp_path: Path) -> None:
    evidence = [Evidence("x", "test", "test")]
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "a.sav").write_bytes(b"a")
    (tmp_path / "empty").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "log.txt").write_bytes(b"log")
    cases = {
        "full": SourceStatus.CONFIRMED,
        "empty": SourceStatus.EXPECTED_ABSENT,
        "missing": SourceStatus.EXPECTED_ABSENT,
    }
    for name, status in cases.items():
        assert common.game_source(tmp_path, name, name, evidence).status is status
    only_log = common.game_source(tmp_path, "logs", "logs", evidence, excludes=["LOG.TXT"])
    assert (only_log.status, only_log.file_count) == (SourceStatus.EXPECTED_ABSENT, 0)
    by_name = common.game_source(tmp_path, "full", "full", evidence, candidate=True)
    assert by_name.status is SourceStatus.CANDIDATE
    game = common.rule_game(
        tmp_path, "testengine", title="T", engine_key="T", sources=[only_log], evidence=evidence
    )
    assert game.status is GameStatus.NOT_FOUND
    found = common.game_source(tmp_path, "full", "full", evidence)
    game = common.rule_game(
        tmp_path, "testengine", title="T", engine_key="T", sources=[found], evidence=evidence
    )
    assert game.status is GameStatus.CONFIRMED and game.display_name == "T"


def test_a_profile_folder_that_cannot_be_found_is_left_out_with_a_warning(
    games_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_pattern_game(games_root, "Lonely", "Lonely")
    monkeypatch.delenv(roots.env_name(roots.LOCALAPPDATA))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    (game,) = scan(games_root).games
    assert [s.source_id for s in game.sources] == ["root"]
    assert any("LOCALAPPDATA is not set" in w for w in game.warnings)


def test_a_pattern_source_goes_back_file_for_file(games_root: Path, tmp_path: Path) -> None:
    """D65 2: only ``Save*.dat`` directly in the game folder, and the profile folder without
    its log: backed up, deleted, restored byte for byte; nothing else is touched."""
    root, saves = build_pattern_game(games_root, "Pattern Game", "Pattern Game")
    (game,) = scan(games_root).games
    assert game.status is GameStatus.CONFIRMED and game.engine == "testengine"
    by_id = {s.source_id: s for s in game.sources}
    assert (by_id["root"].file_count, by_id["profile"].file_count) == (2, 2)
    assert by_id["profile"].root_token is RootToken.LOCALAPPDATA
    assert by_id["profile"].kind is SourceKind.GLOBAL
    repo = back_up([games_root], tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    assert record["identity"]["engine_key"] == "Pattern Game"
    manifest = repo.load_manifest(record["game_id"], repo.latest_snapshot(record["game_id"]))
    sources = {s["source_id"]: s for s in manifest["sources"]}
    assert sources["root"]["patterns"] == ["Save*.dat"] and sources["root"]["relative_root"] == "."
    assert sources["profile"]["excludes"] == ["log.txt"]
    assert sorted((f["source_id"], f["path"]) for f in manifest["files"]) == [
        ("profile", "slot1.sav"),
        ("profile", "sub/slot2.sav"),
        ("root", "Save01.dat"),
        ("root", "save02.DAT"),
    ]
    doc = repo.load_game(record["game_id"])
    templates = {t["source_id"]: t for t in doc["source_templates"]}
    assert templates["root"]["patterns"] == ["Save*.dat"]
    before = {p: p.read_bytes() for p in (root / "Save01.dat", root / "save02.DAT")}
    profile_before = {p: p.read_bytes() for p in (saves / "slot1.sav", saves / "sub" / "slot2.sav")}
    for path in before:
        path.unlink()
    shutil.rmtree(saves)
    (root / "config.ini").write_text("volume=9\n", encoding="utf-8")
    restore_plan = plan_restore(repo, repo.load_catalog(), "Pattern Game")
    targets = {t.source["source_id"]: t for t in restore_plan.targets}
    assert targets["root"].target_dir == root and targets["profile"].target_dir == saves
    result = execute_restore(restore_plan)
    assert result.failures == [] and result.written == 4
    for path, data in {**before, **profile_before}.items():
        assert path.read_bytes() == data
    assert (root / "config.ini").read_text(encoding="utf-8") == "volume=9\n", "not a save"
    assert (root / "Data" / "Save03.dat").read_bytes() == b"game data, not a save"
    assert not (saves / "log.txt").exists()


def test_the_safety_snapshot_before_a_restore_takes_only_the_sources_files(
    games_root: Path, tmp_path: Path
) -> None:
    root, _saves = build_pattern_game(games_root, "Safe Game", "Safe Game")
    repo = back_up([games_root], tmp_path / "repo")
    (root / "Save01.dat").write_bytes(b"newer progress")
    result = execute_restore(plan_restore(repo, repo.load_catalog(), "Safe Game"))
    assert result.safety_snapshot is not None
    game_id = repo.load_catalog()["games"][0]["game_id"]
    safety = repo.load_manifest(game_id, result.safety_snapshot)
    root_files = {f["path"] for f in safety["files"] if f["source_id"] == "root"}
    assert root_files == {"Save01.dat", "save02.DAT"}, "never config.ini or Data/"
    assert (root / "Save01.dat").read_bytes() == b"slot one"


def test_a_restore_follows_the_root_override(games_root: Path, tmp_path: Path) -> None:
    """The profile folder is placed where ``--root-override`` says on this machine."""
    build_pattern_game(games_root, "Moved Game", "Moved Game")
    repo = back_up([games_root], tmp_path / "repo")
    elsewhere = tmp_path / "other-profile"
    code = main(
        ["restore", "--repo", str(repo.root), "--game", "Moved Game", "--sources", "profile",
         "--yes", "--root-override", f"LOCALAPPDATA={elsewhere}"]
    )  # fmt: skip
    assert code == 0
    assert (elsewhere / "Moved Game" / "Saves" / "sub" / "slot2.sav").read_bytes() == b"profile two"
    assert os.environ[roots.env_name(roots.LOCALAPPDATA)] != str(elsewhere), "put back"


def test_the_export_readme_says_where_profile_sources_go(games_root: Path, tmp_path: Path) -> None:
    from enginesave.export.archive import export_readme

    build_pattern_game(games_root, "Readme Game", "Readme Game")
    repo = back_up([games_root], tmp_path / "repo")
    game_id = repo.load_catalog()["games"][0]["game_id"]
    manifest = repo.load_manifest(game_id, repo.latest_snapshot(game_id))
    text = export_readme(repo.load_game(game_id), manifest, "2026-10-26T00:00:00Z", False)
    assert "data/GAME_ROOT/  ->  <game folder>" in text  # D67 3
    assert "data/LOCALAPPDATA/Readme Game/Saves/  ->  %LOCALAPPDATA%\\Readme Game\\Saves" in text


# ------------------------------------------------------------------------------- identity


def test_another_engine_is_matched_by_its_key_and_engine() -> None:
    assert match_key("testengine", {"engine_key": " Foo  Bar "}) == "testengine:foo bar"
    assert match_key("testengine", {"renpy_save_directory": "Foo"}) == ""
    assert match_key("renpy", {"renpy_save_directory": "Foo", "engine_key": "x"}) == "foo"
    known = [
        {"game_id": "a" * 32, "engine": "testengine", "identity": {"engine_key": "Foo"}},
        {"game_id": "b" * 32, "engine": "otherengine", "identity": {"engine_key": "Bar"}},
    ]
    key = match_key("testengine", {"engine_key": "FOO"})
    assert match_record(key, set(), known, engine="testengine") == "a" * 32
    other = match_key("otherengine", {"engine_key": "Foo"})
    assert match_record(other, set(), known, engine="otherengine") is None, "another engine"
    twice = [
        *known,
        {"game_id": "c" * 32, "engine": "testengine", "identity": {"engine_key": "foo"}},
    ]
    assert match_record(key, set(), twice, engine="testengine") is None, "not the only one"


def test_rule_two_needs_the_same_engine_and_no_other_key() -> None:
    root = "c:/games/x"
    record = {
        "game_id": "a" * 32,
        "engine": "testengine",
        "identity": {"engine_key": "Old Title"},
        "installs": [{"game_root": "C:\\Games\\X"}],
    }
    renpy = {
        "game_id": "b" * 32,
        "engine": "renpy",
        "identity": {"renpy_save_directory": None},
        "installs": [{"game_root": "C:\\Games\\X"}],
    }
    from enginesave.fsutil import path_key

    roots_ = {path_key(root)}
    assert match_record("", roots_, [record], engine="testengine") == "a" * 32
    key = match_key("testengine", {"engine_key": "New Title"})
    assert match_record(key, roots_, [record], engine="testengine") is None, "another key"
    assert match_record("", roots_, [renpy], engine="testengine") is None, "another engine"


def test_a_moved_game_of_another_engine_finds_its_record(games_root: Path, tmp_path: Path) -> None:
    build_pattern_game(games_root, "Wanderer", "Wanderer")
    repo = back_up([games_root], tmp_path / "repo")
    game_id = repo.load_catalog()["games"][0]["game_id"]
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    shutil.move(str(games_root / "Wanderer"), str(moved / "Wanderer v2"))
    report = run_scan(ScanOptions(roots=[moved], detectors=detectors(), repo=repo.root))
    assert report.games[0].known_game_id == game_id


# -------------------------------------------------------------------------------- cleanup


def test_a_profile_folder_is_cleaned_up_after_its_checks(games_root: Path, tmp_path: Path) -> None:
    """D65 4: another engine's profile folder is a ``global`` target, cleaned up with its game
    folder as Ren'Py's (the game would use it again): fresh after the backup; a changed save
    refuses it, a changed log (not a save) does not."""
    root, saves = build_pattern_game(games_root, "Clean Game", "Clean Game")
    repo = back_up([games_root], tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    doc = repo.load_game(record["game_id"])
    assert save_folder(doc) == saves
    (saves / "log.txt").write_text("a longer log\n", encoding="utf-8")
    requests = [(GAME_ROOT, str(root)), (GLOBAL, None)]
    alone = plan_cleanup(repo, record, [(GLOBAL, None)], recycle_limit=10**12).targets[0]
    assert any("still used by" in r and str(root) in r for r in alone.reasons), alone.reasons
    _game, target = plan_cleanup(repo, record, requests, recycle_limit=10**12).targets
    assert target.ok, target.reasons
    assert target.files == 3, "the log goes with the folder"
    (saves / "slot1.sav").write_bytes(b"newer")
    _game, target = plan_cleanup(repo, record, requests, recycle_limit=10**12).targets
    assert not target.ok and any(
        "changed since the backup: Saves/slot1.sav" in r for r in target.reasons
    )


def test_a_profile_folder_another_install_still_uses_is_kept(
    games_root: Path, tmp_path: Path
) -> None:
    """D65 4: two records (two titles) that use one profile folder: the global target of one
    is refused while the other's install is there, by the folder's path."""
    build_pattern_game(games_root, "First", "First", profile="Shared")
    second = games_root / "Second"
    shutil.copytree(games_root / "First", second)
    (second / MARKER).write_text("Second\nShared\n", encoding="utf-8")
    repo = back_up([games_root], tmp_path / "repo")
    records = {r["display_name"]: r for r in repo.load_catalog()["games"]}
    assert set(records) == {"First", "Second"}
    requests = [(GAME_ROOT, str(games_root / "First")), (GLOBAL, None)]
    both = plan_cleanup(repo, records["First"], requests, recycle_limit=10**12)
    reasons = both.targets[1].reasons
    assert any("still used by" in r and str(second) in r for r in reasons), "Second stays"
    assert not any(str(games_root / "First") + " " in r for r in reasons), "First goes"
    shutil.rmtree(second)
    both = plan_cleanup(repo, records["First"], requests, recycle_limit=10**12)
    assert both.targets[1].ok, both.targets[1].reasons


def test_a_game_folder_of_another_engine_checks_only_its_saves(
    games_root: Path, tmp_path: Path
) -> None:
    root, _saves = build_pattern_game(games_root, "Folder Game", "Folder Game")
    repo = back_up([games_root], tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    (root / "config.ini").write_text("volume=1\n", encoding="utf-8")  # not a save
    (target,) = plan_cleanup(repo, record, [(GAME_ROOT, str(root))], recycle_limit=10**12).targets
    assert target.ok, target.reasons
    (root / "Save07.dat").write_bytes(b"a new slot")
    (target,) = plan_cleanup(repo, record, [(GAME_ROOT, str(root))], recycle_limit=10**12).targets
    assert any("not in the backup: Save07.dat" in r for r in target.reasons), target.reasons


def test_settings_and_profile_roots_are_never_cleaned_up(games_root: Path, tmp_path: Path) -> None:
    """A record whose profile folder would be EngineSave's own settings folder (an Electron app
    named EngineSave, say) or a profile root itself is refused."""
    build_pattern_game(games_root, "Odd Game", "Odd Game")
    repo = back_up([games_root], tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    doc = repo.load_game(record["game_id"])
    settings = Path(os.environ["APPDATA"]) / "EngineSave"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "gui.json").write_text("{}", encoding="utf-8")
    for template in doc["source_templates"]:
        if template["source_id"] == "profile":
            template.update(root_token="APPDATA", relative_root="EngineSave")
    repo.save_game(doc)
    (target,) = plan_cleanup(repo, record, [(GLOBAL, None)], recycle_limit=10**12).targets
    assert any("is or contains the profile folder" in r for r in target.reasons), target.reasons
    assert (settings / "gui.json").is_file()


def test_the_mirror_stays_out_of_profile_folders(games_root: Path, tmp_path: Path) -> None:
    from enginesave.mirror import mirror_repo

    _root, saves = build_pattern_game(games_root, "Mirror Game", "Mirror Game")
    repo = back_up([games_root], tmp_path / "repo")
    from enginesave.mirror import forbidden_places

    with pytest.raises(UsageError, match="inside or around"):
        mirror_repo(repo, saves / "mirror", forbidden=forbidden_places(repo))
    assert not (saves / "mirror").exists()


# --------------------------------------------------------------------------------- shared


def test_one_listing_per_folder_for_every_detector(
    games_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enginesave.engines import fingerprints

    for name in ("A", "B"):
        folder = games_root / name
        folder.mkdir(parents=True)
        (folder / "UnityPlayer.dll").write_bytes(b"x")

    class Unity(common.EngineDetector):
        engines = ("unity",)

        def recognise(self, directory, listing, found, ctx):  # noqa: ANN001, ANN201
            return None

    calls: list[str] = []
    real = common.list_folder

    def counting(directory: Path):  # noqa: ANN202
        calls.append(str(directory))
        return real(directory)

    monkeypatch.setattr(common, "list_folder", counting)
    report = run_scan(
        ScanOptions(roots=[games_root], detectors=[Unity(), fingerprints.FingerprintDetector()])
    )
    assert sorted(g.engine for g in report.games) == ["unity", "unity"]
    assert len([c for c in calls if c.endswith(("A", "B"))]) == 2, calls


def test_the_scan_json_with_patterns_conforms(games_root: Path) -> None:
    from test_schemas import SCHEMAS, validate

    build_pattern_game(games_root, "Schema Game", "Schema Game")
    data = json.loads(json.dumps(scan(games_root).to_dict()))
    schema = json.loads((SCHEMAS / "scan.schema.json").read_text(encoding="utf-8"))
    assert validate(schema, data, schema) == []


def test_the_window_shows_another_engines_identity_and_its_file_patterns(
    games_root: Path, tmp_path: Path
) -> None:
    from enginesave.gui.state.cleaning import cleanup_requests
    from enginesave.gui.state.scanning import game_detail_lines

    build_pattern_game(games_root, "Shown Game", "Shown Game")
    (game,) = scan(games_root).games
    lines = [text for text, _folder in game_detail_lines(game)]
    assert "标题：Shown Game" in lines and "身份键：Shown Game" in lines
    assert not any(line.startswith("save_directory") for line in lines)
    assert any(line.strip().startswith("只取：Save*.dat") for line in lines)
    assert any(line.strip() == "不取：log.txt" for line in lines)
    repo = back_up([games_root], tmp_path / "repo")
    doc = repo.load_game(repo.load_catalog()["games"][0]["game_id"])
    assert (GLOBAL, None) in cleanup_requests(doc)


# ------------------------------------------------------------------------ the M39 review


def test_a_manifest_file_outside_its_sources_selection_is_refused() -> None:
    """A file a source with patterns/excludes would never take (a crafted or edited
    manifest, an imported zip) is refused: the restore would write where the safety snapshot
    does not look."""
    good = {"source_id": "root", "kind": "local", "root_token": "GAME_ROOT", "relative_root": ".",
            "patterns": ["Save*.dat"]}  # fmt: skip
    profile = {"source_id": "profile", "kind": "global", "root_token": "LOCALAPPDATA",
               "relative_root": "X/Saves", "excludes": ["log.txt", "Cache"]}  # fmt: skip

    def manifest(source_id: str, path: str) -> dict:
        return {
            "sources": [good, profile],
            "files": [{"source_id": source_id, "path": path, "sha256": "0" * 64, "size": 1}],
        }

    validate_manifest(manifest("root", "Save01.dat"))
    validate_manifest(manifest("profile", "sub/log.txt"))  # only the top level is excluded
    for source_id, path in (
        ("root", "Game.exe"),
        ("root", "Data/Save03.dat"),
        ("profile", "log.txt"),
        ("profile", "cache/data_0"),
    ):
        with pytest.raises(FatalError, match="not one of the files"):
            validate_manifest(manifest(source_id, path))


def test_root_overrides_before_and_after_the_subcommand_both_count(
    games_root: Path, tmp_path: Path
) -> None:
    build_pattern_game(games_root, "Both Sides", "Both Sides")
    repo = back_up([games_root], tmp_path / "repo")
    first, other = tmp_path / "first", tmp_path / "other"
    code = main(
        ["--root-override", f"LOCALAPPDATA={first}", "restore", "--repo", str(repo.root),
         "--game", "Both Sides", "--sources", "profile", "--yes",
         "--root-override", f"APPDATA={other}"]
    )  # fmt: skip
    assert code == 0
    assert (first / "Both Sides" / "Saves" / "slot1.sav").is_file()


def test_a_relative_root_variable_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(roots.env_name(roots.LOCALAPPDATA), "relative-folder")
    path, how = roots.profile_root(roots.LOCALAPPDATA, os.environ)
    assert path is None and "not an absolute path" in how


def test_game_root_must_be_a_game_of_the_records_engine_and_key(
    games_root: Path, tmp_path: Path
) -> None:
    from fixtures.engines import build_wolf

    build_pattern_game(games_root, "Alpha", "Alpha")
    repo = back_up([games_root], tmp_path / "repo")
    other = tmp_path / "other"
    build_pattern_game(other, "Beta", "Beta", profile="Beta")
    with pytest.raises(UsageError, match="not Alpha"):
        plan_restore(repo, repo.load_catalog(), "Alpha", sources=["root"], game_root=other / "Beta")
    wolf = build_wolf(tmp_path / "wolf", exe="Alpha")
    with pytest.raises(UsageError, match="not a testengine game"):
        plan_restore(repo, repo.load_catalog(), "Alpha", sources=["root"], game_root=wolf.root)


def test_an_export_stays_out_of_profile_folders_after_an_import(
    games_root: Path, tmp_path: Path
) -> None:
    from enginesave.export import export_snapshot, import_archive

    _root, saves = build_pattern_game(games_root, "Travel", "Travel")
    repo = back_up([games_root], tmp_path / "repo")
    game_id = repo.load_catalog()["games"][0]["game_id"]
    archive = tmp_path / "travel.zip"
    export_snapshot(repo, game_id, repo.latest_snapshot(game_id), archive)
    second = Repo.open_or_create(tmp_path / "second")
    import_archive(second.root, archive)
    with pytest.raises(UsageError):
        export_snapshot(second, game_id, second.latest_snapshot(game_id), saves / "inside.zip")
    assert not (saves / "inside.zip").exists()


def test_a_profile_folder_inside_or_around_another_games_is_kept(
    games_root: Path, tmp_path: Path
) -> None:
    root, _saves = build_pattern_game(games_root, "Outer", "Outer", profile="Shared")
    inner = games_root / "Inner"
    shutil.copytree(root, inner)
    (inner / MARKER).write_text("Inner\nShared/Saves/Inner\n", encoding="utf-8")
    (local_appdata() / "Shared" / "Saves" / "Inner" / "Saves").mkdir(parents=True)
    (local_appdata() / "Shared" / "Saves" / "Inner" / "Saves" / "x.sav").write_bytes(b"inner")
    repo = back_up([games_root], tmp_path / "repo")
    records = {r["display_name"]: r for r in repo.load_catalog()["games"]}
    for name, folder in (("Outer", root), ("Inner", inner)):
        requests = [(GAME_ROOT, str(folder)), (GLOBAL, None)]
        target = plan_cleanup(repo, records[name], requests, recycle_limit=10**12).targets[1]
        assert any("still used by" in r for r in target.reasons), (name, target.reasons)


def test_the_last_scan_counts_for_a_profile_folder(games_root: Path, tmp_path: Path) -> None:
    """A game outside the library that the window's last scan found, using the same profile
    folder, keeps it (the record's own install is cleaned in the same run)."""
    root, _saves = build_pattern_game(games_root, "Recorded", "Recorded", profile="Common")
    repo = back_up([games_root], tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    others = tmp_path / "others"
    build_pattern_game(others, "Unrecorded", "Unrecorded", profile="Common")
    report = run_scan(ScanOptions(roots=[games_root, others], detectors=detectors()))
    requests = [(GAME_ROOT, str(root)), (GLOBAL, None)]
    target = plan_cleanup(repo, record, requests, recycle_limit=10**12, report=report).targets[1]
    assert any("still used by" in r and "Unrecorded" in r for r in target.reasons), target.reasons
    shutil.rmtree(others / "Unrecorded")
    target = plan_cleanup(repo, record, requests, recycle_limit=10**12, report=report).targets[1]
    assert target.ok, target.reasons


def test_a_profile_folder_is_checked_whole_whatever_its_patterns(
    games_root: Path, tmp_path: Path
) -> None:
    root, saves = build_pattern_game(games_root, "Partial", "Partial")
    repo = back_up([games_root], tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    doc = repo.load_game(record["game_id"])
    game_id = record["game_id"]
    manifest = repo.load_manifest(game_id, repo.latest_snapshot(game_id))
    for source in [*manifest["sources"], *doc["source_templates"]]:
        if source["source_id"] == "profile":
            source["patterns"] = ["slot1.sav"]  # as if the source took only that
    manifest["files"] = [
        f for f in manifest["files"] if f["source_id"] != "profile" or f["path"] == "slot1.sav"
    ]
    snapshot = repo.snapshot_dir(game_id, repo.latest_snapshot(game_id))
    shutil.rmtree(layout.source_data_dir(snapshot, manifest, "profile") / "sub")
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    repo.save_game(doc)
    requests = [(GAME_ROOT, str(root)), (GLOBAL, None)]
    target = plan_cleanup(repo, record, requests, recycle_limit=10**12).targets[1]
    assert any("not in the backup: Saves/sub/slot2.sav" in r for r in target.reasons), (
        target.reasons
    )


def test_a_record_follows_its_sources_new_profile_folder(games_root: Path, tmp_path: Path) -> None:
    root, _saves = build_pattern_game(games_root, "Mover", "Mover", profile="Old")
    repo = back_up([games_root], tmp_path / "repo")
    (root / MARKER).write_text("Mover\nNew\n", encoding="utf-8")
    (local_appdata() / "New" / "Saves").mkdir(parents=True)
    (local_appdata() / "New" / "Saves" / "a.sav").write_bytes(b"new place")
    back_up([games_root], tmp_path / "repo")
    doc = repo.load_game(repo.load_catalog()["games"][0]["game_id"])
    (profile,) = [t for t in doc["source_templates"] if t["source_id"] == "profile"]
    assert profile["relative_root"] == "New/Saves"
    assert save_folder(doc) == local_appdata() / "New" / "Saves"


def test_a_game_folder_now_of_another_engine_is_not_cleaned_up(
    games_root: Path, tmp_path: Path
) -> None:
    root, _saves = build_pattern_game(games_root, "Changed", "Changed")
    repo = back_up([games_root], tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    (root / MARKER).unlink()
    (root / "UnityPlayer.dll").write_bytes(b"dll")
    (target,) = plan_cleanup(repo, record, [(GAME_ROOT, str(root))], recycle_limit=10**12).targets
    assert any("no game is recognised" in r for r in target.reasons), target.reasons


def test_name_suggestions_stay_within_one_engine_and_skip_generic_exes(
    games_root: Path, tmp_path: Path
) -> None:
    build_pattern_game(games_root, "Alpha", "Alpha")
    repo = back_up([games_root], tmp_path / "repo")
    others = tmp_path / "others"
    build_pattern_game(others, "Totally Different", "Totally Different")  # Game.exe too
    report = run_scan(ScanOptions(roots=[others], detectors=detectors(), repo=repo.root))
    assert report.games[0].link_suggestions == [], "Game.exe is no reason"
    (others / "Totally Different" / "Game.exe").rename(others / "Totally Different" / "Alpha.exe")
    record = repo.load_catalog()["games"][0]
    known = [{**record, "engine": "otherengine"}]
    from enginesave.identity import link_suggestions

    (game,) = run_scan(ScanOptions(roots=[others], detectors=detectors())).games
    assert link_suggestions(game, known) == [], "another engine's record"


def test_a_helper_exe_does_not_name_the_game(tmp_path: Path) -> None:
    (tmp_path / "zed.exe").write_bytes(b"MZ")
    (tmp_path / "UnityCrashHandler64.exe").write_bytes(b"MZ")
    game = common.rule_game(
        tmp_path, "testengine", title=None, engine_key=None, sources=[], evidence=[]
    )
    assert game.identity.exe_basename == "zed"


def test_rule_two_does_not_cross_engines_in_a_scan(games_root: Path, tmp_path: Path) -> None:
    """A Ren'Py record without a save directory that lists the folder as its install is not
    taken by another engine's game found there."""
    root, _saves = build_pattern_game(games_root, "Shared Folder", "Shared Folder")
    repo = Repo.open_or_create(tmp_path / "repo")
    catalog = repo.load_catalog()
    from enginesave.identity import match_known_game

    known = [{"game_id": "a" * 32, "kind": "game", "engine": "renpy",
              "identity": {"renpy_save_directory": None},
              "installs": [{"game_root": str(root)}]}]  # fmt: skip
    (game,) = run_scan(ScanOptions(roots=[games_root], detectors=detectors())).games
    assert catalog is not None
    assert match_known_game(game, known) is None


def test_an_import_finds_the_record_of_its_engine(games_root: Path, tmp_path: Path) -> None:
    """Rule 1 by engine and key on an import: the other library's record of the same game."""
    from enginesave.export import export_snapshot, plan_import

    build_pattern_game(games_root, "Imported", "Imported")
    first = back_up([games_root], tmp_path / "first")
    second = back_up([games_root], tmp_path / "second")
    game_id = first.load_catalog()["games"][0]["game_id"]
    archive = tmp_path / "imported.zip"
    export_snapshot(first, game_id, first.latest_snapshot(game_id), archive)
    plan = plan_import(second.root, archive)
    assert (plan.game_id, plan.matched_by) == (
        second.load_catalog()["games"][0]["game_id"],
        "identity",
    )


def test_excludes_are_not_entered_and_count_only_at_the_top(tmp_path: Path) -> None:
    from enginesave.fsutil import dir_stats, source_files

    (tmp_path / "Cache" / "deep").mkdir(parents=True)
    (tmp_path / "Cache" / "deep" / "x.bin").write_bytes(b"cache")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "log.txt").write_bytes(b"kept")
    (tmp_path / "LOG.TXT").write_bytes(b"left out")
    (tmp_path / "save.dat").write_bytes(b"save")
    files, errors = source_files(tmp_path, (), ["cache", "log.txt"])
    assert sorted(f.rel for f in files) == ["save.dat", "sub/log.txt"] and errors == []
    assert dir_stats(tmp_path, (), ["cache", "log.txt"]) == (True, 2, 8)
    missing, problems = source_files(tmp_path / "gone", ["*.dat"])
    assert missing == [] and len(problems) == 1, "a folder that is gone is said, not empty"


def test_several_profile_sources_and_profile_roots_are_refused(
    games_root: Path, tmp_path: Path
) -> None:
    root, _saves = build_pattern_game(games_root, "Twice", "Twice")
    repo = back_up([games_root], tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    doc = repo.load_game(record["game_id"])
    doc["source_templates"].append(
        {"source_id": "more", "kind": "global", "root_token": "APPDATA", "relative_root": "More"}
    )
    repo.save_game(doc)
    target = plan_cleanup(
        repo, record, [(GAME_ROOT, str(root)), (GLOBAL, None)], recycle_limit=10**12
    ).targets[1]
    assert any("several save folders" in r for r in target.reasons), target.reasons


@pytest.mark.parametrize(
    "token", [roots.UNITY_LOCALLOW, roots.DOCUMENTS, roots.SAVED_GAMES, roots.VIRTUALSTORE]
)
def test_a_profile_root_inside_the_folder_is_refused(
    games_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """The new roots of 0.11.0 (D69 3) as the old ones: never cleaned up with a folder."""
    root, saves = build_pattern_game(games_root, "Holder", "Holder")
    repo = back_up([games_root], tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    inside = saves / token
    inside.mkdir()
    monkeypatch.setenv(roots.env_name(token), str(inside))
    target = plan_cleanup(
        repo, record, [(GAME_ROOT, str(root)), (GLOBAL, None)], recycle_limit=10**12
    ).targets[1]
    assert any(f"is or contains the profile folder {inside}" in r for r in target.reasons)


def test_the_restore_says_who_else_uses_the_profile_folder(
    games_root: Path, tmp_path: Path
) -> None:
    build_pattern_game(games_root, "One", "One", profile="Both")
    second = games_root / "Two"
    shutil.copytree(games_root / "One", second)
    (second / MARKER).write_text("Two\nBoth\n", encoding="utf-8")
    repo = back_up([games_root], tmp_path / "repo")
    restore_plan = plan_restore(repo, repo.load_catalog(), "One", sources=["profile"])
    assert restore_plan.shared_with == ["Two"]
    assert any("shared with Two" in note for note in restore_plan.notes), restore_plan.notes


def test_the_window_says_which_root_variables_are_set(tmp_path: Path) -> None:
    from enginesave.gui.settings import GuiSettings
    from enginesave.gui.state.guidance import override_notice

    env = {"ENGINESAVE_ROOT_RENPY_SAVES_ROOT": str(tmp_path / "r")}
    notice = override_notice(GuiSettings(), env)
    assert notice is not None and "ENGINESAVE_ROOT_RENPY_SAVES_ROOT" in notice.text
    notice = override_notice(GuiSettings(), {"ENGINESAVE_ROOT_LOCALAPPDATA": str(tmp_path / "l")})
    assert notice is not None and "LOCALAPPDATA" in notice.text
    assert override_notice(GuiSettings(), {}) is None


def test_a_game_folder_named_unsafely_by_the_game_does_not_end_the_scan(games_root: Path) -> None:
    class Unsafe(common.EngineDetector):
        engines = ("unity",)

        def recognise(self, directory, listing, found, ctx):  # noqa: ANN001, ANN201
            return common.rule_game(
                directory, "unity", title=None, engine_key=None, evidence=[],
                sources=[common.game_source(directory, "x", "../..", [])],
            )  # fmt: skip

    from enginesave.engines.fingerprints import FingerprintDetector

    folder = games_root / "Odd"
    folder.mkdir(parents=True)
    (folder / "UnityPlayer.dll").write_bytes(b"dll")
    report = run_scan(ScanOptions(roots=[games_root], detectors=[Unsafe(), FingerprintDetector()]))
    assert [(g.engine, g.status) for g in report.games] == [("unity", GameStatus.UNSUPPORTED)]


def test_a_root_token_on_a_scan_folder_or_the_library_is_refused(
    games_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """D66 7: a token moved onto what the command scans or writes to, or onto another
    token's own folder, is a usage error before anything runs."""
    games_root.mkdir(parents=True, exist_ok=True)
    inside = games_root / "profile"
    code = main(["scan", str(games_root), "--root-override", f"LOCALAPPDATA={inside}"])
    assert code == 2 and "overlaps the scan folder" in capsys.readouterr().err
    library = tmp_path / "library"
    code = main(["list", "--repo", str(library), "--root-override", f"APPDATA={library}"])
    assert code == 2 and "overlaps the library" in capsys.readouterr().err
    locallow = os.environ[roots.env_name(roots.UNITY_LOCALLOW)]
    code = main(["list", "--repo", str(library), "--root-override", f"APPDATA={locallow}"])
    assert code == 2 and "is the folder of UNITY_LOCALLOW too" in capsys.readouterr().err
    # M44 review: around it as well as inside it
    around = games_root.parent
    code = main(["scan", str(games_root), "--root-override", f"LOCALAPPDATA={around}"])
    assert code == 2 and "overlaps the scan folder" in capsys.readouterr().err
    code = main(["list", "--repo", str(library), "--root-override", f"APPDATA={tmp_path}"])
    assert code == 2 and "overlaps the library" in capsys.readouterr().err


def test_a_relative_root_token_is_no_conflict(games_root: Path) -> None:
    """M44 review: a value that is not an absolute path is never used, so it overlaps nothing."""
    env = {roots.env_name(roots.LOCALAPPDATA): "saves"}
    assert roots.override_conflicts(env, [Path(os.path.abspath("saves")).parent]) == []


def test_the_window_shows_a_root_token_conflict(tmp_path: Path) -> None:
    from enginesave.gui.settings import GuiSettings
    from enginesave.gui.state.guidance import override_notice

    env = {roots.env_name(roots.LOCALAPPDATA): str(tmp_path / "games" / "x")}
    notice = override_notice(GuiSettings(scan_roots=[str(tmp_path / "games")]), env)
    assert notice is not None and "冲突" in notice.text and "与扫描根目录" in notice.text  # D70 8
    from enginesave.gui import i18n

    try:
        i18n.set_language(i18n.EN)
        english = override_notice(GuiSettings(scan_roots=[str(tmp_path / "games")]), env)
        assert english is not None and "overlaps the scan folder" in english.text
    finally:
        i18n.set_language(i18n.ZH)
    # M44 review: the window's own saves-root override does not hide the token's conflict
    own = GuiSettings(scan_roots=[str(tmp_path / "games")], saves_root_override=str(tmp_path))
    notice = override_notice(own, env)
    assert notice is not None and notice.clearable
    assert "全局存档根目录已改为" in notice.text and "与扫描根目录" in notice.text
