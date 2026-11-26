"""1.3.0 (D77): records Collections works point to: ``--game collections:<id>``, attachment-only
records, a later backup joining them, merging two works, ``link --collections-work``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from enginesave.attachments import ATTACHMENTS_DIR
from enginesave.cli import main
from enginesave.repo import Repo
from fixtures.builder import build_f1
from test_schemas import conforms, load, validate


def run(args: list[str], capsys) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(args: list[str], capsys) -> tuple[int, dict]:
    code, out, err = run([*args, "--json"], capsys)
    assert out, err
    return code, json.loads(out)


def shaped(action: str, data: dict) -> None:
    schema = load("attachments")
    assert validate(schema["$defs"][action], data, schema) == []


@pytest.fixture
def setup(games_root: Path, saves_root: Path, tmp_path: Path):
    game = build_f1(games_root, saves_root)
    dest = tmp_path / "library"
    dest.mkdir()
    Repo.open_or_create(dest)
    guide = tmp_path / "in" / "guide.txt"
    guide.parent.mkdir()
    guide.write_bytes(b"guide")
    works = tmp_path / "collections.json"
    doc = {"works": [{"work_id": "w1", "title": "Readable", "copies": [{"path": str(game.root)}]}]}
    works.write_text(json.dumps(doc), encoding="utf-8")
    common = ["--renpy-saves-root", str(saves_root)]
    return game, dest, guide, works, common


def test_a_work_without_a_record_gets_one_for_its_attachments(setup, capsys) -> None:
    _game, dest, guide, _works, _common = setup
    base = ["attachments", "list", "--repo", str(dest), "--game", "collections:w1"]
    code, data = run_json(base, capsys)
    shaped("list", data)
    assert code == 0 and data["game_id"] is None and data["folder"] is None

    add = ["attachments", "add", "--repo", str(dest), "--game", "collections:w1", str(guide)]
    code, _out, err = run(add, capsys)
    assert code == 2 and "--title" in err, "no record is made without a name for it"
    code, data = run_json([*add, "--title", "可读的游戏"], capsys)
    shaped("add", data)
    assert code == 0 and data["created_record"] is True and data["collections_work_ids"] == ["w1"]
    folder = Path(data["folder"])
    assert folder.name == ATTACHMENTS_DIR and folder.parent.name == "可读的游戏"

    code, data = run_json([*add, "--title", "ignored now"], capsys)
    assert data["created_record"] is False and data["saved"][0]["name"] == "guide (2).txt"
    code, data = run_json(base, capsys)
    assert data["file_count"] == 2 and data["collections_work_ids"] == ["w1"]
    code, data = run_json(["list", "--repo", str(dest)], capsys)
    conforms("list", data)
    (row,) = data["games"]
    assert row["collections_work_ids"] == ["w1"] and row["attachments"]["file_count"] == 2
    assert row["snapshot_count"] == 0 and row["engine"] is None


def test_a_later_backup_joins_that_record(setup, capsys) -> None:
    game, dest, guide, works, common = setup
    run_json(
        ["attachments", "add", "--repo", str(dest), "--game", "collections:w1", "--title",
         "Readable", str(guide)],
        capsys,
    )  # fmt: skip
    (record,) = Repo.open(dest).load_catalog()["games"]
    backup = ["backup", "--game-root", str(game.root), *common, "--dest", str(dest), "--yes"]
    code, data = run_json([*backup, "--collections", str(works)], capsys)
    conforms("backup", data)
    assert code == 0 and data["result"]["results"][0]["game_id"] == record["game_id"]
    repo = Repo.open(dest)
    (after,) = repo.load_catalog()["games"]
    doc = repo.load_game(after["game_id"])
    assert doc["engine"] == "renpy" and doc["display_name"] == "Readable"
    assert len(repo.snapshot_ids(after["game_id"])) == 1
    code, data = run_json(["show", "--repo", str(dest), "--game", "collections:w1"], capsys)
    assert code == 0 and data["game"]["game_id"] == record["game_id"]


def test_a_game_with_its_own_record_absorbs_the_attachment_only_one(setup, capsys) -> None:
    game, dest, guide, works, common = setup
    backup = ["backup", "--game-root", str(game.root), *common, "--dest", str(dest), "--yes"]
    run_json(backup, capsys)  # without Collections: a record of its own
    (own,) = Repo.open(dest).load_catalog()["games"]
    run_json(
        ["attachments", "add", "--repo", str(dest), "--game", "collections:w1", "--title",
         "Readable (Collections)", str(guide)],
        capsys,
    )  # fmt: skip
    assert len(Repo.open(dest).load_catalog()["games"]) == 2
    code, data = run_json([*backup, "--collections", str(works)], capsys)
    assert code == 0 and data["result"]["absorbed"][0]["from"]["removed"] is True
    repo = Repo.open(dest)
    (left,) = repo.load_catalog()["games"]
    assert left["game_id"] == own["game_id"] and left["collections_work_ids"] == ["w1"]
    held = repo.game_dir(own["game_id"]) / ATTACHMENTS_DIR / "guide.txt"
    assert held.read_bytes() == b"guide"


def test_merging_two_works(setup, tmp_path: Path, capsys) -> None:
    game, dest, guide, _works, common = setup
    for work, title in (("w1", "One"), ("w2", "Two")):
        run_json(
            ["attachments", "add", "--repo", str(dest), "--game", f"collections:{work}",
             "--title", title, str(guide)],
            capsys,
        )  # fmt: skip
    merge = ["attachments", "merge", "--repo", str(dest)]
    code, data = run_json([*merge, "--from", "collections:w2", "--into", "collections:w1"], capsys)
    shaped("merge", data)
    assert code == 0 and data["from"]["removed"] is True
    assert data["moved"] == [{"from": "guide.txt", "name": "guide (2).txt"}]
    assert data["into"]["collections_work_ids"] == ["w1", "w2"]
    code, data = run_json(["show", "--repo", str(dest), "--game", "collections:w2"], capsys)
    assert code == 0 and data["game"]["display_name"] == "One", "the old work id still resolves"

    # a record with snapshots is kept: only its attachments and works move
    backup = ["backup", "--game-root", str(game.root), *common, "--dest", str(dest), "--yes"]
    run_json(backup, capsys)
    readable = next(r for r in Repo.open(dest).load_catalog()["games"] if r["snapshot_count"])
    run_json(
        ["link", "--repo", str(dest), "--game", readable["game_id"], "--collections-work", "w3"],
        capsys,
    )
    code, data = run_json([*merge, "--from", "collections:w3", "--into", "collections:w1"], capsys)
    assert code == 0 and data["from"]["removed"] is False and data["from"]["snapshots"] == 1
    assert "w3" in data["into"]["collections_work_ids"]
    assert Repo.open(dest).load_game(readable["game_id"]).get("collections_work_ids") == []


def test_link_refuses_a_work_kept_elsewhere(setup, capsys) -> None:
    _game, dest, guide, _works, _common = setup
    run_json(
        ["attachments", "open", "--repo", str(dest), "--game", "collections:w1", "--title", "A",
         "--create-only"],
        capsys,
    )  # fmt: skip
    run_json(
        ["attachments", "open", "--repo", str(dest), "--game", "collections:w2", "--title", "B",
         "--create-only"],
        capsys,
    )  # fmt: skip
    code, _out, err = run(
        ["link", "--repo", str(dest), "--game", "collections:w2", "--collections-work", "w1"],
        capsys,
    )
    assert code == 2 and "attachments merge" in err
    code, _out, err = run(["link", "--repo", str(dest), "--game", "collections:w1"], capsys)
    assert code == 2 and "--game-root" in err
