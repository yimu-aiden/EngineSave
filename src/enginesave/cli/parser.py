"""The argument parser and ``main``. Each subcommand's module is imported only when that
subcommand runs, so ``enginesave --version`` and ``enginesave list`` do not load the scanner,
the exporter or the cleanup code (D38 2)."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from enginesave import __version__
from enginesave.cli.common import STOPPED_NOTE, Handler, configure_output, ctrl_c_stops
from enginesave.errors import STOPPED_EXIT_CODE, EngineSaveError, Stopped, UsageError
from enginesave.roots import overriding

# ------------------------------------------------------------------------------------ parser


def _common_options(parser: argparse.ArgumentParser, *, subcommand: bool) -> None:
    """Global options, accepted before or after the subcommand."""

    def default(value: Any) -> Any:
        return argparse.SUPPRESS if subcommand else value

    parser.add_argument(
        "--renpy-saves-root",
        metavar="PATH",
        default=default(None),
        help="use PATH instead of %%APPDATA%%\\RenPy for global saves (also via "
        "ENGINESAVE_RENPY_SAVES_ROOT)",
    )
    parser.add_argument(
        "--root-override",
        action="append",
        metavar="TOKEN=PATH",
        # its own list after the subcommand: argparse would replace the one before it
        dest="root_override_sub" if subcommand else "root_override",
        default=default(None),
        help="use PATH for a root token: UNITY_LOCALLOW, LOCALAPPDATA, APPDATA, DOCUMENTS, "
        "SAVED_GAMES, VIRTUALSTORE or RENPY_SAVES_ROOT (repeatable; also via "
        "ENGINESAVE_ROOT_<TOKEN>)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=default(False),
        help="machine-readable JSON on stdout; human output goes to stderr",
    )
    parser.add_argument(
        "--verbose", action="store_true", default=default(False), help="debug logging"
    )


def _unattended_options(parser: argparse.ArgumentParser) -> None:
    """1.2.0 (D73 8, 9): for a program that runs the command without a console."""
    parser.add_argument(
        "--progress",
        choices=["jsonl"],
        help="jsonl: one JSON object per line on stderr while it runs (event progress)",
    )
    parser.add_argument(
        "--stop-file",
        metavar="PATH",
        help="stop at the next safe point once this file exists, as Ctrl+C (exit code 130)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enginesave",
        description="Ren'Py save discovery, backup, verification and restore.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _common_options(parser, subcommand=False)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    scan = commands.add_parser("scan", help="read-only discovery report")
    _common_options(scan, subcommand=True)
    scan.add_argument("roots", nargs="+", metavar="ROOT", help="folders to scan")
    scan.add_argument("--report", metavar="FILE.json", help="also write the JSON report here")
    scan.add_argument("--max-depth", type=int, default=None, help="maximum folder depth")
    scan.add_argument(
        "--exclude", action="append", metavar="GLOB", help="skip folders matching GLOB"
    )
    scan.add_argument("--hash", action="store_true", help="compare engine mirrors by SHA-256")
    scan.add_argument(
        "--no-unlinked", action="store_true", help="skip orphaned global save folders"
    )
    scan.add_argument(
        "--no-cache",
        action="store_true",
        help="do not read or write the parse cache (scan-cache.json in the settings folder)",
    )
    scan.add_argument(
        "--repo", metavar="DIR", help="recognise games already in this backup repository"
    )
    _unattended_options(scan)

    backup = commands.add_parser("backup", help="copy confirmed save sources into a repository")
    _common_options(backup, subcommand=True)
    backup.add_argument(
        "roots", nargs="*", metavar="ROOT", help="folders to scan (not needed with --game-root)"
    )
    backup.add_argument("--dest", required=True, metavar="DIR", help="backup repository")
    backup.add_argument(
        "--game-root",
        action="append",
        metavar="PATH",
        help="only the game installed in this folder (repeatable): only these folders are "
        "scanned; each must be a game's own folder",
    )
    backup.add_argument(
        "--skip-unchanged",
        action="store_true",
        help="no new snapshot of a game whose saves are exactly its latest snapshot "
        "(status unchanged)",
    )
    backup.add_argument(
        "--reason",
        choices=["manual", "auto"],
        default="manual",
        help="what the snapshots are recorded as: manual (default) or auto (started by "
        "another program, e.g. after the game was closed)",
    )
    backup.add_argument(
        "--keep-auto",
        type=int,
        metavar="N",
        help="afterwards keep only the newest N auto snapshots of each game backed up "
        "(manual, imported and pre_restore snapshots are never removed by this)",
    )
    backup.add_argument(
        "--mirror",
        action="store_true",
        help="afterwards mirror the repository to the folder of its last successful mirror",
    )
    backup.add_argument(
        "--auto-mirror",
        action="store_true",
        help="like --mirror, when the window's setting 'mirror automatically' is on",
    )
    _unattended_options(backup)
    backup.add_argument(
        "--select",
        action="append",
        metavar="ID[,ID]",
        help="only these games (game id, save directory or root folder name)",
    )
    backup.add_argument(
        "--include-candidates", action="store_true", help="also copy CANDIDATE sources"
    )
    backup.add_argument(
        "--include-unlinked",
        action="store_true",
        help="also copy every orphaned global save folder",
    )
    backup.add_argument(
        "--unlinked",
        action="append",
        metavar="NAME[,NAME]",
        help="also copy these orphaned global save folders (repeatable)",
    )
    backup.add_argument(
        "--include-tokens",
        action="store_true",
        help="also copy <saves root>/tokens (contains Ren'Py's save-signing PRIVATE keys)",
    )
    backup.add_argument(
        "--dry-run",
        action="store_true",
        help="show the plan, write nothing to the repository (the parse cache is still "
        "updated unless --no-cache)",
    )
    backup.add_argument(
        "--no-cache",
        action="store_true",
        help="do not read or write the parse cache (scan-cache.json in the settings folder)",
    )
    backup.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    backup.add_argument("--report", metavar="FILE.json", help="write the plan/result as JSON")

    listing = commands.add_parser("list", help="games and snapshots in a repository")
    _common_options(listing, subcommand=True)
    listing.add_argument("--repo", required=True, metavar="DIR")

    show = commands.add_parser("show", help="one game's record, or one snapshot's manifest")
    _common_options(show, subcommand=True)
    show.add_argument("--repo", required=True, metavar="DIR")
    show.add_argument("--game", required=True, metavar="ID", help="game id, save dir or name")
    show.add_argument("--snapshot", metavar="SID", help="snapshot id or 'latest'")

    verify = commands.add_parser("verify", help="re-hash stored snapshots against manifests")
    _common_options(verify, subcommand=True)
    verify.add_argument("--repo", required=True, metavar="DIR")
    verify.add_argument("--game", metavar="ID")
    verify.add_argument("--snapshot", metavar="SID")
    verify.add_argument(
        "--objects",
        action="store_true",
        help="also re-hash every file in the object store (0.2.0 libraries)",
    )

    restore = commands.add_parser("restore", help="put a snapshot back (preview, then confirm)")
    _common_options(restore, subcommand=True)
    restore.add_argument("--repo", required=True, metavar="DIR")
    restore.add_argument("--game", required=True, metavar="ID")
    restore.add_argument("--snapshot", default="latest", metavar="SID|latest")
    restore.add_argument("--game-root", metavar="PATH", help="install folder to restore into")
    restore.add_argument(
        "--sources", metavar="ID[,ID]", help="only these sources (e.g. local,global)"
    )
    restore.add_argument(
        "--from-install",
        metavar="PATH",
        help="with --game-root, when the snapshot holds the local saves of several installs: "
        "those of this install (default: the install the snapshot came from)",
    )
    restore.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    restore.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    restore.add_argument("--no-safety-snapshot", action="store_true", help=argparse.SUPPRESS)
    restore.add_argument("--i-know-what-i-am-doing", action="store_true", help=argparse.SUPPRESS)

    link = commands.add_parser("link", help="always associate an install folder with a game")
    _common_options(link, subcommand=True)
    link.add_argument("--repo", required=True, metavar="DIR")
    link.add_argument("--game", required=True, metavar="ID")
    link.add_argument("--game-root", required=True, metavar="PATH")

    confirm_source = commands.add_parser(
        "confirm-source", help="mark a CANDIDATE save folder as confirmed for a game"
    )
    _common_options(confirm_source, subcommand=True)
    confirm_source.add_argument("--repo", required=True, metavar="DIR")
    confirm_source.add_argument("--game", metavar="ID", help="an existing game record")
    confirm_source.add_argument(
        "--game-root",
        metavar="PATH",
        help="instead of --game: scan this install folder (read only) and create the record "
        "if the repository has none",
    )
    confirm_source.add_argument("--source-id", required=True, metavar="SID")
    confirm_source.add_argument("--path", required=True, metavar="PATH")

    export = commands.add_parser(
        "export", help="pack snapshots into verified zips (one game, several, or --all)"
    )
    _common_options(export, subcommand=True)
    export.add_argument("--repo", required=True, metavar="DIR")
    export.add_argument(
        "--game",
        action="append",
        metavar="ID",
        help="the game to export; repeat it to export the latest snapshot of several games",
    )
    export.add_argument(
        "--all",
        action="store_true",
        help="export the latest snapshot of every record into the --to folder",
    )
    export.add_argument(
        "--to",
        required=True,
        metavar="FILE.zip|DIR",
        help="archive to write (an existing folder gets the default file name); with --all or "
        "several --game, the folder for the archives",
    )
    export.add_argument("--snapshot", default="latest", metavar="SID|latest")
    export.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing archives (otherwise they are skipped with several records)",
    )
    export.add_argument(
        "--include-tokens",
        action="store_true",
        help="also pack machine_tokens (Ren'Py's save-signing PRIVATE keys)",
    )
    export.add_argument(
        "--keep-paths",
        action="store_true",
        help="keep this computer's folder paths (by default they become GAME_ROOT/... and "
        "RENPY_SAVES_ROOT/..., and folders known only by an absolute path are left out)",
    )

    cleanup = commands.add_parser(
        "cleanup", help="remove folders whose saves are backed up and verified (checked first)"
    )
    _common_options(cleanup, subcommand=True)
    cleanup.add_argument("--repo", required=True, metavar="DIR")
    cleanup.add_argument("--game", required=True, metavar="ID")
    cleanup.add_argument("--snapshot", default="latest", metavar="SID|latest")
    cleanup.add_argument(
        "--game-root",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="the game's install folder (default: its only existing install); the game itself "
        "is not in the backup",
    )
    cleanup.add_argument(
        "--global",
        dest="global_folder",
        action="store_true",
        help="the game's global save folder (%%APPDATA%%\\RenPy\\<save directory>)",
    )
    cleanup.add_argument(
        "--unlinked", action="append", metavar="NAME", help="the folder of an orphaned record"
    )
    cleanup.add_argument(
        "--permanent", action="store_true", help="delete permanently, not to the recycle bin"
    )
    cleanup.add_argument(
        "--recycle-limit",
        metavar="SIZE",
        help="largest folder for the recycle bin (default: the room left in the drive's recycle "
        "bin, 4G when its size cannot be read)",
    )
    cleanup.add_argument(
        "--skip-failed", action="store_true", help="clean up only the folders that pass"
    )
    cleanup.add_argument("--dry-run", action="store_true", help="show the checks, delete nothing")
    cleanup.add_argument(
        "--on-full-bin",
        choices=["ask", "purge-ours", "permanent", "stop"],
        default=None,
        help="when the recycle bin has no room for the rest: ask (default in a console), "
        "purge-ours (delete the folders EngineSave put in the recycle bin earlier, then go on), "
        "permanent (delete the rest permanently) or stop (leave them for later; default "
        "otherwise)",
    )
    cleanup.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    importer = commands.add_parser(
        "import", help="add an exported zip, or every zip in a folder, to a repository"
    )
    _common_options(importer, subcommand=True)
    importer.add_argument("--repo", required=True, metavar="DIR")
    importer.add_argument(
        "--from",
        required=True,
        dest="from_path",
        metavar="FILE.zip|DIR",
        help="one archive, or a folder: every *.zip directly in it, by name",
    )
    importer.add_argument(
        "--reimport",
        action="store_true",
        help="import archives again that this repository already holds as imports "
        "(by default they are skipped)",
    )
    importer.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    remove = commands.add_parser(
        "remove", help="delete a game's backups, or some of its snapshots (preview, then --yes)"
    )
    _common_options(remove, subcommand=True)
    remove.add_argument("--repo", required=True, metavar="DIR")
    remove.add_argument("--game", metavar="ID", help="the record (required unless --tmp)")
    remove.add_argument(
        "--snapshot",
        action="append",
        metavar="SID",
        help="one snapshot to delete (repeatable); without it the whole record is deleted",
    )
    remove.add_argument(
        "--tmp",
        action="store_true",
        help="instead: the temporary folders interrupted runs left (not those changed in the "
        "last 10 minutes), then reclaim the object store's space",
    )
    remove.add_argument("--dry-run", action="store_true", help="show what would go, delete nothing")
    remove.add_argument(
        "--yes", action="store_true", help="delete; without it only the preview is shown"
    )

    mirror = commands.add_parser(
        "mirror", help="mirror the repository into another folder (best on another drive)"
    )
    _common_options(mirror, subcommand=True)
    mirror.add_argument("--repo", required=True, metavar="DIR")
    mirror.add_argument(
        "--to",
        required=True,
        metavar="DIR",
        help="an empty folder, or an earlier mirror of this repository",
    )
    mirror.add_argument(
        "--no-verify", action="store_true", help="do not verify the mirror afterwards"
    )
    mirror.add_argument(
        "--repair",
        action="store_true",
        help="check every object the mirror already has by its SHA-256, not its size, and "
        "copy a damaged one again",
    )

    migrate = commands.add_parser(
        "migrate",
        help="migrate a repository of SaveTrail 0.10.0 and earlier to the readable layout "
        "(record folders named after the games); 0.10.0 cannot open it afterwards",
    )
    _common_options(migrate, subcommand=True)
    migrate.add_argument("--repo", required=True, metavar="DIR")
    migrate.add_argument(
        "--dry-run", action="store_true", help="show the folder names it would give, change nothing"
    )

    about = commands.add_parser(
        "about", help="this EngineSave's version and the versions of its JSON outputs"
    )
    _common_options(about, subcommand=True)
    return parser


def load_handler(command: str) -> Handler:
    """Import the module of the subcommand that runs, and only that one (D38 2). The imports
    are spelled out, not looked up by name, so that PyInstaller still finds every module."""
    if command == "scan":
        from enginesave.cli.scanning import cmd_scan

        return cmd_scan
    if command == "backup":
        from enginesave.cli.backups import cmd_backup

        return cmd_backup
    if command == "restore":
        from enginesave.cli.restoring import cmd_restore

        return cmd_restore
    if command == "cleanup":
        from enginesave.cli.cleaning import cmd_cleanup

        return cmd_cleanup
    if command == "remove":
        from enginesave.cli.removing import cmd_remove

        return cmd_remove
    if command == "mirror":
        from enginesave.cli.mirroring import cmd_mirror

        return cmd_mirror
    if command == "migrate":
        from enginesave.cli.migrating import cmd_migrate

        return cmd_migrate
    if command == "about":
        from enginesave.cli.about import cmd_about

        return cmd_about
    if command in ("export", "import"):
        from enginesave.cli import transfer

        return transfer.cmd_export if command == "export" else transfer.cmd_import
    from enginesave.cli import library

    handlers: dict[str, Handler] = {
        "list": library.cmd_list,
        "show": library.cmd_show,
        "verify": library.cmd_verify,
        "link": library.cmd_link,
        "confirm-source": library.cmd_confirm_source,
    }
    return handlers[command]


STOPPABLE = frozenset(
    {"scan", "backup", "verify", "export", "import", "cleanup", "remove", "mirror", "migrate"}
)  # 0.9.0 (D58): Ctrl+C stops these at a safe point
# 0.11.0 (D67 8): they write into the library, which must not be of format 1 (not migrated)
WRITES = frozenset({"backup", "restore", "link", "confirm-source", "cleanup", "import", "remove"})
# 1.2.0 (D73 4): they take the library's write lock (mirror: the mirror's)
LOCKED = WRITES | {"migrate", "mirror"}


def _lock_target(args: argparse.Namespace) -> tuple[Path, bool] | None:
    """``(library folder, create it)`` whose write lock the command takes, or ``None``: a
    command that only reads (``--dry-run``, ``remove`` without ``--yes``), or a folder that is
    no library yet (the command says what is wrong with it; ``backup`` creates one)."""
    from enginesave.repo import CATALOG

    if args.command not in LOCKED or getattr(args, "dry_run", False):
        return None
    if args.command == "remove" and not args.yes:  # only the preview
        return None
    if args.command == "mirror":
        target = Path(os.path.abspath(args.to))
        return (target, False) if target.is_dir() else None
    if args.command == "backup":
        return Path(os.path.abspath(args.dest)), True
    root = Path(os.path.abspath(args.repo))
    return (root, False) if (root / CATALOG).is_file() else None


def _write_lock(args: argparse.Namespace) -> contextlib.AbstractContextManager[None]:
    found = _lock_target(args)
    if found is None:
        return contextlib.nullcontext()
    from enginesave.lock import library_lock

    root, create = found
    return library_lock(root, f"enginesave {args.command}", create=create)


def _busy(args: argparse.Namespace, exc: Any) -> int:
    """D73 4: exit code 4; with ``--json`` the holder on stdout."""
    print(f"error: {exc}; try again later", file=sys.stderr)
    if getattr(args, "json", False):
        print(json.dumps({"error": "library_busy", "holder": exc.holder}, indent=2))
    return exc.exit_code


def _check_overrides(args: argparse.Namespace) -> None:
    """D66 7: root tokens moved onto this command's scan folders or library are refused
    (exit code 2) before anything runs."""
    from pathlib import Path

    from enginesave.roots import override_conflicts

    scan_roots = [Path(os.path.abspath(r)) for r in getattr(args, "roots", None) or []]
    libraries = [
        Path(os.path.abspath(value))
        for value in (getattr(args, "repo", None), getattr(args, "dest", None))
        if value
    ]
    problems = override_conflicts(os.environ, scan_roots, libraries)
    if problems:
        raise UsageError("; ".join(problems) + "; move the root token elsewhere")


def _check_layout(args: argparse.Namespace) -> None:
    """D67 8: a command that writes into a library of format 1 is refused (exit code 2)
    before anything runs; reading works on both formats, and so do ``--dry-run``, ``migrate``,
    ``mirror`` (a mirror has its library's format) and ``remove --tmp`` (needed before a
    migration)."""
    if args.command not in WRITES or getattr(args, "dry_run", False) or getattr(args, "tmp", False):
        return
    folder = getattr(args, "repo", None) or getattr(args, "dest", None)
    if not folder:
        return
    from pathlib import Path

    from enginesave.repo import CATALOG, MIGRATE_HINT, Repo

    root = Path(os.path.abspath(folder))
    if not (root / CATALOG).is_file():
        return
    try:
        old = Repo(root).old_format
    except EngineSaveError:  # the command itself says what is wrong with the library
        return
    if old:
        raise UsageError(MIGRATE_HINT.format(root=root))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_output(args.verbose)
    stops = ctrl_c_stops() if args.command in STOPPABLE else contextlib.nullcontext()
    from enginesave.lock import LibraryBusy

    try:
        overrides = [
            *(getattr(args, "root_override", None) or []),
            *(getattr(args, "root_override_sub", None) or []),
        ]
        with stops, overriding(overrides):  # 0.10.0 (D65 1)
            _check_overrides(args)
            _check_layout(args)
            with _write_lock(args):  # 1.2.0 (D73 4)
                return load_handler(args.command)(args)
    except Stopped:  # stopped before there was anything to report (a scan)
        print(f"error: {STOPPED_NOTE}", file=sys.stderr)
        return STOPPED_EXIT_CODE
    except LibraryBusy as exc:
        return _busy(args, exc)
    except EngineSaveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # a second Ctrl+C, or a command without safe points
        print("error: interrupted", file=sys.stderr)
        return STOPPED_EXIT_CODE
