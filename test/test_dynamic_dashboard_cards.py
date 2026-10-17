"""Event-driven card production has a budget; reads never buy model work.

Turning cards on and off -- the config opt-in, the toggle and the gateway's activation and
cleanup -- is in ``test_dynamic_dashboard_cards_activation.py``; the fixtures both modules
share are in ``dynamic_dashboard_cards_helpers.py``.
"""

import asyncio
import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from dynamic_dashboard_cards_helpers import _seeded_session_tree  # noqa: F401
from dynamic_dashboard_cards_helpers import lifecycle_fixture  # noqa: F401
from dynamic_dashboard_cards_helpers import _parked, _worker_done

from kiro_crew.crew_main_contract import CARD_FIELDS, PHRASES_FIELD
from kiro_crew.dashboard.dynamic_cards import CardBudget, CardPublisher, normalize_card
from kiro_crew.testing.wait import await_until


def test_layout_can_be_reused_but_first_publication_needs_one():
    first = normalize_card(
        {"html": '<p data-dashboard-field="result"></p>', "data": {"result": "A"}}
    )
    assert first is not None
    second = normalize_card({"data": {"result": "B"}}, first)
    assert second == {"html": first["html"], "data": {"result": "B"}}
    assert normalize_card({"data": {"result": "B"}}) is None


@pytest.mark.parametrize("data", [{}, {"renamed": "B"}, {"result": "B", "extra": "C"}])
def test_data_only_updates_preserve_the_complete_field_contract(data):
    previous = {"html": '<p data-dashboard-field="result"></p>', "data": {"result": "A"}}
    assert normalize_card({"data": data}, previous) is None
    replacement = {"html": '<p data-dashboard-field="renamed"></p>', "data": {"renamed": "B"}}
    assert normalize_card(replacement, previous) == replacement


def test_payload_limits_count_utf8_and_refuse_nested_data():
    assert normalize_card({"html": "界" * 8192, "data": {}}) is None
    assert normalize_card({"html": "<p>ok</p>", "data": {"x": {"html": "bad"}}}) is None
    assert normalize_card({"html": "<p>ok</p>", "data": {"x": "界" * 2048}}) is None


@pytest.mark.asyncio
async def test_events_coalesce_and_reads_do_not_generate():
    calls = []
    now = SimpleNamespace(value=0.0)

    async def generate(entry):
        calls.append(entry.reason)
        return {"html": "<p>ok</p>", "data": {}}

    publisher = CardPublisher(
        generate,
        lambda entry: True,
        lambda key: None,
        clock=lambda: now.value,
        wall_clock=lambda: 100 + now.value,
    )
    owner = object()
    publisher.notify("one", owner, "turn", "started")
    publisher.notify("one", owner, "turn", "question")
    for _ in range(10):
        assert publisher.read("one")["published_at"] is None
    assert not calls
    now.value = 2
    assert await publisher.run_ready() == 1
    assert calls == ["question"]
    snapshot = publisher.read("one")
    assert snapshot["published_at"] == 102
    now.value = 50
    assert publisher.read("one")["published_at"] == 102
    assert await publisher.run_ready() == 0


@pytest.mark.asyncio
async def test_cooldown_global_budget_and_failures_count_attempts():
    now = SimpleNamespace(value=0.0)
    calls = []

    async def generate(entry):
        calls.append(entry.key)
        raise ValueError("bad response")

    budget = CardBudget(debounce=0, per_session=120, per_hour=2, capacity=4)
    publisher = CardPublisher(
        generate, lambda entry: True, lambda key: None, budget=budget, clock=lambda: now.value
    )
    owner = object()
    publisher.notify("one", owner, "binding", "failed")
    assert await publisher.run_ready() == 1
    publisher.notify("one", owner, "binding", "completed")
    assert await publisher.run_ready() == 0
    now.value = 120
    assert await publisher.run_ready() == 1
    publisher.notify("two", object(), "binding", "completed")
    assert await publisher.run_ready() == 0
    assert publisher.read("two")["status"] == "budget"
    now.value = 3601
    assert await publisher.run_ready() == 1
    assert calls == ["one", "one", "two"]


@pytest.mark.asyncio
async def test_default_budget_enforces_the_config_help_limits():
    from dataclasses import fields

    from kiro_crew.config.sections import DashboardConfig

    setting = next(f for f in fields(DashboardConfig) if f.name == "dynamic_dashboard_cards")
    assert setting.default is False
    assert "one per session every two minutes" in setting.metadata["help"]
    assert "60 per gateway hour" in setting.metadata["help"]
    now = SimpleNamespace(value=0.0)
    calls = []

    async def generate(entry):
        calls.append(entry.key)
        if len(calls) % 2 == 0:
            raise ValueError("synthetic failed attempt")
        return {"html": "<p>ok</p>", "data": {}}

    # No budget override: exercise the same defaults as CardLifecycle.
    publisher = CardPublisher(
        generate, lambda entry: True, lambda key: None, clock=lambda: now.value
    )
    publisher.notify("one", "owner", "binding", "done")
    now.value = 2
    assert await publisher.run_ready() == 1
    publisher.notify("one", "owner", "binding", "done")
    now.value = 121.999
    assert await publisher.run_ready() == 0
    now.value = 122
    assert await publisher.run_ready() == 1
    assert publisher.read("one")["status"] == "failed"
    for i in range(59):
        publisher.notify(str(i), "owner", str(i), "done")
    now.value = 124
    for _ in range(58):
        assert await publisher.run_ready() == 1
    assert len(calls) == len(publisher.attempts) == 60
    assert await publisher.run_ready() == 0
    assert publisher.read("58")["status"] == "budget"
    now.value = 3601.999
    assert await publisher.run_ready() == 0
    now.value = 3602
    assert await publisher.run_ready() == 1
    assert len(calls) == 61


