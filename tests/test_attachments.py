"""1.3.0 (D76): a record's 附件 (attachments): listing, adding (copy or move, cleaned and
numbered names, nothing partial on failure), the CLI, removal keeping them, a later record of
the same name taking them over, the mirror copying them, and verification ignoring them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from enginesave import attachments
from enginesave.attachments import ATTACHMENTS_DIR, clean_name, numbered
from enginesave.cli import main
from enginesave.repo import Repo
from fixtures.builder import build_f1
from test_schemas import load, validate


def run(args: list[str], capsys) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(args: list[str], capsys) -> tuple[int, dict]:
    code, out, err = run([*args, "--json"], capsys)
    assert out, err
    return code, json.loads(out)


def conforms(action: str, data: dict) -> None:
    schema = load("attachments")
    assert validate(schema["$defs"][action], data, schema) == []


@pytest.fixture
def library(games_root: Path, saves_root: Path, tmp_path: Path, capsys) -> tuple[Path, str]:
    game = build_f1(games_root, saves_root)
    dest = tmp_path / "library"
    args = ["backup", "--game-root", str(game.root), "--renpy-saves-root", str(saves_root)]
    code, _out, _err = run([*args, "--dest", str(dest), "--yes"], capsys)
    assert code == 0
    (record,) = Repo.open(dest).load_catalog()["games"]
    return dest, record["game_id"]


def files(tmp_path: Path, **named: bytes) -> list[Path]:
    folder = tmp_path / "incoming"
    folder.mkdir(exist_ok=True)
    out = []
    for name, data in named.items():
        (folder / name).write_bytes(data)
        out.append(folder / name)
    return out


# ------------------------------------------------------------------------------- names


def test_names_are_cleaned_for_windows(tmp_path: Path) -> None:
    folder = tmp_path / ATTACHMENTS_DIR
    assert clean_name('攻略: "第1章"?.txt', folder) == "攻略_ _第1章__.txt"
    assert clean_name("CON.txt", folder) == "_CON.txt"
    assert clean_name("notes. . ", folder) == "notes"
    assert clean_name("...", folder) == ATTACHMENTS_DIR
    long = clean_name("x" * 400 + ".zip", folder)
    assert long.endswith(".zip") and len(str(folder / long)) + len(" (999)") <= 259


def test_adding_starts_in_the_games_own_folder(tmp_path: Path) -> None:
    from enginesave.gui.state import attachments_start_folder

    old, new = tmp_path / "Hero-0.3", tmp_path / "Hero-0.4"
    old.mkdir()
    new.mkdir()
    doc = {
        "installs": [
            {"game_root": str(old), "last_seen": "2026-11-01T00:00:00Z"},
            {"game_root": str(new), "last_seen": "2026-11-20T00:00:00Z"},
            {"game_root": str(tmp_path / "gone"), "last_seen": "2026-11-25T00:00:00Z"},
        ]
    }
    assert attachments_start_folder(doc) == new, "the newest seen that is still there"
    assert attachments_start_folder({"installs": []}) is None


def test_taken_names_are_numbered() -> None:
    taken = {"guide.txt", "guide (2).txt", "readme"}
    assert numbered("Guide.txt", taken) == "Guide (3).txt"
    assert numbered("readme", taken) == "readme (2)"
    assert numbered("new.txt", taken) == "new.txt"


# ------------------------------------------------------------------------------- the CLI


def test_list_add_open(library, tmp_path: Path, capsys, monkeypatch) -> None:
    dest, game_id = library
    base = ["attachments", "list", "--repo", str(dest), "--game", game_id]
    code, data = run_json(base, capsys)
    conforms("list", data)
    folder = Repo.open(dest).game_dir(game_id) / ATTACHMENTS_DIR
    assert code == 0 and data["folder"] == str(folder) and data["exists"] is False
    assert data["entries"] == [] and data["file_count"] == 0

    guide, patch = files(tmp_path, **{"guide.txt": b"g" * 10, "patch.zip": b"p" * 20})
    add = ["attachments", "add", "--repo", str(dest), "--game", "Readable Game"]
    code, data = run_json([*add, str(guide), str(patch)], capsys)
    conforms("add", data)
    assert code == 0 and [s["name"] for s in data["saved"]] == ["guide.txt", "patch.zip"]
    assert guide.is_file(), "copied: the original stays"
    code, data = run_json([*add, "--move", str(guide)], capsys)
    assert code == 0 and data["saved"][0]["name"] == "guide (2).txt" and not guide.exists()
    (folder / "sub").mkdir()
    (folder / "sub" / "deep.bin").write_bytes(b"d" * 5)

    code, data = run_json(base, capsys)
    assert data["exists"] and data["file_count"] == 4 and data["total_bytes"] == 45
    assert [(e["name"], e["kind"]) for e in data["entries"]] == [
        ("guide (2).txt", "file"),
        ("guide.txt", "file"),
        ("patch.zip", "file"),
        ("sub", "dir"),
    ]
    index = (dest / "INDEX.txt").read_text(encoding="utf-8")
    assert "附件：3 个文件" in index

    opened: list[str] = []
    monkeypatch.setattr(attachments, "open_in_explorer", lambda f: opened.append(str(f)))
    monkeypatch.setattr(
        "enginesave.cli.attaching.open_in_explorer", lambda f: opened.append(str(f))
    )
    code, data = run_json(["attachments", "open", "--repo", str(dest), "--game", game_id], capsys)
    conforms("open", data)
    assert code == 0 and opened == [str(folder)]
    code, data = run_json(
        ["attachments", "open", "--repo", str(dest), "--game", game_id, "--create-only"], capsys
    )
    assert code == 0 and opened == [str(folder)], "--create-only opens nothing"


def test_bad_input_is_refused(library, tmp_path: Path, capsys) -> None:
    dest, game_id = library
    add = ["attachments", "add", "--repo", str(dest), "--game", game_id]
    code, _out, err = run([*add, str(tmp_path / "missing.txt")], capsys)
    assert code == 2 and "not a file" in err
    code, _out, err = run([*add, str(tmp_path)], capsys)
    assert code == 2 and "not a file" in err
    code, _out, err = run(["attachments", "list", "--repo", str(dest), "--game", "nope"], capsys)
    assert code == 2


def test_a_failed_copy_leaves_nothing(library, tmp_path: Path, capsys, monkeypatch) -> None:
    dest, game_id = library
    (source,) = files(tmp_path, **{"big.bin": b"b" * 100})

    def broken(reader, writer, _size=0) -> None:
        writer.write(b"half")
        raise OSError("disk full")

    monkeypatch.setattr(attachments.shutil, "copyfileobj", broken)
    monkeypatch.setattr(attachments.os, "rename", _cross_drive(attachments.os.rename))
    code, data = run_json(
        ["attachments", "add", "--repo", str(dest), "--game", game_id, "--move", str(source)],
        capsys,
    )
    assert code == 1 and data["saved"] == [] and "disk full" in data["failed"][0]["error"]
    folder = Repo.open(dest).game_dir(game_id) / ATTACHMENTS_DIR
    assert list(folder.iterdir()) == [], "no partial file"
    assert source.read_bytes() == b"b" * 100, "the original stays"


def _cross_drive(real):
    """os.rename that refuses to move the source file (as across drives), renames otherwise."""

    def rename(src, dst):
        if "incoming" in str(src):
            raise OSError(17, "not the same device")
        return real(src, dst)

    return rename


def test_writing_takes_the_library_lock(library, tmp_path: Path, capsys) -> None:
    from test_lock import holder, write_lock

    dest, game_id = library
    (source,) = files(tmp_path, **{"a.txt": b"a"})
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        write_lock(dest, holder(child.pid))
        add = ["attachments", "add", "--repo", str(dest), "--game", game_id, str(source)]
        code, data = run_json(add, capsys)
        assert code == 4 and data["error"] == "library_busy"
        code, _data = run_json(
            ["attachments", "list", "--repo", str(dest), "--game", game_id], capsys
        )
        assert code == 0, "reading is never blocked"
    finally:
        child.kill()
        child.wait()


# ---------------------------------------------------------------- the rest of the library


def test_removing_the_record_keeps_them_and_the_next_record_takes_them(
    library, games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    dest, game_id = library
    (source,) = files(tmp_path, **{"guide.txt": b"g"})
    run_json(["attachments", "add", "--repo", str(dest), "--game", game_id, str(source)], capsys)
    record_folder = Repo.open(dest).game_dir(game_id)

    code, data = run_json(["remove", "--repo", str(dest), "--game", game_id], capsys)
    assert code == 0 and data["plan"]["items"][0]["attachments"]["file_count"] == 1
    code, data = run_json(["remove", "--repo", str(dest), "--game", game_id, "--yes"], capsys)
    assert code == 0 and data["plan"]["with_attachments"] is False
    assert [p.name for p in record_folder.iterdir()] == [ATTACHMENTS_DIR]
    repo = Repo.open(dest)
    assert repo.orphan_game_dirs() == [] and repo.kept_attachments() == [record_folder]
    code, data = run_json(["list", "--repo", str(dest)], capsys)
    assert data["orphan_record_dirs"] == []

    # the same game backed up again: its new record has that folder, attachments included
    game_root = next(p for p in games_root.iterdir() if p.is_dir())
    run(
        ["backup", "--game-root", str(game_root), "--renpy-saves-root", str(saves_root),
         "--dest", str(dest), "--yes"],
        capsys,
    )  # fmt: skip
    (record,) = Repo.open(dest).load_catalog()["games"]
    assert Repo.open(dest).game_dir(record["game_id"]) == record_folder
    code, data = run_json(
        ["attachments", "list", "--repo", str(dest), "--game", record["game_id"]], capsys
    )
    assert data["file_count"] == 1

    code, data = run_json(
        ["remove", "--repo", str(dest), "--game", record["game_id"], "--yes", "--with-attachments"],
        capsys,
    )
    assert code == 0 and not record_folder.exists()


def test_verify_and_keep_auto_leave_them_alone(
    library, games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    dest, game_id = library
    game_root = next(p for p in games_root.iterdir() if p.is_dir())
    run(
        [
            "backup",
            "--game-root",
            str(game_root),
            "--renpy-saves-root",
            str(saves_root),
            "--dest",
            str(dest),
            "--yes",
        ],
        capsys,
    )  # fmt: skip: a second snapshot, so that removing one leaves the record
    (source,) = files(tmp_path, **{"guide.txt": b"g"})
    run_json(["attachments", "add", "--repo", str(dest), "--game", game_id, str(source)], capsys)
    code, data = run_json(["verify", "--repo", str(dest)], capsys)
    assert code == 0 and data["orphan_record_dirs"] == []
    code, data = run_json(
        ["remove", "--repo", str(dest), "--game", game_id, "--snapshot", "latest", "--yes"], capsys
    )
    assert code == 0
    folder = Repo.open(dest).game_dir(game_id) / ATTACHMENTS_DIR
    assert (folder / "guide.txt").is_file()


def test_the_mirror_copies_them(library, tmp_path: Path, capsys) -> None:
    dest, game_id = library
    (source,) = files(tmp_path, **{"guide.txt": b"g" * 3})
    run_json(["attachments", "add", "--repo", str(dest), "--game", game_id, str(source)], capsys)
    target = tmp_path / "mirror"
    code, data = run_json(["mirror", "--repo", str(dest), "--to", str(target)], capsys)
    assert code == 0 and data["attachments_copied"] == 1
    folder_name = Repo.open(dest).folder_name(game_id)
    copied = target / "games" / folder_name / ATTACHMENTS_DIR / "guide.txt"
    assert copied.read_bytes() == b"g" * 3
    code, data = run_json(["mirror", "--repo", str(dest), "--to", str(target)], capsys)
    assert data["attachments_copied"] == 0, "unchanged files are not copied again"
    os.remove(Repo.open(dest).game_dir(game_id) / ATTACHMENTS_DIR / "guide.txt")
    run_json(["mirror", "--repo", str(dest), "--to", str(target)], capsys)
    assert copied.is_file(), "the mirror never deletes one"
