"""Unreal Engine 4 and 5 (0.10.0, D65 third tier): a packaged game is ``Engine\\`` beside its
project folder ``<Project>\\`` (with ``Binaries\\Win64\\<Project>*.exe`` and
``Content\\Paks``); it saves in ``<Project>\\Saved\\SaveGames``, below ``%LOCALAPPDATA%`` for a
shipping build and below the game folder for others (or one started ``-NotInstalled``), so
both are looked at. Logs, crash reports and settings (``Saved\\Logs``, ``Crashes``,
``Config``) are not saves. Unreal Engine 3 is laid out otherwise and is not taken.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from enginesave import roots
from enginesave.engines import common
from enginesave.engines.base import ScanContext
from enginesave.fsutil import UnsafePathError, safe_relative_component
from enginesave.models import DetectedGame, Evidence


def project_folder(directory: Path, listing: common.Listing) -> str | None:
    """The project folder: the one with ``Content\\Paks`` and an exe in ``Binaries\\Win64``
    -- of several, the one whose exe is named after it (a target may be named otherwise)."""
    found: list[tuple[str, bool]] = []
    for name in sorted(listing.dirs.values()):
        if name.lower() == "engine":
            continue
        paks = directory / name / "Content" / "Paks"
        binaries = common.list_folder(directory / name / "Binaries" / "Win64")
        if not paks.is_dir() or binaries is None or not binaries.exes():
            continue
        named = any(Path(e).stem.lower().startswith(name.lower()) for e in binaries.exes())
        found.append((name, named))
    if len(found) == 1:
        return found[0][0]
    named = [name for name, by_name in found if by_name]
    return named[0] if len(named) == 1 else None


class UnrealDetector(common.EngineDetector):
    engine = "unreal"
    engines = ("unreal",)

    def recognise(
        self, directory: Path, listing: common.Listing, found: Any, ctx: ScanContext
    ) -> DetectedGame | None:
        project = project_folder(directory, listing)
        if project is None:
            return None
        try:
            safe_relative_component(project)
        except UnsafePathError:
            return None
        evidence = [
            Evidence(found.marker, "engine_fingerprint", "Unreal Engine pak files"),
            Evidence(f"{project}/Binaries/Win64", "unreal_project", f"project {project!r}"),
        ]
        warnings: list[str] = []
        relative = f"{project}/Saved/SaveGames"
        sources = []
        installed = common.profile_source(
            "savegames", roots.LOCALAPPDATA, relative, ctx, evidence[1:], warnings
        )
        sources += [installed] if installed is not None else []
        sources.append(common.game_source(directory, "savegames_local", relative, evidence[1:]))
        return common.rule_game(
            directory,
            "unreal",
            title=project,
            engine_key=project,
            sources=sources,
            evidence=evidence,
            warnings=warnings,
            project=project,
        )
