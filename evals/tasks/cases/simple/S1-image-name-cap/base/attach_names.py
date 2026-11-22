"""Stored names for images copied into a chat session's attachments folder.

A local image named in a chat message is copied into the session as
``<digest>-<safe name>``. The dashboard and history replay decide by the
file's extension whether a stored file is an image.
"""

from __future__ import annotations

import re

#: Extensions the dashboard serves as images.
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})

_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: Longest name kept after the digest prefix.
MAX_NAME_CHARS = 64


def safe_name(name: str) -> str:
    """A filename-safe, length-bounded form of an LLM-authored basename."""
    safe = _UNSAFE_NAME_RE.sub("_", name).lstrip(".")[:MAX_NAME_CHARS].strip("._")
    return safe or "image"


def stored_name(digest: str, name: str) -> str:
    """The name an image is stored under in the attachments folder."""
    return f"{digest}-{safe_name(name)}"
