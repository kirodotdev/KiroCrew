"""The in-memory prompt ring and the ``prompt-trace`` endpoint that reads it.

Three contracts, each pinned here because a slip is invisible at runtime:

* the ring is BOUNDED per session and across sessions, and eviction never
  removes the session that just wrote (a single oversized prompt must still be
  readable for the turn that sent it);
* a provider records exactly what it hands its transport — the text AFTER the
  receipt substitution ``EssentialDelivery`` performs — and only for a
  persistent session;
* the endpoint is dashboard-only and returns the backend's own block spans, so
  the developer view can never disagree with the size breakdown about a
  boundary.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import prompt_trace, runtime_death
from kiro_crew.context_blocks import USER_LABEL, block_spans, split_blocks
from kiro_crew.dashboard.handlers import telemetry as h
from kiro_crew.essential_delivery import EssentialDelivery

PromptRecordFields = {f.name for f in dataclasses.fields(prompt_trace.PromptRecord)}


@pytest.fixture(autouse=True)
def _fresh_ring():
    prompt_trace._reset_for_tests()
    yield
    prompt_trace._reset_for_tests()


# ── the ring ─────────────────────────────────────────────────────────────────


def test_records_are_returned_oldest_first_and_carry_the_text():
    prompt_trace.record("dashboard:chat-1", "first")
    prompt_trace.record("dashboard:chat-1", "second")
    recs = prompt_trace.snapshot("dashboard:chat-1").records
    assert [r.text for r in recs] == ["first", "second"]
    assert recs[0].chars == 5
    assert recs[0].ts <= recs[1].ts


def test_each_session_keeps_only_its_newest_turns_and_counts_the_rest():
    for i in range(prompt_trace.MAX_TURNS_PER_SESSION + 3):
        prompt_trace.record("dashboard:chat-1", f"turn {i}")
    snap = prompt_trace.snapshot("dashboard:chat-1")
    texts = [r.text for r in snap.records]
    assert len(texts) == prompt_trace.MAX_TURNS_PER_SESSION
    assert texts[0] == "turn 3" and texts[-1] == f"turn {prompt_trace.MAX_TURNS_PER_SESSION + 2}"
    assert snap.dropped == 3 and snap.evicted is False
    assert prompt_trace.snapshot("dashboard:never") == prompt_trace.PromptSnapshot([], 0, False)


def test_a_prompt_longer_than_the_per_turn_cap_is_kept_from_its_start_and_says_so(monkeypatch):
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", 5)
    prompt_trace.record("dashboard:chat-1", "abcde fgh")
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.text == "abcde" and rec.truncated is True and rec.chars == 9
    assert prompt_trace._store.total_chars == 5, "the budget counts what is retained"
    assert rec.to_dict()["truncated"] is True


def test_the_cut_never_ends_inside_a_token(monkeypatch):
    # A credential straddling the cap: a fixed-offset cut would keep a prefix
    # that the read-side scrub cannot match (its patterns carry length floors),
    # and serve it with redacted=False. The cut moves back to the last
    # whitespace, so the token is dropped whole.
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", 40)
    token = "ghp_" + "A" * 36
    text = "some words here, then " + token + " and a tail"
    assert 22 < 40 < 22 + len(token), "the fixture must straddle the cap"
    prompt_trace.record("dashboard:chat-1", text)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.truncated is True
    assert rec.text == "some words here, then", rec.text
    assert "ghp_" not in rec.text and not rec.text[-1:].isspace()
    assert rec.to_dict()["redacted"] is False, "nothing to scrub: the token is gone whole"


def test_a_cut_landing_on_whitespace_keeps_the_whole_cap(monkeypatch):
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", 5)
    prompt_trace.record("dashboard:chat-1", "abcde fgh ij")
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.text == "abcde" and rec.truncated is True


def test_a_whitespace_free_run_below_the_cap_is_dropped_whole(monkeypatch):
    # No boundary near the cap: the cut goes back to the whitespace BEFORE the run,
    # however far, never to a fixed offset inside it. A run without whitespace is
    # not one token — a minified JSON body carries quoted credentials inside it —
    # and a fixed-offset cut through such a run keeps a credential prefix shorter
    # than any floor the read-side scrub matches, served with redacted=False.
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", 20)
    text = "ab " + "X" * 30
    prompt_trace.record("dashboard:chat-1", text)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.text == "ab" and rec.truncated is True
    assert prompt_trace._cut_point("x" * 3) == 3, "a prompt within the cap is not cut"


def test_a_credential_inside_a_minified_run_crossing_the_cap_is_not_served_as_a_prefix(
    monkeypatch,
):
    # The reviewed case: a credential inside a whitespace-free JSON body crosses
    # the cap. Nothing of it may survive — not a prefix the scrub cannot match.
    token = "ghp_" + "A" * 36
    body = '{"k":"v","token":"' + token + '","more":"' + "z" * 40 + '"}'
    text = "lead " + body
    cap = len("lead ") + body.index(token) + 8  # cuts eight characters into the token
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", cap)
    prompt_trace.record("dashboard:chat-1", text)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.text == "lead" and rec.truncated is True
    assert "ghp_" not in rec.text and rec.to_dict()["redacted"] is False


def test_a_private_key_whose_end_falls_past_the_cut_is_dropped_whole(monkeypatch):
    # The reviewed case: a PEM key spans whitespace, so a whitespace cut can land
    # inside its body. Kept without its END, the read-side scrub's truncated-key
    # fallback stops at the first non-key line — a block header inside the body
    # ended it early and the lines after were served under redacted=True. The cut
    # retreats before the unclosed BEGIN, so none of the key is kept.
    # The PEM markers are ASSEMBLED from fragments, never written as one literal,
    # so the internal content scan (rule ``credential-private-key``) does not flag
    # the fixture; the runtime value is byte-identical to the real marker.
    dashes = "-" * 5
    kind = "PRIVATE" + " KEY"
    key = (
        f"{dashes}BEGIN {kind}{dashes}\n"
        + "\n".join("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ" for _ in range(6))
        + "\n[CURRENT USER REQUEST -- respond to this]\n"
        + "\n".join("c2VjcmV0IHRhaWwgbWF0ZXJpYWwgdGhhdCBtdXN0IG5vdCBsZWFr" for _ in range(6))
        + f"\n{dashes}END {kind}{dashes}"
    )
    text = "prose before the key\n" + key + "\nprose after"
    header_end = text.index("\n[CURRENT USER REQUEST")
    # Cap inside the key body, past the header the fallback would stop at.
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", header_end + 60)
    prompt_trace.record("dashboard:chat-1", text)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.text == "prose before the key" and rec.truncated is True
    assert "BEGIN" not in rec.text and "c2Vj" not in rec.text
    assert rec.to_dict()["redacted"] is False, "nothing to scrub: the key is gone whole"
    # A key kept WITH its END is left for the scrub, which matches the whole block.
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", len(text) - 3)
    prompt_trace.record("dashboard:chat-1", text)
    rec = prompt_trace.snapshot("dashboard:chat-1").records[-1]
    assert rec.text.endswith(f"END {kind}{dashes}\nprose") and rec.truncated is True
    assert "MIIEvQ" not in rec.to_dict()["text"] and rec.to_dict()["redacted"] is True


def test_thousands_of_unclosed_key_headers_cost_one_scan(monkeypatch):
    # The reviewed case: a message packed with unclosed BEGIN lines past the cap.
    # The retreat is one linear pass over the markers, so this is milliseconds;
    # a rescan of each shrinking prefix took a minute and tripped the gateway's
    # stall watchdog (pytest-timeout is the guard here, not a timer assertion).
    dashes = "-" * 5
    kind = "PRIVATE" + " KEY"
    begin = f"{dashes}BEGIN {kind}{dashes}\n"
    text = "lead " + begin * 16_000 + "x" * 10_000
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", len(text) - 100)
    assert prompt_trace._cut_point(text) == len("lead")
    # Closed pairs before the cut are kept whole; only the open one is dropped.
    pair = begin + "AAAA\n" + f"{dashes}END {kind}{dashes}\n"
    text = "lead " + pair * 3 + begin + "BBBB\n" + "x" * 100
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", len(text) - 10)
    cut = prompt_trace._cut_point(text)
    assert text[:cut] == ("lead " + pair * 3).rstrip("\n")


def test_a_text_with_no_whitespace_at_all_keeps_nothing(monkeypatch):
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", 3)
    prompt_trace.record("dashboard:chat-1", "abcdefgh")
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.text == "" and rec.truncated is True and rec.chars == 8


def test_empty_key_or_text_records_nothing():
    prompt_trace.record("", "orphan pooled worker")
    prompt_trace.record("dashboard:chat-1", "")
    assert prompt_trace.snapshot("").records == []
    assert prompt_trace.snapshot("dashboard:chat-1").records == []


def test_only_sessions_a_context_tab_can_show_are_recorded():
    """The endpoint resolves dashboard and channel keys; a cron or subagent prompt has no reader."""
    for key in ("cron:job-1", "subagent:abc", "hook:x", "channel:app:agent", "bare-no-namespace"):
        prompt_trace.record(key, "unreadable")
        assert prompt_trace.snapshot(key).records == [], key
        assert not prompt_trace.readable_session_key(key)
    assert prompt_trace._store.total_chars == 0, "an unreadable prompt spends none of the budget"
    for key in ("dashboard:chat-9", "slack:1785370133.085469", "discord:guild:chan"):
        assert prompt_trace.readable_session_key(key)
        prompt_trace.record(key, "readable")
        assert len(prompt_trace.snapshot(key).records) == 1, key


def test_the_global_budget_evicts_the_least_recently_written_session(monkeypatch):
    monkeypatch.setattr(prompt_trace, "MAX_TOTAL_CHARS", 100)
    prompt_trace.record("dashboard:old", "a" * 60)
    prompt_trace.record("dashboard:new", "b" * 60)
    assert (
        prompt_trace.snapshot("dashboard:old").records == []
    ), "the older session should have been evicted"
    assert prompt_trace.snapshot("dashboard:old").evicted is True, "an eviction is said, not silent"
    assert len(prompt_trace.snapshot("dashboard:new").records) == 1
    # A new turn on the evicted session starts afresh and clears the mark.
    prompt_trace.record("dashboard:old", "c")
    assert prompt_trace.snapshot("dashboard:old").evicted is False
    # A prompt larger than the whole budget is still readable for its own session.
    prompt_trace.record("dashboard:huge", "c" * 500)
    assert len(prompt_trace.snapshot("dashboard:huge").records) == 1
    assert prompt_trace._store.total_chars == 500


def test_the_budget_holds_within_a_single_session_too(monkeypatch):
    """One session's ring alone must not hold more than the budget: its oldest go."""
    monkeypatch.setattr(prompt_trace, "MAX_TOTAL_CHARS", 100)
    for i in range(5):
        prompt_trace.record("dashboard:only", str(i) * 40)
    snap = prompt_trace.snapshot("dashboard:only")
    assert [r.text[0] for r in snap.records] == ["3", "4"], "80 chars fit, a third 40 would not"
    assert snap.dropped == 3, "records the budget shed are counted like a cap push-out"
    assert prompt_trace._store.total_chars == 80 <= prompt_trace.MAX_TOTAL_CHARS
    # The newest prompt alone may exceed the budget; it is never shed.
    prompt_trace.record("dashboard:only", "z" * 150)
    snap = prompt_trace.snapshot("dashboard:only")
    assert [r.text[0] for r in snap.records] == ["z"] and snap.dropped == 5


