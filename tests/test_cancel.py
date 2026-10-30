"""Stopping a running operation (0.9.0, D58): ``should_stop`` is asked only at safe points,
and stopping at the Nth of them leaves what was done whole, nothing half-done (no temporary
folder, no half snapshot, no half archive) and what was committed before untouched. The
command line stops on Ctrl+C the same way and exits with 130."""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from enginesave import store
from enginesave.backup import execute_backup, prepare_backup
from enginesave.cleanup import GAME_ROOT, GLOBAL, PERMANENT, execute_cleanup, plan_cleanup
from enginesave.cli import main
from enginesave.errors import STOPPED_EXIT_CODE, Stopped
from enginesave.export import export_records, import_archives, zip_archives
from enginesave.export.archive import TMP_SUFFIX
from enginesave.removal import execute_remove, plan_remove
from enginesave.repo import Repo
from enginesave.scan import ScanOptions, run_scan
from enginesave.scan_cache import ScanCache
from enginesave.verify import verify_objects, verify_repo
from fixtures.builder import build_f1, build_game, simple_spec


class Stopper:
    """``should_stop`` that says yes from its ``at``-th question on, counting them."""

    def __init__(self, at: int | None = None) -> None:
        self.at, self.asked = at, 0

    def __call__(self) -> bool:
        self.asked += 1
        return self.at is not None and self.asked > self.at


@pytest.fixture
def two_games(games_root: Path, saves_root: Path) -> Path:
    build_f1(games_root, saves_root)
    build_game(games_root, simple_spec("Second", "Second", "Second-1600000002"), saves_root)
    return games_root


def intact(repo: Repo) -> bool:
    """No leftovers, every snapshot whole, every record's count right."""
    catalog = repo.load_catalog()
    checks = verify_repo(repo, catalog)
    counts = all(
        r.get("snapshot_count", len(repo.snapshot_ids(r["game_id"])))
        == len(repo.snapshot_ids(r["game_id"]))
        for r in catalog["games"]
    )
    empty = repo.orphan_game_dirs() == []  # a stopped first snapshot leaves no record folder
    return repo.stale_tmp_dirs() == [] and all(c.clean for c in checks) and counts and empty


def test_a_backup_stopped_at_any_safe_point(
    two_games: Path, saves_root: Path, tmp_path: Path
) -> None:
    """Every safe point of a two-game backup in turn: the games done have their snapshot,
    the one being copied has none (and no record or temporary folder), the rest none."""
    counting = Stopper()
    plan = prepare_backup([two_games], tmp_path / "count", saves_root_override=saves_root)
    execute_backup(plan, Repo.open_or_create(plan.dest), should_stop=counting)
    assert counting.asked > 4, "a question per game and per file"
    for at in range(counting.asked):
        dest = tmp_path / f"at{at}"
        plan = prepare_backup([two_games], dest, saves_root_override=saves_root)
        repo = Repo.open_or_create(plan.dest)
        result = execute_backup(plan, repo, should_stop=Stopper(at))
        assert result.cancelled and result.to_dict()["cancelled"] is True
        assert all(r.status == "ok" and r.snapshot_id for r in result.results)
        snapshots = [
            s for r in repo.load_catalog()["games"] for s in repo.snapshot_ids(r["game_id"])
        ]
        assert len(snapshots) == len(result.results) < len(plan.games)
        assert intact(repo), at
    done = execute_backup(plan, repo)  # the same plan again finishes the job
    assert not done.cancelled and intact(repo)


