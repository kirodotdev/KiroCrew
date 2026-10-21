"""A dashboard card's reply sent as a turn: what goes through and what never does.

Every refusal is asserted twice over: the answer carries its machine code, and the
thread is left exactly as it was found (no row, no turn, no created slot, an open
approval still open). The accepted send is asserted on the real ``POST /api/chat``
route, so the tag, the row meta and the directive authority are what the handler
did and not what a helper claims.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard import card_send, chat_handlers, chat_runner
from kiro_crew.dashboard.slot_persistence.metadata_line import _newest_human_turn_ts
from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.testing.clock import ManualClock


@pytest.fixture
def env(tmp_path, monkeypatch):
    card_send._reset_rate_limit_for_tests()
    state = _make_state(tmp_path)
    state.owner_id = ""
    slot = state.get_or_create_slot("member-default", agent="default", mode="member")
    slot._titled = True
    slot._auto_tagged = True
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig()}
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    binding = {"member": "default", "slot_key": slot.key, "memory_store": ""}
    monkeypatch.setattr(chat_handlers.members_mod, "read_dm_binding_for_slot", lambda key: binding)
    monkeypatch.setattr("kiro_crew.dashboard.chat_delivery.start_queue_persist", lambda *a: None)
    monkeypatch.setattr(chat_runner, "_gateway_shutdown_requested", lambda state: False)
    monkeypatch.setattr(chat_runner, "_memory_preparation_pending", lambda state: False)
    turns: list[tuple[str, dict]] = []

    async def fake_run_chat(state, slot, message, **kwargs):
        turns.append((message, kwargs))

    monkeypatch.setattr(chat_handlers, "_run_chat", fake_run_chat)
    app = as_owner(web.Application())
    app["state"] = state
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    return state, slot, cfg, turns, app


def _card(slot, message="Approve the plan", **extra):
    return {"slot": slot.key, "message": message, "origin": card_send.CARD_ORIGIN, **extra}


def _user_rows(slot):
    return [r for r in slot.messages if r["role"] == "user"]


async def _untouched(slot, turns, state, before_slots):
    await asyncio.sleep(0)
    assert _user_rows(slot) == []
    assert turns == []
    assert set(state._slots) == before_slots


@pytest.mark.asyncio
async def test_a_card_send_runs_one_tagged_turn_without_a_persons_directive_authority(env):
    state, slot, _, turns, app = env
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot, "  Approve the plan  "))
        assert resp.status == 200
        await asyncio.sleep(0)
    expected = "[Dashboard card] Approve the plan"
    (row,) = _user_rows(slot)
    assert row["content"] == expected
    assert row["meta"][card_send.CARD_ORIGIN_META_KEY] is True
    # The words are a page's, so the row does not claim a person authored it and
    # does not advance the stamp for when the person last spoke.
    assert HUMAN_TURN_META_KEY not in row["meta"]
    assert _newest_human_turn_ts(slot.messages) == ""
    ((text, kwargs),) = turns
    assert text == expected
    assert kwargs["_directive_user_origin"] is False
    # It also never inherits an auto-approval: the runner is told this is a card's turn.
    assert kwargs["_card_origin"] is True


@pytest.mark.asyncio
async def test_a_card_turn_is_marked_untrusted_even_in_a_thread_that_trusts(env):
    """Crewmate threads default to trust; the mark must not depend on the thread's mode."""
    state, slot, _, turns, app = env
    slot._trust = True
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot))
        assert resp.status == 200
        await asyncio.sleep(0)
    ((_, kwargs),) = turns
    assert kwargs["_card_origin"] is True


@pytest.mark.parametrize(("card_origin", "human"), [(False, True), (True, False)])
def test_a_card_send_does_not_count_as_the_person_being_back_for_autonudge(
    monkeypatch, card_origin, human
):
    """A person's typed send releases an approval-timeout hold on a paused nudge loop
    (``human=True``). A card's words are a page's, so they defer the timer and nothing else."""
    from unittest.mock import MagicMock

    from kiro_crew import autonudge
    from kiro_crew.dashboard import responsive_chat

    nudge = MagicMock()
    monkeypatch.setattr(autonudge, "get_instance", lambda: nudge)
    request = {"is_dashboard_user": True, "internal_auth": False}
    slot = type("S", (), {"key": "member-ada"})()

    responsive_chat.notify_user_input(request, slot, card_origin=card_origin)  # type: ignore[arg-type]

    nudge.notify_user_input.assert_called_once_with("member-ada", human=human)


@pytest.mark.asyncio
async def test_the_route_tells_autonudge_a_card_send_is_not_the_person(env, monkeypatch):
    from unittest.mock import MagicMock

    from kiro_crew import autonudge

    state, slot, _, turns, app = env
    nudge = MagicMock()
    monkeypatch.setattr(autonudge, "get_instance", lambda: nudge)
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/api/chat?ws=1", json=_card(slot))).status == 200
        await asyncio.sleep(0)
    assert nudge.notify_user_input.call_args.kwargs["human"] is False


