"""Slack threads are RECORDED as anchored threads, not re-keyed.

The thing under test is one claim: the session Slack already opens for a
``thread_ts`` gains a durable anchor naming the message it hangs off, in the same
shape the dashboard writes, and nothing else about Slack's key discipline moves.

So the pins come in two halves. The first is the anchor itself -- written once,
idempotent, in the neutral shape. The second is every case that must NOT be
anchored, because that is where a "record it" change quietly becomes a "re-key it"
change: a 1:1 DM folded onto its channel key, and a message routed into a
dashboard-linked session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiro_crew.history import ConversationLog
from kiro_crew.messaging.link import ThreadAnchor, canonical_key
from kiro_crew.slack import threads as slack_threads

CHANNEL = "C01ABCDEF"
ROOT_TS = "1785370133.085469"
THREAD_KEY = f"slack:{ROOT_TS}"


@pytest.fixture()
def log(tmp_path: Path) -> ConversationLog:
    return ConversationLog(tmp_path / "sessions")


def _seed(log: ConversationLog, key: str) -> None:
    """A session with a transcript, which is what a live Slack thread has."""
    log.append(key, "user", "hello")


# ── The anchor ────────────────────────────────────────────────────────────────


def test_a_slack_thread_session_is_recorded_as_anchored_to_its_root_message(
    log: ConversationLog,
) -> None:
    _seed(log, THREAD_KEY)
    anchor = slack_threads.record_anchor(
        conversation_log=log, session_key=THREAD_KEY, channel=CHANNEL, reply_ts=ROOT_TS
    )
    assert anchor == ThreadAnchor("slack", CHANNEL, ROOT_TS)
    stored = log.get_metadata(THREAD_KEY).get(slack_threads.THREAD_ANCHOR_META)
    # The SAME shape the dashboard writes, read back by the same reader: that is
    # the whole point of one neutral type rather than a Slack-shaped record.
    assert ThreadAnchor.from_dict(stored) == anchor


def test_recording_twice_leaves_the_first_anchor_and_writes_no_second_one(
    log: ConversationLog,
) -> None:
    # "Is this session new" is the dispatch's answer, not the recorder's: a process
    # restart makes a live session new again while its metadata is still on disk.
    _seed(log, THREAD_KEY)
    first = slack_threads.record_anchor(
        conversation_log=log, session_key=THREAD_KEY, channel=CHANNEL, reply_ts=ROOT_TS
    )
    assert first is not None
    again = slack_threads.record_anchor(
        conversation_log=log, session_key=THREAD_KEY, channel=CHANNEL, reply_ts=ROOT_TS
    )
    assert again is None
    assert ThreadAnchor.from_dict(
        log.get_metadata(THREAD_KEY).get(slack_threads.THREAD_ANCHOR_META)
    ) == ThreadAnchor("slack", CHANNEL, ROOT_TS)


def test_a_store_that_raises_leaves_the_turn_alone(tmp_path: Path) -> None:
    class Exploding:
        def get_metadata(self, key: str) -> dict:
            raise OSError("no")

        def update_metadata(self, key: str, changes: dict) -> None:  # pragma: no cover
            raise AssertionError("must not be reached")

    # An anchor is a record ABOUT a turn. A turn that answered the user must not
    # fail because its bookkeeping did.
    assert (
        slack_threads.record_anchor(
            conversation_log=Exploding(),
            session_key=THREAD_KEY,
            channel=CHANNEL,
            reply_ts=ROOT_TS,
        )
        is None
    )


# ── What must NOT be anchored ─────────────────────────────────────────────────


def test_a_folded_one_to_one_dm_is_not_anchored(log: ConversationLog) -> None:
    # `slack.dm_single_session` keys the session by the CHANNEL, so a reply there
    # is a layout habit rather than a new topic and there is no thread to anchor.
    flat_key = "slack:D0PRIVATE"
    _seed(log, flat_key)
    assert (
        slack_threads.record_anchor(
            conversation_log=log, session_key=flat_key, channel="D0PRIVATE", reply_ts=ROOT_TS
        )
        is None
    )
    assert slack_threads.THREAD_ANCHOR_META not in log.get_metadata(flat_key)


def test_a_message_routed_into_a_dashboard_linked_session_is_not_anchored(
    log: ConversationLog,
) -> None:
    # The dashboard slot's conversation is not this thread's, and the mirror
    # binding is deliberately separate from the anchor -- guardrail G3.
    linked = "dashboard:chat-7-1785370000"
    _seed(log, linked)
    assert (
        slack_threads.record_anchor(
            conversation_log=log, session_key=linked, channel=CHANNEL, reply_ts=ROOT_TS
        )
        is None
    )
    assert slack_threads.THREAD_ANCHOR_META not in log.get_metadata(linked)


def test_the_thread_test_reads_the_key_the_dispatch_used(log: ConversationLog) -> None:
    # Reading the key rather than the flag is what keeps the two exclusions above
    # from drifting apart: either one changes the key, and this sees it.
    assert slack_threads.is_thread_session(THREAD_KEY, ROOT_TS) is True
    assert slack_threads.is_thread_session("slack:C01ABCDEF", ROOT_TS) is False
    assert slack_threads.is_thread_session("", ROOT_TS) is False
    assert slack_threads.is_thread_session(THREAD_KEY, "") is False


# ── The adapter seam ──────────────────────────────────────────────────────────


# ── The round trip, through the real dispatch ─────────────────────────────────
#
# Everything above tests `record_anchor` directly. These two drive the REAL Slack
# inbound path -- `SlackTransport.receive` -> `slack/handler.py::handle_message`,
# the same harness `test_slack_transport_integration` uses -- so the pin is that
# the dispatch actually reaches the recorder at the point it claims to, and that
# the answer goes back into the thread the anchor names. Only the outermost
# boundary is substituted: the Slack HTTP client and the model provider.


def _roundtrip(monkeypatch, tmp_path: Path, *, thread_ts: str | None, msg_ts: str):
    """Drive one inbound Slack message through the real handler. Returns
    ``(session_key, recording slack client, conversation log)``."""
    import importlib
    import sys
    from unittest.mock import AsyncMock

    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
    from kiro_crew.messaging.transport import InboundMessage
    from kiro_crew.slack import handler as slack_handler
    from kiro_crew.slack.transport import SlackTransport

    test_dir = Path(__file__).parent
    if str(test_dir) not in sys.path:  # pragma: no cover
        sys.path.insert(0, str(test_dir))
    golden = importlib.import_module("test_slack_golden_transcript")

    monkeypatch.setattr(slack_handler, "_dashboard_state", None, raising=False)
    monkeypatch.setattr(slack_handler, "is_owner", lambda uid: True)
    monkeypatch.setattr(slack_handler, "is_allowed_user", lambda uid: True)
    monkeypatch.setattr(slack_handler, "_get_default_agent", lambda: "")
    monkeypatch.setattr(
        slack_handler, "_hydrate_thread_overrides", AsyncMock(return_value=None), raising=False
    )
    monkeypatch.setattr(slack_handler, "_hydrate_conv_flags", lambda *a, **k: None, raising=False)

    log = ConversationLog(tmp_path / "sessions")
    slack = golden.RecordingSlackClient()
    provider = golden.ScriptedProvider(
        [
            golden.make_event(EVENT_TEXT_CHUNK, text="on it"),
            golden.make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )

    class NewSessions(golden.FakeSessions):
        """The harness' session manager, answering ``is_new`` on the first open.

        That is the branch the anchor is recorded in, and the stock stand-in
        always says False -- so a test built on it unchanged would pass while the
        recorder was never called.
        """

        def __init__(self, prov: object) -> None:
            super().__init__(prov)
            self.opened: list[str] = []

        async def get_or_create(self, session_key, agent=None, channel_id=None):
            first = session_key not in self.opened
            self.opened.append(session_key)
            return self._provider, first, False

    sessions = NewSessions(provider)
    seen: dict[str, str] = {}

    async def dispatch(msg: InboundMessage) -> None:
        # The transcript the anchor hangs off has to exist, as it does in
        # production: the session's first turn writes it. Written here because the
        # harness' session manager runs no persistence of its own.
        key = canonical_key(msg.thread_id or msg_ts)
        log.append(key, "user", msg.text)
        seen["key"] = key
        await slack_handler.handle_message(
            slack=slack,
            sessions=sessions,
            channel=msg.conversation_id,
            text=msg.text,
            thread_ts=msg.thread_id,
            msg_ts=msg_ts,
            user_id="U_OWNER",
            conversation_log=log,
            context_builder=None,
        )

    transport = SlackTransport(slack, allowed_users={"U_OWNER"}, dispatch=dispatch)
    event: dict[str, object] = {
        "user": "U_OWNER",
        "channel": CHANNEL,
        "text": "look at the HBM supply question",
        "ts": msg_ts,
    }
    if thread_ts:
        event["thread_ts"] = thread_ts
    asyncio.run(transport.receive({"event": event}))
    return seen.get("key", ""), slack, log


def test_a_message_in_a_slack_thread_is_anchored_and_answered_in_that_thread(
    monkeypatch, tmp_path: Path
) -> None:
    key, slack, log = _roundtrip(
        monkeypatch, tmp_path, thread_ts=ROOT_TS, msg_ts="1785370200.111111"
    )
    # Reached the session Slack has always keyed for this thread -- not a new one.
    assert key == THREAD_KEY
    # The turn ran.
    methods = [m for m, _ in slack.transcript]
    assert "start_stream" in methods and "stop_stream" in methods, methods
    # And the anchor is on that session, naming the thread's root message.
    assert ThreadAnchor.from_dict(
        log.get_metadata(THREAD_KEY).get(slack_threads.THREAD_ANCHOR_META)
    ) == ThreadAnchor("slack", CHANNEL, ROOT_TS)
    # The answer went back INTO the thread: every outbound call that carries a
    # thread ts carries the root's, which is what makes this a round trip rather
    # than a message that merely arrived. Asserted as the exact set, not as
    # "nothing else" -- an empty set would mean the reply went to the channel.
    thread_targets = {
        args.get("thread_ts")
        for _, args in slack.transcript
        if isinstance(args, dict) and args.get("thread_ts")
    }
    assert thread_targets == {ROOT_TS}, thread_targets


def test_a_top_level_message_is_anchored_to_itself(monkeypatch, tmp_path: Path) -> None:
    # A top-level message keys on its OWN ts and becomes its own thread-session.
    # That is Slack's rule, not something this change introduces, so the anchor
    # follows it: the root message is the message itself.
    own_ts = "1785370300.222222"
    key, _slack, log = _roundtrip(monkeypatch, tmp_path, thread_ts=None, msg_ts=own_ts)
    assert key == canonical_key(own_ts)
    assert ThreadAnchor.from_dict(
        log.get_metadata(key).get(slack_threads.THREAD_ANCHOR_META)
    ) == ThreadAnchor("slack", CHANNEL, own_ts)
