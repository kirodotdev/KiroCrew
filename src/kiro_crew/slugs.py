"""Shared helpers for the backend slug builders and slug validators.

Each slug builder filters a user-supplied name down to a small ASCII set and
needs a fallback when nothing survives: a name written wholly in a non-ASCII
script (Chinese, Korean, Arabic, ...) filters to the empty string. Substituting
a bare constant there makes every such name derive the SAME identifier, so
distinct names collide.

Mirror ``knowledge.agent_source.document_slug`` and the frontend Spec Builder's
``slugify`` instead: fall back to a stable hash of the ORIGINAL input, so the
same name always derives the same id and distinct names derive distinct ids.

A slug VALIDATOR refuses rather than derives -- a name that becomes a directory
and an argv component stays a small ASCII alphabet on purpose. What it owes the
user instead is the character it refused, so the dashboard can say which one in
the user's own language (``refused_slug_char``).
"""

import hashlib
import unicodedata

__all__ = ["SLUG_CHARS", "refused_slug_char", "slug_hash_fallback"]

#: Every character a refuse-don't-derive slug validator admits anywhere in a name:
#: the papyrus project name and the code-review-sage namespace name. Their regexes
#: carry the same class; tests pin each one to this set.
SLUG_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


def slug_hash_fallback(original: str, prefix: str) -> str:
    """Return ``<prefix>-<16 hex chars>`` derived deterministically from ``original``.

    The digest is a pure function of the input, so the same name derives the
    same id across calls and processes, while distinct names get distinct ids.
    The ``prefix-`` shape keeps each caller's naming convention, and the
    lowercase-hex charset plus the short fixed length satisfy every caller's
    slug pattern and length cap. ``surrogatepass`` keeps the encoding total:
    a lone surrogate is representable in a ``str`` (JSON escapes decode to
    one), and a strict encode would raise where the builders must not.
    """
    digest = hashlib.sha256(original.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def refused_slug_char(typed: str) -> str:
    """Return the first character of *typed* a slug name cannot hold, as the user typed it.

    Reporting only, never validation: the caller's own pattern decides whether
    the name is accepted, and this answers the follow-up question "which
    character stopped it". *typed* is the name BEFORE the caller's normalizer
    (trim, whitespace to ``-``, lowercase), and each character is judged by what
    that normalizer makes of it: whitespace becomes ``-`` and a letter counts by
    its lowercase form. So the answer is a character the user actually typed --
    ``Отчет`` reports the capital ``О``, ``İstanbul`` reports ``İ`` (whose
    lowercase gains a combining dot) -- never a by-product of lowercasing.
    ``""`` means no single character is to blame, so the name failed on its
    length or on where a character sits (a leading dot, say).

    NFC first, so a decomposed ``u`` + U+0308 is reported as the ``ü`` the user
    typed rather than as a bare combining mark. A character that cannot stand on
    its own in a sentence -- a control or format character (a bidi override, a
    zero-width space), a combining mark -- comes back as its code point
    (``U+202E``) instead, so the message that names it can be neither hidden nor
    reordered by it.
    """
    for char in unicodedata.normalize("NFC", typed):
        if char.isspace() or all(c in SLUG_CHARS for c in char.lower()):
            continue
        if char.isprintable() and unicodedata.category(char)[0] != "M":
            return char
        return f"U+{ord(char):04X}"
    return ""