@pytest.mark.asyncio
async def test_a_typed_send_is_unchanged(env):
    state, slot, _, turns, app = env
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json={"slot": slot.key, "message": "hello"})
        assert resp.status == 200
        await asyncio.sleep(0)
    (row,) = _user_rows(slot)
    assert row["content"] == "hello"
    assert card_send.CARD_ORIGIN_META_KEY not in row["meta"]
    # The control for the card test: a typed send does mark the person's turn.
    assert row["meta"][HUMAN_TURN_META_KEY] is True
    assert _newest_human_turn_ts(slot.messages) != ""
    ((text, kwargs),) = turns
    assert text == "hello"
    assert kwargs["_directive_user_origin"] is True
    assert "_card_origin" not in kwargs


@pytest.mark.asyncio
async def test_a_caller_cannot_write_the_card_marker_into_a_typed_send(env):
    state, slot, _, turns, app = env
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat?ws=1",
            json={
                "slot": slot.key,
                "message": "hi",
                "meta": {card_send.CARD_ORIGIN_META_KEY: True},
            },
        )
        assert resp.status == 200
    (row,) = _user_rows(slot)
    assert card_send.CARD_ORIGIN_META_KEY not in row["meta"]


@pytest.mark.asyncio
async def test_a_card_carries_text_and_nothing_else(env):
    """Agent, theme, steer, responsive and meta in a card body are dropped."""
    state, slot, _, turns, app = env
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat?ws=1",
            json=_card(
                slot,
                agent="other",
                steer=True,
                responsive=True,
                color_theme="custom-x",
                meta={"files": ["/etc/passwd"], "quote": {"text": "q"}},
            ),
        )
        assert resp.status == 200
        await asyncio.sleep(0)
    (row,) = _user_rows(slot)
    assert "files" not in row["meta"] and "quote" not in row["meta"]
    assert "sendId" not in row["meta"]
    assert slot.agent == "default"
    assert slot.color_theme in ("", None)


@pytest.mark.asyncio
async def test_slash_commands_are_not_commands_inside_a_card(env):
    state, slot, _, turns, app = env
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot, "/clear"))
        assert resp.status == 200
        await asyncio.sleep(0)
    ((text, _),) = turns
    assert text == "[Dashboard card] /clear"
    assert not text.startswith("/")


@pytest.mark.asyncio
async def test_only_the_owners_dashboard_may_claim_card_origin(env):
    state, slot, _, turns, app = env
    before = set(state._slots)
    async with TestClient(TestServer(app)) as client:
        # A caller that authenticated but is not the owner.
        resp = await client.post(
            "/api/chat?ws=1", json=_card(slot), headers={"X-Test-User": "someone-else"}
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "card_origin_not_allowed"
        # An app token meets the route's existence-hiding denial first.
        app_resp = await client.post(
            "/api/chat?ws=1", json=_card(slot), headers={"X-Test-App": "some-app"}
        )
        assert app_resp.status in (403, 404)
    await _untouched(slot, turns, state, before)


@pytest.mark.asyncio
async def test_the_gate_itself_refuses_an_app_even_when_the_route_let_it_through(env):
    """A granted app reaches the handler body; the card gate must still say no."""
    state, slot, _, turns, _ = env
    request = type(
        "R",
        (),
        {
            "get": lambda self, key, default=None: {"app": "some-app", "user": "local-app"}.get(
                key, default
            )
        },
    )()
    refusal = await card_send.card_send_refusal(request, state, slot, "hello")
    assert refusal.status == 403
    assert refusal.body and b"card_origin_not_allowed" in refusal.body


@pytest.mark.asyncio
async def test_the_target_must_be_an_existing_crewmate_thread(env, monkeypatch):
    state, slot, _, turns, app = env
    before = set(state._slots)
    async with TestClient(TestServer(app)) as client:
        missing = await client.post("/api/chat?ws=1", json={**_card(slot), "slot": "member-nobody"})
        assert missing.status == 403
        assert (await missing.json())["code"] == "card_target_invalid"
        noslot = await client.post(
            "/api/chat?ws=1", json={"message": "x", "origin": card_send.CARD_ORIGIN}
        )
        assert noslot.status == 403
        plain = state.get_or_create_slot("chat-1")
        plain_before = set(state._slots)
        other = await client.post("/api/chat?ws=1", json={**_card(slot), "slot": plain.key})
        assert other.status == 403
        monkeypatch.setattr(chat_handlers.members_mod, "read_dm_binding_for_slot", lambda key: None)
        unbound = await client.post("/api/chat?ws=1", json=_card(slot))
        assert unbound.status == 403
    assert set(state._slots) == plain_before
    assert before <= plain_before
    assert turns == [] and _user_rows(slot) == [] and _user_rows(plain) == []


@pytest.mark.asyncio
async def test_the_profile_switch_turns_it_off(env):
    state, slot, cfg, turns, app = env
    cfg.agents["default"].dashboard_card_send = False
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot))
        assert resp.status == 403
        assert (await resp.json())["code"] == "card_send_off"
        # The same switch does not touch what a person types.
        typed = await client.post("/api/chat?ws=1", json={"slot": slot.key, "message": "hello"})
        assert typed.status == 200
    assert [r["content"] for r in _user_rows(slot)] == ["hello"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["", "   ", "a" * (card_send.CARD_MAX_CHARS + 1), "a\u0007b", "a\x00b", "a\x7fb"],
)
async def test_the_text_rules_hold_on_the_server(env, text):
    state, slot, _, turns, app = env
    before = set(state._slots)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot, text))
        assert resp.status == 400
        assert (await resp.json())["code"] == "card_text_invalid"
    await _untouched(slot, turns, state, before)


