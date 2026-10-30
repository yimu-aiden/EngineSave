"""Release smoke test in .local (as for 0.5.0, EXECUTION_LOG M19): only .local is written; the
global saves come from .local/fake-appdata; the samples folder (ENGINESAVE_SAMPLES, default
Test/) and the fake saves are compared before and
after. usage: .venv\\Scripts\\python.exe tools\\release_smoke.py . TAG"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

repo, tag = Path(sys.argv[1]), sys.argv[2]
samples = Path(os.environ.get("ENGINESAVE_SAMPLES") or repo / "Test")  # only read
local = repo / ".local"
cli = repo / ".venv" / "Scripts" / "enginesave.exe"
fake = local / "fake-appdata"
library, exports, imported, mirrored = (
    local / f"smoke-{tag}",
    local / "exports" / f"smoke-{tag}",
    local / f"import-smoke-{tag}",
    local / f"mirror-smoke-{tag}",
)
for folder in (library, exports, imported, mirrored):
    if folder.exists():
        assert local in folder.parents
        shutil.rmtree(folder)


def state(folder: Path) -> dict[str, list[int]]:
    return {
        str(p.relative_to(folder)): [p.stat().st_size, p.stat().st_mtime_ns]
        for p in folder.rglob("*")
    }


def run(*args: object) -> dict:
    command = [str(cli), *map(str, args), "--renpy-saves-root", str(fake), "--json"]
    # 0.9.0: the CLI's parse cache goes under .local, never the real %APPDATA%\EngineSave
    env = {**os.environ, "APPDATA": str(local / f"smoke-appdata-{tag}")}
    # 0.10.0: the other engines' profile folders are .local folders too (Test\ has none)
    for token in (  # 0.11.0: the new tokens too
        "UNITY_LOCALLOW",
        "LOCALAPPDATA",
        "APPDATA",
        "DOCUMENTS",
        "SAVED_GAMES",
        "VIRTUALSTORE",
    ):
        env[f"ENGINESAVE_ROOT_{token}"] = str(local / f"smoke-profile-{tag}" / token)
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=repo,
        env=env,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = None
    return {"exit": proc.returncode, "data": data, "stderr": proc.stderr[-300:]}


before = {"samples": state(samples), "fake": state(fake)}
steps = []
r = run("backup", samples, "--dest", library, "--yes")
results = r["data"]["result"]["results"]
steps.append(
    ("backup", r["exit"], [(x["display_name"], x["status"], x["file_count"]) for x in results])
)
r = run("verify", "--repo", library, "--objects")
steps.append(("verify --objects", r["exit"], None))
r = run("list", "--repo", library)
steps.append(
    (
        "list (0.7.0)",
        r["exit"],
        [(g["display_name"], g.get("install_present")) for g in r["data"]["games"]],
    )
)
# the per-game steps take the first record whose game folder is there
game = next(g["game_id"] for g in r["data"]["games"] if g.get("install_present"))
keys = ("new", "objects_copied", "snapshots_copied", "storage", "verified", "failed")
r = run("mirror", "--repo", library, "--to", mirrored)  # 0.9.0 (D57), into .local
steps.append(("mirror (0.9.0)", r["exit"], {k: r["data"][k] for k in keys}))
r = run("mirror", "--repo", library, "--to", mirrored)
steps.append(("mirror again", r["exit"], {k: r["data"][k] for k in keys}))
r = run("verify", "--repo", mirrored, "--objects")
steps.append(("verify mirror --objects", r["exit"], None))
r = run("list", "--repo", library)
steps.append(("list stats (0.9.0)", r["exit"], r["data"]["stats"]))
r = run("remove", "--repo", library, "--tmp")  # 0.9.0 (D55): a list only
steps.append(("remove --tmp (0.9.0)", r["exit"], r["data"]))
r = run("remove", "--repo", library, "--game", game)  # 0.8.0: a preview only
plan = r["data"]["plan"]
steps.append(
    (
        "remove preview (0.8.0)",
        r["exit"],
        {
            "result": r["data"]["result"],
            "records": plan["records"],
            "snapshots": plan["snapshots"],
            "freed": plan["freed_bytes"],
            "blocked": plan["gc_blocked"],
        },
    )
)
r = run("cleanup", "--repo", library, "--game", game, "--game-root", "--global", "--dry-run")
plan = r["data"]["plan"]
steps.append(
    (
        "cleanup --dry-run",
        r["exit"],
        {
            "targets": [(t["kind"], t["ok"], t["files"], t["bytes"]) for t in plan["targets"]],
            "recycle_bins": plan.get("recycle_bins"),
        },
    )
)
r = run("export", "--repo", library, "--all", "--to", exports)
steps.append(("export --all", r["exit"], [i["result"] for i in r["data"]["items"]]))
r = run("import", "--repo", imported, "--from", exports, "--yes")
steps.append(("import folder", r["exit"], [i["result"] for i in r["data"]["items"]]))
r = run("import", "--repo", imported, "--from", exports, "--yes")
steps.append(("import again", r["exit"], [i["result"] for i in r["data"]["items"]]))
r = run("verify", "--repo", imported, "--objects")
steps.append(("verify import --objects", r["exit"], None))
r = run("list", "--repo", imported)
steps.append(("list", r["exit"], len(r["data"]["games"])))
after = {"samples": state(samples), "fake": state(fake)}
changed = {
    k: sum(1 for p in before[k].keys() | after[k].keys() if before[k].get(p) != after[k].get(p))
    for k in before
}
for step in steps:
    print(json.dumps(step, ensure_ascii=False))
print("entries", {k: len(v) for k, v in after.items()}, "changed", changed)
for k in before:
    for p in sorted(before[k].keys() | after[k].keys()):
        if before[k].get(p) != after[k].get(p):
            print("  changed", k, p, before[k].get(p), after[k].get(p))
