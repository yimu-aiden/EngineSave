"""Export zip and import (D24, plan section 17 M9)."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from enginesave.backup import execute_backup, prepare_backup
from enginesave.cli import main
from enginesave.errors import FatalError, UsageError, VerificationError
from enginesave.export import (
    EXPORT_JSON,
    default_export_name,
    execute_import,
    export_snapshot,
    import_archive,
    member_name,
    plan_import,
)
from enginesave.repo import Repo
from enginesave.restore import execute_restore, plan_restore
from enginesave.verify import verify_objects, verify_repo
from fixtures.builder import (
    BuiltGame,
    Define,
    build_f1,
    build_f8,
    build_game,
    reinstall_copy,
    save_set,
    simple_spec,
    stored,
    to_v1,
    write_files,
)

Library = tuple[Repo, str, str, BuiltGame]


def back_up(games_root: Path, saves_root: Path, dest: Path) -> tuple[Repo, str, str]:
    plan = prepare_backup([games_root], dest, saves_root_override=saves_root, include_tokens=True)
    repo = Repo.open_or_create(plan.dest)
    (outcome,) = execute_backup(plan, repo).results
    assert outcome.status == "ok" and outcome.snapshot_id
    return repo, outcome.game_id, outcome.snapshot_id


@pytest.fixture
def library(games_root: Path, saves_root: Path, tmp_path: Path) -> Library:
    built = build_f1(games_root, saves_root)
    build_f8(saves_root)  # tokens/ (and orphans that are not backed up)
    repo, game_id, snapshot_id = back_up(games_root, saves_root, tmp_path / "repo")
    return repo, game_id, snapshot_id, built


def export(library: Library, dest: Path, saves_root: Path, **options: object):
    repo, game_id, snapshot_id, _ = library
    return export_snapshot(
        repo, game_id, snapshot_id, dest, saves_root_override=saves_root, **options
    )


def read_zip(path: Path) -> tuple[dict, dict[str, bytes]]:
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        members = {info.filename: zf.read(info) for info in zf.infolist()}
    return json.loads(members[EXPORT_JSON]), members


def rewrite(src: Path, dest: Path, change: Callable[[str, bytes], bytes]) -> Path:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info.filename, change(info.filename, zin.read(info)))
    return dest


def edit_header(src: Path, dest: Path, edit: Callable[[dict], None]) -> Path:
    def change(name: str, data: bytes) -> bytes:
        if name != EXPORT_JSON:
            return data
        header = json.loads(data)
        edit(header)
        return json.dumps(header).encode("utf-8")

    return rewrite(src, dest, change)


def make_v1(repo: Repo, game_id: str, snapshot_id: str) -> None:
    """The 0.1.0 layout: plain copies in data/<source> and manifest format 1."""
    to_v1(repo, game_id, snapshot_id)


# ------------------------------------------------------------------------------------ export


# A drive path (also JSON-escaped, C:\\) or a UNC path (\\server, \\\\server in JSON).
ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]|(?:^|[\s'\"(])\\{2,}[A-Za-z0-9]", re.MULTILINE)


def metadata_texts(members: dict[str, bytes]) -> dict[str, str]:
    return {n: b.decode("utf-8") for n, b in members.items() if not n.startswith("data/")}


def test_export_contents_match_the_manifest_with_kept_paths(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    """``keep_paths`` writes the records exactly as stored (the 0.3.0 behaviour)."""
    repo, game_id, snapshot_id, built = library
    dest = tmp_path / "exports" / "readable.zip"
    result = export(library, dest, saves_root, keep_paths=True)
    assert (result.path, result.file_count, result.paths) == (str(dest), 12, "original")
    assert result.archive_bytes == dest.stat().st_size

    header, members = read_zip(dest)
    manifest = header["manifest"]
    assert header["export_format_version"] == 2  # D67 6
    assert (header["snapshot_id"], header["game"]["game_id"]) == (snapshot_id, game_id)
    assert (header["includes_tokens"], header["excluded_sources"]) == (False, ["machine_tokens"])
    assert (header["paths"], header["excluded_reasons"]) == (
        "original",
        {"machine_tokens": "private_keys"},
    )
    assert [s["source_id"] for s in manifest["sources"]] == ["local", "global"]
    data = {name: blob for name, blob in members.items() if name.startswith("data/")}
    assert len(data) == len(manifest["files"]) == 12
    for item in manifest["files"]:
        blob = data[member_name(item, manifest)]
        assert (hashlib.sha256(blob).hexdigest(), len(blob)) == (item["sha256"], item["size"])
    for name, blob in built.local_files.items():
        assert data[f"data/GAME_ROOT/game/saves/{name}"] == blob  # D67 6
    stored = repo.load_manifest(game_id, snapshot_id)
    assert manifest["sources"] == stored["sources"][:2]
    assert manifest["files"] == stored["files"][:12]
    assert json.loads(members["manifest.json"]) == manifest
    assert json.loads(members["game.json"]) == header["game"] == repo.load_game(game_id)

    readme = members["README.txt"].decode("utf-8")
    assert "Game:      Readable Game" in readme and snapshot_id in readme
    assert f"backed up from: {built.local_dir}" in readme and "Paths:     original" in readme
    assert "data/GAME_ROOT/game/saves/  ->  <game folder>\\game\\saves" in readme
    global_line = "data/RENPY_SAVES_ROOT/ReadableGame-1600000001/  ->  %APPDATA%\\RenPy\\"
    assert global_line + "ReadableGame-1600000001" in readme
    assert "enginesave import" in readme and "PRIVATE" not in readme
    assert not list(dest.parent.glob("*.enginesave-tmp"))
    assert all(c.clean for c in verify_repo(repo, repo.load_catalog()))


def test_default_export_contains_no_absolute_paths(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    repo, game_id, snapshot_id, built = library
    stored = repo.load_manifest(game_id, snapshot_id)
    result = export(library, tmp_path / "anonymous.zip", saves_root)
    assert (result.paths, result.file_count) == ("anonymized", 12)
    header, members = read_zip(tmp_path / "anonymous.zip")
    for name, text in metadata_texts(members).items():
        assert not ABSOLUTE.search(text), f"{name} still holds an absolute path"
        assert str(tmp_path).casefold() not in text.casefold()
    manifest = header["manifest"]
    assert header["paths"] == "anonymized" and header["game"]["installs"] == []
    assert [s["origin_path"] for s in manifest["sources"]] == [
        "GAME_ROOT/game/saves",
        "RENPY_SAVES_ROOT/ReadableGame-1600000001",
    ]
    assert manifest["files"] == stored["files"][:12], "only the descriptions change"
    assert header["game"]["identity"] == repo.load_game(game_id)["identity"]
    readme = members["README.txt"].decode("utf-8")
    assert "backed up from: GAME_ROOT/game/saves" in readme and "Paths:     anonymized" in readme
    assert "data/GAME_ROOT/game/saves/  ->  <game folder>\\game\\saves" in readme
    data = {name: blob for name, blob in members.items() if name.startswith("data/")}
    for name, blob in built.local_files.items():
        assert data[f"data/GAME_ROOT/game/saves/{name}"] == blob


def test_error_texts_lose_their_paths(library: Library, saves_root: Path, tmp_path: Path) -> None:
    repo, game_id, snapshot_id, built = library
    manifest_path = repo.snapshot_dir(game_id, snapshot_id) / "manifest.json"
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    denied = repr(str(built.local_dir / "1-1-LT1.save"))
    stored["errors"] = [
        {"source_id": "local", "path": "x.save", "error": f"PermissionError: denied: {denied}"},
        {"source_id": "global", "path": "y", "error": "OSError: lost D:\\Other Place\\y.save"},
        {"source_id": "global", "path": "z", "error": "OSError: lost \\\\server\\share\\z.save"},
    ]
    manifest_path.write_text(json.dumps(stored), encoding="utf-8")
    export(library, tmp_path / "anonymous.zip", saves_root)
    header, members = read_zip(tmp_path / "anonymous.zip")
    errors = [e["error"] for e in header["manifest"]["errors"]]
    assert errors[0].startswith("PermissionError: denied: 'GAME_ROOT/game/saves")
    assert errors[1] == "OSError: lost <path> Place\\y.save"
    assert errors[2] == "OSError: lost <path>"
    for text in metadata_texts(members).values():
        assert not ABSOLUTE.search(text)


def test_tokens_only_when_asked(library: Library, saves_root: Path, tmp_path: Path) -> None:
    result = export(library, tmp_path / "with-tokens.zip", saves_root, include_tokens=True)
    header, members = read_zip(tmp_path / "with-tokens.zip")
    assert result.includes_tokens and header["includes_tokens"]
    assert header["excluded_sources"] == [] and result.file_count == 14
    keys = next(f for f in header["manifest"]["files"] if f["source_id"] == "machine_tokens")
    assert members[member_name(keys, header["manifest"])].startswith(b"signing-key")
    assert "PRIVATE KEY" in members["README.txt"].decode("utf-8")


def test_targets_inside_library_or_save_locations_are_refused(
    library: Library, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    repo, _, _, built = library
    assert built.global_dir is not None
    for bad in (
        repo.root / "x.zip",
        repo.root / "games" / "x.zip",
        built.root / "x.zip",
        built.local_dir / "x.zip",
        built.global_dir / "x.zip",
        saves_root / "x.zip",
    ):
        with pytest.raises(UsageError):
            export(library, bad, saves_root)
        assert not bad.exists()
    scan_root_target = games_root / "x.zip"
    with pytest.raises(UsageError, match="must not be inside"):
        export(library, scan_root_target, saves_root, forbidden_roots=[games_root])
    assert not scan_root_target.exists()
    folder = tmp_path / "a-folder"
    folder.mkdir()
    with pytest.raises(UsageError, match="is a folder"):
        export(library, folder, saves_root)


def test_existing_archive_needs_overwrite(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "old.zip"
    dest.write_bytes(b"old content")
    with pytest.raises(UsageError, match="--overwrite"):
        export(library, dest, saves_root)
    assert dest.read_bytes() == b"old content"
    export(library, dest, saves_root, overwrite=True)
    assert zipfile.is_zipfile(dest)
    assert not (tmp_path / "old.zip.enginesave-tmp").exists()


def test_v1_and_v2_snapshots_export_the_same_files(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    repo, game_id, snapshot_id, _ = library
    export(library, tmp_path / "v2.zip", saves_root)
    make_v1(repo, game_id, snapshot_id)
    export(library, tmp_path / "v1.zip", saves_root)
    header_v2, members_v2 = read_zip(tmp_path / "v2.zip")
    header_v1, members_v1 = read_zip(tmp_path / "v1.zip")
    # D67 6: both archives in the readable layout, whatever the snapshot's own
    assert header_v2["manifest"]["format_version"] == header_v1["manifest"]["format_version"] == 3
    assert repo.load_manifest(game_id, snapshot_id)["format_version"] == 1
    assert header_v1["manifest"]["files"] == header_v2["manifest"]["files"]
    data_v1 = {n: b for n, b in members_v1.items() if n.startswith("data/")}
    data_v2 = {n: b for n, b in members_v2.items() if n.startswith("data/")}
    assert data_v1 == data_v2 and len(data_v1) == 12


def test_a_damaged_snapshot_copy_fails_verification(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    repo, game_id, snapshot_id, built = library
    name = sorted(built.local_files)[0]
    copy = stored(repo, game_id, snapshot_id, "local", name)
    copy.unlink()  # break the hard link only; the object stays intact
    copy.write_bytes(b"damaged")
    dest = tmp_path / "damaged.zip"
    with pytest.raises(VerificationError, match="does not match the manifest"):
        export(library, dest, saves_root)
    assert not dest.exists() and not (tmp_path / "damaged.zip.enginesave-tmp").exists()

    code = main(
        ["export", "--repo", str(repo.root), "--game", game_id, "--to", str(dest)]
        + ["--renpy-saves-root", str(saves_root)]
    )
    assert code == 1 and not dest.exists()


def test_a_missing_snapshot_file_is_fatal(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    repo, game_id, snapshot_id, built = library
    name = sorted(built.local_files)[0]
    stored(repo, game_id, snapshot_id, "local", name).unlink()
    dest = tmp_path / "missing.zip"
    with pytest.raises(FatalError, match="run verify"):
        export(library, dest, saves_root)
    assert not dest.exists() and not (tmp_path / "missing.zip.enginesave-tmp").exists()


def test_cli_export_into_a_folder_uses_the_default_name(
    library: Library, saves_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, snapshot_id, _ = library
    folder = tmp_path / "exports"
    folder.mkdir()
    args = ["export", "--repo", str(repo.root), "--game", "readable game", "--to", str(folder)]
    args += ["--renpy-saves-root", str(saves_root)]
    assert main([*args, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    expected = default_export_name("Readable Game", snapshot_id)
    assert expected == f"EngineSave-readablegame-{snapshot_id}.zip"
    assert Path(data["path"]) == folder / expected and (folder / expected).is_file()
    assert data["excluded_sources"] == ["machine_tokens"] and data["paths"] == "anonymized"

    assert main(args) == 2
    assert "already exists" in capsys.readouterr().err
    assert main([*args, "--overwrite", "--snapshot", snapshot_id]) == 0
    out = capsys.readouterr().out
    assert "every file verified" in out and "paths: anonymized" in out
    assert main([*args, "--overwrite", "--keep-paths"]) == 0
    assert "paths: original" in capsys.readouterr().out
    header, _ = read_zip(folder / expected)
    assert header["paths"] == "original" and header["game"]["installs"]
    assert main([*args, "--snapshot", "20000101T000000Z"]) == 2


# ------------------------------------------------------------------------------------ import


def test_import_into_a_new_library_round_trips(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    repo, game_id, snapshot_id, built = library
    archive = tmp_path / "readable.zip"
    export(library, archive, saves_root)
    other_root = tmp_path / "other"
    plan = plan_import(other_root, archive)
    assert (plan.game_id, plan.matched_by, plan.ignored_members) == (game_id, None, 0)
    assert not other_root.exists(), "planning writes nothing"
    result = execute_import(plan)
    assert (result.game_id, result.created_record, result.file_count) == (game_id, True, 12)

    other = Repo.open(other_root)
    manifest = other.load_manifest(game_id, result.snapshot_id)
    original = repo.load_manifest(game_id, snapshot_id)
    assert (manifest["reason"], manifest["format_version"]) == ("import", 3)
    assert manifest["imported_from"]["snapshot_id"] == snapshot_id
    assert manifest["files"] == [f for f in original["files"] if f["source_id"] != "machine_tokens"]
    assert all(c.clean for c in verify_repo(other, other.load_catalog()))
    assert verify_objects(other).clean
    doc, source_doc = other.load_game(game_id), repo.load_game(game_id)
    assert doc["display_name"] == source_doc["display_name"]
    assert doc["identity"] == source_doc["identity"]
    assert {t["source_id"] for t in doc["source_templates"]} == {"local", "global"}
    assert doc["installs"] == [], "an anonymized export names no install folder"
    assert [s["origin_path"] for s in manifest["sources"]][0] == "GAME_ROOT/game/saves"

    new_root = reinstall_copy(built.root, tmp_path / "Reinstalled")
    new_saves = tmp_path / "new-saves"
    dry_run = ["restore", "--repo", str(other_root), "--game", game_id, "--dry-run"]
    assert main([*dry_run, "--renpy-saves-root", str(new_saves)]) == 2, "no install is known"
    assert main([*dry_run, "--game-root", str(new_root), "--renpy-saves-root", str(new_saves)]) == 0
    restore = plan_restore(
        other, other.load_catalog(), game_id, game_root=new_root, saves_root_override=new_saves
    )
    assert restore.snapshot_id == result.snapshot_id
    assert {t.source_id: t.target_dir for t in restore.targets} == {
        "local": new_root / "game" / "saves",
        "global": new_saves / "ReadableGame-1600000001",
    }
    outcome = execute_restore(restore)
    assert (outcome.written, outcome.failures) == (12, [])
    for name, blob in built.local_files.items():
        assert (new_root / "game" / "saves" / name).read_bytes() == blob


def test_import_into_the_same_library_reuses_the_record(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    repo, game_id, _, _ = library
    archive = tmp_path / "readable.zip"
    export(library, archive, saves_root)
    objects_before = sorted(name for name, _ in repo.iter_objects())
    plan = plan_import(repo.root, archive)
    assert (plan.game_id, plan.matched_by) == (game_id, "game_id")
    result = execute_import(plan)
    assert not result.created_record and result.storage == "hardlink"
    assert len(repo.snapshot_ids(game_id)) == 2
    assert repo.latest_snapshot(game_id) == result.snapshot_id
    assert sorted(name for name, _ in repo.iter_objects()) == objects_before
    assert all(c.clean for c in verify_repo(repo, repo.load_catalog()))
    (record,) = repo.load_catalog()["games"]
    assert (record["snapshot_count"], record["latest_snapshot"]) == (2, result.snapshot_id)


def test_import_matches_another_record_by_save_directory(
    library: Library, games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    archive = tmp_path / "readable.zip"
    export(library, archive, saves_root)
    other, other_id, _ = back_up(games_root, saves_root, tmp_path / "other")
    assert other_id != library[1]
    plan = plan_import(other.root, archive)
    assert (plan.game_id, plan.matched_by) == (other_id, "identity")
    execute_import(plan)
    assert len(other.load_catalog()["games"]) == 1
    assert len(other.snapshot_ids(other_id)) == 2


def test_a_tampered_archive_imports_nothing(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    repo, game_id, _, built = library
    archive = tmp_path / "readable.zip"
    export(library, archive, saves_root)
    target = f"data/GAME_ROOT/game/saves/{sorted(built.local_files)[-1]}"  # D67 6
    tampered = rewrite(
        archive, tmp_path / "tampered.zip", lambda n, d: b"tampered" if n == target else d
    )
    other_root = tmp_path / "other"
    with pytest.raises(VerificationError, match="nothing was imported"):
        import_archive(other_root, tampered)
    other = Repo.open(other_root)
    assert other.load_catalog()["games"] == [] and list((other_root / "games").iterdir()) == []
    assert other.stale_tmp_dirs() == []

    code = main(["import", "--repo", str(repo.root), "--from", str(tampered), "--yes"])
    assert code == 1
    assert len(repo.snapshot_ids(game_id)) == 1 and repo.stale_tmp_dirs() == []


def test_unsafe_or_foreign_archives_are_refused(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    archive = tmp_path / "readable.zip"
    export(library, archive, saves_root)
    other_root = tmp_path / "other"

    def escape(header: dict) -> None:
        header["manifest"]["files"][0]["path"] = "../../evil.save"

    def absolute_root(header: dict) -> None:
        header["manifest"]["sources"][0]["relative_root"] = "C:\\Windows"

    def bad_hash(header: dict) -> None:
        header["manifest"]["files"][0]["sha256"] = "not-a-hash"

    for edit in (escape, absolute_root, bad_hash):
        crafted = edit_header(archive, tmp_path / f"{edit.__name__}.zip", edit)
        with pytest.raises(FatalError):
            plan_import(other_root, crafted)
    foreign = tmp_path / "foreign.zip"
    with zipfile.ZipFile(foreign, "w") as zf:
        zf.writestr("hello.txt", "not an export")
    with pytest.raises(FatalError, match="not an EngineSave export"):
        plan_import(other_root, foreign)
    (tmp_path / "not-a-zip.zip").write_bytes(b"plain bytes")
    with pytest.raises(FatalError, match="cannot read"):
        plan_import(other_root, tmp_path / "not-a-zip.zip")
    with pytest.raises(UsageError, match="not an existing file"):
        plan_import(other_root, tmp_path / "absent.zip")
    assert not other_root.exists()


def test_unlisted_members_are_ignored(library: Library, saves_root: Path, tmp_path: Path) -> None:
    archive = tmp_path / "readable.zip"
    export(library, archive, saves_root)
    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("data/local/extra.save", b"not in the manifest")
    plan = plan_import(tmp_path / "other", archive)
    assert plan.ignored_members == 1
    result = execute_import(plan)
    other = Repo.open(tmp_path / "other")
    paths = {f["path"] for f in other.load_manifest(result.game_id, result.snapshot_id)["files"]}
    assert "extra.save" not in paths
    assert all(c.clean for c in verify_repo(other, other.load_catalog()))


def test_cli_import_needs_confirmation(
    library: Library, saves_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = tmp_path / "readable.zip"
    export(library, archive, saves_root)
    args = ["import", "--repo", str(tmp_path / "other"), "--from", str(archive)]
    assert main(args) == 2
    assert not (tmp_path / "other").exists()
    assert main([*args, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "import plan" in out and "new record" in out and "to restore: enginesave restore" in out


# ------------------------------------------------------------------------- paths (0.3.1)


def test_absolute_sources_stay_out_unless_paths_are_kept(
    games_root: Path, saves_root: Path, tmp_path: Path
) -> None:
    spec = simple_spec(
        "Extra Game",
        "ExtraGame",
        "ExtraGame-1600000009",
        extra=[Define("config", "extra_savedirs", '["mirror"]')],
    )
    built = build_game(games_root, spec, saves_root)
    write_files(built.root / "mirror", save_set(2))
    repo, game_id, snapshot_id = back_up(games_root, saves_root, tmp_path / "repo")
    stored = repo.load_manifest(game_id, snapshot_id)
    extra = next(s for s in stored["sources"] if s["kind"] == "extra")
    assert (extra["root_token"], extra["relative_root"]) == ("ABSOLUTE", str(built.root / "mirror"))
    library = (repo, game_id, snapshot_id, built)

    result = export(library, tmp_path / "anonymous.zip", saves_root)
    assert result.excluded_reasons == {extra["source_id"]: "absolute_path"}
    header, members = read_zip(tmp_path / "anonymous.zip")
    assert not any(name.startswith("data/ABSOLUTE/") for name in members)  # D67 6
    assert extra["source_id"] not in {s["source_id"] for s in header["manifest"]["sources"]}
    assert all(t["root_token"] != "ABSOLUTE" for t in header["game"]["source_templates"])
    assert "--keep-paths to add it" in members["README.txt"].decode("utf-8")
    for text in metadata_texts(members).values():
        assert not ABSOLUTE.search(text)

    kept = export(library, tmp_path / "kept.zip", saves_root, keep_paths=True)
    assert (kept.excluded_sources, kept.paths) == ([], "original")
    imported = import_archive(tmp_path / "other", tmp_path / "kept.zip")
    other = Repo.open(tmp_path / "other")
    restore = plan_restore(
        other,
        other.load_catalog(),
        imported.game_id,
        game_root=reinstall_copy(built.root, tmp_path / "new-root"),
        saves_root_override=tmp_path / "new-saves",
    )
    targets = {t.source_id: t.target_dir for t in restore.targets}
    assert targets[extra["source_id"]] == built.root / "mirror", "kept paths restore in place"


def test_import_accepts_old_archives_and_ignores_bare_install_names(
    library: Library, saves_root: Path, tmp_path: Path
) -> None:
    export(library, tmp_path / "kept.zip", saves_root, keep_paths=True)

    def as_0_3_0(header: dict) -> None:
        del header["paths"], header["excluded_reasons"]

    old = edit_header(tmp_path / "kept.zip", tmp_path / "old.zip", as_0_3_0)
    result = import_archive(tmp_path / "from-old", old)
    doc = Repo.open(tmp_path / "from-old").load_game(result.game_id)
    assert [i["game_root"] for i in doc["installs"]] == [str(library[3].root)]

    def bare_name(header: dict) -> None:
        header["game"]["installs"] = [{"game_root": "Readable Game 1.0"}]

    crafted = edit_header(tmp_path / "kept.zip", tmp_path / "bare.zip", bare_name)
    result = import_archive(tmp_path / "from-bare", crafted)
    assert Repo.open(tmp_path / "from-bare").load_game(result.game_id)["installs"] == []
