"""The frozen build knows every engine (0.10.0): synthetic fixtures of the nine engines are
built in .local\\engine-fixtures (dummy bytes, never run), their profile folders in
.local\\engine-profile; the portable CLI scans them with ENGINESAVE_ROOT_* and APPDATA pointing
into .local, so no real profile folder is read; the registry is only asked (read-only) for a
key no game has. Prints each game's engine and status, and whether the source CLI agrees.

    .venv\\Scripts\\python.exe tools\\portable_engines.py <repo>
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
local = repo / ".local"
fixtures_dir, profile = local / "engine-fixtures", local / "engine-profile"
for folder in (fixtures_dir, profile):
    if folder.exists():
        assert local in folder.parents
        shutil.rmtree(folder)
env = {
    **os.environ,
    "ENGINESAVE_ROOT_UNITY_LOCALLOW": str(profile / "LocalLow"),
    "ENGINESAVE_ROOT_LOCALAPPDATA": str(profile / "Local"),
    "ENGINESAVE_ROOT_APPDATA": str(profile / "Roaming"),
    # 0.11.0 (D69 3): never the real Documents, Saved Games or VirtualStore
    "ENGINESAVE_ROOT_DOCUMENTS": str(profile / "Documents"),
    "ENGINESAVE_ROOT_SAVED_GAMES": str(profile / "Saved Games"),
    "ENGINESAVE_ROOT_VIRTUALSTORE": str(profile / "Local" / "VirtualStore"),
    "APPDATA": str(profile / "Roaming"),
    "ENGINESAVE_RENPY_SAVES_ROOT": str(local / "fake-appdata"),
}
os.environ.update({k: v for k, v in env.items() if k.startswith("ENGINESAVE_ROOT_")})
sys.path.insert(0, str(repo / "tests"))
from fixtures import engines as fx  # noqa: E402

for build in (
    fx.build_mv, fx.build_mz, lambda p: fx.build_rgss(p, "xp"), lambda p: fx.build_rgss(p, "vx"),
    lambda p: fx.build_rgss(p, "vxace"), fx.build_wolf, fx.build_kirikiri, fx.build_unity,
    fx.build_electron, fx.build_unreal, fx.build_godot,
    # 0.11.0 (D69): a Godot pack that names its project, RPG Maker MV in Electron, KiriKiri
    # saving in 「文档」, an mkxp game with mkxp.json
    lambda p: fx.build_godot(p, "Fixture Godot Pck", exe="moon", project="Moon Garden",
                             pck_version=3),
    lambda p: fx.build_electron(
        p, "Fixture Electron MV", product="maker-quest", game=False,
        extra={"www/js/rpg_core.js": b"//", "www/data/System.json": b'{"gameTitle": "Maker"}'},
    ),
    lambda p: fx.build_kirikiri(p, "Fixture KiriKiri Docs", cf="$(personalpath)\\M\\T"),
    lambda p: (
        fx.build_rgss(p, "xp", "Fixture mkxp", saves=False),
        (p / "Fixture mkxp" / "mkxp.json").write_bytes(b'{"dataPathOrg": "T", "dataPathApp": "M"}'),
    ),
):  # fmt: skip
    build(fixtures_dir)


def scan(cli: Path) -> dict:
    proc = subprocess.run(
        [str(cli), "scan", str(fixtures_dir), "--json", "--no-cache"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=repo,
    )  # fmt: skip
    data = json.loads(proc.stdout)
    data.pop("timing", None)
    data.pop("generated_at", None)
    return {"exit": proc.returncode, "data": data, "traceback": "Traceback" in proc.stderr}


frozen = scan(repo / "dist" / "EngineSave" / "enginesave-cli.exe")
source = scan(repo / ".venv" / "Scripts" / "enginesave.exe")
games = sorted(
    (Path(g["game_root"]).name, g["engine"], g["status"]) for g in frozen["data"]["games"]
)
print(json.dumps({
    "frozen_exit": frozen["exit"], "frozen_traceback": frozen["traceback"],
    "games": games, "same_as_source": frozen["data"] == source["data"],
}, ensure_ascii=False, indent=1))  # fmt: skip
