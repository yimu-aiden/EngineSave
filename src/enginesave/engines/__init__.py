"""Engine detector registry. Copying files -- backing up the sources a scan found, putting a
snapshot's files back -- needs no engine code; finding the games does, and so does a
restore's check of the game folder it is given (it scans that folder to recognise the game,
``restore.check_game_root``), never the copying itself.

The detectors are imported when they are first asked for, so that importing a small helper
such as ``enginesave.engines.renpy.paths`` does not load every engine (D38).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from enginesave.engines.base import Detector


def default_detectors() -> list[Detector]:
    """Detectors in priority order; the first one that recognises a directory wins."""
    from enginesave.engines.electron import ElectronDetector
    from enginesave.engines.fingerprints import FingerprintDetector
    from enginesave.engines.godot import GodotDetector
    from enginesave.engines.kirikiri import KiriKiriDetector
    from enginesave.engines.renpy.detector import RenPyDetector
    from enginesave.engines.rpgmaker_mv import RpgMakerMvDetector
    from enginesave.engines.rpgmaker_rgss import RgssDetector
    from enginesave.engines.unity import UnityDetector
    from enginesave.engines.unreal import UnrealDetector
    from enginesave.engines.wolf import WolfDetector

    # 0.10.0 (D65): the rule engines take the folders the fingerprint gives them; the
    # fingerprint detector labels the rest (and what their rules cannot settle) unsupported
    rules = [
        RpgMakerMvDetector(),
        RgssDetector(),
        WolfDetector(),
        KiriKiriDetector(),
        UnityDetector(),
        ElectronDetector(),
        UnrealDetector(),
        GodotDetector(),
    ]
    return [RenPyDetector(), *rules, FingerprintDetector()]
