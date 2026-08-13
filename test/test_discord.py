"""Unit tests for the Discord channel on the messaging-transport abstraction.

Covers: command parsing (commands.py), text chunking + [OPTIONS:] extraction +
button components (renderer.py), deny-by-default auth + DM-only guard +
capabilities + inbound normalization (transport.py), streaming render +
finalization (renderer.py), the interactive approval decider, and the dispatch
turn + interaction routing (transport_dispatch.py). Mirrors test_telegram.py.
"""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
)
from kiro_crew.discord.attachments import process_discord_attachments
from kiro_crew.discord.client import (
    _INTENT_DIRECT_MESSAGES,
    _INTENT_GUILD_MESSAGES,
    _INTENT_MESSAGE_CONTENT,
    DISCORD_CHUNK_LIMIT,
    DiscordClient,
    DiscordInbound,
    DiscordInteraction,
    _find_button_label,
)
from kiro_crew.discord.commands import (
    parse_command,
    parse_mid_turn_override,
)
from kiro_crew.discord.renderer import (
    DiscordApprovalDecider,
    DiscordRenderer,
    _ends_inside_fence,
    _extract_options,
    _split_markdown,
    _split_text,
    _strip_steering,
    build_option_components,
)
from kiro_crew.discord.transport import (
    DISCORD_CAPABILITIES,
    DiscordInboundMessage,
    DiscordTransport,
)
from kiro_crew.discord.transport_dispatch import (
    _STEER_ACK_EMOJI,
    DiscordDispatcher,
)
from kiro_crew.messaging.attachments import cleanup
from kiro_crew.messaging.link import (
    UNBIND_REASON_UNSPECIFIED,
    ChannelLink,
    legacy_dashboard_mirror_key,
)
from kiro_crew.messaging.queue_receipt import receipt_text as _receipt_text
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.session import _opt_out_key
from kiro_crew.session_map import ConversationOwnershipConflict

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

# ── Fakes ──────────────────────────────────────────────────────────────────


