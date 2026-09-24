"""An owner-DM channel session may act as a conductor; every other channel caller stays contained.

Three gates decide whether a caller's turns may reach another session or the work
ledger: the creator gate (``_refuse_ineligible_creator``), the target gate
(``authorize_target``'s caller half) and the ledger gate
(``handlers/work_ledger._caller_key``). Before this predicate existed they keyed
on three different facts — the live ``linked_session_key``, the key prefix, the
mirror store — and refused every channel-born session alike, which made a Discord
or Telegram DM whose only human is the configured owner unusable as a conductor.

The suite pins the one predicate all three now consult (``audience_is_owner``),
its fail-closed edges, and that the gates cannot disagree about a slot.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import work_ledger as ledger_routes
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.transport import ConfiguredChannelTarget

OWNER = "123456789012345678"
GUEST = "111111111111111111"
THREAD = "987654321098765432"

DISCORD_DM = f"discord:kirocrew-conductor:direct:{OWNER}:gen5"
DISCORD_THREAD = f"discord:kirocrew-conductor:group:{THREAD}:gen2"
TELEGRAM_DM = f"telegram:kirocrew:direct:{OWNER}:gen4"
TELEGRAM_FORUM = "telegram:kirocrew:forum:-1001:77:gen1"

DISCORD_DM_CONVERSATION = ChannelLink("discord", channel_id="dm-channel-4242")
TELEGRAM_DM_CONVERSATION = ChannelLink("telegram", channel_id=OWNER)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Every test runs in the shipped (enabled) state without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    """The per-caller create-rate window is process-wide module state."""
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


class _Transport:
    """The one transport surface the predicate reads: ``configured_targets()``.

    Shaped like the real Discord and Telegram transports — one ``user:`` target
    per allow-listed identity, one ``thread:`` target per allow-listed thread.
    """

    def __init__(self, channel_type: str, users=(), threads=(), *, available=True) -> None:
        self.channel_type = channel_type
        self.capabilities = SimpleNamespace(supports_proactive_send=True)
        self._users = list(users)
        self._threads = list(threads)
        self._available = available

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        targets = [
            ConfiguredChannelTarget(f"user:{u}", f"DM · {u}", available=self._available)
            for u in self._users
        ]
        targets.extend(
            ConfiguredChannelTarget(f"thread:{t}", f"thread · {t}") for t in self._threads
        )
        return targets


def _state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    # The session manager's in-memory record of the conversation each channel
    # session was started from, as the dispatchers write it on every inbound turn.
    origins: dict[str, ChannelLink] = {}
    state.sessions.set_origin_link = MagicMock(side_effect=origins.__setitem__)
    state.sessions.get_origin_link = MagicMock(side_effect=origins.get)
    state.push_slots_update = MagicMock()
    return state


def _channel_slot(state, session_key: str, *, origin: ChannelLink | None, mirror=...):
    """A channel-born slot the way ``surface_channel_session`` builds one.

    *origin* is what the channel dispatcher recorded as the conversation the
    session lives in; *mirror* is the outbound mirror binding, which the origin
    bind makes equal to the origin unless a test retargets it.
    """
    name = session_key.replace(":", "_")
    slot = state.get_or_create_slot(name, linked_session_key=session_key, channel_origin=True)
    if origin is not None:
        state.sessions.set_origin_link(session_key, origin)
    if mirror is ...:
        mirror = origin
    if mirror is not None:
        state.sessions.set_mirror_link(session_key, mirror)
    return slot


def _key(slot) -> str:
    """The session key the MCP process presents for *slot*."""
    return slot_history_key(slot)


def _ledger_gate(state, sk: str):
    """Run the ledger's caller gate for *sk*; ``(ledger_key, None)`` or ``(None, refusal)``."""
    app = web.Application()
    app["state"] = state
    request = make_mocked_request("GET", "/api/work-ledger", app=app, headers={"X-Session-Key": sk})
    request["internal_auth"] = True
    return asyncio.run(ledger_routes._caller_key(request, "work_ledger_read"))


@pytest.fixture
def open_ledger_gate(monkeypatch):
    """Bypass recognition and restriction, whose own suites cover them."""

    async def _recognized(*a, **k):
        return None

    monkeypatch.setattr(ledger_routes, "_recognize_session", _recognized)
    monkeypatch.setattr(ledger_routes, "_is_restricted_session", lambda *a: False)


def _owner_discord(state) -> None:
    state.register_channel_transport(_Transport("discord", users=[OWNER], threads=[THREAD]))


