"""Tests for the Mochi builtin's lifecycle wiring (hooks.py).

What must hold: hooks are idempotent (the LifecycleDispatcher fires them at
gateway start/stop AND app enable/disable), disable leaks no task, the owner
loop survives a poll exception, and an unbound spawn seam degrades through
the ported failure paths instead of crashing the loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.mochi import hooks


class _Ctx:
    """Minimal AppContext stand-in (name, data_dir, events)."""

    def __init__(self, tmp_path: Path) -> None:
        self.name = "mochi"
        self.data_dir = tmp_path
        self.events = None
        self.config: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _fresh_module_state():
    hooks.set_spawn_fn(None)
    hooks._runtime = None
    yield
    hooks.set_spawn_fn(None)
    hooks._runtime = None


class TestLifecycleHookResolution:
    """Pins the framework fix: builtin hooks resolve via dotted package import
    (builtins ship no code in the data-home app dir), and — load-bearing —
    the hook and the routes share ONE module instance."""

    def test_builtin_hook_resolves_from_package(self):
        from kiro_crew.apps.lifecycle import LifecycleDispatcher

        func = LifecycleDispatcher._resolve_hook("mochi", "hooks:on_startup")
        assert func is hooks.on_startup  # same object, not a parallel copy

    def test_builtin_hook_missing_callable_raises(self):
        from kiro_crew.apps.lifecycle import LifecycleDispatcher

        with pytest.raises(ImportError, match="not found in builtin"):
            LifecycleDispatcher._resolve_hook("mochi", "hooks:nonexistent")

    def test_the_dispatcher_does_not_load_a_parallel_copy(self):
        """`_invoke` must resolve through `_resolve_hook`, not `load_app_module`.

        `load_app_module` is a file-path load that registers the module as
        `_kirocrew_app_<app>.<mod>` — a SECOND object. For a builtin whose routes
        read state its `on_startup` created, that means the hook populates the
        copy and the routes keep reading the empty original: no error anywhere,
        the feature is just inert. Upstream's launch-boundary change routed
        `_invoke` straight at `load_app_module`, which reintroduced exactly that.
        """
        import inspect

        from kiro_crew.apps.lifecycle import LifecycleDispatcher

        src = inspect.getsource(LifecycleDispatcher._invoke)
        assert "_resolve_hook(" in src
        # A CALL, not a mention — the explanatory comment names the function.
        assert "load_app_module(" not in src, (
            "a direct file-path load here gives a builtin's hook a different "
            "module object than its routes see"
        )

    @pytest.mark.asyncio
    async def test_dispatch_enable_starts_the_runtime(self, tmp_path, monkeypatch):
        """End-to-end through the real dispatcher: enable → on_startup runs."""
        from kiro_crew.apps import lifecycle as lc

        monkeypatch.setattr(lc, "app_dir", lambda name: tmp_path)
        dispatcher = lc.LifecycleDispatcher()
        app_info = {
            "name": "mochi",
            "manifest": {
                "backend": {"hooks": {"on_startup": "hooks:on_startup"}},
                "permissions": {},
            },
        }
        ok = await dispatcher.dispatch_enable(app_info)
        assert ok is True
        assert hooks._runtime is not None
        await hooks.on_shutdown(None)


class TestLifecycleHooks:
    @pytest.mark.asyncio
    async def test_startup_builds_runtime_and_shutdown_stops_it(self, tmp_path):
        ctx = _Ctx(tmp_path)
        await hooks.on_startup(ctx)
        rt = hooks._runtime
        assert rt is not None
        assert rt._task is not None and not rt._task.done()
        # stats were loaded and the launch recorded
        assert rt.stats.get_stats()["streak"] == 1

        await hooks.on_shutdown(ctx)
        assert rt._task is None
        # stats were flushed to disk on shutdown
        assert (tmp_path / "stats.json").exists()

    @pytest.mark.asyncio
    async def test_hooks_are_idempotent(self, tmp_path):
        ctx = _Ctx(tmp_path)
        await hooks.on_startup(ctx)
        first_task = hooks._runtime._task
        await hooks.on_startup(ctx)  # double-enable: same task, no second loop
        assert hooks._runtime._task is first_task

        await hooks.on_shutdown(ctx)
        await hooks.on_shutdown(ctx)  # double-disable: no error

    @pytest.mark.asyncio
    async def test_reenable_after_disable_restarts_the_loop(self, tmp_path):
        ctx = _Ctx(tmp_path)
        await hooks.on_startup(ctx)
        await hooks.on_shutdown(ctx)
        await hooks.on_startup(ctx)
        rt = hooks._runtime
        assert rt._task is not None and not rt._task.done()
        await hooks.on_shutdown(ctx)

    @pytest.mark.asyncio
    async def test_disable_leaves_no_pending_task(self, tmp_path):
        ctx = _Ctx(tmp_path)
        await hooks.on_startup(ctx)
        task = hooks._runtime._task
        await hooks.on_shutdown(ctx)
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_unbound_spawn_degrades_not_crashes(self, tmp_path):
        """With no spawn fn bound, a poll that wants to spawn takes the ported
        failure path (lock backoff) and the loop keeps running."""
        ctx = _Ctx(tmp_path)
        # A due freestyle task would spawn — missing queue triggers plan,
        # which raises through the seam; poller must swallow it (try/except
        # around trigger_plan) and the loop must go on.
        await hooks.on_startup(ctx)
        rt = hooks._runtime
        await rt.poller.poll()  # drive one poll directly: no exception escapes
        assert rt._task is not None and not rt._task.done()
        await hooks.on_shutdown(ctx)

    @pytest.mark.asyncio
    async def test_bound_spawn_reaches_the_seam(self, tmp_path):
        calls: list[tuple[str, str]] = []

        async def spawn(prompt: str, agent: str) -> str:
            calls.append((prompt, agent))
            return "spawn-1"

        hooks.set_spawn_fn(spawn)
        ctx = _Ctx(tmp_path)
        await hooks.on_startup(ctx)
        rt = hooks._runtime
        await rt.poller.poll()  # missing queue → trigger_plan → seam
        assert calls and "mochi-plan" in calls[0][0]
        # Background work runs as mochi-bg, never the foreground chat agent.
        assert calls[0][1] == "mochi-bg"
        await hooks.on_shutdown(ctx)

    @pytest.mark.asyncio
    async def test_deterministic_tasks_run_without_spawner(self, tmp_path):
        """Stats/pinned/deterministic queue tasks work with NO spawn fn —
        graceful degradation, not half-broken."""
        ctx = _Ctx(tmp_path)
        events: list[tuple[str, dict]] = []

        class Bus:
            def publish_to_app(self, event_type: str, data: dict) -> None:
                events.append((event_type, data))

        ctx.events = Bus()
        # execute_after must be RECENT: the ported stale-skip marks planned
        # move/mood tasks >30 min overdue as done UNEXECUTED (quirk 1 of the
        # poller port — this test originally tripped over it with a month-old
        # timestamp, which was the port working as specified).
        import datetime as _dt

        just_due = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        far_future = "2099-01-01T00:00:00.000Z"
        (tmp_path / "mochi-queue.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "planned_at": just_due,
                    "planned_until": far_future,
                    "needs_replan": False,
                    "tasks": [
                        {
                            "id": "m1",
                            "type": "mood",
                            "action": {"mood": "happy"},
                            "execute_after": just_due,
                            "done": False,
                            "source": "agent",
                        }
                    ],
                }
            )
        )
        await hooks.on_startup(ctx)
        await hooks._runtime.poller.poll()
        mood_events = [e for e in events if e[0] == "mochi:mood"]
        assert len(mood_events) == 1
        # Mood now routes through the pet state manager (single source of truth),
        # which broadcasts via the {args:[value]} helper and stores current_mood.
        assert mood_events[0][1]["args"] == ["happy"]
        assert hooks._runtime.state_manager.current_mood == "happy"
        await hooks.on_shutdown(ctx)


class TestAgentAuthoredEventsAreRedacted:
    """Every publish sink carrying agent-authored free text must scrub
    credentials before the payload reaches the browser -- the same two-way
    redaction the plan/pins/activity read paths apply. Split-literal key so
    CodeQL does not read this redaction-CONTROL test as storing a real secret.
    """

    async def _bus(self, tmp_path: Path):
        ctx = _Ctx(tmp_path)
        events: list[tuple[str, dict]] = []

        class Bus:
            def publish_to_app(self, event_type: str, data: dict) -> None:
                events.append((event_type, data))

        ctx.events = Bus()
        await hooks.on_startup(ctx)
        return ctx, events

    @pytest.mark.asyncio
    async def test_watchlist_changed_redacts_item_credentials(self, tmp_path):
        planted = "AKIA" + "IOSFODNN7EXAMPLE"
        (tmp_path / "mochi-watchlist.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [{"id": "w1", "kind": "url", "title": f"watch {planted}"}],
                }
            )
        )
        ctx, events = await self._bus(tmp_path)
        try:
            hooks._runtime._watchlist_changed()
            wl_events = [e for e in events if e[0] == "mochi:watchlist-changed"]
            assert wl_events, "watchlist-changed must be published"
            assert planted not in json.dumps(wl_events[-1][1])
        finally:
            await hooks.on_shutdown(ctx)

    @pytest.mark.asyncio
    async def test_degraded_notify_redacts_summary(self, tmp_path):
        planted = "AKIA" + "IOSFODNN7EXAMPLE"
        ctx, events = await self._bus(tmp_path)
        try:
            hooks._WatchlistCallbacks(hooks._runtime).on_notify_direct(f"leaked {planted}")
            notify = [e for e in events if e[0] == "mochi:notify"]
            assert notify, "degraded notify must be published"
            assert planted not in json.dumps(notify[-1][1])
        finally:
            await hooks.on_shutdown(ctx)

    @pytest.mark.asyncio
    async def test_mood_is_redacted_before_set_mood(self, tmp_path):
        planted = "AKIA" + "IOSFODNN7EXAMPLE"
        ctx, _events = await self._bus(tmp_path)
        sm = hooks._runtime.state_manager
        orig_set_mood = sm.set_mood
        try:
            captured: list[str] = []
            sm.set_mood = lambda m, now: captured.append(m)  # type: ignore[method-assign]
            await hooks._PollerCallbacks(hooks._runtime).on_mood({"mood": f"busy {planted}"})
            assert captured, "on_mood must set a mood"
            assert planted not in captured[0], "mood must be scrubbed before set_mood"
        finally:
            # _runtime is a module global reused across tests — restore set_mood.
            sm.set_mood = orig_set_mood  # type: ignore[method-assign]
            await hooks.on_shutdown(ctx)

    @pytest.mark.asyncio
    async def test_mood_only_notify_redacts_mood(self, tmp_path):
        # A notify carrying ONLY a mood (no summary) must still scrub it: the mood
        # reaches the browser via /pet-state and /stats. The redaction used to live
        # inside the has_summary branch, so a mood-only notify leaked the raw value.
        planted = "AKIA" + "IOSFODNN7EXAMPLE"
        ctx, _events = await self._bus(tmp_path)
        sm = hooks._runtime.state_manager
        orig_set_mood = sm.set_mood
        try:
            captured: list[str] = []
            sm.set_mood = lambda m, now: captured.append(m)  # type: ignore[method-assign]
            hooks._runtime.notify_user({"mood": f"busy {planted}"})  # no summary
            assert captured, "mood-only notify must still set a mood"
            assert planted not in captured[0], "mood must be scrubbed before set_mood"
        finally:
            sm.set_mood = orig_set_mood  # type: ignore[method-assign]
            await hooks.on_shutdown(ctx)

    @pytest.mark.asyncio
    async def test_imported_pack_description_never_enters_the_persona(self, tmp_path):
        # A downloaded pack's meta.description is untrusted; it must NOT be
        # sourced into the persona (agent system prompt) — prompt-injection.
        from kiro_crew.apps.builtins.mochi.settings import save_settings

        save_settings(tmp_path, {"activeAppearance": "evil-imported-pack"})
        ctx, _events = await self._bus(tmp_path)
        try:
            pack_id, description = hooks._runtime._stored_appearance()
            assert pack_id == "evil-imported-pack"
            assert description is None, "custom pack description must not reach the persona"
        finally:
            await hooks.on_shutdown(ctx)


class TestStartupPromptOrdering:
    """The pet's persona reaches its agent only if the rendered prompt files are
    on disk BEFORE ``apply_policy`` materializes the agent configs.

    ``bridges._apply_agent_prompt`` pins the prompt "ABSOLUTE and EXISTING only":
    a prompt whose file does not yet exist is dropped with a warning ("prompt file
    does not exist, ignoring"), so the materialized ``mochi--mochi.json`` carries
    no ``prompt`` and the pet answers as the generic default agent until a later
    restart. The drop itself is already covered by
    ``test_mochi_agent_prompt.py::TestApplyAgentPrompt`` — the untested gap this
    pins is the ``on_startup`` ORDERING that used to run ``apply_policy`` (which
    reads the files) BEFORE ``MochiRuntime.__init__`` rendered them.
    """

    @pytest.mark.asyncio
    async def test_prompt_files_exist_before_policy_is_applied(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.mochi import soul_loader

        ctx = _Ctx(tmp_path)
        seen: dict[str, bool] = {}
        real_apply = hooks.apply_policy

        def _spy(data_dir: Any, settings: Any) -> Any:
            # Capture whether BOTH generated prompt files are on disk at the exact
            # moment the agent configs are (re)materialized. Before the fix
            # apply_policy ran first, so neither existed and the persona was
            # silently dropped from the materialized config.
            seen["chat"] = soul_loader.rendered_prompt_path(Path(data_dir)).is_file()
            seen["bg"] = soul_loader.rendered_bg_prompt_path(Path(data_dir)).is_file()
            return real_apply(data_dir, settings)

        monkeypatch.setattr(hooks, "apply_policy", _spy)
        await hooks.on_startup(ctx)
        try:
            assert seen == {"chat": True, "bg": True}
            # And the files really are the rendered persona documents on disk.
            assert soul_loader.rendered_prompt_path(tmp_path).is_file()
            assert soul_loader.rendered_bg_prompt_path(tmp_path).is_file()
        finally:
            await hooks.on_shutdown(ctx)
