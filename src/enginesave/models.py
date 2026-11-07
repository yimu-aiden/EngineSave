"""Scan data model. Dataclass field names are the JSON field names (``snake_case``)."""

from __future__ import annotations

import dataclasses
import types
import typing
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Self

FORMAT_VERSION = 1


# What a restore says when every file went back (1.0.0, D72 R19: here, not in the console
# renderer, so that restore does not import report)
LOAD_NOTE = (
    "file-level restore succeeded; whether the game loads these saves depends on game "
    "version compatibility"
)


class GameStatus(str, Enum):  # noqa: UP042 - plan 5.1 specifies (str, Enum)
    CONFIRMED = "CONFIRMED"
    CANDIDATE = "CANDIDATE"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNSUPPORTED = "UNSUPPORTED"
    UNLINKED = "UNLINKED"


class SourceKind(str, Enum):  # noqa: UP042
    LOCAL = "local"
    GLOBAL = "global"
    EXTRA = "extra"
    MACHINE_TOKENS = "machine_tokens"
    # 0.10.0 (D65): a registry key kept as one .reg file (Unity's PlayerPrefs); never written
    REGISTRY = "registry"


class SourceStatus(str, Enum):  # noqa: UP042
    CONFIRMED = "CONFIRMED"
    CANDIDATE = "CANDIDATE"
    EXPECTED_ABSENT = "EXPECTED_ABSENT"
    USER_CONFIRMED = "USER_CONFIRMED"


class MetadataState(str, Enum):  # noqa: UP042
    RESOLVED = "resolved"
    DYNAMIC = "dynamic"
    MISSING = "missing"
    CONFLICT = "conflict"


class RootToken(str, Enum):  # noqa: UP042
    """What a source's ``relative_root`` is relative to (``enginesave.roots`` finds them)."""

    GAME_ROOT = "GAME_ROOT"
    RENPY_SAVES_ROOT = "RENPY_SAVES_ROOT"
    ABSOLUTE = "ABSOLUTE"
    # 0.10.0 (D65 1): folders of the user profile, for other engines
    UNITY_LOCALLOW = "UNITY_LOCALLOW"  # %USERPROFILE%\AppData\LocalLow
    LOCALAPPDATA = "LOCALAPPDATA"
    APPDATA = "APPDATA"
    # 0.11.0 (D69 3)
    DOCUMENTS = "DOCUMENTS"  # 「文档」 (KiriKiri's $(personalpath))
    SAVED_GAMES = "SAVED_GAMES"  # 「保存的游戏」 ($(savedgamespath))
    VIRTUALSTORE = "VIRTUALSTORE"  # %LOCALAPPDATA%\VirtualStore (games in Program Files)


BACKUP_READY = (SourceStatus.CONFIRMED, SourceStatus.USER_CONFIRMED)


