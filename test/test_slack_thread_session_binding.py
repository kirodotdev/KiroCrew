"""Regression tests: one Slack thread belongs to exactly one session.

Three defects shipped together after the channel-session unification, all
rooted in the fact that ``slack_thread_ts`` on a session-map entry means two
different things depending on who wrote it -- "the thread I live in" for a
Slack-born session, versus "the thread I mirror to" for a dashboard-born one.

1. ``handle_message_transport`` resolved the session key syntactically from the
   thread timestamp and never consulted the thread index, so replying in a
   thread the dashboard had created forked a second session with none of the
   original context. The native path had always done this lookup; the transport
   rewrite (which is the default) dropped it.
2. The same path then self-linked unconditionally, overwriting the dashboard's
   binding in the thread index -- so the corruption outlived the reply and
   re-pointed the thread at the wrong session permanently.
3. ``_slot_links`` surfaced a Slack-born session's own origin thread as an
   outbound *mirror*, so its dashboard tab drew the Slack brand icon twice.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.messaging.link import ChannelLink, canonical_key
from kiro_crew.session_map import SessionMap
from kiro_crew.slack import transport_dispatch

_test_dir = Path(__file__).parent
if str(_test_dir) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(_test_dir))
_golden = importlib.import_module("test_slack_golden_transcript")

FakeSessions = _golden.FakeSessions
RecordingSlackClient = _golden.RecordingSlackClient
ScriptedProvider = _golden.ScriptedProvider
make_event = _golden.make_event

# The dashboard created this thread when the user linked their session to Slack.
_THREAD_TS = "1785861292.072329"
_DASHBOARD_KEY = "dashboard:chat-68-1785861270"
_CHANNEL = "D0AP0870FFH"


@pytest.fixture()
def session_map(tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


# ── 1 + 2: inbound routing and index integrity ───────────────────────────────


class _RoutingSessions(FakeSessions):
    """FakeSessions with a real thread index and a record of key resolution."""

    def __init__(self, provider, thread_index=None):
        super().__init__(provider)
        self._thread_index = dict(thread_index or {})
        self.acquired_keys: list[str] = []

    def get_session_for_thread(self, thread_ts):
        return self._thread_index.get(thread_ts)

    async def get_or_create(self, session_key, agent=None, channel_id=None):
        self.acquired_keys.append(session_key)
        return await super().get_or_create(session_key, agent=agent, channel_id=channel_id)

    def set_slack_link(self, key, thread_ts, channel_id):
        super().set_slack_link(key, thread_ts, channel_id)
        self._thread_index[thread_ts] = key


def _drive_transport(monkeypatch, sessions, *, hydrate_conv_flags=True, hydrate_overrides=True):
    """Run one inbound Slack reply through the transport path."""
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "")
    if hydrate_overrides:
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
    if hydrate_conv_flags:
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

    asyncio.run(
        transport_dispatch.handle_message_transport(
            RecordingSlackClient(),
            sessions,
            _CHANNEL,
            "Awesome - this reply is from Slack.",
            _THREAD_TS,
            "1785861317.000100",
            "W017SQBPZBN",
        )
    )


def test_reply_in_dashboard_linked_thread_does_not_fork(monkeypatch):
    """A reply routes into the dashboard session that owns the thread."""
    provider = ScriptedProvider([make_event("ok")])
    sessions = _RoutingSessions(provider, {_THREAD_TS: _DASHBOARD_KEY})

    _drive_transport(monkeypatch, sessions)

    assert _DASHBOARD_KEY in sessions.acquired_keys, (
        "reply must resume the dashboard session that owns this thread"
    )
    assert canonical_key(_THREAD_TS) not in sessions.acquired_keys, (
        "a second slack:<ts> session was minted -- the fork bug is back"
    )


def test_reply_does_not_overwrite_the_dashboard_binding(monkeypatch):
    """The thread index still points at the dashboard session afterwards."""
    provider = ScriptedProvider([make_event("ok")])
    sessions = _RoutingSessions(provider, {_THREAD_TS: _DASHBOARD_KEY})

    _drive_transport(monkeypatch, sessions)

    assert sessions.get_session_for_thread(_THREAD_TS) == _DASHBOARD_KEY, (
        "self-link clobbered the dashboard binding; the thread now resolves to "
        "the wrong session for every later reply"
    )


def test_slack_born_thread_still_self_links(monkeypatch):
    """An unclaimed thread must still register itself, or replies never route."""
    provider = ScriptedProvider([make_event("ok")])
    sessions = _RoutingSessions(provider, {})

    _drive_transport(monkeypatch, sessions)

    assert sessions.acquired_keys == [canonical_key(_THREAD_TS)]
    assert sessions.get_session_for_thread(_THREAD_TS) == canonical_key(_THREAD_TS)


def test_a_link_created_before_acquisition_routes_to_the_owner(monkeypatch):
    """Ownership resolved late: the turn runs in the owner, not a stale key.

    The routing lookup at the top of the handler is many awaits old by the time
    the session is acquired. If a Link-to-Dashboard click lands in that window,
    acquiring under the stale key would run this reply in a contextless Slack
    session, so ownership is re-resolved immediately before acquisition.
    """
    provider = ScriptedProvider([make_event("ok")])
    sessions = _RoutingSessions(provider, {})

    # Stand in for the click landing after the early lookup but before acquire.
    def claim_before_acquisition(*_a, **_k):
        sessions._thread_index[_THREAD_TS] = _DASHBOARD_KEY

    monkeypatch.setattr(
        transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: claim_before_acquisition()
    )
    _drive_transport(monkeypatch, sessions, hydrate_conv_flags=False)

    assert _DASHBOARD_KEY in sessions.acquired_keys, (
        "the turn was acquired under a stale key and ran in the wrong session"
    )
    assert canonical_key(_THREAD_TS) not in sessions.acquired_keys


def test_a_reroute_rehydrates_privacy_state_for_the_new_owner(monkeypatch):
    """Durable incognito/temporary flags must follow the session, not the key.

    Hydration at entry runs for the entry key. If a reroute leaves it there,
    ``_is_slack_restricted()`` consults an unpopulated flag map for the session
    the turn actually runs in -- so an incognito session's turn is written to
    disk. This is the privacy consequence of making ownership mobile.
    """
    provider = ScriptedProvider([make_event("ok")])
    sessions = _RoutingSessions(provider, {})
    hydrated_overrides: list[str] = []
    hydrated_flags: list[str] = []

    async def claim_during_governance(_channel):
        sessions._thread_index[_THREAD_TS] = _DASHBOARD_KEY
        return True

    monkeypatch.setattr(transport_dispatch, "channel_inbound_permitted", claim_during_governance)
    monkeypatch.setattr(
        transport_dispatch,
        "_hydrate_thread_overrides",
        lambda key, _log=None: hydrated_overrides.append(key),
    )
    monkeypatch.setattr(
        transport_dispatch,
        "_hydrate_conv_flags",
        lambda _s, key: hydrated_flags.append(key),
    )
    _drive_transport(monkeypatch, sessions, hydrate_overrides=False, hydrate_conv_flags=False)

    assert _DASHBOARD_KEY in hydrated_overrides, (
        "thread overrides were never hydrated for the rerouted owner"
    )
    assert _DASHBOARD_KEY in hydrated_flags, (
        "durable privacy flags were never hydrated for the rerouted owner -- an "
        "incognito turn would be persisted to disk"
    )


def test_privacy_modifiers_receive_the_current_owner(monkeypatch):
    """The privacy handlers must not act on a key that went stale.

    ``maybe_apply_privacy_modifiers`` calls ``set_slack_link`` unconditionally,
    so if it is handed a key that went stale while inbound governance awaited,
    it overwrites the dashboard's binding and every later reply misroutes.
    """
    provider = ScriptedProvider([make_event("ok")])
    sessions = _RoutingSessions(provider, {})
    seen: list[str] = []

    async def claim_during_governance(_channel):
        # Stand in for a Link-to-Dashboard click landing during governance.
        sessions._thread_index[_THREAD_TS] = _DASHBOARD_KEY
        return True

    async def spy_modifiers(text, cmd_text, session_key, *a, **k):
        seen.append(session_key)
        return text, cmd_text, False

    monkeypatch.setattr(transport_dispatch, "channel_inbound_permitted", claim_during_governance)
    monkeypatch.setattr(transport_dispatch, "maybe_apply_privacy_modifiers", spy_modifiers)
    _drive_transport(monkeypatch, sessions)

    assert seen == [_DASHBOARD_KEY], (
        f"privacy modifiers got a stale key {seen!r}; they would overwrite the "
        "dashboard binding for this thread"
    )


def test_approval_decider_follows_a_reroute(monkeypatch):
    """Trust must apply to the session the turn actually runs in.

    The decider is constructed before ownership is re-resolved, and its
    session_key is what maps a human's Trust click back to a session. If a
    reroute leaves it stale, the click lands on the wrong session.
    """
    provider = ScriptedProvider([make_event("ok")])
    sessions = _RoutingSessions(provider, {})
    made: list = []

    real_decider = transport_dispatch.SlackApprovalDecider

    def spy_decider(*a, **k):
        d = real_decider(*a, **k)
        made.append(d)
        return d

    def claim_before_acquisition(*_a, **_k):
        sessions._thread_index[_THREAD_TS] = _DASHBOARD_KEY

    monkeypatch.setattr(transport_dispatch, "SlackApprovalDecider", spy_decider)
    monkeypatch.setattr(
        transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: claim_before_acquisition()
    )
    _drive_transport(monkeypatch, sessions, hydrate_conv_flags=False)

    assert made, "no approval decider was constructed"
    assert made[0].session_key == _DASHBOARD_KEY, (
        "decider kept the pre-reroute key -- a Trust click would be applied to "
        "the wrong session"
    )


def test_a_link_created_mid_turn_is_not_clobbered(monkeypatch):
    """A dashboard link landing during session acquisition must survive.

    The routing lookup happens many awaits before the self-link -- inbound
    governance, the hook path and ``get_or_create`` all yield. If the dashboard
    claims this thread inside that window, self-linking on the stale "unclaimed"
    reading would overwrite the newer binding and send every later reply to the
    wrong session.
    """
    provider = ScriptedProvider([make_event("ok")])
    sessions = _RoutingSessions(provider, {})

    original = sessions.get_or_create

    async def claim_during_acquisition(session_key, agent=None, channel_id=None):
        # Stand in for the dashboard's send-to-Slack landing mid-turn.
        sessions._thread_index[_THREAD_TS] = _DASHBOARD_KEY
        return await original(session_key, agent=agent, channel_id=channel_id)

    monkeypatch.setattr(sessions, "get_or_create", claim_during_acquisition)
    _drive_transport(monkeypatch, sessions)

    assert sessions.get_session_for_thread(_THREAD_TS) == _DASHBOARD_KEY, (
        "a self-link decided on a stale lookup clobbered a newer dashboard binding"
    )


# ── 2b: the index heals a map already corrupted by the fork ──────────────────


def _corrupted_map(session_map, first, second):
    """Two entries claiming one thread, written in the given order."""
    for key in (first, second):
        session_map.set_slack_link(key, _THREAD_TS, _CHANNEL)
    session_map._rebuild_thread_index()
    return session_map


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_DASHBOARD_KEY, canonical_key(_THREAD_TS)),
        (canonical_key(_THREAD_TS), _DASHBOARD_KEY),
    ],
)
def test_thread_index_prefers_the_owning_session(session_map, first, second):
    """Order-independent: the non-derived key wins the thread."""
    sm = _corrupted_map(session_map, first, second)
    assert sm.get_session_for_thread(_THREAD_TS) == _DASHBOARD_KEY


def test_thread_index_keeps_a_lone_slack_session(session_map):
    """A self-derived key is still used when nothing else claims the thread."""
    session_map.set_slack_link(canonical_key(_THREAD_TS), _THREAD_TS, _CHANNEL)
    session_map._rebuild_thread_index()
    assert session_map.get_session_for_thread(_THREAD_TS) == canonical_key(_THREAD_TS)


def test_thread_index_survives_reload(session_map, tmp_path):
    """The tie-break is applied on load, so corrupted files heal without a migration.

    Write order matters and mirrors what actually happened on disk: the dashboard
    claimed the thread first, then the forked ``slack:<ts>`` session claimed it.
    A plain last-write-wins rebuild therefore resolves to the fork -- which is
    exactly the state this test must reject.
    """
    _corrupted_map(session_map, _DASHBOARD_KEY, canonical_key(_THREAD_TS))
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        reloaded = SessionMap()
    assert reloaded.get_session_for_thread(_THREAD_TS) == _DASHBOARD_KEY


def test_thread_index_ignores_a_non_string_timestamp(session_map):
    """A numeric ts must not crash the rebuild.

    The pre-fix index only ever used ``slack_thread_ts`` as a dict key, so a
    hand-edited or legacy file holding a number survived. The tie-break calls
    ``str.endswith``, which would raise TypeError inside ``_load`` and take
    gateway startup down with it.
    """
    session_map._data["slack:1"] = {"sid": "x", "slack_thread_ts": 1785861292.072329}
    session_map._data[_DASHBOARD_KEY] = {"sid": "y", "slack_thread_ts": _THREAD_TS}
    session_map._rebuild_thread_index()  # must not raise
    assert session_map.get_session_for_thread(_THREAD_TS) == _DASHBOARD_KEY


# ── 3: the Slack-born tab is badged once ─────────────────────────────────────


def _links_for(session_key, thread_ts):
    """Run _slot_links against a slot whose session carries thread_ts."""
    from kiro_crew.dashboard.state import DashboardState

    class _Sessions:
        def get_mirror_link(self, key):
            return ChannelLink("slack", _CHANNEL, thread_ts)

        def get_slack_link(self, key):
            return (thread_ts, _CHANNEL)

        def mirror_accepts_inbound(self, key):
            return False

    class _Slot:
        key = session_key
        linked_session_key = session_key
        _slack_thread_ts = thread_ts
        _slack_channel = _CHANNEL

    state = DashboardState.__new__(DashboardState)
    state.sessions = _Sessions()
    with patch(
        "kiro_crew.dashboard.chat_utils.effective_session_key",
        return_value=session_key,
    ):
        return state._slot_links(_Slot())


def _slack_out(links):
    return [x for x in links if x["channel"] == "slack" and x["direction"] == "out"]


def test_slack_born_session_is_not_badged_twice():
    """Its own origin thread is not an outbound mirror."""
    links, slack_linked, _, _ = _links_for(canonical_key(_THREAD_TS), _THREAD_TS)
    assert _slack_out(links) == [], (
        "a Slack-born session's own thread was surfaced as a mirror -- the "
        "sidebar draws the brand icon twice and offers it as releasable"
    )
    # The detail panel synthesizes its own Slack row from slack_linked whenever
    # no slack wire link is present, so dropping the link alone would just move
    # the phantom mirror from the sidebar into that panel.
    assert slack_linked is False, (
        "slack_linked stayed true, so LinkedSurfacesSection rebuilds the "
        "releasable Slack mirror it was just stopped from showing"
    )
    # Not surfaced as a mirror, but surfaced: the thread the conversation LIVES
    # in is named as an origin. Emitting nothing at all is what left the panel
    # with no Slack row, so it fell through to the target picker and offered
    # "Send to Slack" for a session already in Slack -- and left nowhere to hang
    # a pause control.
    slack_rows = [x for x in links if x["channel"] == "slack"]
    assert [x["direction"] for x in slack_rows] == ["origin"], (
        "a Slack-born session must carry exactly one Slack row, as its origin"
    )
    assert slack_rows[0]["paused"] is False


def test_dashboard_mirror_still_shows_its_slack_target():
    """A genuine dashboard->Slack mirror keeps its outbound link."""
    links, slack_linked, channel, ts = _links_for(_DASHBOARD_KEY, _THREAD_TS)
    assert len(_slack_out(links)) == 1, "the real mirror link must survive the fix"
    assert slack_linked is True
    assert (channel, ts) == (_CHANNEL, _THREAD_TS)


def test_slack_session_mirroring_elsewhere_keeps_its_link():
    """Only the SELF-reference is suppressed, not a different thread."""
    links, slack_linked, _, _ = _links_for(canonical_key("1785861252.833429"), _THREAD_TS)
    assert len(_slack_out(links)) == 1
    assert slack_linked is True