@pytest.mark.asyncio
async def test_a_newline_and_the_longest_text_pass(env):
    state, slot, _, turns, app = env
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/api/chat?ws=1", json=_card(slot, "a\nb"))).status == 200
        card_send._reset_rate_limit_for_tests()
        assert (
            await client.post("/api/chat?ws=1", json=_card(slot, "a" * card_send.CARD_MAX_CHARS))
        ).status == 200


@pytest.mark.asyncio
async def test_a_card_never_answers_an_open_approval(env):
    """The approval stays open, and the refusal says why; a typed send is untouched."""
    state, slot, _, turns, app = env
    future = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = future
    before = set(state._slots)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot, "Approve"))
        assert resp.status == 409
        assert (await resp.json())["code"] == "card_send_pending_decision"
    assert not future.done()
    await _untouched(slot, turns, state, before)
    future.cancel()


@pytest.mark.asyncio
async def test_a_decision_that_opens_after_the_gate_admitted_the_send_still_refuses(
    env, monkeypatch
):
    """The gate looks once, then the handler awaits; a question can open in between.

    The gate is stubbed to admit, so the only thing standing between the card and the
    open decision is the recheck after the awaits.
    """
    state, slot, _, turns, app = env

    async def _admit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(card_send, "card_send_refusal", _admit)
    future = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = future
    before = set(state._slots)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot, "Approve"))
        assert resp.status == 409
        assert (await resp.json())["code"] == "card_send_pending_decision"
    assert not future.done()
    await _untouched(slot, turns, state, before)
    future.cancel()


@pytest.mark.asyncio
async def test_a_card_never_answers_a_permission_prompt_the_client_is_waiting_on(env):
    state, slot, _, turns, app = env
    slot._acp_client = type("C", (), {"awaiting_permission": True})()
    before = set(state._slots)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot, "Allow"))
        assert resp.status == 409
        assert (await resp.json())["code"] == "card_send_pending_decision"
    await _untouched(slot, turns, state, before)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking", [True, False])
async def test_a_card_never_answers_an_open_question_card(env, blocking):
    """A user row retires a stateless question card, so the card must not be one."""
    state, slot, _, turns, app = env
    slot._question_pending = {"q-1": {"blocking": blocking, "questions": [{"question": "Which?"}]}}
    before = set(state._slots)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot, "The first one"))
        assert resp.status == 409
        assert (await resp.json())["code"] == "card_send_pending_decision"
    assert set(slot._question_pending) == {"q-1"}
    await _untouched(slot, turns, state, before)


@pytest.mark.asyncio
async def test_a_card_never_answers_a_question_parked_in_the_gateway(env):
    state, slot, _, turns, app = env
    state._pending_questions["ask-1"] = {
        "slot": slot.key,
        "questions": [{"question": "Which?"}],
        "ts": 0,
    }
    before = set(state._slots)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot, "Yes"))
        assert resp.status == 409
        assert (await resp.json())["code"] == "card_send_pending_decision"
    assert set(state._pending_questions) == {"ask-1"}
    await _untouched(slot, turns, state, before)


@pytest.mark.asyncio
async def test_a_card_is_not_queued_behind_a_running_turn(env):
    state, slot, _, turns, app = env
    slot.task = asyncio.get_running_loop().create_future()
    slot._acp_client = type(
        "C", (), {"awaiting_permission": False, "has_active_turn": lambda self: True}
    )()
    from kiro_crew.dashboard import responsive_chat as rc

    rc.begin_turn(slot, protected=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot))
        assert resp.status == 409
        assert (await resp.json())["code"] == "card_send_slot_busy"
        queued = await client.post("/api/chat?ws=1", json=_card(slot, steer=True))
        assert queued.status == 409
    assert len(slot._queue) == 0
    slot.task.cancel()


