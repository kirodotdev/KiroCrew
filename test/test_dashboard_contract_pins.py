"""Regression pins for dashboard contracts that are currently carried by convention.

Each test here asserts an invariant the code already satisfies, so none of them changes
behaviour. They exist because each invariant is enforced by agreement between two places
that nothing checks: a default in two request handlers, and a filename derivation
against the set that enumerates it. (The slot-owned key set the two save forms write is
pinned behaviourally at its owner, in ``test_slot_metadata_codec.py``.)
"""

from __future__ import annotations

import inspect

from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.state import _ChatSlot


def test_an_omitted_ephemeral_flag_stays_memory_only():
    """An omitted ``ephemeral`` must not write a caller's content to disk.

    Durability is opt-IN via an explicit ``ephemeral: false``. Flipping the default would
    be a one-way contract change for every external caller that names nothing, and both
    in-repo callers pass it explicitly, so no in-repo test would notice.
    """
    from kiro_crew.dashboard import chat_handlers as ch

    for fn in (ch.api_chat_slot_context, ch.api_chat_slot_note):
        src = " ".join(inspect.getsource(fn).split())
        assert 'body.get("ephemeral", True)' in src, (
            f"{fn.__name__} defaults `ephemeral` to False, so a caller that names nothing "
            "has its content written to disk -- a contract change it never asked for"
        )
        # Control: the flag must still be READ, or a default of True is unreachable.
        assert 'body.get("ephemeral"' in src


def test_the_ephemeral_flag_is_stamped_on_the_entry():
    """``ephemeral`` is recorded on the entry rather than accepted and discarded.

    A flag dropped at the boundary cannot be honoured by anything downstream, and the
    request has already been answered 200 by then.
    """
    from kiro_crew.dashboard import chat_handlers as ch

    src = inspect.getsource(ch._build_pending_context_entry)
    assert '"ephemeral"' in src, "the flag must be stamped, not discarded at the boundary"
    # Positive control: the fields that ARE stored are still stored.
    assert '"content": content' in src
    assert '"source": source' in src
    assert '"injectedAt"' in src


def test_transcript_naming_is_closed_over_transcript_stems():
    """``_path`` may only produce names ``transcript_stems`` enumerates.

    Any consumer that accepts the enumerated set and refuses everything else is safe only
    while that holds: a transcript stored under a name the set omits would be refused. The
    two functions derive names by the same two rules, so the set is closed by construction
    -- but only while they agree, and a third derivation added to ``_path`` alone would
    break it silently.
    """
    from kiro_crew.history import ConversationLog, transcript_stem, transcript_stems

    src = inspect.getsource(ConversationLog._path)
    # Every filename in `_path` is built through `_safe_key`, so a new derivation
    # cannot slip in without changing this count.
    assert src.count("_safe_key(") == 2, (
        "`_path` gained or lost a filename derivation -- mirror it in "
        f"`transcript_stems` and update this pin. Source:\n{src}"
    )
    assert src.count("legacy_key(") == 1, "`_path`'s legacy fallback changed shape"

    for key in (
        "chat-1785370133",
        "slack:C123:1785370133.085469",
        "slack:1785370133.085469",
        "1785370133.085469",
        "discord:dm:12345",
        "cron:job-11",
        "dashboard:local",
    ):
        stems = transcript_stems(key)
        assert stems, f"{key!r} enumerated no stem at all"
        assert stems[0] == transcript_stem(key), f"{key!r}: canonical stem must be stems[0]"


def test_a_message_less_slot_still_short_circuits_the_save(tmp_path):
    """A titled slot with no messages reports a successful save under ``force``.

    The early return exists for exactly this shape, and a caller cannot tell "nothing to
    write" from "the write failed" if it starts reporting False.
    """
    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-ctx-nomsg-empty")
    slot.title = "t"
    slot._titled = True
    state._slots[slot.key] = slot
    assert _save_slot_to_history(state, slot, force=True) is True
