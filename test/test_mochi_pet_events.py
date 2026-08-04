"""Tests for the pet-event route that drives the behaviour state machine.

Kept separate from ``test_mochi_routes`` only for size; the harness mirrors it.

These pin the fix for a bug that produced no error anywhere: ``apply_event`` had
exactly two call sites (``connect`` at startup, ``disconnect`` at shutdown), so
the ``pet:state-change`` broadcast never carried anything but ``idle``/``offline``
and every appearance pack rendered a single clip forever.
"""

from __future__ import annotations

import ast
import contextlib
import json
import pathlib
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.mochi import hooks
from kiro_crew.apps.builtins.mochi.backend import routes


class _Ctx:
    def __init__(self, tmp_path) -> None:
        self.name = "mochi"
        self.data_dir = tmp_path
        self.events = None
        self.config: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)
    hooks._runtime = None
    yield
    hooks._runtime = None


@contextlib.asynccontextmanager
async def _live_runtime(tmp_path):
    """Start the runtime inside the test's own loop, stop it on exit.

    An async CONTEXT MANAGER rather than an ``@pytest_asyncio.fixture``: the
    suite pins pytest-asyncio 0.20.3, whose async-fixture wrapper reads the
    ``fixturedef.unittest`` attribute pytest 8.1 removed — on CI every
    async-generator fixture errors at setup. The repo avoids the decorator by
    convention (see test_denied_commands_api.py's module docstring).
    """
    await hooks.on_startup(_Ctx(tmp_path))
    try:
        yield hooks._runtime
    finally:
        await hooks.on_shutdown(None)


def _post(body: dict | None):
    req = make_mocked_request(
        "POST", "/api/apps/mochi/pet-event", headers={"Content-Type": "application/json"}
    )

    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    req.json = _json  # type: ignore[method-assign]
    return req


