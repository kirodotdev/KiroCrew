"""Automatic dashboard cards turned on and off: the opt-in, the toggle and gateway activation.

Cards default off and the setting round-trips; a disabled service never queues or spends;
enabling seeds the open sessions without a GET and disabling cancels in-flight work
without refunding its budget; a rapid toggle waits for its cancel. At the gateway the live
config owns the cost opt-in, registering routes never imports the card producer,
activation waits until the server listens and seeds restored sessions, an activation
failure leaves config watching on, and cleanup cancels the worker for good. Five cases on
work in flight sit among them: the latest event survives a generation in flight, eviction
cannot reset a session's cooldown, a timeout releases the only permit and keeps the last
good card, a rewrite reads the current disk rows, and the card GET runs under the real
history locks and owner check. Publication, invalidation, redaction and the review
follow-ups are in ``test_dynamic_dashboard_cards.py``; the shared fixtures are in
``dynamic_dashboard_cards_helpers.py``.
"""

import asyncio
from types import SimpleNamespace

import pytest
from dynamic_dashboard_cards_helpers import _seeded_session_tree  # noqa: F401
from dynamic_dashboard_cards_helpers import lifecycle_fixture  # noqa: F401
from dynamic_dashboard_cards_helpers import _parked, _worker_done

from kiro_crew.dashboard.dynamic_cards import CardBudget, CardPublisher
from kiro_crew.testing.wait import await_until


def test_automatic_cards_default_off_and_config_roundtrips():
    from kiro_crew.config.loader import KiroCrewConfig, _build_dashboard_config

    assert KiroCrewConfig().dashboard.dynamic_dashboard_cards is False
    cfg = KiroCrewConfig(
        dashboard=_build_dashboard_config(set(), {"dynamic_dashboard_cards": True})
    )
    assert cfg.dashboard.dynamic_dashboard_cards is True
    assert cfg.to_dict()["dashboard"]["dynamic_dashboard_cards"] is True


@pytest.mark.asyncio
async def test_disabled_cards_never_queue_or_spend(lifecycle):
    service, slot, state = lifecycle
    service.set_enabled(False)
    service.notify(slot, "done")
    assert service.worker is None
    assert not service.publisher.entries
    assert (await service.read(slot))["status"] == "disabled"


@pytest.mark.asyncio
async def test_enabling_seeds_open_sessions_without_a_get(lifecycle, monkeypatch):
    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle
    service.set_enabled(False)
    calls = []

    async def generate(*args, **kwargs):
        calls.append(kwargs["crew_log_session_key"])
        return '{"html":"<p>open session</p>","data":{}}'

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.set_enabled(True)
    await _worker_done(service)
    assert calls == ["dashboard:one"]
    assert (await service.read(slot))["status"] == "published"


@pytest.mark.asyncio
async def test_disable_cancels_inflight_without_refunding_budget(lifecycle, monkeypatch):
    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        try:
            # flake-ok: pre-existing unbounded park of the fake generation until the disable cancels it, moved verbatim; tracked for a de-flake pass
            await _parked()
        finally:
            cancelled.set()

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await await_until(entered, what="the generation starting")
    service.set_enabled(False)
    await await_until(cancelled, what="the cancellation reaching the generator")
    with pytest.raises(asyncio.CancelledError):
        # flake-ok: pre-existing await with no timeout of the worker task the disable above cancels, moved verbatim; tracked for a de-flake pass
        await service.worker
    assert len(service.publisher.attempts) == 1
    assert service.publisher.active is None
    assert (await service.read(slot))["card"] is None


@pytest.mark.asyncio
async def test_latest_event_is_not_lost_while_layout_is_generating():
    entered, release = asyncio.Event(), asyncio.Event()
    now = SimpleNamespace(value=0.0)

    async def generate(entry):
        entered.set()
        await await_until(release, what="the test's release gate")
        return {"html": "<p data-dashboard-field=next></p>", "data": {"next": "review"}}

    publisher = CardPublisher(
        generate,
        lambda entry: True,
        lambda key: None,
        budget=CardBudget(debounce=0),
        clock=lambda: now.value,
    )
    owner = object()
    publisher.notify("one", owner, "session-one", "started")
    task = asyncio.create_task(publisher.run_ready())
    try:
        await await_until(entered, what="the generation starting")
        publisher.notify("one", owner, "session-one", "completed")
    finally:
        release.set()
        await await_until(task, what="the parked task finishing")
    assert publisher.read("one")["stale"] is True
    assert publisher.entries["one"].pending is True
    now.value = 120
    assert await publisher.run_ready() == 1
    assert publisher.read("one")["stale"] is False


