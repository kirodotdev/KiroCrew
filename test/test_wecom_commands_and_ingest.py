"""WeCom command surface and attachment ingest, end to end through the dispatcher.

Covers the paths a user actually walks that the other WeCom suites do not: the
command intercepts (`/help`, `/stop`, the `/steer` and `/queue` prefixes) and the
media-ingest branches, including every way ingest can decline without losing the
message. These are the branches where a mistake is silent — the user sends
something and simply never hears back — so each one asserts what the user is TOLD,
not only what the code returned.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.messaging.attachments import Attachment, IngestResult
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments
from kiro_crew.wecom.client import WeComInbound
from kiro_crew.wecom.commands import (
    COMMAND_SPEC,
    build_help_text,
    build_override_usage,
    is_bare_mid_turn_override,
    parse_command,
    parse_mid_turn_override,
)
from kiro_crew.wecom.transport_dispatch import WeComDispatcher


class FakeClient:
    #: The ingest opens its own aiohttp session, so it has to be TOLD the proxy;
    #: the real client exposes it for exactly that reason.
    proxy: str | None = None

    def __init__(self) -> None:
        self.said: list[str] = []
        self.frames: list[dict] = []

    async def say(self, inbound: Any, content: str) -> bool:
        self.said.append(content)
        return True

    async def send_stream(self, req_id, sid, content, *, finish) -> bool:
        self.frames.append({"sid": sid, "content": content, "finish": finish})
        return True

    async def send_reply(self, url, content) -> None:
        self.said.append(content)

    def stream_is_dead(self, stream_id) -> bool:
        return False


class FakeProvider:
    def __init__(self, *, cancel_raises: bool = False, no_cancel: bool = False) -> None:
        self.cancelled = False
        self._raises = cancel_raises
        if no_cancel:
            # A provider with no cancel at all, e.g. a cold-start stub.
            del self.cancel

    async def cancel(self, *, wait_ack_timeout: float = 0) -> None:
        if self._raises:
            raise RuntimeError("no")
        self.cancelled = True


class FakeSessions:
    def __init__(self, provider: Any = None, *, busy: bool = False) -> None:
        self._provider = provider
        self._busy = busy
        self.opted_out: dict = {}
        self.mirror_links: dict = {}
        self.origin_links: dict = {}

    def is_busy(self, key) -> bool:
        return self._busy

    def get_provider(self, key) -> Any:
        return self._provider

    def has_session(self, key) -> bool:
        return self._provider is not None

    def max_generation(self, bucket: str) -> int:
        return -1

    # -- mirror surface (the dispatcher binds its origin mirror every turn) --
    @contextlib.contextmanager
    def batched_save(self):
        yield

    def mirror_opt_out(self, key) -> bool:
        return bool(self.opted_out.get(key))

    def set_mirror_opt_out(self, key, value) -> None:
        self.opted_out[key] = value

    def get_mirror_link(self, key):
        return self.mirror_links.get(key)

    def set_mirror_link(self, key, link, *, reason="") -> None:
        self.mirror_links[key] = link

    def clear_mirror_link(self, key, *, reason="") -> bool:
        return self.mirror_links.pop(key, None) is not None

    def clear_mirror_links_at(self, location, *, reason="") -> list:
        return []

    def set_origin_link(self, key, link) -> None:
        self.origin_links[key] = link

    def is_mirror_paused(self, key, *, origin=False) -> bool:
        return False

    # -- turn slot (/compact takes it so it cannot race a running turn) --
    async def try_acquire(self, key) -> bool:
        return not self._busy

    def release(self, key) -> None:
        pass


def _cfg():
    return SimpleNamespace(
        agent=SimpleNamespace(default_agent="", approval_mode="interactive"),
        wecom=SimpleNamespace(hard_threshold_pct=95.0, soft_threshold_pct=80.0),
        messaging=SimpleNamespace(idle_reset_minutes=0, daily_reset_hour=-1, dm_scope="per_user"),
    )


def _dispatcher(sessions: Any, client: FakeClient) -> WeComDispatcher:
    d = WeComDispatcher(
        sessions=sessions,
        ctx_builder=SimpleNamespace(hooks=None),
        cfg=_cfg(),
        owner_id="Wei",
    )
    d.client = client  # type: ignore[assignment]
    return d


def _inbound(text: str = "hi", **kw: Any) -> WeComInbound:
    base = dict(userid="Wei", text=text, response_url="https://r", req_id="rq1")
    base.update(kw)
    return WeComInbound(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _permit_inbound(monkeypatch):
    """The governance gate is not what these tests are about."""

    async def _permit(_channel: str) -> bool:
        return True

    monkeypatch.setattr("kiro_crew.wecom.transport_dispatch.inbound_permitted", _permit)


# ---------------------------------------------------------------------------
# The command catalogue
# ---------------------------------------------------------------------------


class TestCommandCatalogue:
    @pytest.mark.asyncio
    async def test_every_advertised_command_is_actually_intercepted(self, monkeypatch) -> None:
        """Each `/help` row must be answered in-channel, never handed to the model.

        Asserted through the DISPATCHER rather than through `parse_command`,
        because interception is the property that matters and not every command
        goes through the exact-alias table -- a command taking an ARGUMENT would
        need its own parser. Pinning one parser would call a command "intercepted"
        while the dispatcher still forwarded it to the LLM, which reads to the user
        exactly like the command not existing.
        """
        drove: list = []

        async def fake_drive_turn(turn, *, sessions, ctx_builder):
            drove.append(turn.user_text)

        monkeypatch.setattr("kiro_crew.wecom.transport_dispatch.drive_turn", fake_drive_turn)

        for name, _desc in COMMAND_SPEC:
            client = FakeClient()
            d = _dispatcher(FakeSessions(), client)
            await d.handle_message(_inbound(f"/{name}"))
            assert client.said, f"/{name} is advertised but answered nothing"
            assert not drove, f"/{name} is advertised but was forwarded to the model"

    def test_the_help_card_lists_every_spec_row(self) -> None:
        card = build_help_text()
        for name, desc in COMMAND_SPEC:
            assert f"/{name}" in card
            assert desc in card

    def test_the_help_card_does_not_promise_queueing(self) -> None:
        # WeCom cannot hold a message: a reply is addressed by the inbound req_id.
        # The card used to advertise "answered after the current turn", which the
        # busy dispatcher then refuses.
        card = build_help_text()
        assert "/queue" in card, "the prefix is still worth documenting"
        assert "暂不支持排队" in card, "it must say queueing is unsupported"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("/new", "new"),
            ("新对话", "new"),
            ("清空", "new"),
            ("/compact", "compact"),
            ("压缩", "compact"),
            ("/stop", "stop"),
            ("/cancel", "stop"),
            ("停止", "stop"),
            ("/help", "help"),
            ("帮助", "help"),
            ("/?", "help"),
            ("/link", "link"),
            ("/unlink", "unlink"),
            ("  /NEW  ", "new"),
            ("@Kiro /stop", "stop"),
            ("hello", None),
            ("/stop please", None),
            ("@a @b /new", None),
            ("@Kiro please /new", None),
        ],
    )
    def test_parse_command(self, text: str, expected: str | None) -> None:
        assert parse_command(text) == expected

    @pytest.mark.parametrize(
        "text,mode,rest",
        [
            ("/steer 换成中文", "steer", "换成中文"),
            ("/queue 等会儿说", "queue", "等会儿说"),
            ("@Kiro /steer 换成中文", "steer", "换成中文"),
            ("/steer", None, "/steer"),
            ("hello", None, "hello"),
        ],
    )
    def test_parse_mid_turn_override(self, text: str, mode: str | None, rest: str) -> None:
        assert parse_mid_turn_override(text) == (mode, rest)

    def test_the_override_payload_is_content_never_a_command(self) -> None:
        # /queue /new must queue the literal text, not schedule a reset.
        assert parse_mid_turn_override("/queue /new") == ("queue", "/new")

    @pytest.mark.parametrize("text", ["/steer", "/queue", "  /STEER ", "@Kiro /queue"])
    def test_a_bare_override_is_recognized(self, text: str) -> None:
        assert is_bare_mid_turn_override(text) is True

    @pytest.mark.parametrize("text", ["/steer hi", "hello", "/new"])
    def test_a_complete_or_unrelated_message_is_not_a_bare_override(self, text: str) -> None:
        assert is_bare_mid_turn_override(text) is False


class TestCommandDispatch:
    @pytest.mark.asyncio
    async def test_help_answers_with_the_card(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(), client)
        await d.handle_message(_inbound("/help"))
        assert client.said and "/compact" in client.said[0]

    @pytest.mark.asyncio
    async def test_a_bare_override_answers_with_its_usage(self) -> None:
        # Handing the literal "/queue" to the model gets it ANSWERED as chat text,
        # which reads to the user exactly like the feature not existing.
        client = FakeClient()
        d = _dispatcher(FakeSessions(), client)
        await d.handle_message(_inbound("/steer"))
        assert client.said == [build_override_usage()]


class TestLinkCommandsStayOffTheLoop:
    """``/link`` and ``/unlink`` must not persist the session map on the loop.

    Neither handler opens ``batched_save`` itself, but both reach code that does:
    ``SessionManager.set_mirror_opt_out`` batches two flag writes internally, and
    ``release_conversation_location`` batches three clears so the location is freed
    atomically. A batch block's exit writes the WHOLE map inline on the thread
    leaving the block -- so on the loop it is a synchronous disk write that stalls
    every gateway turn and the WS heartbeat with it, on a map that grows with every
    session ever created.

    The other mutations these handlers make (``set_mirror_link``,
    ``clear_mirror_link``, ``set_origin_link``) reach ``SessionMap._save``
    unbatched, whose loop-aware branch marks the map dirty and schedules ONE
    debounced flush that writes in a worker thread. Those are correct as-is, which
    is why this pins the two that are not, and why it pins them by THREAD: what
    makes the write safe is not being on the loop, and asserting on the mechanism
    keeps the guarantee legible when the session layer's internals move.
    """

    @staticmethod
    def _recording_sessions() -> Any:
        class Recording(FakeSessions):
            def __init__(self) -> None:
                super().__init__()
                self.opt_out_threads: list[int] = []

            def set_mirror_opt_out(self, key, value) -> None:
                self.opt_out_threads.append(threading.get_ident())
                super().set_mirror_opt_out(key, value)

        return Recording()

    @pytest.mark.asyncio
    async def test_link_offloads_the_batching_write(self) -> None:
        sessions = self._recording_sessions()
        d = _dispatcher(sessions, FakeClient())

        await d.handle_message(_inbound("/link"))

        assert sessions.opt_out_threads, "the opt-out was never cleared"
        assert threading.get_ident() not in sessions.opt_out_threads, (
            "set_mirror_opt_out ran on the event-loop thread, so its internal "
            "batched_save wrote the whole session map inline on the loop"
        )

    @pytest.mark.asyncio
    async def test_unlink_offloads_both_batching_calls(self, monkeypatch) -> None:
        sessions = self._recording_sessions()
        release_threads: list[int] = []

        def fake_release(sessions_arg, *, key, location, channel):
            release_threads.append(threading.get_ident())
            return "✅ Unlinked.", []

        monkeypatch.setattr(
            "kiro_crew.wecom.transport_dispatch.release_conversation_location", fake_release
        )
        d = _dispatcher(sessions, FakeClient())

        await d.handle_message(_inbound("/unlink"))

        loop_thread = threading.get_ident()
        assert sessions.opt_out_threads and loop_thread not in sessions.opt_out_threads
        assert release_threads and loop_thread not in release_threads, (
            "release_conversation_location batches three clears; on the loop that "
            "is an inline whole-map write"
        )

    @pytest.mark.asyncio
    async def test_the_refusal_is_still_persisted_before_the_release(self) -> None:
        # The offload must not reorder these: mirroring is re-asserted on every
        # inbound turn, so a release that lands before the opt-out is undone by the
        # next message. Awaiting each to_thread in turn is what preserves it.
        order: list[str] = []

        class Ordered(FakeSessions):
            def set_mirror_opt_out(self, key, value) -> None:
                order.append("opt_out")
                super().set_mirror_opt_out(key, value)

            def clear_mirror_link(self, key, *, reason="") -> bool:
                order.append("clear")
                return super().clear_mirror_link(key, reason=reason)

        d = _dispatcher(Ordered(), FakeClient())
        await d.handle_message(_inbound("/unlink"))

        assert order[0] == "opt_out", f"the release outran the refusal: {order}"


class TestCleanupCoversEveryAwaitAfterIngest:
    """Decrypted attachments must not survive a cancellation anywhere after ingest.

    The dispatcher deletes the plaintext in a ``finally``, so every await that
    happens AFTER ingest has to sit inside that block. The origin bind is one: it
    offloads to a thread, so a gateway shutdown can cancel there — and outside the
    `try` that cancellation skips the `finally` and leaves the user's decrypted
    media on disk with nothing to report it.
    """

    @pytest.mark.asyncio
    async def test_a_cancelled_origin_bind_still_deletes_the_plaintext(
        self, monkeypatch, tmp_path
    ) -> None:
        plaintext = tmp_path / "decrypted.png"
        plaintext.write_bytes(b"cleartext image")

        async def fake_process(pairs, **kw):
            return IngestResult(image_paths=[str(plaintext)])

        monkeypatch.setattr(
            "kiro_crew.wecom.transport_dispatch.process_wecom_attachments", fake_process
        )

        # SYNC: the real one is called through asyncio.to_thread, so an async fake
        # would only produce an un-awaited coroutine and never raise.
        def cancelled_bind(sessions, *, key, location):
            raise asyncio.CancelledError()

        monkeypatch.setattr("kiro_crew.wecom.transport_dispatch.bind_origin_mirror", cancelled_bind)
        d = _dispatcher(FakeSessions(), FakeClient())

        with pytest.raises(asyncio.CancelledError):
            await d.handle_message(_inbound("look at this", attachments=[_pair()]))

        assert not plaintext.exists(), (
            "the turn was cancelled during the origin bind and the decrypted "
            "attachment stayed on disk"
        )


class TestTurnPathStaysOffTheLoop:
    """The per-turn origin bind must not write the session map on the loop.

    ``bind_origin_mirror`` consults ``SessionManager.mirror_opt_out``, and that read
    WRITES: a refusal stored under an older generation key is promoted to the bucket
    inside ``batched_save``, whose block exit rewrites the whole map inline on the
    calling thread. This runs on EVERY inbound message, so on the loop a one-time
    migration stalls every other conversation and the WS heartbeat behind a disk
    write. Pinned by thread identity, because what makes it safe is not being on
    the loop.
    """

    @pytest.mark.asyncio
    async def test_the_origin_bind_is_offloaded(self, monkeypatch) -> None:
        threads: list[int] = []

        def recording_bind(sessions, *, key, location):
            threads.append(threading.get_ident())
            return True

        monkeypatch.setattr("kiro_crew.wecom.transport_dispatch.bind_origin_mirror", recording_bind)
        d = _dispatcher(FakeSessions(), FakeClient())

        await d._bind_origin_mirror("wecom:kirocrew:direct:Wei", _inbound("hi"))

        assert threads, "the bind never ran"
        assert threading.get_ident() not in threads, (
            "bind_origin_mirror ran on the event-loop thread, so a legacy opt-out "
            "migration writes the whole session map inline on the turn path"
        )

    @pytest.mark.asyncio
    async def test_set_origin_link_stays_ON_the_loop(self, monkeypatch) -> None:
        # The non-vacuity half: only the batching call is offloaded.
        # ``set_origin_link`` reaches ``SessionMap._save`` unbatched, whose
        # loop-aware branch schedules ONE debounced flush that writes in a worker
        # thread -- offloading it too would buy nothing and cost a thread hop.
        threads: list[int] = []

        class Recording(FakeSessions):
            def set_origin_link(self, key, link) -> None:
                threads.append(threading.get_ident())
                super().set_origin_link(key, link)

        d = _dispatcher(Recording(), FakeClient())

        await d._bind_origin_mirror("wecom:kirocrew:direct:Wei", _inbound("hi"))

        assert threads == [threading.get_ident()]


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_cancels_a_running_turn(self) -> None:
        provider = FakeProvider()
        client = FakeClient()
        d = _dispatcher(FakeSessions(provider, busy=True), client)

        await d.handle_message(_inbound("/stop"))

        assert provider.cancelled is True
        assert any("已停止" in s for s in client.said)

    @pytest.mark.asyncio
    async def test_stop_with_nothing_running_says_so(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider(), busy=False), client)
        await d.handle_message(_inbound("/stop"))
        assert any("没有正在生成" in s for s in client.said)

    @pytest.mark.asyncio
    async def test_a_provider_that_cannot_cancel_is_reported(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(SimpleNamespace(), busy=True), client)
        await d.handle_message(_inbound("/stop"))
        assert any("不支持停止" in s for s in client.said)

    @pytest.mark.asyncio
    async def test_a_failing_cancel_is_reported_rather_than_swallowed(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider(cancel_raises=True), busy=True), client)
        await d.handle_message(_inbound("/stop"))
        assert any("停止失败" in s for s in client.said)


# ---------------------------------------------------------------------------
# Attachment ingest
# ---------------------------------------------------------------------------


def _pair(url: str = "https://cdn/x") -> tuple[Attachment, str]:
    return Attachment(name="shot.png", mimetype="image/png", url=url), "key"


class TestACaptionIsNeverACommand:
    """A photo captioned `/new` must not be dropped on the floor.

    Every command branch RETURNS before `_ingest_media` runs, and a WeCom media
    URL lives about five minutes. So a captioned attachment whose caption parsed
    as a command reset the conversation (or printed the help card) and the picture
    simply never arrived — no error, nothing said about it, and by the time the
    user noticed and resent it the URL was already dead. Slack draws the same line
    with `and not files` on its stop keyword.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caption", ["/new", "/compact", "/help", "/stop", "/link", "/unlink"])
    async def test_a_captioned_attachment_reaches_ingestion(self, monkeypatch, caption) -> None:
        ingested: list = []

        async def fake_process(pairs, **kw):
            ingested.append([att.url for att, _ in pairs])
            return IngestResult(text_blocks=["[image]"])

        monkeypatch.setattr(
            "kiro_crew.wecom.transport_dispatch.process_wecom_attachments", fake_process
        )

        drove: list = []

        async def fake_drive_turn(turn, *, sessions, ctx_builder):
            drove.append(turn.user_text)

        monkeypatch.setattr("kiro_crew.wecom.transport_dispatch.drive_turn", fake_drive_turn)
        client = FakeClient()
        d = _dispatcher(FakeSessions(), client)

        await d.handle_message(_inbound(caption, attachments=[_pair()]))

        assert ingested == [["https://cdn/x"]], (
            f"a photo captioned {caption!r} never reached ingestion — the URL expires "
            f"in ~5 minutes, so the picture is gone with nothing said about it"
        )
        assert drove, "the message must still run as a turn"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caption", ["/steer", "/queue"])
    async def test_a_bare_override_caption_does_not_swallow_the_attachment(
        self, monkeypatch, caption
    ) -> None:
        # The bare-override usage reply returns too, so it loses the picture the
        # same way a command does.
        ingested: list = []

        async def fake_process(pairs, **kw):
            ingested.append([att.url for att, _ in pairs])
            return IngestResult(text_blocks=["[image]"])

        monkeypatch.setattr(
            "kiro_crew.wecom.transport_dispatch.process_wecom_attachments", fake_process
        )

        async def fake_drive_turn(turn, *, sessions, ctx_builder):
            pass

        monkeypatch.setattr("kiro_crew.wecom.transport_dispatch.drive_turn", fake_drive_turn)
        d = _dispatcher(FakeSessions(), FakeClient())

        await d.handle_message(_inbound(caption, attachments=[_pair()]))

        assert ingested == [["https://cdn/x"]]

    @pytest.mark.asyncio
    async def test_a_command_in_its_own_message_still_works(self, monkeypatch) -> None:
        """The non-vacuity half: the rule is about attachments, not about commands.

        Without this, disabling the intercept entirely would also pass above.
        """
        drove: list = []

        async def fake_drive_turn(turn, *, sessions, ctx_builder):
            drove.append(turn.user_text)

        monkeypatch.setattr("kiro_crew.wecom.transport_dispatch.drive_turn", fake_drive_turn)
        client = FakeClient()
        d = _dispatcher(FakeSessions(), client)

        await d.handle_message(_inbound("/new"))

        assert client.said == ["✅ 已开始新对话"]
        assert not drove, "a bare command must not become a turn"


