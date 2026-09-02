"""The shared pre-turn sequence, and the ratchet that keeps channels on it.

Three findings on the Feishu channel review traced to one cause: the shared
state object (``ConversationState``) existed, but every dispatcher had to
REMEMBER to drive it in the right order, and nothing enforced that. These tests
pin the order itself, plus a source-level ratchet so a channel added later
cannot quietly hand-roll the sequence again and re-open the same hole.
"""

from __future__ import annotations

import pathlib

import pytest

from kiro_crew.messaging.conversation import ConversationState
from kiro_crew.messaging.pre_turn import resolve_pre_turn

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Behaviour
# --------------------------------------------------------------------------- #


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
        """The load-bearing ordering constraint.

        Rotating first advances the generation, which mints a NEW session key --
        so the in-flight turn on the old key is missed, ``is_busy`` reads False,
        and a second concurrent turn starts instead of the message folding in via
        steer. Pinned by making the message idle-eligible AND busy: the busy
        branch must win and the generation must not move.
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
        """``maybe_rotate`` is also what stamps ``last_active``. A dispatcher that
        skips it leaves that frozen, and BOTH idle_reset_minutes and
        daily_reset_hour go silently inert -- configured, documented and dead.
        """
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


# --------------------------------------------------------------------------- #
# Ratchet
# --------------------------------------------------------------------------- #

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: Channels whose dispatcher drives the shared pre-turn sequence. The others are
#: deliberately absent: their dispatchers carry extra pre-turn work between the
#: busy check and rotation (album buffering, forum routing, service-URL binding,
#: mid-turn override parsing, attachment handling, media ingestion), and in
#: several cases the busy path is no longer a plain ``on_busy(session_key)``.
#: Listing them here would assert a migration that has not happened.
_PRE_TURN_CHANNELS = ("webex", "imessage", "feishu")

#: Rostered channels deliberately NOT on the shared helper. telegram, discord,
#: teams and slack carry extra pre-turn work between the busy check and
#: rotation; weixin and wecom run the same kind of steps from their
#: ``handle_message`` with their own busy checks (wecom additionally clears
#: attachments there and ingests media before rotating); whatsapp hand-rolls
#: the sequence in its dispatcher, and although its busy
#: handling mirrors webex's closely enough that migration looks mechanical,
#: it is still a behaviour change -- so folding any of them into the helper
#: is a separate, reviewed change. Both ratchet directions read this one set:
#: joining it (a rostered channel must be accounted for) and leaving it (a
#: migrated channel must be pruned) are each an explicit decision.
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
        """The point of the helper is that the ordering has ONE owner.

        A dispatcher that also calls ``maybe_rotate`` itself has a second,
        unreviewed copy of the sequence -- which is exactly how the ordering
        drifted per channel before.
        """
        src = (_SRC / channel / "transport_dispatch.py").read_text(encoding="utf-8")
        assert "maybe_rotate(" not in src, f"{channel} still rotates outside the helper"

    @pytest.mark.parametrize("channel", _PRE_TURN_CHANNELS)
    def test_compact_command_releases_the_notice_latch(self, channel: str) -> None:
        """A user who complies with the soft context notice must be nudged again
        next cycle. Missing this turns the notice from a per-growth-cycle nudge
        into a once-ever one -- silent, and only visible as "it stopped warning
        me". Cheap to assert, and it was got wrong once.
        """
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
        """The reverse direction: an exempt entry asserts the channel still
        hand-rolls its pre-turn sequence. Once any module in the channel's
        package calls the shared helper, the entry is stale, and with nothing
        forcing removal the set would silently accrete entries that no longer
        describe anything. The channels host their pre-turn logic in different
        modules (weixin and wecom drive it from ``handle_message``, slack's
        busy check lives in its gateway), so the scan covers the whole package
        rather than assuming one dispatch filename. The scan anchors on the
        package directory matching the channel type, so that mapping is
        asserted first: a renamed package must fail loudly here instead of
        reading as never-migrated forever.
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