@pytest.mark.asyncio
async def test_eviction_or_toggle_cannot_reset_session_cooldown():
    now = SimpleNamespace(value=0.0)

    async def generate(entry):
        return {"html": "<p>ready</p>", "data": {}}

    publisher = CardPublisher(
        generate,
        lambda entry: True,
        lambda key: None,
        budget=CardBudget(debounce=0),
        clock=lambda: now.value,
    )
    owner = object()
    publisher.notify("one", owner, "session-one", "done")
    await publisher.run_ready()
    publisher.forget("one")
    publisher.notify("one", owner, "session-one", "restored")
    assert await publisher.run_ready() == 0
    assert publisher.next_delay() == 120
    assert len(publisher.last_attempt) == 1


@pytest.mark.asyncio
async def test_timeout_releases_the_only_permit_and_keeps_last_good_card():
    async def blocked(entry):
        # flake-ok: pre-existing unbounded park of the stub generation that the publisher's timeout cuts off, moved verbatim; tracked for a de-flake pass
        await _parked()

    # flake-ok: pre-existing 10 ms publisher timeout, which is the behaviour under test, moved verbatim; tracked for a de-flake pass
    publisher = CardPublisher(
        blocked, lambda entry: True, lambda key: None, budget=CardBudget(debounce=0, timeout=0.01)
    )
    publisher.notify("one", object(), "session-one", "done")
    publisher.entries["one"].payload = {"html": "<p>last good</p>", "data": {}}
    assert await publisher.run_ready() == 1
    assert publisher.active is None
    assert publisher.read("one")["status"] == "failed"
    assert publisher.read("one")["card"]["html"] == "<p>last good</p>"
    assert publisher.next_delay() is None


@pytest.mark.asyncio
async def test_rewrite_discards_previous_layout_and_reads_current_disk_rows(lifecycle, monkeypatch):
    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle
    service.publisher.budget = CardBudget(debounce=0, per_session=0)
    prompts = []

    async def generate(_sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"html":"<p>obsolete secret result</p>","data":{}}'

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await _worker_done(service)
    state.conversation_log.generation += 1
    state.conversation_log.rows = [{"role": "assistant", "content": "Replacement evidence"}]
    service.notify(slot, "done")
    await _worker_done(service)
    assert "obsolete secret result" not in prompts[1]
    assert "Tests failed" not in prompts[1]
    assert "Replacement evidence" in prompts[1]


@pytest.mark.asyncio
async def test_real_history_locks_and_card_get_authority(lifecycle, monkeypatch, tmp_path):
    import json

    from kiro_crew.dashboard import card_lifecycle
    from kiro_crew.dashboard.routes.chat import api_dashboard_card
    from kiro_crew.history import ConversationLog

    service, slot, state = lifecycle
    state.conversation_log = ConversationLog(tmp_path / "sessions")
    state.conversation_log.append("dashboard:one", "user", "Publish a card")
    state._dynamic_cards = service

    async def generate(*args, **kwargs):
        return '{"html":"<p>ready</p>","data":{}}'

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await _worker_done(service)

    class Request(dict):
        app = {"state": state}
        match_info = {"slot": "one"}

    response = await api_dashboard_card(Request(user="local-app", app=""))
    assert json.loads(response.text)["card"]["html"] == "<p>ready</p>"
    assert len(service.publisher.attempts) == 1
    denied = await api_dashboard_card(Request(app="other-app"))
    assert denied.status == 403
    assert json.loads(denied.text)["code"] == "owner_only"
    other_user = await api_dashboard_card(Request(user="not-owner", app=""))
    assert other_user.status == 403
    assert len(service.publisher.attempts) == 1


