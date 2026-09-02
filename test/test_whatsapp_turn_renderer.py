"""WhatsApp turn-renderer tests: streaming, buffered emit, sentinel, approval."""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.messaging.display_safety import canonicalize_display
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.whatsapp.group_gate import SILENCE_SENTINEL
from kiro_crew.whatsapp.turn_renderer import WhatsAppRenderer

CAPS = TransportCapabilities(max_message_chars=4096, max_buttons=0)

#: Split so this file never holds the contiguous key: an absence assertion
#: against a literal the source already carries proves nothing about the literal a
#: scanner would have to match.
KEY = "AKIA" + "IOSFODNN7EXAMPLE"
#: A credential the driver's byte-level stream scan cannot see, because the bold
#: markers break it. WhatsApp eats those markers, so the reader gets an intact key
#: unless the renderer re-scans the form it actually sends.
SPLIT_KEY = f"AKIA**I**{KEY[5:]}"


def shown(text: str) -> str:
    """*text* as a WhatsApp client renders it, with its markup consumed.

    Asserting on the delivered bytes is the mistake the display screen exists to
    prevent: ``AKIA*I*OSFODNN7EXAMPLE`` passes ``KEY not in sent`` while the
    reader sees the key.
    """
    return canonicalize_display(text)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.images: list[tuple[str, bytes, str]] = []
        #: Flipped by a test to stand in for a transient wire failure.
        self.fail = False
        self._n = 0

    async def send_message(self, jid: str, content: str) -> str:
        if self.fail:
            raise RuntimeError("wa send timeout")
        self.sent.append((jid, content))
        self._n += 1
        # Distinct ids so a test can tell one bubble from another.
        return f"ID{self._n}"

    async def send_image(self, jid: str, data: bytes, caption: str) -> str:
        self.images.append((jid, data, caption))
        return "IMG1"


class FakeClient:
    def __init__(self, edits_ok: bool = True) -> None:
        self.typing: list[bool] = []
        self.edits: list[tuple[str, str]] = []
        self.reactions: list[tuple[str, str]] = []
        self._edits_ok = edits_ok

    async def send_typing(self, jid: str, active: bool) -> None:
        self.typing.append(active)

    async def edit_text(self, jid: str, message_id: str, text: str) -> bool:
        self.edits.append((message_id, text))
        return self._edits_ok

    async def react(self, jid: str, sender: str, message_id: str, emoji: str) -> bool:
        self.reactions.append((message_id, emoji))
        return True


def make(unprompted: bool = False, edits_ok: bool = True):
    transport, client = FakeTransport(), FakeClient(edits_ok=edits_ok)
    r = WhatsAppRenderer(transport, client, "chat@s.whatsapp.net", CAPS, unprompted=unprompted)
    return r, transport, client


async def settle(renderer) -> None:
    """Let the renderer's coalescing flush task run to completion.

    Streaming is deliberately throttled and single-flight, so a test that only
    yields once observes whatever the scheduler happened to do. Awaiting the task
    makes the assertion about the renderer rather than about timing.
    """
    task = getattr(renderer, "_flush_task", None)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
class TestBufferedEmit:
    async def test_nothing_sent_before_on_done(self):
        r, transport, _ = make()
        await r.on_turn_start()
        await r.on_text_chunk("part one ")
        await r.on_text_chunk("part two")
        assert transport.sent == []
        await r.on_done()
        assert len(transport.sent) == 1
        assert transport.sent[0][1] == "part one part two"

    async def test_options_trailer_is_stripped(self):
        r, transport, _ = make()
        await r.on_turn_start()
        await r.on_text_chunk("Pick one.\n[OPTIONS: a | b]")
        await r.on_done()
        assert transport.sent[0][1] == "Pick one."

    async def test_error_turn_sends_apology(self):
        r, transport, _ = make()
        await r.on_turn_start()
        await r.on_done(stop_reason="error")
        assert "went wrong" in transport.sent[0][1]

    async def test_close_finalizes_an_unfinished_turn(self):
        r, transport, _ = make()
        await r.on_turn_start()
        await r.on_text_chunk("answer")
        await r.close()
        assert len(transport.sent) == 1


