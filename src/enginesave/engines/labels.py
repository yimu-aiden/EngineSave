"""Engine names for people (no detector code: backup and restore may use them)."""

from __future__ import annotations

ENGINE_LABELS = {
    "unity": "Unity",
    "rpgmaker_mv_mz": "RPG Maker MV/MZ",
    "rpgmaker_mv": "RPG Maker MV",  # 0.10.0 (D65): told apart, saves located
    "rpgmaker_mz": "RPG Maker MZ",
    "rpgmaker_xp_vx_vxace": "RPG Maker XP/VX/VX Ace",
    "rpgmaker_xp": "RPG Maker XP",
    "rpgmaker_vx": "RPG Maker VX",
    "rpgmaker_vxace": "RPG Maker VX Ace",
    "godot": "Godot",
    "unreal": "Unreal Engine",
    "wolf_rpg": "Wolf RPG Editor",
    "kirikiri": "KiriKiri",
    "electron": "Electron",
    "nwjs": "NW.js",
    "emuera": "Emuera",  # 0.9.0 (D52 4): Era games; the ERB scripts are never read
}


def engine_label(engine: str) -> str:
    """An engine's name for people (``renpy`` stays as it is)."""
    return ENGINE_LABELS.get(engine, engine)
