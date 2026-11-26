"""1.3.0 (D74): Collections says which folders are versions of one game; EngineSave keeps
them in one record, also when a new version's save directory changed, and restores a
version's saves into another version (``restore --retarget``)."""

from __future__ import annotations

import http.server
import json
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from enginesave import collections_link
from enginesave.cli import main
from enginesave.collections_link import CollectionsUnavailable, address, parse
from enginesave.repo import Repo
from fixtures.builder import BuiltGame, build_f4, build_game, simple_spec
from test_schemas import conforms


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(args: list[str], capsys) -> tuple[int, dict]:
    code, out, err = run([*args, "--json"], capsys)
    assert out, err
    return code, json.loads(out)


class Hero:
    """Hero 0.3 and 0.4: one game, but 0.4 keeps its saves under a new save directory."""

    def __init__(self, games_root: Path, saves_root: Path, tmp_path: Path) -> None:
        self.old = build_game(
            games_root,
            simple_spec("Hero-0.3", "Hero", "Hero-1600000000", name="Hero", version="0.3"),
            saves_root,
        )
        self.new = build_game(
            games_root,
            simple_spec(
                "Hero-0.4", "Hero", "Hero-1700000000", name="Hero", version="0.4", local_saves=1
            ),
            saves_root,
        )
        self.saves_root = saves_root
        self.games_root = games_root
        self.dest = tmp_path / "library"
        self.common = ["--renpy-saves-root", str(saves_root)]
        self.works = tmp_path / "collections.json"
        self.write_works()

    def write_works(self, *, enginesave_id: str | None = None) -> None:
        copies = [
            {"path": str(self.old.root), "version": "0.3", "status": "present"},
            {
                "path": str(self.new.root),
                "version": "0.4",
                "status": "present",
                "enginesave_id": enginesave_id,
            },
        ]
        doc = {
            "format_version": 1,
            "works": [
                {"work_id": "w-hero", "title": "Hero (Collections)", "copies": copies},
                {"work_id": "w-other", "title": "Other", "copies": [{"path": "relative\\x"}]},
            ],
        }
        self.works.write_text(json.dumps(doc), encoding="utf-8")

    def backup(self, game: BuiltGame, capsys, *extra: str) -> tuple[int, dict]:
        args = ["backup", "--game-root", str(game.root), *self.common, "--dest", str(self.dest)]
        return run_json([*args, "--yes", *extra], capsys)

    def records(self) -> list[dict]:
        return Repo.open(self.dest).load_catalog()["games"]


@pytest.fixture
def hero(games_root: Path, saves_root: Path, tmp_path: Path) -> Hero:
    return Hero(games_root, saves_root, tmp_path)


# ------------------------------------------------------------------------------- the source


def test_the_answer_is_read_leniently_and_checked() -> None:
    works = parse(
        json.dumps(
            {
                "works": [
                    {"work_id": "a", "title": "A", "copies": [{"path": "D:\\Games\\A-1"}]},
                    {"work_id": "", "copies": [{"path": "D:\\Games\\B"}]},  # no work id
                    {"work_id": "c", "copies": [{"path": "Games\\C"}, "x", {"path": 3}]},
                    "not a work",
                ],
                "extra": True,
            }
        ),
        "test",
    )
    assert [(c.work_id, c.path) for c in works.copies] == [("a", "D:\\Games\\A-1")]
    assert works.copy_at("d:\\games\\a-1") is not None, "folders compare as Windows does"
    with pytest.raises(CollectionsUnavailable, match="newer"):
        parse(json.dumps({"format_version": 2, "works": []}), "test")
    with pytest.raises(CollectionsUnavailable, match="no list of works"):
        parse("{}", "test")
    with pytest.raises(CollectionsUnavailable, match="not JSON"):
        parse("<html>", "test")


def test_a_server_address_gets_the_copies_path() -> None:
    assert address("http://127.0.0.1:7788") == "http://127.0.0.1:7788/api/enginesave/copies"
    assert address("http://127.0.0.1:7788/") == "http://127.0.0.1:7788/api/enginesave/copies"
    assert address("http://h:1/x/copies.json") == "http://h:1/x/copies.json"


def test_off_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(collections_link.ENV_VAR, "http://127.0.0.1:7788")
    assert collections_link.source_from(None) == "http://127.0.0.1:7788"
    assert collections_link.source_from("off") is None
    monkeypatch.delenv(collections_link.ENV_VAR)
    assert collections_link.source_from(None) is None


# ------------------------------------------------------------------------------- the scan


