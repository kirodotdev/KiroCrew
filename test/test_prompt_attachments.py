"""Image blocks come ONLY from the channel's structured attachment list.

The prompt text is never scanned for image paths. Before this, any text that
merely CONTAINED a readable image path became an image block: the session
ledger's ``artifact <name>: <path>.png`` snapshot line in every nudge cycle, a
nudge body, an injected envelope, an agent's own ``![shot](...png)`` reply
quoted back. Because the backend replays every stored image block, one
screenshot named in a per-cycle snapshot grew the request by its full encoded
size on every turn until the backend rejected the body.

The tests here pin both halves of the contract: text alone produces zero image
blocks whatever it contains, and one structured attachment produces exactly one
block plus the ``[image: <name>]`` marker in the text.
"""

from __future__ import annotations

import base64
import json
import pathlib

import pytest

from kiro_crew.acp.prompt_blocks import build_prompt_blocks
from kiro_crew.prompt_attachments import (
    NAME_MAX_CHARS,
    PromptAttachment,
    bounded_name,
    image_attachments,
    markdown_image_dest,
    named_in_text,
    path_spans,
    path_spellings,
)

# Smallest valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _png(tmp_path, name="shot.png"):
    p = tmp_path / name
    p.write_bytes(_PNG)
    return p


#: The composer's destination grammar, pinned once for both implementations
#: (see the fixture's own comment; the frontend reads the same file).
_SHARED_DEST_VECTORS = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "markdown_image_dest.json"
)


def _types(blocks):
    return [b["type"] for b in blocks]


class TestTextIsNeverScanned:
    """Whatever the text contains, no attachment list means no image block."""

    def test_ledger_snapshot_line_and_nudge_body_produce_zero_image_blocks(self, tmp_path):
        """The exact shape the autonudge fire path sends every cycle:
        ``render_snapshot`` lines, a blank line, then the nudge body."""
        p = _png(tmp_path)
        snapshot = "\n".join(
            [
                "[work ledger — durable state for this session; "
                "authoritative over memory of prior cycles]",
                "goal: keep the PR green",
                "phase: awaiting-ci",
                f"artifact screenshot: {p}",
            ]
        )
        body = f"[auto-nudge cycle 3] re-check the CI lanes; the capture is at {p}"
        text = f"{snapshot}\n\n{body}"

        blocks = build_prompt_blocks(text)

        assert _types(blocks) == ["text"]
        # The text is passed through untouched: no marker, no rewrite.
        assert blocks[0]["text"] == text

    def test_agent_reply_quoted_back_produces_no_image_block(self, tmp_path):
        p = _png(tmp_path)
        text = f"You said earlier:\n> ![shot]({p})\n\nwhat did it show?"
        blocks = build_prompt_blocks(text)
        assert _types(blocks) == ["text"]
        assert blocks[0]["text"] == text

    def test_dashboard_wire_text_without_the_list_produces_no_image_block(self, tmp_path):
        """The composer's ``![image](dest)`` line is a RENDERING of the upload,
        not the upload: only ``meta.images`` says the user attached it."""
        p = _png(tmp_path)
        text = f"![image]({p})\n\nwhat is this?"
        blocks = build_prompt_blocks(text)
        assert _types(blocks) == ["text"]
        assert blocks[0]["text"] == text

    def test_typed_bare_path_produces_no_image_block(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"look at {p} please")
        assert _types(blocks) == ["text"]
        assert blocks[0]["text"] == f"look at {p} please"

    def test_empty_list_is_the_same_as_no_list(self, tmp_path):
        p = _png(tmp_path)
        assert build_prompt_blocks(f"see {p}", attachments=()) == [
            {"type": "text", "text": f"see {p}"}
        ]