@pytest.mark.asyncio
async def test_forget_invalidates_only_removed_entries_without_refunding_attempts():
    changes = []

    async def generate(entry):
        return {"html": "<p>old occupant</p>", "data": {}}

    publisher = CardPublisher(
        generate,
        lambda entry: True,
        changes.append,
        budget=CardBudget(debounce=0),
        clock=lambda: 1,
    )
    publisher.notify("one", object(), "binding", "done")
    await publisher.run_ready()
    publisher.notify("other", object(), "other-binding", "done")
    changes.clear()
    publisher.forget("one")
    publisher.forget("one")
    publisher.forget("missing")
    assert changes == ["one"]
    assert publisher.read("one") is None
    assert publisher.read("other") is not None
    assert list(publisher.attempts) == [1]
    assert publisher.last_attempt == {"binding": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_removal", [False, True])
async def test_one_inflight_latest_event_survives_and_removal_wins(explicit_removal):
    entered, release = asyncio.Event(), asyncio.Event()
    now = SimpleNamespace(value=0.0)
    current = {"one": object()}

    async def generate(entry):
        entered.set()
        await await_until(release, what="the test's release gate")
        return {"html": "<p>old</p>", "data": {}}

    publisher = CardPublisher(
        generate,
        lambda entry: current.get(entry.key) is entry.owner,
        lambda key: None,
        budget=CardBudget(debounce=0),
        clock=lambda: now.value,
    )
    publisher.notify("one", current["one"], "binding", "question")
    task = asyncio.create_task(publisher.run_ready())
    try:
        await await_until(entered, what="the generation starting")
        publisher.notify("one", current["one"], "binding", "completed")
        assert await publisher.run_ready() == 0
        assert publisher.read("one")["status"] == "generating"
        current.clear()
        if explicit_removal:
            publisher.forget("one")
            assert publisher.active is not None
            assert await publisher.run_ready() == 0
    finally:
        release.set()
        await await_until(task, what="the parked task finishing")
    assert publisher.read("one") is None
    assert list(publisher.attempts) == [0]
    assert publisher.last_attempt == {"binding": 0}


@pytest.mark.parametrize("change", ["owner", "binding", "ordinary"])
def test_replacement_clears_presentation_before_installing_new_entry(change):
    events = []
    publisher = CardPublisher(
        None,
        lambda entry: True,
        lambda key: events.append((key, key not in publisher.entries, publisher.read(key))),
        budget=CardBudget(capacity=1),
        clock=lambda: 1,
    )
    publisher.notify("one", "owner", "binding", "restored")
    original = publisher.entries["one"]
    original.payload = {"html": "old occupant", "data": {}}
    publisher.attempts.append(1)
    publisher.last_attempt["binding"] = 1
    events.clear()
    publisher.notify(
        "one",
        "replacement" if change == "owner" else "owner",
        "new-binding" if change == "binding" else "binding",
        "activity",
    )
    if change == "ordinary":
        # Already queued and already stale: a reader would see nothing new.
        assert events == []
        assert publisher.read("one")["card"] == original.payload
        assert publisher.entries["one"] is original
        assert publisher.entries["one"].reason == "activity"
    else:
        assert events[0] == ("one", True, None)
        assert len(events) == 2 and events[1][1] is False
        assert events[1][2]["card"] is None
        assert publisher.entries["one"] is not original
    assert len(publisher.entries) == 1
    assert list(publisher.attempts) == [1]
    assert publisher.last_attempt == {"binding": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "binding"])
async def test_inflight_old_generation_cannot_publish_over_replacement(change):
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(entry):
        entered.set()
        await await_until(release, what="the test's release gate")
        return {"html": "old occupant", "data": {}}

    publisher = CardPublisher(
        generate,
        lambda entry: True,
        lambda key: None,
        budget=CardBudget(debounce=0, capacity=1),
        clock=lambda: 1,
    )
    publisher.notify("one", "owner", "binding", "activity")
    original = publisher.entries["one"]
    task = asyncio.create_task(publisher.run_ready())
    try:
        await await_until(entered, what="the generation starting")
        publisher.notify(
            "one",
            "replacement" if change == "owner" else "owner",
            "new-binding" if change == "binding" else "binding",
            "activity",
        )
        replacement = publisher.entries["one"]
        assert publisher.active is original
        assert await publisher.run_ready() == 0
        assert publisher.read("one")["card"] is None
    finally:
        release.set()
        await await_until(task, what="the parked task finishing")
    assert publisher.active is None
    assert publisher.entries["one"] is replacement
    assert replacement.pending
    assert publisher.read("one")["card"] is None
    assert list(publisher.attempts) == [1]
    assert publisher.last_attempt == {"binding": 1}


def test_pending_and_cache_are_bounded_and_rebinding_discards_content():
    publisher = CardPublisher(
        None, lambda entry: True, lambda key: None, budget=CardBudget(capacity=3)
    )
    owner = object()
    for i in range(100):
        publisher.notify(str(i), owner, "binding", "turn")
    assert len(publisher.entries) == 3
    assert publisher.read("0") is None
    publisher.entries["99"].payload = {"html": "old", "data": {}}
    publisher.notify("99", owner, "new-binding", "turn")
    assert publisher.read("99")["card"] is None


@pytest.mark.asyncio
async def test_real_slot_events_ignore_replay_and_stream_reader_does_not_hide_events():
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("one")
    events = []
    slot._on_card_event = lambda owner, reason: events.append((owner, reason))
    slot._has_reader = True
    slot.append("assistant", "historic", broadcast=False)
    slot.append("chunk", "streaming token")
    slot.append("user", "Build it")
    slot.append("assistant", "Stage complete")
    slot.append("error", "Test failed")
    slot.append("done", "")
    assert [reason for owner, reason in events] == ["user", "assistant", "error", "done"]
    assert all(owner is slot for owner, _ in events)


def test_same_key_empty_replacement_invalidates_the_previous_card(lifecycle, monkeypatch):
    service, slot, state = lifecycle
    frames = []
    monkeypatch.setattr(state, "broadcast_ws_owners", lambda *args: frames.append(args))
    service.publisher.notify(slot.key, slot._dashboard_card_identity, "dashboard:one", "done")
    service.publisher.entries[slot.key].payload = {"html": "old occupant", "data": {}}
    replacement = SimpleNamespace(**vars(slot))
    replacement._dashboard_card_identity = "replacement"
    replacement.messages = []
    state._slots[slot.key] = replacement
    frames.clear()
    service.notify(replacement, "restored")
    service.notify(replacement, "restored")
    assert service.publisher.read(slot.key) is None
    assert frames == [("dashboard_card", {"slot": slot.key, "removed": True})]
    assert service.worker is None