@pytest.mark.asyncio
async def test_rapid_toggle_waits_for_cancel_and_shutdown_does_not_respawn(lifecycle, monkeypatch):
    from kiro_crew.dashboard import card_lifecycle

    service, slot, state = lifecycle
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def generate(*args, **kwargs):
        calls.append(1)
        entered.set()
        try:
            # flake-ok: pre-existing unbounded park of the fake generation until the disable cancels it, moved verbatim; tracked for a de-flake pass
            await _parked()
        finally:
            cancelling.set()
            await await_until(release, what="the test's release gate")

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    service.notify(slot, "done")
    await await_until(entered, what="the generation starting")
    old_worker = service.worker
    try:
        service.set_enabled(False)
        await await_until(cancelling, what="the cancellation reaching the generator")
        service.set_enabled(True)
        assert service.worker is old_worker
        assert service.restart_after_cancel
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await await_until(old_worker, what="the cancelled worker settling")
    # The restarted worker must wait out the SAME session's cooldown.
    restarted = service.worker
    assert restarted is not old_worker
    assert len(calls) == 1
    assert len(service.publisher.attempts) == 1
    restarted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(restarted, what="the restarted worker settling")
    assert service.worker is restarted
    assert service.publisher.active is None
    assert not state._background_tasks


@pytest.mark.asyncio
async def test_gateway_live_config_owns_cost_opt_in(monkeypatch):
    from aiohttp import web

    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.server import _kick_config_watch, _register_config_watch
    from kiro_crew.dashboard.state import DashboardState

    watcher = live.ConfigWatch()
    monkeypatch.setattr(live, "watch", lambda: watcher)
    state = SimpleNamespace(
        _slots={},
        workflow_service=None,
        channel_manager=None,
        _dynamic_cards=None,
        _background_tasks=set(),
    )
    state.set_dynamic_cards_enabled = lambda enabled: DashboardState.set_dynamic_cards_enabled(
        state, enabled
    )
    initial = KiroCrewConfig()
    app = web.Application()
    _register_config_watch(app, state, initial)
    assert state._dynamic_cards is None
    _kick_config_watch(app, state)
    await await_until(
        asyncio.gather(*state._background_tasks), what="the background tasks finishing"
    )
    assert state._dynamic_cards is None
    updated = KiroCrewConfig()
    updated.dashboard.dynamic_dashboard_cards = True
    await watcher._dispatch(
        live.ConfigChange(
            old=initial, new=updated, changed=frozenset({"dashboard.dynamic_dashboard_cards"})
        )
    )
    assert state._dynamic_cards.enabled is True
    service = state._dynamic_cards
    await watcher._dispatch(
        live.ConfigChange(
            old=updated, new=initial, changed=frozenset({"dashboard.dynamic_dashboard_cards"})
        )
    )
    assert state._dynamic_cards.enabled is False
    assert state._dynamic_cards.worker is None
    await watcher.stop()
    assert state._dynamic_cards is service


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("registration", ["routes", "config"])
def test_registering_routes_and_config_never_imports_card_producer(
    monkeypatch, enabled, registration
):
    import builtins
    import importlib

    from aiohttp import web

    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.routes import chat
    from kiro_crew.dashboard.server import _register_config_watch

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name != "kiro_crew.dashboard.card_lifecycle", "producer imported before bind"
        return original_import(name, *args, **kwargs)

    # flake-ok: pre-existing builtins.__import__ guard, scoped to this test by monkeypatch, moved verbatim; tracked for a de-flake pass
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(live, "watch", lambda: watcher)
    watcher = live.ConfigWatch()
    initial = KiroCrewConfig()
    initial.dashboard.dynamic_dashboard_cards = enabled
    state = SimpleNamespace(set_dynamic_cards_enabled=lambda value: None)
    app = web.Application()
    if registration == "routes":
        # flake-ok: pre-existing reload of the chat routes module under the import guard, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-in-process-reload -- pre-existing reload of the chat routes module under the import guard, moved verbatim; tracked for a de-flake pass
        importlib.reload(chat)
        chat.register(app)
    else:
        _register_config_watch(app, state, initial)


