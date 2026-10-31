"""The app's catalogs, read by its backend.

An editor binding's answer is shown by the host, not by this app's bundle, so a
sentence the backend writes (why there is no answer: no server installed, the
server still starting) is the one place this app's words reach the reader
without passing through its own UI. The same catalogs the bundle compiles in
live in ``locales/`` next to this module; this reads them for those sentences.
The language is not chosen here: the routes pass the one the dashboard resolved
(see ``backend/routes.py``).

``{{name}}`` placeholders match the bundle's, so a string is written once.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_LOCALES = Path(__file__).resolve().parent / "locales"
_DEFAULT = "en"
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@lru_cache(maxsize=None)
def _catalog(lang: str) -> dict[str, str]:
    path = _LOCALES / f"{lang}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def has(lang: str, key: str) -> bool:
    """Whether *lang*'s own catalog carries *key* (no English fallback)."""
    return key in _catalog(lang)


def plural(lang: str, stem: str, count: int, **values: object) -> str:
    """``<stem>_one`` for one in a language that has the form, else ``<stem>_other``.

    The dashboard's catalogs follow CLDR: Japanese, Korean and Chinese carry only
    ``_other``, so asking for ``_one`` there must not fall through to English.
    """
    key = f"{stem}_one" if count == 1 and has(lang, f"{stem}_one") else f"{stem}_other"
    return t(lang, key, count=count, **values)


def t(lang: str, key: str, **values: object) -> str:
    """The string for *key* in *lang*, English when missing, the key when unknown."""
    text = _catalog(lang).get(key) or _catalog(_DEFAULT).get(key) or key
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)
