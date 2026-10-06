"""An owner-DM channel session may act as a conductor; every other channel caller stays contained.

Three gates decide whether a caller's turns may reach another session or the work
ledger: the creator gate (``_refuse_ineligible_creator``), the target gate
(``authorize_target``'s caller half) and the ledger gate
(``handlers/work_ledger._caller_key``). Before this predicate existed they keyed
on three different facts — the live ``linked_session_key``, the key prefix, the
mirror store — and refused every channel-born session alike, which made a Discord
or Telegram DM whose only human is the configured owner unusable as a conductor.

The suite pins the one predicate all three now consult (``owner_dm_refusal``, the
clause walk that answers ``""`` for an admitted owner DM and otherwise names the
first fact that failed), its fail-closed edges, and that the gates cannot disagree
about a slot -- for both shapes that reach the owner's own DM: the channel-born DM
session itself, and a dashboard-born session whose outbound mirror is that DM, whose
peer the predicate reads off the transport's own record (``direct_peer_of``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import work_ledger as ledger_routes
from kiro_crew.messaging.link import ChannelLink, parse_session_key, release_conversation_location
from kiro_crew.messaging.transport import ConfiguredChannelTarget
from kiro_crew.session import _opt_out_key
from kiro_crew.session_map import MIRROR_OPT_OUT_FLAG, SessionMap

OWNER = "123456789012345678"
GUEST = "111111111111111111"
THREAD = "987654321098765432"

DISCORD_DM = f"discord:kirocrew-conductor:direct:{OWNER}:gen5"
DISCORD_THREAD = f"discord:kirocrew-conductor:group:{THREAD}:gen2"
TELEGRAM_DM = f"telegram:kirocrew:direct:{OWNER}:gen4"
TELEGRAM_FORUM = "telegram:kirocrew:forum:-1001:77:gen1"

DISCORD_DM_CONVERSATION = ChannelLink("discord", channel_id="dm-channel-4242")
TELEGRAM_DM_CONVERSATION = ChannelLink("telegram", channel_id=OWNER)

#: The predicate's own words for the two clauses several tests pin.
NOT_THE_SOLE_OWNER = "the channel's roster does not name this conversation's peer as its sole owner"
MIRROR_ELSEWHERE = "the outbound mirror points somewhere other than this conversation"
SLACK_THREAD_BESIDE = "the session also mirrors to a Slack thread"


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
    """The two transport surfaces the predicate reads: ``configured_targets()`` and
    ``direct_peer_of()``.

    Shaped like the real Discord and Telegram transports — one ``user:`` target
    per allow-listed identity, one ``thread:`` target per allow-listed thread, and
    a DM conversation names its peer only when this process holds the pairing
    (Discord's ``create_dm_channel`` / inbound-DM record; Telegram's identity
    ``chat_id == user_id``, which a test spells as ``{peer: peer}``).
    """

    def __init__(
        self, channel_type: str, users=(), threads=(), *, available=True, dm_pairings=None
    ) -> None:
        self.channel_type = channel_type
        # ``max_message_chars`` / ``max_message_bytes`` are what the delivery leg's
        # chunker reads; 0 bytes = not byte-capped, the character path.
        self.capabilities = SimpleNamespace(
            supports_proactive_send=True, max_message_chars=4096, max_message_bytes=0
        )
        self._users = list(users)
        self._threads = list(threads)
        self._available = available
        self._dm_pairings = dict(dm_pairings or {})
        # The send surface the cross-surface ladder consults when a test drives the
        # publisher itself: permissive recipient check (the refusal path is
        # ``test_channel_transport_outbound_authz``'s), recorded sends.
        self.send_message = AsyncMock(return_value="mid-1")

    def may_send_to(self, conversation_id: str, thread_id=None, principal: str = "") -> bool:
        return True

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        targets = [
            ConfiguredChannelTarget(f"user:{u}", f"DM · {u}", available=self._available)
            for u in self._users
        ]
        targets.extend(
            ConfiguredChannelTarget(f"thread:{t}", f"thread · {t}") for t in self._threads
        )
        return targets

    def direct_peer_of(self, conversation_id: str) -> str:
        return self._dm_pairings.get(conversation_id, "")


def _state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    # The session manager's in-memory record of the conversation each channel
    # session was started from, as the dispatchers write it on every inbound turn.
    origins: dict[str, ChannelLink] = {}
    state.sessions.set_origin_link = MagicMock(side_effect=origins.__setitem__)
    state.sessions.get_origin_link = MagicMock(side_effect=origins.get)
    state.push_slots_update = MagicMock()
    _with_real_mirror_store(state, tmp_path, monkeypatch)
    return state


def _with_real_mirror_store(state, tmp_path, monkeypatch) -> SessionMap:
    """Back the mirror rows with a real ``SessionMap`` rather than the helper's dict.

    The shared double keeps mirrors in a plain dict, so an unlink there leaves
    nothing behind. The real map is what the predicate reads in production, and
    its shape is the point: a channel session's first turn writes the namespaced
    bucket into the legacy ``slack_channel_id`` field, ``clear_mirror_link`` pops
    only ``mirror``, and ``get_mirror_link`` must read that threadless row as no
    mirror at the source -- the row every unlinked channel session carries. The two
    ``SessionManager`` one-liners the dispatchers and the in-channel unlink call
    are re-spelled on the double the way the manager spells them.
    """
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    store = SessionMap()
    sessions = state.sessions
    for name in (
        "set_slack_link",
        "get_slack_link",
        "clear_slack_link",
        "set_mirror_link",
        "get_mirror_link",
        "clear_mirror_link",
        "clear_mirror_links_at",
        "find_mirror_sessions",
        "mirror_accepts_inbound",
        "batched_save",
    ):
        setattr(sessions, name, getattr(store, name))

    async def _set_channel(key: str, channel_id: str) -> None:
        _set_channel_bucket(store, key, channel_id)

    sessions.set_channel = _set_channel
    sessions.set_mirror_opt_out = lambda key, opted_out: store.set_flag(
        _opt_out_key(key), MIRROR_OPT_OUT_FLAG, opted_out
    )
    sessions.mirror_opt_out = lambda key: store.get_flag(_opt_out_key(key), MIRROR_OPT_OUT_FLAG)
    state.sessions._store = store
    return store


def _set_channel_bucket(store: SessionMap, key: str, channel_id: str) -> None:
    """``SessionManager.set_channel``, as the manager spells it: the bucket lands in
    the legacy ``slack_channel_id`` field beside whatever thread the row already
    names (none, for a channel session)."""
    thread_ts, _ = store.get_slack_link(key)
    store.set_slack_link(key, thread_ts or "", channel_id)


def _channel_slot(state, session_key: str, *, origin: ChannelLink | None, mirror=...):
    """A channel-born slot the way ``surface_channel_session`` builds one, after the
    dispatcher's first inbound turn.

    That turn stamps the conversation's namespaced bucket on the session
    (``set_channel``), records *origin* -- the conversation the session lives in --
    and binds it as the outbound mirror; *mirror* is that binding, equal to the
    origin unless a test retargets it.
    """
    name = session_key.replace(":", "_")
    slot = state.get_or_create_slot(name, linked_session_key=session_key, channel_origin=True)
    parsed = parse_session_key(session_key)
    peer = parsed.scope[0] if parsed is not None and parsed.scope else session_key
    _set_channel_bucket(
        state.sessions._store, session_key, f"{session_key.split(':', 1)[0]}:{peer}"
    )
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


def _owner_discord(state, *, dm_pairings=None) -> None:
    state.register_channel_transport(
        _Transport("discord", users=[OWNER], threads=[THREAD], dm_pairings=dm_pairings)
    )


def _owner_telegram(state, *, dm_pairings=None) -> None:
    state.register_channel_transport(_Transport("telegram", users=[OWNER], dm_pairings=dm_pairings))


#: The pairing each transport holds for the owner's DM once it has opened it (the
#: dashboard link's ``resolve_configured_target`` -> ``create_dm_channel``) or
#: received a message from it: Discord's is the DM channel id the platform
#: returned, Telegram's is the identity a private ``chat_id`` already carries.
DISCORD_DM_PAIRING = {DISCORD_DM_CONVERSATION.channel_id: OWNER}
TELEGRAM_DM_PAIRING = {OWNER: OWNER}


def _mirrored_slot(state, name: str, mirror: ChannelLink | None):
    """A dashboard-born slot the dashboard's connect row gave an outbound mirror,
    bound on the key the mirror leg and the containment probe read it under."""
    slot = state.get_or_create_slot(name)
    if mirror is not None:
        state.sessions.set_mirror_link(slot_history_key(slot), mirror)
    return slot


def _in_runner_turn(slot) -> None:
    """Put *slot* inside a dashboard-runner turn the way ``_run_chat`` does: it
    publishes the turn's session identity on the slot once the turn is admitted and
    retires it in the same teardown that empties ``_steer_audience_fences``. The
    audience record is written only under this marker, so a test that asserts a
    record simulates the turn its reader is calling from."""
    slot._active_turn_session_key = slot_history_key(slot)


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

    assert sc.owner_dm_refusal(state, thread) == "the conversation is not a 1:1 direct message"
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

    assert sc.owner_dm_refusal(state, topic) == "the conversation is not a 1:1 direct message"
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

    assert sc.owner_dm_refusal(state, dm) == ""
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
    assert sc.owner_dm_refusal(state, dm) == ""


# ── the predicate fails CLOSED on every edge it cannot answer ──


def test_a_second_allow_listed_identity_removes_the_owner(tmp_path, monkeypatch):
    """Same one-identity rule as `/sessions` and the owner notice: an allow-list
    is a list of people permitted to talk to the agent, not a claim that any one
    of them is the operator, so two entries name nobody."""
    state = _state(tmp_path, monkeypatch)
    state.register_channel_transport(_Transport("discord", users=[OWNER, GUEST]))
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    assert sc.owner_dm_refusal(state, dm) == NOT_THE_SOLE_OWNER
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(dm)))
    assert exc.value.code == "linked_session_caller"


def test_a_dm_with_someone_other_than_the_sole_owner_is_refused(tmp_path, monkeypatch):
    """The DM peer has to BE the configured identity, not merely a DM."""
    state = _state(tmp_path, monkeypatch)
    state.register_channel_transport(_Transport("discord", users=[GUEST]))
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    assert sc.owner_dm_refusal(state, dm) == NOT_THE_SOLE_OWNER


def test_an_unavailable_or_absent_transport_names_no_owner(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    assert sc.owner_dm_refusal(state, dm) == "the discord channel is not running"
    state.register_channel_transport(_Transport("discord", users=[OWNER], available=False))
    assert sc.owner_dm_refusal(state, dm) == NOT_THE_SOLE_OWNER, "the only target is unavailable"


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
    assert sc.owner_dm_refusal(state, to_thread) == MIRROR_ELSEWHERE
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(to_thread)))
    assert exc.value.code == "linked_session_caller"

    state.sessions.set_mirror_link(DISCORD_DM, ChannelLink("telegram", channel_id="7"))
    assert sc.owner_dm_refusal(state, to_thread) == MIRROR_ELSEWHERE

    # Back to its own conversation: the same audience again.
    state.sessions.set_mirror_link(DISCORD_DM, DISCORD_DM_CONVERSATION)
    assert sc.owner_dm_refusal(state, to_thread) == ""
    # And with the binding cleared the only audience is the DM itself -- the first
    # turn's threadless bucket row is still on the entry, and the store reads it as
    # no mirror.
    state.sessions.clear_mirror_link(DISCORD_DM)
    assert state.sessions.get_slack_link(DISCORD_DM) == ("", f"discord:{OWNER}")
    assert state.sessions.get_mirror_link(DISCORD_DM) is None
    assert sc.owner_dm_refusal(state, to_thread) == ""


def test_an_unknown_origin_conversation_fails_closed(tmp_path, monkeypatch):
    """Without the dispatcher's record of the conversation the session lives in,
    a mirror cannot be told apart from a retarget — so it is not waved through."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    dm = _channel_slot(state, DISCORD_DM, origin=None, mirror=DISCORD_DM_CONVERSATION)
    assert sc.owner_dm_refusal(state, dm) == sc.ORIGIN_NOT_ON_RECORD


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
    assert sc.owner_dm_refusal(state, cold) == ""


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
    assert sc.owner_dm_refusal(state, slot) != ""