@pytest.mark.parametrize("source", ["scratch", "previous-owner"])
def test_unregistered_slot_callback_cannot_invalidate_live_card(lifecycle, monkeypatch, source):
    from kiro_crew.dashboard.state import _ChatSlot

    service, _, state = lifecycle
    live = _ChatSlot("one")
    live.append("user", "Committed work", broadcast=False)
    live._on_card_event = service.notify
    if source == "scratch":
        sender = copy.copy(live)
        sender.messages = list(live.messages)
        sender._pending = []
        sender._question_pending = {}
        sender.event = asyncio.Event()
    else:
        sender = live
        live = _ChatSlot("one")
        live.append("user", "Replacement work", broadcast=False)
    state._slots["one"] = live
    service.publisher.notify("one", live._dashboard_card_identity, "dashboard:one", "done")
    entry = service.publisher.entries["one"]
    entry.payload = {"html": "Published live evidence", "data": {}}
    entry.pending = False
    frames = []
    monkeypatch.setattr(state, "broadcast_ws_owners", lambda *args: frames.append(args))
    sender.append("user", "Uncommitted edit")
    assert service.publisher.entries.get("one") is entry
    assert service.publisher.read("one")["card"] == entry.payload
    assert not entry.pending
    assert frames == []
    assert service.worker is None
    assert not service.wake.is_set()
    assert not service.publisher.attempts
    assert not service.publisher.last_attempt


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("cached_owner", ["retired", "replacement", None])
def test_retired_callback_forgets_only_its_own_card(
    lifecycle, monkeypatch, registered, cached_owner
):
    from kiro_crew.dashboard.state import _ChatSlot

    service, _, state = lifecycle
    retired = _ChatSlot("one")
    replacement = _ChatSlot("one")
    state._slots = {"one": replacement} if registered else {}
    entry = None
    if cached_owner is not None:
        owner = retired if cached_owner == "retired" else replacement
        service.publisher.notify("one", owner._dashboard_card_identity, "dashboard:one", "done")
        entry = service.publisher.entries["one"]
        entry.payload = {"html": "Published evidence", "data": {}}
        entry.pending = False
    service.publisher.attempts.append(7)
    service.publisher.last_attempt["one"] = 7
    frames = []
    monkeypatch.setattr(state, "broadcast_ws_owners", lambda *args: frames.append(args))
    service.notify(retired, "done")
    service.notify(retired, "done")
    if cached_owner == "retired":
        assert "one" not in service.publisher.entries
        assert frames == [("dashboard_card", {"slot": "one", "removed": True})]
    else:
        assert service.publisher.entries.get("one") is entry
        assert frames == []
    assert service.worker is None
    assert not service.wake.is_set()
    assert list(service.publisher.attempts) == [7]
    assert service.publisher.last_attempt == {"one": 7}


@pytest.mark.parametrize("cached_owner", ["retired", "replacement"])
def test_slot_removal_evicts_a_predecessor_card_under_a_registered_replacement(
    lifecycle, monkeypatch, cached_owner
):
    """A same-name replacement can be registered before the close's removal push
    runs, and neither slot need fire a card event afterwards. The removal itself
    judges the cached card by owner: the retired slot's card goes, so the
    replacement never presents it. Negative control: a card the replacement
    already owns stays, and nothing is broadcast for it."""
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

    service, _, state = lifecycle
    state._dynamic_cards = service
    retired = _ChatSlot("one")
    replacement = _ChatSlot("one")
    state._slots = {"one": replacement}
    owner = retired if cached_owner == "retired" else replacement
    service.publisher.notify("one", owner._dashboard_card_identity, "dashboard:one", "done")
    entry = service.publisher.entries["one"]
    entry.payload = {"html": "Published evidence", "data": {}}
    entry.pending = False
    frames = []
    monkeypatch.setattr(state, "broadcast_ws_owners", lambda *args: frames.append(args))
    snapshots = []
    state.push_slots_update = lambda: snapshots.append(True)

    DashboardState.push_slot_removed(state, "one")

    assert snapshots == [True]
    assert state._slots["one"] is replacement
    if cached_owner == "retired":
        assert "one" not in service.publisher.entries
        assert frames == [("dashboard_card", {"slot": "one", "removed": True})]
    else:
        assert service.publisher.entries.get("one") is entry
        assert frames == []


@pytest.mark.asyncio
async def test_cancelled_turn_invalidates_old_card_after_same_key_recreation(
    lifecycle, monkeypatch
):
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

    service, _, state = lifecycle
    state._dynamic_cards = service
    retired = _ChatSlot("one")
    retired._on_card_event = service.notify
    state._slots["one"] = retired
    service.publisher.notify("one", retired._dashboard_card_identity, "dashboard:one", "done")
    service.publisher.entries["one"].payload = {"html": "Old occupant", "data": {}}
    frames = []
    monkeypatch.setattr(state, "broadcast_ws_owners", lambda *args: frames.append(args))
    started, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def turn():
        started.set()
        try:
            await _parked()
        except asyncio.CancelledError:
            cancelling.set()
            await await_until(release, what="the test's release gate")
            retired.append("done", "Cancelled")

    task = asyncio.create_task(turn())
    try:
        await await_until(started, what="the turn starting")
        # Close removes the registry owner before awaiting its cancelled turn.
        state._slots.pop("one")
        task.cancel()
        await await_until(cancelling, what="the cancellation reaching the generator")
        replacement = _ChatSlot("one")
        state._slots["one"] = replacement
        release.set()
        await await_until(task, what="the parked task finishing")
        # This removal path sends a full snapshot, not a card invalidation.
        snapshots = []
        state.push_slots_update = lambda: snapshots.append(True)
        DashboardState.push_slot_removed(state, "one")
        assert snapshots == [True]
        assert state._slots["one"] is replacement
        assert "one" not in service.publisher.entries
        assert frames == [("dashboard_card", {"slot": "one", "removed": True})]
        assert service.worker is None
        assert not service.publisher.attempts
        assert not service.publisher.last_attempt
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await await_until(asyncio.gather(task, return_exceptions=True), what="the turn settling")


@pytest.mark.parametrize("invalidate", ["empty", "incognito", "temporary", "executor", "worker"])
def test_registered_slot_invalidation_still_forgets_card(lifecycle, monkeypatch, invalidate):
    service, slot, state = lifecycle
    service.publisher.notify(slot.key, slot._dashboard_card_identity, "dashboard:one", "done")
    service.publisher.entries[slot.key].payload = {"html": "Old evidence", "data": {}}
    frames = []
    monkeypatch.setattr(state, "broadcast_ws_owners", lambda *args: frames.append(args))
    if invalidate == "empty":
        slot.messages = []
    elif invalidate in {"incognito", "temporary"}:
        slot.memory_mode = invalidate
    elif invalidate == "worker":
        slot._created_by = "conductor"
    else:
        slot.executor = "remote"
    service.notify(slot, "done")
    assert service.publisher.read(slot.key) is None
    assert frames == [("dashboard_card", {"slot": slot.key, "removed": True})]
    assert service.worker is None


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["edit-resend", "rewind"])
@pytest.mark.parametrize("commit", [False, True])
async def test_edit_boundary_preserves_card_until_real_rewrite(
    tmp_path, monkeypatch, endpoint, commit
):
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_regenerate, chat_rewind
    from kiro_crew.dashboard.card_lifecycle import CardLifecycle

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("one")
    slot.append("user", "Original question")
    slot.append("assistant", "Original answer")
    await asyncio.to_thread(state.flush_slot_now, slot)
    service = CardLifecycle(state, enabled=True)
    state._dynamic_cards = service
    slot._on_card_event = service.notify
    service.publisher.notify(slot.key, slot._dashboard_card_identity, "dashboard:one", "done")
    entry = service.publisher.entries[slot.key]
    entry.pending = False
    entry.payload = {"html": "Original answer", "data": {}}
    entry.published_source = (
        state.conversation_log.rotation_generation("dashboard:one"),
        ("dashboard:one",),
    )
    assert (await service.read(slot))["card"] == entry.payload
    frames = MagicMock()
    monkeypatch.setattr(state, "broadcast_ws_owners", frames)
    state.sessions.discard_conversation = AsyncMock(return_value=commit)
    state.sessions._session_map.get = MagicMock(return_value="")
    module = chat_regenerate if endpoint == "edit-resend" else chat_rewind
    monkeypatch.setattr(module, "_run_chat", AsyncMock())
    handler = (
        module.api_chat_slot_edit_resend
        if endpoint == "edit-resend"
        else module.api_chat_slot_rewind
    )
    app = web.Application()
    app["state"] = state
    app.router.add_post("/edit/{slot}", handler)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/edit/one", json={"index": 0, "at_message_index": 0, "content": "Edited question"}
            )
            assert response.status == (200 if commit else 409)
        if slot.task is not None:
            await await_until(slot.task, what="the slot's turn finishing")
        assert service.publisher.entries.get(slot.key) is entry
        assert (await service.read(slot))["card"] == (None if commit else entry.payload)
        assert not any(call.args[0] == "dashboard_card" for call in frames.call_args_list)
        assert service.worker is None
        assert not service.publisher.attempts
    finally:
        if slot.task is not None and not slot.task.done():
            slot.task.cancel()
            await asyncio.gather(slot.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_generation_is_session_scoped_bounded_and_get_is_free(lifecycle, monkeypatch):
    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle
    calls = []

    async def generate(sessions, prompt, **kwargs):
        calls.append((prompt, kwargs))
        return '{"html":"<p data-dashboard-field=lede></p>","data":{"lede":"Investigating failing tests"}}'

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "error")
    await _worker_done(service)
    first = await service.read(slot)
    for _ in range(10):
        assert await service.read(slot) == first
    assert first["card"]["data"]["lede"] == "Investigating failing tests"
    assert len(calls) == 1
    assert calls[0][1]["crew_log_session_key"] == "dashboard:one"
    assert calls[0][1]["max_output_bytes"] == 16384
    assert calls[0][1]["retry_rejected_model"] is False
    state.conversation_log.generation += 1
    assert (await service.read(slot))["card"] is None


