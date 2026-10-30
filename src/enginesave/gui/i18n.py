"""The window's languages (0.11.0, D68): Chinese, the source text, and English.

Every text of the window goes through :func:`_`: the Chinese text is the key, the English
one comes from :data:`enginesave.gui.i18n_en.EN`; a text with values is a template
(``已勾选 {n} 款游戏``
→ ``{n} games ticked``) filled after it is looked up. A module-level constant keeps its Chinese
value (it is evaluated once, at import) and is looked up where it is used, so that a switch of
language needs only the window to be built again. A text the table lacks is shown in Chinese
and remembered (:func:`missing`; the tests fail on any). The command line is English already
and does not use this.

The language is ``gui.json``'s ``language`` (``auto``, ``zh`` or ``en``), chosen in the settings
dialog; ``auto`` -- the default, so the first start asks nothing -- takes Windows' display
language (:func:`default_language`: Chinese → ``zh``, anything else → ``en``).
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

log = logging.getLogger("enginesave.gui")

AUTO, ZH, EN = "auto", "zh", "en"
LANGUAGES = (ZH, EN)
CHOICES = (AUTO, ZH, EN)  # gui.json's language, in the settings dialog's order (D68 3)
NAMES = {ZH: "中文", EN: "English"}  # each language by its own name
CJK = re.compile(r"[　-鿿＀-￯]")
_current = ZH
_missing: set[str] = set()


def set_language(language: str) -> str:
    """Use ``language`` (``zh`` or ``en``; anything else is Chinese) from now on."""
    global _current
    _current = language if language in LANGUAGES else ZH
    return _current


def language() -> str:
    return _current


def _(text: str, /, **values: Any) -> str:
    """``text`` in the window's language, its ``{placeholders}`` filled from ``values``."""
    translated = text
    if _current == EN and isinstance(text, str):
        from enginesave.gui.i18n_en import EN as TABLE

        found = TABLE.get(text)
        if found is None:
            if CJK.search(text) and text not in _missing:
                _missing.add(text)
                log.debug("no English for %r", text)
        elif isinstance(found, tuple):  # (one, many): by the count it is given
            counts = [v for v in values.values() if isinstance(v, int) and v is not True]
            translated = found[0] if counts[:1] == [1] else found[1]
        else:
            translated = found
    return translated.format(**values) if values else translated


def N_(text: str) -> str:  # noqa: N802 - the usual name of the marker
    """A text looked up later (a module-level constant): marked, not translated here."""
    return text


def resolve(choice: str) -> str:
    """The language a ``gui.json`` choice means: ``zh`` or ``en`` as such, anything else (``auto``)
    Windows' display language."""
    return choice if choice in LANGUAGES else default_language()


def missing() -> set[str]:
    """The texts shown in Chinese because the English table lacks them."""
    return set(_missing)


def default_language() -> str:
    """Windows' display language: Chinese → ``zh``, anything else → ``en``."""
    if sys.platform != "win32":
        return ZH
    try:
        import ctypes

        langid = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return ZH
    return ZH if langid & 0x3FF == 0x04 else EN  # LANG_CHINESE