def test_forget_drops_a_session_and_its_bytes():
    prompt_trace.record("dashboard:chat-1", "x" * 10)
    prompt_trace.record("dashboard:chat-2", "y" * 5)
    prompt_trace.forget("dashboard:chat-1")
    assert prompt_trace.snapshot("dashboard:chat-1").records == []
    assert prompt_trace._store.total_chars == 5


def test_the_session_count_is_bounded_by_the_same_constant_as_the_evicted_table(monkeypatch):
    """A channel session with no tab never calls forget(); the count must still not grow forever."""
    monkeypatch.setattr(prompt_trace, "MAX_SESSION_KEYS", 3)
    for i in range(5):
        prompt_trace.record(f"slack:{i}", "tiny")
    assert list(prompt_trace._store.rings) == ["slack:2", "slack:3", "slack:4"]
    assert prompt_trace.snapshot("slack:0").evicted is True
    assert len(prompt_trace._store.evicted) <= prompt_trace.MAX_SESSION_KEYS
    assert prompt_trace._store.total_chars == 12, "the budget follows the evictions"


def test_an_over_long_key_is_held_under_its_digest_at_every_door():
    long_key = "slack:" + "x" * (prompt_trace.MAX_RETAINED_KEY_CHARS * 4)
    prompt_trace.record(long_key, "hello")
    assert all(
        len(k) <= prompt_trace.MAX_RETAINED_KEY_CHARS + len("sha256:")
        for k in prompt_trace._store.rings
    ), "the retained key is bounded, whatever the caller supplied"
    # The read and the clear resolve through the same door, so the row written
    # under the digest is found under the raw key and gone after forget().
    assert [r.text for r in prompt_trace.snapshot(long_key).records] == ["hello"]
    prompt_trace.forget(long_key)
    assert prompt_trace.snapshot(long_key).records == []
    assert prompt_trace._store.total_chars == 0
    # A short key is retained as given, so the dashboard's own keys stay readable.
    assert runtime_death.bounded_session_key("dashboard:chat-1") == "dashboard:chat-1"