def test_an_unreadable_mirror_store_fails_closed(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    state.sessions.get_mirror_link = MagicMock(side_effect=RuntimeError("store unreadable"))
    assert sc.owner_dm_refusal(state, dm) == "the session store is unreadable"


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
    assert sc.owner_dm_refusal(state, slot) != ""


def test_a_dashboard_session_mirrored_to_a_dm_nobody_can_place_is_refused(
    tmp_path, monkeypatch, open_ledger_gate
):
    """The mirror row records a conversation id, not a person. Without the
    transport's own record of which peer that DM belongs to -- the pairing it
    writes when it opens the DM or when a message arrives from it -- a mirror to
    the owner's DM cannot be told from a mirror to any other DM, so all three gates
    refuse it and say what clears it."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)  # roster names the owner; the DM pairing is not on record
    caller = _mirrored_slot(state, "chat-mirrored", DISCORD_DM_CONVERSATION)
    state.get_or_create_slot("chat-peer")

    assert sc.owner_dm_refusal(state, caller) == sc.MIRROR_PEER_NOT_ON_RECORD
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    assert exc.value.code == "mirrored_caller"
    assert sc.MIRROR_PEER_NOT_ON_RECORD in exc.value.message
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-peer", operation="send"
        )
    assert exc.value.code == "mirrored_caller"
    _, refusal = _ledger_gate(state, _key(caller))
    assert refusal is not None and refusal.status == 403
    assert '"channel_session"' in refusal.text
    assert "does not place the mirror" in refusal.text


# ── (e) a dashboard-born slot whose outbound mirror is the owner's own DM ──


@pytest.mark.parametrize(
    ("register", "pairing", "conversation"),
    [
        (_owner_discord, DISCORD_DM_PAIRING, DISCORD_DM_CONVERSATION),
        (_owner_telegram, TELEGRAM_DM_PAIRING, TELEGRAM_DM_CONVERSATION),
    ],
    ids=["discord", "telegram"],
)
def test_a_dashboard_session_mirrored_to_the_owners_dm_conducts(
    tmp_path, monkeypatch, open_ledger_gate, register, pairing, conversation
):
    """The same audience as the channel-born owner DM, reached by the other
    mechanism: a ``chat-*`` conductor the operator linked to their own 1:1 DM so
    they can follow it from a phone. Its mirror's peer is the roster's sole owner,
    the surface is verified, and no wider room is bound beside it -- so it creates a
    worker, sends to it, reads it, stops it, and reaches the ledger."""
    state = _state(tmp_path, monkeypatch)
    register(state, dm_pairings=pairing)
    caller = _mirrored_slot(state, "chat-conductor", conversation)

    assert sc.owner_dm_refusal(state, caller) == ""
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    worker_key = created["target"]
    assert state.get_slot(worker_key)._created_by == caller.key
    for op in ("send", "read", "stop"):
        target = sc.authorize_target(
            state, caller_session_key=_key(caller), target=worker_key, operation=op
        )
        assert target.key == worker_key, op
    key, refusal = _ledger_gate(state, _key(caller))
    assert refusal is None
    assert key == caller.key


@pytest.mark.parametrize(
    ("register", "pairing", "mirror", "why"),
    [
        # A guild thread: the pairing store never holds a guild id, so the transport
        # cannot place it as anyone's DM.
        (_owner_discord, DISCORD_DM_PAIRING, ChannelLink("discord", channel_id=THREAD), None),
        # A DM whose peer is not on the roster at all.
        (
            _owner_discord,
            {"dm-channel-guest": GUEST},
            ChannelLink("discord", channel_id="dm-channel-guest"),
            NOT_THE_SOLE_OWNER,
        ),
        # The owner's DM, on a roster that names a second identity: two entries name
        # nobody, the same one-identity rule the channel-born case applies.
        (
            lambda state, dm_pairings: state.register_channel_transport(
                _Transport("discord", users=[OWNER, GUEST], dm_pairings=dm_pairings)
            ),
            DISCORD_DM_PAIRING,
            DISCORD_DM_CONVERSATION,
            NOT_THE_SOLE_OWNER,
        ),
        # A Telegram forum topic: a thread beside the chat id is a room, not a DM.
        (
            _owner_telegram,
            TELEGRAM_DM_PAIRING,
            ChannelLink("telegram", channel_id="-1001", thread_id="77"),
            "the outbound mirror names a thread, not a 1:1 direct message",
        ),
        # A surface never verified for this, even with a roster of one.
        (
            lambda state, dm_pairings: state.register_channel_transport(
                _Transport("webex", users=["someone@example.com"], dm_pairings=dm_pairings)
            ),
            {"room-1": "someone@example.com"},
            ChannelLink("webex", channel_id="room-1"),
            "the outbound mirror is not on a verified owner-DM surface",
        ),
        # A Slack thread as the mirror itself (the store synthesizes the link).
        (
            _owner_discord,
            DISCORD_DM_PAIRING,
            ChannelLink("slack", channel_id="C0PEOPLE", thread_id="1786300000.000200"),
            "the outbound mirror is not on a verified owner-DM surface",
        ),
    ],
    ids=["guild-thread", "strangers-dm", "two-identities", "forum-topic", "webex", "slack-thread"],
)
def test_a_dashboard_session_mirrored_anywhere_else_is_still_refused(
    tmp_path, monkeypatch, open_ledger_gate, register, pairing, mirror, why
):
    """Every other mirror keeps today's refusal, with today's codes: the exemption
    admits exactly one shape and the clause that failed is named."""
    state = _state(tmp_path, monkeypatch)
    register(state, dm_pairings=pairing)
    caller = _mirrored_slot(state, "chat-mirrored", mirror)
    state.get_or_create_slot("chat-peer")

    reason = sc.owner_dm_refusal(state, caller)
    assert reason != ""
    if why is not None:
        assert reason == why
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    assert exc.value.code == "mirrored_caller"
    for op in ("send", "stop", "read"):
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state, caller_session_key=_key(caller), target="chat-peer", operation=op
            )
        assert exc.value.code == "mirrored_caller", op
    _, refusal = _ledger_gate(state, _key(caller))
    assert refusal is not None and refusal.status == 403
    assert '"channel_session"' in refusal.text


def test_an_unattended_tab_mirrored_to_the_owners_dm_is_not_this_shape(tmp_path, monkeypatch):
    """A cron tab carries a ``cron:`` link the channel-link reading strips, so it
    reaches the dashboard-born shape with a plain key -- and a cron slot is an
    admitted, fenced session-control source. The shape admits the PERSON's tab
    only: a scheduled run's tab mirrored to the owner's DM keeps the mirror refusal
    it meets today, and a workflow result tab is refused as a source anyway."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    state.get_or_create_slot("chat-peer")
    cron = state.get_or_create_slot("cron-job-1", linked_session_key="cron:job-1")
    state.sessions.set_mirror_link(slot_history_key(cron), DISCORD_DM_CONVERSATION)
    # A user-created job with a live authoring tab, so the cron caller passes its
    # own ownership check and the mirror refusal is the one it meets.
    state.crons.list_jobs = MagicMock(
        return_value=[SimpleNamespace(id="job-1", created_by="", session_key="chat-peer")]
    )
    workflow = _mirrored_slot(state, "workflow-run-1", DISCORD_DM_CONVERSATION)

    unattended = "the session is unattended, not a person's dashboard tab"
    assert sc.owner_dm_refusal(state, cron) == unattended
    assert sc.owner_dm_refusal(state, workflow) == unattended
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(cron), target="chat-peer", operation="send"
        )
    assert exc.value.code == "mirrored_caller"
    assert unattended in exc.value.message