class TestPetEventRoute:
    @pytest.mark.asyncio
    async def test_user_input_moves_idle_to_thinking(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            runtime.state_manager.set_pet_state("idle", 0)
            res = await routes._handle_pet_event(_post({"event": "user_input"}))
            assert res.status == 200
            assert json.loads(res.text)["state"] == "thinking"
            assert runtime.state_manager.current == "thinking"

    @pytest.mark.asyncio
    async def test_tool_call_then_complete_round_trips(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            runtime.state_manager.set_pet_state("idle", 0)
            await routes._handle_pet_event(_post({"event": "user_input"}))
            await routes._handle_pet_event(_post({"event": "tool_call"}))
            assert runtime.state_manager.current == "working"
            await routes._handle_pet_event(_post({"event": "task_complete"}))
            assert runtime.state_manager.current == "idle"

    @pytest.mark.asyncio
    async def test_error_event_reaches_error_state(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            runtime.state_manager.set_pet_state("idle", 0)
            await routes._handle_pet_event(_post({"event": "error"}))
            assert runtime.state_manager.current == "error"

    @pytest.mark.asyncio
    async def test_unknown_event_is_a_400_not_a_silent_noop(self, tmp_path):
        async with _live_runtime(tmp_path):
            # A typo must be loud: swallowing it reproduces the original bug, where
            # the pet simply never animated again and nothing said why.
            res = await routes._handle_pet_event(_post({"event": "definitely_not_real"}))
            assert res.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event", ["connect", "disconnect", "walk_start", "timeout"])
    async def test_non_chat_events_are_refused(self, tmp_path, event):
        async with _live_runtime(tmp_path) as runtime:
            # These are the runtime's own statements (gateway connectivity, the walk
            # routes that also MOVE the pet, the error deadline). A page must not be
            # able to contradict them even though the transition table accepts them.
            before = runtime.state_manager.current
            res = await routes._handle_pet_event(_post({"event": event}))
            assert res.status == 400
            assert runtime.state_manager.current == before

    @pytest.mark.asyncio
    async def test_malformed_body_is_a_400(self, tmp_path):
        async with _live_runtime(tmp_path):
            res = await routes._handle_pet_event(_post(None))
            assert res.status == 400

    @pytest.mark.asyncio
    async def test_route_is_registered(self):
        from aiohttp import web

        app = web.Application()
        routes.register_routes(app)
        paths = {
            (r.method, str(r.resource.canonical))
            for r in app.router.routes()
            if r.resource is not None
        }
        assert ("POST", "/api/apps/mochi/pet-event") in paths


class TestNotifyMood:
    """A notify action's ``mood`` must reach the state manager.

    Mood regression pin: the mood field was once dropped by the notify
    path — the bubble text arrived over
    ``mochi:notify`` while ``mochi:mood`` was never broadcast, so no surface ever
    saw a mood — no title-bar mood, no mood animation, for every pack.
    """

    @pytest.mark.asyncio
    async def test_mood_is_applied_and_broadcast(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            seen: list[tuple[str, dict]] = []
            runtime.publish = lambda ch, data: seen.append((ch, data))  # type: ignore[assignment]
            runtime.notify_user({"summary": "boo", "mood": "scared"})
            assert runtime.state_manager.current_mood == "scared"
            assert ("mochi:mood", {"args": ["scared"]}) in seen
            # The notify event itself must still go out — the bubble depends on it.
            assert any(ch == "mochi:notify" for ch, _ in seen)

    @pytest.mark.asyncio
    async def test_notify_without_mood_leaves_mood_alone(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            runtime.state_manager.set_mood("busy", 0)
            runtime.notify_user({"summary": "fyi"})
            assert runtime.state_manager.current_mood == "busy"

    @pytest.mark.asyncio
    async def test_non_string_mood_is_ignored(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            runtime.notify_user({"summary": "x", "mood": 7})
            assert runtime.state_manager.current_mood == "neutral"

    @pytest.mark.asyncio
    async def test_notify_summary_credential_is_redacted_before_broadcast(self, tmp_path):
        """The mochi:notify broadcast is a browser sink; an agent-authored
        credential in the summary must be scrubbed before it goes out."""
        async with _live_runtime(tmp_path) as runtime:
            seen: list[tuple[str, dict]] = []
            runtime.publish = lambda ch, data: seen.append((ch, data))  # type: ignore[assignment]
            # Fake AWS key, split to avoid a CodeQL clear-text-storage FP on a
            # redaction-control test (runtime value is a full AKIA key).
            planted = "AKIA" + "IOSFODNN7EXAMPLE"
            runtime.notify_user({"summary": f"use {planted} now"})
            notify = next(d for ch, d in seen if ch == "mochi:notify")
            assert planted not in notify["summary"]
            assert "[REDACTED" in notify["summary"]


class TestChatPush:
    """notify_user's pushToChat sink: content choice, guard, window, log.

    The guard semantics are the original main process's (exact match OR >80%
    word overlap against recently accepted pushes); the TIME window is the
    `quietPeriodMins` setting, which this feature gives its documented
    meaning ("don't re-notify within this window").
    """

    def _pushes(self, seen):
        return [d for ch, d in seen if ch == "mochi:chat-push"]

    @pytest.mark.asyncio
    async def test_push_prefers_chat_message_over_summary(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            seen: list[tuple[str, dict]] = []
            runtime.publish = lambda ch, data: seen.append((ch, data))  # type: ignore[assignment]
            runtime.notify_user(
                {"summary": "bubble", "chatMessage": "the long story", "pushToChat": True}
            )
            assert [p["content"] for p in self._pushes(seen)] == ["the long story"]

    @pytest.mark.asyncio
    async def test_no_push_without_flag_and_notify_logs_activity(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            seen: list[tuple[str, dict]] = []
            runtime.publish = lambda ch, data: seen.append((ch, data))  # type: ignore[assignment]
            logged: list[tuple[str, str]] = []
            runtime._log_activity = lambda kind, content: logged.append((kind, content))
            runtime.notify_user({"summary": "fyi"})
            assert self._pushes(seen) == []
            # The activity-log entry is load-bearing: the watch skill tells the
            # agent to read it before notifying (agent-side dedup).
            assert ("notification", "fyi") in logged

    @pytest.mark.asyncio
    async def test_exact_duplicate_within_window_is_dropped(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            seen: list[tuple[str, dict]] = []
            runtime.publish = lambda ch, data: seen.append((ch, data))  # type: ignore[assignment]
            runtime.notify_user({"summary": "CR-123 still pending", "pushToChat": True})
            runtime.notify_user({"summary": "CR-123 still pending", "pushToChat": True})
            assert len(self._pushes(seen)) == 1

    @pytest.mark.asyncio
    async def test_fuzzy_duplicate_is_dropped_and_distinct_is_not(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            seen: list[tuple[str, dict]] = []
            runtime.publish = lambda ch, data: seen.append((ch, data))  # type: ignore[assignment]
            runtime.notify_user(
                {"summary": "Pipeline release-web is still blocked on approval", "pushToChat": True}
            )
            # Same words, different punctuation/case — >80% overlap, dropped.
            runtime.notify_user(
                {
                    "summary": "pipeline RELEASE-WEB: is still blocked, on approval!",
                    "pushToChat": True,
                }
            )
            # Genuinely different content — delivered.
            runtime.notify_user(
                {"summary": "Your flight price dropped to $210", "pushToChat": True}
            )
            assert len(self._pushes(seen)) == 2

    @pytest.mark.asyncio
    async def test_duplicate_outside_window_is_delivered(self, tmp_path, monkeypatch):
        async with _live_runtime(tmp_path) as runtime:
            from kiro_crew.apps.builtins.mochi import hooks as hooks_mod

            seen: list[tuple[str, dict]] = []
            runtime.publish = lambda ch, data: seen.append((ch, data))  # type: ignore[assignment]
            clock = {"now": 1_000_000}
            monkeypatch.setattr(hooks_mod, "_now_ms", lambda: clock["now"])
            runtime.notify_user({"summary": "water reminder", "pushToChat": True})
            clock["now"] += 6 * 60_000  # default quietPeriodMins=5 — step past it
            runtime.notify_user({"summary": "water reminder", "pushToChat": True})
            assert len(self._pushes(seen)) == 2

    @pytest.mark.asyncio
    async def test_emoji_only_pushes_never_fuzzy_match(self, tmp_path):
        async with _live_runtime(tmp_path) as runtime:
            # normalize() strips punctuation; empty word sets must not match
            # everything (the original's words.size === 0 guard).
            seen: list[tuple[str, dict]] = []
            runtime.publish = lambda ch, data: seen.append((ch, data))  # type: ignore[assignment]
            runtime.notify_user({"summary": "!!!", "pushToChat": True})
            runtime.notify_user({"summary": "???", "pushToChat": True})
            assert len(self._pushes(seen)) == 2


class TestManifestDeclaresEveryPublishedEvent:
    """``permissions.events`` must cover every event name the backend publishes.

    ``EventBus.publish`` raises ``PermissionError`` on an undeclared name, and the
    publish sites are spread across hooks, routes and the state manager — so a new
    feature that publishes a new event is one edit away from a runtime raise on a
    path no unit test happens to drive. That is exactly how ``mochi:quiet`` and
    ``mochi:chat-push`` shipped undeclared: both were added after the manifest was
    written, and the surrounding tests stub ``publish`` (which skips the permission
    check), so nothing noticed.

    This walks the AST rather than grepping so it sees the name in CALL position
    only, and it follows the module-level channel constants the state manager uses
    (``PET_STATE_CHANGE_CHANNEL``), which a string search over the app package
    would report as unpublished.
    """

    _PUBLISH_FNS = {"publish", "publish_to_app", "_broadcast", "broadcast"}
    # Forwarders take the caller's name through; their own literal-less call is not
    # a publish site of its own.
    _PASSTHROUGH_PARAMS = {"event_type", "channel"}

    def _pkg_root(self):
        import kiro_crew.apps.builtins.mochi as pkg

        return pathlib.Path(pkg.__file__).parent

    def _published_names(self, root) -> tuple[set[str], list[str]]:
        published: set[str] = set()
        unresolved: list[str] = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            consts = {
                target.id: node.value.value
                for node in tree.body
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name not in self._PUBLISH_FNS:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    published.add(arg.value)
                elif isinstance(arg, ast.Name) and arg.id in consts:
                    published.add(consts[arg.id])
                elif isinstance(arg, ast.Name) and arg.id in self._PASSTHROUGH_PARAMS:
                    continue
                else:
                    unresolved.append(f"{path.name}:{node.lineno}")
        return published, unresolved

    def test_every_published_event_is_declared(self) -> None:
        root = self._pkg_root()
        declared = set(json.loads((root / "app.json").read_text())["permissions"]["events"])
        published, unresolved = self._published_names(root)

        assert not unresolved, (
            "publish() call(s) whose event name this guard cannot resolve, so it "
            f"cannot check them against the manifest: {unresolved}. Use a literal or "
            "a module-level constant."
        )
        undeclared = published - declared
        assert not undeclared, (
            f"published but absent from permissions.events: {sorted(undeclared)} — "
            "EventBus.publish raises PermissionError on these at runtime."
        )

    def test_no_declared_event_is_unpublished(self) -> None:
        """The reverse direction, so the list cannot rot into a wish list.

        A declared-but-unpublished name is not a crash, but it is a permission
        granted for nothing — and it hides the real defect the forward check exists
        for by making the list look maintained.
        """
        root = self._pkg_root()
        declared = set(json.loads((root / "app.json").read_text())["permissions"]["events"])
        published, _ = self._published_names(root)
        assert not declared - published, (
            f"declared in permissions.events but never published: "
            f"{sorted(declared - published)}"
        )