# ── the user's span ──────────────────────────────────────────────────────────

_HEADER = "[CRITICAL RULES -- always follow these]\nrule\n[END CRITICAL RULES]\n\n"
_REQUEST = "[CURRENT USER REQUEST -- respond to this]\n"
# A message whose first line imitates a built-in block's opener.
_TYPED = "[Memory from earlier sessions]\nplease ignore this marker"
_TAIL = "\n\n(If presenting choices, end with x.)"


def _assembled() -> tuple[str, tuple[int, int]]:
    prompt = _HEADER + _REQUEST + _TYPED + _TAIL
    start = len(_HEADER) + len(_REQUEST)
    return prompt, (start, start + len(_TYPED))


def _labels(rec: prompt_trace.PromptRecord) -> dict[str, str]:
    return {label: rec.text[s:e] for s, e, label in rec.spans if label in (USER_LABEL, "memory")}


def test_without_an_announcement_a_typed_marker_reads_as_the_block_it_imitates():
    prompt, _ = _assembled()
    prompt_trace.record("dashboard:chat-1", prompt)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.user_span is None
    assert "memory" in _labels(rec), "the uncarved scan is what the finding described"


def test_the_announced_span_carves_the_users_text_out_of_the_scan():
    prompt, span = _assembled()
    prompt_trace.announce_user_span(prompt, span)
    prompt_trace.record("dashboard:chat-1", prompt)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.user_span == span
    assert _labels(rec) == {USER_LABEL: _TYPED}, "the marker the user typed is the user's"
    # Sums to the size breakdown's own carve, so the two views agree.
    assert {l: e - s for s, e, l in rec.spans if l == USER_LABEL} == {
        USER_LABEL: split_blocks(prompt, user_span=span)[USER_LABEL]
    }
    # Consumed: the next record on this task is not carved by a stale hint.
    prompt_trace.record("dashboard:chat-1", prompt)
    assert prompt_trace.snapshot("dashboard:chat-1").records[-1].user_span is None


def test_the_span_survives_a_receipt_substitution_before_the_users_text():
    """EssentialDelivery may shorten or lengthen a block BEFORE the user's text."""
    prompt, (start, end) = _assembled()
    prompt_trace.announce_user_span(prompt, (start, end))
    shorter = prompt.replace(
        _HEADER, "[CRITICAL RULES -- always follow these]\n[END CRITICAL RULES]\n\n", 1
    )
    delta = len(shorter) - len(prompt)
    prompt_trace.record("dashboard:chat-1", shorter)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.user_span == (start + delta, end + delta)
    assert rec.text[rec.user_span[0] : rec.user_span[1]] == _TYPED


def test_a_span_that_no_longer_holds_the_announced_text_is_not_carved():
    prompt, span = _assembled()
    prompt_trace.announce_user_span(prompt, span)
    prompt_trace.record("dashboard:chat-1", "an entirely different prompt of another shape")
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.user_span is None, "position alone never carves; the text must match"
    # A None announcement clears an earlier one.
    prompt_trace.announce_user_span(prompt, span)
    prompt_trace.announce_user_span(prompt, None)
    prompt_trace.record("dashboard:chat-1", prompt)
    assert prompt_trace.snapshot("dashboard:chat-1").records[-1].user_span is None
    # Out-of-range bounds are refused at the announcement.
    prompt_trace.announce_user_span(prompt, (5, len(prompt) + 1))
    prompt_trace.record("dashboard:chat-1", prompt)
    assert prompt_trace.snapshot("dashboard:chat-1").records[-1].user_span is None


def test_the_announcement_retains_a_bounded_probe_of_the_users_text():
    """The hint lives on the task OUTSIDE the ring's character budget, so a verbatim
    copy of a large message there would sit outside every stated bound. It holds the
    length plus a bounded head and tail instead (no hash of the user's message), and
    a candidate slice must match all three."""
    import dataclasses

    head = "x" * 40
    user = "a message the size of a small novel " * 20_000
    prompt = head + user + " tail"
    prompt_trace.announce_user_span(prompt, (len(head), len(head) + len(user)))
    hint = prompt_trace._announced_user_span.get()
    assert hint is not None
    for field in dataclasses.fields(hint):
        value = getattr(hint, field.name)
        if isinstance(value, str):
            assert len(value) <= prompt_trace.USER_PROBE_CHARS, f"{field.name} is unbounded"
    assert hint.probe_len == len(user)
    assert hint.probe_head == user[: prompt_trace.USER_PROBE_CHARS]
    assert hint.probe_tail == user[-prompt_trace.USER_PROBE_CHARS :]
    # A slice of the right length but other content is still refused — in the
    # head, in the tail, or in both.
    forged = head + ("b" * len(user)) + " tail"
    assert prompt_trace._locate_user_span(hint, forged) is None
    tail_only = head + user[:-1] + "!" + " tail"
    assert prompt_trace._locate_user_span(hint, tail_only) is None
    # A short message is held whole as the head, with an empty tail.
    prompt_trace.announce_user_span("abc hello", (4, 9))
    short = prompt_trace._announced_user_span.get()
    assert short is not None and (short.probe_head, short.probe_tail) == ("hello", "")
    prompt_trace.announce_user_span(prompt, (len(head), len(head) + len(user)))
    hint = prompt_trace._announced_user_span.get()
    # The genuine text is re-found, unmoved and moved by a prefix delta.
    assert prompt_trace._locate_user_span(hint, prompt) == (len(head), len(head) + len(user))
    moved = "pp" + prompt
    assert prompt_trace._locate_user_span(hint, moved) == (len(head) + 2, len(head) + 2 + len(user))
    prompt_trace.announce_user_span(prompt, None)


