"""Shared hash fallback for the backend slug builders.

Each slug builder filters a user-supplied name down to a small ASCII set and
needs a fallback when nothing survives: a name written wholly in a non-ASCII
script (Chinese, Korean, Arabic, ...) filters to the empty string. Substituting
a bare constant there makes every such name derive the SAME identifier, so
distinct names collide.

Mirror ``knowledge.agent_source.document_slug`` and the frontend Spec Builder's
``slugify`` instead: fall back to a stable hash of the ORIGINAL input, so the
same name always derives the same id and distinct names derive distinct ids.
"""

import hashlib

__all__ = ["slug_hash_fallback"]


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