class FakeClient:
    """Captures outbound Discord REST calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.edits: list[tuple[str, str, Any]] = []
        self.component_edits: list[tuple[str, Any]] = []
        self.acked: list[str] = []
        self.reactions: list[tuple[str, str]] = []
        self.thread_channels: set[str] = set()
        self.created_threads: list[tuple[str, str, str]] = []
        self.attachment_bodies: dict[str, bytes] = {}
        self.attachment_downloads: list[str] = []
        self._mid = 100

    async def is_thread_channel(self, channel_id: str) -> bool:
        return channel_id in self.thread_channels

    async def send_typing(self, channel_id: str) -> None:
        return None

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        components: Any = None,
        reply_to_message_id: Any = None,
    ) -> str:
        await asyncio.sleep(0)  # yield like a real network await (exposes races)
        self._mid += 1
        self.sent.append((text, components))
        return str(self._mid)

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        text: str,
        *,
        components: Any = None,
    ) -> bool:
        self.edits.append((message_id, text, components))
        return True

    async def edit_message_components(
        self, channel_id: str, message_id: str, components: Any
    ) -> bool:
        self.component_edits.append((message_id, components))
        return True

    async def ack_component_interaction(self, interaction_id: str, interaction_token: str) -> None:
        self.acked.append(interaction_id)

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((message_id, emoji))

    async def create_dm_channel(self, user_id: str) -> str:
        return f"dm-{user_id}"

    async def create_thread_from_message(
        self, channel_id: str, message_id: str, name: str
    ) -> str:
        thread_id = f"thread-{message_id}"
        self.created_threads.append((channel_id, message_id, name))
        self.thread_channels.add(thread_id)
        return thread_id

    async def download_attachment(self, url: str, dest: str) -> None:
        self.attachment_downloads.append(url)
        with open(dest, "wb") as fh:
            fh.write(self.attachment_bodies[url])

    def final_text(self) -> Any:
        """Text the user ultimately sees on the live message: the last edit if
        it was edited (edit-streaming), else the last send."""
        if self.edits:
            return self.edits[-1][1]
        return self.sent[-1][0] if self.sent else None

    def final_components(self) -> Any:
        if self.edits:
            return self.edits[-1][2]
        return self.sent[-1][1] if self.sent else None


class _Ev:
    def __init__(self, kind: str, text: str = "", stop_reason: str = "", title: str = "") -> None:
        self.kind = kind
        self.text = text
        self.stop_reason = stop_reason
        self.tool_call_id = ""
        self.title = title
        self.context_usage_pct = 0.0


class FakeProvider:
    supports_steer = True

    def __init__(self, reply: str = "Answer") -> None:
        self._reply = reply
        self.steered: list = []
        self.cancelled = 0
        self.active_turn = True

    def has_active_turn(self) -> bool:
        return self.active_turn

    async def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> str:
        self.cancelled += 1
        return "acked"

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_TEXT_CHUNK, text=f"{self._reply}: {message[:16]}")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

    async def stream_command(self, command: str) -> Any:
        yield _Ev(EVENT_COMPACTION_STATUS, text="completed", title="ok")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

    async def compact(self, context: str = "") -> None:
        return None

    async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
        return {"type": "completed", "summary": "ok"}

    async def approve_tool(self, request_id: Any) -> None:
        return None

    async def reject_tool(self, request_id: Any) -> None:
        return None


class FakeSessions:
    async def aflush(self) -> None:  # in-memory double: already durable
        pass

    def __init__(self, raise_on_get: bool = False) -> None:
        self.released: list[str] = []
        self.acquired: list[str] = []
        self.destroyed: list[str] = []
        self.discarded: list[str] = []
        self.successes: list[str] = []
        self.failures: list[str] = []
        self.last_agent: Any = None
        self.raise_on_get = raise_on_get
        self._busy = False
        self._has = True
        self.queued: list = []
        self._gp = FakeProvider()
        self.mirror_links: dict[str, Any] = {}
        self.origin_links: dict[str, Any] = {}
        self.inbound_mirror_keys: set[str] = set()
        self.mirror_opt_outs: set[str] = set()
        # Batch bookkeeping, mirroring the real SessionManager: the unlink path
        # wraps its three clears in one batch, and a double without the context
        # manager would make that path unreachable from these tests. Each entry is
        # True when that mirror mutation ran inside a batch, so a test can pin
        # that one user-visible action costs one whole-map write.
        self.batch_depth = 0
        self.batched_writes: list[bool] = []
        # Interface parity with the real SessionManager: the dispatcher's
        # disconnect gate consults this. Entries are ``(session_key, origin)``.
        # Extended here rather than relying on the gate's fail-open, so a test
        # about the gate exercises the gate instead of its fallback.
        self.paused_deliveries: set[tuple[str, bool]] = set()

    def is_mirror_paused(self, key: str, *, origin: bool = False) -> bool:
        return (key, origin) in self.paused_deliveries

    async def get_or_create(self, key: str, *, agent: Any = None, channel_id: Any = None) -> Any:
        self.last_agent = agent
        if self.raise_on_get:
            raise RuntimeError("cold-start failed")
        return FakeProvider(), True, False

    async def set_channel(self, key: str, channel: str) -> None:
        return None

    def record_success(self, key: str) -> None:
        self.successes.append(key)

    async def record_failure(self, key: str) -> None:
        self.failures.append(key)

    def check_context_usage(self, key: str, provider: Any) -> float:
        return 10.0

    def release(self, key: str) -> None:
        self.released.append(key)

    def get_provider(self, key: str) -> Any:
        return self._gp

    def is_busy(self, key: str) -> bool:
        return self._busy

    def max_generation(self, bucket: str) -> int:
        return -1

    def set_mirror_link(
        self,
        key: str,
        link: Any,
        *,
        accepts_inbound: bool = False,
        reason: str = UNBIND_REASON_UNSPECIFIED,
    ) -> None:
        # Interface parity with the real SessionMap: a conversation is exclusive
        # once it is inbound-committed — this claim is inbound-capable, or an
        # occupant already is. Two outbound-only mirrors stay allowed. A fake that
        # accepts what production refuses lets a test go green against a state the
        # product cannot reach.
        rivals = [
            other for other, held in self.mirror_links.items() if other != key and held == link
        ]
        if rivals and (
            accepts_inbound or any(other in self.inbound_mirror_keys for other in rivals)
        ):
            raise ConversationOwnershipConflict(
                f"{getattr(link, 'channel_type', '?')} conversation is already held"
            )
        self.batched_writes.append(self.batch_depth > 0)
        self.mirror_links[key] = link
        if accepts_inbound:
            self.inbound_mirror_keys.add(key)
        else:
            self.inbound_mirror_keys.discard(key)

    @contextmanager
    def batched_save(self) -> Any:
        self.batch_depth += 1
        try:
            yield
        finally:
            self.batch_depth -= 1

    def set_mirror_opt_out(self, key: str, opted_out: bool) -> None:
        # Bucket-keyed, like the real manager: the refusal is a preference about
        # the CONVERSATION, so it must outlive a generation rotation.
        self.batched_writes.append(self.batch_depth > 0)
        if opted_out:
            self.mirror_opt_outs.add(_opt_out_key(key))
        else:
            self.mirror_opt_outs.discard(_opt_out_key(key))

    def mirror_opt_out(self, key: str) -> bool:
        return _opt_out_key(key) in self.mirror_opt_outs

    def get_mirror_link(self, key: str) -> Any:
        return self.mirror_links.get(key)

    def set_origin_link(self, key: str, link: Any) -> None:
        self.origin_links[key] = link

    def get_origin_link(self, key: str) -> Any:
        return self.origin_links.get(key)

    def find_mirror_sessions(self, link: Any, *, inbound_only: bool = False) -> list[str]:
        return [
            key
            for key, candidate in self.mirror_links.items()
            if candidate == link and (not inbound_only or key in self.inbound_mirror_keys)
        ]

    def clear_mirror_link(self, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED) -> bool:
        self.batched_writes.append(self.batch_depth > 0)
        self.inbound_mirror_keys.discard(key)
        self.batched_writes.append(self.batch_depth > 0)
        return self.mirror_links.pop(key, None) is not None

    def clear_mirror_links_at(
        self, link: Any, *, reason: str = UNBIND_REASON_UNSPECIFIED
    ) -> list[str]:
        self.batched_writes.append(self.batch_depth > 0)
        cleared = self.find_mirror_sessions(link)
        for key in cleared:
            self.inbound_mirror_keys.discard(key)
            self.mirror_links.pop(key, None)
        return cleared

    def enqueue(self, key: str, ts: str, text: str, *, force: bool = False, **kw: Any) -> bool:
        if force or self._busy:
            self.queued.append((ts, text, kw))
            return True
        return False

    def dequeue(self, key: str) -> Any:
        return self.queued.pop(0) if self.queued else None

    def clear_queue(self, key: str) -> None:
        self.queued.clear()

    def has_session(self, key: str) -> bool:
        return self._has

    async def try_acquire(self, key: str) -> bool:
        if self._busy or not self._has:
            return False
        self.acquired.append(key)
        return True

    async def destroy(self, key: str) -> None:
        self.destroyed.append(key)

    async def discard_conversation(self, key: str) -> None:
        self.discarded.append(key)


class _FakeHooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, *a: Any, **k: Any) -> Any:
        return SimpleNamespace(action="allow")


class FakeCtx:
    def __init__(self) -> None:
        self.hooks = _FakeHooks()
        self.messages: list[str] = []

    def build_message(self, text: str, is_new: bool, key: str, **kw: Any) -> Any:
        self.messages.append(text)
        return text, None


def _cfg(soft: int = 80, default_agent: str = "", dm_scope: str = "per-channel-peer") -> Any:
    return SimpleNamespace(
        discord=SimpleNamespace(soft_threshold_pct=soft),
        agent=SimpleNamespace(default_agent=default_agent),
        messaging=SimpleNamespace(
            dm_scope=dm_scope,
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
    )


def _dispatcher(
    allowed: set[str],
    *,
    allowed_threads: set[str] | None = None,
    raise_on_get: bool = False,
    default_agent: str = "",
    dm_scope: str = "per-channel-peer",
) -> tuple[DiscordDispatcher, FakeClient, FakeSessions]:
    sess = FakeSessions(raise_on_get=raise_on_get)
    d = DiscordDispatcher(
        sessions=sess,  # type: ignore[arg-type]
        ctx_builder=FakeCtx(),  # type: ignore[arg-type]
        cfg=_cfg(default_agent=default_agent, dm_scope=dm_scope),
        allowed_user_ids=allowed,
        allowed_thread_ids=allowed_threads,
        agent=None,
        conv_log=None,
    )
    cli = FakeClient()
    d.client = cli  # type: ignore[assignment]
    return d, cli, sess


# ── commands.py ──────────────────────────────────────────────────────────


class TestParseCommand:
    def test_new_aliases(self) -> None:
        assert parse_command("!new") == "new"
        assert parse_command("!start") == "new"
        assert parse_command("/new") == "new"  # Telegram muscle memory

    def test_compact(self) -> None:
        assert parse_command("!compact") == "compact"
        assert parse_command("/compact") == "compact"

    def test_stop_aliases(self) -> None:
        assert parse_command("!stop") == "stop"
        assert parse_command("!cancel") == "stop"

    def test_link_unlink_help(self) -> None:
        assert parse_command("!link") == "link"
        assert parse_command("!unlink") == "unlink"
        assert parse_command("!help") == "help"

    def test_case_and_whitespace(self) -> None:
        assert parse_command("  !NEW  ") == "new"

    def test_plain_text_is_not_a_command(self) -> None:
        assert parse_command("hello there") is None
        assert parse_command("!unknown") is None
        assert parse_command("") is None

    def test_command_with_trailing_words_still_matches(self) -> None:
        assert parse_command("!new please") == "new"


class TestMidTurnOverride:
    def test_queue_override(self) -> None:
        assert parse_mid_turn_override("!queue do it later") == (
            "queue",
            "do it later",
        )

    def test_steer_override(self) -> None:
        assert parse_mid_turn_override("!steer focus on X") == (
            "steer",
            "focus on X",
        )

    def test_slash_aliases(self) -> None:
        assert parse_mid_turn_override("/steer now") == ("steer", "now")

    def test_bare_directive_is_content(self) -> None:
        assert parse_mid_turn_override("!queue") == (None, "!queue")

    def test_plain_text_passthrough(self) -> None:
        assert parse_mid_turn_override("hello") == (None, "hello")


# ── renderer.py helpers ──────────────────────────────────────────────────

_ORACLE_CODE = "x = 1\n"
_ORACLE_PROSE = "Ordinary prose about how chat surfaces render markdown.\n"

#: Fence shapes swept by BOTH renderer oracles -- the strip/append symmetry one
#: and the whitespace-fidelity one. Shared so neither can drift onto a corpus the
#: other never sees. They cover the information a per-chunk fence walk cannot
#: recover from the source: 3/4/5-backtick openers, authored inner bare ``` lines,
#: literal backticks in prose, 4-space-indented lookalikes, info strings, and a
#: run of blank lines at the tail.
_FENCE_SHAPES = [
    "```py\n" + _ORACLE_CODE * 40,  # open 3-backtick fence
    "```py\n" + _ORACLE_CODE * 40 + "```\n",  # closed 3-backtick fence
    "````md\n" + _ORACLE_CODE * 40,  # open 4-backtick fence
    "````md\n" + ("```py\n" + _ORACLE_CODE + "```\n") * 25,  # inner bare closers
    "````md\n" + ("```py\n" + _ORACLE_CODE + "```\n") * 25 + "````\n",  # …then closed
    "`````\n" + ("```\n" + _ORACLE_CODE + "```\n") * 25,  # 5-backtick outer
    _ORACLE_PROSE * 12,  # no fence at all
    "You type ``` to open a block.\n" + _ORACLE_PROSE * 12,  # literal in prose
    "You type ``` inline.\n\n```py\n" + _ORACLE_CODE * 30,  # literal, then open
    _ORACLE_PROSE * 6 + "    ```\n" + _ORACLE_CODE * 20,  # indented lookalike
    "   ```py\n" + _ORACLE_CODE * 40,  # 3-space indent still opens
    "```a`b\n" + _ORACLE_CODE * 40,  # backtick in info string == inline code
    "```py\n" + _ORACLE_CODE * 20 + "```\n\n```sh\nls\n" + _ORACLE_CODE * 20,  # two fences
    "````md\n" + _ORACLE_CODE * 20 + "```\n" + _ORACLE_CODE * 20 + "\n\n\n",  # ws tail
    # Blank code lines INSIDE a fence with more code after them -- the shape the
    # remainder used to delete, swept at every limit so the cut lands on each
    # newline of the run in turn.
    "```py\n" + _ORACLE_CODE * 20 + "\n\n" + _ORACLE_CODE * 20,
    "```py\n" + (_ORACLE_CODE + "\n\n\n") * 12,  # 4-newline runs throughout
]


class TestSplitText:
    def test_short_text_single_chunk(self) -> None:
        assert _split_text("hello", 100) == ["hello"]

    def test_empty_text(self) -> None:
        assert _split_text("", 100) == []

    def test_splits_at_paragraph_boundary(self) -> None:
        text = "para one\n\npara two\n\npara three"
        chunks = _split_text(text, 20)
        assert all(len(c) <= 20 for c in chunks)
        assert "".join(c.replace("\n", "") for c in chunks)  # nothing lost

    def test_split_markdown_balances_fences(self) -> None:
        code = "```py\n" + ("x = 1\n" * 50) + "```"
        chunks, appended = _split_markdown(code, 120)
        assert len(chunks) > 1
        for ch in chunks:
            assert ch.count("```") % 2 == 0  # every chunk self-contained
        # The source's own closer lands in the final chunk, so nothing was
        # invented there.
        assert appended is False

    def test_split_markdown_reports_its_own_synthetic_closer(self) -> None:
        # The flag is the splitter's record of what it appended to the FINAL
        # chunk -- the only sound basis for the rotation's strip, since the
        # per-chunk walk runs after a bare ``` reopen has already discarded the
        # original opener's run length.
        open_fence = "```py\n" + ("x = 1\n" * 50)
        chunks, appended = _split_markdown(open_fence, 120)
        assert len(chunks) > 1
        assert appended is True
        assert chunks[-1].endswith("\n```")
        # A lone chunk is handed back untouched, so the flag alone expresses the
        # "nothing to undo" case -- no len(chunks) guard is needed at the strip.
        for text in ["```py\nx = 1\n", "type ``` here", "", "plain prose"]:
            lone, lone_appended = _split_markdown(text, 1900)
            assert len(lone) <= 1
            assert lone_appended is False
            assert lone == _split_text(text, 1900)

    def test_inline_backticks_are_not_a_fence(self) -> None:
        # A ``` written MID-LINE is prose about fencing, not a fence. Counting
        # ``` substrings calls this "open" and flips every later fence decision.
        assert not _ends_inside_fence("Type ``` to open a block.")
        assert not _ends_inside_fence("Prose\nsay ``` here\nmore prose")
        # ...and it must not mask a fence that really is open.
        assert _ends_inside_fence("Type ``` first.\n\n```py\nx = 1\n")

    def test_fence_state_walk_follows_commonmark(self) -> None:
        assert _ends_inside_fence("```py\nx = 1\n")
        assert not _ends_inside_fence("```py\nx = 1\n```")
        assert not _ends_inside_fence("```py\nx = 1\n```\ntrailing prose")
        # Up to 3 spaces of indent still opens; 4+ is an indented code line.
        assert _ends_inside_fence("   ```\nx\n")
        assert not _ends_inside_fence("    ```\nx\n")
        # A closer may be LONGER than its opener but never shorter, and carries
        # nothing after it -- an info string is legal only on the opener.
        assert not _ends_inside_fence("```\nx\n`````")
        assert _ends_inside_fence("`````\nx\n```")
        assert _ends_inside_fence("```\nx\n``` not a closer")
        # An info string containing a backtick is inline code, not a fence.
        assert not _ends_inside_fence("```a`b\nx\n")

    def test_continuation_preserves_leading_indentation(self) -> None:
        # A cut landing just before an INDENTED line must not re-indent it: only
        # newlines are stripped at the boundary, never horizontal whitespace.
        text = "a" * 30 + "\n" + "    indented = 1\n" + "b" * 30
        chunks = _split_text(text, 40)
        assert len(chunks) > 1
        assert chunks[1].startswith("    indented = 1")

    def test_newline_only_remainder_keeps_blank_code_lines(self) -> None:
        # Blank lines are CONTENT inside a fenced block. A boundary that leaves a
        # remainder of nothing but newlines must not drop them, or the blank code
        # lines vanish and every later code line shifts up one row.
        text = "```py\n" + "x = 1\n" * 299 + "\n\n"
        chunks = _split_text(text, 1800)
        # The tail is its own chunk -- it neither vanished nor re-entered the
        # loop unchanged. Its first newline terminates the last code line, so it
        # is the separator this boundary represents and only it is absorbed; the
        # two authored blank lines are what remain.
        assert chunks[-1] == "\n\n"
        # Exact accounting, replacing a raw total-newline count: that count came
        # out equal only because the absorbed separator was being counted as
        # content, which is one blank line more than the source has. Rejoining on
        # the separator each cut consumed reproduces the source verbatim, so no
        # newline is lost and none is invented.
        assert "\n".join(chunks) == text

    def test_all_newline_text_terminates(self) -> None:
        # A zero-width cut on an all-newline tail must not re-enter the loop
        # unchanged. Watchdogged so a regression fails instead of hanging.
        done: list[list[str]] = []
        t = threading.Thread(target=lambda: done.append(_split_text("\n\n", 1)), daemon=True)
        t.start()
        t.join(5.0)
        assert done, "_split_text did not terminate on an all-newline tail"

    def test_blank_code_lines_straddling_a_cut_survive(self) -> None:
        # A blank line FOLLOWED BY CONTENT is an authored code line, not the
        # boundary separator. The cut lands on the run, so lstrip("\n") deleted
        # every newline in it at once and the next code line shifted up two rows.
        text = "```py\nx = 1\n\n\ny = 2\ny = 3\n"
        chunks = _split_text(text, 20)
        assert len(chunks) == 2
        assert chunks[0] == "```py\nx = 1"
        # BOTH authored blank lines lead the continuation.
        assert chunks[1] == "\n\ny = 2\ny = 3\n"
        # Exactly one newline -- the separator this boundary itself represents --
        # was absorbed, so rejoining on it reproduces the source verbatim.
        assert "\n".join(chunks) == text

    def test_prose_paragraph_gap_survives_a_cut(self) -> None:
        # The prose twin: the blank line of a paragraph gap is equally authored.
        text = "para one\n\npara two\n\npara three"
        chunks = _split_text(text, 20)
        assert chunks == ["para one\n\npara two", "\npara three"]
        assert "\n".join(chunks) == text

    def test_continuation_gains_no_leading_blank_line(self) -> None:
        # The opposite failure mode of the widened preservation: where the source
        # had a single separator and no blank line, the continuation must render
        # exactly as it does today -- content first, no invented gap.
        text = "a" * 30 + "\n" + "b" * 30
        assert _split_text(text, 40) == ["a" * 30, "b" * 30]

    def test_swept_cut_absorbs_at_most_one_newline(self) -> None:
        """Whitespace-fidelity oracle over the same corpus as the strip sweep.

        That sweep asserts only that non-whitespace survives, so it is blind to
        blank lines -- exactly the class ``lstrip("\\n")`` deleted. Every chunk is
        a verbatim slice of the source, so walking the slices in order exposes
        what the splitter dropped at each cut: it must be whitespace only, and it
        must be at most the single line separator the message boundary itself
        represents. That bounds newline-run loss inside fences and in prose alike
        at oracle strength, and it fails on ``lstrip("\\n")``, which drops the
        whole run.
        """
        cuts = 0
        for src in _FENCE_SHAPES:
            for limit in list(range(40, 201, 7)) + [1900]:
                chunks = _split_text(src, limit)
                cuts += len(chunks) - 1
                pos = 0
                for ch in chunks:
                    where = f"shape={src[:14]!r} limit={limit}"
                    at = src.find(ch, pos)
                    assert at >= 0, f"chunk is not a source slice: {where}"
                    gap = src[pos:at]
                    assert not gap.strip(), f"authored text dropped at a cut: {where}"
                    assert gap.count("\n") <= 1, (
                        f"cut absorbed {gap.count(chr(10))} newlines, "
                        f"blank lines deleted: {where}"
                    )
                    pos = at + len(ch)
                assert not src[pos:].strip(), f"authored tail dropped: {src[:14]!r}"
        assert cuts > 300, cuts  # the sweep is not vacuous


class TestOptionComponents:
    def test_empty_returns_none(self) -> None:
        assert build_option_components([]) is None

    def test_builds_rows_of_five(self) -> None:
        comps = build_option_components([f"opt{i}" for i in range(7)])
        assert comps is not None
        assert len(comps) == 2  # 5 + 2
        assert len(comps[0]["components"]) == 5
        assert len(comps[1]["components"]) == 2
        assert comps[0]["components"][0]["custom_id"] == "opt:0"

    def test_label_capped_at_80(self) -> None:
        comps = build_option_components(["x" * 200])
        assert comps is not None
        assert len(comps[0]["components"][0]["label"]) == 80

    def test_caps_at_25_options(self) -> None:
        comps = build_option_components([f"o{i}" for i in range(30)])
        assert comps is not None
        total = sum(len(r["components"]) for r in comps)
        assert total == 25


class TestExtractOptions:
    def test_no_options(self) -> None:
        assert _extract_options("plain body") == ("plain body", [])

    def test_extracts_trailing_options(self) -> None:
        body, opts = _extract_options("Pick one\n[OPTIONS: A | B | C]")
        assert body == "Pick one"
        assert opts == ["A", "B", "C"]

    def test_holds_back_streaming_partial(self) -> None:
        body, opts = _extract_options("Pick one\n[OPTIONS: A | B")
        assert body == "Pick one"
        assert opts == []

    def test_unterminated_options_tag_is_not_redos(self) -> None:
        # Regression (py/polynomial-redos): a plain greedy ``.*`` body could
        # consume a "[" that ALSO starts the outer "[OPTIONS:" literal, so over
        # text with many "[OPTIONS:" prefixes search() re-explored the body from
        # each position — polynomial. The tempered body
        # (?:[^[]|\[(?!OPTIONS:))* forbids only a re-occurring "[OPTIONS:", so
        # the body is unambiguous (linear). A whitespace-padded unterminated tag
        # and many repeated "[OPTIONS:" prefixes (the real pump) must both return
        # promptly.
        import time

        for evil in (
            "[OPTIONS:" + ("\t" * 200_000) + "x",
            "[OPTIONS:" * 100_000 + "x",
        ):
            start = time.perf_counter()
            body, opts = _extract_options(evil)
            elapsed = time.perf_counter() - start
            assert elapsed < 1.0, f"_extract_options took {elapsed:.2f}s (possible ReDoS)"
            assert opts == []


class TestStripSteering:
    def test_removes_complete_marker(self) -> None:
        assert _strip_steering("before [STEERING steer-ab12: do X] after") == (
            "before  after".replace("  ", " ")
        ) or "STEERING" not in _strip_steering("before [STEERING steer-ab12: do X] after")

    def test_removes_unclosed_trailing_marker(self) -> None:
        out = _strip_steering("body text [STEERING steer-ab12: still stream")
        assert "STEERING" not in out
        assert out.startswith("body text")


class TestFindButtonLabel:
    def test_recovers_label(self) -> None:
        components = [
            {
                "type": 1,
                "components": [
                    {"type": 2, "custom_id": "opt:0", "label": "First"},
                    {"type": 2, "custom_id": "opt:1", "label": "Second"},
                ],
            }
        ]
        assert _find_button_label(components, "opt:1") == "Second"
        assert _find_button_label(components, "opt:9") == ""


# ── client.py Gateway + attachment download ──────────────────────────────


class TestGatewayAttachmentNormalization:
    @pytest.mark.asyncio
    async def test_message_create_copies_attachments(self) -> None:
        captured: list[DiscordInbound] = []

        async def _capture(inbound: DiscordInbound) -> None:
            captured.append(inbound)

        client = DiscordClient(token="test", on_message=_capture)
        raw_attachment = {
            "filename": "photo.png",
            "content_type": "image/png",
            "size": len(_PNG),
            "url": "https://cdn.discordapp.com/attachments/c/m/photo.png",
        }
        client._on_dispatch(
            "MESSAGE_CREATE",
            {
                "channel_id": "c1",
                "id": "m1",
                "content": "caption",
                "author": {"id": "u1", "username": "user"},
                "attachments": [raw_attachment],
            },
        )
        tasks = tuple(client._handler_tasks)
        assert tasks
        await asyncio.gather(*tasks)

        assert captured[0].text == "caption"
        assert captured[0].attachments == [raw_attachment]

    @pytest.mark.asyncio
    async def test_download_file_operations_run_off_loop(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop_thread = threading.get_ident()
        operation_threads: dict[str, list[int]] = {
            "open": [],
            "write": [],
            "close": [],
        }
        real_open = open

        class _TrackedFile:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def write(self, chunk: bytes) -> int:
                operation_threads["write"].append(threading.get_ident())
                return self._inner.write(chunk)

            def close(self) -> None:
                operation_threads["close"].append(threading.get_ident())
                self._inner.close()

        def _tracked_open(*args: Any, **kwargs: Any) -> _TrackedFile:
            operation_threads["open"].append(threading.get_ident())
            return _TrackedFile(real_open(*args, **kwargs))

        class _Content:
            async def iter_chunked(self, size: int) -> Any:
                assert size == 8192
                yield b"first"
                yield b"second"

        class _Response:
            status = 200
            content = _Content()

            async def __aenter__(self) -> "_Response":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            def raise_for_status(self) -> None:
                return None

        class _Session:
            def get(self, *args: Any, **kwargs: Any) -> _Response:
                return _Response()

        async def _ensure_session() -> _Session:
            return _Session()

        client = DiscordClient(token="test")
        monkeypatch.setattr(client, "_ensure_session", _ensure_session)
        monkeypatch.setattr("builtins.open", _tracked_open)
        dest = tmp_path / "download.bin"

        await client.download_attachment(
            "https://cdn.discordapp.com/attachments/c/m/download.bin",
            str(dest),
        )

        assert dest.read_bytes() == b"firstsecond"
        assert all(operation_threads.values())
        assert all(
            thread != loop_thread
            for threads in operation_threads.values()
            for thread in threads
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/file.png",
            "https://cdn.discordapp.com.evil.example/file.png",
            "http://cdn.discordapp.com/file.png",
            "https://media.discordapp.net:444/file.png",
        ],
    )
    async def test_download_refuses_non_discord_origin(
        self, tmp_path: Any, url: str
    ) -> None:
        client = DiscordClient(token="test")
        with pytest.raises(ValueError, match="Discord attachment URL"):
            await client.download_attachment(url, str(tmp_path / "out"))
        assert client._session is None


class TestDiscordAttachmentAdapter:
    @pytest.mark.asyncio
    async def test_audio_is_returned_for_transcription(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeClient()
        url = "https://cdn.discordapp.com/attachments/c/m/voice.ogg"
        client.attachment_bodies[url] = b"OggS" + b"\x00" * 32
        transcribed: list[str] = []

        async def _transcribe(path: str) -> str:
            assert os.path.exists(path)
            transcribed.append(path)
            return "spoken words"

        monkeypatch.setattr(
            "kiro_crew.transcribe.is_available", lambda: True
        )
        monkeypatch.setattr(
            "kiro_crew.transcribe.transcribe_audio", _transcribe
        )

        result = await process_discord_attachments(
            client,  # type: ignore[arg-type]
            [
                {
                    "filename": "voice.ogg",
                    "content_type": "audio/ogg",
                    "size": 36,
                    "url": url,
                }
            ],
        )

        assert transcribed == result.audio_paths
        assert any("spoken words" in block for block in result.text_blocks)
        cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_stt_availability_check_runs_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop_thread = threading.get_ident()
        observed: list[int] = []
        client = FakeClient()
        url = "https://cdn.discordapp.com/attachments/c/m/voice.ogg"
        client.attachment_bodies[url] = b"OggS" + b"\x00" * 32

        def _available() -> bool:
            observed.append(threading.get_ident())
            return False

        monkeypatch.setattr(
            "kiro_crew.transcribe.is_available", _available
        )
        result = await process_discord_attachments(
            client,  # type: ignore[arg-type]
            [
                {
                    "filename": "voice.ogg",
                    "content_type": "audio/ogg",
                    "size": 36,
                    "url": url,
                }
            ],
        )

        assert observed and loop_thread not in observed
        assert result.rejections == [
            "[Audio attachment — transcription is unavailable]"
        ]
        cleanup(result.temp_paths)


class _FakeWs:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)


class TestGatewayIntents:
    @pytest.mark.asyncio
    async def test_dm_only_requests_no_privileged_intent(self) -> None:
        client = DiscordClient(token="test", enable_guild_threads=False)
        ws = _FakeWs()
        await client._identify(ws)
        assert ws.payloads[0]["d"]["intents"] == _INTENT_DIRECT_MESSAGES

    @pytest.mark.asyncio
    async def test_thread_mode_requests_guild_messages_and_content(self) -> None:
        client = DiscordClient(token="test", enable_guild_threads=True)
        ws = _FakeWs()
        await client._identify(ws)
        intents = ws.payloads[0]["d"]["intents"]
        assert intents & _INTENT_DIRECT_MESSAGES
        assert intents & _INTENT_GUILD_MESSAGES
        assert intents & _INTENT_MESSAGE_CONTENT


# ── transport.py ─────────────────────────────────────────────────────────


class TestTransportAuth:
    def test_empty_allowlist_denies_everyone(self) -> None:
        t = DiscordTransport(FakeClient())  # type: ignore[arg-type]
        msg = InboundMessage(channel_type="discord", user_id="123", conversation_id="c1", text="hi")
        assert t.authorize(msg) is False

    def test_allowed_user_passes(self) -> None:
        t = DiscordTransport(FakeClient(), allowed_user_ids=["123"])  # type: ignore[arg-type]
        msg = InboundMessage(channel_type="discord", user_id="123", conversation_id="c1", text="hi")
        assert t.authorize(msg) is True

    def test_unlisted_user_denied(self) -> None:
        t = DiscordTransport(FakeClient(), allowed_user_ids=["123"])  # type: ignore[arg-type]
        msg = InboundMessage(channel_type="discord", user_id="456", conversation_id="c1", text="hi")
        assert t.authorize(msg) is False

    def test_empty_user_id_denied(self) -> None:
        t = DiscordTransport(FakeClient(), allowed_user_ids=["123"])  # type: ignore[arg-type]
        msg = InboundMessage(channel_type="discord", user_id="", conversation_id="c1", text="hi")
        assert t.authorize(msg) is False

    def test_capabilities(self) -> None:
        assert DISCORD_CAPABILITIES.max_message_chars == DISCORD_CHUNK_LIMIT
        assert DISCORD_CAPABILITIES.streaming is True
        assert DISCORD_CAPABILITIES.edit is True
        assert DISCORD_CAPABILITIES.reactions is True
        assert DISCORD_CAPABILITIES.files_inbound is True
        assert DISCORD_CAPABILITIES.files_outbound is False  # no upload path exists
        assert DISCORD_CAPABILITIES.threads is True


class TestPublicInjectionSurface:
    """Locks the out-of-band injection contract used by AutoNudge + REST.

    The AutoNudge fire path and POST /api/autonudge reach the dispatcher only
    through ``transport.dispatcher`` and call only ``is_authorized`` /
    ``current_session_key`` / ``handle_message``. If any of these are renamed,
    these tests fail loudly — before a refactor can silently retire live
    monitoring loops at fire time.
    """

    def test_transport_dispatcher_exposes_bound_dispatcher(self) -> None:
        d, _cli, _sess = _dispatcher({"42"})
        t = DiscordTransport(FakeClient(), dispatch=d.handle_message)  # type: ignore[arg-type]
        assert t.dispatcher is d

    def test_transport_dispatcher_none_when_unwired(self) -> None:
        t = DiscordTransport(FakeClient())  # type: ignore[arg-type]
        assert t.dispatcher is None

    def test_is_authorized_deny_by_default(self) -> None:
        d, _cli, _sess = _dispatcher(set())
        assert d.is_authorized("42") is False
        assert d.is_authorized("") is False

    def test_is_authorized_allowlisted_user(self) -> None:
        d, _cli, _sess = _dispatcher({"42"})
        assert d.is_authorized("42") is True
        assert d.is_authorized("99") is False

    def test_current_session_key_matches_inbound_derivation(self) -> None:
        d, _cli, _sess = _dispatcher({"42"}, default_agent="kirocrew")
        # Must agree with the private derivation the inbound path uses — the
        # generation guard compares a stored loop key against this value.
        assert d.current_session_key("42") == d._session_key("42")
        assert d.current_session_key("42").startswith("discord:")


class TestConfiguredTargets:
    @pytest.mark.asyncio
    async def test_resolves_allowlisted_dm(self) -> None:
        client = FakeClient()
        transport = DiscordTransport(client, allowed_user_ids=["u1"])  # type: ignore[arg-type]

        assert await transport.resolve_configured_target("user:u1") == ("dm-u1", None)

    @pytest.mark.asyncio
    async def test_resolves_allowlisted_confirmed_thread(self) -> None:
        client = FakeClient()
        client.thread_channels.add("t1")
        transport = DiscordTransport(client, allowed_thread_ids=["t1"])  # type: ignore[arg-type]

        assert await transport.resolve_configured_target("thread:t1") == ("t1", None)

    @pytest.mark.asyncio
    async def test_denies_allowlisted_normal_guild_channel(self) -> None:
        client = FakeClient()
        transport = DiscordTransport(client, allowed_thread_ids=["c1"])  # type: ignore[arg-type]

        assert await transport.resolve_configured_target("thread:c1") is None


class TestTransportReceive:
    def _transport(
        self,
        allowed: list[str],
        allowed_threads: list[str] | None = None,
        allowed_channels: list[str] | None = None,
    ) -> tuple[DiscordTransport, list[InboundMessage], FakeClient]:
        dispatched: list[InboundMessage] = []

        async def _dispatch(m: InboundMessage) -> None:
            dispatched.append(m)

        client = FakeClient()
        client.thread_channels.update(allowed_threads or [])
        t = DiscordTransport(
            client,  # type: ignore[arg-type]
            allowed_user_ids=allowed,
            allowed_thread_ids=allowed_threads or [],
            allowed_channel_ids=allowed_channels or [],
            dispatch=_dispatch,
        )
        return t, dispatched, client

    @pytest.mark.asyncio
    async def test_authorized_dm_dispatches(self) -> None:
        t, dispatched, _ = self._transport(["u1"])
        await t.receive(
            DiscordInbound(channel_id="c1", user_id="u1", text="hello", message_id="m1")
        )
        assert len(dispatched) == 1
        msg = dispatched[0]
        assert isinstance(msg, DiscordInboundMessage)
        assert msg.conversation_id == "c1"
        assert msg.message_id == "m1"

    @pytest.mark.asyncio
    async def test_allowed_user_in_unapproved_thread_is_audited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.discord.transport.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kwargs: events.append(kwargs)),
        )
        t, dispatched, _ = self._transport(["u1"], ["t1"])
        await t.receive(DiscordInbound(channel_id="c1", user_id="u1", text="hello", guild_id="g1"))
        assert dispatched == []
        assert [event["outcome"] for event in events] == ["denied_unapproved_thread"]

    @pytest.mark.asyncio
    async def test_unrelated_guild_chatter_is_dropped_without_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.discord.transport.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kwargs: events.append(kwargs)),
        )
        t, dispatched, _ = self._transport(["u1"], ["t1"])
        await t.receive(DiscordInbound(channel_id="c1", user_id="u2", text="hello", guild_id="g1"))
        assert dispatched == []
        assert events == []

    @pytest.mark.asyncio
    async def test_allowlisted_thread_dispatches_for_allowed_user(self) -> None:
        t, dispatched, _ = self._transport(["u1"], ["t1"])
        await t.receive(DiscordInbound(channel_id="t1", user_id="u1", text="hello", guild_id="g1"))
        assert len(dispatched) == 1
        assert dispatched[0].thread_id == "t1"

    @pytest.mark.asyncio
    async def test_allowlisted_channel_creates_thread_before_dispatch(self) -> None:
        t, dispatched, client = self._transport(["u1"], allowed_channels=["c1"])
        await t.receive(
            DiscordInbound(
                channel_id="c1",
                user_id="u1",
                text="Plan the release",
                message_id="m1",
                guild_id="g1",
            )
        )

        assert client.created_threads == [("c1", "m1", "Plan the release")]
        assert len(dispatched) == 1
        assert dispatched[0].conversation_id == "thread-m1"
        assert dispatched[0].thread_id == "thread-m1"

    @pytest.mark.asyncio
    async def test_followup_message_in_auto_created_thread_dispatches(self) -> None:
        """The thread the transport just created must be immediately valid for
        the same user's next message, not just for button interactions -- a
        frozen ``_allowed_threads`` would silently strand every reply."""
        t, dispatched, _ = self._transport(["u1"], allowed_channels=["c1"])
        await t.receive(
            DiscordInbound(
                channel_id="c1",
                user_id="u1",
                text="Plan the release",
                message_id="m1",
                guild_id="g1",
            )
        )
        assert len(dispatched) == 1
        created_thread_id = dispatched[0].conversation_id

        await t.receive(
            DiscordInbound(
                channel_id=created_thread_id,
                user_id="u1",
                text="Here's a follow-up",
                message_id="m2",
                guild_id="g1",
            )
        )

        assert len(dispatched) == 2
        assert dispatched[1].conversation_id == created_thread_id
        assert dispatched[1].thread_id == created_thread_id
        assert dispatched[1].text == "Here's a follow-up"

    @pytest.mark.asyncio
    async def test_channels_governance_denial_blocks_thread_creation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A runtime channels-governance deny must stop the REST thread-create
        call itself -- not just the turn that would have followed it -- since
        creating the thread is a visible, irreversible side effect."""

        async def _denied(_channel_type: str) -> bool:
            return False

        monkeypatch.setattr("kiro_crew.discord.transport.channel_inbound_permitted", _denied)
        t, dispatched, client = self._transport(["u1"], allowed_channels=["c1"])
        await t.receive(
            DiscordInbound(
                channel_id="c1",
                user_id="u1",
                text="Plan the release",
                message_id="m1",
                guild_id="g1",
            )
        )

        assert client.created_threads == []
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_allowlisted_channel_rejects_unapproved_user_without_thread(self) -> None:
        t, dispatched, client = self._transport(["u1"], allowed_channels=["c1"])
        await t.receive(
            DiscordInbound(
                channel_id="c1",
                user_id="u2",
                text="hello",
                message_id="m1",
                guild_id="g1",
            )
        )

        assert client.created_threads == []
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_normal_channel_denied_even_if_id_is_allowlisted(self) -> None:
        t, dispatched, client = self._transport(["u1"], ["c1"])
        client.thread_channels.clear()
        await t.receive(DiscordInbound(channel_id="c1", user_id="u1", text="hello", guild_id="g1"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_allowlisted_thread_denies_unapproved_user(self) -> None:
        t, dispatched, _ = self._transport(["u1"], ["t1"])
        await t.receive(DiscordInbound(channel_id="t1", user_id="u2", text="hello", guild_id="g1"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_unauthorized_user_dropped(self) -> None:
        t, dispatched, _ = self._transport(["u1"])
        await t.receive(DiscordInbound(channel_id="c1", user_id="u2", text="hello"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_empty_text_dropped(self) -> None:
        t, dispatched, _ = self._transport(["u1"])
        await t.receive(DiscordInbound(channel_id="c1", user_id="u1", text=""))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_attachment_only_message_dispatches(self) -> None:
        t, dispatched, _ = self._transport(["u1"])
        attachment = {
            "filename": "photo.png",
            "content_type": "image/png",
            "size": len(_PNG),
            "url": "https://cdn.discordapp.com/attachments/c/m/photo.png",
        }
        await t.receive(
            DiscordInbound(
                channel_id="c1",
                user_id="u1",
                text="",
                attachments=[attachment],
            )
        )
        assert len(dispatched) == 1
        assert dispatched[0].text == ""
        assert dispatched[0].attachments == [attachment]

    @pytest.mark.asyncio
    async def test_non_inbound_envelope_ignored(self) -> None:
        t, dispatched, _ = self._transport(["u1"])
        await t.receive({"random": "dict"})
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_resolve_conversation_creates_dm_channel(self) -> None:
        t, _, _ = self._transport(["u1"])
        assert await t.resolve_conversation("u1") == "dm-u1"


# ── renderer.py streaming/finalization ───────────────────────────────────


class TestRenderer:
    def _renderer(self) -> tuple[DiscordRenderer, FakeClient]:
        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        return r, cli

    @pytest.mark.asyncio
    async def test_stream_and_finalize(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("Hello ")
        await r.on_text_chunk("world")
        await r.on_done()
        assert cli.final_text() == "Hello world"

    @pytest.mark.asyncio
    async def test_options_become_buttons_and_never_stream(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("Pick\n[OPTIONS: A | B]")
        # Live frames must never show the raw directive.
        for text, _ in cli.sent:
            assert "[OPTIONS" not in text
        await r.on_done()
        comps = cli.final_components()
        assert comps is not None
        labels = [b["label"] for row in comps for b in row["components"]]
        assert labels == ["A", "B"]
        assert "[OPTIONS" not in cli.final_text()

    @pytest.mark.asyncio
    async def test_long_options_before_streamed_steer_ack_become_buttons(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # The assistant's final line is a valid OPTIONS trailer. The provider's
        # internal steer acknowledgment follows it and arrives across chunks,
        # with the combined buffer well past Discord's message cap.
        await r.on_text_chunk(
            ("x" * 3800)
            + "\n\n[OPTIONS: Alpha | Bravo | Charlie]"
            + "\n\n[STEERING steer-7e6a4a0d"
        )
        await r.on_text_chunk("94314d2db: acknowledged]")
        await r.on_done()

        components = [c for _, c in cli.sent if c] + [c for _, _, c in cli.edits if c]
        labels = [b["label"] for row in components[0] for b in row["components"]]
        assert labels == ["Alpha", "Bravo", "Charlie"]
        visible = "\n".join([t for t, _ in cli.sent] + [t for _, t, _ in cli.edits])
        assert "[OPTIONS" not in visible
        assert "[STEERING" not in visible
        assert "steer-7e6a4a0d" not in visible
        assert "94314d2db" not in visible

    @pytest.mark.asyncio
    async def test_long_output_rotates_messages(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("A" * 5000)
        await r.on_done()
        # More than one message posted, none over the API cap.
        assert len(cli.sent) >= 2
        for text, _ in cli.sent:
            assert len(text) <= 2000
        for _, text, _c in cli.edits:
            assert len(text) <= 2000

    @pytest.mark.asyncio
    async def test_rotation_mid_code_block_keeps_live_fence_open(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # Open a fence and stream past one message's worth of code so rotation
        # fires while the model's closing ``` has NOT arrived yet.
        await r.on_text_chunk("```py\n" + "x = 1\n" * 400)
        # The SEALED chunk is self-contained (synthetic closer appended)…
        sealed = cli.sent[0][0]
        assert sealed.count("```") % 2 == 0
        assert sealed.rstrip().endswith("```")
        # …but the retained live buffer must keep its fence OPEN, or everything
        # streamed afterwards renders as prose outside the code block.
        assert "".join(r._buf).count("```") % 2 == 1
        assert "".join(r._buf).startswith("```")  # continuation reopens the fence
        # The model's real closer finally streams in.
        await r.on_text_chunk("y = 2\n```")
        await r.on_done()
        final = cli.final_text()
        assert final.count("```") % 2 == 0  # balanced -> no stray backticks
        assert final.startswith("```") and final.rstrip().endswith("```")
        assert "y = 2" in final.split("```")[1]  # post-rotation code stays inside

    @pytest.mark.asyncio
    async def test_rotation_mid_code_block_keeps_line_break(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # The retained tail's content ends with a newline; dropping the
        # synthetic closer must not eat it, or the next streamed line lands on
        # the previous one ("x = 1y = 2") and both code lines are corrupted.
        await r.on_text_chunk("```py\n" + "x = 1\n" * 400)
        assert "".join(r._buf).endswith("\n")
        await r.on_text_chunk("y = 2\n```")
        await r.on_done()
        final = cli.final_text()
        assert "x = 1y = 2" not in final
        assert "x = 1\ny = 2" in final

    @pytest.mark.asyncio
    async def test_rotation_keeps_blank_code_lines(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # The rotation boundary lands on the blank lines the model just emitted
        # INSIDE the open fence, leaving a newline-only remainder. Dropping it
        # pulls the next code line up onto the last one.
        await r.on_text_chunk("```py\n" + "x = 1\n" * 299 + "\n\n")
        assert "".join(r._buf).endswith("\n\n\n")
        await r.on_text_chunk("y = 2\n```")
        await r.on_done()
        final = cli.final_text()
        assert "x = 1\ny = 2" not in final  # later code did not shift up
        assert "\n\ny = 2" in final  # the blank code line survived

    @pytest.mark.asyncio
    async def test_rotation_keeps_blank_code_lines_before_more_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r, cli = self._renderer()
        monkeypatch.setattr(r, "_limit", lambda: 60)
        await r.on_turn_start()
        # Blank code lines straddling the rotation cut, with more code AFTER
        # them. The all-newline-tail case is already pinned above; this is the
        # one the round-2 remainder deleted -- the cut lands on the run, the
        # sealed side keeps the code, and the blank lines belong to the tail.
        await r.on_text_chunk("```py\n" + "x = 1\n" * 6 + "\n\n" + "y = 2\n" * 6)
        sealed = cli.sent[0][0]
        assert sealed.endswith("x = 1\n```")  # code sealed, synthetic closer on
        # The retained tail reopens the fence and still carries BOTH blank lines.
        assert "".join(r._buf) == "```\n" + "\n\n" + "y = 2\n" * 6
        await r.on_text_chunk("```")
        await r.on_done()
        # The blank code line survived to what the user reads and the later code
        # did not shift up. Only ONE blank line is asserted here: _strip_steering
        # collapses every run of 3+ newlines to 2 on EVERY render, so the visible
        # cap is that normalizer's (pre-existing, and the same before this cut
        # changed), not the splitter's.
        assert "\n\ny = 2" in cli.final_text()
        assert "x = 1\ny = 2" not in cli.final_text()

    @pytest.mark.asyncio
    async def test_rotation_ignores_inline_backticks_in_prose(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # Prose ABOUT fencing: the ``` sits mid-line, so it opens nothing. A
        # ``` SUBSTRING count reads the stream as "inside a code block", so the
        # splitter invents a closer for the sealed chunk, reopens the fence on
        # the retained tail, and the rotation then deletes that tail's closer --
        # leaving the live buffer inside an UNCLOSED code block, so every later
        # sentence renders as code.
        await r.on_text_chunk(
            "To open a code block you type ``` at the start of a line. "
            + ("Ordinary prose about how chat surfaces render markdown. " * 45)
        )
        buf = "".join(r._buf)
        assert "```" not in buf  # no reopen, no synthetic closer
        await r.on_text_chunk("Final sentence, outside any code block.")
        await r.on_done()
        # The author wrote no fence LINE anywhere, so any bare ``` line in any
        # frame -- live or sealed -- is one this renderer invented.
        for text in [t for t, _ in cli.sent] + [t for _, t, _c in cli.edits]:
            assert not any(ln.strip() == "```" for ln in text.split("\n"))
        assert "Final sentence, outside any code block." in cli.final_text()

    @pytest.mark.asyncio
    async def test_rotation_strips_closer_when_inline_backticks_hide_open_fence(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # The mirror miscount: one literal ``` in the prose plus a genuinely
        # OPEN fence makes the substring count even, so the strip never fires
        # and the retained tail is sealed shut around a live code block.
        await r.on_text_chunk(
            "You type ``` to open a block, like this:\n\n```py\n" + "x = 1\n" * 400
        )
        buf = "".join(r._buf)
        assert buf.startswith("```")  # continuation reopens the live fence
        assert not buf.rstrip().endswith("```")  # synthetic closer dropped
        await r.on_text_chunk("y = 2\n```")
        await r.on_done()
        final = cli.final_text()
        assert final.count("```") % 2 == 0
        assert "x = 1\ny = 2" in final  # post-rotation code stayed in the block

    @pytest.mark.asyncio
    async def test_authored_trailing_backticks_survive_when_nothing_split(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # Prose about fencing ends with a literal ``` and is itself UNDER the
        # cap -- only the long [OPTIONS:] trailer pushes the buffer over it. The
        # trailer is detached before splitting, so the splitter hands back a
        # lone chunk and reports no synthetic closer (appended_closer False).
        # The strip therefore never fires and the author's backticks survive.
        body = ("To fence a block in Discord, open the line with " * 36) + "type ```"
        assert len(body) < r._limit()
        trailer = "\n\n[OPTIONS: " + ("Yes " * 20) + " | " + ("No " * 20) + "]"
        await r.on_text_chunk(body + trailer)
        await r.on_done()
        visible = "\n".join([t for t, _ in cli.sent] + [t for _, t, _c in cli.edits])
        assert "type ```" in visible

    @pytest.mark.asyncio
    async def test_rotation_keeps_authored_inner_fence_line_in_4_backtick_block(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        # A 4-backtick block whose CONTENT is markdown containing 3-backtick
        # examples -- how you document fencing. Per CommonMark the inner bare
        # ``` closes nothing (a closer must be at least as long as its opener),
        # so the source fence is still open at the cut.
        #
        # The splitter judges each chunk AFTER prepending a bare ``` reopen,
        # which throws away the 4-backtick opener's run length: to the walk the
        # tail's fence is a 3-backtick one that the authored inner ``` CLOSES,
        # so it appends no synthetic closer. Any predicate re-derived from the
        # source disagrees -- it still sees the 4-backtick fence open -- and the
        # strip then deletes the author's own ``` line from the retained buffer.
        src = "````markdown\n" + "Nest a block:\n\n```py\nx = 1\n```\n\n" * 90
        assert len(src) > r._limit()
        await r.on_text_chunk(src)
        buf = "".join(r._buf)
        assert len(buf) < len(src)  # the rotation really fired
        # The author's inner closer is the last thing they wrote; it must still
        # be there, with the blank line that followed it.
        assert buf.endswith("```\n\n")
        # Stronger: the retained tail IS the source's own tail (modulo the
        # continuation reopen) -- not a shortened copy of it.
        assert src.endswith(buf[4:] if buf.startswith("```\n") else buf)
        # It survives all the way to what the user reads.
        await r.on_text_chunk("Done.\n````")
        await r.on_done()
        frames = [t for t, _ in cli.sent] + [t for _, t, _c in cli.edits]
        authored = src.count("\n```\n")
        assert sum(f.count("\n```\n") for f in frames) >= authored

    @pytest.mark.asyncio
    async def test_rotation_strip_matches_the_splitter_in_both_directions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Swept oracle: the strip fires iff the splitter appended.

        Two directions, both pinned. (a) Nothing the author wrote is ever
        deleted -- the retained tail stays a suffix of the source and every
        non-whitespace source character survives across the frames. (b) A
        synthetic closer never survives into the retained tail, or the live
        code block is sealed shut and every later token renders outside it.

        Shapes cover the information the per-chunk walk cannot see from the
        source: 3/4/5-backtick openers, authored inner bare ``` lines, literal
        backticks in prose, 4-space-indented lookalikes, and info strings.
        """
        rotated = appends = 0
        for src in _FENCE_SHAPES:
            assert src and "[OPTIONS" not in src and "[STEERING" not in src  # no detach
            for limit in list(range(40, 201, 7)) + [1900]:
                chunks, appended = _split_markdown(src, limit)
                rotated += len(chunks) > 1
                appends += appended
                r, cli = self._renderer()
                monkeypatch.setattr(r, "_limit", lambda limit=limit: limit)
                r._buf = [src]
                await r._rotate_on_length()
                tail = "".join(r._buf)
                where = f"shape={src[:14]!r} limit={limit}"
                # (b) the strip fired exactly when the splitter appended…
                if appended:
                    assert tail != chunks[-1], f"synthetic closer retained: {where}"
                # …and never otherwise.
                else:
                    assert tail == chunks[-1], f"untouched tail was modified: {where}"
                # (a) the retained tail is the source's own tail, modulo the
                # bare ``` reopen a continuation chunk carries. This one does
                # not consult the flag, so it holds the strip to the source even
                # if the splitter's report were wrong.
                assert src.endswith(tail[4:] if tail.startswith("```\n") else tail), (
                    f"retained tail is not a source suffix: {where}"
                )
                # (a) nothing authored was dropped anywhere: every
                # non-whitespace source character still appears, in order,
                # across the sealed frames plus the retained tail. Synthetic
                # backticks only ever ADD.
                seen = "".join(t for t, _ in cli.sent) + tail
                it = iter("".join(seen.split()))
                assert all(c in it for c in "".join(src.split())), (
                    f"authored characters deleted: {where}"
                )
        # The sweep must not go vacuous: most cases really rotate, and a large
        # share really do get a synthetic closer on the final chunk.
        assert rotated > 300 and appends > 100, (rotated, appends)

    @pytest.mark.asyncio
    async def test_tool_footer_transient(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_tool_call("t1", "grep")
        assert any("grep" in text for text, _ in cli.sent)
        await r.on_text_chunk("Result body")
        await r.on_done()
        assert "grep" not in cli.final_text()

    @pytest.mark.asyncio
    async def test_error_placeholder_when_no_output(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_done(stop_reason="error")
        assert "⚠️" in cli.final_text()

    @pytest.mark.asyncio
    async def test_prompt_choice_sends_separate_approval_message(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_prompt_choice([], request_id="req9")
        text, comps = cli.sent[-1]
        assert "Approve" in text
        ids = [b["custom_id"] for row in comps for b in row["components"]]
        # a:<rid>:<nonce>:<flag> — nonce guards against reused request IDs.
        assert len(ids) == 2
        assert ids[0].startswith("a:req9:") and ids[0].endswith(":1")
        assert ids[1].startswith("a:req9:") and ids[1].endswith(":0")
        nonce = ids[0].split(":")[2]
        assert len(nonce) == 16  # 8 random bytes hex
        DiscordApprovalDecider._NONCES.pop(DiscordApprovalDecider.key("sk", "req9"), None)

    @pytest.mark.asyncio
    async def test_steer_marker_rotates_message_with_chip(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("first part [STEERING steer-ab12: focus on Y] second part")
        await r.on_done()
        all_texts = [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]
        # Marker never shown raw; chip carries the summary.
        assert all("[STEERING" not in t for t in all_texts)
        assert any("focus on Y" in t for t in all_texts)

    @pytest.mark.asyncio
    async def test_close_finalizes_unfinished_turn(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("partial")
        await r.close()
        assert cli.final_text() == "partial"

    @pytest.mark.asyncio
    async def test_no_rotation_steer_summary_chip(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        r.note_steer("my steer words")
        await r.on_text_chunk("answer body")
        await r.on_done()
        assert "my steer words" in cli.final_text()
        assert "answer body" in cli.final_text()


# ── DiscordApprovalDecider ───────────────────────────────────────────────


class TestApprovalDecider:
    @pytest.mark.asyncio
    async def test_resolve_approves_with_valid_nonce(self) -> None:
        decider = DiscordApprovalDecider(session_key="sk")
        ev = SimpleNamespace(request_id="r1")
        task = asyncio.ensure_future(decider(ev))
        await asyncio.sleep(0)  # let the Future register
        key = DiscordApprovalDecider.key("sk", "r1")
        nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, True, nonce=nonce)
        assert await task is True

    @pytest.mark.asyncio
    async def test_stale_nonce_fails_closed(self) -> None:
        """A button from an earlier prompt (reused request ID) cannot resolve
        a new pending request — the nonce must match the CURRENT prompt's."""
        decider = DiscordApprovalDecider(session_key="sk")
        ev = SimpleNamespace(request_id="r1")
        task = asyncio.ensure_future(decider(ev))
        await asyncio.sleep(0)
        key = DiscordApprovalDecider.key("sk", "r1")
        DiscordApprovalDecider.register_nonce(key)  # current prompt's nonce
        # Press carries an OLD nonce (from a prompt before a restart).
        assert not DiscordApprovalDecider.resolve_global(key, True, nonce="deadbeefdeadbeef")
        assert not task.done()  # still pending — stale press had no effect
        # A missing nonce also fails closed.
        assert not DiscordApprovalDecider.resolve_global(key, True)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_resolve_unknown_key_returns_false(self) -> None:
        assert not DiscordApprovalDecider.resolve_global("sk:none", True, nonce="x")

    @pytest.mark.asyncio
    async def test_timeout_records_the_stall_for_a_bound_loop(self, monkeypatch) -> None:
        """A Discord nudge cycle stalls here, not in the dashboard runner.

        Without this the loop keeps waking, is denied by default, and spends its
        whole cycle cap accomplishing nothing.
        """
        from kiro_crew import autonudge as _an
        from kiro_crew.discord import renderer as _rend

        recorded: list[str] = []
        monkeypatch.setattr(
            _an,
            "get_instance",
            lambda: SimpleNamespace(notify_approval_stalled=recorded.append),
        )
        monkeypatch.setattr(_rend, "_APPROVAL_TIMEOUT_S", 0.01)

        decider = DiscordApprovalDecider(session_key="discord:a:direct:7")
        assert await decider(SimpleNamespace(request_id="r-timeout")) is False

        assert recorded == ["discord:a:direct:7"], "the stall was not recorded"

    @pytest.mark.asyncio
    async def test_a_pressed_deny_is_not_recorded_as_a_stall(self, monkeypatch) -> None:
        """An explicit deny is a decision, not evidence that nobody is present.

        Recording it would stop a loop whose operator is right there declining
        one tool.
        """
        from kiro_crew import autonudge as _an

        recorded: list[str] = []
        monkeypatch.setattr(
            _an,
            "get_instance",
            lambda: SimpleNamespace(notify_approval_stalled=recorded.append),
        )

        decider = DiscordApprovalDecider(session_key="discord:a:direct:7")
        ev = SimpleNamespace(request_id="r-deny")
        task = asyncio.ensure_future(decider(ev))
        await asyncio.sleep(0)
        key = DiscordApprovalDecider.key("discord:a:direct:7", "r-deny")
        nonce = DiscordApprovalDecider.register_nonce(key)
        assert DiscordApprovalDecider.resolve_global(key, False, nonce=nonce)

        assert await task is False
        assert recorded == [], "a pressed deny must not count as an unanswered prompt"


# ── transport_dispatch.py ────────────────────────────────────────────────


class TestDispatcher:
    def _msg(self, text: str, user: str = "u1", chan: str = "c1") -> InboundMessage:
        return InboundMessage(channel_type="discord", user_id=user, conversation_id=chan, text=text)

    @pytest.mark.asyncio
    async def test_a_disconnected_conversation_gets_no_reply(self) -> None:
        """Disconnecting Discord in the dashboard must actually stop the replies.

        Discord runs its OWN copy of the turn loop rather than going through
        ``messaging.dispatch.drive_turn``, so the gate there does not reach it.
        Before this, the dashboard control flipped its own label and nothing else:
        the next message in the conversation was answered exactly as before.

        The turn still runs and the message still lands in the session — the
        binding is retained by design — so this asserts on what the CONVERSATION
        receives, which is the whole of what "disconnect" promises.
        """
        d, cli, sess = _dispatcher({"u1"})
        key = d._session_key("u1")
        # True = the conversation this session was BORN in, which is what a Discord
        # session's own key names.
        sess.paused_deliveries.add((key, True))

        await d.handle_message(self._msg("hello"))

        assert cli.sent == [], f"a disconnected conversation still replied: {cli.sent}"

    @pytest.mark.asyncio
    async def test_a_connected_conversation_still_replies(self) -> None:
        """The non-vacuity half: without it, a broken renderer would pass above."""
        d, cli, _ = _dispatcher({"u1"})

        await d.handle_message(self._msg("hello"))

        assert cli.sent, "a connected conversation must still be answered"

    @pytest.mark.asyncio
    async def test_new_command_bumps_generation(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        k1 = d._session_key("u1")
        await d.handle_message(self._msg("!new"))
        assert d._session_key("u1") != k1
        assert "New conversation" in cli.sent[-1][0]

    @pytest.mark.asyncio
    async def test_help_command(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.handle_message(self._msg("!help"))
        assert "Kiro Crew" in cli.sent[-1][0]
        assert "!sessions [query]" in cli.sent[-1][0]

    @pytest.mark.asyncio
    async def test_typing_indicator_starts_before_the_session_cold_start(
        self, monkeypatch
    ) -> None:
        """TTFT guard: the typing loop must be STARTED before the ACP cold start.

        ``sessions.get_or_create`` can spend seconds spawning and handshaking an
        ACP session. ``on_turn_start`` does not send the indicator inline -- it
        spawns a refresh task -- so it must be called BEFORE the cold start, or
        the task is not even created until the cold start has finished and the
        user sees several seconds of dead air. That regressed when attachment
        ingestion was inserted ahead of ``on_turn_start`` (#1053). The shared
        skeleton in messaging/dispatch.py documents this order as "typing
        indicator before cold start"; telegram/transport_dispatch.py follows it.

        Asserting the ORDER of the two calls, not merely that both happened:
        both happen either way, so order is the entire bug. Deliberately spying
        on ``on_turn_start`` rather than ``send_typing`` -- the latter runs on a
        spawned task and cannot fire until the loop next yields, which makes it
        useless for pinning this ordering.
        """
        d, cli, sess = _dispatcher({"u1"})
        order: list[str] = []

        real_get_or_create = sess.get_or_create
        real_on_turn_start = DiscordRenderer.on_turn_start

        async def _spy_get_or_create(*args: Any, **kwargs: Any) -> Any:
            order.append("cold_start")
            return await real_get_or_create(*args, **kwargs)

        async def _spy_on_turn_start(self_: Any) -> None:
            order.append("typing_started")
            await real_on_turn_start(self_)

        monkeypatch.setattr(sess, "get_or_create", _spy_get_or_create)
        monkeypatch.setattr(DiscordRenderer, "on_turn_start", _spy_on_turn_start)

        await d.handle_message(self._msg("hello world"))

        assert "typing_started" in order, "typing was never started"
        assert "cold_start" in order, "session was never acquired"
        assert order.index("typing_started") < order.index("cold_start"), (
            f"typing must start before the cold start, got {order}"
        )

    @pytest.mark.asyncio
    async def test_normal_turn_streams_and_releases(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("hello world"))
        assert "Answer: hello world" in (cli.final_text() or "")
        assert sess.successes and sess.released

    @pytest.mark.asyncio
    async def test_cold_start_failure_releases_nothing_but_closes_renderer(
        self,
    ) -> None:
        d, cli, sess = _dispatcher({"u1"}, raise_on_get=True)
        await d.handle_message(self._msg("hello"))
        # No semaphore was acquired -> no release/record_failure of a held slot.
        assert sess.released == []
        assert sess.failures == []

    @pytest.mark.asyncio
    async def test_session_released_even_when_renderer_close_raises(self, monkeypatch) -> None:
        """A rendering-finalization failure (e.g. Discord returning a
        malformed body) must never leave the session permanently busy."""
        from kiro_crew.discord.renderer import DiscordRenderer

        async def _boom(self) -> None:
            raise RuntimeError("finalization failed")

        monkeypatch.setattr(DiscordRenderer, "close", _boom)
        d, _, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("hello"))
        assert sess.released  # release still happened
        assert d._active_renderers == {}  # renderer entry cleaned up

    @pytest.mark.asyncio
    async def test_text_and_image_reach_prompt_then_temp_is_cleaned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop_thread = threading.get_ident()
        cleanup_threads: list[int] = []

        def _cleanup(paths: list[str]) -> None:
            cleanup_threads.append(threading.get_ident())
            cleanup(paths)

        monkeypatch.setattr(
            "kiro_crew.discord.transport_dispatch.cleanup_attachments",
            _cleanup,
        )
        d, cli, _ = _dispatcher({"u1"})
        url = "https://cdn.discordapp.com/attachments/c/m/photo.png"
        cli.attachment_bodies[url] = _PNG
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="c1",
                text="look at this",
                attachments=[
                    {
                        "filename": "photo.png",
                        "content_type": "image/png",
                        "size": len(_PNG),
                        "url": url,
                    }
                ],
            )
        )
        await asyncio.sleep(0)

        prompt = d.ctx_builder.messages[-1]
        lines = prompt.splitlines()
        assert lines[0] == "look at this"
        assert lines[1].endswith(".png")
        assert cli.attachment_downloads == [url]
        assert not os.path.exists(lines[1])
        assert cleanup_threads and loop_thread not in cleanup_threads

    @pytest.mark.asyncio
    async def test_attachment_turn_acquires_before_download_yields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d, cli, sess = _dispatcher({"u1"})
        d.cfg.messaging.queue_mode = "queue"
        download_started = asyncio.Event()
        finish_download = asyncio.Event()
        url = "https://cdn.discordapp.com/attachments/c/m/slow.png"

        real_get_or_create = sess.get_or_create
        real_release = sess.release

        async def _get_or_create(*args: Any, **kwargs: Any) -> Any:
            result = await real_get_or_create(*args, **kwargs)
            sess._busy = True
            return result

        def _release(key: str) -> None:
            sess._busy = False
            real_release(key)

        async def _slow_download(download_url: str, dest: str) -> None:
            cli.attachment_downloads.append(download_url)
            download_started.set()
            await finish_download.wait()
            with open(dest, "wb") as fh:
                fh.write(_PNG)

        monkeypatch.setattr(sess, "get_or_create", _get_or_create)
        monkeypatch.setattr(sess, "release", _release)
        monkeypatch.setattr(cli, "download_attachment", _slow_download)

        first = asyncio.create_task(
            d.handle_message(
                InboundMessage(
                    channel_type="discord",
                    user_id="u1",
                    conversation_id="c1",
                    text="first",
                    attachments=[
                        {
                            "filename": "slow.png",
                            "content_type": "image/png",
                            "size": len(_PNG),
                            "url": url,
                        }
                    ],
                )
            )
        )
        await asyncio.wait_for(download_started.wait(), timeout=1)

        assert sess._busy, "session must be acquired before attachment download"
        await d.handle_message(self._msg("second"))
        assert [queued[1] for queued in sess.queued] == ["second"]

        finish_download.set()
        await first

        assert d.ctx_builder.messages[0].splitlines()[0] == "first"
        assert d.ctx_builder.messages[1] == "second"
        assert sess.queued == []

    @pytest.mark.asyncio
    async def test_command_like_caption_does_not_discard_attachment(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        url = "https://cdn.discordapp.com/attachments/c/m/command.png"
        cli.attachment_bodies[url] = _PNG
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="c1",
                text="!help",
                attachments=[
                    {
                        "filename": "command.png",
                        "content_type": "image/png",
                        "size": len(_PNG),
                        "url": url,
                    }
                ],
            )
        )
        await asyncio.sleep(0)

        prompt = d.ctx_builder.messages[-1]
        assert prompt.splitlines()[0] == "!help"
        assert prompt.splitlines()[1].endswith(".png")
        assert "Kiro Crew — Discord" not in "\n".join(text for text, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_attachment_rejection_is_not_silent(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="c1",
                text="",
                attachments=[
                    {
                        "filename": "archive.bin",
                        "content_type": "application/octet-stream",
                        "size": 10,
                        "url": "https://cdn.discordapp.com/a.bin",
                    }
                ],
            )
        )
        await asyncio.sleep(0)

        assert "unsupported type" in d.ctx_builder.messages[-1]
        assert cli.attachment_downloads == []

    @pytest.mark.asyncio
    async def test_busy_attachment_waits_for_queued_turn_before_cleanup(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        url = "https://media.discordapp.net/attachments/c/m/queued.png"
        attachment = {
            "filename": "queued.png",
            "content_type": "image/png",
            "size": len(_PNG),
            "url": url,
        }
        cli.attachment_bodies[url] = _PNG
        sess._busy = True
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="c1",
                text="",
                attachments=[attachment],
            )
        )

        assert cli.attachment_downloads == []
        assert sess.queued[0][2]["attachments"] == [attachment]
        sess._busy = False
        native_key = d._session_key("u1")
        sess.set_mirror_link(
            "dashboard:chat-1", ChannelLink("discord", channel_id="c1"), accepts_inbound=True
        )
        await d._drain_queue(native_key, "u1", "c1")
        await asyncio.sleep(0)

        assert sess.get_origin_link(native_key) == ChannelLink("discord", channel_id="c1")
        prompt_path = d.ctx_builder.messages[-1].splitlines()[0]
        assert cli.attachment_downloads == [url]
        assert prompt_path.endswith(".png")
        assert not os.path.exists(prompt_path)

    @pytest.mark.asyncio
    async def test_drain_defers_messages_that_exceed_attachment_cap(self) -> None:
        d, cli, sess = _dispatcher({"u1"})

        def _batch(prefix: str) -> list[dict[str, Any]]:
            batch: list[dict[str, Any]] = []
            for i in range(10):
                url = (
                    "https://cdn.discordapp.com/attachments/c/m/"
                    f"{prefix}-{i}.png"
                )
                cli.attachment_bodies[url] = _PNG
                batch.append(
                    {
                        "filename": f"{prefix}-{i}.png",
                        "content_type": "image/png",
                        "size": len(_PNG),
                        "url": url,
                    }
                )
            return batch

        first = _batch("first")
        second = _batch("second")
        sess.queued = [
            ("t1", "first batch", {"attachments": first}),
            ("t2", "second batch", {"attachments": second}),
            ("t3", "after second", {"attachments": []}),
        ]

        await d._drain_queue(d._session_key("u1"), "u1", "c1")

        assert cli.attachment_downloads == [
            item["url"] for item in [*first, *second]
        ]
        assert sess.queued == []
        assert len(d.ctx_builder.messages) == 2
        assert d.ctx_builder.messages[0].splitlines()[0] == "first batch"
        assert d.ctx_builder.messages[1].splitlines()[0] == "second batch"
        assert "after second" in d.ctx_builder.messages[1]

    @pytest.mark.asyncio
    async def test_busy_steers_and_acks_with_reaction(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._busy = True
        msg = DiscordInboundMessage(
            channel_type="discord",
            user_id="u1",
            conversation_id="c1",
            text="steer text",
            message_id="m42",
        )
        await d.handle_message(msg)
        assert sess._gp.steered == ["steer text"]
        assert cli.reactions == [("m42", _STEER_ACK_EMOJI)]

    @pytest.mark.asyncio
    async def test_busy_queue_override_enqueues_with_receipt(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._busy = True
        await d.handle_message(self._msg("!queue later please"))
        assert [t for _, t, _ in sess.queued] == ["later please"]
        assert any("Queued" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_stop_cancels_and_clears_queue(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._busy = True
        sess.queued.append(("ts", "queued msg", {}))
        await d.handle_message(self._msg("!stop"))
        assert sess._gp.cancelled == 1
        assert sess.queued == []
        assert "Stopped" in cli.sent[-1][0]

    @pytest.mark.asyncio
    async def test_compact_uses_try_acquire_and_releases(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!compact"))
        assert sess.acquired and sess.released
        visible = " ".join([text for text, _ in cli.sent] + [text for _, text, _ in cli.edits])
        assert "Context compacted" in visible

    @pytest.mark.asyncio
    async def test_compact_summary_body_is_not_sent(self) -> None:
        d, cli, sess = _dispatcher({"u1"})

        async def _completed(timeout: float = 0.0) -> dict:
            return {"type": "completed", "summary": "## OBJECTIVE\ninternal guidance"}

        sess._gp.wait_for_compaction = _completed
        await d.handle_message(self._msg("!compact"))
        visible = " ".join([text for text, _ in cli.sent] + [text for _, text, _ in cli.edits])
        assert "Context compacted" in visible
        assert "OBJECTIVE" not in visible and "internal guidance" not in visible

    @pytest.mark.asyncio
    async def test_compact_timeout_reports_gracefully(self) -> None:
        # Regression: nested 120s timeouts made the graceful-timeout branch
        # unreachable and destroyed a healthy session. A compaction that yields
        # no terminal status must report a timeout and KEEP the session.
        d, cli, sess = _dispatcher({"u1"})

        async def _timeout(timeout: float = 0.0) -> dict:
            return {"type": "timeout"}

        sess._gp.wait_for_compaction = _timeout
        await d.handle_message(self._msg("!compact"))
        assert any("timed out" in t for _, t, _ in cli.edits) or any(
            "timed out" in t for t, _ in cli.sent
        )
        assert sess.destroyed == [] and sess.discarded == []  # healthy session preserved

    @pytest.mark.asyncio
    async def test_link_and_unlink(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!link"))
        key = d._session_key("u1")
        assert key in sess.mirror_links
        assert legacy_dashboard_mirror_key(key) not in sess.mirror_links
        assert sess.mirror_links[key].channel_id == "c1"
        await d.handle_message(self._msg("!unlink"))
        assert key not in sess.mirror_links

    # ── Automatic origin mirroring ────────────────────────────────────────
    #
    # A Discord conversation IS its own mirror. Without the per-turn bind the
    # binding existed only after an explicit `!link`, so a turn later taken from
    # the dashboard resolved no `discord` target and the chat sat there looking
    # dead while the conversation continued elsewhere.

    @pytest.mark.asyncio
    async def test_a_turn_binds_this_conversation_as_its_own_mirror(self) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("hello"))
        key = d._session_key("u1")
        assert sess.mirror_links[key] == ChannelLink("discord", channel_id="c1", thread_id=None)

    @pytest.mark.asyncio
    async def test_a_thread_turn_binds_the_thread_channel(self) -> None:
        # A Discord thread IS a channel with its own id, so channel_id already
        # scopes the conversation and is also where the transport posts.
        d, _cli, sess = _dispatcher({"u1"}, allowed_threads={"t9"})
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="t9",
                text="hello",
                thread_id="t9",
            )
        )
        key = d._session_key("u1", "t9")
        assert sess.mirror_links[key] == ChannelLink("discord", channel_id="t9", thread_id=None)

    @pytest.mark.asyncio
    async def test_the_second_turn_writes_nothing(self) -> None:
        # The bind is re-asserted per turn, so the repeating path must be a READ:
        # a session-map mutation rewrites the whole map on the event loop.
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("first"))
        writes = len(sess.batched_writes)
        await d.handle_message(self._msg("second"))
        assert len(sess.batched_writes) == writes

    @pytest.mark.asyncio
    async def test_unlink_survives_the_users_next_message(self) -> None:
        # The whole point of persisting the refusal: an entry with no binding is
        # indistinguishable from one that was never linked, so without the flag
        # "off" would last exactly one message.
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("hello"))
        await d.handle_message(self._msg("!unlink"))
        await d.handle_message(self._msg("hello again"))
        assert sess.mirror_links == {}

    @pytest.mark.asyncio
    async def test_unlink_survives_a_generation_rotation(self) -> None:
        # `!new` (and the configured idle/daily reset) rotate the :genN suffix.
        # Keyed per generation the refusal would expire on rotation, so an idle
        # reset would undo the user's `!unlink` with no action on their part.
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!unlink"))
        await d.handle_message(self._msg("!new"))
        await d.handle_message(self._msg("hello"))
        assert sess.mirror_links == {}

    @pytest.mark.asyncio
    async def test_link_withdraws_the_refusal_so_the_bind_resumes(self) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!unlink"))
        await d.handle_message(self._msg("!link"))
        assert sess.mirror_opt_outs == set()
        sess.mirror_links.clear()  # simulate a sweep / restart-cold binding
        await d.handle_message(self._msg("hello"))
        assert sess.mirror_links[d._session_key("u1")].channel_id == "c1"

    @pytest.mark.asyncio
    async def test_link_and_unlink_each_cost_one_batched_write(self) -> None:
        # One user-visible action, one whole-map write — each mutation would
        # otherwise rewrite the entire session map on the loop.
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!link"))
        assert sess.batched_writes and all(sess.batched_writes)
        sess.batched_writes.clear()
        await d.handle_message(self._msg("!unlink"))
        assert sess.batched_writes and all(sess.batched_writes)

    @pytest.mark.asyncio
    async def test_a_refused_link_persists_nothing(self) -> None:
        """Ordering guard inside the batch.

        ``batched_save`` writes on the way out even when the block raises, so a
        refusal raised AFTER the opt-out withdrawal would persist that withdrawal
        for a link that never happened — silently turning mirroring back on. The
        claim is refused before it mutates anything, so it goes first. Two owners now
        also make the routing decision refuse `!link` ahead of the handler; either
        way nothing is persisted for a link that did not happen.
        """
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!unlink"))
        self._occupy_ambiguously(sess)
        await d.handle_message(self._msg("!link"))
        assert any("`!unlink`" in t for t, _ in cli.sent)
        assert sess.mirror_opt_outs == {_opt_out_key(d._session_key("u1"))}, (
            "a refused link must not withdraw the refusal"
        )

    @pytest.mark.asyncio
    async def test_an_ambiguous_conversation_is_answered_but_not_processed(self) -> None:
        """Two owners deny routing, so the turn must be refused — and answered.

        Falling through to this conversation's own session would answer from a
        session holding none of the context the user is looking at; an uncaught
        raise here would answer nothing at all. So: a reply, and no turn.
        """
        d, cli, sess = _dispatcher({"u1"})
        self._occupy_ambiguously(sess)
        await d.handle_message(self._msg("hello world"))
        assert "Ambiguous link" in (cli.final_text() or "")
        assert "Answer: hello world" not in (cli.final_text() or ""), (
            "the message was processed while routing was denied"
        )
        assert d._session_key("u1") not in sess.mirror_links

    @pytest.mark.asyncio
    async def test_a_unified_dm_scope_is_not_auto_bound(self) -> None:
        # dm_scope=unified collapses every allowed user's DMs into one
        # unified:{agent} bucket — channel and user drop out of the key — so an
        # automatic bind would deliver one user's dashboard replies into another
        # user's chat. `!link` stays available: it names the channel the user is in.
        d, _cli, sess = _dispatcher({"u1", "u2"}, dm_scope="unified")
        await d.handle_message(self._msg("hello", user="u1"))
        assert sess.mirror_links == {}

    @pytest.mark.asyncio
    async def test_a_thread_route_is_still_bound_under_a_unified_scope(self) -> None:
        # A guild thread keys per-channel-peer regardless of dm_scope, so its
        # bucket still names one conversation.
        d, _cli, sess = _dispatcher({"u1"}, allowed_threads={"t9"}, dm_scope="unified")
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="t9",
                text="hello",
                thread_id="t9",
            )
        )
        assert sess.mirror_links[d._session_key("u1", "t9")].channel_id == "t9"

    @pytest.mark.asyncio
    async def test_a_dashboard_mirror_aimed_at_another_channel_survives(self) -> None:
        # The dashboard can aim this session's mirror at any surface. Overwriting
        # it on the next Discord message would silently redirect the owner's
        # replies from the chat they chose into this one.
        d, _cli, sess = _dispatcher({"u1"})
        key = d._session_key("u1")
        chosen = ChannelLink("telegram", channel_id="7", thread_id=None)
        sess.mirror_links[key] = chosen
        await d.handle_message(self._msg("hello"))
        assert sess.mirror_links == {key: chosen}

    @pytest.mark.asyncio
    async def test_a_resumed_session_is_not_bound_to_this_conversation(self) -> None:
        """A resumed dashboard session's own surface owns its output.

        Both writes live behind the ``resumed_key is None`` branch, which is what
        keeps a dashboard entry from being stamped with Discord's identity;
        ``set_origin_link`` is the observable half, since the mirror bind would
        decline anyway on finding the resume binding for this same channel.
        `!link` refuses in this state too, so the automatic path must not do what
        the explicit one declines.
        """
        d, cli, sess = _dispatcher({"u1"})
        resumed = ChannelLink("discord", channel_id="c1")
        sess.mirror_links["dashboard:chat-1"] = resumed
        sess.inbound_mirror_keys.add("dashboard:chat-1")
        await d.handle_message(self._msg("hello world"))
        assert "Answer: hello world" in (cli.final_text() or "")
        assert sess.mirror_links == {"dashboard:chat-1": resumed}
        assert sess.inbound_mirror_keys == {"dashboard:chat-1"}, "resume stayed two-way"
        assert sess.origin_links == {}

    @staticmethod
    def _occupy_ambiguously(sess: FakeSessions) -> None:
        """Occupy channel ``c1`` in the one state that reaches a refused claim.

        Discord declares ``supports_session_resume``, so its conversations are
        inbound-committable and an inbound-committed occupant refuses a claim. A
        single such occupant never gets that far — the dispatcher routes the turn
        to it and skips the bind, and `!link` refuses earlier. But
        ``resumed_session`` fails CLOSED on duplicates: with two inbound bindings
        it denies routing and reports none, so both paths proceed to a claim that
        is then refused.
        """
        for key in ("dashboard:chat-9", "dashboard:chat-10"):
            sess.mirror_links[key] = ChannelLink("discord", channel_id="c1")
            sess.inbound_mirror_keys.add(key)

    @pytest.mark.asyncio
    async def test_an_explicit_bind_to_another_channel_is_not_repointed(self) -> None:
        # Nothing repoints a binding: a swept or rival-claimed one is REMOVED, not
        # moved. So a discord binding naming another channel is deliberate (the
        # dashboard can bind a surfaced session anywhere).
        d, _cli, sess = _dispatcher({"u1"})
        key = d._session_key("u1")
        chosen = ChannelLink("discord", channel_id="c-elsewhere")
        sess.mirror_links[key] = chosen
        await d.handle_message(self._msg("hello"))
        assert sess.mirror_links == {key: chosen}

    @pytest.mark.asyncio
    async def test_unlink_clears_binding_stranded_by_generation_rotation(self) -> None:
        # THE stale-mirror regression: a binding written at one DM generation,
        # then the conversation rotates (!new / idle / daily reset). The row's
        # key spelling no longer derives from the current session key, so the
        # key-addressed clears cannot reach it — yet it still occupies the
        # location and blocks `!session` resume. Unlink must free it by value.
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!link"))
        stale_key = d._session_key("u1")
        await d.handle_message(self._msg("!new"))  # rotate the generation
        key = d._session_key("u1")
        assert stale_key != key  # binding is now stranded under the old spelling
        assert stale_key not in (key, legacy_dashboard_mirror_key(key))
        await d.handle_message(self._msg("!unlink"))
        assert sess.mirror_links == {}
        assert any("Unlinked" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_unlink_clears_dashboard_mirror_into_this_channel(self) -> None:
        # A dashboard session mirroring outbound into this conversation is the
        # exact occupant `!session`'s conflict check refuses on ("attached to
        # another session") — `!unlink` in the conversation must clear it.
        d, cli, sess = _dispatcher({"u1"})
        sess.mirror_links["dashboard:chat-9"] = ChannelLink("discord", channel_id="c1")
        await d.handle_message(self._msg("!unlink"))
        assert sess.mirror_links == {}
        assert any("Unlinked" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_unlink_leaves_other_locations_alone(self) -> None:
        # The value sweep is exact-match: a mirror into a DIFFERENT Discord
        # channel must survive an unlink here, and with nothing pointing at
        # this conversation the reply stays truthful ("wasn't linked").
        d, cli, sess = _dispatcher({"u1"})
        other = ChannelLink("discord", channel_id="c2")
        sess.mirror_links["dashboard:chat-9"] = other
        await d.handle_message(self._msg("!unlink"))
        assert sess.mirror_links == {"dashboard:chat-9": other}
        assert any("wasn't linked" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_unlink_frees_location_in_one_shot_with_resumed_session(self) -> None:
        # A resumed session AND an outbound dashboard mirror can co-occupy a
        # location: a session map can hold co-located bindings written before
        # conversations became exclusive. The resumed-session early path must
        # still free the WHOLE location — one `!unlink`, not two. The rows go in
        # directly because `set_mirror_link` refuses to create this state.
        d, cli, sess = _dispatcher({"u1"})
        loc = ChannelLink("discord", channel_id="c1")
        sess.mirror_links["dashboard:resumed"] = loc
        sess.inbound_mirror_keys.add("dashboard:resumed")
        sess.mirror_links["dashboard:chat-9"] = loc
        await d.handle_message(self._msg("!unlink"))
        assert sess.mirror_links == {}
        assert any("Left the resumed session" in t for t, _ in cli.sent)
        await d.handle_message(self._msg("!unlink"))
        assert any("wasn't linked" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_unlink_repairs_duplicate_inbound_bindings(self) -> None:
        # Duplicate inbound bindings make the resolver fail closed. `!unlink`
        # repairs and settles them in the resume layer instead of falling through
        # to native unlink. Rows go in directly because the writer refuses them.
        d, cli, sess = _dispatcher({"u1"})
        loc = ChannelLink("discord", channel_id="c1")
        for wedged in ("dashboard:wedged-a", "dashboard:wedged-b"):
            sess.mirror_links[wedged] = loc
            sess.inbound_mirror_keys.add(wedged)
        await d.handle_message(self._msg("!unlink"))
        assert sess.mirror_links == {}
        assert any("Left the resumed session" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_new_frees_whole_location_when_leaving_resumed_session(self) -> None:
        # `!new` releases a resumed session through the same whole-location
        # sweep as `!unlink`: a co-located outbound mirror must not leak into
        # the fresh conversation the command starts. The rows go in directly
        # because `set_mirror_link` refuses to create this state.
        d, cli, sess = _dispatcher({"u1"})
        loc = ChannelLink("discord", channel_id="c1")
        sess.mirror_links["dashboard:resumed"] = loc
        sess.inbound_mirror_keys.add("dashboard:resumed")
        sess.mirror_links["dashboard:bystander"] = loc
        await d.handle_message(self._msg("!new"))
        assert sess.mirror_links == {}
        assert any("left the resumed session" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_default_agent_fallback(self) -> None:
        d, _, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("hi"))
        assert sess.last_agent == "kirocrew"

    @pytest.mark.asyncio
    async def test_configured_default_agent_wins(self) -> None:
        d, _, sess = _dispatcher({"u1"}, default_agent="custom")
        await d.handle_message(self._msg("hi"))
        assert sess.last_agent == "custom"

    def test_thread_session_is_shared_but_dms_remain_per_user(self) -> None:
        d, _, _ = _dispatcher({"u1", "u2"}, allowed_threads={"t1"})
        assert d._session_key("u1", "t1") == d._session_key("u2", "t1")
        assert d._session_key("u1") != d._session_key("u2")


class TestInteractions:
    def _itx(self, custom_id: str, label: str = "", guild: str = "") -> DiscordInteraction:
        return DiscordInteraction(
            interaction_id="i1",
            interaction_token="tok",
            channel_id="c1",
            user_id="u1",
            message_id="m1",
            custom_id=custom_id,
            label=label,
            guild_id=guild,
        )

    @pytest.mark.asyncio
    async def test_unauthorized_interaction_not_acked(self) -> None:
        d, cli, _ = _dispatcher({"other"})
        await d.on_interaction(self._itx("a:r1:aabbccdd:1"))
        assert cli.acked == []

    @pytest.mark.asyncio
    async def test_guild_interaction_denied(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.on_interaction(self._itx("a:r1:aabbccdd:1", guild="g1"))
        assert cli.acked == []

    @pytest.mark.asyncio
    async def test_allowlisted_thread_interaction_is_acked(self) -> None:
        d, cli, _ = _dispatcher({"u1"}, allowed_threads={"c1"})
        cli.thread_channels.add("c1")
        await d.on_interaction(self._itx("a:r9:aabbccdd:1", guild="g1"))
        assert cli.acked == ["i1"]
        assert any("expired" in text for _, text, _ in cli.edits)

    @pytest.mark.asyncio
    async def test_approval_resolves_pending_future(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(self._itx(f"a:r1:{nonce}:1"))
            assert fut.result() is True
            assert cli.acked == ["i1"]
            assert any("Approved" in t for _, t, _ in cli.edits)
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_channels_deny_drops_approval_interaction(self, tmp_path, monkeypatch) -> None:
        # HIGH (GPT pass 1 #1 + #4): a channels-governance DENY must stop a button
        # press from resolving a pending tool approval — otherwise a policy denial
        # applied after connect could still execute a governed tool via a stale
        # approval button. This regression-locks the on_interaction chokepoint
        # (removing the gate makes the pending future resolve → test fails).
        import json

        from kiro_crew.platform import governance_profiles as gp

        pdir = tmp_path / "profiles"
        pdir.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
        gp.reset_store()
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
                }
            )
        )
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(self._itx(f"a:r1:{nonce}:1"))
            # The interaction IS acked (ack happens after auth, before the gate, to
            # meet Discord's ~3s deadline — acking is a no-op UI dismissal), but the
            # approval is DROPPED before resolution: the pending future stays
            # unresolved, so the governed tool never executes.
            assert not fut.done(), "denied channel must not resolve the tool approval"
            assert cli.acked == ["i1"]
            # No verdict edit (Approved/Denied) — resolution never happened.
            assert not any("Approved" in t or "Denied" in t for _, t, _ in cli.edits)
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)
            gp.reset_store()

    @pytest.mark.asyncio
    async def test_channels_deny_still_resolves_reject_interaction(self, tmp_path, monkeypatch):
        # MEDIUM (GPT round-13 #3): a REJECT press ("a:...:0") on a denied channel
        # must STILL resolve the pending approval as refused (False) — a reject is a
        # denial, exactly what a channels-deny wants, and silently dropping it would
        # strand the pending future until timeout (~300s). Only APPROVE is gated out.
        import json

        from kiro_crew.platform import governance_profiles as gp

        pdir = tmp_path / "profiles"
        pdir.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
        gp.reset_store()
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
                }
            )
        )
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(self._itx(f"a:r1:{nonce}:0"))  # reject (flag 0)
            assert fut.done() and fut.result() is False, (
                "a reject on a denied channel must resolve the approval as refused, "
                "not strand it"
            )
            assert any("Denied" in t for _, t, _ in cli.edits)
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)
            gp.reset_store()

    @pytest.mark.asyncio
    async def test_channels_deny_drops_inbound_message(self, tmp_path, monkeypatch) -> None:
        # HIGH (GPT pass 1 #4): a channels DENY must stop handle_message from
        # driving a turn. Regression-locks the dispatcher's inbound chokepoint.
        import json

        from kiro_crew.platform import governance_profiles as gp

        pdir = tmp_path / "profiles"
        pdir.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
        gp.reset_store()
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
                }
            )
        )
        d, cli, sess = _dispatcher({"u1"})
        try:
            await d.handle_message(
                InboundMessage(
                    channel_type="discord", user_id="u1", conversation_id="c1", text="hello"
                )
            )
            # No turn ran: nothing sent, no session success recorded.
            assert cli.final_text() in (None, "")
            assert sess.successes == []
        finally:
            gp.reset_store()

    @pytest.mark.asyncio
    async def test_wrong_nonce_reports_expiry_not_approval(self) -> None:
        """A stale button press (nonce mismatch) must not display 'Approved'."""
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(self._itx("a:r1:0000000000000000:1"))
            assert not fut.done()
            assert any("expired" in t for _, t, _ in cli.edits)
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_expired_approval_reports_expiry(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.on_interaction(self._itx("a:r9:aabbccdd:1"))
        assert any("expired" in t for _, t, _ in cli.edits)

    @pytest.mark.asyncio
    async def test_option_choice_reinjects_as_turn(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        await d.on_interaction(self._itx("opt:0", label="Choice A"))
        # Buttons retired without clobbering the answer text.
        assert cli.component_edits == [("m1", [])]
        # Choice echoed as a quote, then answered as a fresh turn.
        assert any(t.startswith("> Choice A") for t, _ in cli.sent)
        assert any(
            "Answer: Choice A" in t for t in [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]
        )

    @pytest.mark.asyncio
    async def test_option_without_label_asks_to_type(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.on_interaction(self._itx("opt:0", label=""))
        assert any("type it instead" in t for t, _ in cli.sent)


def test_receipt_text_caps_displayed_items() -> None:
    texts = [f"message {i}" for i in range(8)]
    out = _receipt_text(texts)
    assert out.startswith("⏳ Queued (8):")
    assert "…and 3 more" in out


# ── context thresholds ───────────────────────────────────────────────────


class TestContextThresholdNotices:
    @pytest.mark.asyncio
    async def test_soft_threshold_nudges(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess.check_context_usage = lambda key, provider: 85.0  # >= soft (80)

        await d._maybe_notice("chan1", "scope1", "key", object())

        assert any("!compact" in s[0] for s in cli.sent)

    @pytest.mark.asyncio
    async def test_below_soft_threshold_stays_silent(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess.check_context_usage = lambda key, provider: 10.0

        await d._maybe_notice("chan1", "scope1", "key", object())

        assert cli.sent == []