class TestStructuredAttachments:
    def test_one_attachment_produces_exactly_one_block_and_a_marker(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks("what is this?", attachments=image_attachments([str(p)]))

        assert _types(blocks) == ["text", "image"]
        # The marker tells the model what it was given, even though the text
        # never named the path.
        assert blocks[0]["text"] == "what is this?\n[image: shot.png]"
        assert blocks[1]["mimeType"] == "image/png"
        assert base64.b64decode(blocks[1]["data"]) == _PNG

    def test_known_path_in_the_text_is_rewritten_to_the_marker(self, tmp_path):
        """A channel that also wrote the path into the text (Slack appends it as
        a bare line, the dashboard renders it as markdown) gets the SAME text
        shape as before: the path becomes ``[image: <name>]`` in place."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(
            f"![image]({p})\n\nwhat is this?",
            attachments=image_attachments([str(p)]),
        )
        assert _types(blocks) == ["text", "image"]
        assert blocks[0]["text"] == "![image]([image: shot.png])\n\nwhat is this?"
        assert str(p) not in blocks[0]["text"]

    def test_marker_uses_the_attachment_name_when_given(self, tmp_path):
        p = _png(tmp_path)
        att = PromptAttachment(path=str(p), name="Screenshot 2026.png")
        blocks = build_prompt_blocks("see", attachments=[att])
        assert blocks[0]["text"] == "see\n[image: Screenshot 2026.png]"

    def test_same_path_listed_twice_is_encoded_once(self, tmp_path):
        p = _png(tmp_path)
        atts = [
            PromptAttachment(path=str(p)),
            PromptAttachment(path=str(p)),
        ]
        blocks = build_prompt_blocks(f"{p} then {p} again", attachments=atts)
        assert _types(blocks) == ["text", "image"]
        assert blocks[0]["text"] == "[image: shot.png] then [image: shot.png] again"

    def test_two_attachments_each_get_a_block_in_list_order(self, tmp_path):
        a = _png(tmp_path, "a.png")
        b = _png(tmp_path, "b.png")
        blocks = build_prompt_blocks("compare", attachments=image_attachments([str(a), str(b)]))
        assert _types(blocks) == ["text", "image", "image"]
        assert blocks[0]["text"] == "compare\n[image: a.png]\n[image: b.png]"

    def test_capability_gate_leaves_the_text_untouched(self, tmp_path):
        """No advertised image capability: no block, and the channel's own path
        text is left in place so a tool-capable agent can still open the file."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(
            f"look at {p}",
            attachments=image_attachments([str(p)]),
            allow_image=False,
        )
        assert _types(blocks) == ["text"]
        assert blocks[0]["text"] == f"look at {p}"

    def test_oversized_attachment_falls_back_to_the_text(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks(
            f"see {p}",
            attachments=image_attachments([str(p)]),
            max_image_bytes=1,
        )
        assert _types(blocks) == ["text"]
        assert blocks[0]["text"] == f"see {p}"

    def test_missing_file_and_directory_are_skipped(self, tmp_path):
        d = tmp_path / "weird.png"
        d.mkdir()
        atts = image_attachments(["/definitely/not/here.png", str(d)])
        blocks = build_prompt_blocks("see", attachments=atts)
        assert blocks == [{"type": "text", "text": "see"}]

    def test_non_raster_bytes_are_not_inlined(self, tmp_path):
        """The channel's claim is a claim: the bytes decide what reaches the
        wire, exactly as before."""
        p = tmp_path / "script.png"
        p.write_bytes(b"#!/bin/sh\necho hi\n" + b" " * 64)
        blocks = build_prompt_blocks("see", attachments=image_attachments([str(p)]))
        assert blocks == [{"type": "text", "text": "see"}]

    def test_the_wire_type_is_sniffed_from_the_bytes_not_the_suffix(self, tmp_path):
        """The record carries no type and the suffix is a claim: a PNG named
        ``.jpg`` goes out as ``image/png``."""
        p = tmp_path / "photo.jpg"
        p.write_bytes(_PNG)
        blocks = build_prompt_blocks("see", attachments=[PromptAttachment(path=str(p))])
        assert _types(blocks) == ["text", "image"]
        assert blocks[1]["mimeType"] == "image/png"

    def test_text_block_always_leads(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks("", attachments=image_attachments([str(p)]))
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "[image: shot.png]"


class TestLeafModule:
    def test_image_attachments_keeps_order_drops_blanks_and_dedups(self):
        atts = image_attachments(["/a.png", "", "  ", "/b.png", "/a.png", None])  # type: ignore[list-item]
        assert [a.path for a in atts] == ["/a.png", "/b.png"]

    def test_display_name_defaults_to_the_basename(self):
        assert PromptAttachment(path="/tmp/x/shot.png").display_name == "shot.png"
        assert PromptAttachment(path="/tmp/x/shot.png", name="mine").display_name == "mine"

    def test_display_name_is_one_bounded_line(self):
        """The name is the sender's filename, read inline by the model: no
        newline may break the marker and no length may flood it."""
        att = PromptAttachment(path="/tmp/a.png", name="a\nb\t c" + "x" * 300)
        shown = att.display_name
        assert "\n" not in shown and "\t" not in shown
        assert shown.startswith("a b c")
        assert len(shown) == NAME_MAX_CHARS
        # An all-whitespace name falls back to the basename.
        assert PromptAttachment(path="/tmp/a.png", name=" \n ").display_name == "a.png"

    def test_the_basename_fallback_is_bounded_and_flattened_too(self):
        """With no name, the basename is what the model reads: the same bound
        and the same one-line rule apply to it."""
        long_base = "y" * 300 + ".png"
        shown = PromptAttachment(path=f"/tmp/{long_base}").display_name
        assert len(shown) == NAME_MAX_CHARS
        odd = PromptAttachment(path="/tmp/two\nlines .png").display_name
        assert "\n" not in odd
        assert odd == "two lines .png"

    def test_bounded_name_is_the_one_rule(self):
        assert bounded_name("a\nb" + "x" * 500) == ("a b" + "x" * 500)[:NAME_MAX_CHARS]
        assert bounded_name("  spaced   out  ") == "spaced out"
        assert bounded_name("") == ""

    def test_record_is_immutable(self):
        att = PromptAttachment(path="/tmp/a.png")
        with pytest.raises(AttributeError):
            att.path = "/tmp/b.png"  # type: ignore[misc]

    def test_markdown_image_dest_mirrors_the_composer(self):
        """One vector file, two consumers: the frontend's ``fileTokens.test.ts``
        asserts ``mdImageDest(path) === dest`` over the same cases this asserts
        ``markdown_image_dest(path) == dest`` for, so a change to either half of
        the grammar that is not mirrored in the other goes red here -- the
        failure mode otherwise is silent: the prune stops recognising the
        spelling the composer wrote and keeps a picture the user deleted."""
        cases = json.loads(_SHARED_DEST_VECTORS.read_text(encoding="utf-8"))["cases"]
        assert len(cases) >= 8
        for case in cases:
            assert markdown_image_dest(case["path"]) == case["dest"], case["name"]

    def test_path_spellings_cover_bare_slashed_and_wrapped_forms(self):
        assert path_spellings("/tmp/shot.png") == ("/tmp/shot.png",)
        assert set(path_spellings("/tmp/my shot.png")) == {"/tmp/my shot.png", "</tmp/my shot.png>"}
        assert set(path_spellings(r"C:\x\shot.png")) == {r"C:\x\shot.png", "C:/x/shot.png"}
        # Longest first, so a substitution replaces the wrapped form whole.
        assert path_spellings("/tmp/my shot.png")[0] == "</tmp/my shot.png>"

    def test_a_posix_path_holding_a_backslash_gets_no_slash_translated_sibling(self):
        # Only a Windows-shaped path was spelled with forward slashes by a
        # channel; a POSIX name that merely contains a backslash was not, and
        # a slash-translated sibling of it would be an unrelated path that the
        # marker rewrite then replaced out of the user's prose (GPT review).
        weird = "/tmp/weird\\name.png"
        spellings = path_spellings(weird)
        assert "/tmp/weird/name.png" not in spellings
        assert weird in spellings
        text = f"see /tmp/weird/name.png and ![image]({markdown_image_dest(weird)})"
        spans = path_spans(weird, text)
        assert [text[a:b] for a, b in spans] == [markdown_image_dest(weird)]
        assert named_in_text("/tmp/my shot.png", "![image](</tmp/my shot.png>)") is True
        assert named_in_text("/tmp/my shot.png", "nothing here") is False

    def test_a_longer_path_sharing_the_prefix_is_not_a_naming(self):
        """``/tmp/a.png.bak`` names a different file: a bare substring test
        would keep a removed picture alive through it and let the marker
        rewrite corrupt the longer path. A spelling counts only when it stands
        delimited -- a bare token, or a markdown destination."""
        assert named_in_text("/tmp/a.png", "see /tmp/a.png.bak") is False
        assert named_in_text("/tmp/a.png", "/tmp/a.png.bak") is False
        assert named_in_text("/tmp/a.png", "x/tmp/a.png") is False
        assert named_in_text("/tmp/a.png", "/tmp/a.png") is True
        assert named_in_text("/tmp/a.png", "look\n/tmp/a.png\nplease") is True
        assert named_in_text("/tmp/a.png", "![image](/tmp/a.png)") is True
        assert named_in_text("/tmp/a.png", '"/tmp/a.png" and `/tmp/a.png`') is True

    def test_path_spans_is_linear_in_the_matches(self):
        """The queued-edit prune runs this on the gateway loop over caller
        text; a text made of tens of thousands of bare occurrences must not
        cost the square of that (the old overlap test re-scanned every
        accepted span per candidate)."""
        import time

        text = " ".join(["/tmp/a.png"] * 20_000)
        t0 = time.perf_counter()
        spans = path_spans("/tmp/a.png", text)
        assert time.perf_counter() - t0 < 2.0
        assert len(spans) == 20_000
        assert spans == sorted(spans) and all(b <= c for (_, b), (c, _) in zip(spans, spans[1:]))

    def test_path_spans_cover_only_delimited_occurrences(self):
        text = "see /tmp/a.png.bak then ![image](/tmp/a.png) and /tmp/a.png"
        spans = path_spans("/tmp/a.png", text)
        assert [text[a:b] for a, b in spans] == ["/tmp/a.png", "/tmp/a.png"]
        assert text[spans[0][0] - 1] == "(" and text[spans[0][1]] == ")"
        assert path_spans("/tmp/a.png", "/tmp/a.png.bak") == []
        # The wrapped destination is one span, not the bare path inside it.
        wrapped = f"![image]({markdown_image_dest('/tmp/my shot.png')})"
        assert [wrapped[a:b] for a, b in path_spans("/tmp/my shot.png", wrapped)] == [
            "</tmp/my shot.png>"
        ]


class TestEscapedDestinationsInTheText:
    def test_a_wrapped_destination_is_rewritten_whole(self, tmp_path):
        """The composer escapes and wraps a destination with a space; the
        marker must replace that whole spelling, not leave `<[image: ..]>`."""
        p = _png(tmp_path, "my shot.png")
        dest = markdown_image_dest(str(p))
        assert dest.startswith("<") and dest.endswith(">")
        blocks = build_prompt_blocks(
            f"![image]({dest})\n\nlook", attachments=image_attachments([str(p)])
        )
        assert _types(blocks) == ["text", "image"]
        assert blocks[0]["text"] == "![image]([image: my shot.png])\n\nlook"

    def test_a_longer_path_sharing_the_prefix_is_left_alone(self, tmp_path):
        """``/tmp/a.png.bak`` in the text is another file: the marker rewrite
        must not turn it into ``[image: a.png].bak``. Only the delimited
        destination is rewritten; when only the longer path is named, the text
        is untouched and the marker is appended."""
        p = _png(tmp_path, "a.png")
        longer = f"{p}.bak"
        blocks = build_prompt_blocks(
            f"compare {longer} with ![image]({p})",
            attachments=image_attachments([str(p)]),
        )
        assert _types(blocks) == ["text", "image"]
        assert blocks[0]["text"] == f"compare {longer} with ![image]([image: a.png])"

        blocks = build_prompt_blocks(f"only {longer} here", attachments=image_attachments([str(p)]))
        assert _types(blocks) == ["text", "image"]
        assert blocks[0]["text"] == f"only {longer} here\n[image: a.png]"


# ── the seams between a channel and the builder ────────────────────────────


class _RecordingProvider:
    """Records what the driver hands it; yields one chunk and completes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def stream(self, message, **kw):
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent

        self.calls.append((message, kw))
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    async def approve_tool(self, request_id, *, always=False):
        return None

    async def reject_tool(self, request_id):
        return None


async def _drain(it):
    return [x async for x in it]


class TestTurnDriverSeam:
    @pytest.mark.asyncio
    async def test_run_forwards_the_list_to_the_provider(self, tmp_path):
        from kiro_crew.messaging.driver import APPROVAL_AUTO, TurnDriver
        from kiro_crew.messaging.renderer import SilentRenderer

        provider = _RecordingProvider()
        driver = TurnDriver(provider, SilentRenderer(), approval_mode=APPROVAL_AUTO)
        atts = image_attachments([str(tmp_path / "a.png")])
        await driver.run("look", attachments=atts)
        assert provider.calls == [("look", {"attachments": atts})]

    @pytest.mark.asyncio
    async def test_a_text_only_turn_passes_no_keyword(self):
        """A provider stand-in predating the keyword still takes every text turn."""
        from kiro_crew.messaging.driver import APPROVAL_AUTO, TurnDriver
        from kiro_crew.messaging.renderer import SilentRenderer

        provider = _RecordingProvider()
        driver = TurnDriver(provider, SilentRenderer(), approval_mode=APPROVAL_AUTO)
        await driver.run("look")
        assert provider.calls == [("look", {})]


class TestChannelTurnSeam:
    """Teams, Webex, WeCom, Weixin and WhatsApp reach the driver through the
    shared ``ChannelTurn`` -> ``drive_turn`` pipeline; its ``attachments`` field
    is what carries their ingested images."""

    @staticmethod
    def _pipeline(monkeypatch, runs):
        from kiro_crew.messaging import dispatch as D

        class _Driver:
            last_stop_reason = ""

            def __init__(self, *a, **kw):
                self._closing_gate = kw.get("closing_gate")

            async def run(self, message, **kw):
                if self._closing_gate is not None:
                    self._closing_gate()
                runs.append((message, kw))
                return "the reply"

        async def _permitted(_channel_type):
            return True

        async def _publish(_sessions, _key):
            pass

        async def _embed(fn, *args, **kw):
            return fn(*args, **kw)

        monkeypatch.setattr(D, "inbound_permitted", _permitted)
        monkeypatch.setattr(D, "publish_turn_identity", _publish)
        monkeypatch.setattr(D, "run_in_embed_pool", _embed)
        monkeypatch.setattr(D, "TurnDriver", _Driver)

    class _Sessions:
        async def get_or_create(self, key, agent=None, channel_id=None, start_priority=None):
            return object(), False, False

        def begin_turn(self, key):
            pass

        async def set_channel(self, key, channel_id):
            pass

        def record_success(self, key):
            pass

        async def record_failure(self, key):
            pass

        def release(self, key):
            pass

        def get_provider(self, key):
            return object()

    class _Renderer:
        async def on_turn_start(self):
            pass

        async def close(self):
            pass

    class _CtxBuilder:
        def build_message(self, text, is_new, session_key, **kw):
            return text, None

    def _turn(self, attachments=()):
        from kiro_crew.messaging.dispatch import ChannelTurn

        return ChannelTurn(
            channel_type="weixin",
            session_key="weixin:agentA:direct:userA",
            conversation_id="weixin:userA",
            agent="agentA",
            user_text="what is this?",
            renderer=self._Renderer(),
            approval_mode="auto",
            attachments=attachments,
        )

    @pytest.mark.asyncio
    async def test_the_turn_attachments_reach_the_driver(self, monkeypatch, tmp_path):
        from kiro_crew.messaging.dispatch import drive_turn

        runs: list[tuple[str, dict]] = []
        self._pipeline(monkeypatch, runs)
        atts = image_attachments([str(tmp_path / "shot.jpg")])
        await drive_turn(
            self._turn(atts), sessions=self._Sessions(), ctx_builder=self._CtxBuilder()
        )
        assert runs == [("what is this?", {"attachments": atts})]

    @pytest.mark.asyncio
    async def test_a_turn_without_attachments_passes_no_keyword(self, monkeypatch):
        from kiro_crew.messaging.dispatch import drive_turn

        runs: list[tuple[str, dict]] = []
        self._pipeline(monkeypatch, runs)
        await drive_turn(self._turn(), sessions=self._Sessions(), ctx_builder=self._CtxBuilder())
        assert runs == [("what is this?", {})]

    def test_the_field_defaults_to_empty(self):
        from kiro_crew.messaging.dispatch import ChannelTurn

        assert ChannelTurn.__dataclass_fields__["attachments"].default == ()


class TestSessionProviderSeam:
    """The shared-runtime provider binds the list onto ``handle.prompt``; the
    essential-delivery seam between them rewrites text only."""

    @staticmethod
    def _provider(calls):
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp.session_provider import AcpSessionProvider
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent, AcpPromptStats

        async def _prompt(message, **kw):
            calls.append((message, kw))
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        handle = MagicMock()
        handle.session_id = "s1"
        handle.memory_mode = "persistent"
        handle.last_prompt_stats = AcpPromptStats()
        handle.destroy = AsyncMock()
        handle.prompt = _prompt
        runtime = MagicMock()
        runtime.saw_not_logged_in.return_value = False
        return AcpSessionProvider(handle, runtime)

    @pytest.mark.asyncio
    async def test_stream_binds_the_list_onto_the_handle_prompt(self, tmp_path):
        calls: list[tuple[str, dict]] = []
        provider = self._provider(calls)
        atts = image_attachments([str(tmp_path / "a.png")])
        await _drain(provider.stream("hi", attachments=atts))
        assert calls == [("hi", {"attachments": atts})]

    @pytest.mark.asyncio
    async def test_a_text_only_stream_calls_the_handle_bare(self):
        calls: list[tuple[str, dict]] = []
        provider = self._provider(calls)
        await _drain(provider.stream("hi"))
        assert calls == [("hi", {})]

    @pytest.mark.asyncio
    async def test_stream_events_is_the_same_seam(self, tmp_path):
        calls: list[tuple[str, dict]] = []
        provider = self._provider(calls)
        atts = image_attachments([str(tmp_path / "a.png")])
        await _drain(provider.stream_events("hi", attachments=atts))
        assert calls == [("hi", {"attachments": atts})]


class TestAcpProviderSeam:
    """The direct-client provider binds the list onto ``client.stream_events``."""

    @staticmethod
    def _provider(calls):
        from unittest.mock import MagicMock, patch

        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
        from kiro_crew.providers.acp import AcpProvider

        async def _stream_events(message, **kw):
            calls.append((message, kw))
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        with patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider()
        provider._client = MagicMock()
        provider._client.stream_events = _stream_events
        return provider

    @pytest.mark.asyncio
    async def test_stream_binds_the_list_onto_stream_events(self, tmp_path):
        calls: list[tuple[str, dict]] = []
        provider = self._provider(calls)
        atts = image_attachments([str(tmp_path / "a.png")])
        await _drain(provider.stream("hi", attachments=atts))
        assert calls == [("hi", {"attachments": atts})]

    @pytest.mark.asyncio
    async def test_a_text_only_stream_calls_the_client_bare(self):
        calls: list[tuple[str, dict]] = []
        provider = self._provider(calls)
        await _drain(provider.stream("hi"))
        assert calls == [("hi", {})]


class TestSessionHandleSeam:
    @pytest.mark.asyncio
    async def test_prompt_hands_the_list_to_the_builder(self, monkeypatch, tmp_path):
        """The one place image blocks are built: the handle's ``prompt`` passes
        the channel's list straight into ``build_prompt_blocks``."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp import session_handle as sh

        seen: list[dict] = []

        def _builder(message, **kw):
            seen.append(kw)
            return [{"type": "text", "text": message}]

        monkeypatch.setattr(sh, "build_prompt_blocks", _builder)
        runtime = MagicMock()
        runtime.supports_image_prompt = True
        runtime.send_request = AsyncMock(side_effect=asyncio.CancelledError())
        handle = sh.AcpSessionHandle("s1", asyncio.Queue(), runtime)
        atts = image_attachments([str(tmp_path / "a.png")])
        gen = handle.prompt("hi", timeout=3.0, attachments=atts)
        with pytest.raises(asyncio.CancelledError):
            await gen.__anext__()
        assert seen == [{"attachments": atts, "allow_image": True}]


class TestDashboardMetaPlumbing:
    """The composer's ``meta.images`` list is the dashboard's structured
    attachment source: validated with the marker lists, drained alone, pruned
    with an edit, and turned into the runner's list."""

    def test_attachment_meta_keeps_the_image_list(self):
        from kiro_crew.dashboard.chat_delivery import attachment_meta

        out = attachment_meta({"files": ["/tmp/a.pdf"], "images": ["/tmp/shot.png", "/tmp/b.png"]})
        assert out == {"files": ["/tmp/a.pdf"], "images": ["/tmp/shot.png", "/tmp/b.png"]}

    def test_attachment_meta_refuses_a_malformed_image_list(self):
        from kiro_crew.dashboard.chat_delivery import attachment_meta

        assert attachment_meta({"images": ["/tmp/a.png", 7]}) == {}
        assert attachment_meta({"images": []}) == {}
        assert attachment_meta({"images": "/tmp/a.png"}) == {}

    def test_an_image_bearing_entry_drains_alone(self):
        from kiro_crew.dashboard.chat_utils import carries_attachments

        assert carries_attachments({"meta": {"images": ["/tmp/a.png"]}}) is True
        assert carries_attachments({"meta": {"images": []}}) is False
        assert carries_attachments({"meta": {}}) is False

    def test_an_edit_that_removes_the_image_line_drops_it_from_the_list(self):
        from kiro_crew.dashboard.slot_queue_repository import prune_attachment_meta

        meta = {"images": ["/tmp/a.png", "/tmp/b.png"]}
        previous = "![image](/tmp/a.png)\n![image](/tmp/b.png)\n\nlook"
        content = prune_attachment_meta(meta, "![image](/tmp/b.png)\n\nlook", previous)
        assert content == "![image](/tmp/b.png)\n\nlook"
        assert meta == {"images": ["/tmp/b.png"]}

    def test_an_edit_that_removes_every_image_line_drops_the_key(self):
        from kiro_crew.dashboard.slot_queue_repository import prune_attachment_meta

        meta = {"images": ["/tmp/a.png"]}
        prune_attachment_meta(meta, "look", "![image](/tmp/a.png)\n\nlook")
        assert meta == {}

    def test_an_image_the_previous_text_never_spelled_out_is_kept(self):
        from kiro_crew.dashboard.slot_queue_repository import prune_attachment_meta

        meta = {"images": ["/tmp/my shot.png"]}
        prune_attachment_meta(meta, "look", "look")
        assert meta == {"images": ["/tmp/my shot.png"]}

    def test_an_escaped_destination_the_user_removed_is_pruned(self):
        """The composer wraps a destination it had to escape (a space, a `%`, a
        `<`) in ``<...>``, so the bare path is not a substring of the line; the
        prune must still see the line go, or the model keeps a picture the user
        deleted."""
        from kiro_crew.dashboard.slot_queue_repository import prune_attachment_meta

        spaced = "/tmp/my shot.png"
        percent = "/tmp/100%.png"
        previous = f"![image]({markdown_image_dest(spaced)})\n![image]({markdown_image_dest(percent)})\n\nlook"
        assert spaced not in markdown_image_dest(percent)
        meta = {"images": [spaced, percent]}
        prune_attachment_meta(meta, f"![image]({markdown_image_dest(spaced)})\n\nlook", previous)
        assert meta == {"images": [spaced]}
        prune_attachment_meta(meta, "look", previous)
        assert meta == {}

    def test_with_added_images_appends_new_pictures_once(self):
        from kiro_crew.dashboard.slot_queue_repository import with_added_images

        kept = {"images": ["/tmp/a.png"]}
        assert with_added_images(kept, ["/tmp/b.png", "/tmp/a.png"]) == {
            "images": ["/tmp/a.png", "/tmp/b.png"]
        }
        assert with_added_images({}, ["/tmp/b.png"]) == {"images": ["/tmp/b.png"]}
        assert with_added_images({}, []) == {}

    def test_a_removed_image_is_pruned_even_when_a_longer_path_stays(self):
        """``/tmp/a.png.bak`` left in the text is not ``/tmp/a.png``: the
        prune must see the picture's own line go, not keep it alive through a
        longer path that starts the same way."""
        from kiro_crew.dashboard.slot_queue_repository import prune_attachment_meta

        meta = {"images": ["/tmp/a.png"]}
        previous = "![image](/tmp/a.png)\n\ncompare with /tmp/a.png.bak"
        prune_attachment_meta(meta, "compare with /tmp/a.png.bak", previous)
        assert meta == {}

    def test_retained_image_meta_reapplies_the_shared_bounds(self):
        """A persisted row is a writable file and the union of two accepted
        lists can exceed one: the count and path-length bounds the send path
        applies (``attachment_meta``) are re-applied where the list is read
        back, refusing the list whole as the send path does."""
        from kiro_crew.dashboard.slot_queue_repository import (
            ATTACHMENT_LIST_MAX_ITEMS,
            ATTACHMENT_PATH_MAX_LEN,
            retained_image_meta,
            with_added_images,
        )

        over = [f"/tmp/f{i}.png" for i in range(ATTACHMENT_LIST_MAX_ITEMS + 1)]
        assert retained_image_meta({"images": over}, "x", "x") == {}
        at_count = over[:-1]
        assert retained_image_meta({"images": at_count}, "x", "x") == {"images": at_count}
        too_long = "/" + "x" * ATTACHMENT_PATH_MAX_LEN + ".png"
        assert retained_image_meta({"images": [too_long]}, "x", "x") == {}
        assert retained_image_meta({"images": ["/tmp/a.png", ""]}, "x", "x") == {}

        kept = {"images": at_count}
        assert with_added_images(kept, ["/tmp/one-more.png"]) is None
        assert with_added_images(kept, [at_count[0]]) == {"images": at_count}
        assert with_added_images({"images": ["/tmp/a.png"]}, [too_long]) is None

    def test_retained_image_meta_for_a_re_run(self):
        from kiro_crew.dashboard.slot_queue_repository import retained_image_meta

        row_meta = {"files": ["/tmp/a.pdf"], "images": ["/tmp/a.png", "/tmp/b.png"]}
        previous = "![image](/tmp/a.png)\n![image](/tmp/b.png)\n\nlook"
        # A regenerate re-runs the same text: every image is kept, files stay
        # with the row.
        assert retained_image_meta(row_meta, previous, previous) == {
            "images": ["/tmp/a.png", "/tmp/b.png"]
        }
        # An edit that removed one line keeps the other.
        assert retained_image_meta(row_meta, "![image](/tmp/b.png)\n\nlook", previous) == {
            "images": ["/tmp/b.png"]
        }
        assert retained_image_meta({"files": ["/tmp/a.pdf"]}, "x", "x") == {}
        assert retained_image_meta(None, "x", "x") == {}

    def test_the_runner_derives_its_list_from_meta_images_only(self):
        from kiro_crew.dashboard.chat_runner import _turn_prompt_attachments

        atts = _turn_prompt_attachments({"files": ["/tmp/a.pdf"], "images": ["/tmp/a.png"]})
        assert [a.path for a in atts] == ["/tmp/a.png"]
        assert _turn_prompt_attachments({"files": ["/tmp/a.pdf"]}) == ()
        assert _turn_prompt_attachments(None) == ()

    def test_a_credential_shaped_filename_reaches_the_provider_unredacted(self):
        """The upload writer keeps the caller's filename in the path, and the
        redactor rewrites anything credential-shaped in it. The persisted and
        client copies must be redacted; the ONE copy the provider opens must
        not be, or the builder probes a path that exists nowhere and the
        user's own upload is silently dropped."""
        from kiro_crew.dashboard.chat_delivery import attachment_meta, prompt_image_paths
        from kiro_crew.dashboard.chat_runner import _turn_prompt_attachments

        raw = "/tmp/uploads/ab12_ghp_" + "A" * 36 + ".png"
        user_meta = {"images": [raw], "files": ["/tmp/uploads/report.pdf"]}
        redacted = attachment_meta(user_meta)
        assert redacted["images"] != [raw] and "ghp_" not in redacted["images"][0]
        assert prompt_image_paths(user_meta) == [raw]
        # The provider copy wins; the redacted list is the fallback for a
        # dispatch that has no raw copy (an entry restored from disk).
        assert [a.path for a in _turn_prompt_attachments(redacted, [raw])] == [raw]
        assert [a.path for a in _turn_prompt_attachments(redacted)] == redacted["images"]
        # Same bounds as the redacted copy: refused whole, not sliced.
        assert prompt_image_paths({"images": ["/tmp/a.png", 7]}) == []
        assert prompt_image_paths(None) == []

    @pytest.mark.parametrize(
        "case", ["match", "wrapped", "escaped", "missing", "ambiguous", "repeat"]
    )
    def test_redacted_project_image_requires_one_matching_destination(
        self, tmp_path, monkeypatch, case
    ):
        from kiro_crew.dashboard.chat_delivery import attachment_meta, resolve_image_paths
        from kiro_crew.dashboard.chat_runner import _turn_prompt_attachments

        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", tmp_path)
        prefix = "diagram 100%" if case == "escaped" else "diagram"
        raw = (tmp_path / f"{prefix}_ghp_{'A' * 36}.png").as_posix()
        other = (tmp_path / f"{prefix}_ghp_{'B' * 36}.png").as_posix()
        meta = attachment_meta({"images": [raw]})
        assert meta["images"] != [raw]
        assert attachment_meta({"images": [other]}) == meta
        image = f"![image]({raw})"
        text = {
            "match": image,
            "wrapped": f"![image](<{raw}>)",
            "escaped": f"![image]({markdown_image_dest(raw)})",
            "missing": f"not attached: {raw}",
            "ambiguous": f"{image}\n![image]({other})",
            "repeat": f"{image}\n{image}",
        }[case]
        expected = meta["images"] if case in ("missing", "ambiguous") else [raw]
        assert resolve_image_paths(meta["images"], text=text) == expected
        assert [a.path for a in _turn_prompt_attachments(meta, text=text)] == expected
        assert _turn_prompt_attachments(None, text=text) == ()

    def test_a_redacted_upload_spelling_resolves_to_the_file_the_server_minted(
        self, tmp_path, monkeypatch
    ):
        """Every rebuild path (regenerate, edit-resend, rewind, a restored
        entry, a marker-restored row) only has the PERSISTED copy, whose path
        the redactor rewrote. The upload writer's own ``<uuid>_`` key survives
        that rewrite, so the one server-side resolver maps the spelling back to
        the real file -- inside the server's upload directory only."""
        from kiro_crew.dashboard.chat_delivery import attachment_meta, resolve_image_paths
        from kiro_crew.dashboard.chat_runner import _turn_prompt_attachments

        uploads = tmp_path / "uploads"
        uploads.mkdir()
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", uploads)
        key = "0123456789abcdef0123456789abcdef"
        real = uploads / f"{key}_ghp_{'A' * 36}.png"
        real.write_bytes(_PNG)
        redacted = attachment_meta({"images": [str(real)]})["images"][0]
        assert redacted != str(real) and "[REDACTED" in redacted and key in redacted

        assert resolve_image_paths([redacted]) == [str(real)]
        assert [a.path for a in _turn_prompt_attachments({"images": [redacted]})] == [str(real)]
        other = str(uploads / f"{key}_ghp_{'B' * 36}.png")
        assert attachment_meta({"images": [other]})["images"] == [redacted]
        assert resolve_image_paths([redacted], text=f"![image]({other})") == [str(real)]
        # An unredacted path, and one the resolver cannot place, pass through.
        assert resolve_image_paths([str(real), "/tmp/plain.png"]) == [str(real), "/tmp/plain.png"]
        elsewhere = str(tmp_path / "other" / f"{key}_[REDACTED: credential].png")
        assert resolve_image_paths([elsewhere]) == [elsewhere]
        # Two files under one key is ambiguous: kept as written, never guessed.
        (uploads / f"{key}_ghp_{'B' * 36}.png").write_bytes(_PNG)
        assert resolve_image_paths([redacted]) == [redacted]

    def test_upload_index_keeps_only_requested_keys(self, tmp_path):
        from kiro_crew.dashboard.chat_delivery import _upload_files_by_key

        keys = {"a" * 32, "b" * 32}
        expected = {key: [str(_png(tmp_path, f"{key}_wanted.png"))] for key in keys}
        for index in range(100):
            _png(tmp_path, f"{index:032x}_unrelated.png")
        (tmp_path / f"{'a' * 32}_directory.png").mkdir()
        _png(tmp_path, "unkeyed.png")

        assert _upload_files_by_key(tmp_path, keys | {"f" * 32}) == expected
        assert _upload_files_by_key(tmp_path, set()) == {}

    def test_upload_index_never_follows_a_leaf_link(self, tmp_path):
        """A link planted under a valid upload key must not be indexed, and the
        index must not stat its target: on Windows that target can be a
        network share, and the stat alone starts an outbound authentication
        before any image reader's link guard runs."""
        import os

        from kiro_crew.dashboard import chat_delivery
        from kiro_crew.dashboard.chat_delivery import _upload_files_by_key

        key = "a" * 32
        real = _png(tmp_path, f"{key}_wanted.png")
        target = tmp_path / "elsewhere.png"
        target.write_bytes(_PNG)
        try:
            (tmp_path / f"{key}_planted.png").symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available here")

        followed: list[str] = []
        real_scandir = os.scandir

        class _Entry:
            def __init__(self, entry):
                self._entry = entry
                self.name = entry.name
                self.path = entry.path

            def is_file(self, *, follow_symlinks=True):
                if follow_symlinks and self._entry.is_symlink():
                    followed.append(self.name)
                return self._entry.is_file(follow_symlinks=follow_symlinks)

        class _Scan:
            def __init__(self, path):
                self._it = real_scandir(path)

            def __enter__(self):
                return (_Entry(e) for e in self._it)

            def __exit__(self, *exc):
                self._it.close()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(chat_delivery.os, "scandir", _Scan)
            indexed = _upload_files_by_key(tmp_path, {key})
        assert indexed == {key: [str(real)]}
        assert followed == []

    def test_upload_index_retains_two_matches_and_leaves_ambiguity_unresolved(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_delivery import (
            _upload_files_by_key,
            attachment_meta,
            resolve_image_paths,
        )

        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", tmp_path)
        key = "a" * 32
        paths = [str(_png(tmp_path, f"{key}_ghp_{letter * 36}.png")) for letter in "ABC"]
        redacted = attachment_meta({"images": paths[:1]})["images"][0]

        indexed = _upload_files_by_key(tmp_path, {key})
        assert set(indexed) == {key}
        assert len(indexed[key]) == 2
        assert len(set(indexed[key])) == 2
        assert set(indexed[key]) <= set(paths)
        assert resolve_image_paths([redacted]) == [redacted]

    def test_the_resolver_lists_the_upload_dir_once_per_call_and_only_when_needed(
        self, tmp_path, monkeypatch
    ):
        """The ordinary list (no redacted spelling) touches no file; a list
        with several redacted spellings costs ONE directory listing, shared by
        every path of the call and never kept across calls."""
        from kiro_crew.dashboard import chat_delivery

        uploads = tmp_path / "uploads"
        uploads.mkdir()
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", uploads)
        listings: list[tuple[str, set[str]]] = []
        _orig = chat_delivery._upload_files_by_key

        def _counting(upload_dir, requested_keys):
            listings.append((str(upload_dir), requested_keys))
            return _orig(upload_dir, requested_keys)

        monkeypatch.setattr(chat_delivery, "_upload_files_by_key", _counting)

        assert chat_delivery.resolve_image_paths(["/tmp/a.png", "/tmp/b.png"]) == [
            "/tmp/a.png",
            "/tmp/b.png",
        ]
        assert listings == []
        keys = ["0" * 32, "1" * 32, "2" * 32]
        reals = []
        for k in keys:
            f = uploads / f"{k}_ghp_{'A' * 36}.png"
            f.write_bytes(_PNG)
            reals.append(str(f))
        redacted = [str(uploads / f"{k}_[REDACTED: credential].png") for k in keys]
        plain = str(_png(uploads, f"{'3' * 32}_plain.png"))
        assert chat_delivery.resolve_image_paths(redacted + [plain]) == reals + [plain]
        assert listings == [(str(uploads), set(keys))]

    def test_the_queue_entry_carries_the_provider_copy_out_of_sight(self):
        """The raw list rides the entry under a process-local key: the drain
        reads it, the wire view and the durable projection never do."""
        from kiro_crew.dashboard.chat_delivery import queue_entry_view
        from kiro_crew.dashboard.slot_queue_repository import (
            PROMPT_IMAGES_ENTRY_KEY,
            durable_queue_entries,
        )
        from kiro_crew.dashboard.state import _ChatSlot

        raw = "/tmp/uploads/ab12_ghp_" + "A" * 36 + ".png"
        slot = _ChatSlot("chat-1")
        qid = slot.queue_append("look", meta={"images": [raw]}, prompt_images=[raw])
        entry = next(e for e in slot._queue if e["id"] == qid)
        assert entry[PROMPT_IMAGES_ENTRY_KEY] == [raw]
        view = queue_entry_view(entry)
        assert PROMPT_IMAGES_ENTRY_KEY not in view
        assert "ghp_" not in view["meta"]["images"][0]
        durable = durable_queue_entries(slot._queue)
        assert all(PROMPT_IMAGES_ENTRY_KEY not in d for d in durable)
        # An insert (the steer fall-through, the refusal replay) stamps it too.
        qid2 = slot.queue_insert(0, "again", meta={"images": [raw]}, prompt_images=[raw])
        assert next(e for e in slot._queue if e["id"] == qid2)[PROMPT_IMAGES_ENTRY_KEY] == [raw]


class TestIngestResultSeam:
    def test_prompt_attachments_lists_only_the_images(self, tmp_path):
        from kiro_crew.messaging.attachments import IngestResult

        result = IngestResult(
            image_paths=["/tmp/a.png", "/tmp/b.jpg"],
            file_paths=["/tmp/opaque.bin"],
            audio_paths=["/tmp/memo.ogg"],
        )
        atts = result.prompt_attachments()
        assert [a.path for a in atts] == ["/tmp/a.png", "/tmp/b.jpg"]
        assert IngestResult().prompt_attachments() == ()


class TestEveryProducerHandsTheListOn:
    """Structural pin over the channel dispatchers.

    A channel that ingests attachments must hand the turn its structured list:
    through ``ChannelTurn(attachments=...)`` on the shared pipeline, or straight
    into ``driver.run(..., attachments=...)`` where the channel drives its own
    ``TurnDriver``. A dispatcher that dropped the keyword would still run every
    turn -- and silently ship no picture -- so the omission is pinned here.
    """

    _SHARED = ("teams", "webex", "wecom", "weixin", "whatsapp")
    _DIRECT = ("discord", "telegram")

    @staticmethod
    def _calls(module_path, func_name):
        import ast
        from pathlib import Path

        import kiro_crew

        root = Path(kiro_crew.__file__).resolve().parent
        tree = ast.parse((root / module_path).read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name == func_name:
                out.append(node)
        return out

    @pytest.mark.parametrize("channel", _SHARED)
    def test_shared_pipeline_channels_pass_attachments_to_channel_turn(self, channel):
        calls = self._calls(f"{channel}/transport_dispatch.py", "ChannelTurn")
        assert calls, f"{channel}: no ChannelTurn construction found"
        for call in calls:
            assert "attachments" in {kw.arg for kw in call.keywords}, (
                f"{channel}: a ChannelTurn is built without attachments= -- the "
                "ingested images would never reach the model"
            )

    @pytest.mark.parametrize("channel", _DIRECT)
    def test_direct_driver_channels_pass_attachments_to_run(self, channel):
        calls = self._calls(f"{channel}/transport_dispatch.py", "run")
        with_kw = [c for c in calls if "attachments" in {kw.arg for kw in c.keywords}]
        assert with_kw, f"{channel}: no driver.run(..., attachments=...) site found"

    def test_the_slack_routes_pass_attachments(self):
        legacy = self._calls("slack/handler.py", "stream")
        assert any("attachments" in {kw.arg for kw in c.keywords} for c in legacy)
        transport = self._calls("slack/transport_dispatch.py", "run")
        assert any("attachments" in {kw.arg for kw in c.keywords} for c in transport)
        for name in ("handle_message", "handle_message_transport"):
            routes = self._calls("slack/events.py", name)
            assert routes, f"events.py: no {name} call found"
            assert all(
                "attachments" in {kw.arg for kw in c.keywords} for c in routes
            ), f"events.py: a {name} dispatch omits attachments="