def test_a_paused_mirror_to_the_owners_dm_still_admits_the_dashboard_session(tmp_path, monkeypatch):
    """The dashboard's Disconnect row pauses delivery and RETAINS the binding; the
    predicate reads the binding and never ``mirror_paused``, so a paused mirror to
    the owner's DM reads exactly like a live one -- the audience did not change --
    just as the channel-born shape's paused origin mirror does."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    caller = _mirrored_slot(state, "chat-conductor", DISCORD_DM_CONVERSATION)
    state.sessions.is_mirror_paused = MagicMock(return_value=True)
    assert sc.owner_dm_refusal(state, caller) == ""


def test_a_slack_thread_bound_beside_the_owner_dm_mirror_refuses_the_dashboard_session(
    tmp_path, monkeypatch
):
    """The second audience the mirror read cannot see, for the mirrored shape: the
    dashboard's slack-link binds its thread on the slot's effective key beside the
    ``mirror`` row, and ``get_mirror_link`` answers that row alone."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    caller = _mirrored_slot(state, "chat-conductor", DISCORD_DM_CONVERSATION)
    assert sc.owner_dm_refusal(state, caller) == ""

    state.sessions.set_slack_link(slot_history_key(caller), "1786300000.000200", "C0PEOPLE")
    assert sc.owner_dm_refusal(state, caller) == SLACK_THREAD_BESIDE
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    assert exc.value.code == "mirrored_caller"

    state.sessions.clear_slack_link(slot_history_key(caller))
    assert sc.owner_dm_refusal(state, caller) == ""


def test_a_mirror_retargeted_from_the_owners_dm_refuses_and_an_unlink_restores_the_tab(
    tmp_path, monkeypatch
):
    """A live retarget is judged at the next call, exactly as the channel-born
    case is: the exemption rests on the CURRENT mirror row, never on an earlier
    admission. An unlink leaves an ordinary dashboard tab, judged by no channel
    refusal at all."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    caller = _mirrored_slot(state, "chat-conductor", DISCORD_DM_CONVERSATION)
    state.get_or_create_slot("chat-peer")
    assert sc.owner_dm_refusal(state, caller) == ""

    state.sessions.set_mirror_link(
        slot_history_key(caller), ChannelLink("discord", channel_id=THREAD)
    )
    assert sc.owner_dm_refusal(state, caller) == sc.MIRROR_PEER_NOT_ON_RECORD
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    assert exc.value.code == "mirrored_caller"

    state.sessions.set_mirror_link(slot_history_key(caller), DISCORD_DM_CONVERSATION)
    assert sc.owner_dm_refusal(state, caller) == ""

    state.sessions.clear_mirror_link(slot_history_key(caller))
    assert state.sessions.get_mirror_link(slot_history_key(caller)) is None
    # Not the owner's DM -- not a channel conversation at all -- and not refused
    # either: the gates consult the predicate only for a linked or mirrored caller.
    assert sc.owner_dm_refusal(state, caller) == "the session is neither channel-born nor mirrored"
    sc._refuse_ineligible_creator(state, caller)


def test_the_mirrored_shape_fails_closed_on_every_edge_it_cannot_answer(tmp_path, monkeypatch):
    """The same fail-closed edges as the channel-born shape: no transport, an
    unreadable roster, an unavailable owner, a peer lookup that raises, and an
    unreadable store each refuse rather than admit."""
    state = _state(tmp_path, monkeypatch)
    caller = _mirrored_slot(state, "chat-conductor", DISCORD_DM_CONVERSATION)
    assert sc.owner_dm_refusal(state, caller) == "the discord channel is not running"

    state.register_channel_transport(
        _Transport("discord", users=[OWNER], available=False, dm_pairings=DISCORD_DM_PAIRING)
    )
    assert sc.owner_dm_refusal(state, caller) == NOT_THE_SOLE_OWNER

    broken = _Transport("discord", users=[OWNER], dm_pairings=DISCORD_DM_PAIRING)
    broken.configured_targets = MagicMock(side_effect=RuntimeError("roster unreadable"))
    state.register_channel_transport(broken)
    assert sc.owner_dm_refusal(state, caller) == "the discord roster is unreadable"

    raising = _Transport("discord", users=[OWNER], dm_pairings=DISCORD_DM_PAIRING)
    raising.direct_peer_of = MagicMock(side_effect=RuntimeError("pairing unreadable"))
    state.register_channel_transport(raising)
    assert sc.owner_dm_refusal(state, caller) == "the discord channel cannot place the mirror"

    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    assert sc.owner_dm_refusal(state, caller) == ""
    state.sessions.get_mirror_link = MagicMock(side_effect=RuntimeError("store unreadable"))
    assert sc.owner_dm_refusal(state, caller) == "the session store is unreadable"
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    assert exc.value.code == "mirrored_caller"


def test_a_read_records_the_readers_audience_so_a_retarget_before_publication_withholds(
    tmp_path, monkeypatch
):
    """The read is authorized against the reader's audience as it holds NOW, and what
    it returns becomes part of a reply the reader's turn publishes to its mirror
    LATER, resolved live. So ``session_read_message`` records the reader's own
    containment at its gate, and the publisher (``cross_surface_withheld``) withholds
    the cross-surface legs when a constraint newly holds at delivery: the owner-DM
    mirror retargeted to a guild thread, or a Slack thread bound beside it. Exact --
    a mirror that did not move, or moved back, publishes -- and a retarget is also
    refused outright at the reader's next call."""
    from kiro_crew.dashboard.chat_runner import cross_surface_withheld

    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    reader = _mirrored_slot(state, "chat-conductor", DISCORD_DM_CONVERSATION)
    _in_runner_turn(reader)
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(reader)))
    worker_key = created["target"]
    state.get_slot(worker_key).messages.append({"role": "assistant", "content": "private"})

    # The dispatch above recorded the same audience (the creator gate records too);
    # start the read's own record from empty so the assertion is about the read.
    reader._steer_audience_fences.clear()
    result = sc.read_messages(state, caller_session_key=_key(reader), target=worker_key)
    assert [m["content"] for m in result["messages"]] == ["private"]
    assert len(reader._steer_audience_fences) == 1
    assert cross_surface_withheld(state, reader) is False, "nothing moved: publish"

    reader_key = slot_history_key(reader)
    state.sessions.set_mirror_link(reader_key, ChannelLink("discord", channel_id=THREAD))
    assert cross_surface_withheld(state, reader) is True, "retargeted since the read"
    with pytest.raises(sc.SessionControlError) as exc:
        sc.read_messages(state, caller_session_key=_key(reader), target=worker_key)
    assert exc.value.code == "mirrored_caller"

    state.sessions.set_mirror_link(reader_key, DISCORD_DM_CONVERSATION)
    assert cross_surface_withheld(state, reader) is False, "back to the admitted audience"
    state.sessions.set_slack_link(reader_key, "1786300000.000200", "C0PEOPLE")
    assert cross_surface_withheld(state, reader) is True, "a room added beside the DM"

    # Turn-scoped: the teardown empties the record, as it does for a peer steer.
    reader._steer_audience_fences.clear()
    state.sessions.clear_slack_link(reader_key)
    assert cross_surface_withheld(state, reader) is False


def test_a_read_by_an_unmirrored_tab_withholds_a_mirror_gained_before_publication(
    tmp_path, monkeypatch
):
    """The same record for the ordinary shape: a plain dashboard tab reads a peer
    under no mirror, gains one before its reply publishes, and the reply is withheld
    from the channel -- the transcript keeps it."""
    from kiro_crew.dashboard.chat_runner import cross_surface_withheld

    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    reader = state.get_or_create_slot("chat-plain")
    _in_runner_turn(reader)
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(reader)))
    sc.read_messages(state, caller_session_key=_key(reader), target=created["target"])
    assert len(reader._steer_audience_fences) == 1
    assert cross_surface_withheld(state, reader) is False
    state.sessions.set_mirror_link(slot_history_key(reader), DISCORD_DM_CONVERSATION)
    assert cross_surface_withheld(state, reader) is True, "a mirror gained since the read"


