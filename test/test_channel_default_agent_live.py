"""``agent.default_agent`` reaches every non-Slack channel live, with its counters.

The setting carries no ``restart`` mark, Slack and the dashboard apply it live, and
the agent is part of each channel's session key. So each of the nine dispatchers
reads the default at the point of use, and a route that follows the default moves
to the new agent's bucket with its generation counter reseeded from there, as the
``/agent`` switch does. Without the reseed, the route's counter still belongs to
the old agent: the next turn and ``/new`` would land on one of the new agent's
older saved conversations instead of its latest one or a fresh one.

Every channel is DATA from ``_hot_reload_helpers.channel_cases``; :data:`_ROUTES`
adds the one direct conversation each test addresses.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any

import pytest
from _hot_reload_helpers import ChannelCase, cfg_with, channel_cases, prime_live

from kiro_crew.agent_switch_command import handle_channel_agent_command
from kiro_crew.config import live
from kiro_crew.messaging.conversation import ConversationState

_NEW = "amzn-builder"

#: ``channel -> (ConversationState key, read that conversation's session key)``.
_ROUTES: dict[str, tuple[Hashable, Callable[[Any], str]]] = {
    "telegram": (("direct", "7"), lambda d: d._session_key(("direct", "7"))),
    "discord": ("user:11", lambda d: d._session_key("11")),
    "webex": ("a@example.com", lambda d: d._session_key("a@example.com")),
    "teams": ("a@x.com", lambda d: d._session_key("a@x.com")),
    "imessage": ("+15550100000", lambda d: d._session_key("+15550100000")),
    "whatsapp": (
        "15550100000@s.whatsapp.net",
        lambda d: d._session_key("15550100000@s.whatsapp.net"),
    ),
    "wecom": ("Wei", lambda d: d._session_key("Wei")),
    "weixin": ("wxid_abc", lambda d: d._session_key("wxid_abc")),
    "feishu": (("direct", "ou_abc"), lambda d: d._session_key(("direct", "ou_abc"))),
}

#: The channels with a channel ``/agent`` pick store.
_PICKING = frozenset({"telegram", "discord", "webex", "teams", "feishu"})


def _cases() -> tuple[ChannelCase, ...]:
    return channel_cases()


def _picking_cases() -> tuple[ChannelCase, ...]:
    return tuple(c for c in channel_cases() if c.name in _PICKING)


def _agent(key: str) -> str:
    """The agent segment of a per-channel-peer ``{channel}:{agent}:...`` key."""
    return key.split(":")[1]


def _gen(key: str) -> int:
    last = key.rsplit(":", 1)[-1]
    return int(last.removeprefix("gen")) if last.startswith("gen") else 0


def _saved_generations(d: Any, agent: str, top: int) -> None:
    """Make every bucket of *agent* report *top* saved generations, others none."""
    d.sessions.max_generation = lambda bucket: top if _agent(bucket) == agent else 0


def test_every_channel_has_a_route() -> None:
    assert {c.name for c in channel_cases()} == set(_ROUTES)
    assert _PICKING <= set(_ROUTES)


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c.name)
def test_a_live_default_change_reaches_the_routes_next_conversation(case: ChannelCase) -> None:
    """An open conversation keeps the agent it started on; ``/new`` reads the default."""
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    assert _agent(read(d)) == "kirocrew"

    prime_live("agent", default_agent=_NEW)
    assert _agent(read(d)) == "kirocrew"

    d._conv.bump_gen(conv_key)
    assert _agent(read(d)) == _NEW


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c.name)
def test_an_idle_rotation_reads_the_default_again(case: ChannelCase) -> None:
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    d._conv.maybe_rotate(conv_key, 1000.0, idle_minutes=1)
    assert _agent(read(d)) == "kirocrew"

    prime_live("agent", default_agent=_NEW)
    d._conv.maybe_rotate(conv_key, 1030.0, idle_minutes=1)
    assert _agent(read(d)) == "kirocrew"

    d._conv.maybe_rotate(conv_key, 2000.0, idle_minutes=1)
    assert _agent(read(d)) == _NEW


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c.name)
def test_a_running_turn_stays_reachable_after_the_default_moves(case: ChannelCase) -> None:
    """``/stop`` and the busy check derive the key again: they must still find the
    turn running under the old agent, so a follow-up queues instead of running
    beside it on the new agent."""
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    _, read = _ROUTES[case.name]
    running = read(d)
    d.sessions.is_busy = lambda key: key == running

    prime_live("agent", default_agent=_NEW)

    assert read(d) == running
    assert d.sessions.is_busy(read(d))


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c.name)
def test_a_turn_admitted_after_the_default_moves_runs_its_keys_agent(case: ChannelCase) -> None:
    """A message's key is fixed before the fair-admission wait and the turn resolves
    its agent after it (Telegram's ``_run_turn``): a default change in that window
    must not run agent B under agent A's key."""
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    frozen = read(d)

    prime_live("agent", default_agent=_NEW)

    assert d._resolve_agent(conv_key) == _agent(frozen)


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c.name)
def test_a_route_first_seen_after_the_change_resumes_the_new_agents_latest(
    case: ChannelCase,
) -> None:
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    _saved_generations(d, _NEW, 3)

    prime_live("agent", default_agent=_NEW)

    key = read(d)
    assert (_agent(key), _gen(key)) == (_NEW, 3)
    d._conv.bump_gen(conv_key)
    assert _gen(read(d)) == 4


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c.name)
def test_a_new_conversation_first_after_the_change_is_past_the_new_agents(
    case: ChannelCase,
) -> None:
    """``/new`` bumps the counter BEFORE anything derives the key: the bump must land
    on the new agent's counter, not on the old one that the move then discards."""
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    _saved_generations(d, _NEW, 3)
    assert _gen(read(d)) == 0

    prime_live("agent", default_agent=_NEW)
    d._conv.bump_gen(conv_key)

    key = read(d)
    assert (_agent(key), _gen(key)) == (_NEW, 4)


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c.name)
def test_a_discarded_agent_section_keeps_the_default_in_force(case: ChannelCase) -> None:
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    prime_live("agent", default_agent=_NEW)
    assert _agent(read(d)) == _NEW

    # A discarded section loads as defaults; reading those would revert to kirocrew.
    live.watch().prime(cfg_with("agent", degraded=frozenset({"agent"}), default_agent=""))
    d._conv.bump_gen(conv_key)

    assert _agent(read(d)) == _NEW


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c.name)
def test_a_section_discarded_from_the_start_reads_the_boot_copy(case: ChannelCase) -> None:
    live.watch().prime(cfg_with("agent", degraded=frozenset({"agent"}), default_agent=""))
    d = case.make_dispatcher()
    d.cfg.agent.default_agent = "boot-agent"
    _, read = _ROUTES[case.name]

    assert _agent(read(d)) == "boot-agent"