@pytest.mark.asyncio
class TestSilenceSentinel:
    async def test_sentinel_reply_is_suppressed_entirely(self):
        r, transport, _ = make(unprompted=True)
        await r.on_turn_start()
        await r.on_text_chunk(SILENCE_SENTINEL)
        await r.on_done()
        assert transport.sent == []
        assert r.suppressed is True

    async def test_empty_unprompted_reply_is_suppressed(self):
        r, transport, _ = make(unprompted=True)
        await r.on_turn_start()
        await r.on_done()
        assert transport.sent == [] and r.suppressed

    async def test_real_unprompted_answer_is_delivered(self):
        r, transport, _ = make(unprompted=True)
        await r.on_turn_start()
        await r.on_text_chunk("Actually, the answer is 42.")
        await r.on_done()
        assert len(transport.sent) == 1
        assert not r.suppressed

    async def test_prompted_turn_never_suppresses_sentinel_text(self):
        r, transport, _ = make(unprompted=False)
        await r.on_turn_start()
        await r.on_text_chunk(SILENCE_SENTINEL)
        await r.on_done()
        assert len(transport.sent) == 1


@pytest.mark.asyncio
class TestTyping:
    async def test_typing_stops_by_on_done(self):
        r, _, client = make()
        await r.on_turn_start()
        await r.on_text_chunk("x")
        await r.on_done()
        assert client.typing and client.typing[-1] is False


@pytest.mark.asyncio
class TestStreaming:
    """The reply is shown as it arrives, by editing one bubble.

    The Web protocol exposes an edit, so this channel can stream where iMessage
    and Weixin cannot. What is pinned here is the SHAPE: one bubble opened, then
    edited, with the final send carrying only what streaming had not already
    delivered.
    """

    async def test_a_short_reply_stays_one_message(self):
        """Below the first-flush threshold nothing opens early, so a one-line
        answer arrives as a single message rather than a stub that is then edited.
        """
        r, transport, client = make()
        await r.on_turn_start()
        await r.on_text_chunk("ok")
        await settle(r)
        assert transport.sent == []
        await r.on_done()
        assert [c for _j, c in transport.sent] == ["ok"]
        assert client.edits == []

    async def test_a_long_reply_opens_a_bubble_then_edits_it(self):
        r, transport, client = make()
        await r.on_turn_start()
        await r.on_text_chunk("x" * 60)
        await settle(r)
        assert len(transport.sent) == 1, "the first flush should open one bubble"
        r._last_edit_at = 0.0  # skip the throttle for the second flush
        await r.on_text_chunk("y" * 60)
        await settle(r)
        assert len(transport.sent) == 1, "more text must EDIT, not open a second bubble"
        assert client.edits, "the live bubble was never edited"
        assert client.edits[-1][1] == "x" * 60 + "y" * 60

    async def test_the_final_send_does_not_repeat_streamed_text(self):
        """The reader watched the text arrive; sending the whole body again would
        show every word twice.
        """
        r, transport, client = make()
        await r.on_turn_start()
        await r.on_text_chunk("z" * 60)
        await settle(r)
        await r.on_done()
        bodies = [c for _j, c in transport.sent]
        assert len(bodies) == 1, bodies
        assert bodies[0] == "z" * 60

    async def test_a_refused_edit_seals_and_opens_a_new_bubble(self):
        """A server that refuses the edit must not be retried forever against the
        same message: the text continues in a fresh bubble instead of vanishing.
        """
        r, transport, client = make(edits_ok=False)
        await r.on_turn_start()
        await r.on_text_chunk("a" * 60)
        await settle(r)
        assert len(transport.sent) == 1
        r._last_edit_at = 0.0
        await r.on_text_chunk("b" * 60)
        await settle(r)
        await r.on_done()
        joined = "".join(c for _j, c in transport.sent)
        assert "a" * 60 in joined and "b" * 60 in joined

    async def test_an_unprompted_group_turn_never_streams(self):
        """It may still choose silence, and a streamed prefix cannot be unsent."""
        r, transport, client = make(unprompted=True)
        await r.on_turn_start()
        await r.on_text_chunk("q" * 200)
        await settle(r)
        assert transport.sent == []
        assert client.edits == []

    async def test_a_silent_unprompted_turn_delivers_nothing(self):
        r, transport, _ = make(unprompted=True)
        await r.on_turn_start()
        await r.on_text_chunk(SILENCE_SENTINEL)
        await settle(r)
        await r.on_done()
        assert transport.sent == []
        assert r.suppressed and not r.delivered

    async def test_a_flush_parked_on_its_throttle_does_not_outlive_the_turn(self):
        """on_done cancels the pending flush before finalizing, so a late task
        cannot edit a bubble the turn has already sealed.
        """
        r, transport, _ = make()
        await r.on_turn_start()
        r._last_edit_at = asyncio.get_running_loop().time()  # force a real wait
        await r.on_text_chunk("w" * 60)
        assert r._flush_task is not None and not r._flush_task.done()
        await r.on_done()
        assert r._flush_task is None