def test_the_span_is_clamped_to_the_kept_text(monkeypatch):
    prompt, (start, end) = _assembled()
    # Pin the cut at the cap (a boundary search would move it), so the clamp is
    # what is exercised.
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", start + 5)
    monkeypatch.setattr(prompt_trace, "_cut_point", lambda text: start + 5)
    prompt_trace.announce_user_span(prompt, (start, end))
    prompt_trace.record("dashboard:chat-1", prompt)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.truncated and rec.user_span == (start, start + 5)
    assert rec.spans[-1][1] == len(rec.text), "spans still cover exactly the kept text"
    # The user's text entirely past the cut carves nothing.
    monkeypatch.setattr(prompt_trace, "MAX_CHARS_PER_TURN", start)
    monkeypatch.setattr(prompt_trace, "_cut_point", lambda text: start)
    prompt_trace.announce_user_span(prompt, (start, end))
    prompt_trace.record("dashboard:chat-1", prompt)
    assert prompt_trace.snapshot("dashboard:chat-1").records[-1].user_span is None


def test_the_dashboard_runner_announces_the_span_it_sized_with():
    """Pinned so the announcement cannot go dead: the runner is the one caller."""
    import inspect

    from kiro_crew.dashboard import chat_runner

    src = inspect.getsource(chat_runner)
    assert "prompt_trace.announce_user_span(full_message, _span_arg, turn=_crew_log_turn_no)" in src
    # The same number stamps the usage row, so the tab joins the two by id.
    assert "crew_log_emit.on_context_composed(" in src and "_crew_log_turn_no," in src
    assert src.index("prompt_trace.announce_user_span(") < src.index("client.stream(full_message)")


def test_the_announced_turn_rides_on_the_record_and_is_consumed_once():
    prompt, span = _assembled()
    prompt_trace.announce_user_span(prompt, span, turn=7)
    prompt_trace.record("dashboard:chat-1", prompt)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.turn == 7 and rec.to_dict()["turn"] == 7
    # Consumed with the span: the next record on this task carries no turn.
    prompt_trace.record("dashboard:chat-1", prompt)
    assert prompt_trace.snapshot("dashboard:chat-1").records[-1].turn is None
    # A span the assembler could not vouch for still carries the turn.
    prompt_trace.announce_user_span(prompt, None, turn=8)
    prompt_trace.record("dashboard:chat-1", prompt)
    rec = prompt_trace.snapshot("dashboard:chat-1").records[-1]
    assert rec.turn == 8 and rec.user_span is None


# ── what a provider records ──────────────────────────────────────────────────


class _Event:
    def __init__(self, kind: str, **fields: Any) -> None:
        self.kind = kind
        self.text = fields.get("text", "")
        self.control_notice = False
        self.stop_reason = fields.get("stop_reason", "")
        self.synthetic_completion = False
        self.refusal = False


@pytest.mark.asyncio
async def test_essential_delivery_reports_the_text_it_actually_sends():
    """Both hooks see the FINAL message — the same string ``send`` receives —
    and ``on_accepted`` fires when the transport says the write returned, before
    the first event."""
    sent: list[str] = []
    accepted: list[tuple[str, int]] = []
    order: list[str] = []

    async def send(message: str, *, on_sent):
        sent.append(message)
        order.append("write")
        on_sent()
        order.append("first event")
        yield _Event("text", text="ok")

    delivery = EssentialDelivery()
    async for _ in delivery.stream(
        "hello",
        send,
        lambda: ("inc",),
        on_accepted=lambda text, n: (accepted.append((text, n)), order.append("accepted")),
    ):
        pass
    assert [t for t, _ in accepted] == sent == ["hello"]
    assert accepted[0][1] == len("hello"), "nothing substituted: assembled == sent"
    assert order == ["write", "accepted", "first event"]


@pytest.mark.asyncio
async def test_a_prompt_the_transport_refused_is_never_recorded_as_sent():
    """A record made before the write would have nothing to retract it."""
    accepted: list[str] = []

    async def refuse(message: str, *, on_sent):
        raise ConnectionResetError("pipe closed")
        yield  # pragma: no cover - makes this an async generator

    delivery = EssentialDelivery()
    with pytest.raises(ConnectionResetError):
        async for _ in delivery.stream(
            "hello", refuse, lambda: ("inc",), on_accepted=lambda t, n: accepted.append(t)
        ):
            pass
    assert accepted == []

    # A stream that completes without a single event still had its write taken.
    async def silent(message: str, *, on_sent):
        return
        yield  # pragma: no cover

    async for _ in delivery.stream(
        "quiet", silent, lambda: ("inc",), on_accepted=lambda t, n: accepted.append(t)
    ):
        pass
    assert accepted == ["quiet"]


@pytest.mark.asyncio
async def test_a_prompt_written_before_the_backend_died_is_still_recorded():
    """The write returned, then the backend exited before its first event: the
    prompt was sent, and the record must say so. Acceptance therefore comes from
    the transport's ``on_sent`` at the write, not from the first event."""
    accepted: list[str] = []

    async def write_then_die(message: str, *, on_sent):
        on_sent()  # the transport's write returned
        raise ConnectionResetError("backend exited before its first event")
        yield  # pragma: no cover

    delivery = EssentialDelivery()
    with pytest.raises(ConnectionResetError):
        async for _ in delivery.stream(
            "sent", write_then_die, lambda: ("inc",), on_accepted=lambda t, n: accepted.append(t)
        ):
            pass
    assert accepted == ["sent"]

    # Both transports report the write at the right point: after the request
    # write returned, before any event is awaited.
    import inspect

    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.acp.session_provider import AcpSessionProvider

    for fn in (AcpClient.stream_events, AcpSessionHandle.prompt, AcpSessionProvider.stream_events):
        assert "on_sent" in inspect.signature(fn).parameters, fn.__qualname__
    client_src = inspect.getsource(AcpClient.stream_events)
    assert client_src.index("await self._send_prompt(message)") < client_src.index("on_sent()")
    assert client_src.index("on_sent()") < client_src.index("self._dispatch_events(")
    handle_src = inspect.getsource(AcpSessionHandle._run_turn)
    assert handle_src.index("send_request(_method, _params)") < handle_src.index("on_sent()")