def _owner_telegram(state) -> None:
    state.register_channel_transport(_Transport("telegram", users=[OWNER]))


# ── (a) a group or thread channel session is still refused by all three gates ──


def test_a_discord_thread_session_is_refused_by_all_three_gates(
    tmp_path, monkeypatch, open_ledger_gate
):
    """Unchanged behaviour, pinned: a thread has an audience the operator does not
    control, so nothing about the owner allow-list makes it a conductor."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    thread = _channel_slot(state, DISCORD_THREAD, origin=ChannelLink("discord", channel_id=THREAD))
    state.get_or_create_slot("chat-peer")

    assert sc.audience_is_owner(state, thread) is False
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(thread)))
    assert exc.value.code == "linked_session_caller"
    for op in ("send", "stop", "read"):
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state, caller_session_key=_key(thread), target="chat-peer", operation=op
            )
        assert exc.value.code == "linked_session_caller", op
    key, refusal = _ledger_gate(state, DISCORD_THREAD)
    assert key is None and refusal is not None
    assert refusal.status == 403
    assert '"channel_session"' in refusal.text


def test_a_telegram_forum_topic_is_refused_by_all_three_gates(
    tmp_path, monkeypatch, open_ledger_gate
):
    state = _state(tmp_path, monkeypatch)
    _owner_telegram(state)
    topic = _channel_slot(
        state, TELEGRAM_FORUM, origin=ChannelLink("telegram", channel_id="-1001", thread_id="77")
    )
    state.get_or_create_slot("chat-peer")

    assert sc.audience_is_owner(state, topic) is False
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(topic)))
    assert exc.value.code == "linked_session_caller"
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(topic), target="chat-peer", operation="send"
        )
    assert exc.value.code == "linked_session_caller"
    key, refusal = _ledger_gate(state, TELEGRAM_FORUM)
    assert key is None and refusal is not None and refusal.status == 403


# ── (b) an owner 1:1 DM passes all three, on Discord and on Telegram ──


@pytest.mark.parametrize(
    ("session_key", "register", "conversation"),
    [
        (DISCORD_DM, _owner_discord, DISCORD_DM_CONVERSATION),
        (TELEGRAM_DM, _owner_telegram, TELEGRAM_DM_CONVERSATION),
    ],
    ids=["discord", "telegram"],
)
def test_an_owner_dm_session_conducts(
    tmp_path, monkeypatch, open_ledger_gate, session_key, register, conversation
):
    """The whole conductor loop from a DM whose only human is the configured owner:
    create a worker, send to it, read it, stop it, and reach the ledger. The
    origin mirror the dispatcher binds on every turn is the DM itself, so it is
    not a second audience."""
    state = _state(tmp_path, monkeypatch)
    register(state)
    dm = _channel_slot(state, session_key, origin=conversation)

    assert sc.audience_is_owner(state, dm) is True
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(dm)))
    worker_key = created["target"]
    assert state.get_slot(worker_key)._created_by == dm.key
    for op in ("send", "read", "stop"):
        target = sc.authorize_target(
            state, caller_session_key=_key(dm), target=worker_key, operation=op
        )
        assert target.key == worker_key, op
    key, refusal = _ledger_gate(state, session_key)
    assert refusal is None
    assert key == session_key


def test_an_owner_dm_conductor_is_fenced_to_the_sessions_it_created(tmp_path, monkeypatch):
    """Relaxed: dispatching and driving its own workers. Kept: the person's other
    sessions. The DM inherits a crew member's fence, not the owner's own tab's reach,
    so a wrong audience inference costs the sessions the DM created and nothing else."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    state.get_or_create_slot("chat-the-persons-own-tab")

    for op in ("send", "read", "stop"):
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=_key(dm),
                target="chat-the-persons-own-tab",
                operation=op,
            )
        assert exc.value.code == "not_creator", op
        assert "owner-DM" in exc.value.message
    assert sc._caller_is_ownership_fenced(state, dm.key) is True