class TestIngestMedia:
    @pytest.mark.asyncio
    async def test_no_attachments_passes_the_text_through_untouched(self) -> None:
        d = _dispatcher(FakeSessions(), FakeClient())
        assert await d._ingest_media(_inbound("hi"), "hi", "Wei") == ("hi", [])

    @pytest.mark.asyncio
    async def test_ingested_material_is_appended_and_temp_paths_returned(
        self, monkeypatch, tmp_path
    ) -> None:
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"\x89PNG")

        async def fake_process(pairs, **kw):
            return IngestResult(image_paths=[str(shot)], text_blocks=["[image: shot.png]"])

        monkeypatch.setattr(
            "kiro_crew.wecom.transport_dispatch.process_wecom_attachments", fake_process
        )
        inbound = _inbound("what is this?", attachments=[_pair()])
        d = _dispatcher(FakeSessions(), FakeClient())

        text, temps = await d._ingest_media(inbound, "what is this?", "Wei")

        assert text is not None and "what is this?" in text
        assert temps == [str(shot)]
        assert inbound.attachments == [], "consumed attachments must be cleared"

    @pytest.mark.asyncio
    async def test_an_unreadable_attachment_keeps_the_message_and_says_so(
        self, monkeypatch
    ) -> None:
        # Silence here is indistinguishable from the bot ignoring the user.
        async def boom(pairs, **kw):
            raise RuntimeError("cdn gone")

        monkeypatch.setattr("kiro_crew.wecom.transport_dispatch.process_wecom_attachments", boom)
        inbound = _inbound("look", attachments=[_pair()])
        d = _dispatcher(FakeSessions(), FakeClient())

        text, temps = await d._ingest_media(inbound, "look", "Wei")

        assert text is not None
        assert "look" in text and "附件无法读取" in text
        assert temps == []
        assert inbound.attachments == [], "a refused attachment must not be retried"

    @pytest.mark.asyncio
    async def test_an_unreadable_attachment_with_no_caption_still_replies(
        self, monkeypatch
    ) -> None:
        async def boom(pairs, **kw):
            raise RuntimeError("cdn gone")

        monkeypatch.setattr("kiro_crew.wecom.transport_dispatch.process_wecom_attachments", boom)
        d = _dispatcher(FakeSessions(), FakeClient())
        text, _ = await d._ingest_media(_inbound("", attachments=[_pair()]), "", "Wei")
        assert text == "[附件无法读取]"

    @pytest.mark.asyncio
    async def test_a_turn_starting_before_ingest_asks_for_a_resend(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(busy=True), client)
        inbound = _inbound("look", attachments=[_pair()])

        text, temps = await d._ingest_media(inbound, "look", "Wei")

        assert text == "look", "the caption still runs; only the attachment is declined"
        assert temps == []
        assert any("重新发送附件" in s for s in client.said)

    @pytest.mark.asyncio
    async def test_a_media_only_message_arriving_mid_turn_is_dropped_after_being_told(
        self,
    ) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(busy=True), client)
        text, _ = await d._ingest_media(_inbound("", attachments=[_pair()]), "", "Wei")
        assert text is None, "nothing to run, so no turn"
        assert any("重新发送附件" in s for s in client.said)

    @pytest.mark.asyncio
    async def test_a_turn_starting_DURING_ingest_cleans_up_the_decrypted_files(
        self, monkeypatch, tmp_path
    ) -> None:
        # A download takes real time, so a turn can start while it is in flight.
        # Without the second check the already-written plaintext would be inlined
        # into a steer whose files this frame then deletes.
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"\x89PNG")
        sessions = FakeSessions(busy=False)

        async def fake_process(pairs, **kw):
            sessions._busy = True  # a turn began while we were downloading
            return IngestResult(image_paths=[str(shot)])

        monkeypatch.setattr(
            "kiro_crew.wecom.transport_dispatch.process_wecom_attachments", fake_process
        )
        client = FakeClient()
        d = _dispatcher(sessions, client)

        text, temps = await d._ingest_media(_inbound("look", attachments=[_pair()]), "look", "Wei")

        assert temps == [], "the caller must not be handed files this frame deleted"
        assert not shot.exists(), "the decrypted plaintext must not be left on disk"
        assert any("重新发送附件" in s for s in client.said)

    @pytest.mark.asyncio
    async def test_a_refusal_reaches_the_prompt_rather_than_being_swallowed(
        self, monkeypatch
    ) -> None:
        # The contract is that a refused attachment is VISIBLE, and the mechanism is
        # `append_attachment_context` splicing rejections into the text the model
        # sees -- not a log line. Asserting on caplog instead made this depend on
        # logger propagation, which differs between a local run and CI's sharded
        # one: it passed here and failed on the 3.10 shard.
        async def fake_process(pairs, **kw):
            return IngestResult(rejections=["[Attachment shot.png — too large]"])

        monkeypatch.setattr(
            "kiro_crew.wecom.transport_dispatch.process_wecom_attachments", fake_process
        )
        d = _dispatcher(FakeSessions(), FakeClient())

        text, temps = await d._ingest_media(_inbound("look", attachments=[_pair()]), "look", "Wei")

        assert text is not None
        assert "look" in text, "the caption must survive"
        assert "too large" in text, "the refusal must reach the model, not vanish"
        assert temps == []

    @pytest.mark.asyncio
    async def test_a_refusal_with_no_caption_still_produces_a_prompt(self, monkeypatch) -> None:
        async def fake_process(pairs, **kw):
            return IngestResult(rejections=["[Attachment shot.png — too large]"])

        monkeypatch.setattr(
            "kiro_crew.wecom.transport_dispatch.process_wecom_attachments", fake_process
        )
        d = _dispatcher(FakeSessions(), FakeClient())
        text, _ = await d._ingest_media(_inbound("", attachments=[_pair()]), "", "Wei")
        assert text and "too large" in text