@pytest.mark.asyncio
async def test_initial_enabled_activation_is_deferred_and_seeds_restored_sessions(
    lifecycle, monkeypatch
):
    from unittest.mock import AsyncMock

    from aiohttp import web

    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import card_lifecycle
    from kiro_crew.dashboard.server import _kick_config_watch, _register_config_watch
    from kiro_crew.dashboard.state import DashboardState

    service, slot, state = lifecycle
    state._dynamic_cards = None
    state.set_dynamic_cards_enabled = lambda enabled: DashboardState.set_dynamic_cards_enabled(
        state, enabled
    )
    service.enabled = False
    constructions = []

    def construct(owner):
        constructions.append(owner)
        return service

    monkeypatch.setattr(card_lifecycle, "CardLifecycle", construct)
    monkeypatch.setattr(
        card_lifecycle,
        "run_bg_oneliner",
        AsyncMock(return_value='{"html":"<p>restored</p>","data":{}}'),
    )
    watcher = live.ConfigWatch()
    monkeypatch.setattr(live, "watch", lambda: watcher)
    monkeypatch.setattr(watcher, "start", AsyncMock())
    initial = KiroCrewConfig()
    initial.dashboard.dynamic_dashboard_cards = True
    app = web.Application()
    _register_config_watch(app, state, initial)
    assert not constructions
    _kick_config_watch(app, state)
    assert not constructions, "the optional import must be deferred out of the boot stack"
    await await_until(
        asyncio.gather(*state._background_tasks), what="the background tasks finishing"
    )
    await _worker_done(service)
    assert constructions == [state]
    assert (await service.read(slot))["card"]["html"] == "<p>restored</p>"
    state.set_dynamic_cards_enabled(False)
    state.set_dynamic_cards_enabled(True)
    assert state._dynamic_cards is service
    assert len(service.publisher.attempts) == 1
    assert len(constructions) == 1
    state.set_dynamic_cards_enabled(False)
    # flake-ok: pre-existing gather with no timeout of the background tasks the final disable leaves, moved verbatim; tracked for a de-flake pass
    await asyncio.gather(*state._background_tasks, return_exceptions=True)


def test_both_server_entrypoints_defer_card_activation_until_after_listening():
    import inspect
    from pathlib import Path

    from kiro_crew.dashboard import server

    # The boot phases an entrypoint delegates to live in its server_runtime owners.
    owners = sorted((Path(server.__file__).parent / "server_runtime").glob("[!_]*.py"))
    assert owners, "expected the server_runtime owners beside server.py"
    phases = "".join(path.read_text(encoding="utf-8") for path in owners)
    for entrypoint in (server.start_dashboard, server.start_api_server):
        source = inspect.getsource(entrypoint) + phases
        listener = (
            "await _start_site(site, port)"
            if entrypoint is server.start_api_server
            else "await site.start()"
        )
        assert source.index(listener) < source.index("_kick_config_watch(app, state)")
        assert "card_lifecycle" not in source


@pytest.mark.asyncio
async def test_optional_card_activation_failure_does_not_disable_config_watching(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from aiohttp import web

    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.server import _kick_config_watch

    watcher = live.ConfigWatch()
    monkeypatch.setattr(live, "watch", lambda: watcher)
    monkeypatch.setattr(watcher, "start", AsyncMock())
    initial = KiroCrewConfig()
    initial.dashboard.dynamic_dashboard_cards = True
    app = web.Application()
    app["config_watch_initial"] = initial
    state = SimpleNamespace(
        set_dynamic_cards_enabled=Mock(side_effect=RuntimeError("optional producer unavailable")),
        _background_tasks=set(),
    )
    _kick_config_watch(app, state)
    await await_until(
        asyncio.gather(*state._background_tasks), what="the background tasks finishing"
    )
    watcher.start.assert_awaited_once_with(initial=initial)


@pytest.mark.asyncio
async def test_config_cleanup_cancels_card_worker_without_respawning(lifecycle, monkeypatch):
    from aiohttp import web

    from kiro_crew.config import live
    from kiro_crew.dashboard import card_lifecycle
    from kiro_crew.dashboard.server import _register_config_watch

    service, slot, state = lifecycle
    state._dynamic_cards = service
    state.set_dynamic_cards_enabled = service.set_enabled
    watcher = live.ConfigWatch()
    monkeypatch.setattr(live, "watch", lambda: watcher)
    entered = asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        # flake-ok: pre-existing unbounded park of the fake generation until the config-watch shutdown cancels it, moved verbatim; tracked for a de-flake pass
        await _parked()

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    app = web.Application()
    _register_config_watch(app, state, None)
    service.notify(slot, "done")
    await await_until(entered, what="the generation starting")
    shutdown = next(cb for cb in app.on_cleanup if cb.__name__ == "_config_watch_shutdown")
    await await_until(asyncio.ensure_future(shutdown(app)), what="the config-watch shutdown")
    assert not service.enabled
    assert service.worker.done()
    assert not state._background_tasks
    assert len(service.publisher.attempts) == 1
