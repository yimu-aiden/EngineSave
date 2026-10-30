"""Interface preferences in ``gui.json`` (D23) inside the configuration folder (D28).

That folder is ``%APPDATA%\\EngineSave``, or ``config\\`` beside a portable build
(:mod:`enginesave.appdirs`). Only choices made in the window live here: scan roots, library
path, the backup check boxes, window geometry, the saves-root override and the last export
folder. Matching rules stay in the library's ``catalog.json`` (D12). A damaged file never stops
the window from opening: the defaults are used and the caller gets a note for the log panel.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enginesave.appdirs import config_dir
from enginesave.fsutil import atomic_write_text
from enginesave.gui.i18n import _

SETTINGS_NAME = "gui.json"
LOG_DIR_NAME = "logs"
SETTINGS_VERSION = 1


def app_dir(env: Mapping[str, str] | None = None) -> Path:
    """The configuration folder: ``%APPDATA%\\EngineSave``, or ``config\\`` when portable."""
    return config_dir(env)


def settings_path(env: Mapping[str, str] | None = None) -> Path:
    return app_dir(env) / SETTINGS_NAME


def log_dir(env: Mapping[str, str] | None = None) -> Path:
    return app_dir(env) / LOG_DIR_NAME


CLEANUP_METHODS = ("recycle", "permanent")


@dataclass
class GuiSettings:
    scan_roots: list[str] = field(default_factory=list)
    repo_path: str = ""
    saves_root_override: str | None = None  # null: the real saves root (0.3.1, S-065)
    include_candidates: bool = False
    include_tokens: bool = False
    window_geometry: str = ""
    last_export_dir: str = ""
    # 0.5.0 (D32): defaults that save clicks. Private keys and original paths in exports are
    # never remembered; they start unticked every time. 0.5.0 also had ``auto_scan``; 0.6.0
    # withdrew scanning at start-up and ignores that key in an older file.
    cleanup_method: str = "recycle"
    export_overwrite: bool = False
    last_import_dir: str = ""
    mirror_folder: str = ""  # 0.9.0 (D57 4): the last mirror's folder
    # 0.10.0 (D64): after a backup, mirror to that folder (shown once there is one)
    auto_mirror: bool = True
    mirror_verify: bool = True  # 「镜像后校验」 as last chosen; the automatic mirror too
    # 0.10.0 (D63): folders a scan skips (absolute paths, or patterns as for --exclude)
    scan_excludes: list[str] = field(default_factory=list)
    # 0.11.0 (D68 2): "auto" (Windows' display language), "zh" or "en"
    language: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        return {"format_version": SETTINGS_VERSION, **dataclasses.asdict(self)}

    @property
    def override_path(self) -> Path | None:
        """The saves-root override as a path, or ``None`` when it is not set."""
        text = (self.saves_root_override or "").strip()
        return Path(text) if text else None


def blank_settings(previous: GuiSettings | None = None) -> GuiSettings:
    """Defaults for handing the window to a user; only the window geometry is carried over."""
    return GuiSettings(window_geometry=previous.window_geometry if previous else "")


def _valid(name: str, value: Any) -> bool:
    if name in ("scan_roots", "scan_excludes"):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if name in (
        "include_candidates",
        "include_tokens",
        "export_overwrite",
        "auto_mirror",
        "mirror_verify",
    ):
        return isinstance(value, bool)
    if name == "saves_root_override":
        return value is None or isinstance(value, str)
    if name == "cleanup_method":
        return value in CLEANUP_METHODS
    if name == "language":
        return value in ("auto", "zh", "en")
    return isinstance(value, str)


def load_settings(path: Path) -> tuple[GuiSettings, str | None]:
    """Settings from ``path`` and a note when something in it was damaged. A file that is not
    JSON at all gives the defaults; since 1.0.0 (D70 4) a single field of the wrong type or
    value gives only that field its default -- the rest is kept, and so it is when the window
    writes the file again. A ``language`` that is not ``auto``/``zh``/``en`` is ``auto``."""
    if not path.is_file():
        return GuiSettings(), None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        return GuiSettings(), _("设置文件已损坏，改用默认设置：{path}（{exc}）", path=path, exc=exc)
    values: dict[str, Any] = {}
    reset: list[str] = []
    for item in dataclasses.fields(GuiSettings):
        if item.name not in data:
            continue
        if _valid(item.name, data[item.name]):
            values[item.name] = data[item.name]
        elif item.name == "language":
            values[item.name] = "auto"  # an unknown language: Windows' own (S-222)
        else:
            reset.append(item.name)
    if not (values.get("saves_root_override") or "").strip():
        values["saves_root_override"] = None  # 0.3.0 wrote "" for "not set"
    note = None
    if reset:
        note = _(
            "设置文件里这些项不对，已改回默认值（其余设置照旧）：{fields}（{path}）",
            fields=", ".join(reset),
            path=path,
        )
    return GuiSettings(**values), note


def save_settings(settings: GuiSettings, path: Path) -> None:
    """Atomic write; creates the configuration folder on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(settings.to_dict(), indent=2, ensure_ascii=False) + "\n")
