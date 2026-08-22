"""Lexical core for crew variables: name/value validation and ``{{name}}`` expansion.

This module is deliberately a LEAF: it imports nothing from ``kiro_crew``. Every
consumer of variables sits in a different subsystem — config loading, context
assembly, the dashboard chat runner, cron dispatch, the autonudge handler — and a
module that reached back into any of them would create the same import cycle
``cron.py`` already works around by duplicating the ``$skill`` regex.

Two invariants here carry the security argument for the whole feature, so they are
enforced in the grammar rather than left to callers:

* Substitution is SINGLE-PASS. A value is inserted verbatim and never rescanned,
  so a value containing ``{{other}}`` cannot chain into another variable and a
  cycle is unrepresentable. ``re.sub`` with a replacement *callable* also means a
  value containing ``\\1`` or ``\\g<0>`` is inserted literally rather than
  interpreted as a backreference.
* An unknown name is left BYTE-IDENTICAL rather than substituted with an empty
  string. Blanking is the more dangerous failure: it silently turns
  ``curl {{baseUrl}}/health`` into ``curl /health``, which reads as a valid
  instruction, whereas a surviving ``{{baseUrl}}`` is visibly wrong.

Values are trusted only to the level of text the user typed themselves. That
holds because callers pass ONLY user-authored text here — never a SKILL.md body,
an ``@prompt`` file, or a steering file, which can arrive from a cloned repo or
the public skill registry.
"""

from __future__ import annotations

import re
from typing import Mapping

# Prompt tokens the gateway already resolves. A user variable may not take one of
# these names: expansion runs after those passes, so a collision would be an
# inert shadow that looks like it should work.
RESERVED_TOKENS: frozenset[str] = frozenset(
    {
        "MAX_SUBAGENTS",
        "VERBOSITY_BLOCK",
        "WIDGET_BLOCK",
        "STOP_FILE",
        "ALIAS",
        "bot_name",
    }
)

# Names are ASCII identifiers so a token can never be confused with surrounding
# prose, and so the same spelling is legal in a shell, a URL and a JSON key.
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# Interior whitespace is tolerated ({{ baseUrl }}) because hand-typed tokens
# commonly carry it; the captured name never does.
TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")

# A variable is a value to paste into a sentence, not a document. The cap keeps a
# runaway value from displacing the surrounding instruction.
MAX_VALUE_LEN = 4096

# Tab is the one control character allowed: it appears in legitimate pasted
# values. Every other C0 code and DEL is rejected, newline included — a value
# spanning lines could otherwise forge a context header in the assembled prompt.
_FORBIDDEN_CHARS = frozenset(chr(c) for c in range(0x20) if c != 0x09) | {chr(0x7F)}


def validate_pair(key: object, value: object) -> tuple[str | None, str]:
    """Validate one variable pair.

    Returns ``(key, coerced_value)`` when the pair is usable, or
    ``(None, reason)`` when it is not. The reason is a short phrase intended to
    be logged next to the offending key and scope; it never quotes the value,
    since a rejected value may be long or contain the very characters that got
    it rejected.

    A bool/int/float is coerced rather than refused: those are the types a
    hand-edited JSON config produces for an unquoted value, and refusing them
    would reject a config that looks obviously correct to the person who wrote
    it. Bools take their JSON spelling so the expanded text matches the config.
    """
    if not isinstance(key, str) or not NAME_RE.match(key):
        return None, "name must start with a letter and contain only letters, digits, underscore"
    if key in RESERVED_TOKENS:
        return None, "name is reserved for a built-in prompt token"

    if isinstance(value, bool):
        coerced = "true" if value else "false"
    elif isinstance(value, str):
        coerced = value
    elif isinstance(value, (int, float)):
        coerced = str(value)
    else:
        return None, "value must be a string, boolean, or number"

    if len(coerced) > MAX_VALUE_LEN:
        return None, f"value exceeds {MAX_VALUE_LEN} characters"
    if any(ch in _FORBIDDEN_CHARS for ch in coerced):
        return None, "value contains a control character other than tab"
    if "{{" in coerced:
        # A value may not carry the opening delimiter, which is what makes
        # expansion IDEMPOTENT rather than merely single-pass-per-call.
        #
        # ``expand`` never rescans what it substituted, so one call cannot expand
        # a token that arrived from a value. But a message can legitimately cross
        # more than one expansion boundary — an auto-nudge body is rendered with
        # the loop's armed crew and then passes through the transport's own
        # inbound expansion — and two single-pass calls in series would together
        # resolve a token embedded in a value, which is the indirect expansion
        # the single-pass rule exists to forbid. Refusing the delimiter here
        # closes that at the source, so no boundary has to know how many other
        # boundaries ran, and adding one later cannot reintroduce it.
        return None, "value may not contain '{{'"
    return key, coerced


def expand(text: str, values: Mapping[str, str]) -> tuple[str, frozenset[str]]:
    """Substitute ``{{name}}`` tokens in *text* from *values*, in a single pass.

    Returns the result and the set of names that were referenced but absent from
    *values*. Those tokens are left in place; callers surface them so a typo is
    visible instead of silently blanking.

    With an empty mapping the input object is returned unscanned, which keeps the
    expander free on the overwhelmingly common path of a config that defines no
    variables at all.
    """
    if not values or not text:
        return text, frozenset()

    unresolved: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        unresolved.add(name)
        return match.group(0)

    return TOKEN_RE.sub(_replace, text), frozenset(unresolved)
