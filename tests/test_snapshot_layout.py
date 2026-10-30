"""0.11.0 (D67 3-7, M46): a snapshot's files where they belong (manifest format 3), its
README.txt, the library's INDEX.txt, the export zip in the same layout, and a library of
0.10.0 migrated with READMEs written into its old snapshots."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from enginesave import layout
from enginesave.backup import execute_backup, prepare_backup
from enginesave.export import export_snapshot, plan_import
from enginesave.export.importing import execute_import
from enginesave.migrate import execute_migration
from enginesave.mirror import mirror_repo
from enginesave.repo import Repo
from enginesave.verify import verify_objects, verify_repo
from fixtures.builder import (
    Define,
    build_f1,
    build_f4,
    build_game,
    save_set,
    simple_spec,
    stored,
    write_files,
)

FIXTURE = Path(__file__).parent / "fixtures" / "library_v1.zip"


def back_up(games_root: Path, saves_root: Path, library: Path, **options: object) -> Repo:
    plan = prepare_backup([games_root], library, saves_root_override=saves_root, **options)
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    return Repo.open(plan.dest)


def tree(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def clean(repo: Repo) -> bool:
    checks = verify_repo(repo, repo.load_catalog())
    return bool(checks) and all(c.clean for c in checks) and verify_objects(repo).clean


def only_snapshot(repo: Repo, name: str) -> tuple[str, str]:
    record = next(r for r in repo.load_catalog()["games"] if r["display_name"] == name)
    return record["game_id"], repo.snapshot_ids(record["game_id"])[-1]


# ------------------------------------------------------------------------- the snapshot


def test_a_new_snapshot_keeps_its_files_where_they_belong_with_a_readme(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D67 3, 4: ``data\\GAME_ROOT\\game\\saves`` and ``data\\RENPY_SAVES_ROOT\\<directory>``;
    the README says where each goes on this computer and lists every file; it is not an
    extra file to the check."""
    built = build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    game_id, snapshot_id = only_snapshot(repo, "Readable Game")
    snapshot = repo.snapshot_dir(game_id, snapshot_id)
    manifest = repo.load_manifest(game_id, snapshot_id)
    assert manifest["format_version"] == 3
    dirs = {s["source_id"]: s["data_dir"] for s in manifest["sources"]}
    assert dirs == {
        "local": "data/GAME_ROOT/game/saves",
        "global": "data/RENPY_SAVES_ROOT/ReadableGame-1600000001",
    }
    files = tree(snapshot)
    assert {"manifest.json", "README.txt"} <= set(files)
    assert {f"data/GAME_ROOT/game/saves/{n}" for n in built.local_files} <= set(files)
    assert all(f in ("manifest.json", "README.txt") or f.startswith("data/") for f in files)
    for item in manifest["files"]:  # the real files: the same bytes as backed up
        path = layout.snapshot_file_path(snapshot, manifest, item)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
    raw = (snapshot / "README.txt").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "UTF-8 with a BOM"
    readme = raw.decode("utf-8-sig")
    assert readme.index("EngineSave 快照说明") < readme.index("EngineSave snapshot")
    assert "游戏：Readable Game" in readme and f"记录 ID：{game_id}" in readme
    assert f"快照：{snapshot_id}（手动备份，完整）" in readme
    assert "data\\GAME_ROOT\\game\\saves\\  →  <游戏目录>\\game\\saves" in readme
    global_folder = saves_root / "ReadableGame-1600000001"
    assert f"data\\RENPY_SAVES_ROOT\\ReadableGame-1600000001\\  →  {global_folder}" in readme
    assert f"<game folder>\\game\\saves ({len(built.local_files)} files)" in readme
    for item in manifest["files"]:
        assert f"{item['size']:,} 字节" in readme
    assert f"记录的游戏目录：{built.root}" in readme
    assert "直接复制即可" in readme and "copy them back as they are" in readme
    (check,) = verify_repo(repo, repo.load_catalog(), game_id, snapshot_id)
    assert check.clean and check.extra == [], "README.txt is the snapshot's own"


