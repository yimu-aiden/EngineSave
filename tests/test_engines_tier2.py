"""0.10.0 second tier (D65, M41): Unity -- ``app.info``, the LocalLow folder (through
``ENGINESAVE_ROOT_UNITY_LOCALLOW``, a sandbox folder in the tests) and the PlayerPrefs kept as a
``.reg`` file. The registry is never written: the round trips use a fake reader, and one test
reads an existing key of the user's registry, read-only, to check the real reader."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from enginesave import registry
from enginesave.backup import execute_backup, prepare_backup
from enginesave.cleanup import GAME_ROOT, GLOBAL, plan_cleanup
from enginesave.models import GameStatus, RootToken, SourceKind, SourceStatus
from enginesave.registry import REG_BINARY, REG_DWORD, REG_QWORD, REG_SZ, Value
from enginesave.repo import Repo
from enginesave.restore import execute_restore, plan_restore
from enginesave.scan import ScanOptions, run_scan
from fixtures.engines import build_unity

PREFS = [
    Value("unity.player_session_count_h922449978", REG_DWORD, (7).to_bytes(4, "little")),
    Value("volume_h1234", REG_DWORD, b"\x00\x00\x00\x00\x00\x00\xe8\x3f"),  # 0.75 as a double
    Value("name_h5678", REG_BINARY, b"Hero\x00"),
    Value("Screenmanager Resolution Width_h182942802", REG_DWORD, (1920).to_bytes(4, "little")),
]


@pytest.fixture
def prefs(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Value]]:
    """A fake registry for the scan and the backup: subkey (case-folded) -> values."""
    keys: dict[str, list[Value]] = {}
    monkeypatch.setattr(registry, "reader", lambda subkey: keys.get(subkey.casefold()))
    return keys


def only(root: Path):  # noqa: ANN201
    games = run_scan(ScanOptions(roots=[root])).games
    assert len(games) == 1, [(g.game_root, g.engine) for g in games]
    return games[0]


def back_up(root: Path, dest: Path) -> Repo:
    plan = prepare_backup([root], dest)
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    return repo


def test_unity_saves_and_playerprefs_go_back(
    games_root: Path, tmp_path: Path, prefs: dict[str, list[Value]]
) -> None:
    built = build_unity(games_root)
    prefs["software\\fixture co\\fixture game"] = PREFS
    game = only(games_root)
    assert (game.engine, game.status, game.display_name) == (
        "unity",
        GameStatus.CONFIRMED,
        "Fixture Game",
    )
    assert (game.identity.company, game.identity.product) == ("Fixture Co", "Fixture Game")
    locallow, playerprefs = game.sources
    assert (locallow.root_token, locallow.relative_root) == (
        RootToken.UNITY_LOCALLOW,
        "Fixture Co/Fixture Game",
    )
    assert (locallow.file_count, locallow.excludes) == (2, ["Player.log", "Player-prev.log"])
    assert (playerprefs.kind, playerprefs.status, playerprefs.file_count) == (
        SourceKind.REGISTRY,
        SourceStatus.CONFIRMED,
        1,
    )
    repo = back_up(games_root, tmp_path / "repo")
    game_id = repo.load_catalog()["games"][0]["game_id"]
    manifest = repo.load_manifest(game_id, repo.latest_snapshot(game_id))
    reg = next(f for f in manifest["files"] if f["source_id"] == "playerprefs")
    assert reg["path"] == "PlayerPrefs.reg"
    exported = registry.export("Software\\Fixture Co\\Fixture Game", PREFS)
    for path in built.saves:
        path.unlink()
    restore_plan = plan_restore(repo, repo.load_catalog(), game_id)
    assert any("PlayerPrefs.reg" in note for note in restore_plan.notes), restore_plan.notes
    result = execute_restore(restore_plan)
    assert result.failures == [] and result.written == len(built.saves) + 1
    for path, data in built.saves.items():
        assert path.read_bytes() == data
    assert (built.root / "PlayerPrefs.reg").read_bytes() == exported, "imported by the user"


def test_without_app_info_a_unity_game_is_unsupported(games_root: Path, prefs: dict) -> None:
    build_unity(games_root, app_info=False)
    game = only(games_root)
    assert (game.engine, game.status, game.sources) == ("unity", GameStatus.UNSUPPORTED, [])
    assert any("app.info" in w for w in game.warnings)


def test_no_saves_and_no_key_is_not_found(games_root: Path, prefs: dict) -> None:
    build_unity(games_root, saves=False)
    game = only(games_root)
    assert game.status is GameStatus.NOT_FOUND
    assert [s.status for s in game.sources] == [SourceStatus.EXPECTED_ABSENT] * 2


def test_default_company_is_never_cleaned_up(games_root: Path, tmp_path: Path, prefs: dict) -> None:
    built = build_unity(games_root, company="DefaultCompany", product="My project")
    game = only(games_root)
    assert any("DefaultCompany" in w for w in game.warnings)
    repo = back_up(games_root, tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    requests = [(GAME_ROOT, str(built.root)), (GLOBAL, None)]
    _game, target = plan_cleanup(repo, record, requests, recycle_limit=10**12).targets
    assert any("DefaultCompany" in r for r in target.reasons), target.reasons


def test_the_registry_is_never_a_cleanup_target(
    games_root: Path, tmp_path: Path, prefs: dict[str, list[Value]]
) -> None:
    built = build_unity(games_root)
    prefs["software\\fixture co\\fixture game"] = PREFS
    repo = back_up(games_root, tmp_path / "repo")
    record = repo.load_catalog()["games"][0]
    requests = [(GAME_ROOT, str(built.root)), (GLOBAL, None)]
    game_target, global_target = plan_cleanup(repo, record, requests, recycle_limit=10**12).targets
    assert game_target.ok and global_target.ok, (game_target.reasons, global_target.reasons)
    assert "HKEY" not in global_target.path


# ------------------------------------------------------------------------- the .reg file


def test_the_reg_file_is_what_regedit_writes() -> None:
    values = [
        Value("", REG_SZ, "default\x00".encode("utf-16-le")),
        Value('quote " and \\ slash', REG_SZ, "a \\ b\x00".encode("utf-16-le")),
        Value("n_h1", REG_DWORD, (255).to_bytes(4, "little")),
        Value("f_h2", REG_DWORD, b"\x00\x00\x00\x00\x00\x00\xf0\x3f"),
        Value("q_h3", REG_QWORD, (1).to_bytes(8, "little")),
        Value("b_h4", REG_BINARY, bytes(range(40))),
        Value("odd", REG_SZ, b"\x41\x00"),  # no terminating NUL: kept as bytes
    ]
    data = registry.export("Software\\Co\\Game", values)
    assert data.startswith(b"\xff\xfe")
    text = data[2:].decode("utf-16-le")
    lines = text.split("\r\n")
    assert lines[:3] == [
        "Windows Registry Editor Version 5.00",
        "",
        "[HKEY_CURRENT_USER\\Software\\Co\\Game]",
    ]
    assert '@="default"' in lines
    assert '"quote \\" and \\\\ slash"="a \\\\ b"' in lines
    assert '"n_h1"=dword:000000ff' in lines
    assert '"f_h2"=hex(4):00,00,00,00,00,00,f0,3f' in lines, "a double under REG_DWORD stays whole"
    assert '"q_h3"=hex(b):01,00,00,00,00,00,00,00' in lines
    assert '"odd"=hex(1):41,00' in lines
    start = next(i for i, line in enumerate(lines) if line.startswith('"b_h4"'))
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith("  "):
            break
        block.append(line)
    assert len(block) > 1 and all(len(line) <= 80 for line in block), "wrapped as regedit does"
    joined = "\r\n".join(block).replace("\\\r\n  ", "")
    assert joined == '"b_h4"=hex:' + ",".join(f"{b:02x}" for b in range(40))
    assert text.endswith("\r\n\r\n")
    assert "\n" not in text.replace("\r\n", "")


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows registry")
def test_the_real_reader_reads_an_existing_key_read_only() -> None:
    """``HKCU\\Control Panel\\International`` (always there): opened with KEY_READ, values raw.
    The one test that reaches the real registry, and only to read: nothing is ever written to
    it (rule 12; every other test replaces ``registry.reader``)."""
    values = registry.read_values("Control Panel\\International")
    assert values, "the key has values"
    assert all(isinstance(v.data, bytes) and isinstance(v.kind, int) for v in values)
    assert registry.read_values("Software\\EngineSave-no-such-key-0f3a") is None


def test_the_scan_page_sees_changed_playerprefs(
    games_root: Path, tmp_path: Path, prefs: dict[str, list[Value]]
) -> None:
    """D54 for a registry source: the export now against the snapshot's, by content."""
    from enginesave.gui.state.changes import save_changes
    from enginesave.gui.state.common import game_key

    build_unity(games_root)
    key = "software\\fixture co\\fixture game"
    prefs[key] = PREFS
    repo = back_up(games_root, tmp_path / "repo")
    report = run_scan(ScanOptions(roots=[games_root], repo=repo.root))
    (game,) = report.games
    change = save_changes(report, repo.root)[game_key(game)]
    assert (change.added, change.changed, change.removed, change.error) == (0, 0, 0, None)
    prefs[key] = [*PREFS[:-1], Value(PREFS[-1].name, REG_DWORD, (1280).to_bytes(4, "little"))]
    change = save_changes(report, repo.root)[game_key(game)]
    assert (change.added, change.changed, change.removed) == (0, 1, 0)
    del prefs[key]
    change = save_changes(report, repo.root)[game_key(game)]
    assert change.removed == 1