def test_a_new_version_with_another_save_directory_joins_the_record(hero: Hero, capsys) -> None:
    code, data = hero.backup(hero.old, capsys)
    assert code == 0
    (record,) = hero.records()
    scan = ["scan", str(hero.games_root), *hero.common, "--repo", str(hero.dest)]

    code, data = run_json(scan, capsys)  # without Collections: another game
    new = next(g for g in data["games"] if g["game_root"] == str(hero.new.root))
    assert code == 0 and new["known_game_id"] is None and "collections" not in new
    assert "collections" not in data

    code, data = run_json([*scan, "--collections", str(hero.works)], capsys)
    assert code == 0
    conforms("scan", data)
    assert data["collections"] == {
        "source": str(hero.works),
        "works": 1,
        "copies": 2,
        "error": None,
    }
    by_root = {g["game_root"]: g for g in data["games"]}
    new, old = by_root[str(hero.new.root)], by_root[str(hero.old.root)]
    assert new["known_game_id"] == record["game_id"]
    assert new["collections"]["record_by"] == "collections"
    assert new["collections"]["title"] == "Hero (Collections)"
    assert new["collections"]["version"] == "0.4"
    assert new["link_suggestions"] == []
    assert new["installs"][0]["collections"] == {"work_id": "w-hero", "version": "0.4"}
    assert old["known_game_id"] == record["game_id"]
    assert old["collections"]["record_by"] is None, "found by its save directory, as before"


def test_backup_puts_the_new_version_into_the_record_and_links_it(hero: Hero, capsys) -> None:
    hero.backup(hero.old, capsys)
    (record,) = hero.records()
    code, data = hero.backup(hero.new, capsys, "--collections", str(hero.works))
    assert code == 0
    conforms("backup", data)
    assert data["plan"]["games"][0]["collections"]["record_by"] == "collections"
    assert data["result"]["results"][0]["game_id"] == record["game_id"]
    (link,) = data["result"]["linked"]
    assert link["game_root"] == str(hero.new.root) and link["linked_by"] == "collections"
    assert len(hero.records()) == 1, "one game, one record"

    # later scans need no Collections: the folder is linked to the record
    code, data = hero.backup(hero.new, capsys, "--dry-run")
    assert code == 0 and data["plan"]["games"][0]["game_id"] == record["game_id"]


def test_two_new_versions_make_one_record(hero: Hero, capsys) -> None:
    code, data = run_json(
        [
            "backup",
            str(hero.games_root),
            *hero.common,
            "--dest",
            str(hero.dest),
            "--yes",
            "--collections",
            str(hero.works),
        ],
        capsys,
    )
    assert code == 0
    ids = {r["game_id"] for r in data["result"]["results"]}
    assert len(ids) == 1 and len(hero.records()) == 1
    repo = Repo.open(hero.dest)
    (game_id,) = ids
    assert len(repo.snapshot_ids(game_id)) == 2
    assert {i["game_root"] for i in repo.load_game(game_id)["installs"]} == {
        str(hero.old.root),
        str(hero.new.root),
    }


def test_several_records_are_offered_never_chosen(hero: Hero, capsys) -> None:
    hero.backup(hero.old, capsys)
    hero.backup(hero.new, capsys)  # without Collections: a record of its own
    old_id, new_id = (r["game_id"] for r in hero.records())
    third = build_game(
        hero.games_root,
        simple_spec("Hero-0.5", "Hero", "Hero-1800000000", name="Hero", version="0.5"),
        hero.saves_root,
    )
    doc = json.loads(hero.works.read_text(encoding="utf-8"))
    doc["works"][0]["copies"].append({"path": str(third.root)})
    hero.works.write_text(json.dumps(doc), encoding="utf-8")
    scan = ["scan", str(hero.games_root), *hero.common, "--repo", str(hero.dest)]
    code, data = run_json([*scan, "--collections", str(hero.works)], capsys)
    by_root = {g["game_root"]: g for g in data["games"]}
    newest = by_root[str(third.root)]
    assert newest["known_game_id"] is None
    offered = [s for s in newest["link_suggestions"] if s["reason"] == "collections_same_work"]
    assert {s["game_id"] for s in offered} == {old_id, new_id}
    assert by_root[str(hero.old.root)]["collections"]["other_records"] == [new_id]


def test_a_copy_names_its_record(hero: Hero, capsys) -> None:
    """Collections' enginesave_id of a copy is enough, also when the record does not know
    the folder (the other copy here is not even in the record)."""
    hero.backup(hero.old, capsys)
    (record,) = hero.records()
    hero.write_works(enginesave_id=record["game_id"])
    doc = json.loads(hero.works.read_text(encoding="utf-8"))
    doc["works"][0]["copies"][0]["path"] = "Z:\\gone\\Hero-0.3"
    hero.works.write_text(json.dumps(doc), encoding="utf-8")
    code, data = hero.backup(hero.new, capsys, "--collections", str(hero.works), "--dry-run")
    assert code == 0 and data["plan"]["games"][0]["game_id"] == record["game_id"]