@pytest.mark.parametrize("case", _picking_cases(), ids=lambda c: c.name)
def test_a_picked_route_keeps_its_agent_and_its_conversation(case: ChannelCase) -> None:
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    d._agent_picks.set(conv_key, "picked")
    d._conv.bump_gen(conv_key)
    before = read(d)
    assert (_agent(before), _gen(before)) == ("picked", 1)
    _saved_generations(d, "picked", 7)

    prime_live("agent", default_agent=_NEW)

    assert read(d) == before


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c.name)
def test_a_default_whose_spec_was_removed_runs_the_managed_template(
    case: ChannelCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The evidence ``default_template`` acts on: a complete installed snapshot that
    # names "gone" as removed, and an edition that does not supply it.
    monkeypatch.setattr(
        "kiro_crew.config.loader_snapshots._removal_evidence",
        lambda: (frozenset({"kirocrew"}), frozenset({"gone"}), True),
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader_snapshots._edition_agent_names", lambda: frozenset()
    )
    prime_live("agent", default_agent="gone")
    d = case.make_dispatcher()
    d.cfg.agent.default_agent = "gone"
    _, read = _ROUTES[case.name]

    assert _agent(read(d)) == "kirocrew"


def test_a_bucket_move_reseeds_the_counter_from_the_new_bucket() -> None:
    bucket = {"k": "old"}
    saved = {"old": 1, "new": 5}
    conv: ConversationState[str] = ConversationState(
        seed_fn=lambda k: saved[bucket[k]], bucket_fn=lambda k: bucket[k]
    )
    assert conv.bump_gen("k") == 2
    conv.set_awaiting("k")

    bucket["k"] = "new"

    assert conv.current_gen("k") == 5
    assert not conv.is_awaiting("k")
    assert conv.bump_gen("k") == 6
    # Staying in the bucket keeps the counter: the seed is read again only on a move.
    saved["new"] = 0
    assert conv.current_gen("k") == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _picking_cases(), ids=lambda c: c.name)
async def test_a_reset_to_an_equal_default_clears_the_pick(case: ChannelCase) -> None:
    """A pick of A reset to default while the default is A answers "Already using"
    and still drops the pick, so the route's next conversation follows the default."""
    prime_live("agent", default_agent=_NEW)
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    d._agent_picks.set(conv_key, _NEW)
    if case.name == "telegram":
        reply = await d._apply_agent(conv_key, "")
    else:
        reply = await handle_channel_agent_command(
            d._agent_picks,
            conv_key,
            "default",
            conv=d._conv,
            configured=_NEW,
            sessions=d.sessions,
            session_key=lambda: read(d),
        )

    assert "Already using" in reply
    assert d._agent_picks.get(conv_key) == ""
    prime_live("agent", default_agent="")
    d._conv.bump_gen(conv_key)
    assert _agent(read(d)) == "kirocrew"