def test_two_installs_an_absolute_folder_and_the_game_folder_itself(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D67 3: a second install's local saves go to ``data\\GAME_ROOT (local-2)``; a folder
    kept by its absolute path to ``data\\ABSOLUTE\\<drive>\\...``."""
    v1, v2 = build_f4(games_root, saves_root)
    spec = simple_spec(
        "Extra Game",
        "ExtraGame",
        "ExtraGame-1600000009",
        extra=[Define("config", "extra_savedirs", '["mirror"]')],
    )
    extra = build_game(games_root, spec, saves_root)
    write_files(extra.root / "mirror", save_set(2))
    repo = back_up(games_root, saves_root, tmp_path / "library")
    record = next(
        r
        for r in repo.load_catalog()["games"]
        if len(repo.load_game(r["game_id"])["installs"]) == 2
    )
    manifest = repo.load_manifest(record["game_id"], repo.snapshot_ids(record["game_id"])[-1])
    dirs = {s["source_id"]: s["data_dir"] for s in manifest["sources"]}
    assert dirs["local"] == "data/GAME_ROOT/game/saves"
    assert dirs["local-2"] == "data/GAME_ROOT (local-2)/game/saves"
    game_id, snapshot_id = only_snapshot(repo, "Extra Game")
    manifest = repo.load_manifest(game_id, snapshot_id)
    absolute = next(s for s in manifest["sources"] if s["root_token"] == "ABSOLUTE")
    drive = Path(absolute["relative_root"]).drive.rstrip(":")
    parts = Path(absolute["relative_root"]).parts[1:]
    assert absolute["data_dir"] == "/".join(["data", "ABSOLUTE", drive, *parts])
    readme = (repo.snapshot_dir(game_id, snapshot_id) / "README.txt").read_text("utf-8-sig")
    assert f"  →  {absolute['relative_root']}" in readme
    assert clean(repo)


def test_an_old_snapshot_is_read_restored_and_checked_as_before(
    tmp_path: Path, games_root: Path, saves_root: Path
) -> None:
    """D67 3: a snapshot of 0.10.0 (manifest format 1 and 2, ``data\\<source id>``) after the
    migration: its README explains that layout, it verifies, a mirror and an export read it."""
    old = tmp_path / "old"
    with zipfile.ZipFile(FIXTURE) as archive:
        archive.extractall(old)
    result = execute_migration(Repo.open(old))
    assert result.readmes == 14 and result.index
    repo = Repo.open(old)
    game_id, _latest = only_snapshot(repo, "Readable Game")
    first = repo.snapshot_ids(game_id)[0]
    manifest = repo.load_manifest(game_id, first)
    assert manifest["format_version"] == 1
    readme = (repo.snapshot_dir(game_id, first) / "README.txt").read_text("utf-8-sig")
    assert "data\\local\\  →  <游戏目录>\\game\\saves" in readme
    assert "data\\global\\  →" in readme
    assert clean(repo)
    exported = export_snapshot(repo, game_id, first, tmp_path / "old.zip")
    with zipfile.ZipFile(exported.path) as archive:
        names = archive.namelist()
    assert any(n.startswith("data/GAME_ROOT/game/saves/") for n in names), "the new layout"
    mirrored = mirror_repo(repo, tmp_path / "mirror")
    assert mirrored.exit_code == 0 and mirrored.verified is True
    copy = Repo.open(tmp_path / "mirror")
    assert (copy.snapshot_dir(game_id, first) / "README.txt").read_bytes() == (
        repo.snapshot_dir(game_id, first) / "README.txt"
    ).read_bytes()
    assert (tmp_path / "mirror" / "INDEX.txt").is_file(), "the mirror has its INDEX.txt"


# ---------------------------------------------------------------------------- the index


def test_the_index_lists_every_record_by_name(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D67 5: Chinese then English, sorted by name: engine, folder, snapshots, latest backup,
    game folder, save folder; rewritten after each change."""
    build_f1(games_root, saves_root)
    build_game(games_root, simple_spec("Alpha", "Alpha", "Alpha-1"), saves_root)
    build_game(games_root, simple_spec("zeta", "Zeta", "Zeta-1"), saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    raw = (repo.root / "INDEX.txt").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    zh, en = text.split("EngineSave library index")
    assert "EngineSave 备份库索引" in zh and "共 3 条记录" in zh
    names = [line for line in zh.splitlines() if line and not line.startswith((" ", "="))]
    assert names[-3:] == ["Alpha", "Readable Game", "zeta"]
    assert "  引擎：Ren'Py    文件夹：games\\Readable Game" in zh
    assert "  快照：1 个，最近一次 " in zh
    assert f"  存档目录：{saves_root / 'ReadableGame-1600000001'}" in zh  # where it was backed up
    assert "  Engine: Ren'Py    Folder: games\\Readable Game" in en
    game_id, snapshot_id = only_snapshot(repo, "Alpha")
    repo.remove_game(game_id)
    from enginesave.layout import write_index

    write_index(repo)
    assert "Alpha" not in (repo.root / "INDEX.txt").read_text("utf-8-sig")


# ---------------------------------------------------------------------------- export (6)


def test_an_export_is_laid_out_like_a_snapshot_and_old_archives_still_import(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D67 6: the zip's files where they belong (export format 2), its README the snapshot's
    with the export's notes; an archive of 0.10.0's layout (format 1) imports as before; an
    imported snapshot is laid out readably."""
    built = build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    game_id, snapshot_id = only_snapshot(repo, "Readable Game")
    archive = tmp_path / "new.zip"
    export_snapshot(repo, game_id, snapshot_id, archive, keep_paths=True)
    with zipfile.ZipFile(archive) as zf:
        header = json.loads(zf.read("EngineSave-export.json"))
        members = {n: zf.read(n) for n in zf.namelist()}
    assert header["export_format_version"] == 2 and header["manifest"]["format_version"] == 3
    for name, blob in built.local_files.items():
        assert members[f"data/GAME_ROOT/game/saves/{name}"] == blob
    readme = members["README.txt"].decode("utf-8")
    assert readme.index("EngineSave 快照说明") < readme.index("EngineSave export")
    assert "data/GAME_ROOT/game/saves/  ->  <game folder>\\game\\saves" in readme
    # the same archive as 0.10.0 wrote it: data/<source>/..., export format 1
    old_zip = tmp_path / "old.zip"
    old_manifest = {
        **header["manifest"],
        "format_version": 2,
        "sources": [
            {**s, "data_dir": f"data/{s['source_id']}"} for s in header["manifest"]["sources"]
        ],
    }
    old_header = {**header, "export_format_version": 1, "manifest": old_manifest}
    with zipfile.ZipFile(old_zip, "w") as zf:
        zf.writestr("EngineSave-export.json", json.dumps(old_header))
        for item in old_manifest["files"]:
            new = f"{layout.source_data_rel(header['manifest'], item['source_id'])}/{item['path']}"
            zf.writestr(f"data/{item['source_id']}/{item['path']}", members[new])
    for zip_path, target in ((archive, "imported-new"), (old_zip, "imported-old")):
        plan = plan_import(tmp_path / target, zip_path)
        result = execute_import(plan)
        other = Repo.open(tmp_path / target)
        manifest = other.load_manifest(result.game_id, result.snapshot_id)
        assert manifest["format_version"] == 3
        local = stored(other, result.game_id, result.snapshot_id, "local")
        assert local.parts[-3:] == ("GAME_ROOT", "game", "saves")
        assert {p.name: p.read_bytes() for p in local.iterdir()} == built.local_files
        assert (other.snapshot_dir(result.game_id, result.snapshot_id) / "README.txt").is_file()
        assert (other.root / "INDEX.txt").is_file()
        assert clean(other)


def test_an_anonymized_export_s_readme_names_no_folder_of_this_computer(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D67 6: the snapshot README inside an anonymized archive writes profile folders as
    their variables and the game folder as <游戏目录>."""
    build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    game_id, snapshot_id = only_snapshot(repo, "Readable Game")
    archive = tmp_path / "anonymous.zip"
    export_snapshot(repo, game_id, snapshot_id, archive)
    with zipfile.ZipFile(archive) as zf:
        readme = zf.read("README.txt").decode("utf-8")
    assert "  →  %APPDATA%\\RenPy\\ReadableGame-1600000001" in readme
    assert str(saves_root) not in readme and str(games_root) not in readme


# ---------------------------------------------------------------------------- by hand


def test_the_readme_is_enough_to_put_the_saves_back_by_hand(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D67 4: without EngineSave -- read the README's folder lines, copy each data folder to
    its place, compare byte for byte (into stand-in folders here)."""
    built = build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    game_id, snapshot_id = only_snapshot(repo, "Readable Game")
    snapshot = repo.snapshot_dir(game_id, snapshot_id)
    readme = (snapshot / "README.txt").read_text("utf-8-sig")
    moves = [
        line.split("  →  ")
        for line in readme.splitlines()
        if line.startswith("data\\") and "  →  " in line
    ]
    assert len(moves) == 2
    by_hand = tmp_path / "by-hand"
    for folder, where in moves:
        place = where.rsplit("（", 1)[0]
        target = by_hand / ("game" if place.startswith("<游戏目录>") else "global")
        shutil.copytree(snapshot / folder.rstrip("\\"), target)
    assert {p.name: p.read_bytes() for p in (by_hand / "game").iterdir()} == built.local_files
    originals = saves_root / "ReadableGame-1600000001"
    for path in originals.rglob("*"):
        if path.is_file():
            assert (
                by_hand / "global" / path.relative_to(originals)
            ).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("bad", ["data/../x", "elsewhere/x", "data/a:b", "data", "data/x."])
def test_a_manifest_s_data_folder_is_checked(bad: str) -> None:
    """D67 3: a tampered ``data_dir`` never leads out of the snapshot."""
    manifest = {
        "format_version": 3,
        "sources": [{"source_id": "local", "data_dir": bad}],
        "files": [],
    }
    with pytest.raises(layout.UnsafePathError):
        layout.source_data_rel(manifest, "local")


# ------------------------------------------------------------------- M46 review fixes


def test_an_absolute_folder_with_a_device_like_name_still_backs_up(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """M46 review: ``aux.d`` is a folder Windows makes but no snapshot folder can be named:
    the source falls back to ``data/<source id>``, as in 0.10.0."""
    deep = games_root / "aux.d"
    spec = simple_spec(
        "Extra Game", "ExtraGame", "ExtraGame-1600000009",
        extra=[Define("config", "extra_savedirs", '["mirror"]')],
    )  # fmt: skip
    built = build_game(deep, spec, saves_root)
    write_files(built.root / "mirror", save_set(2))
    repo = back_up(deep, saves_root, tmp_path / "library")
    game_id, snapshot_id = only_snapshot(repo, "Extra Game")
    manifest = repo.load_manifest(game_id, snapshot_id)
    absolute = next(s for s in manifest["sources"] if s["root_token"] == "ABSOLUTE")
    assert absolute["data_dir"] == f"data/{absolute['source_id']}"
    assert clean(repo)
    names = [layout.data_dir_for({"source_id": "a", "root_token": "GAME_ROOT",
                                  "relative_root": "game/saves"}, taken := [])]  # fmt: skip
    for source_id in ("K", "k", "K (2)", "K"):  # ids that fold alike: never stuck
        names.append(layout.data_dir_for(
            {"source_id": source_id, "root_token": "GAME_ROOT", "relative_root": "game/saves"},
            taken,
        ))  # fmt: skip
    assert len({n.casefold() for n in names}) == len(names)


def test_a_manifest_whose_data_folders_are_unsafe_is_refused_before_anything(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """M46 review: restore, export and cleanup refuse it up front (FatalError)."""
    from enginesave.errors import FatalError
    from enginesave.restore import validate_manifest

    build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    game_id, snapshot_id = only_snapshot(repo, "Readable Game")
    manifest = repo.load_manifest(game_id, snapshot_id)
    for bad in ("data/x/../y", "data/GAME_ROOT/game/saves", "data/nul"):
        changed = json.loads(json.dumps(manifest))
        changed["sources"][1]["data_dir"] = bad
        with pytest.raises(FatalError):
            validate_manifest(changed)


def test_the_index_follows_every_operation_and_sorts_without_case(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """M46 review: rewritten by a removal, a folder rename and a restore (not by hand);
    sorted by name without regard to case; the saves where the backup took them."""
    from enginesave.removal import execute_remove, plan_remove

    for name, exe, directory in (("beta", "Beta", "Beta-1"), ("Alpha", "Alpha", "Alpha-1")):
        build_game(games_root, simple_spec(name, exe, directory), saves_root)
    build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")

    def index() -> str:
        return (
            (repo.root / "INDEX.txt")
            .read_text(encoding="utf-8-sig")
            .split("EngineSave library index")[0]
        )

    names = [line for line in index().splitlines() if line and not line.startswith((" ", "="))]
    assert names[-3:] == ["Alpha", "beta", "Readable Game"]
    assert f"  存档目录：{saves_root / 'ReadableGame-1600000001'}" in index()
    catalog = repo.load_catalog()
    alpha = next(r for r in catalog["games"] if r["display_name"] == "Alpha")
    execute_remove(plan_remove(repo, catalog, [(alpha["game_id"], None)]))
    assert "Alpha" not in index()
    beta = next(r for r in repo.load_catalog()["games"] if r["display_name"] == "beta")
    doc = repo.load_game(beta["game_id"])
    doc["display_name"] = "Beta Renamed"
    repo.save_game(doc)
    repo.rename_folder(beta["game_id"])
    assert "games\\Beta Renamed" in index()


def test_the_window_shows_record_folders(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """D67 7 (M46 review): the cleanup dialog's label, the details' record line, two records
    of one name and version told apart by their folders, a search by folder name."""
    from enginesave.gui import state
    from enginesave.scan import ScanOptions, run_scan

    assert state.record_label({"game_id": "x", "display_name": "Game", "folder": "Game"}) == (
        "Game  (games\\Game)"
    )
    shown = state.distinct_names(
        [("a" * 8, "Twin", "1.0", "Twin"), ("b" * 8, "Twin", "1.0", "Twin (bbbbbbbb)")]
    )
    assert shown == {"a" * 8: "Twin · games\\Twin", "b" * 8: "Twin · games\\Twin (bbbbbbbb)"}
    build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    (game,) = run_scan(
        ScanOptions(roots=[games_root], saves_root_override=saves_root, repo=repo.root)
    ).games
    folder = repo.game_dir(str(game.known_game_id))
    lines = state.game_detail_lines(game, folder)
    assert ("备份库记录：games\\Readable Game", str(folder)) in lines
    (row,) = state.record_rows(repo, repo.load_catalog())
    assert "Readable Game" in row.search


def test_the_readme_lists_each_file_s_original_time(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    """M46 review: the times a copy may lose are in the README."""
    build_f1(games_root, saves_root)
    repo = back_up(games_root, saves_root, tmp_path / "library")
    game_id, snapshot_id = only_snapshot(repo, "Readable Game")
    readme = (repo.snapshot_dir(game_id, snapshot_id) / "README.txt").read_text("utf-8-sig")
    for item in repo.load_manifest(game_id, snapshot_id)["files"]:
        assert f"{item['size']:,} 字节    {layout._local(item['mtime'])}" in readme


# ---------------------------------------- 1.0.0 (D70 6): README and INDEX never fail the job


def two_records(tmp_path: Path):  # noqa: ANN201
    """Two games backed up: ``(repo, built_one, games, saves, id_one, id_two)``."""
    from fixtures.builder import build_f1, build_game, simple_spec

    games, saves = tmp_path / "games", tmp_path / "saves"
    games.mkdir()
    saves.mkdir()
    one = build_f1(games, saves)
    build_game(
        games,
        simple_spec("Second Game", "SecondGame", "second-1", local_saves=1, global_saves=1),
        saves,
    )
    plan = prepare_backup([games], tmp_path / "repo", saves_root_override=saves)
    repo = Repo.open_or_create(plan.dest)
    assert execute_backup(plan, repo).exit_code == 0
    ids = {r["display_name"]: r["game_id"] for r in repo.load_catalog()["games"]}
    first = next(i for name, i in ids.items() if name != "Second Game")
    return repo, one, games, saves, first, ids["Second Game"]


def test_a_readme_that_cannot_be_written_does_not_cost_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D70 6: the README is explained, the snapshot is the data: a README that fails is
    logged, the snapshot is committed, and migrate adds the README later."""
    from enginesave import layout

    repo, one, games, saves, first, _second = two_records(tmp_path)
    for path in one.local_dir.rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes() + b" more")

    def broken(*_args: object) -> None:
        raise OSError(28, "synthetic: no space left for the README")

    monkeypatch.setattr(layout, "write_readme", broken)
    before = repo.snapshot_ids(first)
    result = execute_backup(prepare_backup([games], repo.root, saves_root_override=saves), repo)
    assert result.exit_code == 0
    new = [s for s in repo.snapshot_ids(first) if s not in before]
    assert len(new) == 1 and (first, new[0]) in set(layout.missing_readmes(repo))


def test_a_damaged_record_does_not_fail_a_backup_a_removal_or_a_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D70 6: INDEX.txt skips a record it cannot read (here: its game.json is damaged and the
    index cannot list it) and never turns the backup, removal or rename into a failure."""
    from enginesave import layout
    from enginesave.removal import execute_remove, plan_remove

    repo, one, games, saves, first, second = two_records(tmp_path)
    (repo.game_dir(second) / "game.json").write_text("{damaged", encoding="utf-8")
    real = layout._save_places

    def fussy(repo_: object, game_id: str, *args: object) -> list[str]:
        if game_id == second:
            raise RuntimeError("synthetic: this record cannot be listed")
        return real(repo_, game_id, *args)

    monkeypatch.setattr(layout, "_save_places", fussy)
    for path in one.local_dir.rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes() + b" more")
    prepare = prepare_backup([games], repo.root, saves_root_override=saves, select=[first])
    assert execute_backup(prepare, repo).exit_code == 0
    index = (repo.root / "INDEX.txt").read_text(encoding="utf-8-sig")
    assert repo.folder_name(first) in index and "Second Game" not in index  # skipped, said
    plan = plan_remove(repo, repo.load_catalog(), [(first, [repo.snapshot_ids(first)[0]])])
    assert execute_remove(plan).exit_code == 0
    doc = repo.load_game(first)
    doc["display_name"] = "Renamed Game"
    repo.save_game(doc)
    old, new = repo.rename_folder(first)
    assert old != new and (repo.root / "games" / new).is_dir()
    assert layout.write_index(repo) is True