def test_every_admission_gate_records_the_audience_not_only_the_transcript_read(
    tmp_path, monkeypatch, open_ledger_gate
):
    """The transcript read is not the only private result a mirrored conductor
    carries into its reply: the roster of created sessions and their titles
    (``session_status``) and the work ledger (``work_ledger_read``, ``work_brief``)
    ride the same admission. The record is therefore written at the ONE caller-side
    gate every session-control verb passes and at the ledger's one gate, never per
    verb -- so a status read or a ledger read alone arms the publisher's fence, and
    a retarget before the reply publishes withholds it."""
    from kiro_crew.dashboard.chat_runner import cross_surface_withheld

    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    reader = _mirrored_slot(state, "chat-conductor", DISCORD_DM_CONVERSATION)
    _in_runner_turn(reader)
    asyncio.run(sc.create_session(state, caller_session_key=_key(reader)))
    reader._steer_audience_fences.clear()

    asyncio.run(sc.created_session_status(state, caller_session_key=_key(reader)))
    assert len(reader._steer_audience_fences) == 1, "the status verb records the audience"
    reader._steer_audience_fences.clear()

    key, refusal = _ledger_gate(state, _key(reader))
    assert refusal is None and key == reader.key
    assert len(reader._steer_audience_fences) == 1, "the ledger gate records the audience"
    assert cross_surface_withheld(state, reader) is False
    state.sessions.set_mirror_link(
        slot_history_key(reader), ChannelLink("discord", channel_id=THREAD)
    )
    assert cross_surface_withheld(state, reader) is True, "retargeted since the ledger read"


def test_polling_records_one_audience_entry_per_turn_not_one_per_call(tmp_path, monkeypatch):
    """A conductor polls a worker every few seconds inside one turn. Re-recording the
    same audience is a no-op -- the entry is keyed by the snapshot itself -- so the
    record holds one entry per DISTINCT containment state the turn passed through,
    each a real change the publisher must see, and never grows with the call count.
    No cap: evicting an admission would turn the fail-closed gate fail-open."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    reader = _mirrored_slot(state, "chat-conductor", DISCORD_DM_CONVERSATION)
    _in_runner_turn(reader)
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(reader)))
    reader._steer_audience_fences.clear()

    for _ in range(25):
        sc.read_messages(state, caller_session_key=_key(reader), target=created["target"])
    assert len(reader._steer_audience_fences) == 1
    (only_key,) = reader._steer_audience_fences
    assert only_key.startswith(sc.AUDIENCE_ADMISSION_KEY_PREFIX)

    # A genuine change is a second entry, and polling under it stays at two.
    state.sessions.clear_mirror_link(slot_history_key(reader))
    for _ in range(25):
        sc.read_messages(state, caller_session_key=_key(reader), target=created["target"])
    assert len(reader._steer_audience_fences) == 2

    # A peer steer's uuid-keyed entry lives beside the admissions untouched.
    reader._steer_audience_fences["0123456789abcdef"] = sc.containment_meta(state, reader)
    sc.read_messages(state, caller_session_key=_key(reader), target=created["target"])
    assert len(reader._steer_audience_fences) == 3


@pytest.mark.parametrize("gate", ["session_control", "work_ledger"])
@pytest.mark.parametrize("retarget_lands_after_read", [1, 2, 3])
def test_a_retarget_landing_between_validation_and_record_is_never_the_admitted_audience(
    tmp_path, monkeypatch, open_ledger_gate, gate, retarget_lands_after_read
):
    """The gate validates the mirror row and records the audience it admitted; if
    those are two reads, a retarget landing between them -- a link handler on
    another thread -- is recorded as the admitted audience without having been
    judged, and the publisher, comparing the live row against that record, finds
    nothing changed and publishes the privately-read transcript into the new room.
    So the admission reads the row ONCE: the row it judged is the row it records,
    and a retarget landing after that read is a difference the publisher sees.

    Driven by a store that answers the owner's DM (A) for the first N reads of the
    reader's row during the admission and a guild thread (B) after -- N walks the
    retarget across every read the gate might make -- and pinned from the outside:
    whatever the gate decided, if it admitted, the audience it recorded is A and a
    reply under the live row B is withheld; and the admission read the row once."""
    from kiro_crew.dashboard.chat_runner import cross_surface_withheld

    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    reader = _mirrored_slot(state, "chat-conductor", DISCORD_DM_CONVERSATION)
    _in_runner_turn(reader)
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(reader)))
    worker_key = created["target"]
    state.get_slot(worker_key).messages.append({"role": "assistant", "content": "private"})
    reader._steer_audience_fences.clear()
    reader_key = slot_history_key(reader)
    retargeted = ChannelLink("discord", channel_id=THREAD)

    real_get_mirror_link = state.sessions.get_mirror_link
    reads = {"n": 0}

    def _racing_get_mirror_link(key: str):
        if key != reader_key:
            return real_get_mirror_link(key)
        reads["n"] += 1
        return DISCORD_DM_CONVERSATION if reads["n"] <= retarget_lands_after_read else retargeted

    monkeypatch.setattr(state.sessions, "get_mirror_link", _racing_get_mirror_link)
    admitted = True
    if gate == "session_control":
        try:
            sc.read_messages(state, caller_session_key=_key(reader), target=worker_key)
        except sc.SessionControlError as exc:
            assert exc.code == "mirrored_caller"
            admitted = False
    else:
        _, refusal = _ledger_gate(state, _key(reader))
        admitted = refusal is None
    reads_during_admission = reads["n"]
    monkeypatch.setattr(state.sessions, "get_mirror_link", real_get_mirror_link)

    # The retarget has landed: the live row is B from here on.
    state.sessions.set_mirror_link(reader_key, retargeted)
    if not admitted:
        # A refusal is always a safe answer -- the gate saw B and said so. Nothing
        # was read, so nothing publishes.
        assert reader._steer_audience_fences == {}
        return
    (recorded,) = reader._steer_audience_fences.values()
    identity = recorded[sc.QUEUED_CONTAINMENT_META_KEY]["mirror_identity"]
    assert sc.mirror_audience(identity) == sc.mirror_audience(
        f"discord:{DISCORD_DM_CONVERSATION.channel_id}:"
    ), "the recorded audience is the row the gate judged, never the retarget"
    assert cross_surface_withheld(state, reader) is True, "a reply under B is withheld"
    # Moved back to the admitted audience, the reply publishes.
    state.sessions.set_mirror_link(reader_key, DISCORD_DM_CONVERSATION)
    assert cross_surface_withheld(state, reader) is False
    # And the mechanism: the admission judged and recorded ONE read, so there is
    # no second read for a retarget to land between.
    assert reads_during_admission == 1, reads_during_admission


@pytest.mark.parametrize("retarget_lands_after_read", [0, 1, 2])
def test_a_retarget_landing_during_publication_never_reaches_the_new_room(
    tmp_path, monkeypatch, retarget_lands_after_read
):
    """The publisher decides whether a reply may cross surfaces and then delivers
    it; if those are two reads of the mirror row, a retarget landing between them
    -- the mirror-link writer runs off the loop, in ``asyncio.to_thread`` -- is
    judged on one row and delivered to another: the decision sees the owner's DM
    the transcript was read under and the send resolves the guild thread that
    replaced it. So the channel-neutral leg reads the binding ONCE, judges that row
    against the turn's recorded admissions and resolves its transport from the same
    row.

    Driven by a store that answers the owner's DM (A) for the first N reads of the
    reader's row during publication and a guild thread (B) after -- N walks the
    retarget across every read the leg might make, including before the first --
    and pinned from the outside: B never receives the reply; when the leg
    delivers, it delivers to A, the room it judged; and the publication read the
    row once."""
    from kiro_crew.dashboard.chat_runner import _deliver_cross_surface_reply

    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    discord = state.get_channel_transport("discord")
    reader = _mirrored_slot(state, "chat-conductor", DISCORD_DM_CONVERSATION)
    _in_runner_turn(reader)
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(reader)))
    worker_key = created["target"]
    state.get_slot(worker_key).messages.append({"role": "assistant", "content": "private"})
    reader._steer_audience_fences.clear()
    # The read under the owner's DM: its admission is what the reply is judged by.
    sc.read_messages(state, caller_session_key=_key(reader), target=worker_key)
    assert len(reader._steer_audience_fences) == 1
    reader_key = slot_history_key(reader)
    retargeted = ChannelLink("discord", channel_id=THREAD)

    real_get_mirror_link = state.sessions.get_mirror_link
    reads = {"n": 0}

    def _racing_get_mirror_link(key: str):
        if key != reader_key:
            return real_get_mirror_link(key)
        reads["n"] += 1
        return DISCORD_DM_CONVERSATION if reads["n"] <= retarget_lands_after_read else retargeted

    monkeypatch.setattr(state.sessions, "get_mirror_link", _racing_get_mirror_link)
    asyncio.run(
        _deliver_cross_surface_reply(state, reader_key, "the private transcript", slot=reader)
    )
    monkeypatch.setattr(state.sessions, "get_mirror_link", real_get_mirror_link)

    rooms = [call.args[0] for call in discord.send_message.await_args_list]
    assert THREAD not in rooms, "the retargeted room never receives the reply"
    if retarget_lands_after_read == 0:
        # The one read already saw B: a constraint newly holds, the leg withholds.
        assert rooms == [], rooms
    else:
        # The one read saw A, the room the admission was judged under: delivered
        # there, and only there.
        assert rooms == [DISCORD_DM_CONVERSATION.channel_id], rooms
    # The mechanism: one read for the decision and the delivery, so there is no
    # second read for a retarget to land between.
    assert reads["n"] == 1, reads["n"]


def test_the_slack_legs_judge_the_thread_they_cached_not_the_live_binding(tmp_path, monkeypatch):
    """The Slack legs post to the ``(channel, thread)`` they read at turn start. An
    operator who unlinks that shared thread mid-turn and mirrors the tab to their own
    DM before the tab reads a peer's transcript leaves a record naming the DM alone;
    the live binding names the DM alone too, so the live comparison
    (``cross_surface_withheld``) finds nothing changed -- and the cached thread, in
    neither side of it, would receive the transcript. The legs therefore also judge
    the destination they actually post to (``slack_publication_withheld``): a room no
    recorded admission saw is withheld, while a thread still linked when the read was
    admitted -- recorded as part of its audience -- publishes as before."""
    from kiro_crew.dashboard.chat_runner import (
        cross_surface_withheld,
        slack_publication_withheld,
    )

    cached_thread, cached_channel = "1786300000.000200", "C0PEOPLE"

    # The unlink-then-mirror shape: the thread was the tab's audience when the turn
    # began, the DM is its audience when the read is admitted. The worker is
    # dispatched first, while the tab is plain -- a threaded tab is no creator.
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    reader = state.get_or_create_slot("chat-conductor")
    reader_key = slot_history_key(reader)
    _in_runner_turn(reader)
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(reader)))
    worker_key = created["target"]
    state.get_slot(worker_key).messages.append({"role": "assistant", "content": "private"})
    reader._steer_audience_fences.clear()
    # Turn start: the Slack legs cache this thread as their destination.
    state.sessions.set_slack_link(reader_key, cached_thread, cached_channel)
    # Mid-turn: the operator unlinks the shared thread and mirrors to their own DM.
    state.sessions.clear_slack_link(reader_key)
    state.sessions.set_mirror_link(reader_key, DISCORD_DM_CONVERSATION)
    sc.read_messages(state, caller_session_key=_key(reader), target=worker_key)
    assert len(reader._steer_audience_fences) == 1
    assert cross_surface_withheld(state, reader) is False, "live binding == admitted: no change"
    assert (
        slack_publication_withheld(state, reader, cached_channel, cached_thread) is True
    ), "the cached thread is a room the admission never saw"
    # The channel-neutral leg's row is the admitted one, so that leg still publishes.
    assert sc.publication_withheld(state, reader, (DISCORD_DM_CONVERSATION, "", "")) is False

    # The ordinary shape: a Slack conversation resumed into the tab records the fence
    # with the thread still bound (``channel_handoff._hold_fence``'s record, the
    # containment holding at admission), so the record carries the room and the legs
    # publish there.
    linked = state.get_or_create_slot("chat-linked")
    linked_key = slot_history_key(linked)
    state.sessions.set_slack_link(linked_key, cached_thread, cached_channel)
    linked._steer_audience_fences["tok"] = sc.containment_meta(state, linked)
    assert slack_publication_withheld(state, linked, cached_channel, cached_thread) is False
    # An unlink AFTER that admission is a narrowing the live comparison already
    # admits; the cached thread was part of the recorded audience.
    state.sessions.clear_slack_link(linked_key)
    assert cross_surface_withheld(state, linked) is False
    assert slack_publication_withheld(state, linked, cached_channel, cached_thread) is False
    # A DIFFERENT thread than the one recorded is a room the admission never saw.
    assert slack_publication_withheld(state, linked, "C0OTHER", cached_thread) is True
    # No record, no destination: nothing withheld.
    linked._steer_audience_fences.clear()
    assert slack_publication_withheld(state, linked, cached_channel, cached_thread) is False
    assert slack_publication_withheld(state, reader, "", "") is False


def test_a_binding_that_moves_after_turn_start_is_judged_per_destination(tmp_path, monkeypatch):
    """The Slack legs publish to the thread cached at turn start and the
    channel-neutral leg to the row it reads at delivery, so the two destinations can
    name different rooms once the binding moves mid-turn. Each is judged as the room
    it is, against every admission the turn recorded:

    * the record was made while the binding named thread B -- the cached thread A
      is a room no admission saw and is refused, while B, admitted, publishes;
    * the record was made under A and the binding then moved to B -- A was admitted
      (the cached destination alone would publish), but B newly holds against the
      record, so the live comparison refuses the Slack legs and the channel-neutral
      leg's one-read judgement refuses the row it would deliver to.

    Either way no room the turn's admissions never saw receives the reply."""
    from kiro_crew.dashboard.chat_runner import (
        cross_surface_withheld,
        slack_publication_withheld,
    )

    thread_a, thread_b, channel = "1786300000.000300", "1786300000.000400", "C0ROOMS"
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("chat-moving")
    key = slot_history_key(slot)

    # Turn start caches A; the operator re-links to B before anything is admitted;
    # the admission (a Slack conversation resumed into the turn) records B.
    state.sessions.set_slack_link(key, thread_a, channel)
    state.sessions.set_slack_link(key, thread_b, channel)
    slot._steer_audience_fences["tok"] = sc.containment_meta(state, slot)
    assert cross_surface_withheld(state, slot) is False, "live binding == admitted"
    assert slack_publication_withheld(state, slot, channel, thread_a) is True, "A: never admitted"
    assert slack_publication_withheld(state, slot, channel, thread_b) is False, "B: admitted"

    # The other order: admitted under A, then moved to B with no new admission.
    slot._steer_audience_fences.clear()
    state.sessions.set_slack_link(key, thread_a, channel)
    slot._steer_audience_fences["tok"] = sc.containment_meta(state, slot)
    state.sessions.set_slack_link(key, thread_b, channel)
    assert slack_publication_withheld(state, slot, channel, thread_a) is False, "A was admitted"
    assert cross_surface_withheld(state, slot) is True, "B newly holds: the live check refuses"
    # The channel-neutral leg judges the row it reads at delivery -- B -- and refuses.
    binding = sc._read_mirror_binding(state.sessions, key)
    assert binding is not None and binding[1] == thread_b
    assert sc.publication_withheld(state, slot, binding) is True
    # Moved back to A, the admitted audience, every leg publishes again.
    state.sessions.set_slack_link(key, thread_a, channel)
    assert cross_surface_withheld(state, slot) is False
    assert (
        sc.publication_withheld(state, slot, sc._read_mirror_binding(state.sessions, key)) is False
    )