def test_an_export_keeps_the_playerprefs(
    games_root: Path, tmp_path: Path, prefs: dict[str, list[Value]]
) -> None:
    import zipfile

    from enginesave.cli import main

    build_unity(games_root)
    prefs["software\\fixture co\\fixture game"] = PREFS
    repo = back_up(games_root, tmp_path / "repo")
    game_id = repo.load_catalog()["games"][0]["game_id"]
    out = tmp_path / "out.zip"
    assert main(["export", "--repo", str(repo.root), "--game", game_id, "--to", str(out)]) == 0
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        raw = archive.read("README.txt")
    readme = raw.decode("utf-8-sig")
    assert raw.startswith(b"\xef\xbb\xbf"), "M46 review: a BOM as in the library"
    assert "data/GAME_ROOT/PlayerPrefs.reg" in names  # D67 3: GAME_ROOT's . itself
    assert "double-click it to import" in readme
    # M46 review: an anonymized archive names no folder for the key, but the key's form
    assert "注册表 GAME_ROOT" not in readme and "HKEY_CURRENT_USER\\Software\\<公司>" in readme
    manifest = repo.load_manifest(game_id, repo.snapshot_ids(game_id)[-1])
    dirs = {s["root_token"]: s["data_dir"] for s in manifest["sources"]}
    assert dirs["UNITY_LOCALLOW"] == "data/UNITY_LOCALLOW/Fixture Co/Fixture Game"
    snapshot = repo.snapshot_dir(game_id, repo.snapshot_ids(game_id)[-1])
    own = (snapshot / "README.txt").read_text(encoding="utf-8-sig")
    assert "注册表 HKEY_CURRENT_USER\\Software\\Fixture Co\\Fixture Game" in own
    assert "data\\UNITY_LOCALLOW\\Fixture Co\\Fixture Game\\  →  " in own
    index = (repo.root / "INDEX.txt").read_text(encoding="utf-8-sig")
    assert "存档目录：HKEY_CURRENT_USER\\Software\\Fixture Co\\Fixture Game" in index


