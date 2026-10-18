"""Reading another session's transcript through ``session_control.read_messages``.

Covers the cursor contract: ``total`` excludes ``done`` markers and streaming
``chunk`` rows, and on a trimmed session ``total``, row ``index`` and
``next_since`` count the durable frozen prefix, so consecutive ``since`` pages
do not overlap. A ``since`` past the end or below that prefix is refused with a
409 ``cursor_unavailable``, and the fallback tail read returns the newest rows.

Also covers the ``limit`` tail read, the ``streaming`` and ``queue_depth``
fields, truncation at ``MAX_READ_CONTENT_CHARS`` after credential redaction, and
the refusal of a ``limit`` above ``MAX_READ_MESSAGES``.
"""

from __future__ import annotations

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
)

from kiro_crew.dashboard import session_control as sc

# ── Reading: cursor semantics ────────────────────────────────────────────────


def test_completion_markers_never_advance_the_cursor(tmp_path):
    """``done`` rows are appended but never persisted, so counting them drifts.

    Mutation guard: a cursor that counts them names a position rehydration
    cannot reproduce, so ``since=total`` skips real messages after a restart.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("assistant", "the answer", "msg msg-a")
    target.append("done", "", "done")

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert out["total"] == 1, "a done marker must not count toward the cursor"
    assert [m["role"] for m in out["messages"]] == ["assistant"]


def test_a_cursor_past_the_end_is_refused_rather_than_clamped(tmp_path):
    """A shrunk transcript must not silently drop the rows that replaced the old tail.

    Rewind and regenerate move ``total`` backwards under a caller still holding the
    old position. Clamping that cursor to ``total`` starts the read at the end, so
    every replacement row below it is skipped permanently and nothing in the
    response says so -- the same silent-mis-window failure the trimmed-session
    guard already refuses loudly.

    Mutation guard: restoring `min(since, total)` returns rows instead of raising.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for n in range(4):
        target.append("user", f"m{n}", "msg msg-u")

    with pytest.raises(sc.SessionControlError) as exc:
        sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", since=99)

    assert exc.value.code == "cursor_unavailable"
    assert exc.value.status == 409


def test_a_credential_at_the_truncation_boundary_is_still_redacted(tmp_path):
    """Redaction runs over the whole message, then the slice happens.

    Mutation guard: truncating first cuts the secret into a prefix the scanner
    does not match, and that fragment ships to the caller.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    secret = "ghp_" + "B" * 36
    # Straddle the boundary: only the first 10 chars of the secret survive a
    # naive slice, and a 10-char fragment does not match the credential
    # scanner — so it is exactly what leaks when the order is wrong.
    filler = "x" * (sc.MAX_READ_CONTENT_CHARS - 10)
    surviving_fragment = secret[:10]
    target.append("assistant", filler + secret, "msg msg-a")

    row = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")["messages"][0]

    assert (
        surviving_fragment not in row["content"]
    ), "a truncated credential prefix escaped redaction"
    assert row["truncated"] is True


def test_the_cursor_stops_before_the_streaming_tail(tmp_path):
    """Chunk rows are deleted when the segment flushes, so the cursor skips them.

    Mutation guard: counting them inflates ``total``; the flush shrinks the list
    back under it, and the next ``since=total`` read misses the finished reply
    permanently.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("user", "go", "msg msg-u")
    for piece in ("par", "tial", " reply"):
        target.append("chunk", piece, "")

    mid = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert mid["total"] == 1, "streaming chunks must not advance the cursor"
    assert [m["role"] for m in mid["messages"]] == ["user"]
    assert mid["streaming"] is True

    # Stand in for _flush_segment: drop the trailing chunk run, append the real
    # assistant message in its place.
    del target.messages[1:]
    target.append("assistant", "partial reply", "msg msg-a")

    after = sc.read_messages(
        state, caller_session_key=_key(caller), target="chat-2", since=mid["total"]
    )

    assert [m["content"] for m in after["messages"]] == ["partial reply"]
    assert "streaming" not in after