@pytest.mark.parametrize("gate", ["session_control", "work_ledger"])
def test_a_channel_turn_records_no_audience_so_a_later_dashboard_reply_is_not_withheld(
    tmp_path, monkeypatch, open_ledger_gate, gate
):
    """A channel-born owner DM conducts from its OWN turns, which the messaging
    driver runs -- not the dashboard runner, whose teardown is the only thing that
    empties the audience record. An admission recorded there would outlive its turn:
    when the operator later changes the slot's mirror and drives a reply from the
    dashboard, the publisher would compare the live row against a stale admission
    that was never about that reply and withhold its mirror leg. So the record is
    written only under the marker the runner publishes for its own turns; a channel
    turn's admission leaves nothing, and nothing is lost -- the dispatcher publishes
    its replies to the conversation the turn came from, and the runner's publisher is
    this record's only reader."""
    from kiro_crew.dashboard.chat_runner import cross_surface_withheld

    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    assert not dm._active_turn_session_key, "no dashboard-runner turn is running"
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(dm)))
    worker_key = created["target"]
    state.get_slot(worker_key).messages.append({"role": "assistant", "content": "private"})

    if gate == "session_control":
        result = sc.read_messages(state, caller_session_key=_key(dm), target=worker_key)
        assert [m["content"] for m in result["messages"]] == ["private"], "admitted"
    else:
        key, refusal = _ledger_gate(state, DISCORD_DM)
        assert refusal is None and key == DISCORD_DM, "admitted"
    # The operator later points the slot's mirror elsewhere and drives a reply from
    # the dashboard: no stale admission stands in its way.
    state.sessions.set_mirror_link(DISCORD_DM, ChannelLink("discord", channel_id=THREAD))
    assert (
        cross_surface_withheld(state, dm) is False
    ), "a later dashboard reply is withheld by an admission that was never about it"
    assert dm._steer_audience_fences == {}, "a channel turn's admission leaves no record"


def test_a_runner_turn_still_records_and_the_marker_is_the_runners_own(tmp_path, monkeypatch):
    """The same channel-born slot driven from the dashboard -- ``_run_chat`` has
    published the turn's identity on it -- records the admission, and the record
    still fences the read-to-publication path. The marker is the runner's own: a
    real dashboard turn (``turn_harness.run_turn``) publishes it while it runs and
    retires it, with the record, when it ends; and the messaging driver that runs
    channel turns never touches it -- pinned at the driver's source, so a driver
    that starts setting it reds this test rather than resurrecting the stale record."""
    import inspect

    from turn_harness import Do, TurnScript, run_turn

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.messaging import dispatch, driver

    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    _in_runner_turn(dm)
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(dm)))
    dm._steer_audience_fences.clear()
    sc.read_messages(state, caller_session_key=_key(dm), target=created["target"])
    assert len(dm._steer_audience_fences) == 1, "a runner turn records the admission"
    state.sessions.set_mirror_link(DISCORD_DM, ChannelLink("discord", channel_id=THREAD))
    assert chat_runner.cross_surface_withheld(state, dm) is True, "and it still fences"

    # A real runner turn: inside it the slot reads as a runner turn, and an
    # admission recorded during it is gone, with the marker, once the turn ends.
    seen: dict = {}

    def _during(ctx) -> None:
        seen["slot"] = ctx.slot
        seen["inside"] = sc._in_runner_turn(ctx.slot)
        ctx.slot._steer_audience_fences["audience:probe"] = {"probe": True}

    asyncio.run(run_turn(TurnScript(events=(Do(_during),))))
    assert seen["inside"] is True, "the runner publishes its marker while the turn runs"
    assert seen["slot"]._active_turn_session_key == "", "the runner retires its marker"
    assert seen["slot"]._steer_audience_fences == {}, "the runner's teardown empties the record"
    for module in (driver, dispatch):
        assert "_active_turn_session_key" not in inspect.getsource(module), module.__name__
    assert "_active_turn_session_key" in inspect.getsource(sc._in_runner_turn)