def test_a_paused_origin_mirror_still_counts_as_the_owners_dm(tmp_path, monkeypatch):
    """The dashboard's Disconnect row pauses delivery and RETAINS the binding, so a
    paused mirror must read exactly like a live one — the audience did not change."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    state.sessions.is_mirror_paused = MagicMock(return_value=True)
    assert sc.audience_is_owner(state, dm) is True


# ── the predicate fails CLOSED on every edge it cannot answer ──


def test_a_second_allow_listed_identity_removes_the_owner(tmp_path, monkeypatch):
    """Same one-identity rule as `/sessions` and the owner notice: an allow-list
    is a list of people permitted to talk to the agent, not a claim that any one
    of them is the operator, so two entries name nobody."""
    state = _state(tmp_path, monkeypatch)
    state.register_channel_transport(_Transport("discord", users=[OWNER, GUEST]))
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    assert sc.audience_is_owner(state, dm) is False
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(dm)))
    assert exc.value.code == "linked_session_caller"


def test_a_dm_with_someone_other_than_the_sole_owner_is_refused(tmp_path, monkeypatch):
    """The DM peer has to BE the configured identity, not merely a DM."""
    state = _state(tmp_path, monkeypatch)
    state.register_channel_transport(_Transport("discord", users=[GUEST]))
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    assert sc.audience_is_owner(state, dm) is False


def test_an_unavailable_or_absent_transport_names_no_owner(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    assert sc.audience_is_owner(state, dm) is False, "no transport registered"
    state.register_channel_transport(_Transport("discord", users=[OWNER], available=False))
    assert sc.audience_is_owner(state, dm) is False, "the only target is unavailable"


def test_a_retargeted_mirror_widens_the_audience_and_refuses(tmp_path, monkeypatch):
    """The dashboard can aim a session's mirror at any surface. A DM whose mirror
    now points at a thread, or at another channel, republishes what it reads to
    people who are not the owner."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    to_thread = _channel_slot(
        state,
        DISCORD_DM,
        origin=DISCORD_DM_CONVERSATION,
        mirror=ChannelLink("discord", channel_id=THREAD),
    )
    assert sc.audience_is_owner(state, to_thread) is False
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(to_thread)))
    assert exc.value.code == "linked_session_caller"

    state.sessions.set_mirror_link(DISCORD_DM, ChannelLink("telegram", channel_id="7"))
    assert sc.audience_is_owner(state, to_thread) is False

    # Back to its own conversation: the same audience again.
    state.sessions.set_mirror_link(DISCORD_DM, DISCORD_DM_CONVERSATION)
    assert sc.audience_is_owner(state, to_thread) is True
    # And with no mirror at all the only audience is the DM itself.
    state.sessions.clear_mirror_link(DISCORD_DM)
    assert sc.audience_is_owner(state, to_thread) is True


def test_an_unknown_origin_conversation_fails_closed(tmp_path, monkeypatch):
    """Without the dispatcher's record of the conversation the session lives in,
    a mirror cannot be told apart from a retarget — so it is not waved through."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    dm = _channel_slot(state, DISCORD_DM, origin=None, mirror=DISCORD_DM_CONVERSATION)
    assert sc.audience_is_owner(state, dm) is False


def test_a_restart_cold_owner_dm_is_told_to_send_a_message(tmp_path, monkeypatch, open_ledger_gate):
    """The one refusal a correctly configured owner DM still meets, and it names itself.

    The origin is recorded per inbound turn and held in memory only, while the slot
    and its mirror are persisted and re-surfaced at boot — so between a gateway
    restart and the owner's next channel message every other clause holds and this
    one does not. The exemption stays withheld (a mirror without the recorded
    origin cannot be told from a retarget), but all three gates say WHICH fact is
    missing and what clears it, rather than reporting a channel link the caller
    cannot do anything about.
    """
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    cold = _channel_slot(state, DISCORD_DM, origin=None, mirror=DISCORD_DM_CONVERSATION)
    state.get_or_create_slot("chat-peer")

    assert sc.owner_dm_refusal(state, cold) == sc.ORIGIN_NOT_ON_RECORD
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(cold)))
    assert exc.value.code == "linked_session_caller"
    assert sc.ORIGIN_NOT_ON_RECORD in exc.value.message
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(cold), target="chat-peer", operation="send"
        )
    assert sc.ORIGIN_NOT_ON_RECORD in exc.value.message
    _, refusal = _ledger_gate(state, DISCORD_DM)
    assert refusal is not None and "origin is not on record" in refusal.text

    # The next inbound turn records it again, and the DM conducts.
    state.sessions.set_origin_link(DISCORD_DM, DISCORD_DM_CONVERSATION)
    assert sc.audience_is_owner(state, cold) is True


def test_a_group_key_naming_the_owner_is_refused_on_its_chat_type(tmp_path, monkeypatch):
    """The mutation pin for the DIRECT clause: the ONLY thing wrong here is the chat type.

    Every other negative case in this file is independently killed by another
    clause (a thread key's scope element is the thread, a forum key carries two
    scope segments), so deleting ``chat_type != CHAT_TYPE_DIRECT`` would red none
    of them. This key names the sole owner as its scope, with a matching origin
    and mirror, so the chat type is the one fact left to refuse it.
    """
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    group_of_one = f"discord:kirocrew-conductor:group:{OWNER}:gen1"
    slot = _channel_slot(state, group_of_one, origin=DISCORD_DM_CONVERSATION)
    assert sc.owner_dm_refusal(state, slot) == "the conversation is not a 1:1 direct message"
    assert sc.audience_is_owner(state, slot) is False


def test_an_unreadable_mirror_store_fails_closed(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    state.sessions.get_mirror_link = MagicMock(side_effect=RuntimeError("store unreadable"))
    assert sc.audience_is_owner(state, dm) is False


@pytest.mark.parametrize(
    "linked",
    [
        "",  # a dashboard-born slot: the question does not arise
        "cron:job-1",  # a cron tab's link is not a channel
        "slack:1786300000.000200",  # the legacy two-segment shape does not parse
        "unified:kirocrew:gen3",  # a unified bucket names no peer
        "webex:kirocrew:direct:someone@example.com",  # a surface not verified for this
        "discord:kirocrew:direct",  # too short to be an address
        f"discord:kirocrew:direct:{OWNER}:extra",  # a DM scope is exactly the peer
    ],
)
def test_keys_the_predicate_cannot_read_as_an_owner_dm(tmp_path, monkeypatch, linked):
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    state.register_channel_transport(_Transport("webex", users=["someone@example.com"]))
    slot = state.get_or_create_slot("chat-any")
    slot.linked_session_key = linked
    assert sc.audience_is_owner(state, slot) is False


def test_a_dashboard_born_mirrored_caller_is_still_refused(tmp_path, monkeypatch):
    """The predicate is about channel-BORN sessions. A dashboard session that
    mirrors to the owner's DM keeps today's refusal: its own conversation is the
    dashboard, and nothing here re-derives an audience for it."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    caller = state.get_or_create_slot("chat-mirrored")
    state.sessions.set_mirror_link(slot_history_key(caller), DISCORD_DM_CONVERSATION)
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    assert exc.value.code == "mirrored_caller"


