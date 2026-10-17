"""The example key, the source root and the screen readers the split tests share.

Read by test_split_credential_boundary.py and its
``test_split_credential_boundary_<topic>.py`` parts, which are split by behaviour.
"""

from __future__ import annotations

import ast
from pathlib import Path

import kiro_crew
from kiro_crew.messaging.display_safety import joins_to_a_credential
from kiro_crew.messaging.renderer import _default_redactor

#: AWS's own documented example key, the same fixture the display-safety tests
#: use, so no real credential shape is introduced anywhere.
KEY = "AKIAIOSFODNN7EXAMPLE"
HEAD, TAIL = KEY[:10], KEY[10:]

SRC = Path(kiro_crew.__file__).parent

#: The splitters that ACCEPT a redactor, which is what makes "pass one" a
#: well-formed demand. ``chunk_text`` takes no such parameter: it is the blind
#: fixed-width last resort, and its safety comes from the subject it is handed --
#: a chunk already cut and graded -- rather than from a scan it could run itself.
SPLITTERS = frozenset({"split_markdown_safe", "chunk_utf8_bytes"})


def _rejoins(chunks: list[str]) -> bool:
    """Does any boundary hand the reader a key neither chunk holds?

    The rendered pair, because a platform drops the whitespace at a message's
    edges: the test models the screen the same way the splitter does.
    """
    return any(
        joins_to_a_credential(chunks[i].rstrip(), chunks[i + 1].lstrip(), _default_redactor)
        for i in range(len(chunks) - 1)
    )


def _on_screen(chunks: list[str]) -> str:
    """What a reader scrolling the delivered messages reads, as one string.

    Each message's edge whitespace goes, because the platform drops it, and a
    chunk holding nothing else contributes nothing at all -- which is why it
    separates nothing.
    """
    return "".join(chunk.strip() for chunk in chunks)


def _name_of(node: ast.expr) -> str:
    """The bare name a call or reference resolves to, attribute access included."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""