def test_a_scan_stops_between_folders_and_keeps_the_cache(
    two_games: Path, saves_root: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "scan-cache.json"
    options = ScanOptions(
        roots=[two_games],
        saves_root_override=saves_root,
        cache=ScanCache.load(cache),
        should_stop=Stopper(2),
    )
    with pytest.raises(Stopped):
        run_scan(options)
    assert cache.is_file(), "what was parsed is kept"


def test_verification_stops_between_snapshots(
    two_games: Path, saves_root: Path, tmp_path: Path
) -> None:
    plan = prepare_backup([two_games], tmp_path / "repo", saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    execute_backup(plan, repo)
    with pytest.raises(Stopped) as stopped:
        verify_repo(repo, repo.load_catalog(), should_stop=Stopper(1))
    assert len(stopped.value.partial) == 1 and stopped.value.partial[0].clean


def test_export_and_import_stop_between_archives(
    two_games: Path, saves_root: Path, tmp_path: Path
) -> None:
    """The archive being written is finished first: one whole zip, then nothing more; the
    import of it after the first archive leaves one whole snapshot."""
    plan = prepare_backup([two_games], tmp_path / "repo", saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    execute_backup(plan, repo)
    ids = [r["game_id"] for r in repo.load_catalog()["games"]]
    folder = tmp_path / "exports"
    batch = export_records(repo, ids, folder, should_stop=Stopper(1))
    assert batch.cancelled and [i["result"] for i in batch.items] == ["exported"]
    assert len(zip_archives(folder)) == 1 and not any(folder.glob(f"*{TMP_SUFFIX}*"))
    full = export_records(repo, ids, tmp_path / "all")
    target = tmp_path / "imported"
    batch = import_archives(target, zip_archives(tmp_path / "all"), should_stop=Stopper(1))
    assert batch.cancelled and [i["result"] for i in batch.items] == ["imported"]
    assert intact(Repo.open(target)) and len(full.items) == 2


def test_a_cleanup_stops_between_folders(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """Never in the middle of a folder: the first folder is deleted and recorded, the
    second one is untouched."""
    built = build_f1(games_root, saves_root)
    plan = prepare_backup([games_root], tmp_path / "repo", saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    execute_backup(plan, repo)
    (record,) = repo.load_catalog()["games"]
    check = plan_cleanup(
        repo,
        record,
        [(GAME_ROOT, None), (GLOBAL, None)],
        saves_root_override=saves_root,
        method=PERMANENT,
    )
    result = execute_cleanup(check, should_stop=Stopper(1))
    assert result.cancelled and [t["result"] for t in result.targets] == ["deleted"]
    assert not built.root.exists() and built.global_dir.is_dir()
    assert any(built.global_dir.iterdir()), "the global folder is untouched"
    assert repo.load_game(record["game_id"])["cleanups"][0]["targets"][0]["result"] == "deleted"


def test_a_cleanup_batch_stopped_between_records_says_so(
    two_games: Path, saves_root: Path, tmp_path: Path
) -> None:
    """The window's batch: stopped after the first record's folder, the second record is not
    reached; the result says 已取消 (in the dialog and the log), and the second game folder is
    untouched. 「继续清理」 stopped at once keeps everything that waited."""
    from enginesave.cleanup import batch
    from enginesave.gui import state

    plan = prepare_backup([two_games], tmp_path / "repo", saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    execute_backup(plan, repo)
    records = repo.load_catalog()["games"]
    groups = [(r["game_id"], None, [(GAME_ROOT, None)]) for r in records]
    roots = {
        r["game_id"]: repo.load_game(r["game_id"])["installs"][0]["game_root"] for r in records
    }
    options = {"saves_root_override": saves_root}
    outcomes = batch.run_batch(
        repo, groups, method=PERMANENT, options=options, should_stop=Stopper(2)
    )
    assert outcomes.cancelled and batch.cancelled(outcomes) and len(outcomes) == 1
    (plan_done, done), second = outcomes[0], records[1]["game_id"]
    assert done is not None and not done.cancelled and done.targets[0]["result"] == "deleted"
    assert Path(roots[second]).is_dir(), "the record not reached is untouched"
    assert state.cleanup_batch_summary(outcomes, PERMANENT).startswith("已取消：已永久删除 1 项")
    assert state.cleanup_done_log(outcomes).startswith("已取消：")
    assert not batch.cancelled([(plan_done, done)]), "a plain list never stopped"
    waiting = [
        batch.Waiting(second, "Second", None, GAME_ROOT, roots[second], 1, (GAME_ROOT, None))
    ]
    resumed = batch.resume(repo, waiting, options=options, should_stop=lambda: True)
    assert resumed.cancelled and list(resumed) == []
    assert batch.still_waiting(waiting, resumed) == waiting


def test_a_removal_stops_between_snapshots_and_collects_nothing(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    built = build_f1(games_root, saves_root)
    repo = Repo.open_or_create(tmp_path / "repo")
    for n in range(3):
        save = next(built.local_dir.glob("*.save"))
        save.write_bytes(save.read_bytes() + bytes([n]))
        plan = prepare_backup([games_root], repo.root, saves_root_override=saves_root)
        execute_backup(plan, repo)
    (record,) = repo.load_catalog()["games"]
    first, second, _third = repo.snapshot_ids(record["game_id"])
    objects = sum(1 for _ in repo.iter_objects())
    plan = plan_remove(repo, repo.load_catalog(), [(record["game_id"], [first, second])])
    result = execute_remove(plan, should_stop=Stopper(2))
    assert result.cancelled and result.snapshots == 1 and result.garbage.skipped
    assert repo.snapshot_ids(record["game_id"]) == [second, _third]
    assert sum(1 for _ in repo.iter_objects()) == objects, "the object store is not collected"
    assert intact(repo) and verify_objects(repo).clean


def test_ctrl_c_stops_a_backup_at_a_safe_point(
    two_games: Path, saves_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The first Ctrl+C is a request: the command finishes the file it copies, drops the
    unfinished snapshot and exits 130, its JSON saying ``cancelled``."""
    real = store.copy_and_verify
    calls = {"n": 0}

    def copy(*args: object) -> tuple[str, int, str]:
        calls["n"] += 1
        if calls["n"] == 2:
            signal.raise_signal(signal.SIGINT)  # pressed while the second file is copied
        return real(*args)

    monkeypatch.setattr(store, "copy_and_verify", copy)
    dest = tmp_path / "repo"
    args = ["backup", str(two_games), "--dest", str(dest), "--yes", "--json"]
    code = main([*args, "--renpy-saves-root", str(saves_root)])
    out, err = capsys.readouterr()
    assert code == STOPPED_EXIT_CODE and "stopping at the next safe point" in err
    assert json.loads(out)["result"]["cancelled"] is True
    assert intact(Repo.open(dest)) and calls["n"] == 2
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, "handler restored"


def test_ctrl_c_during_one_archive_finishes_it_then_exits_130(
    two_games: Path, saves_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """D62 ①: a single ``export`` or ``import`` is its own safe point -- pressed while the
    archive is written or read, it is finished, the stop is said and the exit code is 130."""
    from enginesave.cli import transfer

    plan = prepare_backup([two_games], tmp_path / "repo", saves_root_override=saves_root)
    repo = Repo.open_or_create(plan.dest)
    execute_backup(plan, repo)
    game_id = repo.load_catalog()["games"][0]["game_id"]
    for name in ("export_snapshot", "execute_import"):
        real = getattr(transfer, name)

        def pressed(*args: object, real=real, **kwargs: object) -> object:
            signal.raise_signal(signal.SIGINT)
            return real(*args, **kwargs)

        monkeypatch.setattr(transfer, name, pressed)
    archive = tmp_path / "one.zip"
    code = main(["export", "--repo", str(repo.root), "--game", game_id, "--to", str(archive)])
    out, err = capsys.readouterr()
    assert code == STOPPED_EXIT_CODE and "stopped at a safe point" in out
    assert zip_archives(tmp_path) == [archive], "the archive is whole"
    target = tmp_path / "imported"
    code = main(["import", "--repo", str(target), "--from", str(archive), "--yes", "--json"])
    out, err = capsys.readouterr()
    assert code == STOPPED_EXIT_CODE and json.loads(out)["result"] is not None
    assert intact(Repo.open(target)) and len(Repo.open(target).load_catalog()["games"]) == 1
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_an_interrupt_without_safe_points_exits_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    from enginesave.cli import parser

    def interrupted(_args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(parser, "load_handler", lambda _command: interrupted)
    assert main(["list", "--repo", str(tmp_path)]) == STOPPED_EXIT_CODE
    assert "interrupted" in capsys.readouterr().err