# ── Reading ──────────────────────────────────────────────────────────────────


def test_read_returns_the_tail_with_a_total_to_poll_from(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(5):
        target.append("assistant", f"line {i}", "msg msg-a")

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", limit=2)

    assert out["total"] == 5
    assert [m["content"] for m in out["messages"]] == ["line 3", "line 4"]
    assert [m["index"] for m in out["messages"]] == [3, 4]
    assert out["running"] is False


def test_read_since_returns_only_what_arrived_after(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(3):
        target.append("assistant", f"old {i}", "msg msg-a")
    first = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    target.append("assistant", "brand new", "msg msg-a")

    second = sc.read_messages(
        state, caller_session_key=_key(caller), target="chat-2", since=first["total"]
    )

    assert [m["content"] for m in second["messages"]] == ["brand new"]


def test_a_stale_cursor_after_a_shrink_is_recoverable(tmp_path):
    """A compacted transcript shrinks; the poller must be able to SEE what replaced it.

    Clamping the stale cursor to ``total`` looks friendlier -- it answers
    ``messages: []`` and hands back a plausible cursor -- but the rows below the
    clamp are the replacement content, and a cursor never moves backwards, so they
    are skipped permanently while the response reads as "nothing new". Refusing
    sends the caller to a tail read, which actually returns that content, so the
    loud answer is the recoverable one.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("assistant", "what replaced the old tail", "msg msg-a")

    with pytest.raises(sc.SessionControlError) as exc:
        sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", since=999)
    assert exc.value.code == "cursor_unavailable"

    # The refusal names the fallback, and the fallback shows the replacement row.
    recovered = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert [m["content"] for m in recovered["messages"]] == ["what replaced the old tail"]

    # A cursor exactly AT the end is not stale -- it is an up-to-date poller.
    caught_up = sc.read_messages(
        state, caller_session_key=_key(caller), target="chat-2", since=recovered["total"]
    )
    assert caught_up["messages"] == []


def test_read_truncates_a_huge_message_and_says_so(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("assistant", "y" * (sc.MAX_READ_CONTENT_CHARS + 500), "msg msg-a")

    row = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")["messages"][0]

    assert row["truncated"] is True
    assert len(row["content"]) <= sc.MAX_READ_CONTENT_CHARS


def test_read_rejects_an_out_of_range_limit(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "chat-2")
    with pytest.raises(sc.SessionControlError):
        sc.read_messages(
            state,
            caller_session_key=_key(caller),
            target="chat-2",
            limit=sc.MAX_READ_MESSAGES + 1,
        )


def test_the_read_cursor_is_absolute_across_a_trimmed_window(tmp_path):
    """Window length freezes at the retention cap; `total` and the indexes must not.

    Mutation guard: deriving `total` from `len(slot.messages)` makes it freeze at
    the cap, so a caller cannot tell how much history exists. Basing it on
    `_disk_older_count` instead of the durable counter shifts every position by
    the transient rows that were trimmed (here: 20), which this pins.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(3):
        target.append("assistant", f"live {i}", "msg msg-a")
    # Stand in for the trim: 5,000 rows aged into the frozen prefix, of which
    # 20 were transient (never returned by a durable read).
    target._disk_older_count = 5000
    target._disk_older_durable_count = 4980

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert out["total"] == 4983, "total must count the DURABLE frozen prefix + the window"
    assert [m["index"] for m in out["messages"]] == [4980, 4981, 4982]
    # Positions are durable-only on both sides of the trim boundary, so the
    # cursor stays exact and is still offered on a trimmed session.
    assert out["next_since"] == 4983
    assert "cursor_exact" not in out, "an exact cursor needs no caveat"


def test_cursor_pagination_stays_exact_once_rows_have_been_trimmed(tmp_path):
    """Two consecutive `since` pages on a trimmed session never overlap.

    This is the regression the durable counter exists for: `_disk_older_count`
    counts every trimmed row including transient ones, so basing positions on it
    advances the base with no durable row behind it — every position shifts and
    a `since` read serves a durable message the caller already had. Basing on
    the durable-only counter keeps the two spaces aligned.

    Mutation guard: swapping the base back to `_disk_older_count` makes the
    second page re-serve the first page's rows (an overlap this asserts away);
    on the pre-fix code the first `since` read raised `cursor_unavailable`.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(4):
        target.append("assistant", f"live {i}", "msg msg-a")
    # A trim that folded transient rows into the frozen prefix: the all-rows
    # counter and the durable counter disagree by 20.
    target._disk_older_count = 5000
    target._disk_older_durable_count = 4980

    first = sc.read_messages(
        state, caller_session_key=_key(caller), target="chat-2", since=4980, limit=2
    )
    assert [m["content"] for m in first["messages"]] == ["live 0", "live 1"]
    assert first["next_since"] == 4982

    second = sc.read_messages(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        since=first["next_since"],
        limit=2,
    )
    assert [m["content"] for m in second["messages"]] == ["live 2", "live 3"]

    served_once = {m["index"] for m in first["messages"]}
    assert served_once.isdisjoint(
        {m["index"] for m in second["messages"]}
    ), "consecutive since pages must never re-serve a row"


def test_a_since_read_on_a_trimmed_session_returns_a_cursor_not_a_409(tmp_path):
    """A trimmed session answers `since` reads exactly instead of refusing.

    The old behaviour raised 409 `cursor_unavailable` for ANY `since` read once
    the frozen-prefix base was non-zero, and withheld `next_since`, so pollers
    on exactly the long-lived sessions worth polling fell back to inexact tail
    reads forever. With a durable-only base the positions are exact again.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("assistant", "the reply", "msg msg-a")
    target._disk_older_count = 100
    target._disk_older_durable_count = 90

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", since=90)

    assert [m["content"] for m in out["messages"]] == ["the reply"]
    assert out["next_since"] == 91
    assert out["total"] == 91


def test_a_cursor_under_the_trimmed_prefix_is_refused_not_skipped_over(tmp_path):
    """A `since` below the durable base cannot be served from memory.

    The rows in ``[since, base)`` exist only on disk; starting the read at the
    window instead would silently skip them, which is the same silent-gap
    failure the past-the-end refusal exists for. Refusing loudly sends the
    caller to a tail read.

    Mutation guard: clamping the offset to 0 returns the window as if it were
    the rows asked for.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("assistant", "newest", "msg msg-a")
    target._disk_older_count = 100
    target._disk_older_durable_count = 90

    with pytest.raises(sc.SessionControlError) as exc:
        sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", since=50)
    assert exc.value.code == "cursor_unavailable"
    assert exc.value.status == 409

    # The tail read is the documented fallback and still works.
    tail = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert [m["content"] for m in tail["messages"]] == ["newest"]


def test_an_untrimmed_session_still_reports_a_gap_free_cursor(tmp_path):
    """With nothing trimmed the cursor is exact, which is the common case.

    An even older version of this test asserted a `trimmed` gap report on a
    session whose rows HAD aged out — that gap count was derived from the mixed
    all-rows counter that made the positions wrong. Positions are now based on
    the durable-only prefix counter, so trimmed sessions get exact cursors too
    (see the trimmed-window tests above) and no gap report exists on any path.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(4):
        target.append("assistant", f"row {i}", "msg msg-a")

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", since=2)

    assert [m["content"] for m in out["messages"]] == ["row 2", "row 3"]
    assert out["next_since"] == 4 == out["total"]
    assert "trimmed" not in out
    assert "cursor_exact" not in out, "an exact cursor needs no caveat"


def test_read_reports_the_targets_queue_depth(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.queue_append("waiting")

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert out["queue_depth"] == 1