def test_a_pin_holds_for_one_conversation() -> None:
    reads = iter(["a", "b", "c", "d"])
    conv: ConversationState[str] = ConversationState()

    def read() -> str:
        return next(reads)

    assert conv.pinned("k", read) == "a"
    assert conv.pinned("k", read) == "a"
    conv.bump_gen("k")
    assert conv.pinned("k", read) == "b"
    conv.reseed("k")
    assert conv.pinned("k", read) == "c"
    assert conv.pinned("other", read) == "d"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _picking_cases(), ids=lambda c: c.name)
async def test_a_reset_to_default_moves_to_the_live_default(case: ChannelCase) -> None:
    """``/agent default`` is a switch: from a pick it goes to the default a new
    conversation would run, not to the one the old conversation pinned."""
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    d.sessions.is_busy = lambda key: False
    d.sessions.has_session = lambda key: False
    d._agent_picks.set(conv_key, "picked")
    assert _agent(read(d)) == "picked"
    prime_live("agent", default_agent=_NEW)

    if case.name == "telegram":
        reply = await d._apply_agent(conv_key, "")
    else:
        reply = await handle_channel_agent_command(
            d._agent_picks,
            conv_key,
            "default",
            conv=d._conv,
            configured=d._configured_agent(),
            running_default=d._configured_agent(conv_key),
            sessions=d.sessions,
            session_key=lambda: read(d),
        )

    assert _NEW in reply
    assert _agent(read(d)) == _NEW


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _picking_cases(), ids=lambda c: c.name)
async def test_an_equal_reset_keeps_the_running_turns_key(case: ChannelCase) -> None:
    """A pick of B over a pinned default A, the default moved to B, a B reply
    running: a reset is "Already using" B, so the key must stay B's running one."""
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    assert _agent(read(d)) == "kirocrew"
    d._agent_picks.set(conv_key, _NEW)
    d._conv.bump_gen(conv_key)
    running = read(d)
    assert _agent(running) == _NEW
    d.sessions.is_busy = lambda key: key == running
    prime_live("agent", default_agent=_NEW)

    if case.name == "telegram":
        reply = await d._apply_agent(conv_key, "")
    else:
        reply = await handle_channel_agent_command(
            d._agent_picks,
            conv_key,
            "default",
            conv=d._conv,
            configured=d._configured_agent(),
            running_default=d._configured_agent(conv_key),
            sessions=d.sessions,
            session_key=lambda: read(d),
        )

    assert "Already using" in reply
    assert d._agent_picks.get(conv_key) == ""
    assert read(d) == running


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _picking_cases(), ids=lambda c: c.name)
async def test_naming_the_pinned_old_default_keeps_it_past_rotation(
    case: ChannelCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/agent kirocrew`` while the open conversation still runs the old default
    kirocrew is "Already using", but the user named it: ``/new`` must keep it."""
    monkeypatch.setattr(
        "kiro_crew.agent_switch_command.pickable_agent_names", lambda: ["kirocrew", _NEW]
    )
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    before = read(d)
    prime_live("agent", default_agent=_NEW)

    if case.name == "telegram":
        reply = await d._apply_agent(conv_key, "kirocrew")
    else:
        reply = await handle_channel_agent_command(
            d._agent_picks,
            conv_key,
            "kirocrew",
            conv=d._conv,
            configured=d._configured_agent(),
            running_default=d._configured_agent(conv_key),
            sessions=d.sessions,
            session_key=lambda: read(d),
        )

    assert "Already using" in reply
    assert read(d) == before
    d._conv.bump_gen(conv_key)
    assert _agent(read(d)) == "kirocrew"


@pytest.mark.asyncio
async def test_a_route_that_moved_during_discovery_goes_through_the_busy_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/agent`` reads the route before awaiting agent discovery. A ``/new`` and a
    turn on the new default in that wait must not let a stale "Already using" move
    the busy route's key: the command falls through to the switch, which refuses."""
    case = next(c for c in _picking_cases() if c.name == "feishu")
    prime_live("agent", default_agent="")
    d = case.make_dispatcher()
    conv_key, read = _ROUTES[case.name]
    assert _agent(read(d)) == "kirocrew"
    prime_live("agent", default_agent=_NEW)
    busy: list[str] = []
    d.sessions.is_busy = lambda key: key in busy
    d.sessions.has_session = lambda key: True

    def _names() -> list[str]:
        d._conv.bump_gen(conv_key)
        busy.append(read(d))
        return ["kirocrew", _NEW]

    monkeypatch.setattr("kiro_crew.agent_switch_command.pickable_agent_names", _names)
    running_default = d._configured_agent(conv_key)

    reply = await handle_channel_agent_command(
        d._agent_picks,
        conv_key,
        "kirocrew",
        conv=d._conv,
        configured=d._configured_agent(),
        running_default=running_default,
        sessions=d.sessions,
        session_key=lambda: read(d),
    )

    assert "Still working" in reply
    assert read(d) == busy[0]