# ── (c) the ledger gate and session control agree on the same slot ──


@pytest.mark.parametrize(
    ("session_key", "mirror", "users"),
    [
        (DISCORD_DM, DISCORD_DM_CONVERSATION, [OWNER]),
        (DISCORD_DM, ChannelLink("discord", channel_id=THREAD), [OWNER]),
        (DISCORD_DM, None, [OWNER, GUEST]),
        (DISCORD_THREAD, ChannelLink("discord", channel_id=THREAD), [OWNER]),
        (TELEGRAM_DM, TELEGRAM_DM_CONVERSATION, [OWNER]),
    ],
    ids=["owner-dm", "dm-retargeted", "two-identities", "thread", "telegram-dm"],
)
def test_the_ledger_gate_and_session_control_agree_on_the_same_slot(
    tmp_path, monkeypatch, open_ledger_gate, session_key, mirror, users
):
    """One predicate, consulted by both, over the slot ``caller_slot_key`` resolves —
    never over the key prefix on one side and the live link on the other."""
    state = _state(tmp_path, monkeypatch)
    surface = session_key.split(":", 1)[0]
    state.register_channel_transport(_Transport(surface, users=users, threads=[THREAD]))
    origin = (
        DISCORD_DM_CONVERSATION
        if session_key == DISCORD_DM
        else (
            TELEGRAM_DM_CONVERSATION
            if session_key == TELEGRAM_DM
            else ChannelLink("discord", channel_id=THREAD)
        )
    )
    slot = _channel_slot(state, session_key, origin=origin, mirror=mirror)
    state.get_or_create_slot("chat-peer")

    verdict = sc.audience_is_owner(state, slot)
    assert sc.session_audience_is_owner(state, session_key) is verdict

    creator_allowed = True
    try:
        sc._refuse_ineligible_creator(state, slot)
    except sc.SessionControlError:
        creator_allowed = False
    assert creator_allowed is verdict

    _, refusal = _ledger_gate(state, session_key)
    assert (refusal is None) is verdict