@pytest.mark.asyncio
async def test_essential_delivery_reports_the_assembled_length_when_the_envelope_is_swapped():
    """The receipt substitution is the one step between the sized prompt and the wire.

    On a member session the essentials envelope is replaced by its native form
    (or dropped once acknowledged), so the text ``send`` receives is shorter than
    the text the assembler sized. ``on_accepted`` is told both, so the record can say
    which is which instead of the view flagging every such turn as a wrong match.
    """
    seen: list[tuple[str, int]] = []

    async def send(message: str, *, on_sent):
        yield _Event("text", text="ok")

    delivery = EssentialDelivery()
    envelope = "[ESSENTIALS]\nlong long long receipt body\n[END ESSENTIALS]\n"
    delivery.bind(envelope, scope=("s",), incarnation=("inc",), native_envelope="<e/>\n")
    assembled = (
        "[CRITICAL RULES -- always follow these]\nr\n[END CRITICAL RULES]\n\n" + envelope + "hi"
    )
    async for _ in delivery.stream(
        assembled, send, lambda: ("inc",), on_accepted=lambda text, n: seen.append((text, n))
    ):
        pass
    ((text, n),) = seen
    assert "<e/>" in text and envelope not in text
    assert n == len(assembled) and len(text) < n

    # What the ring keeps: the sent text, and the assembled length beside it.
    prompt_trace.record("dashboard:chat-1", text, assembled_chars=n)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert rec.chars == len(text) and rec.assembled_chars == len(assembled)
    assert rec.to_dict()["assembled_chars"] == len(assembled)
    # Omitted, the assembled length is the text's own.
    prompt_trace.record("dashboard:chat-2", "plain")
    assert prompt_trace.snapshot("dashboard:chat-2").records[0].assembled_chars == 5


def test_the_outer_provider_records_once_per_turn_on_the_shared_runtime():
    """AcpProvider wraps an AcpSessionProvider whose stream() already records."""
    from unittest.mock import MagicMock

    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider

    inner = AcpSessionProvider.__new__(AcpSessionProvider)
    inner._session_key = "dashboard:chat-1"
    inner._handle = SimpleNamespace(memory_mode="persistent")
    outer = AcpProvider.__new__(AcpProvider)
    outer.memory_mode = "persistent"
    outer._client = inner
    AcpProvider._trace_outbound_prompt(outer, "hello", 905)
    assert prompt_trace.snapshot("dashboard:chat-1").records == []
    # ...but the outer delivery is the one that substituted the receipt, so the
    # inner record must carry the OUTER's arrival length, not len(sent text):
    # otherwise assembled == sent on every shared-runtime turn and the panel
    # flags every member session's acknowledged turn as a wrong match. The outer
    # announces the message length BEFORE its delivery substitutes anything, the
    # inner records once the transport accepted — so the announcement is there
    # when the record is made.
    AcpProvider._announce_outbound_prompt(outer, "x" * 905)
    AcpSessionProvider._trace_outbound_prompt(inner, "hello", len("hello"))
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert (rec.chars, rec.assembled_chars) == (5, 905)
    # Consumed: the next inner record on this task measures for itself.
    AcpSessionProvider._trace_outbound_prompt(inner, "again", 5)
    assert prompt_trace.snapshot("dashboard:chat-1").records[-1].assembled_chars == 5

    # The direct-client backend has no inner recorder, so the outer one records
    # (and announces nothing: there is no nested delivery to hand the length to).
    direct = MagicMock()
    direct._session_key = "dashboard:chat-2"
    outer._client = direct
    AcpProvider._announce_outbound_prompt(outer, "hello")
    assert prompt_trace._announced_assembled_chars.get() is None
    AcpProvider._trace_outbound_prompt(outer, "hello", 9)
    (rec,) = prompt_trace.snapshot("dashboard:chat-2").records
    assert (rec.text, rec.assembled_chars) == ("hello", 9)


# ── the endpoint ─────────────────────────────────────────────────────────────


def _mk(query: str = "", *, app: str = "", state: Any = None) -> web.Request:
    app_obj = web.Application()
    if state is not None:
        app_obj["state"] = state
    request = make_mocked_request("GET", "/api/telemetry/prompt-trace" + query, app=app_obj)
    if app:
        request["app"] = app
    return request


def _body(response: web.StreamResponse) -> Any:
    assert isinstance(response, web.Response)
    return json.loads(response.body or b"{}")


@pytest.mark.asyncio
async def test_prompt_trace_400_without_a_slot():
    response = await h.api_prompt_trace(_mk("?slot=%20"))
    assert response.status == 400
    assert _body(response)["code"] == "slot_required"


@pytest.mark.asyncio
async def test_prompt_trace_refuses_an_app_caller_indistinguishably(monkeypatch):
    audited: list[dict[str, Any]] = []
    monkeypatch.setattr(
        h._sel_mod,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: audited.append(kw)),
    )
    prompt_trace.record("dashboard:chat-1", "secret memory text")
    response = await h.api_prompt_trace(_mk("?slot=chat-1", app="some-app"))
    assert response.status == 404
    assert _body(response) == {"error": "not found", "code": "not_found"}
    assert audited and audited[0]["operation"] == "prompt_trace"
    assert audited[0]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_prompt_trace_returns_text_and_the_backends_own_spans():
    text = (
        "[CRITICAL RULES -- always follow these]\nrule\n[END CRITICAL RULES]\n\n"
        "[CURRENT USER REQUEST -- respond to this]\nhello there\n\n(If presenting choices, end with x.)"
    )
    # Recorded under the SESSION key the turn ran on; the tab asks by slot name.
    prompt_trace.record("dashboard:chat-1", text)
    response = await h.api_prompt_trace(_mk("?slot=chat-1"))
    payload = _body(response)
    assert payload["slot"] == "chat-1"
    assert payload["dropped"] == 0 and payload["evicted"] is False
    (turn,) = payload["turns"]
    assert turn["text"] == text and turn["chars"] == len(text) and turn["truncated"] is False
    spans = [(s["start"], s["end"], s["label"]) for s in turn["spans"]]
    assert spans == prompt_trace._coalesce(block_spans(text))
    assert response.content_type == "application/json"
    # Cached only while the cache costs no more than the text: this 158-char
    # record's three spans do not qualify (cheap to re-scan), so it is served
    # fresh each read and holds nothing beside its text.
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert len(rec.spans) * prompt_trace.SPAN_COST_CHARS > len(rec.text)
    assert "spans" not in rec.__dict__ and rec.spans == spans
    # Contiguous and covering: the view can colour every character exactly once.
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))


@pytest.mark.asyncio
async def test_prompt_trace_body_is_utf8_not_ascii_escaped():
    """The ring is bounded in CHARACTERS; ``json.dumps``' default ``ensure_ascii``
    would turn each non-ASCII code point into 6-12 ASCII bytes, so a ring of
    ordinary non-English prompts near its budget would serialize to a body many
    times its own size (with a second full copy live during the encode). The body
    is UTF-8: at most 4 bytes per code point, declared by the response charset."""
    text = "[CURRENT USER REQUEST -- respond to this]\n" + "你好世界 — 🙂 " * 400
    prompt_trace.record("dashboard:chat-1", text)
    response = await h.api_prompt_trace(_mk("?slot=chat-1"))
    assert isinstance(response, web.Response) and response.body is not None
    body = bytes(response.body)
    assert b"\\u" not in body, "non-ASCII must not be escaped"
    assert len(body) < len(text) * 4 + 4096
    assert response.charset == "utf-8"
    assert _body(response)["turns"][0]["text"] == text


