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
about a slot.
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
    predicate_reads = set(accessor.findall(inspect.getsource(sc.owner_dm_refusal)))
    probe_reads = set(accessor.findall(inspect.getsource(sc._probe_channel_mirror)))
    assert {"get_mirror_link", "get_slack_link"} <= delivery_reads, sorted(delivery_reads)
    assert delivery_reads <= predicate_reads, sorted(delivery_reads - predicate_reads)
    # The drain-side probe (``containment_snapshot``) judges the same audience after
    # the fact, so it reads the same accessors -- or a thread bound while an entry
    # waited would pass the drain the predicate refuses.
    assert delivery_reads <= probe_reads, sorted(delivery_reads - probe_reads)


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
    the gaps this suite's opener tests closed."""
    import importlib
    import inspect

    for surface in sorted(sc.OWNER_DM_CONDUCTOR_SURFACES):
        dispatcher = importlib.import_module(f"kiro_crew.{surface}.transport_dispatch")
        source = inspect.getsource(dispatcher)
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
