"""The ``[attached_file N] path`` marker grammar has three independent readers.

The frontend serializer (``website/src/utils/fileTokens.ts``) WRITES the marker;
``chat_title.py`` strips it when deriving a session title; and the queue
repository prunes and renumbers it when a queued message is edited. None of the
three imports the others' spelling, so a change to the serializer would leave the
two backend readers matching nothing -- silently: the title keeps the raw marker,
and an edited queued entry loses every attachment list. This pin fails loudly
instead: the literal each side spells must be one and the same string.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.dashboard import chat_title
from kiro_crew.dashboard.slot_queue_repository import (
    _ATTACHMENT_MARKERS,
    ATTACHMENT_META_KEYS,
    _marker_spans,
)

_REPO = Path(__file__).resolve().parents[1]
_FILE_TOKENS_TS = _REPO / "website" / "src" / "utils" / "fileTokens.ts"


def _frontend_marker_words() -> set[str]:
    """Every ``[<word> ...]`` marker word the serializer source spells, in its
    producer template (``[attached_file ${n}]``), its parser regexes and its
    prose (``[attached_file N]``) alike."""
    src = _FILE_TOKENS_TS.read_text(encoding="utf-8")
    return set(re.findall(r"\[(attached_[a-z]+) ", src))


def test_backend_marker_words_are_the_ones_the_frontend_writes():
    words = _frontend_marker_words()
    assert words, "fixture: the serializer source must spell at least one marker"
    for key in ATTACHMENT_META_KEYS:
        assert _ATTACHMENT_MARKERS[key] in words, (
            f"queue repository spells `{_ATTACHMENT_MARKERS[key]}` for meta.{key}; "
            f"the frontend serializer spells {sorted(words)}"
        )


def test_chat_title_prefix_matches_the_queue_repository_marker():
    """chat_title reads the marker through a literal prefix; the repository
    through a formatted token. Pin them to each other through a round trip."""
    path = "/tmp/My Report.pdf"
    token = f"[{_ATTACHMENT_MARKERS['files']} 1] {path}"
    assert _marker_spans(token, _ATTACHMENT_MARKERS["files"], 1, path) == [(0, len(token))]
    stripped = chat_title._strip_attached_file_tokens(f"read {token}", (path,))
    assert "[attached_file" not in stripped, (
        "chat_title did not recognise the marker the queue repository formats -- "
        "the two readers have drifted apart"
    )


def test_frontend_source_is_where_the_pin_expects_it():
    """Guard the guard: a moved serializer would make the first test vacuous."""
    assert _FILE_TOKENS_TS.is_file(), f"serializer not found at {_FILE_TOKENS_TS}"
    assert "prepareSendPayload" in _FILE_TOKENS_TS.read_text(encoding="utf-8")
