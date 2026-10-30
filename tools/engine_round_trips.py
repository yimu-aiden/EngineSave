"""Scan -> back up -> delete -> restore of every engine fixture, for the execution log. Runs
in ``.local\\engine-round-trips`` (emptied first): every profile root token, APPDATA and the
Ren'Py saves root point into it, and Unity's PlayerPrefs come from a fake reader (the
registry is never read or written).

    .venv\\Scripts\\python.exe tools\\engine_round_trips.py .
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

repo_dir = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(repo_dir / "src"), str(repo_dir / "tests")]

base = repo_dir / ".local" / "engine-round-trips"
if base.exists():
    shutil.rmtree(base)
base.mkdir(parents=True)
if True:  # the sandbox: every token in it (1.0.0: the six of 0.11.0)
    for token, sub in (
        ("UNITY_LOCALLOW", "LocalLow"),
        ("LOCALAPPDATA", "Local"),
        ("APPDATA", "Roaming"),
        ("DOCUMENTS", "Documents"),
        ("SAVED_GAMES", "Saved Games"),
        ("VIRTUALSTORE", "Local/VirtualStore"),
    ):
        os.environ[f"ENGINESAVE_ROOT_{token}"] = str(base / "profile" / sub)
    os.environ["APPDATA"] = str(base / "profile" / "Roaming")
    os.environ["ENGINESAVE_RENPY_SAVES_ROOT"] = str(base / "renpy-saves")
    os.environ.pop("ENGINESAVE_ROOT_RENPY_SAVES_ROOT", None)

    from enginesave import registry
    from enginesave.backup import execute_backup, prepare_backup
    from enginesave.registry import REG_BINARY, REG_DWORD, Value
    from enginesave.repo import Repo
    from enginesave.restore import execute_restore, plan_restore
    from enginesave.scan import ScanOptions, run_scan
    from fixtures import engines as fx

    prefs = {
        "software\\fixture co\\fixture game": [
            Value("level_h1", REG_DWORD, (3).to_bytes(4, "little")),
            Value("volume_h2", REG_DWORD, b"\x00\x00\x00\x00\x00\x00\xe8\x3f"),
            Value("name_h3", REG_BINARY, b"Hero\x00"),
        ]
    }
    registry.reader = lambda subkey: prefs.get(subkey.casefold())

    cases = [
        ("RPG Maker MV", lambda p: fx.build_mv(p), {}),
        ("RPG Maker MZ", lambda p: fx.build_mz(p), {}),
        ("RPG Maker XP", lambda p: fx.build_rgss(p, "xp"), {}),
        ("RPG Maker VX", lambda p: fx.build_rgss(p, "vx"), {}),
        ("RPG Maker VX Ace", lambda p: fx.build_rgss(p, "vxace", encrypted=False), {}),
        ("Wolf RPG Editor", lambda p: fx.build_wolf(p), {}),
        ("KiriKiri", lambda p: fx.build_kirikiri(p), {}),
        ("Unity", lambda p: fx.build_unity(p), {}),
        ("Electron (asar)", lambda p: fx.build_electron(p), {}),
        ("Unreal (LOCALAPPDATA)", lambda p: fx.build_unreal(p), {}),
        ("Godot (candidate)", lambda p: fx.build_godot(p), {"include_candidates": True}),
    ]
    print(
        "| 引擎 | 扫描状态 | 来源（令牌/相对路径：文件数） "
        "| 备份文件数 | 删后恢复写回 | 逐字节一致 |"
    )
    print("| --- | --- | --- | --- | --- | --- |")
    for index, (label, build, options) in enumerate(cases):
        games_root = base / f"games{index}"
        built = build(games_root)
        (game,) = run_scan(ScanOptions(roots=[games_root])).games
        sources = "；".join(
            f"{s.root_token.value}/{s.relative_root}：{s.file_count}（{s.status.value}）"
            for s in game.sources
        )
        plan = prepare_backup([games_root], base / f"library{index}", **options)
        library = Repo.open_or_create(plan.dest)
        assert execute_backup(plan, library).exit_code == 0
        game_id = library.load_catalog()["games"][0]["game_id"]
        manifest = library.load_manifest(game_id, library.latest_snapshot(game_id))
        for path in built.saves:
            path.unlink()
        reg = built.root / "PlayerPrefs.reg"
        result = execute_restore(plan_restore(library, library.load_catalog(), game_id))
        same = all(path.read_bytes() == data for path, data in built.saves.items())
        if label == "Unity":
            same = same and reg.is_file()
        print(
            f"| {label} | {game.status.value} | {sources} | {len(manifest['files'])} | "
            f"{result.written}（失败 {len(result.failures)}） | {'是' if same else '否'} |"
        )
