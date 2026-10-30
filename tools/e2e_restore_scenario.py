"""End-to-end check: backup -> permanent cleanup -> reinstall -> restore (plan section 21 M22).

The user's real case, played through on copies: a game and its global saves are backed up,
the game folder and the global folder are deleted to free space, a fresh copy of the game is
"installed" somewhere else, and the saves are restored into it and compared file by file.

Everything that is written lives in a sandbox below the repository's ``.local`` folder
(``.local\\e2e`` by default), which is emptied first. The sample game folder and the game's
real global saves folder are only read, to copy them; the report checks that neither changed.
The cleanup deletes ``--permanent``ly: the real recycle bin is never used (rule 16). The large
``images_*.rpa`` archives are left out of the copy (they are only pictures). Since 0.8.0 a
last step deletes backups in the sandbox library (D49), and when the game's real global saves
folder is gone the checked copy in ``.local\\e2e-saves\\<save directory>`` is used (S-124).
Since 0.11.0 (D67 8) the sandbox library starts as a copy of the synthetic library of 0.10.0
(``tests/fixtures/library_v1.zip``): a backup into it is refused until it is migrated, and the
scenario's game is backed up into it after ``enginesave migrate``; before the comparison a
step 7 puts the saves back by hand as the snapshot's ``README.txt`` says, without EngineSave, into
stand-in folders of the sandbox, and compares them byte for byte (D67 4).

usage: python tools/e2e_restore_scenario.py [--sample DIR] [--saves-from DIR]
           [--sandbox DIR] [--exclude GLOB ...] [--what-if]

Writes ``<sandbox>\\report.md`` and prints a summary; the exit code is 0 when every step
passed, 1 when one failed and 2 when the arguments are refused.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SANDBOX = REPO / ".local" / "e2e"
SAVES_COPIES = REPO / ".local" / "e2e-saves"  # S-124: checked copies of real saves
DEFAULT_EXCLUDES = ("game/images_*.rpa",)
SAVES = "game/saves"
OLD_LIBRARY = REPO / "tests" / "fixtures" / "library_v1.zip"  # 0.11.0 (D67 8)


class Refused(Exception):
    """An argument the scenario does not accept (nothing has been written)."""


# ------------------------------------------------------------------------------ the sandbox


def inside(path: Path, folder: Path) -> bool:
    """``path`` is ``folder`` or lies below it (both resolved, case-insensitive on Windows)."""
    path, folder = Path(os.path.normcase(path.resolve())), Path(os.path.normcase(folder.resolve()))
    return path == folder or folder in path.parents


def check_sandbox(sandbox: Path, repo: Path, *, keep_out: Sequence[Path] = ()) -> Path:
    """The sandbox must be a folder strictly below ``<repo>\\.local``; it must not touch the
    folders that are only read (``keep_out``: the sample, the real saves, ``Test``)."""
    sandbox = sandbox.resolve()
    local = (repo / ".local").resolve()
    if not inside(sandbox, local) or sandbox == local:
        raise Refused(f"the sandbox must be a folder below {local}: {sandbox}")
    for folder in keep_out:
        if inside(sandbox, folder) or inside(folder, sandbox):
            raise Refused(f"the sandbox {sandbox} overlaps {folder}, which is only read")
    return sandbox


def clear_sandbox(sandbox: Path, repo: Path, *, keep_out: Sequence[Path] = ()) -> None:
    """Empty the sandbox (after :func:`check_sandbox`); read-only files are made writable."""
    sandbox = check_sandbox(sandbox, repo, keep_out=keep_out)

    def retry(function: Callable[..., Any], failed: str, _exc: BaseException) -> None:
        os.chmod(failed, stat.S_IWRITE)
        function(failed)

    if sandbox.exists():
        shutil.rmtree(sandbox, onexc=retry)
    sandbox.mkdir(parents=True)


def excluded(relative: str, patterns: Sequence[str]) -> bool:
    lowered = relative.casefold()
    return any(
        fnmatch.fnmatchcase(lowered, p.casefold())
        or lowered.startswith(p.casefold().rstrip("/") + "/")
        for p in patterns
    )


def copy_tree(source: Path, target: Path, *, leave_out: Sequence[str] = ()) -> tuple[int, int]:
    """Copy ``source`` into ``target`` (modification times kept); ``leave_out`` are globs on
    the relative path with ``/``. Links are not followed. Returns ``(files, bytes)``."""
    files = size = 0
    for current, dirs, names in os.walk(source):
        rel_dir = Path(current).relative_to(source).as_posix()
        dirs[:] = [
            d
            for d in sorted(dirs)
            if not Path(current, d).is_symlink()
            and not excluded(f"{rel_dir}/{d}".lstrip("./"), leave_out)
        ]
        for name in sorted(names):
            relative = f"{rel_dir}/{name}".lstrip("./")
            path = Path(current, name)
            if path.is_symlink() or excluded(relative, leave_out):
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            files += 1
            size += path.stat().st_size
    return files, size


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(folder: Path) -> dict[str, list[Any]]:
    """``relative path -> [size, sha256, modified (ms)]`` of every file below ``folder``."""
    result: dict[str, list[Any]] = {}
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        info = path.stat()
        relative = path.relative_to(folder).as_posix()
        result[relative] = [info.st_size, sha256(path), info.st_mtime_ns // 1_000_000]
    return result


def differences(expected: dict[str, list[Any]], found: dict[str, list[Any]]) -> list[str]:
    """What differs: missing, extra, or a different size, hash or modification time."""
    lines = []
    for relative in sorted(expected.keys() | found.keys()):
        want, have = expected.get(relative), found.get(relative)
        if want is None:
            lines.append(f"extra: {relative}")
        elif have is None:
            lines.append(f"missing: {relative}")
        elif want != have:
            what = [
                n for n, a, b in zip(("size", "sha256", "mtime"), want, have, strict=True) if a != b
            ]
            lines.append(f"{', '.join(what)} differ: {relative}")
    return lines


def tree_state(folder: Path) -> dict[str, list[int]]:
    """Sizes and modification times only (for the read-only check of the sources)."""
    return {
        p.relative_to(folder).as_posix(): [p.stat().st_size, p.stat().st_mtime_ns]
        for p in folder.rglob("*")
        if p.is_file()
    }


# ------------------------------------------------------------------------------- the steps


@dataclass
class Step:
    number: int
    title: str
    commands: list[str] = field(default_factory=list)
    seconds: float = 0.0
    ok: bool = True
    facts: list[str] = field(default_factory=list)

    def check(self, condition: bool, fact: str) -> None:
        self.facts.append(("ok: " if condition else "FAILED: ") + fact)
        self.ok = self.ok and condition


class Scenario:
    def __init__(self, args: argparse.Namespace) -> None:
        self.sample = Path(args.sample).resolve()
        self.sandbox = Path(args.sandbox).resolve()
        self.leave_out = list(args.exclude or DEFAULT_EXCLUDES)
        self.cli = Path(sys.executable).with_name("enginesave.exe")
        self.steps: list[Step] = []
        self.saves_from = Path(args.saves_from).resolve() if args.saves_from else None
        self.facts: dict[str, Any] = {}
        # 0.9.0 (D52): the command line keeps a parse cache in its APPDATA; it goes into the
        # sandbox (never the user's %APPDATA%\EngineSave, never the fake saves root)
        self.env = {**os.environ, "APPDATA": str(self.sandbox / "appdata-config")}
        # 0.10.0 (D65 1): other engines' folders in the user profile are sandbox folders too
        for token in (
            "UNITY_LOCALLOW", "LOCALAPPDATA", "APPDATA", "DOCUMENTS", "SAVED_GAMES", "VIRTUALSTORE"
        ):  # fmt: skip
            self.env[f"ENGINESAVE_ROOT_{token}"] = str(self.sandbox / "profile" / token)

    # -- helpers

    def run(self, step: Step, *args: str) -> tuple[int, Any]:
        """Run the EngineSave command line with ``--json``; ``(exit code, parsed output)``."""
        command = [str(self.cli), *args, "--json"]
        shown = " ".join(f'"{a}"' if " " in a else a for a in ["enginesave", *args])
        step.commands.append(shown)
        proc = subprocess.run(
            command,
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
        )
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else None
        except json.JSONDecodeError:
            data = None
        if proc.returncode != 0:
            step.facts.append(f"exit code {proc.returncode}: {proc.stderr.strip()[-400:]}")
        return proc.returncode, data

    def timed(self, number: int, title: str, body: Callable[[Step], None]) -> Step:
        step = Step(number, title)
        started = time.perf_counter()
        try:
            body(step)
        except Exception as exc:  # noqa: BLE001 - a failed step is reported, the run goes on
            step.check(False, f"{type(exc).__name__}: {exc}")
        step.seconds = time.perf_counter() - started
        self.steps.append(step)
        print(f"step {number} {'ok' if step.ok else 'FAILED'} ({step.seconds:.1f} s): {title}")
        return step

    @property
    def common(self) -> list[str]:
        return ["--renpy-saves-root", str(self.sandbox / "fake-appdata")]

    # -- the scenario

    def read_sample(self) -> None:
        """The game in the sample (read-only scan): its root, save folder and version."""
        proc = subprocess.run(
            [str(self.cli), "scan", str(self.sample), "--json", "--no-unlinked", "--no-cache"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        games = json.loads(proc.stdout)["games"] if proc.stdout.strip() else []
        renpy = [g for g in games if g["engine"] == "renpy"]
        if len(renpy) != 1:
            raise Refused(f"the sample must hold one Ren'Py game, found {len(renpy)}")
        game = renpy[0]
        self.inner = Path(game["game_root"])
        self.wrapper = self.inner.parent if self.inner != self.sample else None
        self.save_directory = game["identity"]["renpy_save_directory"]
        self.name = game["display_name"]
        self.version = game["identity"]["config_version"]
        if self.saves_from is None:
            appdata = os.environ.get("APPDATA") or str(Path.home())
            self.saves_from = Path(appdata) / "RenPy" / self.save_directory
            copy = SAVES_COPIES / self.save_directory
            if not self.saves_from.is_dir() and copy.is_dir():  # S-124: the user cleaned the
                self.saves_from = copy  # real folder up; a checked copy was kept for this
                self.facts["saves_copy"] = True
        if not self.saves_from.is_dir():
            raise Refused(f"no global saves to copy: {self.saves_from}")

    def layout(self, base: str) -> Path:
        """Where the copy of the game goes below ``base`` (the wrapper kept, as unpacked)."""
        folder = self.sandbox / base
        if self.wrapper is not None:
            folder = folder / self.wrapper.name
        return folder / self.inner.name

    def step1(self, step: Step) -> None:
        game = self.layout("games")
        files, size = copy_tree(self.inner, game, leave_out=[*self.leave_out, SAVES])
        step.commands.append(f"copy {self.inner} -> {game} (without {', '.join(self.leave_out)})")
        step.facts.append(f"game copy: {files} files, {size / 1024**2:.1f} MB")
        saves = copy_tree(self.saves_from, game / SAVES)
        fake = self.sandbox / "fake-appdata" / self.save_directory
        copy_tree(self.saves_from, fake)
        step.commands.append(f"copy {self.saves_from} -> {game / SAVES} and {fake}")
        step.facts.append(f"saves copy: {saves[0]} files, {saves[1] / 1024**2:.1f} MB, twice")
        self.original_local = manifest(game / SAVES)
        self.original_global = manifest(fake)
        (self.sandbox / "original.json").write_text(
            json.dumps({"local": self.original_local, "global": self.original_global}, indent=1),
            encoding="utf-8",
        )
        step.check(
            len(self.original_local) == len(self.original_global) == saves[0] > 0,
            f"original list recorded: {saves[0]} files in each place (size, SHA-256, time)",
        )
        self.facts["save_files"] = saves[0]

    def step2(self, step: Step) -> None:
        code, data = self.run(step, "scan", str(self.sandbox / "games"), *self.common)
        games = (data or {}).get("games", [])
        step.check(code == 0 and len(games) == 1, f"one game found (exit {code})")
        game = games[0]
        sources = {s["source_id"]: s for s in game["sources"]}
        count = self.facts["save_files"]
        step.check(game["status"] == "CONFIRMED", f"status {game['status']}")
        step.check(
            Path(game["game_root"]) == self.layout("games"),
            f"game root is the inner folder: {game['game_root']}",
        )
        for sid in ("local", "global"):
            source = sources.get(sid, {})
            step.check(
                source.get("status") == "CONFIRMED" and source.get("file_count") == count,
                f"{sid}: {source.get('status')}, {source.get('file_count')} files",
            )

    def step3(self, step: Step) -> None:
        repo = self.sandbox / "repo"
        args = ["backup", str(self.sandbox / "games"), "--dest", str(repo), *self.common, "--yes"]
        # 0.11.0 (D67 8): the library is one of 0.10.0, migrated before the backup
        with zipfile.ZipFile(OLD_LIBRARY) as archive:
            archive.extractall(repo)
        step.commands.append(f"unzip {OLD_LIBRARY.name} -> {repo} (a library of 0.10.0)")
        seeded = sum(1 for p in (repo / "objects").rglob("*") if p.is_file())
        code, _ = self.run(step, *args)
        step.check(code == 2, f"a backup into the library of 0.10.0 is refused (exit {code})")
        code, data = self.run(step, "migrate", "--repo", str(repo), "--dry-run")
        step.check(code == 0 and (data or {}).get("needed"), f"migrate --dry-run (exit {code})")
        # 1.0.0 (D70 1): one record's folder is in use, its rename fails; the library is
        # format 2 already (0.10.0 cannot open it half-renamed) and running it again finishes
        held = next(p for p in sorted((repo / "games").iterdir()) if (p / "game.json").is_file())
        with open(held / "game.json", "rb"):
            code, data = self.run(step, "migrate", "--repo", str(repo))
        failed, version = len((data or {}).get("failed") or []), (data or {}).get("format_version")
        step.check(
            code == 1 and version == 2 and failed == 1,
            f"a rename that fails (folder in use): exit {code}, {failed} failed, format {version}",
        )
        code, data = self.run(step, "migrate", "--repo", str(repo))
        step.check(
            code == 0
            and (data or {}).get("format_version") == 2
            and (data or {}).get("renamed") == 1,
            f"migrated again: the last record folder renamed (exit {code})",
        )
        code, data = self.run(step, *args)
        results = ((data or {}).get("result") or {}).get("results", [])
        step.check(
            code == 0 and [r["status"] for r in results] == ["ok"], f"backup ok (exit {code})"
        )
        if results:
            self.game_id = results[0]["game_id"]
            self.snapshot_id = results[0]["snapshot_id"]
            step.facts.append(
                f"snapshot {self.snapshot_id}: {results[0]['file_count']} files, "
                f"{results[0]['total_bytes'] / 1024**2:.1f} MB"
            )
        code, data = self.run(step, "verify", "--repo", str(repo), "--objects")
        objects = sum(1 for p in (repo / "objects").rglob("*") if p.is_file())
        self.facts["objects"] = objects
        contents = {
            entry[1] for entry in [*self.original_local.values(), *self.original_global.values()]
        }
        step.check(code == 0, f"verify --objects clean (exit {code})")
        step.check(
            objects - seeded == len(contents),
            f"{objects - seeded} new objects for {len(contents)} different contents: local and "
            "global copies are stored once (dedup)",
        )
        folder = self.record_dir(repo)
        step.check(
            folder.name != self.game_id and folder.is_dir(),
            f"the game's record folder is named after it: games\\{folder.name} (D67 1)",
        )

    def step4(self, step: Step) -> None:
        repo = self.sandbox / "repo"
        game, fake = self.layout("games"), self.sandbox / "fake-appdata" / self.save_directory
        args = ["cleanup", "--repo", str(repo), "--game", self.name, "--game-root", "--global"]
        code, data = self.run(step, *args, "--permanent", "--yes", *self.common)
        targets = ((data or {}).get("result") or {}).get("targets", [])
        freed = ((data or {}).get("result") or {}).get("freed_bytes", 0)
        step.check(
            code == 0 and [t["result"] for t in targets] == ["deleted", "deleted"],
            f"both folders deleted permanently (exit {code}), freed {freed / 1024**2:.1f} MB",
        )
        step.check(
            not game.exists() and not fake.exists(),
            "the game folder and the global folder are gone",
        )
        code, data = self.run(step, "list", "--repo", str(repo))
        records = self.ours(data)
        step.check(
            any(r.get("cleaned_at") for r in records), "list: the record is marked game removed"
        )
        step.check(
            [r.get("install_present") for r in records] == [False],
            "list: the record is backup only (0.7.0, D43)",
        )
        doc = json.loads((self.record_dir(repo) / "game.json").read_text(encoding="utf-8"))
        step.check(
            bool(doc.get("cleanups")), f"game.json records {len(doc.get('cleanups', []))} cleanup"
        )
        self.facts["freed"] = freed

    def step5(self, step: Step) -> None:
        again = self.layout("games-reinstalled")
        files, size = copy_tree(self.inner, again, leave_out=[*self.leave_out, SAVES])
        step.commands.append(f"copy {self.inner} -> {again} (a fresh unpack: no saves)")
        step.check(not (again / SAVES).exists(), "the new install has no saves")
        step.check(
            not (self.sandbox / "fake-appdata" / self.save_directory).exists(),
            "the global folder stays absent",
        )
        step.facts.append(f"reinstalled: {files} files, {size / 1024**2:.1f} MB")

    def step6(self, step: Step) -> None:
        repo, again = self.sandbox / "repo", self.layout("games-reinstalled")
        given = again.parent if self.wrapper is not None else again
        base = ["restore", "--repo", str(repo), "--game", self.name, *self.common]
        code, data = self.run(step, *base, "--game-root", str(given), "--dry-run")
        plan = (data or {}).get("plan") or {}
        step.check(
            code == 0 and Path(plan.get("game_root") or "") == again,
            f"the folder around the game was replaced by the game: {plan.get('game_root')}",
        )
        if self.wrapper is not None:
            step.check(
                any("holds the game in" in n for n in plan.get("notes", [])),
                "the plan says so (note)",
            )
        code, data = self.run(step, *base, "--game-root", str(again), "--yes")
        result = (data or {}).get("result") or {}
        step.check(
            code == 0 and not result.get("failures"),
            f"restored {result.get('written')} files, {len(result.get('failures') or [])} failed",
        )
        registered = result.get("registered_install")
        step.check(
            Path(registered or "") == again,
            f"the restore recorded the new folder as an install: {registered} (0.7.0, D43)",
        )

    def step_by_hand(self, step: Step) -> None:
        """0.11.0 (D67 4): without EngineSave -- the snapshot's README says where each data
        folder goes; each is copied there by hand (a stand-in folder of the sandbox for each
        place) and compared with the originals byte for byte (the times are in the README)."""
        snapshot = self.record_dir(self.sandbox / "repo") / "snapshots" / self.snapshot_id
        readme = (snapshot / "README.txt").read_text(encoding="utf-8-sig")
        moves = [
            line.split("  →  ", 1)
            for line in readme.splitlines()
            if line.startswith("data\\") and "  →  " in line
        ]
        step.check(len(moves) == 2, f"README.txt names {len(moves)} data folders and their places")
        by_hand = self.sandbox / "by-hand"
        for folder, where in moves:
            place = where.rsplit("（", 1)[0]
            target = by_hand / ("game-saves" if place.startswith("<游戏目录>") else "global")
            copy_tree(snapshot / folder.rstrip("\\"), target)
            step.commands.append(f"copy {snapshot / folder} -> {target}  (README: {place})")
        found = [
            (expected, manifest(by_hand / name))
            for expected, name in (
                (self.original_local, "game-saves"),
                (self.original_global, "global"),
            )
        ]
        diff = [
            relative
            for expected, have in found
            for relative in sorted(expected.keys() | have.keys())
            if (expected.get(relative) or [None, None])[:2]
            != (have.get(relative) or [None, None])[:2]
        ]
        self.facts["by_hand_differences"] = len(diff)
        step.check(
            not diff,
            f"{len(diff)} differences in size or SHA-256 from the originals (copied by hand)",
        )
        step.facts += [f"  {line}" for line in diff[:20]]

    def step7(self, step: Step) -> None:
        again = self.layout("games-reinstalled")
        fake = self.sandbox / "fake-appdata" / self.save_directory
        diff = differences(self.original_local, manifest(again / SAVES))
        diff += differences(self.original_global, manifest(fake))
        self.facts["differences"] = len(diff)
        step.check(
            not diff, f"{len(diff)} differences from the original list (size, SHA-256, time)"
        )
        step.facts += [f"  {line}" for line in diff[:20]]
        repo = self.sandbox / "repo"
        code, data = self.run(step, "list", "--repo", str(repo))  # before any new backup
        records = self.ours(data)
        present = [r.get("install_present") for r in records]
        self.facts["installed_after_restore"] = present == [True]
        step.check(
            code == 0 and present == [True],
            "list: after the restore the record is installed again (本体存在, 0.7.0, D43)",
        )
        code, _ = self.run(step, "verify", "--repo", str(repo), "--objects")
        step.check(code == 0, f"verify --objects clean (exit {code})")
        code, data = self.run(
            step, "scan", str(self.sandbox / "games-reinstalled"), "--repo", str(repo), *self.common
        )
        games = (data or {}).get("games", [])
        known = games[0] if len(games) == 1 else {}
        step.check(
            known.get("known_game_id") == self.game_id
            and Path(known.get("game_root", "")) == again,
            f"scan --repo: known as {known.get('known_game_id')} at {known.get('game_root')}",
        )
        objects_before = self.facts["objects"]
        args = ["backup", str(self.sandbox / "games-reinstalled"), "--dest", str(repo)]
        code, data = self.run(step, *args, *self.common, "--yes")
        step.check(code == 0, f"backup of the new install ok (exit {code})")
        objects = sum(1 for p in (repo / "objects").rglob("*") if p.is_file())
        step.check(objects == objects_before, f"no new objects ({objects}): same content")
        doc = json.loads((self.record_dir(repo) / "game.json").read_text(encoding="utf-8"))
        current = [
            i
            for i in doc.get("installs", [])
            if Path(i["game_root"]) == again and not i.get("removed_at")
        ]
        step.check(bool(current), "game.json: the new install is recorded without removed_at")
        code, data = self.run(step, "list", "--repo", str(repo))
        records = self.ours(data)
        step.check(
            not any(r.get("cleaned_at") for r in records),
            "list: game removed is gone (an install exists again)",
        )

    def step8(self, step: Step) -> None:
        """0.8.0 (D49): a record whose every object another record uses too is deleted, then
        one old snapshot of the game; what is left verifies and keeps its objects."""
        repo = self.sandbox / "repo"
        fake = self.sandbox / "fake-appdata"
        twin = fake / f"{self.save_directory}-copy"
        copy_tree(fake / self.save_directory, twin)
        step.commands.append(f"copy {fake / self.save_directory} -> {twin} (an orphaned copy)")
        games = self.sandbox / "games-reinstalled"
        args = ["backup", str(games), "--dest", str(repo), "--unlinked", twin.name, "--yes"]
        code, _ = self.run(step, *args, *self.common)
        before = sum(1 for p in (repo / "objects").rglob("*") if p.is_file())
        step.check(before == self.facts["objects"], f"the copy adds no object ({before})")
        code, data = self.run(step, "list", "--repo", str(repo))
        orphan = [  # the library of 0.10.0 has an orphaned record of its own
            r
            for r in (data or {}).get("games", [])
            if r.get("kind") == "unlinked" and r.get("display_name") == twin.name
        ]
        step.check(code == 0 and len(orphan) == 1, "list: one orphaned record for the copy")
        if not orphan:
            return
        remove = ["remove", "--repo", str(repo), "--game", orphan[0]["game_id"]]
        code, data = self.run(step, *remove)
        step.check(
            code == 0 and (data or {}).get("result") is None,
            "remove without --yes only shows what would go",
        )
        code, data = self.run(step, *remove, "--yes")
        result = (data or {}).get("result") or {}
        step.check(
            code == 0 and result.get("records") == 1,
            f"the record is deleted (exit {code}), {result.get('objects_removed')} objects removed",
        )
        after = sum(1 for p in (repo / "objects").rglob("*") if p.is_file())
        step.check(after == before, f"the objects the game's record uses are all there ({after})")
        ids = self.snapshot_ids(repo)
        code, data = self.run(
            step,
            "remove",
            "--repo",
            str(repo),
            "--game",
            self.game_id,
            "--snapshot",
            ids[0],
            "--yes",
        )
        result = (data or {}).get("result") or {}
        step.check(
            code == 0 and result.get("snapshots") == 1 and self.snapshot_ids(repo) == ids[1:],
            f"the oldest snapshot {ids[0]} of the game is deleted, {len(ids) - 1} left",
        )
        code, _ = self.run(step, "verify", "--repo", str(repo), "--objects")
        step.check(code == 0, f"verify --objects of everything left is clean (exit {code})")
        code, data = self.run(step, "list", "--repo", str(repo))
        step.check(
            not (data or {}).get("orphan_record_dirs"), "no record folder outside the catalog"
        )
        self.facts["removed"] = True

    def snapshot_ids(self, repo: Path) -> list[str]:
        """The game's snapshots, oldest first (``show``: by when they were made, D67 2)."""
        step = Step(0, "snapshot ids")
        _code, data = self.run(step, "show", "--repo", str(repo), "--game", self.game_id)
        return [s["snapshot_id"] for s in (data or {}).get("snapshots", [])]

    def record_dir(self, repo: Path) -> Path:
        """The game's record folder: the catalog's ``folder`` (D67 1)."""
        catalog = json.loads((repo / "catalog.json").read_text(encoding="utf-8"))
        entry = next(r for r in catalog["games"] if r["game_id"] == self.game_id)
        return repo / "games" / entry.get("folder", self.game_id)

    def ours(self, data: Any) -> list[dict[str, Any]]:
        """The scenario's own record in a ``list`` (the library holds 0.10.0's too)."""
        return [r for r in (data or {}).get("games", []) if r.get("game_id") == self.game_id]

    # -- driver

    def execute(self) -> int:
        self.read_sample()
        sources = [self.sample, self.saves_from]
        before = {str(s): tree_state(s) for s in sources}
        clear_sandbox(self.sandbox, REPO, keep_out=[*sources, REPO / "Test"])
        for number, title, body in (
            (1, "prepare the sandbox: copy the game and its saves", self.step1),
            (2, "scan (read only)", self.step2),
            (3, "back up and verify", self.step3),
            (4, "clean up: delete the game folder and the global folder permanently", self.step4),
            (5, "reinstall: a fresh copy of the game somewhere else", self.step5),
            (
                6,
                "restore into the new install (the wrapper folder first, as a dry run)",
                self.step6,
            ),
            (7, "put the saves back by hand as the snapshot's README says", self.step_by_hand),
            (8, "compare with the originals; verify; scan; back up again", self.step7),
            (9, "delete a backup: an orphaned copy of the saves, then an old snapshot", self.step8),
        ):
            step = self.timed(number, title, body)
            if not step.ok and number < 9:
                break
        after = {str(s): tree_state(s) for s in sources}
        changed = sum(
            1
            for s in before
            for key in before[s].keys() | after[s].keys()
            if before[s].get(key) != after[s].get(key)
        )
        self.facts["sources_changed"] = changed
        self.write_report()
        failed = [s for s in self.steps if not s.ok]
        print(f"report: {self.sandbox / 'report.md'}")
        print(
            f"{len(self.steps) - len(failed)} of {len(self.steps)} steps passed; "
            f"by hand: {self.facts.get('by_hand_differences')} differences; step 8 differences: "
            f"{self.facts.get('differences')}; sources changed: {changed}"
        )
        return 0 if not failed and changed == 0 and len(self.steps) == 9 else 1

    def write_report(self) -> None:
        lines = [
            "# 端到端验证：备份 → 永久清理 → 重装 → 恢复",
            "",
            f"- 样本：`{self.sample}`（游戏 {self.name} {self.version}，只读，只从这里复制）",
            f"- 真实全局存档：`{self.saves_from}`（只读，只从这里复制"
            + ("；真实目录已不在，自动改用核对过的副本" if self.facts.get("saves_copy") else "")
            + "）",
            f"- 沙箱：`{self.sandbox}`（开始时清空）；不复制：{', '.join(self.leave_out)}",
            f"- 结果：{sum(s.ok for s in self.steps)}/{len(self.steps)} 步通过；按 README 手工复制"
            f"（第 7 步）差异 {self.facts.get('by_hand_differences')}；第 8 步差异 "
            f"{self.facts.get('differences')}；样本与真实存档前后变化 "
            f"{self.facts.get('sources_changed')} 项",
            "- 恢复后记录回到“本体存在”（第 8 步，再次备份之前）："
            f"{'是' if self.facts.get('installed_after_restore') else '否'}",
            "- 删除备份后其余记录校验全部通过、共用的对象仍在（第 9 步）："
            f"{'是' if self.facts.get('removed') else '否'}",
            "",
            "| 步骤 | 内容 | 耗时 | 结果 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {s.number} | {s.title} | {s.seconds:.1f} s | {'通过' if s.ok else '失败'} |"
            for s in self.steps
        ]
        for s in self.steps:
            lines += ["", f"## {s.number}. {s.title}", ""]
            lines += [f"    {command}" for command in s.commands]
            lines += [""] + [f"- {fact}" for fact in s.facts]
        lines += [
            "",
            "## 图形界面的等价操作",
            "",
            "扫描与备份页点「备份已勾选项」→ 备份结果里点「清理…」→ 主按钮「移到回收站」"
            "（真实使用时默认是回收站，这里为了不碰真实回收站用了永久删除）→ 重新安装游戏后，"
            "在备份库页选中记录点「恢复…」→ 下拉框里选新的安装目录"
            "（或点「浏览…」选解压出来的文件夹）→ 主按钮「恢复」。",
        ]
        (self.sandbox / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sample", required=True, help="the game folder to copy")
    parser.add_argument(
        "--saves-from", help="its global saves (default: %%APPDATA%%\\RenPy\\<save_directory>)"
    )
    parser.add_argument("--sandbox", default=str(DEFAULT_SANDBOX), help="below .local only")
    parser.add_argument("--exclude", action="append", help="globs left out of the game copy")
    parser.add_argument("--what-if", action="store_true", help="check the arguments, write nothing")
    args = parser.parse_args(argv)
    scenario = Scenario(args)
    try:
        check_sandbox(scenario.sandbox, REPO, keep_out=[scenario.sample, REPO / "Test"])
        if args.what_if:
            scenario.read_sample()
            check_sandbox(
                scenario.sandbox,
                REPO,
                keep_out=[scenario.sample, scenario.saves_from, REPO / "Test"],
            )
            print(
                f"what if: would empty {scenario.sandbox} and copy {scenario.inner} "
                f"(without {', '.join(scenario.leave_out)}) and {scenario.saves_from} into it"
            )
            return 0
        return scenario.execute()
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
