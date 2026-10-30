"""M4 contract: a new engine detector needs no change in backup or restore (plan 1.2.7)."""

from __future__ import annotations

import ast
import hashlib
import shutil
from pathlib import Path

import enginesave.backup
import enginesave.export
import enginesave.restore
from enginesave.backup import execute_backup, prepare_backup
from enginesave.engines import default_detectors
from enginesave.engines.base import DetectionResult, Detector, ScanContext, game_status
from enginesave.fsutil import dir_stats
from enginesave.models import (
    DetectedGame,
    Evidence,
    GameIdentity,
    GameStatus,
    MetadataState,
    RootToken,
    SaveSource,
    SourceKind,
    SourceStatus,
)
from enginesave.repo import Repo
from enginesave.restore import execute_restore, plan_restore
from enginesave.scan import ScanOptions, run_scan
from fixtures.builder import build_f1, write_text


class FakeEngineDetector(Detector):
    """A made-up engine: a ``fake.engine`` marker and saves under ``userdata/``."""

    engine = "fake"

    def detect(self, directory: Path, ctx: ScanContext) -> DetectionResult | None:
        if not (directory / "fake.engine").is_file():
            return None
        saves = directory / "userdata"
        exists, count, size = dir_stats(saves)
        source = SaveSource(
            "local",
            SourceKind.LOCAL,
            str(saves),
            RootToken.GAME_ROOT,
            "userdata",
            SourceStatus.CONFIRMED if count else SourceStatus.EXPECTED_ABSENT,
            "fake:userdata",
            [Evidence("fake.engine", "engine_marker", "fake engine marker")],
        )
        source.exists, source.file_count, source.total_bytes = exists, count, size
        game = DetectedGame(
            game_root=str(directory),
            engine=self.engine,
            engine_version="1.0",
            display_name="Fake Quest",
            identity=GameIdentity(None, "Fake Quest", None, "1.0", None, directory.name),
            metadata_state=MetadataState.RESOLVED,
            status=GameStatus.NOT_FOUND,
            sources=[source],
            installs=[
                {"game_root": str(directory), "config_version": "1.0", "engine_version": "1.0"}
            ],
        )
        game.status = game_status(game)
        return DetectionResult(game=game)


def make_fake_game(parent: Path) -> dict[str, bytes]:
    root = parent / "Fake Quest"
    write_text(root / "fake.engine", "marker\n")
    files = {"slot1.dat": b"fake save one", "nested/slot2.dat": b"fake save two"}
    for rel, data in files.items():
        path = root / "userdata" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return files


def test_new_detector_round_trips_without_backup_or_restore_changes(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    files = make_fake_game(games_root)
    build_f1(games_root, saves_root)
    detectors = [FakeEngineDetector(), *default_detectors()]
    report = run_scan(
        ScanOptions(roots=[games_root], saves_root_override=saves_root, detectors=detectors)
    )
    assert sorted((g.engine, g.status.value) for g in report.games) == [
        ("fake", "CONFIRMED"),
        ("renpy", "CONFIRMED"),
    ]

    dest = tmp_path / "repo"
    plan = prepare_backup([games_root], dest, saves_root_override=saves_root, detectors=detectors)
    repo = Repo.open_or_create(plan.dest)
    result = execute_backup(plan, repo)
    assert [r.status for r in result.results] == ["ok", "ok"]

    fake_root = games_root / "Fake Quest"
    shutil.rmtree(fake_root / "userdata")
    restore_plan = plan_restore(
        repo, repo.load_catalog(), "Fake Quest", saves_root_override=saves_root
    )
    assert [t.target_dir for t in restore_plan.targets] == [fake_root / "userdata"]
    assert execute_restore(restore_plan).written == len(files)
    for rel, data in files.items():
        restored = (fake_root / "userdata" / rel).read_bytes()
        assert hashlib.sha256(restored).digest() == hashlib.sha256(data).digest()

    again = prepare_backup([games_root], dest, saves_root_override=saves_root, detectors=detectors)
    fake = next(g for g in again.games if g.display_name == "Fake Quest")
    assert fake.game_id == restore_plan.game_id, "same install root links to the same record"


def test_backup_and_restore_do_not_import_detectors() -> None:
    forbidden = {"enginesave.engines.renpy.detector", "enginesave.engines.fingerprints"}
    for module in (enginesave.backup, enginesave.restore, enginesave.export):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not imported & forbidden, module.__name__
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert "RenPyDetector" not in text and "FingerprintDetector" not in text