def test_the_ledger_post_read_recheck_uses_the_same_predicate(tmp_path, monkeypatch):
    """Containment decided on entry says nothing about containment after the read:
    a mirror retargeted while the ledger was being read drops the answer, for an
    owner DM exactly as a gained mirror does for a dashboard session."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    app = web.Application()
    app["state"] = state
    request = make_mocked_request(
        "GET", "/api/work-ledger", app=app, headers={"X-Session-Key": DISCORD_DM}
    )
    assert ledger_routes._contained_channel_caller(request, DISCORD_DM) == ""
    state.sessions.set_mirror_link(DISCORD_DM, ChannelLink("discord", channel_id=THREAD))
    assert (
        ledger_routes._contained_channel_caller(request, DISCORD_DM)
        == "the outbound mirror points somewhere other than this conversation"
    ), "the reason is the predicate's own, so the ledger tells the caller what session control would"


def test_a_channel_conductor_owns_the_worker_it_created_at_bind(tmp_path, monkeypatch):
    """``session_create`` stamps the creator's SLOT key while the ledger addresses the
    conductor by its SESSION key; for a dashboard session the two fold to one
    spelling, for a channel session they do not. The ownership check has to
    resolve the conductor's slot, or every channel conductor's bind is refused as
    ``worker_not_owned``."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(dm)))
    worker_key = created["target"]

    app = web.Application()
    app["state"] = state
    request = make_mocked_request(
        "POST", "/api/work-ledger/record", app=app, headers={"X-Session-Key": DISCORD_DM}
    )
    monkeypatch.setattr(ledger_routes, "_has_binding", lambda folded: False)
    assert ledger_routes._refuse_unowned_worker(request, DISCORD_DM, worker_key) is None

    stranger = state.get_or_create_slot("chat-stranger")
    stranger._created_by = "chat-someone-else"
    refusal = ledger_routes._refuse_unowned_worker(request, DISCORD_DM, "chat-stranger")
    assert refusal is not None and refusal.status == 403
    assert '"worker_not_owned"' in refusal.text


# ── (d) mirror-unlink still only clears the mirror ──


@pytest.mark.asyncio
async def test_mirror_unlink_clears_the_mirror_and_nothing_else(tmp_path, monkeypatch):
    """The dashboard's unlink clears the OUTBOUND mirror binding only. The slot
    stays channel-born (``linked_session_key`` untouched), so it neither detaches
    the session from its channel nor changes what the gates decide about it: a
    thread session is refused before and after, an owner DM is admitted before
    and after."""
    from kiro_crew.dashboard.chat_mirror import api_chat_slot_mirror_unlink

    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    thread = _channel_slot(state, DISCORD_THREAD, origin=ChannelLink("discord", channel_id=THREAD))
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{name}/mirror-unlink", api_chat_slot_mirror_unlink)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/chat/slots/{thread.key}/mirror-unlink")
        assert resp.status == 200
        assert (await resp.json())["was_linked"] is True
        assert state.sessions.get_mirror_link(DISCORD_THREAD) is None
        assert thread.linked_session_key == DISCORD_THREAD
        assert sc.audience_is_owner(state, thread) is False
        with pytest.raises(sc.SessionControlError) as exc:
            await sc.create_session(state, caller_session_key=_key(thread))
        assert exc.value.code == "linked_session_caller"

        resp = await client.post(f"/api/chat/slots/{dm.key}/mirror-unlink")
        assert resp.status == 200
        assert (await resp.json())["was_linked"] is True
        assert state.sessions.get_mirror_link(DISCORD_DM) is None
        assert dm.linked_session_key == DISCORD_DM
        assert sc.audience_is_owner(state, dm) is True


# ── the surfaces the predicate is verified for are a closed, documented set ──


def test_the_owner_dm_surfaces_are_channels_that_draw_targets_from_configured_state():
    """Membership asserts two verified facts per surface (a ``direct`` DM key whose
    peer is spelled as the ``user:`` target, and a roster drawn from configuration
    alone), so a surface that learns identities from inbound traffic can never be
    in the set."""
    from kiro_crew.constants import CHANNEL_OWNER_DM_NAMESPACES

    assert sc.OWNER_DM_CONDUCTOR_SURFACES == frozenset({"discord", "telegram"})
    assert sc.OWNER_DM_CONDUCTOR_SURFACES <= set(CHANNEL_OWNER_DM_NAMESPACES)


def test_sole_direct_target_is_the_one_owner_rule():
    from kiro_crew.dashboard.handlers.messaging import _owner_dm_target
    from kiro_crew.messaging.transport import sole_direct_target

    one = _Transport("discord", users=[OWNER], threads=[THREAD])
    many = _Transport("discord", users=[OWNER, GUEST])
    assert sole_direct_target(one.configured_targets()) == f"user:{OWNER}"
    assert sole_direct_target(many.configured_targets()) == ""
    assert _owner_dm_target(one) == sole_direct_target(one.configured_targets())
    assert _owner_dm_target(many) == sole_direct_target(many.configured_targets())