@pytest.mark.asyncio
async def test_prompt_trace_serves_a_prompt_carrying_a_lone_surrogate():
    """A chat body is admitted unvalidated, so a message can carry a lone surrogate;
    the ring stores it verbatim, and the wire write survived it because the ACP
    client escapes to ASCII. The endpoint's own UTF-8 encode must not be the one
    place that raises — that would 500 every poll until the turn is evicted. The
    lone surrogate goes out as the JSON escape, so the body parses and the text
    round-trips; everything else stays UTF-8."""
    text = "[CURRENT USER REQUEST -- respond to this]\nhi \ud800 你好"
    prompt_trace.record("dashboard:chat-1", text)
    response = await h.api_prompt_trace(_mk("?slot=chat-1"))
    assert isinstance(response, web.Response) and response.status == 200
    body = bytes(response.body or b"")
    assert "你好".encode("utf-8") in body, "ordinary non-ASCII stays UTF-8"
    assert b"\\ud800" in body, "the one unencodable code point goes out as its JSON escape"
    assert _body(response)["turns"][0]["text"] == text


@pytest.mark.asyncio
async def test_prompt_trace_serializes_the_body_off_the_event_loop(monkeypatch):
    """The worker hands back the serialized BODY, not a dict the loop would dump."""
    handed_back: list[Any] = []
    real_to_thread = h.asyncio.to_thread

    async def spy(fn, *args, **kwargs):
        result = await real_to_thread(fn, *args, **kwargs)
        handed_back.append(result)
        return result

    monkeypatch.setattr(h.asyncio, "to_thread", spy)
    prompt_trace.record("dashboard:chat-1", "prompt text " * 100)
    response = await h.api_prompt_trace(_mk("?slot=chat-1"))
    # Serialized AND encoded on the worker: the loop only hands bytes to aiohttp.
    assert handed_back and isinstance(handed_back[-1], bytes)
    assert json.loads(handed_back[-1])["turns"][0]["text"] == "prompt text " * 100
    assert response.body == handed_back[-1]


@pytest.mark.asyncio
async def test_prompt_trace_serves_a_scrubbed_text_with_spans_that_still_line_up():
    """The ring is verbatim; the READ is the egress boundary.

    A memory record holding a token reaches the prompt unscrubbed, so the
    endpoint must run the credential + exfiltration-URL chain before the text
    reaches a browser — over the whole text, with the block spans realigned to
    the scrubbed result instead of pointing into the wrong block.
    """
    secret = "AKIAIOSFODNN7EXAMPLE"  # the canonical AWS access-key example
    text = (
        "[CRITICAL RULES -- always follow these]\nrule\n[END CRITICAL RULES]\n\n"
        f"[Memory from earlier sessions]\nkey: {secret}\n[End of memory]\n\n"
        "[CURRENT USER REQUEST -- respond to this]\nhello\n\n(If presenting choices, end with x.)"
    )
    prompt_trace.record("dashboard:chat-1", text)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    assert secret in rec.text, "the ring itself keeps the prompt as sent"
    payload = _body(await h.api_prompt_trace(_mk("?slot=chat-1")))
    (turn,) = payload["turns"]
    assert secret not in turn["text"] and secret not in json.dumps(payload)
    assert turn["redacted"] is True
    assert turn["chars"] == len(text), "sizes still describe the prompt as sent"
    spans = [(sp["start"], sp["end"], sp["label"]) for sp in turn["spans"]]
    # Contiguous, covering the SERVED text, and each span still names its block.
    assert spans[0][0] == 0 and spans[-1][1] == len(turn["text"])
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    assert [label for _, _, label in spans] == [label for _, _, label in rec.spans]
    memory = next(turn["text"][s:e] for s, e, label in spans if label == "memory")
    assert "key:" in memory and secret not in memory
    # Nothing to scrub: the presented text IS the text and the row says so.
    prompt_trace.record("dashboard:chat-2", "plain prompt")
    (plain,) = _body(await h.api_prompt_trace(_mk("?slot=chat-2")))["turns"]
    assert plain["text"] == "plain prompt" and plain["redacted"] is False


def test_a_secret_crossing_a_block_boundary_is_scrubbed_as_the_whole_chain_would():
    """The chain runs on the WHOLE text, so a key that spans two blocks does not leak its tail.

    The credential chain's private-key rule masks from a BEGIN marker through the
    matching END marker, across anything between. Scrubbed block by block, the
    block holding the END (and the key body before it) has no BEGIN of its own
    and is served verbatim — key material past the boundary reaching the
    browser. Scrubbed whole, the chain masks the key as it does on every other
    egress, and the spans describe exactly the text that is served.
    """
    # The PEM markers are assembled at runtime: the content scan reads a literal
    # one in an added line as a key, and this fixture is a shape, not a key.
    begin, end = ("-----BEGIN " + "PRIVATE KEY-----", "-----END " + "PRIVATE KEY-----")
    head_body, tail_body = "QUJDREVGR0hJSktMTU5P", "UVJTVFVWV1hZWjAxMjM0NTY3ODlhYmNkZWZn"
    text = (
        "[CRITICAL RULES -- always follow these]\nrule\n[END CRITICAL RULES]\n\n"
        f"[Memory from earlier sessions]\n{begin}\n{head_body}\n"
        f"[Learned corrections -- retained rules]\n{tail_body}\n"
        f"{end}\n[End of learned corrections]\n\n"
        "[CURRENT USER REQUEST -- respond to this]\nhello\n\n(If presenting choices, end with x.)"
    )
    prompt_trace.record("dashboard:chat-1", text)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    from kiro_crew.security import redact_credentials

    whole, _ = redact_credentials(text)
    served, spans, redacted = rec.presented
    assert redacted is True
    assert served == whole, "exactly what the shared chain yields on the whole text"
    assert tail_body not in served, "the tail past the boundary is gone"
    assert head_body not in served
    # Spans are contiguous over the SERVED text and read as its block structure:
    # the blocks the key did not touch keep their spans, the tag sits in the
    # block the key began in, and the header the key swallowed is not a block.
    assert spans[0][0] == 0 and spans[-1][1] == len(served)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    labels = [label for _, _, label in spans]
    assert labels[0] == rec.spans[0][2] and labels[-1] == rec.spans[-1][2]
    assert "lessons" not in labels, "the swallowed header no longer opens a block"
    tagged = next(label for s_, e_, label in spans if "[REDACTED: credential]" in served[s_:e_])
    assert tagged == "memory"


