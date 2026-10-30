"""EngineSave was called SaveTrail up to 1.0.0: its settings are taken over once, its exports
import, and its libraries open as they are (the library format names no program)."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

from enginesave.appdirs import carry_over_settings, config_dir
from enginesave.backup import execute_backup, prepare_backup
from enginesave.export import (
    EXPORT_JSON,
    LEGACY_EXPORT_JSON,
    execute_import,
    export_snapshot,
    plan_import,
)
from enginesave.repo import Repo
from enginesave.verify import verify_repo
from fixtures.builder import build_f1


def roaming() -> Path:
    return Path(os.environ["APPDATA"])  # the sandbox of conftest's isolated_env


def test_savetrails_settings_are_copied_once_and_never_over_engine_saves() -> None:
    old = roaming() / "SaveTrail"
    old.mkdir(parents=True)
    (old / "gui.json").write_text('{"repo": "X:\\\\Library"}', encoding="utf-8")
    (old / "mirrors.json").write_text("{}", encoding="utf-8")
    (old / "scan-cache.json").write_text("{}", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in old.iterdir()}

    assert carry_over_settings() == ["gui.json", "mirrors.json"]
    new = config_dir()
    assert new == roaming() / "EngineSave"
    assert (new / "gui.json").read_bytes() == before["gui.json"]
    assert not (new / "scan-cache.json").exists(), "the cache is made anew"
    assert {p.name: p.read_bytes() for p in old.iterdir()} == before, "SaveTrail's are only read"

    (new / "gui.json").write_text("{}", encoding="utf-8")
    (old / "gui.json").write_text('{"repo": "Y:\\\\Other"}', encoding="utf-8")
    assert carry_over_settings() == []
    assert (new / "gui.json").read_text(encoding="utf-8") == "{}", "EngineSave's own wins"


def test_without_savetrail_nothing_is_made() -> None:
    assert carry_over_settings() == []
    assert not config_dir().exists()


def test_an_export_of_savetrail_imports(games_root: Path, saves_root: Path, tmp_path: Path) -> None:
    build_f1(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "repo", saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    (outcome,) = execute_backup(plan, repo).results
    archive = tmp_path / "new.zip"
    export_snapshot(
        repo, outcome.game_id, outcome.snapshot_id, archive, saves_root_override=saves_root
    )
    old = tmp_path / "SaveTrail-made.zip"
    with zipfile.ZipFile(archive) as zin, zipfile.ZipFile(old, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            name = LEGACY_EXPORT_JSON if info.filename == EXPORT_JSON else info.filename
            zout.writestr(name, zin.read(info))

    other = tmp_path / "other"
    result = execute_import(plan_import(other, old))
    assert (result.game_id, result.created_record) == (outcome.game_id, True)
    library = Repo.open(other)
    assert all(check.clean for check in verify_repo(library, library.load_catalog()))