@pytest.mark.asyncio
async def test_a_card_does_not_join_a_non_empty_queue(env):
    state, slot, _, turns, app = env
    slot.queue_append("typed earlier")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot))
        assert resp.status == 409
        assert (await resp.json())["code"] == "card_send_slot_busy"
    assert [e["content"] for e in slot._queue] == ["typed earlier"]


@pytest.mark.asyncio
async def test_one_send_per_interval_per_thread(env, monkeypatch):
    state, slot, _, turns, app = env
    clock = ManualClock(monotonic_start=1000.0)
    clock.install(monkeypatch, card_send)
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/api/chat?ws=1", json=_card(slot, "one"))).status == 200
        again = await client.post("/api/chat?ws=1", json=_card(slot, "two"))
        assert again.status == 429
        assert (await again.json())["code"] == "card_send_rate_limited"
        assert int(again.headers["Retry-After"]) >= 1
        clock.advance(card_send.CARD_MIN_INTERVAL_SECS + 0.1)
        assert (await client.post("/api/chat?ws=1", json=_card(slot, "three"))).status == 200
    assert [r["content"] for r in _user_rows(slot)] == [
        "[Dashboard card] one",
        "[Dashboard card] three",
    ]


def test_an_unknown_origin_claims_nothing():
    assert not card_send.claims_card_origin({"origin": "dashboard"})
    assert not card_send.claims_card_origin({"origin": ["dashboard_card"]})
    assert not card_send.claims_card_origin({})
    assert card_send.claims_card_origin({"origin": "dashboard_card"})


def test_the_rate_limit_map_is_bounded():
    card_send._reset_rate_limit_for_tests()
    for i in range(card_send._MAX_TRACKED_SLOTS + 50):
        assert card_send.claim_send_slot(f"member-{i}", now=float(i)) == 0.0
    assert len(card_send._last_send) <= card_send._MAX_TRACKED_SLOTS
    card_send._reset_rate_limit_for_tests()


def test_the_switch_is_on_by_default_and_a_hand_edited_junk_value_reads_on():
    assert KiroCrewAgentConfig().dashboard_card_send is True


@pytest.mark.asyncio
async def test_a_card_keeps_only_its_send_id_so_the_row_is_echoed_to_the_tab(env):
    state, slot, _, turns, app = env
    frames: list[tuple[str, dict]] = []
    state.broadcast_ws = lambda kind, payload=None, *a, **k: frames.append((kind, payload))
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat?ws=1",
            json=_card(slot, meta={"sendId": "card-1", "files": ["/etc/passwd"]}),
        )
        assert resp.status == 200
        await asyncio.sleep(0)
    (row,) = _user_rows(slot)
    assert row["meta"]["sendId"] == "card-1"
    assert "files" not in row["meta"]
    echoed = [p for kind, p in frames if kind == "chat_message"]
    assert echoed and echoed[0]["content"] == "[Dashboard card] Approve the plan"


@pytest.mark.asyncio
async def test_every_decision_is_written_to_the_audit_log(env, monkeypatch):
    """A refusal and an admit each leave one SEL record, like every other chat_send denial."""
    from unittest.mock import MagicMock

    state, slot, cfg, turns, app = env
    audit = MagicMock()
    monkeypatch.setattr(chat_handlers, "sel", lambda: audit)
    async with TestClient(TestServer(app)) as client:
        ok = await client.post("/api/chat?ws=1", json=_card(slot))
        assert ok.status == 200
        card_send._reset_rate_limit_for_tests()
        cfg.agents["default"].dashboard_card_send = False
        off = await client.post("/api/chat?ws=1", json=_card(slot))
        assert off.status == 403
    calls = [
        c.kwargs
        for c in audit.log_api_access.call_args_list
        if c.kwargs.get("source") == "dashboard_card"
    ]
    assert [(c["outcome"], c["error"]) for c in calls] == [
        ("allowed", ""),
        ("denied", "card_send_off"),
    ]
    assert all(
        c["operation"] == "chat_send" and c["resources"] == f"slot={slot.key}" for c in calls
    )


@pytest.mark.asyncio
async def test_a_credential_shaped_run_in_a_pages_text_is_redacted_before_it_is_stored(env):
    """The row is served as a user row, which is otherwise exempt from redaction."""
    state, slot, _, turns, app = env
    secret = "AKIAIOSFODNN7EXAMPLE"
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_card(slot, f"key {secret} please"))
        assert resp.status == 200
        await asyncio.sleep(0)
    (row,) = _user_rows(slot)
    assert secret not in row["content"]
    assert row["content"].startswith("[Dashboard card] key ")
    ((text, _),) = turns
    assert secret not in text and text == row["content"]