# ---------------------------------------------------------------------- the engine review


def test_a_second_restore_keeps_the_playerprefs_file_in_its_safety_snapshot(
    games_root: Path, tmp_path: Path, prefs: dict[str, list[Value]]
) -> None:
    """The safety snapshot before a restore keeps the PlayerPrefs.reg in the game folder as a
    file (it once read the game folder's path as a registry key and failed every restore)."""
    built = build_unity(games_root)
    prefs["software\\fixture co\\fixture game"] = PREFS
    repo = back_up(games_root, tmp_path / "repo")
    game_id = repo.load_catalog()["games"][0]["game_id"]
    assert execute_restore(plan_restore(repo, repo.load_catalog(), game_id)).failures == []
    (built.root / "PlayerPrefs.reg").write_bytes(b"\xff\xfeedited by hand")
    result = execute_restore(plan_restore(repo, repo.load_catalog(), game_id))
    assert result.failures == [] and result.safety_snapshot is not None
    safety = repo.load_manifest(game_id, result.safety_snapshot)
    assert ("playerprefs", "PlayerPrefs.reg") in {
        (f["source_id"], f["path"]) for f in safety["files"]
    }


def test_playerprefs_are_kept_when_the_names_are_no_folder_names(
    games_root: Path, prefs: dict[str, list[Value]]
) -> None:
    build_unity(games_root, product="Who?", saves=False)
    prefs["software\\fixture co\\who?"] = PREFS
    game = only(games_root)
    assert [s.source_id for s in game.sources] == ["playerprefs"]
    assert game.status is GameStatus.CONFIRMED


def test_a_destination_named_like_the_registry_path_is_not_refused(
    games_root: Path, tmp_path: Path, prefs: dict[str, list[Value]], monkeypatch: pytest.MonkeyPatch
) -> None:
    build_unity(games_root)
    prefs["software\\fixture co\\fixture game"] = PREFS
    monkeypatch.chdir(tmp_path)
    plan = prepare_backup([games_root], tmp_path / "HKEY_CURRENT_USER")
    assert plan.games