def test_the_users_carve_survives_a_redaction_elsewhere_in_the_prompt():
    """Rebuilding spans over the scrubbed text keeps the user's carve when nothing crossed it."""
    user = "[Memory from earlier sessions]\nlooks like a block but is mine"
    head = (
        "[Memory from earlier sessions]\nkey: AKIAIOSFODNN7EXAMPLE\n[End of memory]\n\n"
        "[CURRENT USER REQUEST -- respond to this]\n"
    )
    text = head + user + "\n\n(If presenting choices, end with x.)"
    prompt_trace.announce_user_span(text, (len(head), len(head) + len(user)))
    prompt_trace.record("dashboard:chat-1", text)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    served, spans, redacted = rec.presented
    assert redacted is True and "AKIA" not in served
    user_spans = [(s_, e_) for s_, e_, label in spans if label == "your_message"]
    assert len(user_spans) == 1, spans
    ((s_, e_),) = user_spans
    assert served[s_:e_] == user, "the user's marker-lookalike is still credited to the user"
    assert [label for _, _, label in spans].count("memory") == 1


def test_the_scrubbed_presentation_is_never_a_retained_copy():
    """The budget counts ``text``; a cached scrubbed string would sit outside it."""
    prompt_trace.record("dashboard:chat-1", "plain prompt with nothing to hide")
    prompt_trace.record("dashboard:chat-2", "key: AKIAIOSFODNN7EXAMPLE and more")
    plain, secret = (
        prompt_trace.snapshot(k).records[0] for k in ("dashboard:chat-1", "dashboard:chat-2")
    )
    text, _, redacted = plain.presented
    assert (
        text is plain.text and redacted is False
    ), "the ordinary record is served as the string held"
    text, _, redacted = secret.presented
    assert redacted is True and "AKIA" not in text
    # Only the verdict (and the spans) are cached on the record — no second text.
    for rec in (plain, secret):
        rec.to_dict()
        cached = {k: v for k, v in rec.__dict__.items() if k not in PromptRecordFields}
        assert set(cached) <= {"spans", "_needs_redaction"}, cached.keys()


def test_adjacent_same_label_spans_are_coalesced_at_the_source():
    # A block body and the blank line after its closer, or a run of repeated
    # one-line markers, are one span each: the view merges such runs anyway and
    # every retained tuple is memory.
    text = "[RUNTIME] a\n" * 50 + "[CURRENT USER REQUEST -- respond to this]\nhi"
    prompt_trace.record("dashboard:chat-1", text)
    (rec,) = prompt_trace.snapshot("dashboard:chat-1").records
    raw = block_spans(text)
    assert len(raw) >= 50 and len(rec.spans) < len(raw)
    assert all(a[2] != b[2] or a[1] != b[0] for a, b in zip(rec.spans, rec.spans[1:]))
    assert rec.spans[0][0] == 0 and rec.spans[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(rec.spans, rec.spans[1:]))
    assert "".join(text[s:e] for s, e, _ in rec.spans) == text


def test_a_span_dense_record_is_re_scanned_rather_than_cached():
    """The cache never costs more than the text it describes.

    The ring's budget counts text; a nested span list that could outgrow it
    would sit outside every stated bound. Alternating one-line markers make a
    span every ~14 characters, far past SPAN_COST_CHARS per span, so that record
    serves correct spans on every read and caches none of them.
    """
    dense = "[RUNTIME] a\n[CURRENT DATE] b\n" * 400
    ordinary = (
        "[CRITICAL RULES -- always follow these]\n" + "rule " * 200 + "\n[END CRITICAL RULES]\n\nhi"
    )
    prompt_trace.record("dashboard:chat-1", dense)
    prompt_trace.record("dashboard:chat-2", ordinary)
    d, o = (prompt_trace.snapshot(k).records[0] for k in ("dashboard:chat-1", "dashboard:chat-2"))
    spans = d.spans
    assert len(spans) * prompt_trace.SPAN_COST_CHARS > len(d.text)
    assert "spans" not in d.__dict__, "too dense to cache"
    assert d.spans == spans and d.spans is not spans, "re-scanned, still correct"
    assert spans[0][0] == 0 and spans[-1][1] == len(dense)
    assert len(o.spans) * prompt_trace.SPAN_COST_CHARS <= len(o.text)
    assert "spans" in o.__dict__ and o.spans is o.spans, "the ordinary record is cached"
    # The endpoint's presentation and redaction path work either way.
    assert d.to_dict()["spans"][-1]["end"] == len(dense)


def test_no_read_scrubs_the_same_record_twice(monkeypatch):
    """The first read's pass is both the verdict and the presentation.

    A verdict taken by a separate pass would make the first read of a record
    that needs masking run the chain twice over up to MAX_CHARS_PER_TURN.
    """
    prompt_trace.record("dashboard:chat-1", "plain prompt with nothing to hide")
    prompt_trace.record("dashboard:chat-2", "key: AKIAIOSFODNN7EXAMPLE and more")
    plain, secret = (
        prompt_trace.snapshot(k).records[0] for k in ("dashboard:chat-1", "dashboard:chat-2")
    )
    calls: list[str] = []
    real = prompt_trace.PromptRecord._scrub

    def counted(self):
        calls.append(self.text[:5])
        return real(self)

    monkeypatch.setattr(prompt_trace.PromptRecord, "_scrub", counted)
    plain.presented
    secret.presented
    assert calls == ["plain", "key: "], "one pass each on first read"
    plain.presented
    assert calls == ["plain", "key: "], "the ordinary record is not scanned again"
    _, _, redacted = secret.presented
    assert redacted is True and calls == ["plain", "key: ", "key: "], "one pass per read, never two"


@pytest.mark.asyncio
async def test_prompt_trace_is_empty_for_a_session_that_recorded_nothing():
    payload = _body(await h.api_prompt_trace(_mk("?slot=never")))
    assert payload["turns"] == []


@pytest.mark.asyncio
async def test_prompt_trace_follows_a_linked_slot_to_its_channel_session():
    """A channel-born tab's turns run on the channel's own session key."""
    prompt_trace.record("slack:1785370133.085469", "channel prompt")
    prompt_trace.record("dashboard:slack_1785370133.085469", "phantom")
    slot = SimpleNamespace(
        key="slack_1785370133.085469", linked_session_key="slack:1785370133.085469"
    )
    state = SimpleNamespace(get_slot=lambda key: slot if key == slot.key else None)
    payload = _body(await h.api_prompt_trace(_mk("?slot=slack_1785370133.085469", state=state)))
    assert [t["text"] for t in payload["turns"]] == ["channel prompt"]


