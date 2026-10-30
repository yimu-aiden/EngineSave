"""Windowless checks of the portable build (plan section 19 M15, section 21 M24).

usage: .venv\\Scripts\\python.exe tools\\portable_checks.py . VERSION

Read-only except for dist/EngineSave/config (the self-test's settings and log, beside the
portable programs). %APPDATA%\\EngineSave is compared by names, sizes and times only; gui.json
is never opened. The samples folder is compared by sizes and times before and after.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

repo, version = Path(sys.argv[1]), sys.argv[2]
dist = repo / "dist" / "EngineSave"
cli, gui = dist / "enginesave-cli.exe", dist / "EngineSave.exe"
# a folder of your own sample games, only read (default: Test\ beside the code)
samples = Path(os.environ.get("ENGINESAVE_SAMPLES") or repo / "Test")
source_cli = repo / ".venv" / "Scripts" / "enginesave.exe"
results: dict[str, object] = {}
# 0.11.0: every root token in .local (the checks scan only Ren'Py samples, but no run may
# resolve the real Documents, Saved Games or another profile folder)
for _token in (
    "UNITY_LOCALLOW",
    "LOCALAPPDATA",
    "APPDATA",
    "DOCUMENTS",
    "SAVED_GAMES",
    "VIRTUALSTORE",
):
    os.environ[f"ENGINESAVE_ROOT_{_token}"] = str(repo / ".local" / "portable-profile" / _token)


def state(folder: Path) -> dict[str, list[int]]:
    if not folder.exists():
        return {}
    return {
        str(p.relative_to(folder)): [p.stat().st_size, p.stat().st_mtime_ns]
        for p in folder.rglob("*")
    }


def run(command: list, **options) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=repo,
        **options,
    )


appdata = Path(os.environ["APPDATA"]) / "EngineSave"
appdata_before, samples_before = state(appdata), state(samples)
config = dist / "config"
results["config_before_self_test"] = config.exists()

# 1. version
proc = run([cli, "--version"])
results["version"] = proc.stdout.strip()
results["version_ok"] = proc.returncode == 0 and proc.stdout.strip() == f"enginesave {version}"

# 2. frozen scan vs source scan
frozen = run([cli, "scan", samples, "--json", "--no-cache"])  # 0.9.0: no cache written
source = run([source_cli, "scan", samples, "--json", "--no-cache"])  # nor the real one
reports = []
for proc in (frozen, source):
    data = json.loads(proc.stdout)
    for key in ("timing", "generated_at"):
        data.pop(key, None)
    reports.append(data)
differences = [
    key
    for key in reports[0].keys() | reports[1].keys()
    if reports[0].get(key) != reports[1].get(key)
]
results["scan_exit_codes"] = [frozen.returncode, source.returncode]
results["scan_differences"] = differences
games = reports[0]["games"]
results["scan_summary"] = {
    "confirmed": sum(g["status"] == "CONFIRMED" for g in games),
    "unlinked": len(reports[0].get("unlinked", [])),
    "skipped": len(reports[0].get("skipped", [])),
}

# 3. every subcommand's module is in the frozen build (lazy imports, D38): each command loads
#    its module and then stops at the missing repository, without a traceback
missing = repo / ".local" / "does-not-exist"
commands = {
    "list": ["list", "--repo", missing],
    "show": ["show", "--repo", missing, "--game", "x"],
    "verify": ["verify", "--repo", missing],
    "restore": ["restore", "--repo", missing, "--game", "x", "--dry-run"],
    "link": ["link", "--repo", missing, "--game", "x", "--game-root", missing],
    "confirm-source": [
        "confirm-source",
        "--repo",
        missing,
        "--game",
        "x",
        "--source-id",
        "s",
        "--path",
        missing,
    ],
    "export": ["export", "--repo", missing, "--game", "x", "--to", missing],
    "import": ["import", "--repo", missing, "--from", missing],
    "cleanup": ["cleanup", "--repo", missing, "--game", "x", "--global", "--dry-run"],
    "backup": ["backup", missing, "--dest", missing, "--dry-run"],
    "remove": ["remove", "--repo", missing, "--game", "x"],
    "remove --tmp": ["remove", "--repo", missing, "--tmp"],  # 0.9.0
    "mirror": ["mirror", "--repo", missing, "--to", missing / "mirror"],  # 0.9.0
    "mirror --repair": ["mirror", "--repo", missing, "--to", missing / "mirror", "--repair"],
    "backup --no-cache": ["backup", missing, "--dest", missing, "--dry-run", "--no-cache"],
    "migrate": ["migrate", "--repo", missing, "--dry-run"],  # 0.11.0
    "--root-override": [  # 0.10.0: a bad value is a usage error before anything runs
        "list",
        "--repo",
        missing,
        "--root-override",
        "NOT_A_TOKEN=C:\\x",
    ],
}
loaded = {}
for name, args in commands.items():
    proc = run([cli, *args])
    loaded[name] = {
        "exit": proc.returncode,
        "error_line": next((x for x in proc.stderr.splitlines() if x.startswith("error:")), ""),
        "traceback": "Traceback" in proc.stderr or "ModuleNotFoundError" in proc.stderr,
    }
results["subcommands"] = loaded
results["subcommands_ok"] = not any(v["traceback"] for v in loaded.values())
assert not missing.exists(), "a check created the missing folder"

# 3a. 0.11.0 (D67 8): the frozen CLI migrates a copy of the format-1 fixture (in .local)
migrated = repo / ".local" / "portable-migrate"
if migrated.exists():
    assert repo / ".local" in migrated.parents
    shutil.rmtree(migrated)
with zipfile.ZipFile(repo / "tests" / "fixtures" / "library_v1.zip") as fixture:
    fixture.extractall(migrated)
steps = {}
for name, args in {
    "backup refused before": ["remove", "--repo", migrated, "--game", "x", "--yes"],
    "dry run": ["migrate", "--repo", migrated, "--dry-run", "--json"],
    "migrate": ["migrate", "--repo", migrated, "--json"],
    "verify": ["verify", "--repo", migrated, "--json"],
    "again": ["migrate", "--repo", migrated, "--json"],
}.items():
    proc = run([cli, *args])
    steps[name] = {"exit": proc.returncode, "traceback": "Traceback" in proc.stderr}
results["migrate_fixture"] = steps
results["migrate_folders"] = sorted(f.name for f in (migrated / "games").iterdir())
results["migrate_index"] = (migrated / "INDEX.txt").is_file()

# 3b. 0.7.0 (D43): the frozen list reads the release smoke's library (in .local, read only)
smoke = repo / ".local" / f"smoke-{version.replace('.', '')}"
if smoke.is_dir():
    proc = run([cli, "list", "--repo", smoke, "--json"])
    rows = json.loads(proc.stdout)["games"] if proc.returncode == 0 else []
    results["list_install_present"] = [[g["display_name"], g.get("install_present")] for g in rows]
    proc = run([cli, "list", "--repo", smoke])
    results["list_text_tail"] = [x.rsplit("  ", 1)[-1] for x in proc.stdout.splitlines() if x][:4]
    # 0.8.0 (D49): remove without --yes only previews; nothing in the library changes
    before_smoke = state(smoke)
    game = rows[0]["game_id"] if rows else "-"  # the first record of the smoke library
    proc = run([cli, "remove", "--repo", smoke, "--game", game, "--json"])
    data = json.loads(proc.stdout) if proc.returncode == 0 else {}
    results["remove_preview"] = {
        "exit": proc.returncode,
        "result": data.get("result"),
        "snapshots": (data.get("plan") or {}).get("snapshots"),
        "library_changed": sorted(
            k
            for k in before_smoke.keys() | state(smoke).keys()
            if before_smoke.get(k) != state(smoke).get(k)
        ),
    }

    # 0.9.0 (D57): the frozen mirror of the smoke library into .local (its mirrors.json goes
    # beside the portable programs), then the stats line of the frozen list
    import shutil

    target = repo / ".local" / f"mirror-frozen-{version.replace('.', '')}"
    if target.exists():
        assert repo / ".local" in target.parents
        shutil.rmtree(target)
    fake = repo / ".local" / "fake-appdata"
    proc = run(
        [cli, "mirror", "--repo", smoke, "--to", target, "--renpy-saves-root", fake, "--json"]
    )
    data = json.loads(proc.stdout) if proc.stdout.lstrip().startswith("{") else {}
    results["frozen_mirror"] = {
        "exit": proc.returncode,
        "new": data.get("new"),
        "snapshots_copied": data.get("snapshots_copied"),
        "storage": data.get("storage"),
        "verified": data.get("verified"),
        "library_changed": sorted(
            k
            for k in before_smoke.keys() | state(smoke).keys()
            if before_smoke.get(k) != state(smoke).get(k)
        ),
    }
    proc = run([cli, "list", "--repo", smoke])
    results["list_stats_line"] = proc.stdout.strip().splitlines()[-1]

# 4. self-test: hidden window built and closed, settings only beside the portable programs
proc = run([gui, "--self-test"], timeout=180)
results["self_test_exit"] = proc.returncode
results["config_after_self_test"] = sorted(
    str(p.relative_to(dist)) for p in config.rglob("*") if p.is_file()
)

# 5. the zip
archive = repo / "dist" / f"EngineSave-{version}-win64-portable.zip"
checksum_file = archive.with_name(archive.name + ".sha256")
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
with zipfile.ZipFile(archive) as z:
    names = z.namelist()
    bad = z.testzip()
results["zip"] = {
    "bytes": archive.stat().st_size,
    "sha256": digest,
    "sha256_file": checksum_file.read_text(encoding="ascii").split()[0],
    "sha256_file_bytes": checksum_file.stat().st_size,
    "entries": len(names),
    "top_level": sorted({n.split("/", 1)[0] for n in names}),
    "has_config": any(n.startswith("EngineSave/config/") for n in names),
    "testzip": bad,
}
results["zip_ok"] = (
    results["zip"]["sha256"] == results["zip"]["sha256_file"]
    and results["zip"]["top_level"] == ["EngineSave"]
    and not results["zip"]["has_config"]
    and bad is None
)

# 6. nothing else changed
appdata_after, samples_after = state(appdata), state(samples)
results["appdata_entries"] = len(appdata_after)
results["appdata_changed"] = sorted(
    k
    for k in appdata_before.keys() | appdata_after.keys()
    if appdata_before.get(k) != appdata_after.get(k)
)
results["samples_entries"] = len(samples_after)
results["samples_changed"] = sorted(
    k
    for k in samples_before.keys() | samples_after.keys()
    if samples_before.get(k) != samples_after.get(k)
)
print(json.dumps(results, indent=1, ensure_ascii=False))
