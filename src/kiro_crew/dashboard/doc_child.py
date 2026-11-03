"""A docwork conductor opening another page: a doc or a board session as its child.

``session_create`` opens ordinary chat sessions. A doc or board conductor may ask for
a page instead (``kind``), with its starting text (``content``): a board turning its
issues into one doc each, or a doc laying its plan out on a board. The child is a
session like any other in the sidebar; what makes it a page is the doc id bound at
birth, exactly as a person's own create binds one.

Two limits keep this from running away:

* Only a page's conductor may open a page. Any other caller keeps the plain create:
  the doc create on the person's own route stays the person's.
* A chain of pages opened by conductors stops at :data:`MAX_DOC_CHAIN_DEPTH` below
  the page a person made, counted through each session's recorded creator, so a doc
  that opens a board that opens a doc cannot keep going by accident.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from kiro_crew.dashboard import doc_files
from kiro_crew.dashboard.session_control_parts.caller_access import SessionControlError

#: The page kinds a conductor may open. A codebase or dashboard page needs inputs a
#: starting text cannot give (a work tree, a page), so it stays the person's to make.
DOC_CHILD_KINDS = ("doc", "board")

#: How many pages deep a chain of conductor-opened pages may go below the page a
#: person made: their board opens a doc (1), that doc opens a board (2), and that
#: board may open nothing more.
MAX_DOC_CHAIN_DEPTH = 2

#: The largest starting text accepted, far below a doc's own cap: it arrives in one
#: tool call, written by a model.
MAX_CHILD_CONTENT_CHARS = 200_000

# The new-doc picks the page makes itself (``docMeta.ts``: DOC_ICONS, GRADIENTS), so
# a conductor's doc looks like one a person just made.
_DOC_ICONS = ("📄", "📝", "📌", "📎", "🗂", "📊", "📈", "🧾", "📚", "🗓")
_GRADIENT_COUNT = 8
_FRONT = "---\n"


def doc_chain_depth(state: Any, slot: Any) -> int:
    """How many conductor-opened pages *slot* sits below a page a person made.

    A page whose creator is not a page (a person's own create records no creator; a
    chat that opened it is not a page) is depth 0. Each hop up a page's recorded
    creator that is itself a page adds one.
    """
    depth = 0
    seen: set[str] = set()
    current = slot
    while current is not None:
        key = str(getattr(current, "key", "") or "")
        if key in seen:
            break
        seen.add(key)
        creator_key = str(getattr(current, "_created_by", "") or "")
        creator = state.get_slot(creator_key) if creator_key else None
        if creator is None or not getattr(creator, "_doc", ""):
            break
        depth += 1
        current = creator
    return depth


def check_doc_child(state: Any, caller_slot: Any, kind: str, content: Any) -> str:
    """The page kind to open, after every refusal; raises :class:`SessionControlError`.

    Runs before the create allocates anything, so a refused page creates nothing.
    """
    kind = (kind or "").strip()
    if kind not in DOC_CHILD_KINDS:
        raise SessionControlError(
            f"kind must be one of: {', '.join(DOC_CHILD_KINDS)}", code="invalid_doc_kind"
        )
    if not getattr(caller_slot, "_doc", ""):
        raise SessionControlError(
            "only a doc or board session's conductor can open a page; open an ordinary "
            "session instead",
            code="doc_child_not_conductor",
            status=403,
        )
    if doc_chain_depth(state, caller_slot) >= MAX_DOC_CHAIN_DEPTH:
        raise SessionControlError(
            f"this page is already {MAX_DOC_CHAIN_DEPTH} pages below one a person made, "
            "so it cannot open another; report back to the conductor that opened it",
            code="doc_chain_too_deep",
            status=403,
        )
    if content is not None:
        if not isinstance(content, str):
            raise SessionControlError("content must be a string", code="invalid_field_type")
        if len(content) > MAX_CHILD_CONTENT_CHARS:
            raise SessionControlError(
                f"content is over {MAX_CHILD_CONTENT_CHARS} characters", code="doc_content_too_long"
            )
        if kind == "board" and content.strip():
            try:
                data = json.loads(content)
            except ValueError as exc:
                raise SessionControlError(
                    "a board's content must be its JSON file", code="invalid_board_content"
                ) from exc
            if not isinstance(data, dict):
                raise SessionControlError(
                    "a board's content must be a JSON object", code="invalid_board_content"
                )
    return kind


def child_file_text(kind: str, content: str | None) -> str:
    """The new page's starting file: a doc gets a random icon and cover, as a person's
    new doc does, unless its text already carries front matter."""
    text = content or ""
    if kind != "doc" or text.startswith(_FRONT):
        return text
    icon = _DOC_ICONS[secrets.randbelow(len(_DOC_ICONS))]
    cover = f"gradient:{secrets.randbelow(_GRADIENT_COUNT)}"
    return f"---\nicon: {icon}\ncover: {cover}\n---\n{text}"


def write_child_doc(doc_id: str, kind: str, content: str | None) -> str:
    """Write the new page's starting file; returns the text written. Blocking: call it
    off the event loop. The file must not exist yet, which a fresh id guarantees."""
    text = child_file_text(kind, content)
    doc_files.write_doc(doc_id, text, expected_version="")
    return text