def test_a_turn_whose_only_call_is_create_session_still_records_its_audience(tmp_path, monkeypatch):
    """``create_session`` (and a fork of the caller's own transcript) never passes
    ``authorize_target``, so the creator gate is the ONE gate such a turn passes --
    and a creation's result, the child's key and title, becomes part of the reply
    exactly as a read does. The creator gate therefore records the verdict's
    admission on its admitted path too, so a conductor turn that only dispatches is
    fenced like one that reads."""
    from kiro_crew.dashboard.chat_runner import cross_surface_withheld

    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    reader = _mirrored_slot(state, "chat-conductor", DISCORD_DM_CONVERSATION)
    _in_runner_turn(reader)
    assert reader._steer_audience_fences == {}
    asyncio.run(sc.create_session(state, caller_session_key=_key(reader)))
    assert len(reader._steer_audience_fences) == 1, "the creator gate records the audience"
    assert cross_surface_withheld(state, reader) is False
    state.sessions.set_mirror_link(
        slot_history_key(reader), ChannelLink("discord", channel_id=THREAD)
    )
    assert cross_surface_withheld(state, reader) is True, "retargeted since the dispatch"


def test_an_admitted_mirrored_tab_keeps_the_reach_it_had_and_a_child_stays_fenced(
    tmp_path, monkeypatch
):
    """The exemption waives the two channel refusals and nothing about the fence.
    A person's own ``chat-*`` tab is not creator-fenced -- its authority is the
    owner's dashboard session, and the mirror changes its audience, not its
    authority -- so it keeps the reach it had before the link. A session an agent
    created stays fenced by its ``_created_by`` mark whether or not it mirrors."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state, dm_pairings=DISCORD_DM_PAIRING)
    tab = _mirrored_slot(state, "chat-conductor", DISCORD_DM_CONVERSATION)
    state.get_or_create_slot("chat-the-persons-own-tab")
    assert sc._caller_is_ownership_fenced(state, tab.key) is False
    for op in ("send", "read", "stop"):
        target = sc.authorize_target(
            state, caller_session_key=_key(tab), target="chat-the-persons-own-tab", operation=op
        )
        assert target.key == "chat-the-persons-own-tab", op

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(tab)))
    child = _mirrored_slot(state, created["target"], DISCORD_DM_CONVERSATION)
    assert sc.owner_dm_refusal(state, child) == ""
    assert sc._caller_is_ownership_fenced(state, child.key) is True
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state,
            caller_session_key=_key(child),
            target="chat-the-persons-own-tab",
            operation="read",
        )
    assert exc.value.code == "not_creator"


@pytest.mark.parametrize(
    ("pairing", "mirror", "users"),
    [
        (DISCORD_DM_PAIRING, DISCORD_DM_CONVERSATION, [OWNER]),
        (DISCORD_DM_PAIRING, ChannelLink("discord", channel_id=THREAD), [OWNER]),
        ({}, DISCORD_DM_CONVERSATION, [OWNER]),
        (DISCORD_DM_PAIRING, DISCORD_DM_CONVERSATION, [OWNER, GUEST]),
    ],
    ids=["owner-dm", "guild-thread", "no-pairing", "two-identities"],
)
def test_the_ledger_gate_and_session_control_agree_on_a_mirrored_dashboard_slot(
    tmp_path, monkeypatch, open_ledger_gate, pairing, mirror, users
):
    """One predicate for the mirrored shape too: the ledger reaches it through
    ``session_owner_dm_refusal`` over the slot ``caller_slot_key`` resolves, so the
    creator gate and the ledger gate cannot disagree about a ``chat-*`` slot."""
    state = _state(tmp_path, monkeypatch)
    state.register_channel_transport(
        _Transport("discord", users=users, threads=[THREAD], dm_pairings=pairing)
    )
    caller = _mirrored_slot(state, "chat-conductor", mirror)
    state.get_or_create_slot("chat-peer")

    why = sc.owner_dm_refusal(state, caller)
    assert sc.session_owner_dm_refusal(state, _key(caller)) == why
    verdict = why == ""

    creator_allowed = True
    try:
        sc._refuse_ineligible_creator(state, caller)
    except sc.SessionControlError:
        creator_allowed = False
    assert creator_allowed is verdict

    _, refusal = _ledger_gate(state, _key(caller))
    assert (refusal is None) is verdict


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

    why = sc.owner_dm_refusal(state, slot)
    assert sc.session_owner_dm_refusal(state, session_key) == why
    verdict = why == ""

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
        ledger_routes._contained_channel_caller(request, DISCORD_DM) == MIRROR_ELSEWHERE
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


def test_an_owner_dm_stays_admitted_after_it_unlinks_its_own_mirror(
    tmp_path, monkeypatch, open_ledger_gate
):
    """``!unlink`` / ``/unlink`` in the DM: the opt-out is persisted and the mirror
    binding released, exactly as the dispatchers do it. The row the store is left
    with is NOT empty -- the first turn's ``set_channel`` left the namespaced bucket
    in the legacy ``slack_channel_id`` field with no thread -- and ``get_mirror_link``
    reads it as no mirror at the source: bookkeeping nobody can deliver through is
    no audience, so the DM must still conduct through all three gates."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    state.get_or_create_slot("chat-peer")
    assert sc.owner_dm_refusal(state, dm) == ""

    with state.sessions.batched_save():
        state.sessions.set_mirror_opt_out(DISCORD_DM, True)
        reply, _swept = release_conversation_location(
            state.sessions, key=DISCORD_DM, location=DISCORD_DM_CONVERSATION, channel="discord"
        )
    assert reply == "✅ Unlinked."
    # The bucket row is still there -- ``clear_mirror_link`` popped only ``mirror``
    # -- and the store reads it as no mirror, filtering the threadless Slack row
    # at the source rather than handing every reader a link nobody chose.
    assert state.sessions.get_slack_link(DISCORD_DM) == ("", f"discord:{OWNER}")
    assert state.sessions.get_mirror_link(DISCORD_DM) is None
    assert state.sessions.mirror_opt_out(DISCORD_DM) is True, "the next turn will not rebind"

    assert sc.owner_dm_refusal(state, dm) == ""
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(dm)))
    worker_key = created["target"]
    for op in ("send", "read", "stop"):
        target = sc.authorize_target(
            state, caller_session_key=_key(dm), target=worker_key, operation=op
        )
        assert target.key == worker_key, op
    key, refusal = _ledger_gate(state, DISCORD_DM)
    assert refusal is None
    assert key == DISCORD_DM
    # A REAL Slack mirror -- one that names a thread -- is a second audience and
    # refuses, by the clause that reads the thread itself (the same row read through
    # ``get_mirror_link`` would also fail the mirror clause; the thread clause runs
    # first because it is the one that still sees the thread when a ``mirror`` row
    # sits beside it, see the next test).
    state.sessions.set_slack_link(DISCORD_DM, "1786300000.000200", "C0THREADED")
    assert sc.owner_dm_refusal(state, dm) == SLACK_THREAD_BESIDE


def test_a_slack_thread_bound_beside_the_origin_mirror_is_a_second_audience(
    tmp_path, monkeypatch, open_ledger_gate
):
    """The dashboard's slack-link on a channel-born slot writes the thread onto the
    slot's EFFECTIVE key -- the channel session itself (``DashboardState.link_slack``)
    -- while the origin mirror row stays. ``get_mirror_link`` then still answers the
    origin, because the explicit ``mirror`` row wins over Slack fields it never
    reads, so the mirror clause alone keeps admitting a DM whose every dashboard
    turn the runner also posts into that thread. The thread is read on its own and
    refuses all three gates; unlinking it restores the DM, since the mirror row was
    never the problem."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    state.get_or_create_slot("chat-peer")
    assert sc.owner_dm_refusal(state, dm) == ""
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(dm)))
    worker_key = created["target"]

    state.link_slack(dm.key, "1786300000.000300", "C0OPSROOM")
    # The shadowing this test exists for: the mirror read is unchanged by the link.
    assert state.sessions.get_mirror_link(DISCORD_DM) == DISCORD_DM_CONVERSATION
    assert state.sessions.get_slack_link(DISCORD_DM) == ("1786300000.000300", "C0OPSROOM")

    assert sc.owner_dm_refusal(state, dm) == SLACK_THREAD_BESIDE
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(dm)))
    assert exc.value.code == "linked_session_caller"
    for op in ("send", "read", "stop"):
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(state, caller_session_key=_key(dm), target=worker_key, operation=op)
        assert exc.value.code == "linked_session_caller", op
    key, refusal = _ledger_gate(state, DISCORD_DM)
    assert key is None and refusal is not None
    assert refusal.status == 403
    assert SLACK_THREAD_BESIDE in refusal.text

    assert state.sessions.clear_slack_link(DISCORD_DM) is True
    assert sc.owner_dm_refusal(state, dm) == ""
    assert (
        sc.authorize_target(
            state, caller_session_key=_key(dm), target=worker_key, operation="read"
        ).key
        == worker_key
    )


def test_a_slack_thread_bound_while_an_entry_waited_is_a_retarget_at_the_drain(
    tmp_path, monkeypatch
):
    """The queue drain compares the audience an entry was admitted under with the
    audience holding at delivery (``containment_snapshot`` / ``newly_held_constraints``).
    That comparison reads the same store the predicate reads, and the same
    shadowing applies: ``get_mirror_link`` answers the origin mirror alone, so a Slack
    thread the dashboard binds onto the channel session while an entry waits would
    leave the identity unchanged and the drain would post the reply into a room the
    admission never saw. The probe composes its identity from both accessors."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    admitted = sc.containment_meta(state, dm)
    before = sc.containment_snapshot(state, dm, on_probe_failure=True)
    assert before["mirrored"] is True and before["mirror_identity"] == "discord:dm-channel-4242:"
    assert sc.newly_held_constraints(before, admitted) == []

    state.link_slack(dm.key, "1786300000.000400", "C0OPSROOM")
    assert state.sessions.get_mirror_link(DISCORD_DM) == DISCORD_DM_CONVERSATION
    now = sc.containment_snapshot(state, dm, on_probe_failure=True)
    assert now["mirror_identity"] == "discord:dm-channel-4242:|slack:C0OPSROOM:1786300000.000400"
    assert sc.newly_held_constraints(now, admitted) == ["mirror_retarget"]

    # Unbinding the thread restores the admitted audience exactly.
    state.sessions.clear_slack_link(DISCORD_DM)
    after = sc.containment_snapshot(state, dm, on_probe_failure=True)
    assert sc.newly_held_constraints(after, admitted) == []


