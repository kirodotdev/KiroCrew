"""The shared pre-turn sequence, and the ratchet that keeps channels on it.

A dispatcher that drives ``ConversationState`` by hand can get the order wrong.
These tests pin the order, and a source-level ratchet stops a new channel from
hand-rolling the sequence again.
"""

from __future__ import annotations

import pathlib

import pytest

from kiro_crew.messaging.conversation import ConversationState
from kiro_crew.messaging.pre_turn import resolve_pre_turn


class FakeSessions:
    """``is_busy`` answers from a set of busy keys, and records what was asked."""

    def __init__(self, busy: set[str] | None = None) -> None:
        self.busy = busy or set()
        self.asked: list[str] = []

    def is_busy(self, key: str) -> bool:
        self.asked.append(key)
        return key in self.busy


def _key_for(conv: ConversationState) -> "callable":
    """A session-key function shaped like the real ones: identity + generation."""

    def session_key_for(key: str) -> str:
        return f"webex:{key}:g{conv.current_gen(key)}"

    return session_key_for


class TestResolvePreTurn:
    @pytest.mark.asyncio
    async def test_idle_message_returns_a_key_and_does_not_fold(self) -> None:
        conv: ConversationState = ConversationState()
        sessions = FakeSessions()
        folded: list[str] = []

        key = await resolve_pre_turn(
            conv=conv,
            sessions=sessions,
            key="a@b.c",
            session_key_for=_key_for(conv),
            on_busy=lambda sk: _record(folded, sk),
        )

        assert key == "webex:a@b.c:g0"
        assert folded == []

    @pytest.mark.asyncio
    async def test_busy_folds_and_returns_none(self) -> None:
        """None is the caller's signal to return WITHOUT driving a turn."""
        conv: ConversationState = ConversationState()
        sessions = FakeSessions(busy={"webex:a@b.c:g0"})
        folded: list[str] = []

        key = await resolve_pre_turn(
            conv=conv,
            sessions=sessions,
            key="a@b.c",
            session_key_for=_key_for(conv),
            on_busy=lambda sk: _record(folded, sk),
        )

        assert key is None
        assert folded == ["webex:a@b.c:g0"], "the busy handler must get the live key"

    @pytest.mark.asyncio
    async def test_busy_is_checked_before_rotation(self) -> None:
        """Rotating first would mint a new key, miss the in-flight turn and start a
        second one. An idle-eligible AND busy message must fold, generation unchanged.
        """
        conv: ConversationState = ConversationState()
        conv.maybe_rotate("a@b.c", 1_000.0)  # seed last_active far in the past
        # (POSITIVE on purpose: should_rotate_generation treats <= 0 as the
        # first message in the bucket and never rotates, which would make
        # this test pass without the guard it exists to pin.)
        gen_before = conv.current_gen("a@b.c")
        sessions = FakeSessions(busy={f"webex:a@b.c:g{gen_before}"})
        folded: list[str] = []

        key = await resolve_pre_turn(
            conv=conv,
            sessions=sessions,
            key="a@b.c",
            session_key_for=_key_for(conv),
            idle_minutes=30,
            on_busy=lambda sk: _record(folded, sk),
            now=10_000.0,
        )

        assert key is None
        assert conv.current_gen("a@b.c") == gen_before, "rotation ran despite a live turn"

    @pytest.mark.asyncio
    async def test_key_is_re_derived_after_rotation(self) -> None:
        """Rotation advances the generation, so the pre-rotation key addresses
        the conversation rotation just retired."""
        conv: ConversationState = ConversationState()
        conv.maybe_rotate("a@b.c", 1_000.0)
        before = _key_for(conv)("a@b.c")

        key = await resolve_pre_turn(
            conv=conv,
            sessions=FakeSessions(),
            key="a@b.c",
            session_key_for=_key_for(conv),
            idle_minutes=30,
            on_busy=_unreachable,
            now=10_000.0,
        )

        assert key is not None and key != before, f"key did not follow rotation ({key})"

    @pytest.mark.asyncio
    async def test_activity_is_recorded_even_when_nothing_rotates(self) -> None:
        """``maybe_rotate`` also stamps ``last_active``; skipping it silently disables
        idle_reset_minutes and daily_reset_hour."""
        conv: ConversationState = ConversationState()

        await resolve_pre_turn(
            conv=conv,
            sessions=FakeSessions(),
            key="a@b.c",
            session_key_for=_key_for(conv),
            idle_minutes=30,
            on_busy=_unreachable,
            now=1_234.0,
        )

        assert conv._get("a@b.c").last_active == 1_234.0

    @pytest.mark.asyncio
    async def test_rotation_is_off_by_default(self) -> None:
        """Defaults must be inert: a caller that passes no policy gets no rotation,
        so adopting the helper cannot silently start resetting conversations."""
        conv: ConversationState = ConversationState()
        conv.maybe_rotate("a@b.c", 1_000.0)
        gen_before = conv.current_gen("a@b.c")

        await resolve_pre_turn(
            conv=conv,
            sessions=FakeSessions(),
            key="a@b.c",
            session_key_for=_key_for(conv),
            on_busy=_unreachable,
            now=10_000_000.0,
        )

        assert conv.current_gen("a@b.c") == gen_before