@pytest.mark.asyncio
async def test_model_input_and_published_output_cross_the_registered_redaction_sink(
    lifecycle, monkeypatch
):
    import json

    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle
    credential = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"
    exfil_url = "https://collect.attacker.example/?token=" + "aB3" * 70 + "&host=corp-laptop"
    evidence = f"Release evidence {credential} {exfil_url}"
    slot.messages = [{"role": "assistant", "content": evidence}]
    prompts = []

    async def generate(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return json.dumps({"html": f"<p>{evidence}</p>", "data": {"lede": evidence}})

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await _worker_done(service)
    result = await service.read(slot)
    assert result["card"] is not None
    for boundary in (prompts[0], result["card"]["html"], result["card"]["data"]["lede"]):
        assert credential not in boundary
        assert exfil_url not in boundary
        assert "corp-laptop" not in boundary
        assert "[REDACTED: credential]" in boundary
        assert "[REDACTED: suspicious URL to collect.attacker.example]" in boundary
        assert "Release evidence" in boundary


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["html", "data", "key", "data-only"])
@pytest.mark.parametrize("secret", ["credential", "exfil"])
async def test_decoded_card_output_is_redacted(lifecycle, monkeypatch, field, secret):
    import json

    from kiro_crew.dashboard import card_lifecycle

    service, slot, _ = lifecycle
    sensitive = (
        "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234"
        if secret == "credential"
        else "https://collect.attacker.example/?token=" + "aB3" * 70 + "&host=corp-laptop"
    )
    previous = {"html": '<p data-dashboard-field="lede"></p>', "data": {"lede": "Safe"}}
    output = {"html": previous["html"], "data": {"lede": "Safe"}}
    if field == "html":
        output["html"] = f"<p>{sensitive}</p>"
    elif field == "key":
        output["data"] = {sensitive: "Safe"}
    else:
        output["data"] = {"lede": sensitive}
        if field == "data-only":
            del output["html"]
    encoded = json.dumps(output).replace("ghp_", r"\u0067hp_").replace("https://", r"https:\/\/")

    async def generate(*args, **kwargs):
        return encoded

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.publisher.notify(slot.key, slot._dashboard_card_identity, "dashboard:one", "done")
    entry = service.publisher.entries[slot.key]
    entry.payload = previous
    await service.publisher.run_ready()
    result = service.publisher.read(slot.key)
    assert sensitive not in json.dumps(result["card"])
    if field == "key":
        assert result["status"] == "failed"
        assert result["card"] == previous
    else:
        assert result["status"] == "published"
        # The model's sentence plus the host's folded numbers, and nothing else.
        assert set(result["card"]["data"]) == CARD_FIELDS | {PHRASES_FIELD}
        if field == "data-only":
            assert result["card"]["html"] == previous["html"]
    assert len(service.publisher.attempts) == 1


def test_decoded_card_output_retains_contextual_credential_checks():
    import json

    from kiro_crew.dashboard.card_lifecycle import _redact, _redact_card_output

    # This family is identified by its field label, not by the value alone.
    raw = {"html": "<p>Safe</p>", "data": {"aws_secret_access_key": "A" * 40}}
    text = json.dumps(raw)
    assert _redact(text) != text
    assert _redact_card_output(text, None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["decimal", "hex", "named"])
@pytest.mark.parametrize("secret", ["credential", "exfil"])
@pytest.mark.parametrize("attribute", [False, True])
@pytest.mark.parametrize("last_good", [False, True])
async def test_html_entities_cannot_publish_decoded_secrets(
    lifecycle, monkeypatch, encoding, secret, attribute, last_good
):
    import html
    import json

    from kiro_crew.dashboard import card_lifecycle

    sensitive = (
        "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234"
        if secret == "credential"
        else "https://collect.attacker.example/?payload=" + "aB3" * 70 + "&host=corp-laptop"
    )
    character = "_" if secret == "credential" else ":"
    reference = {
        "decimal": f"&#{ord(character)};",
        "hex": f"&#x{ord(character):x};",
        "named": "&lowbar;" if secret == "credential" else "&colon;",
    }[encoding]
    encoded = sensitive.replace(character, reference, 1)
    markup = f'<p title="{encoded}">Result</p>' if attribute else f"<p>{encoded}</p>"
    assert sensitive not in markup
    assert card_lifecycle._redact(markup) == markup
    assert card_lifecycle._redact(html.unescape(markup)) != html.unescape(markup)
    calls = []

    async def generate(*args, **kwargs):
        calls.append(True)
        return json.dumps({"html": markup, "data": {}})

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service, slot, _ = lifecycle
    service.publisher.notify(slot.key, slot._dashboard_card_identity, "dashboard:one", "done")
    previous = {"html": "<p>Last good</p>", "data": {}} if last_good else None
    service.publisher.entries[slot.key].payload = previous
    assert await service.publisher.run_ready() == 1
    result = service.publisher.read(slot.key)
    assert result["status"] == "failed"
    assert result["card"] == previous
    assert len(service.publisher.attempts) == len(calls) == 1
    assert await service.publisher.run_ready() == 0
    assert len(calls) == 1