def test_collections_that_cannot_be_read_is_a_warning(hero: Hero, tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nothing.json"
    code, data = run_json(
        ["scan", str(hero.games_root), *hero.common, "--collections", str(missing)], capsys
    )
    assert code == 0
    conforms("scan", data)
    assert data["collections"]["error"] and data["collections"]["copies"] == 0
    assert any(w.startswith("Collections not used") for w in data["warnings"])


@pytest.fixture
def server(hero: Hero) -> Iterator[str]:
    body = hero.works.read_bytes()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the library's name
            if self.path != collections_link.ENDPOINT:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_collections_server_is_read(hero: Hero, server: str, capsys) -> None:
    code, data = run_json(
        ["scan", str(hero.games_root), *hero.common, "--collections", server], capsys
    )
    assert code == 0 and data["collections"]["copies"] == 2
    assert data["collections"]["source"] == f"{server}/api/enginesave/copies"
    code, data = run_json(
        ["scan", str(hero.games_root), *hero.common, "--collections", f"{server}/old"], capsys
    )
    assert "404" in data["collections"]["error"]


# ------------------------------------------------------------------------------- restoring


def test_restoring_into_a_version_with_another_save_directory(hero: Hero, capsys) -> None:
    hero.backup(hero.old, capsys)
    (record,) = hero.records()
    restore = [
        "restore",
        "--repo",
        str(hero.dest),
        "--game",
        record["game_id"],
        "--game-root",
        str(hero.new.root),
        *hero.common,
    ]
    code, _, err = run([*restore, "--dry-run"], capsys)
    assert code == 2 and "--retarget" in err, "refused, and told how"

    code, data = run_json([*restore, "--retarget", "--dry-run"], capsys)
    assert code == 0
    conforms("restore", data)
    plan = data["plan"]
    assert plan["retargeted"] == [
        {"source_id": "global", "from": "Hero-1600000000", "to": "Hero-1700000000"}
    ]
    targets = {t["source_id"]: t["target_dir"] for t in plan["targets"]}
    assert targets["global"] == str(hero.saves_root / "Hero-1700000000")
    assert targets["local"] == str(hero.new.local_dir)

    code, data = run_json([*restore, "--retarget", "--yes"], capsys)
    assert code == 0 and data["result"]["linked"] == str(hero.new.root)
    for name, content in hero.old.global_files.items():
        assert (hero.saves_root / "Hero-1700000000" / name).read_bytes() == content
    for name, content in hero.old.local_files.items():
        assert (hero.new.local_dir / name).read_bytes() == content
    assert len(hero.records()) == 1

    # linked now: its next backup goes into this record, and that snapshot restores into
    # the new version without --retarget (it is that version's own)
    code, data = hero.backup(hero.new, capsys)
    assert data["result"]["results"][0]["game_id"] == record["game_id"]
    code, data = run_json([*restore, "--dry-run"], capsys)
    assert code == 0 and data["plan"]["retargeted"] == []


def test_from_install_is_not_ignored_with_one_local_source(hero: Hero, capsys) -> None:
    hero.backup(hero.old, capsys)
    (record,) = hero.records()
    code, _, err = run(
        [
            "restore",
            "--repo",
            str(hero.dest),
            "--game",
            record["game_id"],
            "--game-root",
            str(hero.old.root),
            "--from-install",
            str(hero.new.root),
            "--dry-run",
            *hero.common,
        ],
        capsys,
    )
    assert code == 2 and "no local saves of that install" in err


# --------------------------------------------------------------------- naming a record (1.3.0)


def test_a_record_is_named_by_its_folder_or_a_game_folder(hero: Hero, capsys) -> None:
    hero.backup(hero.old, capsys)
    code, data = run_json(["list", "--repo", str(hero.dest)], capsys)
    conforms("list", data)
    (row,) = data["games"]
    assert row["folder"] == "Hero"
    for ref in ("Hero", str(hero.old.root)):
        code, data = run_json(["show", "--repo", str(hero.dest), "--game", ref], capsys)
        assert code == 0 and data["game"]["game_id"] == row["game_id"]
    code, _, err = run(["show", "--repo", str(hero.dest), "--game", "nothing"], capsys)
    assert code == 2 and "enginesave list" in err


def test_list_and_show_write_a_report(hero: Hero, tmp_path: Path, capsys) -> None:
    hero.backup(hero.old, capsys)
    report = tmp_path / "list.json"
    code, data = run_json(["list", "--repo", str(hero.dest), "--report", str(report)], capsys)
    assert code == 0 and json.loads(report.read_text(encoding="utf-8")) == data
    inside = hero.dest / "list.json"
    code, _, err = run(["list", "--repo", str(hero.dest), "--report", str(inside)], capsys)
    assert code == 2 and "--report" in err
    shown = tmp_path / "show.json"
    code, data = run_json(
        ["show", "--repo", str(hero.dest), "--game", "Hero", "--snapshot", "latest", "--report",
         str(shown)],
        capsys,
    )  # fmt: skip
    assert code == 0 and json.loads(shown.read_text(encoding="utf-8"))["manifest"]


# ------------------------------------------------------------ the usability review (1.3.0)


def test_a_linked_version_restores_into_its_own_places(hero: Hero, capsys) -> None:
    """A folder the user linked to the record is that game's: no --retarget needed."""
    hero.backup(hero.old, capsys)
    (record,) = hero.records()
    code, _ = run_json(
        ["link", "--repo", str(hero.dest), "--game", "Hero", "--game-root", str(hero.new.root)],
        capsys,
    )
    assert code == 0
    code, data = run_json(
        ["restore", "--repo", str(hero.dest), "--game", "Hero", "--game-root",
         str(hero.new.root), "--dry-run", *hero.common],
        capsys,
    )  # fmt: skip
    assert code == 0
    assert data["plan"]["retargeted"][0]["to"] == "Hero-1700000000"
    assert any("belongs to this record" in note for note in data["plan"]["notes"])


def test_a_new_record_names_the_similar_one(hero: Hero, capsys) -> None:
    hero.backup(hero.old, capsys)
    args = ["backup", "--game-root", str(hero.new.root), *hero.common, "--dest", str(hero.dest)]
    code, out, _ = run([*args, "--dry-run"], capsys)
    assert code == 0 and "new game record" in out
    assert "similar record: Hero (" in out and "enginesave link --repo" in out


@pytest.fixture
def grouped(games_root: Path, saves_root: Path, tmp_path: Path):
    v1, v2 = build_f4(games_root, saves_root)  # one save directory: one game, two installs
    return v1, v2, tmp_path / "library", ["--renpy-saves-root", str(saves_root)]


def test_unchanged_is_measured_against_the_same_install(grouped, capsys) -> None:
    v1, v2, dest, common = grouped
    auto = ["--skip-unchanged", "--reason", "auto", "--yes"]

    def backup(game: BuiltGame) -> dict:
        args = ["backup", "--game-root", str(game.root), *common, "--dest", str(dest), *auto]
        return run_json(args, capsys)[1]["result"]["results"][0]

    first = backup(v1)
    assert backup(v2)["status"] == "ok"
    again = backup(v1)  # the latest is v2's: compared with v1's own snapshot
    assert again["status"] == "unchanged" and first["snapshot_id"] in again["message"]
    assert backup(v2)["status"] == "unchanged"


def test_restoring_a_grouped_snapshot_into_one_of_its_installs(grouped, capsys) -> None:
    v1, v2, dest, common = grouped
    run_json(["backup", str(v1.root.parent), *common, "--dest", str(dest), "--yes"], capsys)
    restore = ["restore", "--repo", str(dest), "--game", "Game A", *common, "--dry-run"]
    code, data = run_json([*restore, "--game-root", str(v2.root)], capsys)
    assert code == 0 and data["plan"]["from_install"] == str(v2.root)
    other = v2.root.parent / "elsewhere"
    shutil.copytree(v2.root, other)
    code, _, err = run([*restore, "--game-root", str(other)], capsys)
    assert code == 2 and f"local: {v1.root}" in err and "(2.0)" in err


def test_small_command_line_fixes(hero: Hero, tmp_path: Path, capsys) -> None:
    hero.backup(hero.old, capsys)
    hero.backup(hero.old, capsys)
    repo = Repo.open(hero.dest)
    (record,) = repo.load_catalog()["games"]
    latest = repo.latest_snapshot(record["game_id"])
    # export to a folder that is not there: refused, no file without .zip
    target = tmp_path / "exports"
    code, _, err = run(
        ["export", "--repo", str(hero.dest), "--game", "Hero", "--to", str(target)], capsys
    )
    assert code == 2 and "not a .zip file name" in err and not target.exists()
    # remove --snapshot latest, as the other commands take it
    code, data = run_json(
        ["remove", "--repo", str(hero.dest), "--game", "Hero", "--snapshot", "latest"], capsys
    )
    assert code == 0 and latest in json.dumps(data["plan"])
    # confirm-source without --source-id
    folder = tmp_path / "Saves Elsewhere"
    folder.mkdir()
    code, data = run_json(
        ["confirm-source", "--repo", str(hero.dest), "--game", "Hero", "--path", str(folder)],
        capsys,
    )
    assert code == 0 and data["source_id"] == "Saves Elsewhere"
