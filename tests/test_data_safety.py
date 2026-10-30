"""1.0.0 (D72): the data-safety fixes of the external review (R01-R10, R15), each turned from
its probe in ``Plan/reference/review_probes_2026-10-29.py`` into a regression: the wrong
operation is refused, the protected bytes stay as they were, the right one still works.

Everything is synthetic and lives below pytest's temporary folder; junctions are made there
and removed by the test itself. The real recycle bin is never called (conftest)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from enginesave.backup import execute_backup, prepare_backup
from enginesave.cli import main
from enginesave.repo import Repo
from fixtures.builder import build_f1, make_junction


def backed_up(tmp_path: Path):  # noqa: ANN201
    """F1 backed up once: ``(built, repo, game_id, games, saves)``."""
    games, saves = tmp_path / "games", tmp_path / "saves"
    games.mkdir()
    saves.mkdir()
    built = build_f1(games, saves)
    plan = prepare_backup([games], tmp_path / "repo", saves_root_override=saves)
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    game_id = repo.load_catalog()["games"][0]["game_id"]
    return built, repo, game_id, games, saves


def digest_tree(root: Path) -> dict[str, str]:
    """Every file below ``root`` by its SHA-256: the bytes a refused operation must not touch."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ------------------------------------------------------------------- R01, R15: the report


def backup_args(games: Path, repo: Path, saves: Path, report: Path, *extra: str) -> list[str]:
    return [
        "backup", str(games), "--dest", str(repo), "--renpy-saves-root", str(saves),
        "--no-cache", "--report", str(report), *extra,
    ]  # fmt: skip


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "backup"])
def test_r01_a_backup_report_inside_its_library_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture, dry_run: bool
) -> None:
    """D72 R01: ``--report <library>\\catalog.json`` used to replace the catalog with the
    report and exit 0; now exit 2 before anything runs, and the library is byte for byte the
    same."""
    _built, repo, game_id, games, saves = backed_up(tmp_path)
    before = digest_tree(repo.root)
    extra = ["--dry-run"] if dry_run else ["--yes"]
    for report in (repo.root / "catalog.json", repo.game_dir(game_id) / "report.json"):
        assert main(backup_args(games, repo.root, saves, report, *extra)) == 2
        assert "--report must not point inside the backup library" in capsys.readouterr().err
    assert digest_tree(repo.root) == before
    assert len(Repo.open(repo.root).snapshot_ids(game_id)) == 1  # nothing backed up either


