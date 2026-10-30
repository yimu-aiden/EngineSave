"""What the user decides about a scanned game, written to the library's catalog (D12): which
record a game folder belongs to (``link_root``, 6.6 rule 6) and which CANDIDATE folders are
its saves (``confirm_source``). ``enginesave link``, ``enginesave confirm-source`` and the
window's 「确认 / 关联…」 (0.8.0, D48 3) all write through here. Nothing is backed up."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from enginesave.backup import register_game
from enginesave.errors import UsageError
from enginesave.fsutil import UnsafePathError, iso_utc, path_key, safe_relative_component, utc_now
from enginesave.models import DetectedGame, GameStatus, SaveSource
from enginesave.repo import Repo
from enginesave.scan import ScanOptions, run_scan
from enginesave.scan_cache import ScanCache


def link_root(repo: Repo, catalog: dict, game_id: str, root: Path) -> tuple[dict, bool]:
    """``root`` always belongs to ``game_id`` from now on; ``(override, replaced an earlier
    link of the same folder)``."""
    root = Path(os.path.abspath(root))
    if not root.is_dir():
        raise UsageError(f"--game-root is not an existing folder: {root}")
    override = {
        "game_id": game_id,
        "type": "link_root",
        "game_root": str(root),
        "linked_at": iso_utc(utc_now()),
    }
    replaced = repo.set_override(
        catalog,
        override,
        lambda o: (
            o.get("type") == "link_root"
            and bool(o.get("game_root"))
            and path_key(o["game_root"]) == path_key(root)
        ),
    )
    return override, replaced


def confirm_source(repo: Repo, catalog: dict, game_id: str, source_id: str, path: Path) -> dict:
    """``path`` is a save folder of ``game_id``: scans with this library show it as
    USER_CONFIRMED (backed up like CONFIRMED)."""
    path = Path(os.path.abspath(path))
    if not path.is_dir():
        raise UsageError(f"--path is not an existing folder: {path}")
    try:
        source_id = safe_relative_component(source_id)
    except UnsafePathError as exc:
        raise UsageError(f"--source-id: {exc}") from exc
    override = {
        "game_id": game_id,
        "type": "confirm_source",
        "source_id": source_id,
        "path": str(path),
        "confirmed_at": iso_utc(utc_now()),
    }
    repo.set_override(
        catalog,
        override,
        lambda o: (
            o.get("type") == "confirm_source"
            and o.get("game_id") == game_id
            and bool(o.get("path"))
            and path_key(o["path"]) == path_key(path)
        ),
    )
    return override


def game_at(
    root: Path,
    *,
    repo: Path | None = None,
    saves_root_override: Path | None = None,
    env: Mapping[str, str] | None = None,
    also: Sequence[Path] = (),
    cache: ScanCache | None = None,
) -> DetectedGame:
    """The game installed exactly at ``root`` (a read-only scan of that folder alone, and of
    ``also``: the other folders of a game found in several, D9)."""
    report = run_scan(
        ScanOptions(
            roots=[root, *also],
            saves_root_override=saves_root_override,
            env=dict(os.environ) if env is None else env,
            cache=cache,
            repo=repo,
            include_unlinked=False,
        )
    )
    wanted = path_key(root)
    for game in report.games:
        roots = {path_key(game.game_root), *(path_key(i["game_root"]) for i in game.installs)}
        if wanted in roots:
            if game.status is GameStatus.UNSUPPORTED:
                raise UsageError(f"{root} is not a supported Ren'Py game ({game.engine})")
            return game
    found = "; ".join(g.game_root for g in report.games) or "none"
    raise UsageError(f"no game is installed at {root} (games found below it: {found})")


@dataclass
class LinkDecision:
    """What 「确认 / 关联…」 wrote."""

    game_id: str
    created: bool  # a new record (nothing backed up)
    link: dict
    confirmed: list[dict] = field(default_factory=list)


def confirm_and_link(
    repo: Repo, game: DetectedGame, game_id: str | None, sources: Sequence[SaveSource]
) -> LinkDecision:
    """D48 3 in one call: ``game`` belongs to record ``game_id`` (a new record, written like
    ``confirm-source --game-root`` does, when ``None``), its folder is linked to it, and each
    of ``sources`` is confirmed as its save folder."""
    catalog = repo.load_catalog()
    created = False
    if game_id is None:
        game_id, created = register_game(repo, catalog, game, new=True)
        catalog = repo.load_catalog()
    else:
        repo.resolve_game(catalog, game_id)  # a record that is still there
    link, _replaced = link_root(repo, catalog, game_id, Path(game.game_root))
    confirmed = [
        confirm_source(repo, catalog, game_id, source.source_id, Path(source.path))
        for source in sources
    ]
    return LinkDecision(game_id, created, link, confirmed)
