"""Structured credential detection, with no regular expressions and no randomness scores.

Three detectors read the text with ``str`` methods and character sets only:

- ``tokens``: whole tokens by published format (prefix, alphabet, length,
  checksum, JOSE structure), plus the AWS secret key paired with a key ID;
- ``armour``: PEM and PGP private-key blocks;
- ``known_key``: the four AWS key names in JSON, assignment, CLI flag and XML
  inputs, with a value-shape gate.

``identifiers`` and ``identifier_values`` mark values that must stay visible
(ARNs, URLs, git object names, public OAuth parameters, request ids). Rules are
data (``table.ROWS``); the loader refuses a ``pattern`` field.

Public API, loaded on first use so importing the package (as the credential plan's
switch read does) builds nothing:

- ``redact(text) -> (redacted_text, spans)``, spans ``(start, end, rule_id, tier)``
  over the input text; only ``tier == "secret"`` spans are replaced;
- ``StreamRedactor().feed(chunk) -> str`` and ``.flush() -> str``; ``.spans`` after
  ``flush()`` equals ``redact(whole)[1]``.

:mod:`.compat` is the seam the credential plan reads; it is off by default.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "redact": "engine",
    "redact_hits": "engine",
    "resolve": "engine",
    "scan": "engine",
    "IDENTIFIER": "model",
    "SECRET": "model",
    "TAG": "model",
    "Hit": "model",
    "Span": "model",
    "StreamRedactor": "stream",
    "ROWS": "table",
    "TABLE": "table",
    "TableError": "table",
    "build_table": "table",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value