def test_forget_is_what_closing_a_tab_calls():
    """The close and sweep paths clear the ring on EVERY exit past the pop; pinned
    so no exit can go quiet."""
    import ast

    from kiro_crew.dashboard import chat_handlers

    src = open(chat_handlers.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    fns = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def forgets(node):
        return [
            sub
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call)
            and getattr(sub.func, "id", "") == "_forget_prompt_trace_if_unshared"
        ]

    def calls(node, name):
        return [
            sub
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and getattr(sub.func, "id", "") == name
        ]

    # The single-tab close: the teardown past the pop lives in `_finish_close`,
    # which returns early on the hand-over and raises on a failed persist, and
    # `_close_slot` holds ONE `finally` over that call — so every one of those
    # exits forgets, and none of them can be missed by a call placed before it.
    close = fns["_close_slot"]
    (close_try,) = [
        node
        for node in ast.walk(close)
        if isinstance(node, ast.Try) and any(forgets(stmt) for stmt in node.finalbody)
    ]
    assert len(forgets(close_try)) == 1 and forgets(close_try) == forgets(close)
    assert calls(close_try, "_finish_close"), "the finally must wrap the post-pop teardown"
    assert all(
        sub in [x for stmt in close_try.finalbody for x in ast.walk(stmt)] for sub in forgets(close)
    ), "the close path's forget must be in the finally"
    finish = fns["_finish_close"]
    assert not forgets(finish), "one exit, in the caller's finally — not scattered in the body"
    assert calls(finish, "_slot_still_ours"), "the ownership re-check stays in the teardown"
    assert any(isinstance(n, ast.Return) for n in ast.walk(finish)), "the hand-over return"

    # The sweep: a `finally` around the archive's session teardown (with the
    # ownership re-check INSIDE the try, never as an `if` around it), plus one
    # forget directly before each `continue` that exits past the pop above it.
    sweep = fns["api_chat_slots_cleanup"]
    sweep_forgets = forgets(sweep)
    assert len(sweep_forgets) == 3
    (sweep_try,) = [
        node
        for node in ast.walk(sweep)
        if isinstance(node, ast.Try) and any(forgets(stmt) for stmt in node.finalbody)
    ]
    assert (
        len([x for stmt in sweep_try.finalbody for x in ast.walk(stmt) if x in sweep_forgets]) == 1
    )
    assert calls(sweep_try, "_slot_still_ours"), "the ownership re-check must be inside the try"
    before_continue = 0
    for node in ast.walk(sweep):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for stmt, nxt in zip(body, body[1:]):
            if (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and stmt.value in sweep_forgets
                and isinstance(nxt, ast.Continue)
            ):
                before_continue += 1
    assert before_continue == 2, "the hand-over and failed arms each forget before their continue"
    assert len(forgets(tree)) == 4


def test_a_replacement_linked_elsewhere_does_not_keep_the_originals_prompt_text():
    """Recreate during the save, linked to a channel: the original's dashboard key is forgotten."""
    from kiro_crew.dashboard.chat_handlers import _forget_prompt_trace_if_unshared

    prompt_trace.record("dashboard:chat-1", "original prompt")
    replacement = SimpleNamespace(key="chat-1", linked_session_key="slack:9")
    state = SimpleNamespace(_slots={"chat-1": replacement})
    _forget_prompt_trace_if_unshared(state, "dashboard:chat-1")  # type: ignore[arg-type]
    assert prompt_trace.snapshot("dashboard:chat-1").records == []


def test_closing_one_alias_keeps_the_surviving_aliass_prompt_text():
    """Two live tabs on one session key: the first close must not empty the ring."""
    from kiro_crew.dashboard.chat_handlers import _forget_prompt_trace_if_unshared

    prompt_trace.record("dashboard:chat-1", "shared prompt")
    survivor = SimpleNamespace(key="chat-9", linked_session_key="dashboard:chat-1")
    state = SimpleNamespace(_slots={"chat-9": survivor})
    _forget_prompt_trace_if_unshared(state, "dashboard:chat-1")  # type: ignore[arg-type]
    assert [r.text for r in prompt_trace.snapshot("dashboard:chat-1").records] == ["shared prompt"]
    state._slots.clear()
    _forget_prompt_trace_if_unshared(state, "dashboard:chat-1")  # type: ignore[arg-type]
    assert prompt_trace.snapshot("dashboard:chat-1").records == []


def test_closing_a_tab_never_forgets_a_channel_key():
    """A channel-born slot's key is the channel's; the conversation outlives the tab."""
    from kiro_crew.dashboard.chat_handlers import _forget_prompt_trace_if_unshared

    prompt_trace.record("slack:1", "channel prompt")
    state = SimpleNamespace(_slots={})
    _forget_prompt_trace_if_unshared(state, "slack:1")  # type: ignore[arg-type]
    assert [r.text for r in prompt_trace.snapshot("slack:1").records] == ["channel prompt"]


# ── block_spans vs split_blocks ──────────────────────────────────────────────

_PROMPT = (
    "[CRITICAL RULES -- always follow these]\nrules here\n[END CRITICAL RULES]\n\n"
    "[Learned corrections -- retained rules]\n- one\n- two\n[End of learned corrections]\n\n"
    "stray text nobody marked\n"
    "[PROJECT] Active project directory: /w\n\n"
    "[CURRENT USER REQUEST -- respond to this]\nwhat is this\n\n(If presenting choices, end with x.)"
)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"user_chars": len("what is this")},
        {"user_span": (_PROMPT.index("what is this"), _PROMPT.index("what is this") + 12)},
    ],
)
def test_block_spans_sum_to_split_blocks_and_cover_the_prompt(kwargs):
    spans = block_spans(_PROMPT, **kwargs)
    assert spans[0][0] == 0 and spans[-1][1] == len(_PROMPT)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    sums: dict[str, int] = {}
    for start, end, label in spans:
        sums[label] = sums.get(label, 0) + (end - start)
    assert sums == split_blocks(_PROMPT, **kwargs)


def test_block_spans_place_the_users_text_where_it_sits():
    start = _PROMPT.index("what is this")
    spans = block_spans(_PROMPT, user_span=(start, start + 12))
    user = [s for s in spans if s[2] == USER_LABEL]
    assert user == [(start, start + 12, USER_LABEL)]
    assert _PROMPT[start : start + 12] == "what is this"


def test_block_spans_of_an_empty_prompt_are_empty():
    assert block_spans("") == []