@pytest.mark.asyncio
class TestDisplaySafety:
    """The screen has to cover BOTH delivery paths and every text sink.

    ``TurnDriver`` scans the provider stream as literal bytes, so a credential the
    markup split reaches this renderer intact. A screen on one path is a bypass:
    the live bubble and the final send are separate code paths, and the caption,
    the fallback and the approval prompt are three more.
    """

    async def test_a_streamed_frame_never_shows_a_split_credential(self):
        r, transport, client = make()
        await r.on_turn_start()
        await r.on_text_chunk(f"here is the key {SPLIT_KEY} for you")
        await settle(r)
        assert transport.sent, "the frame should have opened a bubble"
        assert KEY not in shown("".join(c for _j, c in transport.sent))

    async def test_the_final_send_never_shows_a_split_credential(self):
        r, transport, _ = make()
        await r.on_turn_start()
        await r.on_text_chunk(f"key: {SPLIT_KEY}")
        await r.on_done()
        assert KEY not in shown("".join(c for _j, c in transport.sent))

    async def test_the_whole_reply_fallback_is_screened(self):
        """A body that yields no chunks skips ``to_whatsapp_text`` entirely.

        An answer that is nothing but an image reference hides to empty, so the
        chunk list comes back empty and the fallback puts the RAW body in the
        chat -- the one form of the reply that met no screen at all.
        """
        r, transport, _ = make()
        await r.on_turn_start()
        await r.on_text_chunk(f"![{SPLIT_KEY}](/tmp/chart.png)")
        await r.on_done()
        assert transport.sent, "the fallback must still say something"
        assert KEY not in shown("".join(c for _j, c in transport.sent))

    async def test_text_sent_outside_the_chunk_path_is_screened(self):
        """``_send`` carries the reply fallback and the file-rejection note, and
        neither passes through ``render_chunks``. WhatsApp still collapses markup
        in both, and both are built from model-authored text.
        """
        r, transport, _ = make()
        await r._send(f"could not send {SPLIT_KEY}")
        assert transport.sent
        assert KEY not in shown(transport.sent[-1][1])

    async def test_an_image_caption_is_screened(self, monkeypatch):
        """The caption is the reference's alt text, so it is model-authored and
        WhatsApp renders markup in it exactly as in a body.
        """
        from kiro_crew.messaging.outbound_files import OutboundFile
        from kiro_crew.whatsapp import turn_renderer as module
        from kiro_crew.whatsapp.files import UploadPlan

        async def fake_plan(text, *, within_root):
            return UploadPlan(
                text="",
                files=[OutboundFile(path="/tmp/c.png", data=b"x", alt=SPLIT_KEY, mime="image/png")],
            )

        monkeypatch.setattr(module, "plan_uploads_off_loop", fake_plan)
        caps = TransportCapabilities(max_message_chars=4096, max_buttons=0, files_outbound=True)
        transport, client = FakeTransport(), FakeClient()
        r = WhatsAppRenderer(
            transport, client, "c@s.whatsapp.net", caps, upload_root=lambda: "/tmp"
        )
        await r.on_turn_start()
        await r.on_text_chunk("here is the chart")
        await r.on_done()
        assert transport.images, "the picture should have been sent"
        assert KEY not in shown(transport.images[-1][2])

    async def test_the_prompt_names_the_tool_the_request_asks_about(self):
        """A permission is not always preceded by its own titled tool call, so a
        remembered name can be the PREVIOUS tool's -- and the operator would be
        consenting to something other than what they read. The title and purpose
        are taken as a pair for the same reason.
        """
        transport, client = FakeTransport(), FakeClient()
        r = WhatsAppRenderer(
            transport, client, "c@s.whatsapp.net", CAPS, approval_session_key="sess"
        )
        await r.on_tool_call("t1", "read_file", tool_purpose="peek at a config")
        await r.on_prompt_choice([], "req-1", tool_title="execute_bash")
        prompt = transport.sent[-1][1]
        assert "execute_bash" in prompt
        assert "read_file" not in prompt
        assert "peek at a config" not in prompt

    async def test_the_approval_prompt_is_screened(self):
        """The tool title is model-authored and reaches the chat with no scan of
        its own -- ``build_approval_prompt`` interpolates it verbatim.
        """
        transport, client = FakeTransport(), FakeClient()
        r = WhatsAppRenderer(
            transport, client, "c@s.whatsapp.net", CAPS, approval_session_key="sess"
        )
        await r.on_tool_call("t1", f"run {SPLIT_KEY}")
        await r.on_prompt_choice([], "req-1")
        assert transport.sent, "the prompt should have been posted"
        assert KEY not in shown(transport.sent[-1][1])


