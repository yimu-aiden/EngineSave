"""The ``--json`` outputs of scan/backup/restore conform to ``docs/schemas`` (plan M4).

A small validator for the JSON Schema subset the schemas use keeps the project free of a
``jsonschema`` dependency: type, const, enum, required, properties, additionalProperties,
items and local ``$ref``.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from enginesave.cli import main
from fixtures.builder import build_f1, build_f2, build_f5, build_f7, build_f8, build_f10, build_f18

SCHEMAS = Path(__file__).resolve().parent.parent / "docs" / "schemas"
TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(schema: dict, value: Any, root: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(TYPES[t](value) for t in types):
            return [f"{path}: expected {types}, got {type(value).__name__}"]
        if value is None:
            return []
    if "$ref" in schema:
        errors.extend(validate(root["$defs"][schema["$ref"].split("/")[-1]], value, root, path))
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors.extend(f"{path}: missing {k}" for k in schema.get("required", []) if k not in value)
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}: unexpected {k}" for k in value if k not in props)
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(sub, value[key], root, f"{path}.{key}"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(schema["items"], item, root, f"{path}[{i}]"))
    return errors


def load(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def conforms(name: str, data: Any) -> None:
    schema = load(name)
    assert validate(schema, data, schema) == []


def run_json(args: list[str], capsys: pytest.CaptureFixture[str]) -> Any:
    main([*args, "--json"])
    return json.loads(capsys.readouterr().out)


def test_schema_files_are_well_formed() -> None:
    names = ("scan", "backup", "restore", "export", "import", "cleanup")
    for name in (*names, "export-batch", "import-batch", "remove", "remove-tmp", "mirror", "list"):
        schema = load(name)
        assert schema["$schema"].startswith("https://json-schema.org/")
        refs = {m for m in json.dumps(schema).split('"') if m.startswith("#/$defs/")}
        assert {r.split("/")[-1] for r in refs} <= set(schema.get("$defs", {}))


def test_validator_catches_drift() -> None:
    schema = load("scan")
    assert validate(schema, {"format_version": 2}, schema)
    bad_game = {"games": [{"status": "MAYBE"}]}
    assert any("MAYBE" in e for e in validate(schema, bad_game, schema))


def test_scan_output_conforms(games_root: Path, saves_root: Path, capsys) -> None:
    build_f1(games_root, saves_root)
    build_f2(games_root, saves_root)
    build_f5(games_root, saves_root)
    build_f7(games_root)
    build_f8(saves_root)
    build_f10(games_root)
    build_f18(games_root, saves_root)  # 0.8.0: shared_global_with
    from fixtures.engines import build_kirikiri

    # 1.0.0 (D70 7): a source in a root token of 0.11.0 (the sandbox's 「文档」, conftest)
    build_kirikiri(games_root, "KiriKiri Docs", cf="$(personalpath)\\Maker\\Docs")
    data = run_json(["scan", str(games_root), "--renpy-saves-root", str(saves_root)], capsys)
    tokens = {s["root_token"] for g in data["games"] for s in g["sources"]}
    assert "DOCUMENTS" in tokens
    assert {g["status"] for g in data["games"]} >= {"CONFIRMED", "CANDIDATE", "UNSUPPORTED"}
    assert sum(1 for g in data["games"] if g["shared_global_with"]) == 2
    assert data["skipped"] and data["unlinked"]
    conforms("scan", data)


def test_backup_and_restore_outputs_conform(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    built = build_f1(games_root, saves_root)
    build_f8(saves_root)
    repo = tmp_path / "repo"
    common = ["--renpy-saves-root", str(saves_root)]
    backup = ["backup", str(games_root), *common, "--dest", str(repo), "--include-unlinked"]
    conforms("backup", run_json([*backup, "--dry-run"], capsys))
    done = run_json([*backup, "--yes"], capsys)
    assert [r["status"] for r in done["result"]["results"]] == ["ok", "ok", "ok"]
    conforms("backup", done)

    shutil.rmtree(built.local_dir)
    restore = ["restore", "--repo", str(repo), "--game", "readable game", *common]
    conforms("restore", run_json([*restore, "--dry-run"], capsys))
    (built.global_dir / "1-1-LT1.save").write_bytes(b"newer")
    restored = run_json([*restore, "--yes"], capsys)
    assert restored["result"]["safety_snapshot"] is not None
    conforms("restore", restored)


def test_export_and_import_outputs_conform(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    build_f1(games_root, saves_root)
    repo = tmp_path / "repo"
    common = ["--renpy-saves-root", str(saves_root)]
    run_json(["backup", str(games_root), *common, "--dest", str(repo), "--yes"], capsys)
    archive = tmp_path / "export.zip"
    exported = run_json(
        ["export", "--repo", str(repo), "--game", "readable game", "--to", str(archive), *common],
        capsys,
    )
    conforms("export", exported)
    imported = run_json(["import", "--repo", str(repo), "--from", str(archive), "--yes"], capsys)
    assert imported["plan"]["matched_by"] == "game_id" and imported["reason"] is None
    conforms("import", imported)
    skipped = run_json(["import", "--repo", str(repo), "--from", str(archive)], capsys)
    assert skipped["result"] is None and skipped["reason"] == "already imported"
    conforms("import", skipped)
    restore = ["restore", "--repo", str(repo), "--game", "readable game", "--dry-run", *common]
    planned = run_json(restore, capsys)
    assert planned["plan"]["snapshot_reason"] == "import"
    conforms("restore", planned)


def test_batch_export_and_folder_import_outputs_conform(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    build_f1(games_root, saves_root)
    build_f8(saves_root)
    repo = tmp_path / "repo"
    common = ["--renpy-saves-root", str(saves_root)]
    backup = ["backup", str(games_root), *common, "--dest", str(repo), "--include-unlinked"]
    run_json([*backup, "--yes"], capsys)
    folder = tmp_path / "batch"
    exported = run_json(
        ["export", "--repo", str(repo), "--all", "--to", str(folder), *common], capsys
    )
    assert exported["exported"] == 3
    conforms("export-batch", exported)
    skipped = run_json(
        ["export", "--repo", str(repo), "--all", "--to", str(folder), *common], capsys
    )
    assert skipped["skipped"] == 3 and skipped["items"][0]["export"] is None
    conforms("export-batch", skipped)
    (folder / "zz-broken.zip").write_bytes(b"broken")
    imported = tmp_path / "imported"
    command = ["import", "--repo", str(imported), "--from", str(folder), "--yes"]
    data = run_json(command, capsys)
    assert (data["imported"], data["skipped"], data["failed"]) == (3, 0, 1)
    conforms("import-batch", data)
    again = run_json(command, capsys)
    assert (again["imported"], again["skipped"], again["failed"]) == (0, 3, 1)
    assert {i["reason"] for i in again["items"] if i["result"] == "skipped"} == {"already imported"}
    conforms("import-batch", again)


def test_cleanup_outputs_conform(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    build_f1(games_root, saves_root)
    repo = tmp_path / "repo"
    common = ["--renpy-saves-root", str(saves_root)]
    run_json(["backup", str(games_root), *common, "--dest", str(repo), "--yes"], capsys)
    base = ["cleanup", "--repo", str(repo), "--game", "readable game", *common]
    base += ["--game-root", "--global", "--permanent"]
    planned = run_json([*base, "--dry-run"], capsys)
    assert planned["plan"]["ok"] and planned["result"] is None
    conforms("cleanup", planned)
    done = run_json([*base, "--yes"], capsys)
    assert [t["result"] for t in done["result"]["targets"]] == ["deleted", "deleted"]
    conforms("cleanup", done)


def test_remove_outputs_conform(games_root: Path, saves_root: Path, tmp_path: Path, capsys) -> None:
    """0.8.0 (D49): the preview and the removal."""
    build_f1(games_root, saves_root)
    repo = tmp_path / "repo"
    common = ["--renpy-saves-root", str(saves_root)]
    run_json(["backup", str(games_root), "--dest", str(repo), "--yes", *common], capsys)
    args = ["remove", "--repo", str(repo), "--game", "readable game"]
    preview = run_json(args, capsys)
    assert preview["dry_run"] is True and preview["result"] is None
    conforms("remove", preview)
    done = run_json([*args, "--yes"], capsys)
    assert done["result"]["records"] == 1 and done["result"]["objects_removed"] > 0
    conforms("remove", done)


def test_maintenance_outputs_conform(
    games_root: Path, saves_root: Path, tmp_path: Path, capsys
) -> None:
    """0.9.0: ``list`` with ``stats`` (D56), ``remove --tmp`` (D55) and ``mirror`` (D57)."""
    build_f1(games_root, saves_root)
    repo = tmp_path / "repo"
    common = ["--renpy-saves-root", str(saves_root)]
    run_json(["backup", str(games_root), "--dest", str(repo), "--yes", *common], capsys)
    old = repo / "objects" / ".tmp" / "0123abcd"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"left by an interrupted backup")
    os.utime(old, (1_600_000_000, 1_600_000_000))
    listed = run_json(["list", "--repo", str(repo)], capsys)
    conforms("list", listed)
    assert listed["stats"]["records"] == 1 and listed["stats"]["last_mirror"] is None
    preview = run_json(["remove", "--repo", str(repo), "--tmp"], capsys)
    conforms("remove-tmp", preview)
    assert [t["recent"] for t in preview["tmp"]] == [False] and preview["result"] is None
    done = run_json(["remove", "--repo", str(repo), "--tmp", "--yes"], capsys)
    conforms("remove-tmp", done)
    assert done["result"]["removed"] == [str(old)] and not old.exists()
    mirrored = run_json(["mirror", "--repo", str(repo), "--to", str(tmp_path / "m")], capsys)
    conforms("mirror", mirrored)
    assert mirrored["verified"] is True
    conforms("mirror", run_json(["mirror", "--repo", str(repo), "--to", str(tmp_path / "m"),
                                 "--no-verify"], capsys))  # fmt: skip
    assert run_json(["list", "--repo", str(repo)], capsys)["stats"]["last_mirror"]
