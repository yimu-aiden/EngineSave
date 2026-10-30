"""Numbers for the optimisation milestone (plan section 21 M23, D38): start-up and run times,
test-suite time and module sizes, so that "before" and "after" can be compared.

usage: python tools/measure.py [--samples DIR] [--library DIR] [--game NAME] [--repeat N]
                               [--tests] [--json OUT]

Read-only. The window is built hidden, with its settings and log in a temporary folder (the
real %APPDATA%\\EngineSave is never touched: the command line runs with a temporary APPDATA, so
its parse cache lands there too); scans only read (the synthetic fixtures are built in a
temporary folder first); list and show only read the library. Since 0.9.0 (D52) it also times
the 700-script game in process: options first, the full parse, each without and with the
cache; since 0.10.0 (D60) also with the options in a sub-folder and with the save directory
in the second script read. Times are medians of ``--repeat`` runs, in seconds; they depend
on what else the computer is doing, so compare numbers taken on the same machine in the same
state.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = Path(sys.executable).parent
# The seven largest modules before 0.6.0, each with what was split out of it (D38).
MODULES = {
    "gui/views/dialogs": ("gui/views/dialogs",),
    "gui/state": ("gui/state",),
    "cleanup": ("cleanup",),
    "export": ("export",),
    "cli": ("cli",),
    "gui/app": ("gui/app", "gui/scan_backup"),
    "backup": ("backup", "store"),
}


def window_child(folder: str) -> None:
    """In a child process: build the window hidden and say when it is ready."""
    started = time.perf_counter()
    import tkinter as tk

    from enginesave.gui.app import App, apply_icon, apply_scaling, apply_theme

    imported = time.perf_counter()
    root = tk.Tk()
    root.withdraw()  # never shown (no foreground window, plan rule 15)
    apply_scaling(root)
    apply_theme(root)
    apply_icon(root)
    base = Path(folder)
    env = {**os.environ, "APPDATA": str(base / "AppData")}
    app = App(root, settings_file=base / "gui.json", logs=base / "logs", env=env)
    root.update()
    ready = time.perf_counter()
    print(f"READY import={imported - started:.3f} window={ready - imported:.3f}", flush=True)
    app.on_close()


def window_ready() -> tuple[float, float, float]:
    """Seconds from starting the process until the window is built; and its two parts."""
    with tempfile.TemporaryDirectory() as folder:
        started = time.perf_counter()
        proc = subprocess.Popen(
            [sys.executable, __file__, "--window-child", folder],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        line = proc.stdout.readline()
        total = time.perf_counter() - started
        proc.wait(timeout=60)
    parts = dict(item.split("=") for item in line.split()[1:])
    return total, float(parts["import"]), float(parts["window"])


def build_fixtures(folder: Path) -> tuple[Path, Path]:
    """The synthetic games of the test suite (F1-F16), built into ``folder``."""
    sys.path.insert(0, str(REPO / "tests"))
    from fixtures import builder

    games, saves = folder / "games", folder / "saves"
    games.mkdir(parents=True)
    saves.mkdir()
    for build in (builder.build_f1, builder.build_f2, builder.build_f3, builder.build_f5):
        build(games, saves)
    builder.build_f6(games, saves)
    builder.build_f7(games)
    builder.build_f8(saves)
    builder.build_f9(games)
    builder.build_f10(games)
    builder.build_f13(games, saves, name_alike_dir=False)
    builder.build_f14(games, saves)
    builder.build_f16(games)
    builder.build_nwjs(games)
    return games, saves


def timed(command: list[str], env: dict[str, str] | None = None) -> float:
    started = time.perf_counter()
    subprocess.run(command, cwd=REPO, capture_output=True, check=False, env=env)
    return time.perf_counter() - started


def many_scripts(folder: Path, repeat: int) -> dict[str, float]:
    """D52 acceptance: the 700-script game scanned in process, medians in seconds. ``first``
    parses with an empty cache each time (the files themselves are in the OS cache by then);
    ``cached`` reads everything from a cache file written by the scan before."""
    sys.path.insert(0, str(REPO / "tests"))
    from enginesave.scan import ScanOptions, run_scan
    from enginesave.scan_cache import ScanCache
    from fixtures import builder

    def once(games: Path, saves: Path, cache: ScanCache) -> float:
        started = time.perf_counter()
        run_scan(ScanOptions(roots=[games], saves_root_override=saves, cache=cache))
        return time.perf_counter() - started

    numbers = {}
    cases = (  # 0.10.0 (D60): options in a sub-folder; the save directory in the second script
        ("options", {"options": True}),
        ("options_below", {"options_dir": "ZZ_Config"}),  # after script.rpyc by name too
        ("second_script", {"second": True}),
        ("full_parse", {"options": False}),
    )
    for name, how in cases:
        games, saves = folder / name / "games", folder / name / "saves"
        games.mkdir(parents=True)
        saves.mkdir()
        builder.build_many_scripts(games, saves, **how)
        cache_file = folder / name / "scan-cache.json"
        first = [once(games, saves, ScanCache()) for _ in range(repeat)]
        numbers[f"scan_700_{name}_first_s"] = round(statistics.median(first), 3)
        once(games, saves, ScanCache.load(cache_file))
        cached = [once(games, saves, ScanCache.load(cache_file)) for _ in range(repeat)]
        numbers[f"scan_700_{name}_cached_s"] = round(statistics.median(cached), 3)
    return numbers


def median(function, repeat: int) -> float:
    return statistics.median(function() for _ in range(repeat))


def module_lines() -> dict[str, list[int]]:
    """``[total lines, lines of the largest file]`` of each large module and its parts."""
    base = REPO / "src" / "enginesave"
    sizes = {}
    for name, parts in MODULES.items():
        files = []
        for part in parts:
            path = base / part
            if path.is_dir():
                files += sorted(path.rglob("*.py"))
            elif path.with_suffix(".py").exists():
                files.append(path.with_suffix(".py"))
        lines = [len(f.read_text(encoding="utf-8").splitlines()) for f in files]
        sizes[name] = [sum(lines), max(lines)]
    return sizes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--samples", default=str(REPO / "Test"))
    parser.add_argument("--library", default=str(REPO / ".local" / "smoke-050"))
    parser.add_argument("--game", required=True, help="a record of --library")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--tests", action="store_true", help="also time the whole test suite")
    parser.add_argument("--json", help="write the numbers here too")
    parser.add_argument("--window-child", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.window_child:
        window_child(args.window_child)
        return 0
    cli = str(SCRIPTS / "enginesave.exe")
    sandbox = tempfile.TemporaryDirectory()  # the CLI's APPDATA: its parse cache goes here
    env = {**os.environ, "APPDATA": str(Path(sandbox.name) / "AppData")}
    numbers: dict[str, object] = {}
    windows = [window_ready() for _ in range(args.repeat)]
    numbers["window_ready_s"] = round(statistics.median(w[0] for w in windows), 3)
    numbers["window_import_s"] = round(statistics.median(w[1] for w in windows), 3)
    numbers["window_build_s"] = round(statistics.median(w[2] for w in windows), 3)
    numbers["cli_version_s"] = round(median(lambda: timed([cli, "--version"], env), args.repeat), 3)
    with tempfile.TemporaryDirectory() as folder:
        games, saves = build_fixtures(Path(folder))
        # --no-cache: every run parses, as in 0.8.0, so the numbers stay comparable; the
        # cache's effect is measured on its own by many_scripts below (D52)
        fixtures = [cli, "scan", str(games), "--json", "--renpy-saves-root", str(saves)]
        fixtures.append("--no-cache")
        numbers["cli_scan_fixtures_s"] = round(median(lambda: timed(fixtures, env), args.repeat), 3)
    with tempfile.TemporaryDirectory() as folder:
        numbers.update(many_scripts(Path(folder), args.repeat))
    scan = [cli, "scan", args.samples, "--json", "--no-cache"]
    numbers["cli_scan_samples_s"] = round(median(lambda: timed(scan, env), args.repeat), 3)
    listing = [cli, "list", "--repo", args.library]
    numbers["cli_list_s"] = round(median(lambda: timed(listing, env), args.repeat), 3)
    show = [cli, "show", "--repo", args.library, "--game", args.game]
    numbers["cli_show_s"] = round(median(lambda: timed(show, env), args.repeat), 3)
    if args.tests:
        started = time.perf_counter()
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=REPO,
            capture_output=True,
            check=False,
        )
        numbers["tests_s"] = round(time.perf_counter() - started, 1)
    numbers["module_lines"] = module_lines()
    for key, value in numbers.items():
        print(f"{key}: {value}")
    if args.json:
        Path(args.json).write_text(json.dumps(numbers, indent=1), encoding="utf-8")
    sandbox.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
