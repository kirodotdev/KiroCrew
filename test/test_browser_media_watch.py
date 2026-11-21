"""The gateway tells an app's backend once when its own agent's tab stops playing.

``dashboard/handlers/browser_media_watch.py`` reads the tab of a slot an app owns and
hands each new ending to that app's ``backend.hooks.on_browser_media_ended`` through
``hooks_integration.dispatch_app_event``. These tests pin who gets it (the owning app
only, never for a person's slot), how often (once per ending) and when the watch
stops (the app loses its approved sites, the tab closes).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.apps import hooks_integration
from kiro_crew.apps import lifecycle as lifecycle_mod
from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.dashboard.handlers import browser_media
from kiro_crew.dashboard.handlers import browser_media_watch as watch
from kiro_crew.dashboard.handlers import browser_ops
from kiro_crew.testing.wait import async_wait_until

SITES = ("https://www.youtube.com/watch",)


class _Tab:
    """The ``_read`` stand-in: a scripted list of answers, the last one repeating."""

    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.answers = answers
        self.reads = 0

    async def read(self, _hub: Any, _key: str, _sup: Any, _sites: Any) -> dict[str, Any]:
        answer = self.answers[min(self.reads, len(self.answers) - 1)]
        self.reads += 1
        return dict(answer)


PLAYING = {"ok": True, "ended": False, "duration": 200.0, "origin": "https://www.youtube.com"}
ENDED = {"ok": True, "ended": True, "duration": 200.0, "origin": "https://www.youtube.com"}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    hub_mod.reset_live_hub()
    watch._WATCHES.clear()
    watch._ENDED.clear()
    e = SimpleNamespace(
        sent=[],
        sites={"tunes": SITES, "other": SITES},
        hooked={"tunes", "other"},
    )

    async def dispatch(app: str, hook: str, event: dict[str, Any]) -> bool:
        e.sent.append((app, hook, event))
        return True

    monkeypatch.setattr(browser_ops, "app_browser_sites", lambda app: e.sites.get(app, ()))
    monkeypatch.setattr(watch, "_hook_declared", lambda app: app in e.hooked)
    monkeypatch.setattr(browser_media, "_running_supervisor", lambda: SimpleNamespace())
    monkeypatch.setattr(hooks_integration, "dispatch_app_event", dispatch)
    monkeypatch.setattr(watch, "WATCH_S", 0)
    yield e
    for task in watch._WATCHES.values():
        task.cancel()
    watch._WATCHES.clear()
    watch._ENDED.clear()
    hub_mod.reset_live_hub()


def _state(**slots: str) -> SimpleNamespace:
    hub = hub_mod.get_live_hub()
    for i, key in enumerate(slots):
        hub.bind_target(key, f"T{i}")
    return SimpleNamespace(_slots={k: SimpleNamespace(_app=a) for k, a in slots.items()})


async def _until_stopped(key: str) -> None:
    task = watch._WATCHES.get(key)
    assert task is not None
    await async_wait_until(task.done, describe=lambda: f"the watch of {key} still runs")


async def _run_until_reads(monkeypatch: pytest.MonkeyPatch, tab: _Tab, reads: int) -> None:
    monkeypatch.setattr(watch, "_read", tab.read)
    await async_wait_until(lambda: tab.reads >= reads, describe=lambda: f"{tab.reads} reads")


@pytest.mark.asyncio
async def test_an_ending_reaches_the_owning_app_once(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(**{"tunes-1": "tunes"})
    tab = _Tab([PLAYING, ENDED, ENDED, ENDED, ENDED])
    monkeypatch.setattr(watch, "_read", tab.read)
    watch.ensure_watch(state, "tunes-1")
    watch.ensure_watch(state, "tunes-1")  # a second op does not start a second watch
    await _run_until_reads(monkeypatch, tab, 6)
    assert env.sent == [
        (
            "tunes",
            "on_browser_media_ended",
            {
                "event": "browser_media_ended",
                "session_key": "tunes-1",
                "origin": "https://www.youtube.com",
                "duration": 200.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_a_new_song_ending_is_a_new_event(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(**{"tunes-1": "tunes"})
    tab = _Tab([ENDED, ENDED, PLAYING, ENDED])
    monkeypatch.setattr(watch, "_read", tab.read)
    watch.ensure_watch(state, "tunes-1")
    await _run_until_reads(monkeypatch, tab, 6)
    assert len(env.sent) == 2


@pytest.mark.asyncio
async def test_a_restarted_watch_does_not_repeat_the_same_ending(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(**{"tunes-1": "tunes"})
    tab = _Tab([ENDED])
    monkeypatch.setattr(watch, "_read", tab.read)
    watch.ensure_watch(state, "tunes-1")
    await _run_until_reads(monkeypatch, tab, 3)
    watch._WATCHES.pop("tunes-1").cancel()
    watch.ensure_watch(state, "tunes-1")
    await _run_until_reads(monkeypatch, tab, 6)
    assert len(env.sent) == 1


@pytest.mark.asyncio
async def test_only_the_app_that_owns_the_tab_hears(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(**{"tunes-1": "tunes", "other-1": "other"})
    ended_for: dict[str, list[dict[str, Any]]] = {"tunes-1": [ENDED], "other-1": [ENDED]}
    reads = {"n": 0}

    async def read(_hub: Any, key: str, _sup: Any, _sites: Any) -> dict[str, Any]:
        reads["n"] += 1
        return dict(ended_for[key][0])

    monkeypatch.setattr(watch, "_read", read)
    watch.ensure_watch(state, "tunes-1")
    watch.ensure_watch(state, "other-1")
    await async_wait_until(lambda: reads["n"] >= 8, describe=lambda: f"{reads['n']} reads")
    # Each app hears of its own tab only.
    assert sorted((app, ev["session_key"]) for app, _h, ev in env.sent) == [
        ("other", "other-1"),
        ("tunes", "tunes-1"),
    ]


@pytest.mark.asyncio
async def test_a_persons_slot_is_never_watched(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(**{"chat-1": ""})
    tab = _Tab([ENDED])
    monkeypatch.setattr(watch, "_read", tab.read)
    watch.ensure_watch(state, "chat-1")
    await _until_stopped("chat-1")
    assert tab.reads == 0 and env.sent == []


@pytest.mark.asyncio
async def test_an_app_without_the_hook_is_not_watched(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.hooked.clear()
    state = _state(**{"tunes-1": "tunes"})
    tab = _Tab([ENDED])
    monkeypatch.setattr(watch, "_read", tab.read)
    watch.ensure_watch(state, "tunes-1")
    await _until_stopped("tunes-1")
    assert tab.reads == 0 and env.sent == []


@pytest.mark.asyncio
async def test_the_watch_stops_once_the_app_is_disabled(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(**{"tunes-1": "tunes"})

    class _DisableAfterFirst(_Tab):
        async def read(self, *a: Any) -> dict[str, Any]:
            answer = await super().read(*a)
            env.sites.pop("tunes")  # disabling the app withdraws its approved sites
            return answer

    tab = _DisableAfterFirst([PLAYING, ENDED])
    monkeypatch.setattr(watch, "_read", tab.read)
    watch.ensure_watch(state, "tunes-1")
    await _until_stopped("tunes-1")
    assert tab.reads == 1 and env.sent == []


@pytest.mark.asyncio
async def test_the_watch_stops_when_the_tab_closes(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(**{"tunes-1": "tunes"})
    hub_mod.get_live_hub().forget("tunes-1")
    tab = _Tab([ENDED])
    monkeypatch.setattr(watch, "_read", tab.read)
    watch.ensure_watch(state, "tunes-1")
    await _until_stopped("tunes-1")
    assert tab.reads == 0 and env.sent == []


@pytest.mark.asyncio
async def test_a_busy_browser_skips_the_tick(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(**{"tunes-1": "tunes"})
    tab = _Tab([ENDED])
    monkeypatch.setattr(watch, "_read", tab.read)
    ticks = {"n": 0}
    real = watch._still_watched

    async def counted(*a: Any) -> Any:
        ticks["n"] += 1
        return await real(*a)

    monkeypatch.setattr(watch, "_still_watched", counted)
    await browser_ops._OP_LOCK.acquire()
    try:
        watch.ensure_watch(state, "tunes-1")
        await async_wait_until(lambda: ticks["n"] >= 3, describe=lambda: f"{ticks['n']} ticks")
        assert tab.reads == 0
    finally:
        browser_ops._OP_LOCK.release()
    await _run_until_reads(monkeypatch, tab, 1)


def test_origin_keeps_scheme_and_host_only() -> None:
    assert watch._origin("https://u:p@www.youtube.com:8443/watch?v=x#t") == (
        "https://www.youtube.com:8443"
    )
    assert watch._origin("about:blank") == ""
    assert watch._origin(None) == ""


# --- delivery: hooks_integration.dispatch_app_event -------------------------------

HOOKED = {
    "name": "tunes",
    "enabled": True,
    "manifest": {"backend": {"hooks": {"on_browser_media_ended": "backend.h:on_ended"}}},
}


CTX = SimpleNamespace(job=None)


@pytest.fixture
def delivery(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A real dispatcher with the hook callable and its context stood in."""
    d = SimpleNamespace(calls=[], enabled=True, denied="", hook=None)
    dispatcher = lifecycle_mod.LifecycleDispatcher()

    async def prepare(_info: Any, *, phase: str) -> Any:
        assert phase == "event"
        return CTX

    async def on_ended(ctx: Any, event: dict[str, Any]) -> None:
        d.calls.append((ctx, event))

    d.hook = on_ended
    monkeypatch.setattr(dispatcher, "_prepare_context", prepare)
    monkeypatch.setattr(lifecycle_mod, "loaded_callable", lambda app, path: d.hook)
    monkeypatch.setattr(hooks_integration, "_lifecycle_dispatcher", dispatcher)
    monkeypatch.setattr(hooks_integration, "_loaded_hook_signatures", {"tunes": ("v",)})
    monkeypatch.setattr(hooks_integration, "hook_enable_denied", lambda _app: d.denied)
    monkeypatch.setattr(
        hooks_integration, "get_app", lambda name: {**HOOKED, "name": name, "enabled": d.enabled}
    )
    yield d
    lifecycle_mod._DETACHED_HOOK_TASKS.pop("tunes", None)
    lifecycle_mod._DETACHED_HOOK_RESIDUALS.discard("tunes")