def test_a_slack_thread_unlinked_while_an_entry_waited_narrows_the_audience_and_drains(
    tmp_path, monkeypatch
):
    """The converse of the test above. An entry admitted while the DM mirrored to
    its origin AND to a Slack thread reaches, after the thread is unlinked, only a
    room its admission already saw -- a dropped mirror is a narrowing, not a
    retarget, and the drain must not fire ``mirror_retarget`` on it. Compared as one
    string, the shrunken identity is simply "different" and the entry is dropped.
    A rebind to a DIFFERENT thread is a room the admission never saw and still
    drops."""
    state = _state(tmp_path, monkeypatch)
    _owner_discord(state)
    dm = _channel_slot(state, DISCORD_DM, origin=DISCORD_DM_CONVERSATION)
    state.link_slack(dm.key, "1786300000.000400", "C0OPSROOM")
    admitted = sc.containment_meta(state, dm)
    assert admitted[sc.QUEUED_CONTAINMENT_META_KEY]["mirror_identity"] == (
        "discord:dm-channel-4242:|slack:C0OPSROOM:1786300000.000400"
    )

    assert state.sessions.clear_slack_link(DISCORD_DM) is True
    narrowed = sc.containment_snapshot(state, dm, on_probe_failure=True)
    assert narrowed["mirrored"] is True
    assert narrowed["mirror_identity"] == "discord:dm-channel-4242:"
    assert sc.newly_held_constraints(narrowed, admitted) == []

    state.link_slack(dm.key, "1786300000.000900", "C0OPSROOM")
    rebound = sc.containment_snapshot(state, dm, on_probe_failure=True)
    assert (
        rebound["mirror_identity"] == "discord:dm-channel-4242:|slack:C0OPSROOM:1786300000.000900"
    )
    assert sc.newly_held_constraints(rebound, admitted) == ["mirror_retarget"]


def test_the_drain_compares_mirror_rooms_as_a_set():
    """The comparison rule on its own inputs: a room the admission never saw fires
    ``mirror_retarget`` (retarget or widening); a subset, an unchanged set or a
    reordering does not; an empty drain-time identity is the boolean's business."""
    admitted = {sc.QUEUED_CONTAINMENT_META_KEY: {"mirrored": True, "mirror_identity": "a|b"}}

    def held(identity: str) -> list[str]:
        return sc.newly_held_constraints({"mirrored": True, "mirror_identity": identity}, admitted)

    assert held("a|b") == []
    assert held("b|a") == []
    assert held("a") == []
    assert held("b") == []
    assert held("") == []
    assert held("a|c") == ["mirror_retarget"]
    assert held("c") == ["mirror_retarget"]
    assert held("a|b|c") == ["mirror_retarget"]
    assert sc.mirror_audience("a|b") == frozenset({"a", "b"})
    assert sc.mirror_audience("") == frozenset()
    assert sc.mirror_audience(None) == frozenset()
    assert sc.mirror_audience("teams:19:x@thread.v2:") == frozenset({"teams:19:x@thread.v2:"})


def test_the_probe_reads_a_store_without_a_thread_accessor_as_no_thread():
    """The probe's second read must not turn a store that lacks ``get_slack_link``,
    or answers it in another shape (the shared ``MagicMock`` session double), into an
    unverifiable probe -- that would make every queued entry on every such store a
    fail-closed drop. Only a raising store fails the probe, as a raising mirror read
    always did."""
    slot = SimpleNamespace(key="chat-1", linked_session_key="")
    mirror = ChannelLink("discord", channel_id="c1")

    without = SimpleNamespace(get_mirror_link=lambda key: mirror)
    assert sc._probe_channel_mirror(SimpleNamespace(sessions=without), slot) == "discord:c1:"

    odd_shape = SimpleNamespace(get_mirror_link=lambda key: mirror, get_slack_link=MagicMock())
    assert sc._probe_channel_mirror(SimpleNamespace(sessions=odd_shape), slot) == "discord:c1:"

    threadless = SimpleNamespace(
        get_mirror_link=lambda key: mirror, get_slack_link=lambda key: ("", "discord:u1")
    )
    assert sc._probe_channel_mirror(SimpleNamespace(sessions=threadless), slot) == "discord:c1:"

    def _raise(key):
        raise RuntimeError("store down")

    raising = SimpleNamespace(get_mirror_link=lambda key: mirror, get_slack_link=_raise)
    assert sc._probe_channel_mirror(SimpleNamespace(sessions=raising), slot) is None


def test_the_predicate_reads_every_audience_accessor_the_reply_delivery_legs_read():
    """The dashboard delivers a session's reply to its outbound audiences through two
    legs, and each reads the audience off the store itself: the non-Slack mirror
    through ``_resolve_mirror_target`` (``get_mirror_link``) and the Slack thread
    through ``_deliver_linked_slack_message`` (``get_slack_link``). The predicate
    must consult every accessor those legs consult -- a row the store hands to one
    reader and not the other is an audience the gate cannot see, which is exactly
    the Slack-thread gap this suite pins. Read off the source, so a delivery leg
    that starts reading a new accessor reds this test rather than the operator."""
    import inspect
    import re

    from kiro_crew.dashboard import chat_runner

    accessor = re.compile(r"\b(get_\w+_link)\b")
    delivery_reads: set[str] = set()
    for leg in (
        chat_runner._resolve_mirror_target,
        chat_runner._deliver_cross_surface_reply,
        chat_runner._deliver_linked_slack_message,
    ):
        delivery_reads |= set(accessor.findall(inspect.getsource(leg)))
    # The predicate and the probe rest on ONE shared read of the binding, so the
    # accessors to check are that read's (the thread through its helper).
    shared_read = inspect.getsource(sc._read_mirror_binding) + inspect.getsource(
        sc._slack_thread_of
    )
    shared_reads = set(accessor.findall(shared_read))
    assert {"get_mirror_link", "get_slack_link"} <= delivery_reads, sorted(delivery_reads)
    assert delivery_reads <= shared_reads, sorted(delivery_reads - shared_reads)
    # Both consumers take that read and no other: the predicate's clauses judge the
    # tuple it returns and the audience recorded beside the verdict is built from
    # the same tuple, so a retarget cannot land between a judged row and a
    # recorded one; the drain-side probe (``containment_snapshot``) judges the same
    # audience after the fact through the same read. A CALL is what counts -- the
    # consumers name an accessor in a presence check without reading through it.
    call = re.compile(r"\.(get_\w+_link)\(")
    for consumer in (sc.judge_owner_dm, sc._probe_channel_mirror_for_key):
        source = inspect.getsource(consumer)
        assert "_read_mirror_binding(" in source, consumer.__name__
        assert not call.findall(
            source
        ), f"{consumer.__name__} reads a store accessor beside the shared read"
    assert call.findall(inspect.getsource(sc._owner_dm_clauses)) == ["get_origin_link"], (
        "the clauses may read the origin (a validation input the record does not "
        "carry) and nothing else off the store"
    )


def test_every_owner_dm_surface_opens_the_crew_log_from_its_dispatcher():
    """A surface joins ``OWNER_DM_CONDUCTOR_SURFACES`` by verifying the facts the
    predicate rests on -- and by opening its own sessions' crew logs, because the
    work ledger an admitted DM holds appends to that log and rolls back a write with
    nowhere to land. The dashboard runner opens logs for its own turns only; a
    channel dispatcher runs its own turn loop and must call the shared opener, with
    the predecessor read that keeps a recycled conversation's history on the
    succession chain, and the workspace read off the slot the dashboard surfaces
    the conversation under -- the source a tab on it states the same fact from, so
    the two writers of one log never take turns recording a move. Pinned off the
    dispatcher's source, so the next surface admitted as a conductor cannot re-open
    the gaps this suite's opener tests closed. A dispatcher riding the shared channel
    pipeline declares ``Drift.OPENS_CREW_LOG`` and the pipeline makes the call."""
    import ast
    import importlib
    import inspect

    from kiro_crew.messaging import dispatch as pipeline

    for surface in sorted(sc.OWNER_DM_CONDUCTOR_SURFACES):
        dispatcher = importlib.import_module(f"kiro_crew.{surface}.transport_dispatch")
        source = inspect.getsource(dispatcher)
        if "ChannelTurns(" in source:
            # The drift handed over is the declared set itself, not one with the
            # opener carved out of it (``DISCORD_DRIFT - {...}`` reads as passed).
            drifts = [
                kw.value
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ChannelTurns"
                for kw in node.keywords
                if kw.arg == "drift"
            ]
            assert drifts and all(
                isinstance(value, ast.Name) and value.id == f"{surface.upper()}_DRIFT"
                for value in drifts
            ), f"{surface}: ChannelTurns is not handed {surface.upper()}_DRIFT as declared"
            drift = getattr(pipeline, f"{surface.upper()}_DRIFT", frozenset())
            assert pipeline.Drift.OPENS_CREW_LOG in drift, f"{surface}: pipeline opens no log"
            source = inspect.getsource(pipeline)
        assert "open_turn_crew_log(" in source, f"{surface}: dispatcher opens no crew log"
        assert "predecessor_sid(" in source, f"{surface}: opener would cite no predecessor"
        assert "workspace=slot_workspace(" in source, f"{surface}: opener states no workspace"