def test_safe_html_entities_and_literal_data_survive_without_reserialization():
    import json

    from kiro_crew.dashboard.card_lifecycle import _redact_card_output

    markup = (
        '<p title="A &amp; B">&#65; &#x42; &copy; &unknown;</p><b data-dashboard-field="x"></b>'
    )
    literal = "ghp&lowbar;" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234"
    original = {"html": markup, "data": {"x": literal}}
    assert _redact_card_output(json.dumps(original), None) == original
    assert _redact_card_output(json.dumps({"data": {"x": literal}}), original) == original
    assert _redact_card_output(json.dumps({"data": {"renamed": literal}}), original) is None


_SPLIT_LABEL_MARKUP = "<p><b>aws_secret_access_key:</b> <code>" + "A" * 40 + "</code></p>"


def test_markup_cannot_hold_a_labelled_credential_apart_from_its_label():
    """A labelled credential is one run of text; a tag between the label and the
    value misleads a raw scan, not the browser. The output side refuses
    the card. Negative control: the same text with the tag boundary closed is caught
    by the raw scan, and a layout that names the label with no value publishes."""
    import json

    from kiro_crew.dashboard.card_lifecycle import _redact, _redact_card_output

    # The raw scan takes the closing tag for the value: label gone, value kept.
    assert "A" * 40 in _redact(_SPLIT_LABEL_MARKUP)
    assert _redact_card_output(json.dumps({"html": _SPLIT_LABEL_MARKUP, "data": {}}), None) is None
    joined = "<p>aws_secret_access_key: " + "A" * 40 + "</p>"
    assert _redact(joined) != joined
    # Parity with plain text is the bar: a value the text scan takes for the
    # credential is refused here too, and a label with no value publishes.
    assert (
        _redact_card_output(
            json.dumps(
                {"html": "<p><b>aws_secret_access_key:</b> <code>rotated</code></p>", "data": {}}
            ),
            None,
        )
        is None
    )
    labelled = {"html": "<p><b>aws_secret_access_key:</b> <code></code></p>", "data": {}}
    assert _redact_card_output(json.dumps(labelled), None) is not None


@pytest.mark.parametrize("reference", ["&#65;", "&#x41;", "&sol;"])
def test_markup_cannot_hide_a_split_credential_behind_a_character_reference(reference):
    """The value the browser shows is compared with the markup as the browser reads
    it. A value spelt with a character reference is the same value to the browser,
    so the split-label refusal holds for it. Negative control: the same encoded value
    with no label beside it is not a labelled credential, and publishes."""
    import html
    import json

    from kiro_crew.dashboard.card_lifecycle import _hides_secret, _redact, _redact_card_output

    encoded = reference + "A" * 39
    markup = "<p><b>aws_secret_access_key:</b> <code>" + encoded + "</code></p>"
    assert html.unescape(encoded) != encoded
    # The raw scan neither decodes the value nor keeps the label beside it.
    assert encoded in _redact(markup)
    assert _hides_secret(markup)
    assert _redact_card_output(json.dumps({"html": markup, "data": {}}), None) is None
    unlabelled = {"html": "<p><code>" + encoded + "</code></p>", "data": {}}
    assert not _hides_secret(unlabelled["html"])
    assert _redact_card_output(json.dumps(unlabelled), None) == unlabelled


@pytest.mark.parametrize(
    "split",
    [
        "<span>AKIAIOSFOD</span><span>NN7EXAMPLE</span>",
        "AKIAIOS<b></b>FODNN7EXAMPLE",
        "<p>AKIAIOSFOD</p><p>NN7EXAMPLE</p>",
    ],
)
def test_markup_cannot_split_a_credential_token_across_tags(split):
    """An inline tag boundary joins its neighbours in the text a browser shows, so a
    token cut by tags is still one token on screen. The scanner does not lay the
    page out, so a block boundary is read both ways too and the split is refused
    rather than guessed at. Negative control: the whole token inside one element is
    caught by the raw scan and published redacted, and two tokens that form no
    credential when joined publish untouched."""
    import json

    from kiro_crew.dashboard.card_lifecycle import _hides_secret, _redact, _redact_card_output

    token = "AKIAIOSFODNN7EXAMPLE"
    assert _redact("<p>" + token + "</p>") != "<p>" + token + "</p>"
    markup = "<p>" + split + "</p>"
    assert token not in markup
    assert _redact(markup) == markup
    assert _hides_secret(markup)
    assert _redact_card_output(json.dumps({"html": markup, "data": {}}), None) is None
    whole = "<p><span>" + token + "</span></p>"
    assert not _hides_secret(whole)
    published = _redact_card_output(json.dumps({"html": whole, "data": {}}), None)
    assert published is not None and token not in published["html"]
    benign = {"html": "<p><span>release</span><span>notes</span></p>", "data": {}}
    assert _redact_card_output(json.dumps(benign), None) == benign


def test_markup_cannot_split_a_suspicious_url_across_tags():
    """The exfiltration heuristics judge a URL by its shape, not by a label, so a
    URL cut by an inline tag is whole on screen and in neither half for the raw
    scan. The shown text is scanned again after the raw scan; a URL it still
    redacts was kept from that scan by markup, and the card is refused. Negative
    control: the same URL inside one element is redacted by the raw scan and
    published without it."""
    import json

    from kiro_crew.dashboard.card_lifecycle import _hides_secret, _redact, _redact_card_output

    url = "https://collect.attacker.example/?payload=" + "aB3" * 70 + "&host=corp-laptop"
    cut = url.index("?") + 1
    markup = "<p><span>" + url[:cut] + "</span><span>" + url[cut:] + "</span></p>"
    # Neither half is a suspicious URL on its own: the raw scan leaves the markup alone.
    assert _redact(markup) == markup
    assert _hides_secret(markup)
    assert _redact_card_output(json.dumps({"html": markup, "data": {}}), None) is None
    whole = "<p><span>" + url + "</span></p>"
    assert not _hides_secret(whole)
    published = _redact_card_output(json.dumps({"html": whole, "data": {}}), None)
    assert published is not None and "aB3" * 10 not in published["html"]