class TestProcessWeComAttachments:
    @pytest.mark.asyncio
    async def test_no_pairs_does_no_network_work(self) -> None:
        from kiro_crew.wecom.attachments import process_wecom_attachments

        assert await process_wecom_attachments([]) == IngestResult()

    @pytest.mark.asyncio
    async def test_each_item_is_downloaded_with_its_own_key_and_written_decrypted(
        self, monkeypatch, tmp_path
    ) -> None:
        # The key is PER OBJECT, so the download closure must reach the right one.
        from kiro_crew.wecom import attachments as mod

        seen: list[tuple[str, str]] = []

        async def fake_download(session, url, aeskey, *, proxy=None, max_bytes=0):
            seen.append((url, aeskey))
            return b"\x89PNG\r\n\x1a\n" + b"0" * 64

        monkeypatch.setattr(mod, "download_media", fake_download)

        async def fake_ingest(attachments, *, download, source, limits, handle_audio):
            # Drive the caller's download callback exactly as the real pipeline does.
            dest = tmp_path / "out.bin"
            await download(attachments[0].url, str(dest))
            return IngestResult(image_paths=[str(dest)])

        monkeypatch.setattr(mod, "ingest_attachments", fake_ingest)

        result = await mod.process_wecom_attachments(
            [(Attachment(name="a.png", mimetype="image/png", url="u1"), "k1")]
        )

        assert seen == [("u1", "k1")]
        assert result.image_paths
        assert (tmp_path / "out.bin").read_bytes().startswith(b"\x89PNG")

    def test_the_blocking_write_helper_writes_exactly_the_bytes(self, tmp_path) -> None:
        from kiro_crew.wecom.attachments import _write_bytes

        dest = tmp_path / "x.bin"
        _write_bytes(str(dest), b"abc")
        assert dest.read_bytes() == b"abc"

    @pytest.mark.asyncio
    async def test_the_channels_proxy_reaches_the_download(self, monkeypatch, tmp_path) -> None:
        # The batch opens its OWN aiohttp session and aiohttp does not read
        # HTTPS_PROXY unless asked, so on a proxy-only host an unproxied download
        # fails while the (proxied) WebSocket stays connected and the badge stays
        # green -- the picture just never arrives.
        from kiro_crew.wecom import attachments as mod

        seen: list[str | None] = []

        async def fake_download(session, url, aeskey, *, proxy=None, max_bytes=0):
            seen.append(proxy)
            return b"\x89PNG"

        async def fake_ingest(attachments, *, download, source, limits, handle_audio):
            await download(attachments[0].url, str(tmp_path / "o.bin"))
            return IngestResult()

        monkeypatch.setattr(mod, "download_media", fake_download)
        monkeypatch.setattr(mod, "ingest_attachments", fake_ingest)

        await mod.process_wecom_attachments(
            [(Attachment(name="a.png", mimetype="image/png", url="u"), "k")],
            proxy="http://proxy:3128",
        )
        assert seen == ["http://proxy:3128"]

    @pytest.mark.asyncio
    async def test_the_dispatcher_passes_the_clients_proxy(self, monkeypatch) -> None:
        captured: dict = {}

        async def fake_process(pairs, **kw):
            captured.update(kw)
            return IngestResult()

        monkeypatch.setattr(
            "kiro_crew.wecom.transport_dispatch.process_wecom_attachments", fake_process
        )
        client = FakeClient()
        client.proxy = "http://proxy:3128"  # type: ignore[misc]
        d = _dispatcher(FakeSessions(), client)
        await d._ingest_media(_inbound("look", attachments=[_pair()]), "look", "Wei")
        assert captured.get("proxy") == "http://proxy:3128"

    @pytest.mark.asyncio
    async def test_concurrent_ingests_are_bounded(self, monkeypatch, tmp_path) -> None:
        # Every inbound frame becomes its own turn task, so an authorized media
        # burst would otherwise start an unbounded number of ingests, each holding
        # ciphertext AND a decrypted copy before reaching any session lock.
        from kiro_crew.wecom import attachments as mod

        monkeypatch.setattr(mod, "_INGEST_GATE", None)  # a fresh gate on this loop
        live = 0
        peak = 0
        release = asyncio.Event()

        async def fake_ingest(attachments, *, download, source, limits, handle_audio):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await release.wait()
            live -= 1
            return IngestResult()

        monkeypatch.setattr(mod, "ingest_attachments", fake_ingest)
        pairs = [(Attachment(name="a.png", mimetype="image/png", url="u"), "k")]
        tasks = [asyncio.create_task(mod.process_wecom_attachments(list(pairs))) for _ in range(6)]
        await asyncio.sleep(0.05)
        observed_peak = peak
        release.set()
        await asyncio.gather(*tasks)

        assert observed_peak <= mod._MAX_CONCURRENT_INGESTS, (
            f"{observed_peak} ingests ran at once; the bound is " f"{mod._MAX_CONCURRENT_INGESTS}"
        )

    @pytest.mark.asyncio
    async def test_an_audio_file_is_handled_rather_than_silently_skipped(
        self, monkeypatch, tmp_path
    ) -> None:
        # With handle_audio=False the shared pipeline skipped an AUDIO item with no
        # rejection and no audit row, so an audio-only message produced an empty
        # turn and the sender was told nothing.
        from kiro_crew.wecom import attachments as mod

        seen: dict = {}

        async def fake_ingest(attachments, *, download, source, limits, handle_audio):
            seen["handle_audio"] = handle_audio
            return IngestResult(audio_paths=[str(tmp_path / "a.mp3")])

        transcribed: list[Any] = []

        async def fake_transcribe(result, source):
            transcribed.append(source)
            result.text_blocks.append("[audio transcript]")
            return result

        monkeypatch.setattr(mod, "ingest_attachments", fake_ingest)
        monkeypatch.setattr(mod, "transcribe_audio_attachments", fake_transcribe)

        result = await mod.process_wecom_attachments(
            [(Attachment(name="rec.mp3", mimetype="audio/mpeg", url="u"), "k")]
        )

        assert seen["handle_audio"] is True
        assert transcribed == ["wecom"], "the shared transcriber owns the STT-unavailable wording"
        assert result.text_blocks == ["[audio transcript]"]

    @pytest.mark.asyncio
    async def test_a_cancelled_transcription_does_not_leave_plaintext_on_disk(
        self, monkeypatch, tmp_path
    ) -> None:
        """Shutdown mid-transcription must not strand the DECRYPTED audio.

        The dispatcher cleans up in a ``finally``, but only for a result it
        RECEIVED. Transcription runs after the ingest gate is released and can take
        as long as a model does, so a gateway shutdown lands here — and
        ``CancelledError`` is not an ``Exception``, so it propagates out and the
        dispatcher never sees the paths it was supposed to delete. What is left
        behind is the user's audio in the clear.
        """
        from kiro_crew.wecom import attachments as mod

        monkeypatch.setattr(mod, "_INGEST_GATE", None)
        plaintext = tmp_path / "decrypted.mp3"
        plaintext.write_bytes(b"cleartext audio")

        async def fake_ingest(attachments, *, download, source, limits, handle_audio):
            # temp_paths is derived from image_paths + audio_paths.
            return IngestResult(audio_paths=[str(plaintext)])

        async def cancelled_transcribe(result, source):
            raise asyncio.CancelledError()

        monkeypatch.setattr(mod, "ingest_attachments", fake_ingest)
        monkeypatch.setattr(mod, "transcribe_audio_attachments", cancelled_transcribe)

        with pytest.raises(asyncio.CancelledError):
            await mod.process_wecom_attachments(
                [(Attachment(name="rec.mp3", mimetype="audio/mpeg", url="u"), "k")]
            )

        assert not plaintext.exists(), "decrypted audio survived a cancelled transcription"

    @pytest.mark.asyncio
    async def test_the_cancellation_cleanup_runs_OFF_the_event_loop(
        self, monkeypatch, tmp_path
    ) -> None:
        """Deleting the plaintext must not stall the loop it is shutting down on.

        ``os.unlink`` is a blocking syscall and TMPDIR is not always local -- a
        network- or FUSE-backed temp dir makes each delete a round trip, and the
        cap is ten attachments per message. Inline, that is a gateway-wide stall
        on exactly the path a shutdown takes.

        The delete must still HAPPEN, which is what separates this from simply
        moving the call: it is submitted to the executor, so an interrupted await
        cannot skip it.
        """
        from kiro_crew.wecom import attachments as mod

        monkeypatch.setattr(mod, "_INGEST_GATE", None)
        plaintext = tmp_path / "decrypted.mp3"
        plaintext.write_bytes(b"cleartext audio")
        threads: list[int] = []
        real_cleanup = mod.cleanup_offloaded

        async def recording_cleanup(paths):
            import kiro_crew.messaging.attachments as shared

            original = shared.cleanup

            def spy(p):
                threads.append(threading.get_ident())
                original(p)

            monkeypatch.setattr(shared, "cleanup", spy)
            await real_cleanup(paths)

        monkeypatch.setattr(mod, "cleanup_offloaded", recording_cleanup)

        async def fake_ingest(attachments, *, download, source, limits, handle_audio):
            return IngestResult(audio_paths=[str(plaintext)])

        async def cancelled_transcribe(result, source):
            raise asyncio.CancelledError()

        monkeypatch.setattr(mod, "ingest_attachments", fake_ingest)
        monkeypatch.setattr(mod, "transcribe_audio_attachments", cancelled_transcribe)

        with pytest.raises(asyncio.CancelledError):
            await mod.process_wecom_attachments(
                [(Attachment(name="rec.mp3", mimetype="audio/mpeg", url="u"), "k")]
            )

        assert not plaintext.exists(), "the delete did not happen"
        assert threads, "cleanup never ran"
        assert threading.get_ident() not in threads, (
            "the unlink ran on the event-loop thread, stalling every other "
            "conversation and the WS heartbeat behind a blocking syscall"
        )

    @pytest.mark.asyncio
    async def test_an_audio_FILE_the_platform_carried_is_not_refused_locally(
        self, monkeypatch, tmp_path
    ) -> None:
        """A 5 MB mp3 is under WeCom's file ceiling, so it must reach the turn.

        The audio cap was WeCom's 2 MB *voice-message* limit, which is unreachable
        here: a voice message is transcribed by the platform and ``media_items``
        excludes it, so the only audio that arrives is a FILE — bounded by the 20 MB
        file ceiling. The 2 MB cap therefore refused nothing WeCom would have
        refused and everything between 2 and 20 MB that it accepted, which is the
        local-only rejection these per-channel overrides exist to prevent.
        """
        from kiro_crew.wecom import attachments as mod

        monkeypatch.setattr(mod, "_INGEST_GATE", None)
        five_mb = 5 * 1024 * 1024

        async def fake_download(session, url, aeskey, *, proxy=None, max_bytes=0):
            return b"ID3fake-audio"  # the real one needs a live CDN object

        monkeypatch.setattr(mod, "download_media", fake_download)

        async def fake_transcribe(result, source):
            return result

        monkeypatch.setattr(mod, "transcribe_audio_attachments", fake_transcribe)

        result = await mod.process_wecom_attachments(
            [
                (
                    Attachment(name="rec.mp3", mimetype="audio/mpeg", url="u", size=five_mb),
                    "k",
                )
            ]
        )

        try:
            assert not result.rejections, f"a file WeCom carried was refused: {result.rejections}"
            assert result.audio_paths, "accepted audio must reach the transcriber"
        finally:
            # The ingest contract puts cleanup on the caller; here that is the test.
            cleanup_attachments(result.temp_paths)

    def test_the_text_budget_is_deliberately_below_the_transport_ceiling(self) -> None:
        # Not an oversight and not a platform limit: max_text_bytes budgets what is
        # READ into gateway memory, and only max_text_inject can reach the prompt.
        # Pinned so raising it to the transport ceiling is a deliberate act.
        from kiro_crew.wecom.attachments import WECOM_INGEST_LIMITS as lim

        assert lim.max_text_bytes < lim.max_document_bytes
        assert lim.max_text_inject <= lim.max_text_bytes

    def test_every_transport_ceiling_matches_wecoms_documented_maximum(self) -> None:
        from kiro_crew.wecom.attachments import WECOM_INGEST_LIMITS as lim
        from kiro_crew.wecom.media import MAX_MEDIA_BYTES

        assert lim.max_image_bytes == 10 * 1024 * 1024
        assert lim.max_document_bytes == 20 * 1024 * 1024
        assert (
            lim.max_audio_bytes == lim.max_document_bytes
        ), "audio arrives as a file, so it shares the file ceiling"
        assert lim.max_attachments == 10
        # The download cap must not be the binding constraint, or an item the
        # ceilings admit would be truncated mid-stream instead of accepted. `>=` is
        # NOT enough: the cap is enforced on CIPHERTEXT, and PKCS#7 to a 32-byte
        # multiple always adds 1..32 bytes, so a file at exactly the platform
        # maximum arrives larger than it is. Equality rejected precisely the
        # largest valid attachments, before decryption.
        from kiro_crew.wecom.media import WECOM_MAX_PLAINTEXT_BYTES

        assert lim.max_document_bytes == WECOM_MAX_PLAINTEXT_BYTES
        assert MAX_MEDIA_BYTES - WECOM_MAX_PLAINTEXT_BYTES >= 32, (
            "the ciphertext cap leaves no room for padding, so a maximum-sized "
            "file WeCom accepted is refused locally"
        )


def test_no_event_loop_is_left_running() -> None:
    """Guards against a test above leaking a loop into the next module."""
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()