def test_the_reg_file_keeps_odd_values_importable() -> None:
    values = [
        Value("lone", REG_SZ, b"\x00\xd8\x00\x00"),  # a lone surrogate: written as bytes
        Value("lines", REG_SZ, "a\nb\x00".encode("utf-16-le")),
        Value("bad\nname", REG_DWORD, b"\x01\x00\x00\x00"),
        Value("name \udc80", REG_DWORD, b"\x02\x00\x00\x00"),
    ]
    text = registry.export("Software\\Co\\Game", values)[2:].decode("utf-16-le", "surrogatepass")
    lines = text.split("\r\n")
    assert '"lone"=hex(1):00,d8,00,00' in lines
    assert '"lines"=hex(1):61,00,0a,00,62,00,00,00' in lines
    assert "; left out: a value whose name has a line break" in lines
    assert "\n" not in text.replace("\r\n", "")


def test_playerprefs_that_change_during_the_backup_make_it_partial(
    games_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_unity(games_root)
    calls = {"n": 0}

    def changing(subkey: str) -> list[Value]:
        calls["n"] += 1
        return [Value("n_h1", REG_DWORD, calls["n"].to_bytes(4, "little"))]

    monkeypatch.setattr(registry, "reader", changing)
    plan = prepare_backup([games_root], tmp_path / "repo")
    repo = Repo.open_or_create(plan.dest)
    result = execute_backup(plan, repo)
    game_id = repo.load_catalog()["games"][0]["game_id"]
    manifest = repo.load_manifest(game_id, repo.latest_snapshot(game_id))
    prefs_source = next(s for s in manifest["sources"] if s["source_id"] == "playerprefs")
    assert prefs_source["consistency"] == "changed_during_backup"
    assert manifest["status"] == "partial" and result.exit_code == 1


def test_a_unity_data_folder_named_otherwise_than_the_exe(
    games_root: Path, prefs: dict[str, list[Value]]
) -> None:
    built = build_unity(games_root, exe="Launcher Name")
    (built.root / "Launcher Name.exe").rename(built.root / "Other.exe")
    game = only(games_root)
    assert (game.status, game.identity.product) == (GameStatus.CONFIRMED, "Fixture Game")


def test_default_company_folder_still_opens_but_is_not_cleaned(
    games_root: Path, tmp_path: Path, prefs: dict
) -> None:
    from enginesave.cleanup import save_folder

    build_unity(games_root, company="DefaultCompany", product="My project")
    repo = back_up(games_root, tmp_path / "repo")
    doc = repo.load_game(repo.load_catalog()["games"][0]["game_id"])
    folder = save_folder(doc)
    assert folder is not None and folder.name == "My project"


def test_an_app_info_with_nul_bytes_only_concerns_its_game(
    games_root: Path, prefs: dict[str, list[Value]]
) -> None:
    """D66 3: UTF-16 without a BOM is read; other NUL bytes leave that game unsupported, and
    the scan goes on (the registry was once asked for a key name with a NUL and the whole scan
    stopped)."""
    utf16 = build_unity(games_root, "Wide", saves=False)
    info = utf16.root / "Fixture_Data" / "app.info"
    info.write_bytes("Wide Co\nWide Game".encode("utf-16-le"))
    broken = build_unity(games_root, "Broken", saves=False)
    (broken.root / "Fixture_Data" / "app.info").write_bytes(b"Co\x00\x00x\nGa\x00me\n")
    odd = build_unity(games_root, "Odd", saves=False)  # M44 review: no UTF-16 (odd length)
    (odd.root / "Fixture_Data" / "app.info").write_bytes(b"Co\x00\nGame\n")
    asked: list[str] = []
    real = registry.reader

    def reader(subkey: str) -> object:
        asked.append(subkey)
        return real(subkey)

    registry.reader = reader
    try:
        found = run_scan(ScanOptions(roots=[games_root])).games
    finally:
        registry.reader = real
    games = {Path(g.game_root).name: g for g in found}
    assert (games["Wide"].identity.company, games["Wide"].identity.product) == (
        "Wide Co",
        "Wide Game",
    )
    assert games["Broken"].status is GameStatus.UNSUPPORTED
    assert games["Odd"].status is GameStatus.UNSUPPORTED and games["Odd"].sources == []
    assert not any("\x00" in subkey for subkey in asked), "no key name with a NUL"
    assert registry.read_values("Software\\Co\x00x\\Game") is None