@pytest.mark.asyncio
async def test_model_input_omits_a_message_whose_markup_splits_a_labelled_credential(
    lifecycle, monkeypatch
):
    """The input side omits the whole message, as it does an oversized one: the raw
    scan cannot place the value, so nothing of it may reach the model. A sibling
    message without the split is delivered."""
    import json

    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle
    slot.messages = [
        {"role": "assistant", "content": "Release evidence attached"},
        {"role": "tool_result", "content": _SPLIT_LABEL_MARKUP},
    ]
    prompts = []

    async def generate(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return json.dumps({"html": "<p>ok</p>", "data": {}})

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await _worker_done(service)
    assert prompts and "A" * 40 not in prompts[0]
    assert "aws_secret_access_key" not in prompts[0]
    assert "Release evidence attached" in prompts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("padding", ['"', "界"])
async def test_maximum_previous_card_leaves_room_for_escaped_recent_evidence(
    lifecycle, monkeypatch, padding
):
    import json

    from kiro_crew.dashboard import card_lifecycle
    from kiro_crew.dashboard.dynamic_cards import MAX_DATA_BYTES, MAX_HTML_BYTES, MAX_INPUT_CHARS

    service, slot, state = lifecycle
    service.publisher.budget = CardBudget(debounce=0, per_session=0)
    html = '<p data-dashboard-field="lede"></p>' + padding * 1000
    html += " " * (MAX_HTML_BYTES - len(html.encode("utf-8")))
    previous = {"html": html, "data": {"lede": "A" * (MAX_DATA_BYTES - len("lede"))}}
    assert normalize_card(previous) == previous
    prompts = []

    async def generate(_sessions, prompt, **kwargs):
        prompts.append(prompt)
        return json.dumps(
            (
                {"html": html, "data": {"lede": "Ready"}}
                if len(prompts) == 1
                else {"data": {"lede": "Updated"}}
            ),
            ensure_ascii=False,
        )

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await _worker_done(service)
    first = (await service.read(slot))["card"]
    # A maximum-size layout is kept whole. The sentences are the model's only data now,
    # so the escape-heavy bulk that must still leave room for evidence is the layout.
    assert first["html"] == html
    assert first["data"]["lede"] == "Ready"
    slot.messages = [{"role": "assistant", "content": 'New evidence 界 "\\\n\x01' * 500}]
    service.notify(slot, "completed")
    await _worker_done(service)
    assert len(prompts) == 2
    assert all(len(prompt) <= MAX_INPUT_CHARS for prompt in prompts)
    context = json.loads(prompts[1][len(card_lifecycle._ROOT_PROMPT) :])
    assert "New evidence" in context["recent_messages"][-1]["text"]
    assert "lede" in context["previous"].get("fields", context["previous"].get("data", {}))
    updated = (await service.read(slot))["card"]
    assert updated["html"] == html
    assert updated["data"]["lede"] == "Updated"
    assert len(service.publisher.attempts) == 2


@pytest.mark.asyncio
async def test_invalid_data_only_result_preserves_publication_and_charges_attempt(
    lifecycle, monkeypatch
):
    import json

    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle
    service.publisher.budget = CardBudget(debounce=0, per_session=0)
    responses = iter(
        [
            {"html": '<p data-dashboard-field="lede"></p>', "data": {"lede": "Good"}},
            {"data": {"renamed": "Bad"}},
            {"data": {"lede": "Better"}},
        ]
    )

    async def generate(*args, **kwargs):
        return json.dumps(next(responses))

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await _worker_done(service)
    previous = await service.read(slot)
    service.notify(slot, "done")
    await _worker_done(service)
    rejected = await service.read(slot)
    assert rejected["status"] == "failed"
    for field in ("card", "published_at", "content_event_at"):
        assert rejected[field] == previous[field]
    assert service.publisher.next_delay() is None
    assert len(service.publisher.attempts) == 2
    service.notify(slot, "done")
    await _worker_done(service)
    assert (await service.read(slot))["card"]["data"]["lede"] == "Better"
    assert len(service.publisher.attempts) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["rewrite", "delete", "privacy"])
async def test_source_invalidation_and_failed_update_never_republish_old_content(
    lifecycle, monkeypatch, invalidate
):
    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle
    service.publisher.budget = CardBudget(debounce=0, per_session=0)
    calls = []

    async def generate(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return '{"html":"<p>Old evidence</p>","data":{}}'
        if invalidate == "delete":
            state.conversation_log.present = False
        elif invalidate == "privacy":
            state.conversation_log.allowed = False
        raise RuntimeError("model failed")

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await _worker_done(service)
    assert (await service.read(slot))["card"] is not None
    if invalidate == "rewrite":
        state.conversation_log.generation += 1
    service.notify(slot, "done")
    await _worker_done(service)
    result = await service.read(slot)
    assert result["card"] is None
    assert result["published_at"] is None
    assert len(calls) == 2
    assert len(service.publisher.attempts) == 2
    assert service.publisher.next_delay() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["memory", "disk", "delete", "rebind"])
async def test_derivation_guards_before_and_after_model_call(lifecycle, monkeypatch, revoke):
    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle

    async def generate(*args, **kwargs):
        if revoke == "memory":
            slot.memory_mode = "incognito"
        elif revoke == "disk":
            state.conversation_log.allowed = False
        elif revoke == "delete":
            state._slots.clear()
        else:
            slot.linked_session_key = "new-session"
        return '{"html":"<p>private</p>","data":{}}'

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "assistant")
    await _worker_done(service)
    assert (await service.read(slot))["card"] is None


@pytest.mark.asyncio
async def test_private_session_does_not_start_a_worker(lifecycle):
    service, slot, state = lifecycle
    slot.memory_mode = "temporary"
    service.notify(slot, "done")
    assert service.worker is None
    assert not state._background_tasks