@pytest.mark.asyncio
class TestApprovalPresence:
    """While a permission is pending the agent is waiting on the OPERATOR."""

    async def test_the_indicator_stops_while_an_approval_is_pending(self):
        """``_hold_typing`` is otherwise cancelled only by ``on_done``, so the
        operator watches "composing" for the whole five-minute window while the
        agent is in fact waiting on them.
        """
        transport, client = FakeTransport(), FakeClient()
        r = WhatsAppRenderer(
            transport, client, "c@s.whatsapp.net", CAPS, approval_session_key="sess"
        )
        await r.on_turn_start()
        await r.on_prompt_choice([], "req-1")
        assert client.typing[-1] is False
        assert r._typing_task is None

    async def test_the_indicator_returns_when_the_agent_produces_again(self):
        """No event reports a decision to a renderer -- the driver dispatches the
        prompt and only then awaits the decider -- so the next output event is the
        first news that the wait is over.
        """
        transport, client = FakeTransport(), FakeClient()
        r = WhatsAppRenderer(
            transport, client, "c@s.whatsapp.net", CAPS, approval_session_key="sess"
        )
        await r.on_turn_start()
        await r.on_prompt_choice([], "req-1")
        await r.on_text_chunk("carrying on now")
        assert r._typing_task is not None
        await asyncio.sleep(0)
        assert client.typing[-1] is True
        await r.on_done()


@pytest.mark.asyncio
class TestApprovalTimeoutIsSpoken:
    """Deny-on-silence is otherwise INVISIBLE: the tool is refused, the turn moves
    on, and a live-looking prompt sits in the chat that a later "1" can no longer
    answer.
    """

    async def test_a_timeout_resolves_the_prompt_in_place(self):
        from kiro_crew.messaging.approval import TIMEOUT_NOTICE

        transport, client = FakeTransport(), FakeClient()
        r = WhatsAppRenderer(
            transport, client, "c@s.whatsapp.net", CAPS, approval_session_key="sess"
        )
        await r.on_prompt_choice([], "req-1")
        await r._announce_approval_timeout()
        assert client.edits[-1] == ("ID1", TIMEOUT_NOTICE)

    async def test_a_timeout_posts_the_notice_when_the_edit_is_refused(self):
        from kiro_crew.messaging.approval import TIMEOUT_NOTICE

        transport, client = FakeTransport(), FakeClient(edits_ok=False)
        r = WhatsAppRenderer(
            transport, client, "c@s.whatsapp.net", CAPS, approval_session_key="sess"
        )
        await r.on_prompt_choice([], "req-1")
        await r._announce_approval_timeout()
        assert transport.sent[-1][1] == TIMEOUT_NOTICE

    async def test_the_wait_itself_announces_the_timeout(self):
        """The whole path, not just the callback: the renderer registers the hook
        and the shared wait invokes it when the window closes.
        """
        from kiro_crew.messaging.approval import (
            DENY,
            TIMEOUT_NOTICE,
            claim_approval,
            reset_for_tests,
        )

        reset_for_tests()
        transport, client = FakeTransport(), FakeClient()
        r = WhatsAppRenderer(
            transport, client, "c@s.whatsapp.net", CAPS, approval_session_key="sess"
        )
        await r.on_prompt_choice([], "req-1")
        entry = claim_approval("sess", "req-1")
        assert entry is not None
        assert await entry.wait(0.01) == DENY
        assert client.edits[-1] == ("ID1", TIMEOUT_NOTICE)
        reset_for_tests()