def test_r01_a_scan_report_inside_a_library_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``scan --repo`` and a library the command was not given: both refused."""
    _built, repo, _game_id, games, saves = backed_up(tmp_path)
    before = digest_tree(repo.root)
    report = repo.root / "catalog.json"
    args = ["scan", str(games), "--renpy-saves-root", str(saves), "--no-cache"]
    assert main([*args, "--repo", str(repo.root), "--report", str(report)]) == 2
    assert "inside the backup library" in capsys.readouterr().err
    assert main([*args, "--report", str(report)]) == 2  # any library, given or not
    assert "inside an EngineSave library" in capsys.readouterr().err
    assert digest_tree(repo.root) == before


def test_r01_a_report_through_a_junction_to_the_library_is_refused(tmp_path: Path) -> None:
    """Another name for the library (a junction) is the library."""
    _built, repo, _game_id, games, saves = backed_up(tmp_path)
    alias = tmp_path / "alias"
    if not make_junction(alias, repo.root):
        pytest.skip("cannot create a junction here")
    try:
        before = digest_tree(repo.root)
        code = main(backup_args(games, alias, saves, alias / "catalog.json", "--dry-run"))
        assert code == 2
        code = main(backup_args(games, repo.root, saves, alias / "catalog.json", "--dry-run"))
        assert code == 2
        assert digest_tree(repo.root) == before
    finally:
        os.rmdir(alias)


def test_r01_a_report_outside_is_written_as_before(tmp_path: Path) -> None:
    _built, repo, _game_id, games, saves = backed_up(tmp_path)
    report = tmp_path / "reports" / "plan.json"
    assert main(backup_args(games, repo.root, saves, report, "--dry-run")) == 0
    assert json.loads(report.read_text(encoding="utf-8"))["dry_run"] is True
    scan_report = tmp_path / "reports" / "scan.json"
    args = ["scan", str(games), "--renpy-saves-root", str(saves), "--no-cache"]
    assert main([*args, "--repo", str(repo.root), "--report", str(scan_report)]) == 0
    assert json.loads(scan_report.read_text(encoding="utf-8"))["games"]


def test_r15_a_report_that_cannot_be_written_is_exit_code_3(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """D72 R15: an existing folder as ``--report`` raised OSError out of ``main``; now exit 3
    with no traceback -- and a backup that ran keeps its snapshot and says so."""
    built, repo, game_id, games, saves = backed_up(tmp_path)
    folder = tmp_path / "report-is-a-folder"
    folder.mkdir()
    assert main(backup_args(games, repo.root, saves, folder, "--dry-run")) == 3
    assert "the report could not be written" in capsys.readouterr().err
    args = ["scan", str(games), "--renpy-saves-root", str(saves), "--no-cache"]
    assert main([*args, "--report", str(folder)]) == 3
    changed = next(p for p in built.local_dir.rglob("*") if p.is_file())
    changed.write_bytes(changed.read_bytes() + b"more progress")
    assert main(backup_args(games, repo.root, saves, folder, "--yes")) == 3
    err = capsys.readouterr().err
    assert "the backup finished and its snapshots are kept" in err and "Traceback" not in err
    assert len(Repo.open(repo.root).snapshot_ids(game_id)) == 2


# ------------------------------------------------------------ R02: temporary files of our own


def stray_files(folder: Path, name: str) -> tuple[Path, Path, Path]:
    """At the old fixed temp name ``<name>.enginesave-tmp``: an unrelated file; beside it an
    unrelated file hard-linked at ``<name>.enginesave-tmp2`` style names is not needed -- the
    hard link sits at the fixed name of a second target."""
    folder.mkdir(parents=True, exist_ok=True)
    stray = folder / f"{name}.enginesave-tmp"
    stray.write_bytes(b"unrelated bytes the user keeps")
    victim = folder.parent / "unrelated.bin"
    victim.write_bytes(b"unrelated linked bytes")
    linked = folder / f"second-{name}.enginesave-tmp"
    os.link(victim, linked)
    return stray, victim, linked


@pytest.mark.parametrize("outcome", ["ok", "copy fails", "check fails"])
def test_r02_a_restore_never_touches_a_file_at_the_old_temp_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """D72 R02: ``<target>.enginesave-tmp`` was opened for writing and removed afterwards; a
    file the user had there (or a hard link to another file) is now left as it is."""
    from enginesave import restore

    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    data = b"snapshot bytes"
    (src / "a.save").write_bytes(data)
    stray, victim, linked = stray_files(dest, "a.save")
    item = {"path": "a.save", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if outcome == "copy fails":
        (src / "a.save").unlink()
        with pytest.raises(OSError):
            restore.restore_file(src, dest, item)
    elif outcome == "check fails":
        with pytest.raises(restore.RestoreError):
            restore.restore_file(src, dest, {**item, "sha256": "0" * 64})
    else:
        restore.restore_file(src, dest, item)
        assert (dest / "a.save").read_bytes() == data
    assert stray.read_bytes() == b"unrelated bytes the user keeps"
    assert victim.read_bytes() == b"unrelated linked bytes" and linked.exists()
    assert not [p for p in dest.iterdir() if p.name.startswith("a.save.") and p != stray]


@pytest.mark.parametrize("outcome", ["ok", "check fails"])
def test_r02_an_export_never_writes_through_the_old_temp_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """The probe linked ``export.zip.enginesave-tmp`` to an unrelated file: the export wrote
    the ZIP into it. Now the unrelated bytes stay, success or not."""
    from enginesave.errors import EngineSaveError
    from enginesave.export import archive

    _built, repo, game_id, _games, saves = backed_up(tmp_path)
    out = tmp_path / "exports"
    out.mkdir()
    victim = tmp_path / "unrelated.bin"
    victim.write_bytes(b"unrelated existing bytes")
    os.link(victim, out / "export.zip.enginesave-tmp")
    destination = out / "export.zip"
    snapshot = repo.latest_snapshot(game_id)
    if outcome == "check fails":

        def broken(*_args: object) -> None:
            raise archive.VerificationError("synthetic: the archive does not read back")

        monkeypatch.setattr(archive, "_verify_archive", broken)
        with pytest.raises(EngineSaveError):
            archive.export_snapshot(repo, game_id, snapshot, destination, saves_root_override=saves)
        assert not destination.exists()
    else:
        archive.export_snapshot(repo, game_id, snapshot, destination, saves_root_override=saves)
        assert destination.read_bytes().startswith(b"PK")
        assert not os.path.samefile(victim, destination)
    assert victim.read_bytes() == b"unrelated existing bytes"
    assert sorted(p.name for p in out.iterdir()) == sorted(
        ["export.zip.enginesave-tmp", *(["export.zip"] if outcome == "ok" else [])]
    )


# ------------------------------------------------- R03: listing errors, links inside a target


def junction_in_the_saves(tmp_path: Path):  # noqa: ANN201
    """The probe's setup: a snapshot with ``slot/a.save``, then the target's ``slot`` replaced
    by a junction to a folder outside holding newer progress. ``None`` without junctions."""
    import shutil

    built, repo, game_id, games, saves = backed_up(tmp_path)
    shutil.rmtree(built.local_dir)
    (built.local_dir / "slot").mkdir(parents=True)
    (built.local_dir / "slot" / "a.save").write_bytes(b"snapshot bytes")
    assert (
        execute_backup(
            prepare_backup([games], repo.root, saves_root_override=saves), repo
        ).exit_code
        == 0
    )
    manifest = repo.load_manifest(game_id, repo.latest_snapshot(game_id))
    source_id = next(s["source_id"] for s in manifest["sources"] if s["root_token"] == "GAME_ROOT")
    shutil.rmtree(built.local_dir / "slot")
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (outside / "a.save").write_bytes(b"new progress outside the target")
    link = built.local_dir / "slot"
    if not make_junction(link, outside):
        return None
    return repo, game_id, saves, source_id, link, outside


def test_r03_a_junction_inside_the_target_stops_the_restore_before_any_write(
    tmp_path: Path,
) -> None:
    """D72 R03: with only a junction in the target the safety snapshot had nothing to keep and
    the restore wrote through the junction; now it stops (exit 3) before the first write and
    the bytes behind the junction stay."""
    from enginesave.errors import FatalError
    from enginesave.restore import execute_restore, plan_restore

    found = junction_in_the_saves(tmp_path)
    if found is None:
        pytest.skip("cannot create a junction here")
    repo, game_id, saves, source_id, link, outside = found
    try:
        snapshots = repo.snapshot_ids(game_id)
        plan = plan_restore(
            repo, repo.load_catalog(), game_id, sources=[source_id], saves_root_override=saves
        )
        with pytest.raises(FatalError, match="cannot be listed whole"):
            execute_restore(plan)
        assert (outside / "a.save").read_bytes() == b"new progress outside the target"
        assert repo.snapshot_ids(game_id) == snapshots  # no safety snapshot either
        result = execute_restore(plan, safety_snapshot=False)  # the second guard
        assert result.written == 0
        assert result.failures and "link or junction" in result.failures[0]["error"]
        assert (outside / "a.save").read_bytes() == b"new progress outside the target"
    finally:
        os.rmdir(link)


def test_r03_a_listing_error_is_not_an_empty_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from enginesave import restore
    from enginesave.errors import FatalError

    target = SimpleNamespace(target_dir=tmp_path, source={})
    monkeypatch.setattr(restore, "source_files", lambda *_a: ([], ["synthetic: cannot list"]))
    with pytest.raises(FatalError, match="cannot be listed whole"):
        restore.take_safety_snapshot(SimpleNamespace(targets=[target]))


def test_r03_an_empty_target_folder_is_restored_as_before(tmp_path: Path) -> None:
    import shutil

    from enginesave.restore import execute_restore, plan_restore

    built, repo, game_id, _games, saves = backed_up(tmp_path)
    saved = digest_tree(built.local_dir)
    shutil.rmtree(built.local_dir)
    built.local_dir.mkdir()
    manifest = repo.load_manifest(game_id, repo.latest_snapshot(game_id))
    source_id = next(s["source_id"] for s in manifest["sources"] if s["root_token"] == "GAME_ROOT")
    plan = plan_restore(
        repo, repo.load_catalog(), game_id, sources=[source_id], saves_root_override=saves
    )
    result = execute_restore(plan)
    assert result.failures == [] and result.safety_snapshot is None
    assert digest_tree(built.local_dir) == saved


# ------------------------------------------------------ R04: a turned-off recycle bin, R05


def test_r04_recycle_limit_does_not_skip_the_turned_off_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D72 R04: with ``--recycle-limit`` the "recycle bin is turned off" check was never
    reached; now the plan refuses either way and the Shell's delete is never called."""
    from enginesave.cleanup import GAME_ROOT, RECYCLE, RecycleBin, plan, windows
    from enginesave.cleanup.execute import execute_cleanup
    from enginesave.errors import UsageError

    built, repo, _game_id, _games, saves = backed_up(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(windows, "shell_file_operation", lambda op: calls.append(op) or 0)
    monkeypatch.setattr(windows, "has_recycle_bin", lambda *_a: True)
    monkeypatch.setattr(windows, "drive_type", lambda *_a: plan.DRIVE_FIXED)
    monkeypatch.setattr(
        windows, "recycle_bin_info", lambda drive: RecycleBin(drive, 10**12, 0, disabled=True)
    )
    record = repo.load_catalog()["games"][0]
    before = digest_tree(built.root)
    for limit in (None, 10**12):
        checked = plan.plan_cleanup(
            repo, record, [(GAME_ROOT, str(built.root))], method=RECYCLE,
            recycle_limit=limit, saves_root_override=saves,
        )  # fmt: skip
        assert not checked.ok
        assert any("turned off" in r for r in checked.targets[0].reasons)
        with pytest.raises(UsageError):
            execute_cleanup(checked)
    assert calls == [] and digest_tree(built.root) == before


def orphan_backed_up(tmp_path: Path):  # noqa: ANN201
    """An orphaned global folder (F8's OrphanTwo) backed up as an unlinked record."""
    from fixtures.builder import build_f8

    games, saves = tmp_path / "games", tmp_path / "saves"
    games.mkdir()
    saves.mkdir()
    build_f8(saves)
    plan = prepare_backup(
        [games], tmp_path / "repo", saves_root_override=saves, include_unlinked=["OrphanTwo"]
    )
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    record = next(r for r in repo.load_catalog()["games"] if r["kind"] == "unlinked")
    return repo, record, games, saves


def install_a_user(games: Path, saves: Path) -> None:
    from fixtures.builder import build_game, simple_spec

    spec = simple_spec(
        "Newly Installed", "NewlyInstalled", "OrphanTwo", local_saves=0, global_saves=0
    )
    build_game(games, spec, saves)


@pytest.mark.parametrize("seen", ["in the scan", "in the library", "after the preview"])
def test_r05_an_orphaned_folder_a_game_uses_now_is_not_cleaned_up(
    tmp_path: Path, seen: str
) -> None:
    """D72 R05: the orphaned record's folder, used meanwhile by a newly installed game (seen
    by the scan, recorded in the library, or appearing between the preview and the
    cleanup), is refused like a shared global folder; nothing is deleted."""
    from enginesave.cleanup import PERMANENT, UNLINKED, plan_cleanup
    from enginesave.cleanup.execute import execute_cleanup
    from enginesave.scan import ScanOptions, run_scan

    repo, record, games, saves = orphan_backed_up(tmp_path)
    folder = saves / "OrphanTwo"
    before = digest_tree(folder)
    request = [(UNLINKED, "OrphanTwo")]
    if seen == "after the preview":
        checked = plan_cleanup(repo, record, request, method=PERMANENT, saves_root_override=saves)
        assert checked.ok  # nobody uses it yet
        install_a_user(games, saves)
        prepare = prepare_backup([games], repo.root, saves_root_override=saves)
        assert execute_backup(prepare, repo).exit_code == 0
        result = execute_cleanup(checked)
        assert [t["result"] for t in result.targets] == ["failed"]
        assert "still used by" in result.targets[0]["error"]
    else:
        install_a_user(games, saves)
        report = None
        if seen == "in the scan":
            report = run_scan(ScanOptions(roots=[games], saves_root_override=saves))
        else:
            prepare = prepare_backup([games], repo.root, saves_root_override=saves)
            assert execute_backup(prepare, repo).exit_code == 0
        checked = plan_cleanup(
            repo, record, request, method=PERMANENT, saves_root_override=saves, report=report
        )
        assert not checked.ok
        assert any("still used by" in r for r in checked.targets[0].reasons)
    assert digest_tree(folder) == before


def test_r05_a_truly_orphaned_folder_is_still_cleaned_up(tmp_path: Path) -> None:
    from enginesave.cleanup import PERMANENT, UNLINKED, plan_cleanup
    from enginesave.cleanup.execute import execute_cleanup

    repo, record, _games, saves = orphan_backed_up(tmp_path)
    checked = plan_cleanup(
        repo, record, [(UNLINKED, "OrphanTwo")], method=PERMANENT, saves_root_override=saves
    )
    assert checked.ok, checked.targets[0].reasons
    assert [t["result"] for t in execute_cleanup(checked).targets] == ["deleted"]
    assert not (saves / "OrphanTwo").exists()


# ------------------------------------------------- R06: a removal only of what was previewed


@pytest.mark.parametrize("whole", [True, False], ids=["whole record", "some snapshots"])
def test_r06_an_old_removal_plan_does_not_take_a_snapshot_made_since(
    tmp_path: Path, whole: bool
) -> None:
    """D72 R06: "remove A" (A then the record's only snapshot) planned, B backed up, the old
    plan executed: A, B and the record were gone. Now the plan is refused before anything
    is deleted, and both snapshots stay."""
    from enginesave.errors import UsageError
    from enginesave.removal import execute_remove, plan_remove

    built, repo, game_id, games, saves = backed_up(tmp_path)

    def another_snapshot() -> None:
        changed = next(p for p in built.local_dir.rglob("*") if p.is_file())
        changed.write_bytes(changed.read_bytes() + b"new progress")
        prepare = prepare_backup([games], repo.root, saves_root_override=saves)
        assert execute_backup(prepare, repo).exit_code == 0

    if not whole:
        another_snapshot()
    first = repo.snapshot_ids(game_id)[0]
    plan = plan_remove(repo, repo.load_catalog(), [(game_id, [first])])
    assert plan.items[0].whole is whole
    another_snapshot()
    snapshots = repo.snapshot_ids(game_id)
    before = digest_tree(repo.root / "objects")
    with pytest.raises(UsageError, match="changed since the preview"):
        execute_remove(plan)
    assert repo.snapshot_ids(game_id) == snapshots
    assert [r["game_id"] for r in repo.load_catalog()["games"]] == [game_id]
    assert digest_tree(repo.root / "objects") == before
    fresh = plan_remove(repo, repo.load_catalog(), [(game_id, [first])])  # previewed again
    assert execute_remove(fresh).snapshots == 1
    assert repo.snapshot_ids(game_id) == snapshots[1:]


# ------------------------------------------------------------- R07: links deep in a mirror


@pytest.mark.parametrize("where", ["bucket", "objects tmp", "snapshot"])
def test_r07_a_junction_deep_in_the_mirror_is_refused_before_writing(
    tmp_path: Path, where: str
) -> None:
    r"""D72 R07: a mirror's ``objects\sha256\<bucket>`` replaced by a junction into the
    library was accepted (the library's objects became the mirror's). Now that, a junction at
    ``objects\.tmp`` and one in place of a snapshot folder are refused before anything is
    written; the library and the folder behind the junction stay as they were."""
    import shutil

    from enginesave.errors import UsageError
    from enginesave.mirror import check_target, mirror_repo

    _built, repo, game_id, _games, _saves = backed_up(tmp_path)
    target = tmp_path / "mirror"
    assert mirror_repo(repo, target).exit_code == 0
    digest, source = next(iter(repo.iter_objects()))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_bytes(b"outside bytes")
    if where == "bucket":
        link, to = target / "objects" / "sha256" / digest[:2], source.parent
    elif where == "objects tmp":
        link, to = target / "objects" / ".tmp", elsewhere
    else:
        mirrored = Repo.open(target)
        link = mirrored.snapshot_dir(game_id, mirrored.snapshot_ids(game_id)[0])
        to = elsewhere
    if link.exists():
        shutil.rmtree(link)
    if not make_junction(link, to):
        pytest.skip("cannot create a junction here")
    try:
        library_before, outside_before = digest_tree(repo.root), digest_tree(elsewhere)
        with pytest.raises(UsageError, match="links or junctions"):
            check_target(repo, target)
        with pytest.raises(UsageError, match="links or junctions"):
            mirror_repo(repo, target, repair=True)
        assert digest_tree(repo.root) == library_before
        assert digest_tree(elsewhere) == outside_before
    finally:
        os.rmdir(link)


def test_r07_a_normal_mirror_keeps_copies_of_its_own(tmp_path: Path) -> None:
    from enginesave.mirror import mirror_repo

    _built, repo, _game_id, _games, _saves = backed_up(tmp_path)
    target = tmp_path / "mirror"
    assert mirror_repo(repo, target).exit_code == 0
    assert mirror_repo(repo, target).exit_code == 0  # again: accepted as its mirror
    mirrored = Repo.open(target)
    for digest, source in repo.iter_objects():
        copy = mirrored.object_path(digest)
        assert copy.read_bytes() == source.read_bytes()
        assert not os.path.samefile(copy, source)


# ------------------------------------------------- R08, R10: source ids from an archive


def synthetic_archive(path: Path, ids: list[object]) -> Path:
    """An archive in the old export format whose manifest names these source ids (the
    probe's: two sources at the same place, so the second one's folder is the id itself)."""
    import zipfile

    from enginesave.export.archive import EXPORT_JSON

    source = {
        "kind": "local", "root_token": "GAME_ROOT", "relative_root": "game/saves",
        "origin_path": "", "file_count": 0, "total_bytes": 0, "consistency": "stable",
    }  # fmt: skip
    header = {
        "export_format_version": 1,
        "snapshot_id": "20261029T120000Z",
        "game": {"display_name": "synthetic"},
        "manifest": {
            "format_version": 1,
            "status": "complete",
            "sources": [{**source, "source_id": sid} for sid in ids],
            "files": [],
            "errors": [],
        },
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(EXPORT_JSON, json.dumps(header))
    return path


BAD_IDS = ["*", "a?b", "CON", "aux.txt", "name.", "", ["first"], {"id": 1}, 7]


@pytest.mark.parametrize("bad", BAD_IDS, ids=[repr(b) for b in BAD_IDS])
def test_r08_r10_a_bad_source_id_in_an_archive_is_refused_when_it_is_read(
    tmp_path: Path, bad: object
) -> None:
    """D72 R08: ``*`` passed the old check and then numbered folder names forever; R10: an
    array id raised a bare TypeError. Both are refused when the archive is read (exit 3, a
    controlled error), before anything is written to the library."""
    from enginesave.errors import FatalError
    from enginesave.export.importing import plan_import, read_export

    archive = synthetic_archive(tmp_path / "bad.zip", ["first", bad])
    with pytest.raises(FatalError, match="source id|duplicate"):
        read_export(archive)
    _built, repo, _game_id, _games, _saves = backed_up(tmp_path)
    before = digest_tree(repo.root)
    with pytest.raises(FatalError):
        plan_import(repo.root, archive)
    assert digest_tree(repo.root) == before


def test_r08_the_layout_fails_at_once_on_a_name_that_never_fits() -> None:
    """The layout itself: an id that is no folder name is an error at once, not a loop."""
    from enginesave import layout
    from enginesave.fsutil import UnsafePathError

    source = {"source_id": "*", "root_token": "GAME_ROOT", "relative_root": "game/saves"}
    with pytest.raises(UnsafePathError):
        layout.data_dir_for(source, ["data/GAME_ROOT/game/saves"])


def test_r08_names_that_are_only_taken_are_still_numbered() -> None:
    from enginesave import layout

    sources = [
        {"source_id": sid, "root_token": "GAME_ROOT", "relative_root": "game/saves"}
        for sid in ("save", "Save", "SAVE")
    ]
    dirs = [s["data_dir"] for s in layout.readable_sources(sources)]
    assert dirs[0] == "data/GAME_ROOT/game/saves"
    assert len({d.casefold() for d in dirs}) == 3, dirs


def test_r10_a_bad_archive_does_not_stop_the_good_one_after_it(tmp_path: Path) -> None:
    """A batch: the broken archive is reported, the next one is previewed and imported."""
    from enginesave.export.archive import export_snapshot
    from enginesave.export.batch import import_archives, preview_imports

    _built, repo, game_id, _games, saves = backed_up(tmp_path)
    folder = tmp_path / "zips"
    folder.mkdir()
    synthetic_archive(folder / "a-broken.zip", ["first", ["not", "a", "name"]])
    export_snapshot(
        repo, game_id, repo.latest_snapshot(game_id), folder / "b-good.zip",
        saves_root_override=saves,
    )  # fmt: skip
    archives = sorted(folder.glob("*.zip"))
    other = Repo.open_or_create(tmp_path / "other")
    previews = preview_imports(other.root, archives)
    assert previews[0][1] is None and "source id" in str(previews[0][2])
    assert previews[1][1] is not None and previews[1][2] is None
    batch = import_archives(other.root, archives)
    assert [item["result"] for item in batch.items] == ["failed", "imported"]
    assert len(Repo.open(other.root).load_catalog()["games"]) == 1


# ------------------------------------------------- R09: allocations in a restricted pickle


class Allocates:
    """Pickles as ``bytes(size)`` (or ``bytearray(size)``): what a hostile script could ask."""

    def __init__(self, size: int, kind: type = bytes) -> None:
        self.size, self.kind = size, kind

    def __reduce__(self) -> tuple[object, ...]:
        return (self.kind, (self.size,))


def hostile_pickle(size: int, kind: type = bytes) -> bytes:
    import pickle

    # "save_directory" so that the RPYC prefilter reads it (D72 R09: the filtered entry too)
    return pickle.dumps([Allocates(size, kind), "save_directory"], protocol=2)


@pytest.mark.parametrize("kind", [bytes, bytearray])
def test_r09_an_allocation_over_the_budget_is_refused_before_it_is_made(kind: type) -> None:
    """D72 R09: ``bytes(N)`` was a real constructor: a 30-byte pickle could ask for any N.
    Now N is checked against the budget first -- 2**50 bytes is refused at once (no
    MemoryError, no time spent), and a small one within the budget is made."""
    import pickle

    from enginesave.engines.renpy.rpyc_reader import restricted_loads

    with pytest.raises(pickle.UnpicklingError, match="refused an allocation"):
        restricted_loads(hostile_pickle(2**50, kind))
    assert len(restricted_loads(hostile_pickle(1024, kind))[0]) == 1024
    with pytest.raises(pickle.UnpicklingError):
        restricted_loads(hostile_pickle(1024, kind), budget=512)


def test_r09_the_budget_holds_for_rpa_and_rpyc_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both entries: an RPA index and an RPYC script whose pickle asks for more than a small
    test budget are unreadable (as any malformed file), the budget's own files still read."""
    import zlib

    from enginesave.engines.renpy import rpa_reader, rpyc_reader
    from fixtures.builder import options_stmts, rpa_bytes, rpyc_bytes, rpyc_container

    monkeypatch.setattr(rpyc_reader, "MAX_ALLOCATED", 4096)
    script = tmp_path / "options.rpyc"
    script.write_bytes(rpyc_container(zlib.compress(hostile_pickle(8192))))
    with pytest.raises(rpyc_reader.UnsupportedRpyc, match="refused an allocation"):
        rpyc_reader.read_rpyc(script.read_bytes())
    archive = tmp_path / "archive.rpa"
    good = rpa_bytes({"a.txt": b"x"})
    head, _, _ = good.partition(b"\n")
    offset = int(head.split()[1], 16)
    archive.write_bytes(good[:offset] + zlib.compress(hostile_pickle(8192)))
    with pytest.raises(rpa_reader.UnsupportedRpa, match="refused an allocation"):
        rpa_reader.read_index(archive)
    fine = tmp_path / "fine.rpyc"  # a real (fixture) script and archive still read
    fine.write_bytes(rpyc_bytes(options_stmts(name="Fixture"), "game/options.rpy"))
    assert rpyc_reader.read_rpyc(fine.read_bytes())
    (tmp_path / "fine.rpa").write_bytes(good)
    assert [e.name for e in rpa_reader.read_index(tmp_path / "fine.rpa")] == ["a.txt"]


# ------------------------------------------- R11, R13, R16: damaged input stays its own


@pytest.mark.parametrize("value", [1e400, -1, 1.5, True, None, [3], "12x"])
def test_r11_a_damaged_asar_index_is_a_damaged_archive(tmp_path: Path, value: object) -> None:
    """D72 R11: ``"offset": 1e400`` raised OverflowError out of the scan; any value that is
    not a whole non-negative number now makes that file unreadable, nothing more."""
    from enginesave.engines.electron import Asar, asar_head, asar_read

    asar = Asar(tmp_path / "unused.asar", 0, 10, {"package.json": {"offset": value, "size": 1}})
    assert asar_read(asar, "package.json", 1024) is None
    assert asar_head(asar, "package.json", 1024) is None
    sized = Asar(tmp_path / "unused.asar", 0, 10, {"package.json": {"offset": "0", "size": value}})
    assert asar_read(sized, "package.json", 1024) is None


def test_r11_a_damaged_electron_game_does_not_stop_the_scan(tmp_path: Path) -> None:
    """An app.asar whose index says ``"offset": Infinity`` (``1e400``): that app is
    unreadable, the scan goes on and the other app is found."""
    import struct

    from enginesave.scan import ScanOptions, run_scan
    from fixtures.engines import build_electron

    games = tmp_path / "games"
    games.mkdir()
    build_electron(games, "Good App", product="Good App")
    bad = build_electron(games, "Bad App", product="Bad App")
    package = json.dumps({"name": "bad-app", "productName": "Bad App"}).encode()
    text = json.dumps({"files": {"package.json": {"size": len(package), "offset": 1e400}}})
    raw = text.encode()
    pad = (4 - len(raw) % 4) % 4
    payload = 4 + len(raw) + pad
    head = struct.pack("<IIII", 4, 4 + payload, payload, len(raw))
    (bad.root / "resources" / "app.asar").write_bytes(head + raw + b"\0" * pad + package)
    report = run_scan(ScanOptions(roots=[games]))
    names = {Path(g.game_root).name: g for g in report.games}
    assert names["Good App"].status.value == "CONFIRMED"


def test_r11_a_damaged_save_zip_is_skipped_for_the_version_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``zlib.error`` while reading a save's ``json`` member: that save is skipped (the probe
    raised it out of the orphaned-folder scan)."""
    import zipfile
    import zlib
    from types import SimpleNamespace

    from enginesave.saves_root import version_hint

    damaged, good = tmp_path / "1-1-LT1.save", tmp_path / "1-2-LT1.save"
    for path, version in ((damaged, "9"), (good, "2.0")):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("json", json.dumps({"_version": version}))
    real_read = zipfile.ZipFile.read

    def read(self: zipfile.ZipFile, name: object, *args: object) -> bytes:
        if Path(str(self.filename)).name == damaged.name:
            raise zlib.error("synthetic damaged stream")
        return real_read(self, name, *args)

    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    entries = [
        SimpleNamespace(path=damaged, rel=damaged.name, mtime_ns=2),
        SimpleNamespace(path=good, rel=good.name, mtime_ns=1),
    ]
    assert version_hint(entries) == "2.0"


def test_r13_a_snapshot_folder_that_cannot_be_listed_is_not_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D72 R13: the errors of listing the snapshot's files (to find extra ones) were thrown
    away and the snapshot called clean; now they are its problems."""
    from enginesave import verify

    _built, repo, game_id, _games, _saves = backed_up(tmp_path)
    snapshot = repo.latest_snapshot(game_id)
    assert verify.verify_snapshot(repo, game_id, snapshot).clean
    real = verify.walk_files

    def partial(path: Path):  # noqa: ANN202
        found, errors = real(path)
        return found, [*errors, "synthetic: a folder that cannot be listed"]

    monkeypatch.setattr(verify, "walk_files", partial)
    checked = verify.verify_snapshot(repo, game_id, snapshot)
    assert checked.ok > 0 and not checked.clean
    assert any("cannot list" in p for p in checked.problems)


def test_r16_a_link_inside_the_electron_app_is_not_read(tmp_path: Path) -> None:
    """D72 R16: ``resources\\app`` (or a folder below it) as a junction to another app made
    that app's package.json and RPG Maker files this game's. Now nothing behind a link inside
    the app is read; the game folder the user gave is still taken as it is."""
    import shutil

    from enginesave.engines.electron import AppFiles
    from fixtures.engines import build_electron

    games = tmp_path / "games"
    games.mkdir()
    built = build_electron(games, "Linked App", product="Linked App", packed=False)
    elsewhere = build_electron(
        tmp_path / "elsewhere", "Other App", product="Other App", packed=False,
        extra={"www/js/rpg_core.js": b"//"},
    )  # fmt: skip
    app = built.root / "resources" / "app"
    shutil.rmtree(app)
    if not make_junction(app, elsewhere.root / "resources" / "app"):
        pytest.skip("cannot create a junction here")
    try:
        files = AppFiles(built.root / "resources")
        assert files.folders == [] and files.package() == (None, "resources/app.asar!package.json")
        assert not files.has("www/js/rpg_core.js")
    finally:
        os.rmdir(app)
    deeper = build_electron(games, "Deep App", product="Deep App", packed=False)
    www = deeper.root / "resources" / "app" / "www"
    if not make_junction(www, elsewhere.root / "resources" / "app" / "www"):
        pytest.skip("cannot create a junction here")
    try:
        files = AppFiles(deeper.root / "resources")
        assert files.package()[0] is not None  # its own package.json is read
        assert not files.has("www/js/rpg_core.js")  # not through the junction below it
    finally:
        os.rmdir(www)