async def _result(task: "asyncio.Future[bool]") -> bool:
    """*task*'s answer, waited for with the suite's bound, never open-ended."""
    await async_wait_until(task.done, describe=lambda: "the delivery never returned")
    return task.result()


async def _send(app: str = "tunes") -> bool:
    return await hooks_integration.dispatch_app_event(
        app, "on_browser_media_ended", {"event": "browser_media_ended"}
    )


@pytest.mark.asyncio
async def test_delivery_calls_the_hook_with_ctx_and_event(delivery: SimpleNamespace) -> None:
    assert await _send()
    assert delivery.calls == [(CTX, {"event": "browser_media_ended"})]


@pytest.mark.asyncio
async def test_the_hook_gets_the_job_sdk_its_startup_registered_runners_on(
    delivery: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = object()
    dispatcher = hooks_integration._lifecycle_dispatcher

    async def prepare(_info: Any, *, phase: str) -> Any:
        return SimpleNamespace(job=object())

    monkeypatch.setattr(dispatcher, "_prepare_context", prepare)
    monkeypatch.setattr(lifecycle_mod, "get_sdk", lambda app: registered)
    assert await _send()
    assert delivery.calls[0][0].job is registered


@pytest.mark.asyncio
async def test_delivery_skips_an_app_whose_hooks_are_not_loaded(
    delivery: SimpleNamespace,
) -> None:
    assert not await _send("other")
    assert delivery.calls == []


@pytest.mark.asyncio
async def test_delivery_skips_a_disabled_app(delivery: SimpleNamespace) -> None:
    delivery.enabled = False
    assert not await _send()
    assert delivery.calls == []


@pytest.mark.asyncio
async def test_delivery_skips_an_untrusted_app(delivery: SimpleNamespace) -> None:
    delivery.denied = "trust withdrawn"
    assert not await _send()
    assert delivery.calls == []


@pytest.mark.asyncio
async def test_delivery_waits_for_a_teardown_holding_the_lifecycle_lock(
    delivery: SimpleNamespace,
) -> None:
    from kiro_crew.apps.manager import app_lifecycle_lock

    lock = app_lifecycle_lock("tunes")
    await lock.acquire()
    sending = asyncio.ensure_future(_send())
    try:
        await async_wait_until(lambda: lock._bound()._waiters, describe=lambda: "no waiter")
        # The teardown under the lock finishes by unloading the app's hooks.
        hooks_integration._loaded_hook_signatures.pop("tunes")
    finally:
        lock.release()
    assert not await _result(sending)
    assert delivery.calls == []


@pytest.mark.asyncio
async def test_a_running_hook_is_owned_so_teardown_waits_for_it(
    delivery: SimpleNamespace,
) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow(ctx: Any, event: Any) -> None:
        started.set()
        await release.wait()

    delivery.hook = slow
    sending = asyncio.ensure_future(_send())
    dispatcher = hooks_integration._lifecycle_dispatcher
    try:
        await async_wait_until(
            lambda: started.is_set() or sending.done(), describe=lambda: "hook never started"
        )
        assert started.is_set(), f"delivery returned {sending.result()!r} before the hook"
        assert lifecycle_mod.app_has_retained_startup("tunes")
        assert not await dispatcher.stop_detached_startup_hooks("tunes", bounded=True, timeout=0)
    finally:
        release.set()
    assert await _result(sending)
    assert await dispatcher.stop_detached_startup_hooks("tunes", bounded=True, timeout=0)


@pytest.mark.asyncio
async def test_a_failing_or_slow_hook_reports_false(
    delivery: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    def broken(ctx: Any, event: Any) -> None:
        raise RuntimeError("app bug")

    async def slow(ctx: Any, event: Any) -> None:
        await release.wait()

    delivery.hook = broken
    assert not await _send()
    delivery.hook = slow
    monkeypatch.setattr(lifecycle_mod, "_HOOK_TIMEOUT_SEC", 0)
    try:
        assert not await _send()
    finally:
        release.set()
    await async_wait_until(lambda: not lifecycle_mod.app_has_retained_startup("tunes"))


@pytest.mark.asyncio
async def test_a_hook_module_not_yet_loaded_is_loaded_off_the_loop(
    delivery: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    loop_thread = threading.get_ident()
    seen: list[int] = []

    def resolve(app: str, path: str) -> Any:
        seen.append(threading.get_ident())
        return delivery_hook

    async def delivery_hook(ctx: Any, event: Any) -> None:
        delivery.calls.append(event)

    monkeypatch.setattr(lifecycle_mod, "loaded_callable", lambda app, path: None)
    monkeypatch.setattr(
        hooks_integration._lifecycle_dispatcher, "_resolve_hook", staticmethod(resolve)
    )
    assert await _send()
    assert seen and seen[0] != loop_thread
    assert delivery.calls == [{"event": "browser_media_ended"}]


# --- the pieces the watch tests stand in for -------------------------------------


def test_hook_declared_reads_the_live_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.apps import manager

    def manifest(hook: str) -> Any:
        hooks = SimpleNamespace(on_browser_media_ended=hook)
        return SimpleNamespace(backend=SimpleNamespace(hooks=hooks))

    monkeypatch.setattr(manager, "get_app_manifest", lambda app: manifest("backend.h:on_end"))
    assert watch._hook_declared("tunes")
    monkeypatch.setattr(manager, "get_app_manifest", lambda app: manifest(""))
    assert not watch._hook_declared("tunes")

    def broken(app: str) -> Any:
        raise OSError("unreadable")

    monkeypatch.setattr(manager, "get_app_manifest", broken)
    assert not watch._hook_declared("tunes")


@pytest.mark.asyncio
async def test_read_switches_to_the_slots_own_tab_and_keeps_only_the_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_mod.reset_live_hub()
    hub = hub_mod.get_live_hub()
    hub.bind_target("tunes-1", "T1")
    calls: list[tuple[str, dict[str, Any]]] = []
    current = {"id": "T0", "url": "https://other.example/"}

    class _Ctx:
        async def helper(self, name: str, **kw: Any) -> Any:
            calls.append((name, kw))
            if name == "switch_tab":
                current.update(id=kw["target"], url="https://www.youtube.com/watch?v=abc")
            return {"targetId": current["id"], "url": current["url"]}

    async def control(_ctx: Any, action: str) -> dict[str, Any]:
        assert action == "state"
        return {"ok": True, "ended": True, "duration": 3.0}

    monkeypatch.setattr(browser_ops, "_ledgered_targets", lambda _sup: ["T0", "T1"])
    monkeypatch.setattr(browser_ops, "build_context", lambda *a, **k: _Ctx())
    monkeypatch.setattr(watch.media, "control", control)
    result = await watch._read(hub, "tunes-1", object(), SITES)
    assert result == {
        "ok": True,
        "ended": True,
        "duration": 3.0,
        "origin": "https://www.youtube.com",
    }
    assert ("switch_tab", {"target": "T1", "activate": False}) in calls

    monkeypatch.setattr(browser_ops, "_ledgered_targets", lambda _sup: ["T0"])
    assert (await watch._read(hub, "tunes-1", object(), SITES))["code"] == "no_tab"
    hub_mod.reset_live_hub()


@pytest.mark.asyncio
async def test_a_failed_read_is_retried_on_the_next_tick(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(**{"tunes-1": "tunes"})

    class _FailsFirst(_Tab):
        async def read(self, *a: Any) -> dict[str, Any]:
            if self.reads == 0:
                self.reads += 1
                raise ConnectionError("daemon gone")
            return await super().read(*a)

    tab = _FailsFirst([ENDED])
    monkeypatch.setattr(watch, "_read", tab.read)
    watch.ensure_watch(state, "tunes-1")
    await async_wait_until(lambda: len(env.sent) == 1, describe=lambda: f"{env.sent}")