@pytest.mark.asyncio
class TestNothingIsLostOrLooped:
    async def test_a_refused_final_edit_still_delivers_the_tail(self):
        """The last pass over the text has no later flush to recover it.

        A refused edit leaves the bubble showing its PREVIOUS text, so the chunk
        never landed. Advancing the delivered count without opening a fresh bubble
        would drop the end of the reply silently.
        """
        r, transport, client = make()
        await r.on_turn_start()
        await r.on_text_chunk("a" * 60)
        await settle(r)
        assert len(transport.sent) == 1
        client._edits_ok = False  # the server starts refusing
        await r.on_text_chunk("TAIL-MUST-SURVIVE")
        await r.on_done()
        joined = "".join(c for _j, c in transport.sent)
        assert "TAIL-MUST-SURVIVE" in joined

    async def test_text_after_the_edit_window_closes_still_reaches_the_chat(self):
        """Past the 20-minute window the tail MOVES to a fresh bubble; it is not
        final. Counting it sealed made `_sealed_count == len(chunks)`, so every
        later flush returned at the guard and `on_done`'s pending slice came out
        empty: the rest of the reply vanished with no error anywhere.
        """
        from kiro_crew.whatsapp import client as wa_client

        r, transport, client = make()
        await r.on_turn_start()
        await r.on_text_chunk("b" * 60)
        await settle(r)
        assert len(transport.sent) == 1, "the first flush should open one bubble"

        # Age the live bubble past the edit window, deterministically: the
        # predicate reads the loop clock against this stamp, so no sleep is needed.
        r._live_sent_at = asyncio.get_running_loop().time() - wa_client.EDIT_WINDOW_S - 1
        r._last_edit_at = 0.0
        await r.on_text_chunk("AFTER-WINDOW-ONE ")
        await settle(r)
        assert len(transport.sent) == 2, "the window close should open a fresh bubble"

        # Everything from here on is what the bug dropped.
        r._last_edit_at = 0.0
        await r.on_text_chunk("AFTER-WINDOW-TWO")
        await settle(r)
        await r.on_done()

        shown = "".join(c for _j, c in transport.sent) + "".join(t for _m, t in client.edits)
        assert "AFTER-WINDOW-ONE" in shown
        assert "AFTER-WINDOW-TWO" in shown, "text generated after the window was dropped"

    async def test_a_refused_edit_never_seals_a_chunk_that_did_not_land(self):
        """The third instance of one defect class, and the reason ``_show`` exists.

        Sealing edits the live bubble and then counts the chunk final. When the
        server REFUSES that edit, ``_edit_live`` closes the bubble and the chunk's
        text never reached the chat -- yet the count advanced, so no later flush
        and no ``on_done`` pass would ever look at it again. The middle of the
        reply disappears with nothing raised and nothing logged.
        """
        caps = TransportCapabilities(max_message_chars=60, max_buttons=0)
        transport, client = FakeTransport(), FakeClient()
        r = WhatsAppRenderer(transport, client, "c@s.whatsapp.net", caps)
        await r.on_turn_start()
        await r.on_text_chunk("A" * 30)
        await settle(r)
        assert len(transport.sent) == 1, "the first flush should open one bubble"

        client._edits_ok = False  # the server starts refusing
        r._last_edit_at = 0.0
        await r.on_text_chunk("A" * 20 + "\n\n" + "B" * 50)
        await settle(r)
        await r.on_done()

        # Only a SEND lands while edits are refused, so this is what is on screen.
        landed = "".join(content for _jid, content in transport.sent)
        assert "A" * 50 in landed, "the sealed chunk was counted but never delivered"
        assert "B" * 50 in landed

    async def test_a_still_streaming_options_fragment_is_never_shown(self):
        """``_strip_options`` only removes a COMPLETE trailer, so the half-arrived
        marker renders into the live bubble as protocol litter.
        """
        r, transport, _ = make()
        await r.on_turn_start()
        await r.on_text_chunk("x" * 40 + " [OPTIONS: yes | n")
        await settle(r)
        assert transport.sent, "the visible answer should have opened a bubble"
        assert "[OPTIONS" not in transport.sent[0][1]

    async def test_a_completing_options_trailer_cannot_wedge_the_seal_count(self):
        """The fragment also makes the chunk list NON-MONOTONIC, which is worse
        than a flicker: it is permanent loss.

        55 visible characters plus ` [OPTIONS: yes | n` is 73 and splits in two, so
        a flush seals chunk one and sets the count to 1. The completed
        ` [OPTIONS: yes | no]` then strips back to 55 characters and ONE chunk, so
        ``_sealed_count`` is stuck above ``len(chunks)`` forever: every later flush
        returns at the guard and ``on_done``'s pending slice is empty. The litter
        stays on screen and the real answer never arrives.
        """
        caps = TransportCapabilities(max_message_chars=60, max_buttons=0)
        transport, client = FakeTransport(), FakeClient()
        r = WhatsAppRenderer(transport, client, "c@s.whatsapp.net", caps)
        await r.on_turn_start()
        await r.on_text_chunk("x" * 55 + " [OPTIONS: yes | n")
        await settle(r)
        r._last_edit_at = 0.0
        await r.on_text_chunk("o]")
        await settle(r)
        await r.on_done()

        everything = "".join(c for _j, c in transport.sent) + "".join(t for _m, t in client.edits)
        assert "[OPTIONS" not in everything, "protocol litter reached the chat"
        assert "x" * 55 in everything, "the visible answer never arrived"
        assert r._sealed_count <= len(await r._rendered_chunks())

    async def test_a_failed_streaming_send_still_reaches_the_chat(self):
        """A transient send failure must cost a repeat, never the text.

        The count advances past a failure on purpose, so the flush loop cannot spin
        on an unsendable chunk for the rest of the turn. That makes "counted" stop
        meaning "delivered", so the final pass reopens from the earliest chunk that
        never landed rather than from the count.
        """
        r, transport, _client = make()
        await r.on_turn_start()

        transport.fail = True  # every streaming send raises
        await r.on_text_chunk("c" * 5000)
        await settle(r)
        assert transport.sent == [], "nothing should have landed while failing"
        transport.fail = False

        await r.on_done()
        shown = "".join(c for _j, c in transport.sent)
        assert shown.count("c") >= 5000, "a chunk counted but never sent was dropped"

    async def test_an_unsendable_chunk_does_not_spin_the_flush_loop(self):
        """The other half of the same trade: bounded work, not a hung turn."""
        r, transport, _client = make()
        await r.on_turn_start()
        transport.fail = True
        await r.on_text_chunk("d" * 12000)
        await settle(r)
        # Every seal attempt raised, yet the loop terminated and the count moved.
        assert r._sealed_count > 0
        assert r._undelivered_from == 0

    async def test_a_reaction_produces_no_turn(self):
        """A reaction is not a request, and this channel draws its own phase
        reactions: letting one reach the turn path closes the loop
        react -> from_me echo -> note -> turn -> react.
        """
        from kiro_crew.whatsapp.media import KIND_REACTION, MediaDescription, unsupported_note

        note = unsupported_note(MediaDescription(kind=KIND_REACTION))
        assert note == "", "a reaction must yield no note, so it starts no turn"