# ---------------------------------------------------------------------------- serialization


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses, enums and paths into JSON-compatible values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        optional = getattr(value, "OPTIONAL_FIELDS", ())  # 0.10.0: written only when set
        return {
            f.name: to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if not (f.name in optional and getattr(value, f.name) in (None, "", []))
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    return value


def from_jsonable(tp: Any, data: Any) -> Any:
    """Rebuild a value of type ``tp`` from :func:`to_jsonable` output (unknown keys ignored)."""
    if data is None:
        return None
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        options = [a for a in typing.get_args(tp) if a is not type(None)]
        return from_jsonable(options[0], data) if len(options) == 1 else data
    if origin is list:
        args = typing.get_args(tp)
        return [from_jsonable(args[0] if args else Any, item) for item in data]
    if origin is dict or tp is dict:
        return dict(data)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(data)
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        hints = typing.get_type_hints(tp)
        kwargs = {
            f.name: from_jsonable(hints[f.name], data[f.name])
            for f in dataclasses.fields(tp)
            if f.name in data
        }
        return tp(**kwargs)
    return data


class JsonModel:
    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return from_jsonable(cls, data)


# ------------------------------------------------------------------------------ dataclasses


@dataclass
class Evidence(JsonModel):
    file: str
    method: str
    detail: str
    line: int | None = None


@dataclass
class SaveSource(JsonModel):
    source_id: str
    kind: SourceKind
    path: str
    root_token: RootToken
    relative_root: str
    status: SourceStatus
    method: str
    evidence: list[Evidence] = field(default_factory=list)
    exists: bool = False
    file_count: int = 0
    total_bytes: int = 0
    mirror_of: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # 0.10.0 (D65 2): only the files directly in the folder whose names match (an engine that
    # keeps its saves in the game folder itself, beside everything else); empty: all of it
    patterns: list[str] = field(default_factory=list)
    # 0.10.0: names directly in the folder that are left out (a log, a cache folder)
    excludes: list[str] = field(default_factory=list)
    # 1.2.0 (D73 13): the install folder a local source is in; null for any other source
    install_root: str | None = None

    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = ("patterns", "excludes")


@dataclass
class GameIdentity(JsonModel):
    renpy_save_directory: str | None
    config_name: str | None
    build_name: str | None
    config_version: str | None
    exe_basename: str | None
    root_dir_name: str
    # 0.10.0 (D65 3), other engines: ``engine_key`` is what an automatic match compares (with
    # the engine); the others are where it came from (a title; Unity's company and product;
    # an Unreal project)
    engine_key: str | None = None
    title: str | None = None
    company: str | None = None
    product: str | None = None
    project: str | None = None
    # 0.11.0 (M44 review): the engine_key SaveTrail 0.10.0 gave this game, when its title is
    # read otherwise now (D66 1); a record with it is still this game's. Matched, never stored
    legacy_key: str | None = None

    OPTIONAL_FIELDS: ClassVar[tuple[str, ...]] = (
        "engine_key",
        "title",
        "company",
        "product",
        "project",
        "legacy_key",
    )
    SCAN_ONLY: ClassVar[tuple[str, ...]] = ("legacy_key",)  # not in game.json

    def stored(self) -> dict[str, Any]:
        """What a record keeps of it (``game.json``)."""
        return {k: v for k, v in self.to_dict().items() if k not in self.SCAN_ONLY}


@dataclass
class DetectedGame(JsonModel):
    game_root: str
    engine: str
    engine_version: str | None
    display_name: str
    identity: GameIdentity
    metadata_state: MetadataState
    status: GameStatus
    sources: list[SaveSource] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    known_game_id: str | None = None
    link_suggestions: list[dict] = field(default_factory=list)
    installs: list[dict] = field(default_factory=list)
    # 0.8.0 (D48): the game folders of other games that use the same global save directory
    shared_global_with: list[str] = field(default_factory=list)

    def source(self, source_id: str) -> SaveSource | None:
        return next((s for s in self.sources if s.source_id == source_id), None)


@dataclass
class UnlinkedSaveDir(JsonModel):
    name: str
    path: str
    file_count: int
    total_bytes: int
    last_modified: str
    version_hint: str | None = None
    known_game_id: str | None = None


@dataclass
class ScanReport(JsonModel):
    format_version: int
    tool_version: str
    generated_at: str
    roots: list[str]
    renpy_saves_root: str
    games: list[DetectedGame] = field(default_factory=list)
    unlinked: list[UnlinkedSaveDir] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 0.6.0 (D34): {"total_seconds", "roots": [{"root", "seconds"}], "slowest": [{"path",
    # "seconds"}]}; the slowest folders only when the scan took longer than a minute.
    timing: dict = field(default_factory=dict)

    def summary(self) -> dict[str, int]:
        counts = {status.value: 0 for status in GameStatus}
        for game in self.games:
            counts[game.status.value] += 1
        counts[GameStatus.UNLINKED.value] = len(self.unlinked)
        return counts
