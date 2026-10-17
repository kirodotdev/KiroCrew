"""Deterministic widget-artifact slug derivation (Python side of a two-language pair).

Every ``<mcwidget>`` emitted in chat is auto-registered as an artifact keyed by a
slug derived from the parent message timestamp plus a fingerprint of the widget
body. The frontend (``website/src/lib/widgetSlug.ts``) derives the SAME slug from
the SAME inputs so a ``WidgetFrame`` impression can look up the artifact the
backend registered without the two sides exchanging an id. The index-keyed form
remains available for image artifact identities.

**This file and ``widgetSlug.ts`` MUST stay byte-identical in output.** If they
diverge, every auto-registered widget becomes invisible to the frontend (the
probe 404s) and the star button silently creates a duplicate at the frontend's
slug — the exact save-then-refresh duplication the deterministic scheme exists to
prevent. ``test/test_widget_slug.py`` pins known input -> output vectors, and
``website/src/test/widgetSlug.test.ts`` asserts the same vectors on the TS side;
change one without the other and both suites fail.

The hash is two FNV-1a passes with the 32-bit prime (``0x01000193``) and two
different offset bases, emitted as 16 lowercase hex chars. No crypto properties
are needed — this is an opaque unique-enough id, not a security boundary.

Note the 32-bit prime is used for BOTH passes, deliberately: JavaScript's
``Math.imul`` truncates operands to 32-bit before multiplying, so the 64-bit FNV
prime would silently collapse to ``0x1b3`` there. Python has no such limit, so
matching the JS behavior means explicitly masking to 32 bits here and using the
same prime.
"""

from __future__ import annotations

import re

#: FNV-1a 32-bit prime, used for both passes (see module docstring).
_FNV_PRIME = 0x01000193

#: Offset bases for the two independent passes. Two different bases give 64 bits
#: of namespace from one prime.
_FNV_OFFSET_A = 0x811C9DC5
_FNV_OFFSET_B = 0x62B82175

#: 32-bit mask — mirrors the implicit truncation of JS ``Math.imul`` + ``>>> 0``.
_U32 = 0xFFFFFFFF

#: A derived slug is exactly 16 lowercase hex chars (two 32-bit words). Used to
#: recognize (and re-derive) auto-registered widget slugs.
DERIVED_SLUG_RE = re.compile(r"^[0-9a-f]{16}$")


def _fnv_pair(seed: str) -> tuple[int, int]:
    """Return the two FNV-1a words for ``seed``, matching JS UTF-16 iteration."""
    h1 = _FNV_OFFSET_A
    h2 = _FNV_OFFSET_B
    for ch in seed:
        code = ord(ch)
        # JS iterates UTF-16 code units via charCodeAt. Python iterates code
        # points, so an astral character (one code point, two surrogates in JS)
        # would hash differently. Split it into surrogates to match JS exactly.
        if code > 0xFFFF:
            offset = code - 0x10000
            for unit in (0xD800 + (offset >> 10), 0xDC00 + (offset & 0x3FF)):
                h1 = ((h1 ^ unit) * _FNV_PRIME) & _U32
                h2 = ((h2 ^ unit) * _FNV_PRIME) & _U32
            continue
        h1 = ((h1 ^ code) * _FNV_PRIME) & _U32
        h2 = ((h2 ^ code) * _FNV_PRIME) & _U32
    return h1, h2


def derive_widget_slug(message_ts: str, widget_index: int) -> str:
    """Return the deterministic 16-hex-char index slug used by image artifacts.

    ``message_ts`` is the parent message's timestamp (any stable string id);
    ``widget_index`` is the 0-based ordinal within that message.

    This ordinal form is backend-only: its sole consumer is
    ``image_artifacts._derive_image_slug``, which seeds ``message_ts`` with a
    ``"<ts>#image"`` namespace. It has no counterpart in
    ``website/src/lib/widgetSlug.ts``. Widget identity is keyed on the body via
    :func:`derive_widget_body_slug`, which must match ``deriveWidgetBodySlug``
    on the TS side.
    """
    h1, h2 = _fnv_pair(f"{message_ts}#{widget_index}")
    return f"{h1:08x}{h2:08x}"


def derive_widget_body_slug(message_ts: str, body: str) -> str:
    """Return the deterministic 16-hex-char slug for a widget body.

    Must match ``deriveWidgetBodySlug`` in ``website/src/lib/widgetSlug.ts``.
    """
    h1, h2 = _fnv_pair(f"{message_ts}#w:{body}")
    return f"{h1:08x}{h2:08x}"
