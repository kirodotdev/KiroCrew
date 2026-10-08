"""The Crewmates list's source: when the PERSON last messaged each crew.

Whether a DM thread holds a message, or a crew was created on the dashboard,
does not say whether the user ever talked to it: background work and apps
write there too. ``crew_recency`` records exactly that, at the one place that knows a person typed a message
(``POST /api/chat`` with no app token and no cron attestation), and
``GET /api/members`` ships it as ``last_chat_ts``. These tests pin:

* the store (round trip, write granularity, junk, the one-time seed),
* who records (the dashboard user does; an app token and a cron do not),
* the roster field, the default crew's ``""`` record, and the seed's reading of
  a DM thread (rows carrying the human marker count, unmarked rows do not).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import crew_recency
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.dashboard.chat_handlers import api_chat
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.history import HUMAN_TURN_META_KEY


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


# -- the store -------------------------------------------------------------


def test_record_round_trips_and_survives_a_reread():
    assert crew_recency.read_recency() == {}
    assert crew_recency.record_user_chat("radar", ts=1000.0) is True
    assert crew_recency.record_user_chat("", ts=1200.0) is True
    assert crew_recency.read_recency() == {"radar": 1000.0, "": 1200.0}


def test_a_burst_of_sends_writes_once_and_never_moves_backwards():
    assert crew_recency.record_user_chat("radar", ts=1000.0) is True
    assert crew_recency.record_user_chat("radar", ts=1010.0) is False
    assert crew_recency.read_recency()["radar"] == 1000.0
    assert crew_recency.record_user_chat("radar", ts=2000.0) is True
    # An older clock reading past the window never walks the record back.
    assert crew_recency.record_user_chat("radar", ts=1500.0) is True
    assert crew_recency.read_recency()["radar"] == 2000.0


def _write_raw(raw: str) -> None:
    crew_recency.recency_path().parent.mkdir(parents=True, exist_ok=True)
    crew_recency.recency_path().write_text(raw, encoding="utf-8")


@pytest.mark.parametrize("raw", ["not json", "[]"])
def test_an_unreadable_file_reads_as_nothing_and_is_never_reseeded(raw):
    # Unreadable is not absent: a seed must never overwrite it.
    _write_raw(raw)
    assert crew_recency.read_recency() == {}
    assert crew_recency.needs_seed() is False


@pytest.mark.parametrize("raw", ["not json", "[]"])
def test_no_writer_replaces_a_file_it_could_not_read(raw):
    _write_raw(raw)
    assert crew_recency.record_user_chat("radar", ts=1000.0) is False
    assert crew_recency.seed({"radar": 1000.0}) == {"radar": 1000.0}
    assert crew_recency.recency_path().read_text(encoding="utf-8") == raw


def test_junk_values_are_never_ordering_keys():
    _write_raw(
        json.dumps({"seeded": True, "crews": {"a": True, "b": "x", "c": float("nan"), "d": 5.0}})
    )
    assert crew_recency.read_recency() == {"d": 5.0}


def test_seed_runs_once_and_a_live_record_wins():
    crew_recency.record_user_chat("radar", ts=5000.0)
    assert crew_recency.needs_seed() is True
    merged = crew_recency.seed({"radar": 100.0, "scout": 200.0})
    assert merged == {"radar": 5000.0, "scout": 200.0}
    assert crew_recency.needs_seed() is False
    # A second seed is a no-op: the backfill is one-time.
    assert crew_recency.seed({"late": 300.0}) == {"radar": 5000.0, "scout": 200.0}
    # A later send keeps the seeded mark.
    crew_recency.record_user_chat("late", ts=9000.0)
    assert crew_recency.needs_seed() is False


# -- who records -----------------------------------------------------------


def _chat_app(state, *, app_name: str) -> web.Application:
    @web.middleware
    async def _auth(request: web.Request, handler):
        request["app"] = app_name
        request["user"] = "" if app_name else "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    return app


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    slot = st.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot.agent = "researcher"
    return st


async def _send(state, *, app_name: str = "", cron: str = "") -> MagicMock:
    async def _turn(_state, s, _message, **_kwargs):
        s.append("assistant", "ok")
        s.append("done", "", "done")

    recorder = MagicMock(return_value=True)
    with (
        patch("kiro_crew.apps.permissions.app_can_manage_session_approvals", return_value=True),
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=_turn),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
        patch(
            "kiro_crew.dashboard.chat_handlers.cron_slot_creator", new=AsyncMock(return_value=cron)
        ),
        patch(
            "kiro_crew.dashboard.chat_handlers.cron_creator_admission",
            new=AsyncMock(return_value=(None, False, {})),
        ),
        patch.object(crew_recency, "record_user_chat", recorder),
    ):
        async with TestClient(TestServer(_chat_app(state, app_name=app_name))) as client:
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "hi"})
            await resp.text()
    assert resp.status == 200
    return recorder


@pytest.mark.asyncio
async def test_the_dashboard_user_sending_records_the_slot_crew(state):
    recorder = await _send(state)
    recorder.assert_called_once_with("researcher")


@pytest.mark.asyncio
async def test_a_chat_with_no_crew_picked_records_the_default_crew_key(state):
    state._slots["s1"].agent = ""
    recorder = await _send(state)
    recorder.assert_called_once_with("")


@pytest.mark.asyncio
async def test_an_app_token_send_records_nothing(state):
    recorder = await _send(state, app_name="crew-keyboard")
    recorder.assert_not_called()


@pytest.mark.asyncio
async def test_a_cron_send_records_nothing(state):
    recorder = await _send(state, cron="cron:job-1")
    recorder.assert_not_called()


# -- the roster field ------------------------------------------------------


def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


def _fake_config(*names: str, default_agent: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        agents={n: KiroCrewAgentConfig(kiro_agent=n) for n in names},
        default_agent=default_agent,
        memory_stores={},
        degraded_sections=frozenset(),
    )


async def _roster(state, fake) -> dict[str, dict]:
    with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=fake):
        async with TestClient(TestServer(_members_app(state))) as client:
            body = await (await client.get("/api/members")).json()
    return {r["name"]: r for r in body["members"]}


@pytest.mark.asyncio
async def test_roster_ships_last_chat_ts_and_folds_the_default_key(tmp_path):
    state = _make_state(tmp_path)
    crew_recency.seed({})
    crew_recency.record_user_chat("radar", ts=1000.0)
    crew_recency.record_user_chat("", ts=3000.0)
    crew_recency.record_user_chat("boss", ts=2000.0)
    rows = await _roster(state, _fake_config("radar", "boss", "quiet", default_agent="boss"))
    assert rows["radar"]["last_chat_ts"] == 1000.0
    # The default crew owns a chat that picked no crew.
    assert rows["boss"]["last_chat_ts"] == 3000.0
    assert rows["quiet"]["last_chat_ts"] == 0


@pytest.mark.asyncio
async def test_seed_reads_only_what_the_user_typed_in_a_dm_thread(tmp_path):
    from kiro_crew import members as members_mod

    state = _make_state(tmp_path)
    fake = _fake_config("typed", "peer", "replied")
    with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=fake):
        for name in ("typed", "peer", "replied"):
            slug = members_mod.member_slug(name, fake)
            members_mod.write_dm_binding(slug, member=name, slot_key=f"member-{slug}")
        key = {
            n: members_mod.member_thread_session_alias(members_mod.member_slug(n, fake))
            for n in ("typed", "peer", "replied")
        }
        log = state.conversation_log
        human = {HUMAN_TURN_META_KEY: True}
        log.append(key["typed"], "user", "[1, 2, 3] is the list", extra_meta=human)
        log.append(key["typed"], "user", "[sent by session member-x via session_send]\n\ndo it")
        # A peer delivery and an agent's own speech are not the user chatting.
        log.append(key["peer"], "user", "[sent by session member-x via session_send]\n\nwork")
        log.append(key["replied"], "assistant", "a patrol said something")
    rows = await _roster(state, fake)
    assert rows["typed"]["last_chat_ts"] > 0
    assert rows["peer"]["last_chat_ts"] == 0
    assert rows["replied"]["last_chat_ts"] == 0
    assert crew_recency.needs_seed() is False


@pytest.mark.asyncio
async def test_a_seed_that_could_not_read_a_thread_runs_again_later(tmp_path):
    from kiro_crew import members as members_mod
    from kiro_crew.history import TranscriptBusy

    state = _make_state(tmp_path)
    fake = _fake_config("typed")
    with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=fake):
        slug = members_mod.member_slug("typed", fake)
        members_mod.write_dm_binding(slug, member="typed", slot_key=f"member-{slug}")
        state.conversation_log.append(
            members_mod.member_thread_session_alias(slug),
            "user",
            "hi",
            extra_meta={HUMAN_TURN_META_KEY: True},
        )
    with patch.object(
        state.conversation_log, "derive_messages", side_effect=TranscriptBusy("busy")
    ):
        rows = await _roster(state, fake)
    assert rows["typed"]["last_chat_ts"] == 0
    assert crew_recency.needs_seed() is True
    rows = await _roster(state, fake)
    assert rows["typed"]["last_chat_ts"] > 0
    assert crew_recency.needs_seed() is False


# -- creating a crewmate counts ---------------------------------------------


@pytest.mark.asyncio
async def test_creating_a_crewmate_lists_it_first_and_a_background_one_stays_hidden(tmp_path):
    from test_api_agents_create_template import _fake_config as _create_config
    from test_api_agents_create_template import _post

    crew_recency.seed({})
    crew_recency.record_user_chat("older", ts=1000.0)
    status, _ = await _post(
        {"name": "fresh", "kiro_agent": "kirocrew"}, _create_config(), installed=("kirocrew",)
    )
    assert status == 200
    # A crew a background writer added (the agent sync, an app) has no record.
    rows = await _roster(_make_state(tmp_path), _fake_config("older", "fresh", "synced"))
    assert rows["fresh"]["last_chat_ts"] > rows["older"]["last_chat_ts"] > 0
    assert rows["synced"]["last_chat_ts"] == 0


@pytest.mark.asyncio
async def test_a_cron_attested_create_records_nothing():
    from test_api_agents_create_template import _fake_config as _create_config
    from test_api_agents_create_template import _post

    with patch(
        "kiro_crew.dashboard.handlers._shared.cron_slot_creator",
        new=AsyncMock(return_value="cron:job-1"),
    ):
        status, _ = await _post(
            {"name": "fresh", "kiro_agent": "kirocrew"}, _create_config(), installed=("kirocrew",)
        )
    assert status == 200
    assert crew_recency.read_recency() == {}


@pytest.mark.asyncio
async def test_seed_reads_a_thread_from_before_the_marker_by_the_old_rule(tmp_path):
    """A thread with no marked row predates the marker: a typed row still
    counts and a `[` envelope does not. In a marked thread a newer unmarked
    heartbeat is skipped, so that crew ranks by its typed row. Row stamps are
    monotonic per thread only, so the heartbeat is told apart by its own
    thread's stamps, never by comparing two threads (a coarse Windows clock)."""
    from kiro_crew import members as members_mod
    from kiro_crew.eventlog.members_projections import _parse_ts

    state = _make_state(tmp_path)
    fake = _fake_config("legacy", "marked")
    with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=fake):
        key = {}
        for name in ("legacy", "marked"):
            slug = members_mod.member_slug(name, fake)
            members_mod.write_dm_binding(slug, member=name, slot_key=f"member-{slug}")
            key[name] = members_mod.member_thread_session_alias(slug)
        human = {HUMAN_TURN_META_KEY: True}
        log = state.conversation_log
        log.append(key["legacy"], "user", "an old typed hello")
        log.append(key["legacy"], "user", "[sent by session member-x via session_send]\n\nlater")
        log.append(key["marked"], "user", "typed", extra_meta=human)
        log.append(key["marked"], "user", "\U0001f493 Heartbeat: anything to do?")
        stamps = [_parse_ts(m["ts"]) for m in log.read_messages(key["marked"])]
    rows = await _roster(state, fake)
    assert rows["legacy"]["last_chat_ts"] > 0
    assert stamps[0] < stamps[1]
    assert rows["marked"]["last_chat_ts"] == stamps[0]