async def _record(sink: list[str], session_key: str) -> None:
    sink.append(session_key)


async def _unreachable(session_key: str) -> None:  # pragma: no cover - guard
    raise AssertionError(f"on_busy must not run for an idle key ({session_key})")


_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: Channels whose dispatcher drives the shared pre-turn sequence. The others are
#: deliberately absent: their dispatchers carry extra pre-turn work between the
#: busy check and rotation (album buffering, forum routing, service-URL binding,
#: mid-turn override parsing, attachment handling, media ingestion), and in
#: several cases the busy path is not a plain ``on_busy(session_key)``.
#: Listing them here would assert a migration that has not happened.
_PRE_TURN_CHANNELS = ("webex", "imessage", "feishu")

#: Rostered channels deliberately NOT on the shared helper: they run extra steps
#: between the busy check and rotation, or their own busy checks from
#: ``handle_message`` (weixin, wecom). Moving one is a behaviour change. Both
#: ratchet directions read this set, so joining and leaving it are explicit.
_EXEMPT_CHANNELS = frozenset(
    {"telegram", "discord", "teams", "slack", "weixin", "wecom", "whatsapp"}
)


def _calls_pre_turn_helper(path: pathlib.Path) -> bool:
    """A call on a code line marks a migration; a comment merely naming the
    helper (say, to explain why a channel deliberately avoids it) does not."""
    return any(
        "resolve_pre_turn(" in line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


class TestPreTurnRatchet:
    @pytest.mark.parametrize("channel", _PRE_TURN_CHANNELS)
    def test_dispatcher_uses_the_shared_helper(self, channel: str) -> None:
        src = (_SRC / channel / "transport_dispatch.py").read_text(encoding="utf-8")
        assert "resolve_pre_turn(" in src, f"{channel} does not call resolve_pre_turn"

    @pytest.mark.parametrize("channel", _PRE_TURN_CHANNELS)
    def test_dispatcher_does_not_hand_roll_the_sequence(self, channel: str) -> None:
        """The ordering has ONE owner: a dispatcher calling ``maybe_rotate`` itself
        holds a second copy of the sequence that can drift."""
        src = (_SRC / channel / "transport_dispatch.py").read_text(encoding="utf-8")
        assert "maybe_rotate(" not in src, f"{channel} still rotates outside the helper"

    @pytest.mark.parametrize("channel", _PRE_TURN_CHANNELS)
    def test_compact_command_releases_the_notice_latch(self, channel: str) -> None:
        """After ``/compact`` the soft context notice must fire again next cycle;
        without the release it warns only once ever."""
        src = (_SRC / channel / "transport_dispatch.py").read_text(encoding="utf-8")
        assert "clear_awaiting(" in src, f"{channel} never releases the awaiting latch"

    def test_every_rostered_channel_is_accounted_for(self) -> None:
        """Ratchet on the ratchet: a channel added to the roster must be either
        migrated to the helper or named in the exempt list, so "not yet migrated"
        stays an explicit decision instead of a silent omission.
        """
        from kiro_crew.channels import builtin_channel_descriptors

        rostered = {d.channel_type for d in builtin_channel_descriptors()}
        unrostered = _EXEMPT_CHANNELS - rostered
        assert not unrostered, (
            "exempt entries naming channels no longer in the roster "
            f"(prune them from _EXEMPT_CHANNELS): {sorted(unrostered)}"
        )
        unaccounted = rostered - set(_PRE_TURN_CHANNELS) - _EXEMPT_CHANNELS
        assert not unaccounted, (
            "channels neither using messaging.pre_turn nor listed exempt: " f"{sorted(unaccounted)}"
        )

    def test_exempt_channels_have_not_graduated(self) -> None:
        """An exempt channel whose package calls the helper must be pruned.

        Pre-turn logic lives in different modules per channel, so the scan covers
        the whole package; the package dir is asserted first so a rename fails loudly.
        """
        missing = {c for c in _EXEMPT_CHANNELS if not (_SRC / c).is_dir()}
        assert not missing, (
            "exempt channels without a package dir under src/kiro_crew "
            f"(the graduation scan cannot see them): {sorted(missing)}"
        )
        graduated = {
            channel
            for channel in _EXEMPT_CHANNELS
            if any(_calls_pre_turn_helper(path) for path in (_SRC / channel).rglob("*.py"))
        }
        assert not graduated, (
            "channels in the exempt list already call resolve_pre_turn -- prune "
            "them from _EXEMPT_CHANNELS (a migrated channel belongs in "
            f"_PRE_TURN_CHANNELS instead): {sorted(graduated)}"
        )