@pytest.mark.asyncio
async def test_a_worker_session_spends_no_attempt_and_reads_unavailable(lifecycle):
    service, root, state = lifecycle
    worker = SimpleNamespace(**{**vars(root), "key": "worker", "_created_by": root.key})
    worker._dashboard_card_identity = "owner-worker"
    state._slots[worker.key] = worker
    service.notify(worker, "done")
    assert worker.key not in service.publisher.entries
    assert service.worker is None
    assert (await service.read(worker))["status"] == "unavailable"
    # The root of the same team stays eligible; only the creator link excludes.
    assert service._eligible(root)
    assert (await service.read(root))["status"] == "waiting"
    worker._created_by = ""
    assert service._eligible(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_binding", [False, True])
async def test_remote_executor_never_admits_local_generation(lifecycle, complete_binding):
    service, slot, state = lifecycle
    slot.executor = "remote"
    if complete_binding:
        slot.instance_id, slot.remote_slot = "peer-1", "peer-chat-1"
    service.notify(slot, "restored")
    assert service.worker is None
    assert not service.publisher.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_binding", [False, True])
@pytest.mark.parametrize("transition", ["during", "after"])
async def test_remote_executor_revokes_completed_and_read_cards(
    lifecycle, monkeypatch, complete_binding, transition
):
    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle

    async def generate(*args, **kwargs):
        if transition == "during":
            slot.executor = "remote"
            if complete_binding:
                slot.instance_id, slot.remote_slot = "peer-1", "peer-chat-1"
        return '{"html":"<p>must not publish</p>","data":{}}'

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await _worker_done(service)
    if transition == "after":
        assert (await service.read(slot))["card"] is not None
        slot.executor = "remote"
        if complete_binding:
            slot.instance_id, slot.remote_slot = "peer-1", "peer-chat-1"
    assert (await service.read(slot))["card"] is None
    assert len(service.publisher.attempts) == 1
    entry = service.publisher.entries.get(slot.key)
    if entry is not None and transition == "during":
        assert entry.payload is None


# --- review follow-ups ------------------------------------------------------


def test_a_stray_text_lt_before_a_tag_cannot_hide_a_split_credential():
    from kiro_crew.dashboard import card_lifecycle

    # A browser shows "x < AKIAIOSFODNN7EXAMPLE": the "< " is text, and the
    # stray </b> inside the paragraph is ignored.
    split = "Deploy key: x < AKIA</b>IOSFODNN7EXAMPLE"
    assert card_lifecycle._hides_secret(split)
    html = f'<div><p>{split}</p><p data-dashboard-field="s"></p></div>'
    assert (
        card_lifecycle._redact_card_output(json.dumps({"html": html, "data": {"s": "ok"}}), None)
        is None
    )
    # Real markup is still read as markup.
    assert card_lifecycle._html_texts("<b>a</b> < c")[1] == "a < c"
    # A comment is one unit through "-->", whatever it holds; the browser shows
    # none of it, so a key split by one reads joined.
    # Every ending the HTML tokenizer honours.
    for comment in ("<!-- > -->", "<!---->", "<!-- <b> -->", "<!--x--!>", "<!-->", "<!--->"):
        assert card_lifecycle._hides_secret(f"Deploy key: AKIA{comment}IOSFODNN7EXAMPLE"), comment
    assert card_lifecycle._html_texts("a<!-- b > c")[1] == "a"
    assert card_lifecycle._html_texts("a<!-->b")[1] == "ab"
    assert card_lifecycle._html_texts("a<!--x--!>b<!---->c")[1] == "abc"
    # A "<!--" inside an attribute value is attribute text, not a comment, so the
    # text after the tag is still shown and still scanned.
    split = '<p title="<!--">AKIA<b>IOSFODNN7EXAMPLE</b></p>'
    assert card_lifecycle._html_texts(split)[1] == "AKIAIOSFODNN7EXAMPLE"
    assert card_lifecycle._hides_secret(split)
    # The browser's comment endings live in the override; a stdlib refactor that
    # stopped calling it would silently fall back to the stdlib's own rule.
    calls = []
    original = card_lifecycle._TextProjection.parse_comment

    def spy(self, i, report=1):
        calls.append(i)
        return original(self, i, report)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(card_lifecycle._TextProjection, "parse_comment", spy)
        assert card_lifecycle._html_texts("a<!--x-->b")[1] == "ab"
    assert calls == [1]


def test_scalar_card_data_is_bound_as_text_not_refused():
    from kiro_crew.dashboard import card_lifecycle

    payload = card_lifecycle._redact_card_output(
        json.dumps(
            {
                "html": '<p data-dashboard-field="n"></p><p data-dashboard-field="ok"></p>',
                "data": {"n": 3, "ok": True},
            }
        ),
        None,
    )
    assert payload is not None and payload["data"] == {"n": "3", "ok": "true"}
    assert (
        card_lifecycle._redact_card_output(
            json.dumps({"html": "<p></p>", "data": {"n": [1]}}), None
        )
        is None
    )


def test_injected_automation_rows_reach_the_model_as_automation():
    from kiro_crew.dashboard import card_lifecycle

    rows = card_lifecycle._evidence_rows(
        [
            {"role": "user", "content": "Please fix the build."},
            {"role": "user", "content": "[Subagent completion event]\nAgent a1 finished"},
            {"role": "inject", "content": "Nightly build finished: 3 failures"},
        ]
    )
    assert [row["role"] for row in reversed(rows)] == ["user", "automation", "automation"]


@pytest.mark.asyncio
async def test_a_long_run_of_tool_rows_does_not_empty_the_evidence_window(lifecycle, monkeypatch):
    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle
    state.conversation_log.rows = [
        {"role": "user", "content": "Build a release"},
        {"role": "assistant", "content": "Starting"},
        *({"role": "tool", "content": f"tool {index}"} for index in range(40)),
    ]
    prompts = []

    async def generate(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"html":"<p data-dashboard-field=lede></p>","data":{"lede":"ok"}}'

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await _worker_done(service)
    assert len(prompts) == 1 and "Build a release" in prompts[0]
    assert (await service.read(slot))["status"] == "published"


@pytest.mark.asyncio
async def test_card_scanning_runs_off_the_gateway_loop(lifecycle, monkeypatch):
    import threading

    from kiro_crew.dashboard import card_lifecycle

    service, slot, _ = lifecycle
    loop_thread = threading.current_thread()
    seen = {}
    for name in ("_evidence_rows", "_redact_card_output"):
        real = getattr(card_lifecycle, name)

        def spy(*args, _real=real, _name=name):
            seen[_name] = threading.current_thread()
            return _real(*args)

        monkeypatch.setattr(card_lifecycle, name, spy)

    async def generate(sessions, prompt, **kwargs):
        return '{"html":"<p data-dashboard-field=lede></p>","data":{"lede":"ok"}}'

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await _worker_done(service)
    assert set(seen) == {"_evidence_rows", "_redact_card_output"}
    assert all(thread is not loop_thread for thread in seen.values())


def test_a_live_event_is_served_before_seeded_restore_entries():
    publisher = CardPublisher(
        None, lambda entry: True, lambda key: None, budget=CardBudget(debounce=0), clock=lambda: 10
    )
    for index in range(5):
        publisher.notify(f"idle{index}", "owner", f"b{index}", "restored")
    publisher.notify("live", "owner", "live-b", "restored")
    # A person's message on the seeded session makes it a live event.
    publisher.notify("live", "owner", "live-b", "user")
    served = []

    async def generate(entry):
        served.append(entry.key)
        return {"html": "<p></p>", "data": {}}

    publisher.generate = generate
    asyncio.run(publisher.run_ready())
    assert served == ["live"]


@pytest.mark.asyncio
async def test_a_throwaway_api_slot_and_a_replaying_slot_queue_no_card(lifecycle):
    service, slot, state = lifecycle
    slot._dashboard_card_exempt = True
    service.notify(slot, "user")
    assert slot.key not in service.publisher.entries
    slot._dashboard_card_exempt = False
    state._slots_under_construction = {slot.key}
    service.notify(slot, "assistant")
    assert slot.key not in service.publisher.entries
    assert service.worker is None
    state._slots_under_construction = set()
    service.publisher.budget = CardBudget(debounce=60)
    service.notify(slot, "assistant")
    assert slot.key in service.publisher.entries
    service.worker.cancel()
    await asyncio.gather(service.worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_queued_card_reads_queued_before_its_transcript_exists_or_while_busy(lifecycle):
    from kiro_crew.history import TranscriptBusy

    service, slot, state = lifecycle
    service.publisher.budget = CardBudget(debounce=60)
    service.notify(slot, "user")
    state.conversation_log.present = False
    assert (await service.read(slot))["status"] == "queued"
    state.conversation_log.present = True

    @contextmanager
    def busy(key):
        raise TranscriptBusy("contended")
        yield

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(state.conversation_log, "publication_hold", busy)
        assert (await service.read(slot))["status"] == "queued"
    # A privacy refusal is still final.
    state.conversation_log.allowed = False
    assert (await service.read(slot))["status"] == "unavailable"
    service.worker.cancel()
    await asyncio.gather(service.worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_published_card_read_under_a_busy_lock_is_unavailable_not_blank(lifecycle):
    from kiro_crew.history import TranscriptBusy

    service, slot, state = lifecycle
    service.publisher.notify(slot.key, slot._dashboard_card_identity, "dashboard:one", "done")
    entry = service.publisher.entries[slot.key]
    entry.pending = False
    entry.payload = {"html": "<p>Published</p>", "data": {}}
    entry.published_revision = entry.revision

    @contextmanager
    def busy(key):
        raise TranscriptBusy("contended")
        yield

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(state.conversation_log, "publication_hold", busy)
        assert (await service.read(slot))["status"] == "unavailable"


#: What Chromium's ``textContent`` shows for each input. The projection must show
#: the same text, so markup can never split a credential the browser displays
#: joined. To re-record a key in Playwright's Chromium:
#: ``page.evaluate("m => { const b = document.createElement('body');
#: b.innerHTML = m; return b.textContent }", key)``.
_BROWSER_TEXT = {
    "x < AKIA</b>b": "x < AKIAb",
    '<p title="<!--">AKIA<b>IOSF</b></p>': "AKIAIOSF",
    "a<!-- > -->b": "ab",
    "a<!---->b": "ab",
    "a<!--x--!>b": "ab",
    "a<!-->b": "ab",
    "a<!--->b": "ab",
    "a<!-- b > c": "a",
    "a<!-- <b> -->b": "ab",
    "a<!--x-- >b-->c": "ac",
    "x </ AKIA</b>IOSF": "x IOSF",
    "x </1AKIA>IOSF": "x IOSF",
    "x </>AKIA": "x AKIA",
    "x <?php AKIA>IOSF": "x IOSF",
    "x <!AKIA>IOSF": "x IOSF",
    "x <a1>b": "x b",
    "x </b>AKIA": "x AKIA",
    "&#65;KIA": "AKIA",
    "<svg><text>AKIA<![CDATA[IOSF]]>X</text></svg>": "AKIAIOSFX",
}


@pytest.mark.parametrize("markup", sorted(_BROWSER_TEXT))
def test_the_projection_shows_what_the_browser_shows(markup):
    from kiro_crew.dashboard import card_lifecycle

    assert card_lifecycle._html_texts(markup)[1] == _BROWSER_TEXT[markup]


def test_cdata_is_kept_as_text_where_the_browser_would_hide_it():
    """Showing more than the browser hides nothing; showing less could split a key."""
    from kiro_crew.dashboard import card_lifecycle

    # Chromium renders "AKIAX" here (a bogus comment inside a MathML text point).
    assert (
        card_lifecycle._html_texts("<math><mi>AKIA<![CDATA[IOSF]]>X</mi></math>")[1] == "AKIAIOSFX"
    )


def test_card_prompt_leaves_pending_questions_to_the_host():
    """The card is a model-written summary; a "Needs you" copy of an ask goes
    stale on answer and disagrees with the Questions tab, which is the source."""
    from kiro_crew.dashboard import card_lifecycle

    prompt = " ".join(card_lifecycle._ROOT_PROMPT.split())
    assert "Do not restate questions, choices or decisions waiting for the user" in prompt
    assert "Questions tab is the one place they appear" in prompt
    assert "return replacement html without it" in prompt


async def _card_context(service, slot, monkeypatch):
    """The JSON evidence the card model is given for *slot*'s next card."""
    from kiro_crew.dashboard import card_lifecycle

    prompts = []

    async def generate(_sessions, prompt, **kwargs):
        prompts.append(prompt)
        return json.dumps({"html": '<p data-dashboard-field="lede"></p>', "data": {"lede": "x"}})

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await _worker_done(service)
    assert len(prompts) == 1
    return json.loads(prompts[0][len(card_lifecycle._ROOT_PROMPT) :])


@pytest.mark.asyncio
async def test_card_language_follows_what_a_chinese_user_typed(lifecycle, monkeypatch):
    service, slot, _state = lifecycle
    slot.messages = [
        {
            "role": "user",
            "content": "\u5e2e\u6211\u770b\u4e0b `make build` \u4e3a\u4ec0\u4e48\u5931\u8d25 https://ci.example/run/7",
        },
        {"role": "assistant", "content": "The build failed on a missing dependency."},
    ]
    context = await _card_context(service, slot, monkeypatch)
    assert context["language"] == {
        "user_wrote": "\u5e2e\u6211\u770b\u4e0b \u4e3a\u4ec0\u4e48\u5931\u8d25"
    }


@pytest.mark.asyncio
async def test_card_language_ignores_a_previous_card_in_another_language(lifecycle, monkeypatch):
    service, slot, _state = lifecycle
    slot.messages = [
        {"role": "user", "content": "Rebase the branch onto main"},
        {"role": "assistant", "content": "\u5df2\u7ecf\u53d8\u57fa\u5b8c\u6210"},
        {"role": "inject", "content": "[Cron notification] \u5b9a\u65f6\u4efb\u52a1\u5b8c\u6210"},
    ]
    context = await _card_context(service, slot, monkeypatch)
    assert context["language"] == {"user_wrote": "Rebase the branch onto main"}


@pytest.mark.asyncio
async def test_configured_ui_language_wins_over_the_user_text(lifecycle, monkeypatch):
    from kiro_crew.dashboard import card_lifecycle

    service, slot, _state = lifecycle
    card_lifecycle.KiroCrewConfig.load().dashboard.language = "zh-CN"
    context = await _card_context(service, slot, monkeypatch)
    assert context["language"] == {"tag": "zh-CN"}


@pytest.mark.asyncio
async def test_no_language_signal_sends_no_language(lifecycle, monkeypatch):
    service, slot, _state = lifecycle
    slot.messages = [{"role": "assistant", "content": "Working on it"}]
    context = await _card_context(service, slot, monkeypatch)
    assert "language" not in context


def test_root_prompt_names_the_language_rule():
    from kiro_crew.dashboard import card_lifecycle

    prompt = card_lifecycle._ROOT_PROMPT
    assert '"language"' in prompt and '"tag"' in prompt and '"user_wrote"' in prompt
    assert "Never take the\nlanguage from facts, previous" in prompt
