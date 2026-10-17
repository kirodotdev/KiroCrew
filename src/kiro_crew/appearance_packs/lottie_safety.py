"""Does a Lottie document ask the player to FETCH something? -- the server's copy.

The player refuses such a clip before ``loadAnimation`` (see
``website/src/lib/appearancePacks/lottieSafety.ts`` for why), so a crew wearing
it shows the seeded ghost and the reason reaches only the browser console. The
import answer is the one place the person who picked the file is reading, so the
import names the file there. That only helps if the two predicates agree: a
server that warned on a clip the player draws, or stayed quiet on one it
refuses, is worse than no warning. This module is a line-for-line port of the
TypeScript one, and ``website/src/test/fixtures/lottieRemoteAssetCases.json``
runs the same cases through both.

The port has to answer the way JavaScript does, not the way Python reads the
same words:

* ``undefined`` is a MISSING key. JSON has no ``undefined``, so ``null`` is a
  present value, never the same thing.
* ``=== 1`` and ``=== 0`` are number comparisons. Python's ``True == 1`` and
  ``False == 0`` are not, so a boolean never counts as either number here.
* ``fPath`` is read for JavaScript truthiness, where ``{}`` and ``[]`` are TRUE.

This decides what the import SAYS, never what it ACCEPTS: a pack may carry other
slots that draw fine, and the player's refusal stays the safety boundary.
Nothing here fetches anything.
"""

from __future__ import annotations

import json
import math
from typing import Any

_MISSING = object()


def _is_js_number(value: Any, target: int) -> bool:
    """``value === target`` for a JSON number, with booleans excluded."""
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value == target


def _js_truthy(value: Any) -> bool:
    """JavaScript truthiness of a JSON value. Containers are always truthy."""
    if value is _MISSING or value is None or value is False:
        return False
    if isinstance(value, (dict, list)):
        return True
    if isinstance(value, float):
        return not math.isnan(value) and value != 0
    if isinstance(value, int):
        # An int, never ``math.isnan``: JSON integers are unbounded in Python,
        # and one past the float range overflows the conversion.
        return value != 0
    if isinstance(value, str):
        return value != ""
    return True


def _asset_is_remote(entry: Any) -> bool:
    """Mirror of ``assetIsRemote``: an embedded asset marks itself ``e: 1`` and
    carries a ``data:`` URI in ``p``; anything else is a request."""
    if not isinstance(entry, dict):
        return False
    p = entry.get("p", _MISSING)
    u = entry.get("u", _MISSING)
    # A precomp asset is a nested layer list, not a file.
    if isinstance(entry.get("layers"), list) and p is _MISSING and u is _MISSING:
        return False
    if p is _MISSING and u is _MISSING:
        return False
    # A non-empty ``u`` is a directory prefix, which only exists to be requested.
    if isinstance(u, str) and u != "":
        return True
    if not _is_js_number(entry.get("e", _MISSING), 1):
        return True
    return not isinstance(p, str) or not p.startswith("data:")


def _font_origin_is_local(value: Any) -> bool:
    """Mirror of ``fontOriginIsLocal``: absent, ``null``, ``''``, ``'n'``, ``0``."""
    return (
        value is _MISSING or value is None or value == "" or value == "n" or _is_js_number(value, 0)
    )


def _font_is_remote(entry: Any) -> bool:
    """Mirror of ``fontIsRemote``: fetched unless it is a family the document only names."""
    if not isinstance(entry, dict):
        return False
    if _js_truthy(entry.get("fPath", _MISSING)):
        return True
    if not _font_origin_is_local(entry.get("fOrigin", _MISSING)):
        return True
    return not _font_origin_is_local(entry.get("origin", _MISSING))


def references_remote_asset(doc: Any) -> bool:
    """Mirror of ``referencesRemoteAsset``: ``True`` when the player would request
    something. Total: anything that is not a document answers ``False``."""
    if not isinstance(doc, dict):
        return False
    assets = doc.get("assets")
    if isinstance(assets, list) and any(_asset_is_remote(a) for a in assets):
        return True
    fonts = doc.get("fonts")
    if isinstance(fonts, dict):
        font_list = fonts.get("list")
        if isinstance(font_list, list) and any(_font_is_remote(f) for f in font_list):
            return True
    return False


def _refuse_constant(name: str) -> Any:
    # ``JSON.parse`` rejects ``NaN`` and ``Infinity``; Python's parser accepts
    # them by default. Refusing here keeps "the player parses this" the same answer.
    raise ValueError(f"not JSON: {name}")


def text_references_remote_asset(text: str) -> bool:
    """:func:`references_remote_asset` over a clip's raw text, parsed as ``JSON.parse``
    would. Text the player cannot parse answers ``False``: it is refused for that,
    not for fetching. One known gap: a document nested deeper than Python's
    recursion limit also answers ``False`` here, though ``JSON.parse`` reads it.
    That costs only the warning; the player still refuses the clip."""
    try:
        doc = json.loads(text, parse_constant=_refuse_constant)
    except (RecursionError, ValueError):
        return False
    return references_remote_asset(doc)