@pytest.mark.asyncio
async def test_mirror_unlink_clears_the_mirror_and_nothing_else(tmp_path, monkeypatch):
    """The dashboard's unlink clears the OUTBOUND mirror binding only. The slot
    stays channel-born (``linked_session_key`` untouched), so it neither detaches
    the session from its channel nor changes what the gates decide about it: a
    thread session is refused before and after, an owner DM is admitted before
    and after.

    "Cleared" is the store's word, not an empty row: the first turn's
    ``set_channel`` left the namespaced bucket in the legacy ``slack_channel_id``
    field, and ``get_mirror_link`` filters that threadless Slack row at the source,
    so every reader -- the gates, ``bind_origin_mirror``, the link projection --
    sees no mirror, and the unlink that "changes nothing" cannot refuse the owner's
    own DM.
    """
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
        assert state.sessions.get_slack_link(DISCORD_THREAD) == ("", f"discord:{THREAD}")
        assert state.sessions.get_mirror_link(DISCORD_THREAD) is None
        assert thread.linked_session_key == DISCORD_THREAD
        assert sc.owner_dm_refusal(state, thread) == "the conversation is not a 1:1 direct message"
        with pytest.raises(sc.SessionControlError) as exc:
            await sc.create_session(state, caller_session_key=_key(thread))
        assert exc.value.code == "linked_session_caller"

        resp = await client.post(f"/api/chat/slots/{dm.key}/mirror-unlink")
        assert resp.status == 200
        assert (await resp.json())["was_linked"] is True
        assert state.sessions.get_slack_link(DISCORD_DM) == ("", f"discord:{OWNER}")
        assert state.sessions.get_mirror_link(DISCORD_DM) is None
        assert dm.linked_session_key == DISCORD_DM
        assert sc.owner_dm_refusal(state, dm) == ""
        # A second unlink reaches the bucket row itself (``clear_mirror_link`` falls
        # through to ``clear_slack_link`` once no ``mirror`` is left); the read was
        # None before and after, and the DM is admitted the same either way.
        resp = await client.post(f"/api/chat/slots/{dm.key}/mirror-unlink")
        assert (await resp.json())["was_linked"] is True
        assert state.sessions.get_slack_link(DISCORD_DM) == (None, None)
        assert state.sessions.get_mirror_link(DISCORD_DM) is None
        assert sc.owner_dm_refusal(state, dm) == ""


# ── the surfaces the predicate is verified for are a closed, documented set ──


def test_the_owner_dm_surfaces_are_channels_that_draw_targets_from_configured_state():
    """Membership asserts three verified facts per surface (a ``direct`` DM key whose
    peer is spelled as the ``user:`` target, a roster drawn from configuration
    alone, and a transport that attests a DM conversation's peer from its own
    state), so a surface that learns identities from inbound traffic can never be
    in the set, and one that cannot place a DM never admits a mirrored tab."""
    import importlib
    import inspect

    from kiro_crew.constants import CHANNEL_OWNER_DM_NAMESPACES
    from kiro_crew.messaging.transport import MessagingTransport

    assert sc.OWNER_DM_CONDUCTOR_SURFACES == frozenset({"discord", "telegram"})
    assert sc.OWNER_DM_CONDUCTOR_SURFACES <= set(CHANNEL_OWNER_DM_NAMESPACES)
    for surface in sorted(sc.OWNER_DM_CONDUCTOR_SURFACES):
        module = importlib.import_module(f"kiro_crew.{surface}.transport")
        transports = [
            cls
            for _name, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, MessagingTransport) and cls.__module__ == module.__name__
        ]
        assert transports, f"{surface}: no transport class"
        for cls in transports:
            assert "direct_peer_of" in vars(cls), f"{cls.__name__} inherits the fail-closed default"


def test_the_discord_transport_places_a_dm_only_by_the_pairing_its_client_records():
    """The real answer behind the double: a DM channel is placed by the pairing
    ``create_dm_channel`` and an authorized inbound DM leave on the client, and by
    nothing else -- a guild room, a thread, an unknown id and a blank all read as
    not on record, and the answer names the peer, not whether the roster admits it
    (the predicate compares that against the sole owner itself)."""
    from kiro_crew.discord.client import DiscordClient
    from kiro_crew.discord.transport import DiscordTransport

    client = DiscordClient(token="bot-secret")
    transport = DiscordTransport(
        client, allowed_user_ids=[OWNER], allowed_thread_ids=[THREAD], allowed_channel_ids=["5"]
    )
    assert transport.direct_peer_of("dm-channel-4242") == ""
    client.remember_dm_recipient("dm-channel-4242", OWNER)
    client.remember_dm_recipient("dm-channel-guest", GUEST)
    assert transport.direct_peer_of("dm-channel-4242") == OWNER
    assert transport.direct_peer_of("dm-channel-guest") == GUEST
    for not_a_dm in ("", THREAD, "5", "unknown-channel"):
        assert transport.direct_peer_of(not_a_dm) == "", not_a_dm


def test_the_telegram_transport_places_a_private_chat_as_its_own_peer():
    """A private ``chat_id`` IS the user id, for the chats this transport opens --
    the allow-listed users' -- while a group or forum chat id is never on the user
    roster and reads as not on record."""
    from kiro_crew.telegram.transport import TelegramTransport

    transport = TelegramTransport(MagicMock(), allowed_user_ids={int(OWNER), int(GUEST)})
    assert transport.direct_peer_of(OWNER) == OWNER
    assert transport.direct_peer_of(GUEST) == GUEST
    for not_a_dm in ("", "-1001", "424242"):
        assert transport.direct_peer_of(not_a_dm) == "", not_a_dm

    # The roster is operator-edited text, and a Telegram group or supergroup
    # chat_id is NEGATIVE: an id that is not a positive integer is never a user,
    # so it is never attested as a DM peer even when it is on the allow-list.
    hand_edited = TelegramTransport(
        MagicMock(), allowed_user_ids={OWNER, "-1001234567890", "0", "not-a-number"}
    )
    assert hand_edited.direct_peer_of(OWNER) == OWNER
    for allow_listed_but_not_a_user in ("-1001234567890", "0", "not-a-number"):
        assert (
            hand_edited.direct_peer_of(allow_listed_but_not_a_user) == ""
        ), allow_listed_but_not_a_user


def test_a_tab_mirrored_to_a_group_id_pasted_into_the_telegram_allow_list_is_refused(
    tmp_path, monkeypatch
):
    """The exemption trusts the transport's answer to "whose DM is this", so the
    transport must not attest a conversation the roster merely lists: a negative
    (group) chat_id pasted into the Telegram user allow-list as its only entry
    would make that group read as the sole owner's own DM, admit the mirrored
    conductor, and put its private reads in front of the group. With the real
    transport, the mirrored tab stays refused on the peer clause."""
    from kiro_crew.telegram.transport import TelegramTransport

    group = "-1001234567890"
    state = _state(tmp_path, monkeypatch)
    state.register_channel_transport(TelegramTransport(MagicMock(), allowed_user_ids={group}))
    tab = _mirrored_slot(state, "chat-conductor", ChannelLink("telegram", channel_id=group))
    _in_runner_turn(tab)

    assert sc.owner_dm_refusal(state, tab) == sc.MIRROR_PEER_NOT_ON_RECORD
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(tab)))
    assert exc.value.code == "mirrored_caller"
    assert sc.MIRROR_PEER_NOT_ON_RECORD in exc.value.message

    # A real user id in the same seat is still the owner's DM.
    state.register_channel_transport(TelegramTransport(MagicMock(), allowed_user_ids={OWNER}))
    owner_tab = _mirrored_slot(state, "chat-owner", TELEGRAM_DM_CONVERSATION)
    assert sc.owner_dm_refusal(state, owner_tab) == ""


def test_sole_direct_target_is_the_one_owner_rule():
    from kiro_crew.dashboard.handlers.messaging import _owner_dm_target
    from kiro_crew.messaging.transport import sole_direct_target

    one = _Transport("discord", users=[OWNER], threads=[THREAD])
    many = _Transport("discord", users=[OWNER, GUEST])
    assert sole_direct_target(one.configured_targets()) == f"user:{OWNER}"
    assert sole_direct_target(many.configured_targets()) == ""
    assert _owner_dm_target(one) == sole_direct_target(one.configured_targets())
    assert _owner_dm_target(many) == sole_direct_target(many.configured_targets())
