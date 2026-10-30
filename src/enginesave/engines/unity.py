"""Unity (0.10.0, D65 second tier): a Unity game saves below
``%USERPROFILE%\\AppData\\LocalLow\\<company>\\<product>`` (Unity's ``persistentDataPath``) and
keeps its PlayerPrefs in ``HKEY_CURRENT_USER\\Software\\<company>\\<product>``.

The company and product are the first two lines of ``<game>_Data\\app.info``; without them the
folder is not known (the game stays unsupported; its saves are not backed up).
The LocalLow folder is taken whole, without Unity's ``Player.log`` and ``Player-prev.log``.
The PlayerPrefs are read (never written) and kept as a ``PlayerPrefs.reg`` file: a restore puts
it in the game folder, and the user imports it by double-clicking it.

``DefaultCompany`` is Unity's placeholder: two games can share it, so such a game is matched
by its product with the company and its folder is never cleaned up automatically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from enginesave import registry, roots
from enginesave.engines import common
from enginesave.engines.base import ScanContext
from enginesave.fsutil import UnsafePathError, split_relative
from enginesave.models import (
    DetectedGame,
    Evidence,
    RootToken,
    SaveSource,
    SourceKind,
    SourceStatus,
)

LOGS = ("Player.log", "Player-prev.log")
DEFAULT_COMPANY = "defaultcompany"


def data_folder(directory: Path, listing: common.Listing) -> str | None:
    """``<N>_Data`` of the ``<N>.exe`` beside it; else the only ``*_Data`` with an ``app.info``."""
    for exe in listing.exes():
        found = listing.dir(f"{Path(exe).stem}_Data")
        if found is not None:
            return found
    with_info = [
        name
        for lower, name in sorted(listing.dirs.items())
        if lower.endswith("_data") and (directory / name / "app.info").is_file()
    ]
    return with_info[0] if len(with_info) == 1 else None


def app_info(path: Path) -> tuple[str, str] | None:
    """``(company, product)``: the first two non-empty lines of ``app.info`` -- UTF-16 without
    a BOM is read too; text that still holds NUL characters is not read (D66 3)."""
    data = common.read_small(path)
    if data is None:
        return None
    text = common.decode_text(data)
    if "\x00" in text and len(data) % 2 == 0:
        try:
            text = data.decode("utf-16-le")
        except UnicodeDecodeError:
            return None
    if "\x00" in text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[0], lines[1]) if len(lines) >= 2 else None


def registry_source(company: str, product: str, evidence: list[Evidence]) -> SaveSource:
    """The PlayerPrefs key as a source of one file, ``PlayerPrefs.reg`` (placed in the game
    folder on a restore); ``EXPECTED_ABSENT`` when the key is not there."""
    subkey = f"Software\\{company}\\{product}"
    data = registry.export_key(subkey)
    source = SaveSource(
        "playerprefs",
        SourceKind.REGISTRY,
        f"{registry.HKCU}\\{subkey}",
        RootToken.GAME_ROOT,
        roots.ROOT_ITSELF,
        SourceStatus.CONFIRMED if data is not None else SourceStatus.EXPECTED_ABSENT,
        "unity_playerprefs",
        list(evidence),
        patterns=[registry.REG_FILE],
    )
    if data is not None:
        source.exists, source.file_count, source.total_bytes = True, 1, len(data)
        source.notes.append("exported as PlayerPrefs.reg; a restore puts it in the game folder")
    return source


class UnityDetector(common.EngineDetector):
    engine = "unity"
    engines = ("unity",)

    def recognise(
        self, directory: Path, listing: common.Listing, found: Any, ctx: ScanContext
    ) -> DetectedGame | None:
        evidence = [Evidence(found.marker, "engine_fingerprint", "Unity player")]
        folder = data_folder(directory, listing)
        info = app_info(directory / folder / "app.info") if folder else None
        if info is None:
            return common.rule_game(
                directory, "unity", title=None, engine_key=None, sources=[],
                evidence=evidence, unsupported=True,
                warnings=[
                    "Unity game without a readable <game>_Data\\app.info: its company and "
                    "product (and so its save folder) are unknown; its saves are not backed up"
                ],
            )  # fmt: skip
        company, product = info
        evidence.append(Evidence(f"{folder}/app.info", "unity_app_info", f"{company} / {product}"))
        warnings: list[str] = []
        sources: list[SaveSource] = []
        try:
            split_relative(f"{company}/{product}")
        except UnsafePathError:
            warnings.append(
                f"app.info names {company!r} / {product!r}, not a folder name: no LocalLow folder"
            )
        else:
            locallow = common.profile_source(
                "locallow", roots.UNITY_LOCALLOW, f"{company}/{product}", ctx,
                evidence[1:], warnings, excludes=list(LOGS),
            )  # fmt: skip
            sources += [locallow] if locallow is not None else []
        if not any(c in company + product for c in "\\\x00"):  # what a key name may hold
            sources.append(registry_source(company, product, evidence[1:]))
        weak = company.casefold() == DEFAULT_COMPANY
        if weak:
            warnings.append(
                "the company is Unity's placeholder DefaultCompany: another game may use the "
                "same folders; its folder is never cleaned up automatically"
            )
        return common.rule_game(
            directory,
            "unity",
            title=product,
            engine_key=f"{company}/{product}",
            sources=sources,
            evidence=evidence,
            warnings=warnings,
            company=company,
            product=product,
        )
