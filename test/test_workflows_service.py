"""M6.3/M6.4 — WorkflowService façade (author / start / status / result / cancel).

This is the gateway-side object the chat workflow_* MCP tools and the Workflows
tab both talk to. Asserts:
  * author: NL intent → validated script (retries on invalid model output, fails clean)
  * start: validates + launches a background run, returns run_id, result captured
  * on_done fires with the originating session_key (M6.4 routes result→chat)
  * status/result/list/cancel reach the shared registry

All against fakes — no real model/kiro-cli. ``stream_and_collect`` is patched.
See GATES (M6) and docs/system-specs/modules/workflows.md.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from unittest.mock import AsyncMock

import pytest

import kiro_crew.llm_helpers as llm_helpers
from kiro_crew.acp.runtime import AcpRequestTimeout
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.workflows.library import WorkflowDefinitionLibrary
from kiro_crew.workflows.service import WorkflowService
from kiro_crew.workflows.store import WorkflowRunStore

pytestmark = pytest.mark.asyncio

GOOD_SCRIPT = (
    'META = {"name": "demo", "description": "d"}\n'
    "async def workflow(ctx):\n"
    "    ctx.log('hi')\n"
    "    return {'ok': True}\n"
)


class FakeProvider:
    def __init__(self, scripted: list[str]) -> None:
        self._scripted = scripted
        self._i = 0


class FakeSessions:
    def __init__(self, scripted: list[str]) -> None:
        self._scripted = scripted
        self.released: list[str] = []
        self.acquired: list[tuple[str, dict]] = []  # (key, kwargs) per get_or_create
        self.destroyed: list[str] = []

    async def get_or_create(self, key, **kw):
        self.acquired.append((key, kw))
        return FakeProvider(self._scripted), True, False

    def release(self, key, *, cleanup=False):
        self.released.append(key)

    async def destroy(self, key):
        self.destroyed.append(key)


class StartupScriptedSessions(FakeSessions):
    def __init__(self, failures: list[BaseException]) -> None:
        super().__init__([])
        self._failures = list(failures)
        self.events: list[tuple[str, str]] = []

    async def get_or_create(self, key, **kw):
        self.events.append(("acquire", key))
        self.acquired.append((key, kw))
        if self._failures:
            raise self._failures.pop(0)
        return FakeProvider([]), True, False

    async def destroy(self, key):
        self.events.append(("destroy", key))
        await super().destroy(key)


class DestroyFailingSessions(FakeSessions):
    async def destroy(self, key):
        self.destroyed.append(key)
        raise RuntimeError("destroy failed")


class BlockingSourceStore:
    """Store that holds source-bearing writes until a test releases them."""

    def __init__(self, *, wait_timeout: float = 2) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.save_thread_id = 0
        self.deleted: list[str] = []
        self.wait_timeout = wait_timeout

    def load_all(self) -> list[dict]:
        return []

    def save(self, _run_id: str, payload: dict) -> None:
        if not payload.get("source"):
            return
        self.save_thread_id = threading.get_ident()
        self.started.set()
        if not self.release.wait(timeout=self.wait_timeout):
            raise TimeoutError("test did not release workflow persistence")

    def delete(self, run_id: str) -> None:
        self.deleted.append(run_id)


def _patch_stream(monkeypatch, replies: list[str]) -> dict:
    """Patch stream_and_collect to return successive canned replies."""
    state = {"i": 0}

    async def fake_stream(provider, message, **kw):
        r = replies[min(state["i"], len(replies) - 1)]
        state["i"] += 1
        return r

    monkeypatch.setattr(llm_helpers, "stream_and_collect", fake_stream)
    # service.py binds stream_and_collect at module top (top-level-imports rule),
    # so patch the name in the service module's namespace too.
    import kiro_crew.workflows.service as svc_mod

    monkeypatch.setattr(svc_mod, "stream_and_collect", fake_stream)
    return state


async def _wait_terminal(svc: WorkflowService, run_id: str, timeout: float = 3.0):
    t = 0.0
    while t < timeout:
        snap = svc.status(run_id)
        if snap and snap["status"] != "running":
            return snap
        await asyncio.sleep(0.02)
        t += 0.02
    raise AssertionError("run did not finish")


# --------------------------------------------------------------------------- #
# author
# --------------------------------------------------------------------------- #


async def test_author_returns_valid_script(monkeypatch) -> None:
    _patch_stream(monkeypatch, [GOOD_SCRIPT])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.author("do a tiny thing")
    assert out["ok"] is True


async def test_author_uses_isolated_destroyed_lite_session(monkeypatch) -> None:
    """Authoring destroys its production SessionManager session completely."""
    _patch_stream(monkeypatch, [GOOD_SCRIPT])
    config = KiroCrewConfig()
    providers: list[AsyncMock] = []
    agents: list[str] = []

    def provider_factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.is_process_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        provider.has_active_turn = lambda: False
        provider.cwd = ""
        providers.append(provider)
        agents.append(agent or "")
        return provider

    sessions = SessionManager(config, provider_factory=provider_factory)
    svc = WorkflowService(sessions=sessions, persist=False)
    key = "wf-author:wf_000001:a1"
    sessions._session_map.set(key, "stale-author-sid")
    try:
        out = await svc.author("do a tiny thing")

        assert out["ok"] is True
        assert agents == ["kirocrew-lite"]
        assert providers[0].shutdown.await_count == 1
        assert not sessions.has_session(key)
        assert not sessions._session_map.has_hint(key)
    finally:
        sessions._session_map.set("dashboard:pending-close", "sid-pending-close")
        flush_task = sessions._session_map._flush_task
        assert flush_task is not None
        await sessions.close_all()
        assert flush_task.done()
        assert sessions._session_map._flush_task is None


async def test_author_success_survives_teardown_failure(monkeypatch, caplog) -> None:
    _patch_stream(monkeypatch, [GOOD_SCRIPT])
    sessions = DestroyFailingSessions([])
    svc = WorkflowService(sessions=sessions)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.workflows.service"):
        out = await svc.author("x")

    assert out["ok"] is True
    assert sessions.destroyed == [sessions.acquired[0][0]]
    records = [r for r in caplog.records if "workflow author teardown failed" in r.message]
    assert len(records) == 1
    assert records[0].exc_info is not None


@pytest.mark.parametrize("primary_phase", ["generation", "validation"])
async def test_author_primary_error_survives_teardown_failure(
    monkeypatch, caplog, primary_phase
) -> None:
    import kiro_crew.workflows.service as svc_mod

    primary = LookupError(f"{primary_phase} failed")

    async def generate(provider, message, **kwargs):
        if primary_phase == "generation":
            raise primary
        return GOOD_SCRIPT

    monkeypatch.setattr(svc_mod, "stream_and_collect", generate)
    if primary_phase == "validation":

        def fail_validation(source):
            raise primary

        monkeypatch.setattr(svc_mod, "validate", fail_validation)

    sessions = DestroyFailingSessions([])
    svc = WorkflowService(sessions=sessions)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.workflows.service"):
        with pytest.raises(LookupError) as raised:
            await svc.author("x")

    assert raised.value is primary
    assert sessions.destroyed == [sessions.acquired[0][0]]
    records = [r for r in caplog.records if "workflow author teardown failed" in r.message]
    assert len(records) == 1
    assert records[0].exc_info is not None


async def test_author_retries_then_succeeds(monkeypatch) -> None:
    # first reply invalid (import), second valid → author must retry and succeed
    _patch_stream(monkeypatch, ["import os\n" + GOOD_SCRIPT, GOOD_SCRIPT])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.author("x")
    assert out["ok"] is True


async def test_author_retries_transient_startup_with_fresh_session(monkeypatch) -> None:
    _patch_stream(monkeypatch, [GOOD_SCRIPT])
    import kiro_crew.workflows.service as svc_mod

    monkeypatch.setattr(svc_mod, "_AUTHOR_STARTUP_BACKOFF_SECS", (0.0, 0.0))
    sessions = StartupScriptedSessions([AcpRequestTimeout("initialize timed out")])
    svc = WorkflowService(sessions=sessions)

    out = await svc.author("x")

    assert out["ok"] is True
    assert len(sessions.acquired) == 2
    assert sessions.acquired[0][0] != sessions.acquired[1][0]
    assert sessions.destroyed == [key for key, _ in sessions.acquired]


async def test_author_destroys_partial_session_before_startup_retry(monkeypatch) -> None:
    _patch_stream(monkeypatch, [GOOD_SCRIPT])
    import kiro_crew.workflows.service as svc_mod

    monkeypatch.setattr(svc_mod, "_AUTHOR_STARTUP_BACKOFF_SECS", (0.0, 0.0))
    sessions = StartupScriptedSessions([AcpRequestTimeout("session/new timed out")])
    svc = WorkflowService(sessions=sessions)

    out = await svc.author("x")

    assert out["ok"] is True
    first_key = sessions.acquired[0][0]
    second_key = sessions.acquired[1][0]
    assert sessions.events == [
        ("acquire", first_key),
        ("destroy", first_key),
        ("acquire", second_key),
        ("destroy", second_key),
    ]


async def test_author_does_not_retry_arbitrary_startup_failure(monkeypatch) -> None:
    sessions = StartupScriptedSessions([ValueError("invalid configuration")])
    svc = WorkflowService(sessions=sessions)

    with pytest.raises(ValueError, match="invalid configuration"):
        await svc.author("x")

    assert len(sessions.acquired) == 1
    assert sessions.destroyed == [sessions.acquired[0][0]]


async def test_author_startup_retry_exhaustion_preserves_last_error(monkeypatch) -> None:
    import kiro_crew.workflows.service as svc_mod

    monkeypatch.setattr(svc_mod, "_AUTHOR_STARTUP_BACKOFF_SECS", (0.0, 0.0))
    failures = [
        AcpRequestTimeout("initialize timed out on attempt 1"),
        AcpRequestTimeout("initialize timed out on attempt 2"),
        AcpRequestTimeout("initialize timed out on attempt 3"),
    ]
    sessions = StartupScriptedSessions(failures)
    svc = WorkflowService(sessions=sessions)

    with pytest.raises(AcpRequestTimeout, match="attempt 3") as raised:
        await svc.author("x")

    assert raised.value is failures[-1]
    assert len(sessions.acquired) == 3
    assert sessions.destroyed == [key for key, _ in sessions.acquired]


async def test_author_all_invalid_fails_clean(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["import os\nasync def workflow(ctx):\n    return 1\n"])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.author("x")
    assert out["ok"] is False
    assert out["errors"]


async def test_author_strips_code_fence(monkeypatch) -> None:
    fenced = "```python\n" + GOOD_SCRIPT + "```"
    _patch_stream(monkeypatch, [fenced])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.author("x")
    assert out["ok"] is True
    assert "```" not in out["source"]


async def test_author_uses_a_matching_saved_workflow_as_an_adaptation_example(
    monkeypatch, tmp_path
) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    parent = library.create(
        source=GOOD_SCRIPT,
        name="Debug Project",
        description="Investigate failures and identify the root cause",
    )
    adapted = GOOD_SCRIPT.replace(
        '"description": "d"',
        f'"description": "d", "adapted_from": "{parent["id"]}@1"',
    )
    captured: list[str] = []

    async def fake_stream(provider, message, **kw):
        captured.append(message)
        return adapted

    import kiro_crew.workflows.service as svc_mod

    monkeypatch.setattr(svc_mod, "stream_and_collect", fake_stream)
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)

    out = await svc.author("debugging a failing login flow")

    assert parent["id"] in captured[0]
    assert GOOD_SCRIPT in captured[0]
    assert out["derived_from"] == {"workflow_id": parent["id"], "revision": 1}


async def test_authoring_does_not_use_task_plans_as_python_examples(monkeypatch, tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    task_plan = library.create(
        source="agents:\n  debug:\n    prompt: debug the failure\n",
        name="Debug Task Plan",
        source_format="task-plan",
    )
    captured: list[str] = []

    async def fake_stream(provider, message, **kw):
        captured.append(message)
        return GOOD_SCRIPT

    import kiro_crew.workflows.service as svc_mod

    monkeypatch.setattr(svc_mod, "stream_and_collect", fake_stream)
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)

    out = await svc.author("debug the failure")

    assert out["ok"] is True
    assert task_plan["id"] not in captured[0]


async def test_author_rejects_lineage_to_an_unsupplied_historical_revision(
    monkeypatch, tmp_path
) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    parent = library.create(
        source=GOOD_SCRIPT,
        name="Debug Project",
        description="Investigate failures and identify the root cause",
    )
    library.update(
        parent["id"],
        source=GOOD_SCRIPT.replace("ctx.log('hi')", "ctx.log('revision two')"),
        expected_revision=1,
    )
    adapted = GOOD_SCRIPT.replace(
        '"description": "d"',
        f'"description": "d", "adapted_from": "{parent["id"]}@1"',
    )
    _patch_stream(monkeypatch, [adapted])
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)

    out = await svc.author("debugging a failing login flow")

    assert out["ok"] is True
    assert out["derived_from"] is None


async def test_author_searches_the_definition_library_off_the_event_loop(
    monkeypatch, tmp_path
) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    library.create(source=GOOD_SCRIPT, name="Debug Project")
    loop_thread = threading.get_ident()
    search_threads: list[int] = []
    real_search = library.search

    def observed_search(*args, **kwargs):
        search_threads.append(threading.get_ident())
        return real_search(*args, **kwargs)

    monkeypatch.setattr(library, "search", observed_search)
    _patch_stream(monkeypatch, [GOOD_SCRIPT])
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)

    out = await svc.author("debug a failure")

    assert out["ok"] is True
    assert search_threads and search_threads[0] != loop_thread


async def test_save_and_update_definition_validate_before_persisting(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)

    saved = svc.save_definition(GOOD_SCRIPT)
    assert saved["ok"] is True
    assert saved["definition"]["slug"] == "demo"

    changed = GOOD_SCRIPT.replace("ctx.log('hi')", "ctx.log('changed')")
    updated = svc.update_definition(saved["definition"]["id"], source=changed, expected_revision=1)
    assert updated["ok"] is True
    assert updated["definition"]["revision"] == 2

    invalid = svc.save_definition("import os\n")
    assert invalid["ok"] is False
    assert len(library.list()) == 1


async def test_save_and_update_task_plan_definition_use_yaml_validation(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    source = "agents:\n  test:\n    prompt: run tests\n    force_approval: true\n"

    saved = svc.save_definition(source, name="Test", source_format="task-plan")
    updated = svc.update_definition(
        saved["definition"]["id"],
        source=source.replace("run tests", "run the full suite"),
        expected_revision=1,
    )
    rejected = svc.save_definition("agents: []\n", source_format="task-plan")

    assert saved["ok"] is True
    assert saved["definition"]["format"] == "task-plan"
    assert updated["ok"] is True
    assert updated["definition"]["format"] == "task-plan"
    assert rejected["ok"] is False


async def test_update_definition_distinguishes_missing_from_stale_revision(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    saved = svc.save_definition(GOOD_SCRIPT)

    missing = svc.update_definition("wfd_missing", source=GOOD_SCRIPT, expected_revision=1)
    stale = svc.update_definition(
        saved["definition"]["id"], source=GOOD_SCRIPT, expected_revision=99
    )

    assert missing["not_found"] is True
    assert "conflict" not in missing
    assert stale["conflict"] is True


async def test_save_definition_ignores_lineage_declared_only_in_source(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    parent = library.create(
        source=GOOD_SCRIPT,
        name="Debug Project",
        description="Investigate failures",
    )
    adapted = GOOD_SCRIPT.replace(
        '"description": "d"',
        f'"description": "d", "adapted_from": "{parent["id"]}@1"',
    )
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)

    saved = svc.save_definition(adapted)

    assert saved["ok"] is True
    assert saved["definition"]["derived_from"] is None


async def test_save_definition_validates_explicit_lineage_against_revision_history(
    tmp_path,
) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    parent = library.create(source=GOOD_SCRIPT, name="Debug Project")
    library.update(
        parent["id"],
        source=GOOD_SCRIPT.replace("ctx.log('hi')", "ctx.log('revision two')"),
        expected_revision=1,
    )
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)

    accepted = svc.save_definition(
        GOOD_SCRIPT,
        name="Adapted",
        derived_from={"workflow_id": parent["id"], "revision": 1},
    )
    rejected = svc.save_definition(
        GOOD_SCRIPT,
        name="Fabricated",
        derived_from={"workflow_id": "wfd_missing", "revision": 99},
    )

    assert accepted["ok"] is True
    assert accepted["definition"]["derived_from"] == {
        "workflow_id": parent["id"],
        "revision": 1,
    }
    assert rejected["ok"] is False
    assert "lineage" in rejected["error"]


async def test_save_and_update_reject_source_that_would_be_redacted(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    sensitive = GOOD_SCRIPT.replace("ctx.log('hi')", "ctx.log('AKIAIOSFODNN7EXAMPLE')")

    rejected_create = svc.save_definition(sensitive)
    saved = svc.save_definition(GOOD_SCRIPT)
    rejected_update = svc.update_definition(
        saved["definition"]["id"], source=sensitive, expected_revision=1
    )

    assert rejected_create["ok"] is False
    assert "sensitive data" in rejected_create["error"]
    assert rejected_update["ok"] is False
    assert library.get(saved["definition"]["id"])["revision"] == 1


async def test_promote_run_uses_raw_source_and_declared_lineage(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    parent = library.create(source=GOOD_SCRIPT, name="Debug Project")
    adapted = GOOD_SCRIPT.replace(
        '"description": "d"',
        f'"description": "d", "adapted_from": "{parent["id"]}@1"',
    )
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    started = await svc.start(adapted, name="Adapted")
    await _wait_terminal(svc, started["run_id"])

    promoted = await svc.promote_run_definition(
        started["run_id"],
        name="Saved adaptation",
        description="Kept from this completed run",
        slug="saved-adaptation",
    )

    assert promoted["ok"] is True
    assert promoted["definition"]["source"] == adapted
    assert promoted["definition"]["derived_from"] == {
        "workflow_id": parent["id"],
        "revision": 1,
    }


async def test_promote_paused_taskrunner_plan_saves_a_task_plan_definition(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    source = "agents:\n  test:\n    prompt: run tests\n    force_approval: true\n"
    run_id = await svc.begin_host_run(
        name="Test project",
        source=source,
        source_format="task-plan",
        task_id="task_123",
        driver="taskrunner",
    )
    await svc.pause(run_id)

    promoted = await svc.promote_run_definition(run_id, name="Test project")

    assert promoted["ok"] is True
    assert promoted["definition"]["format"] == "task-plan"
    assert promoted["definition"]["source"] == source


async def test_restored_paused_host_run_continues_its_event_sequence(tmp_path) -> None:
    store = WorkflowRunStore(tmp_path / "store")
    original = WorkflowService(sessions=FakeSessions([]), store=store)
    run_id = await original.begin_host_run(
        name="Restorable plan",
        source="agents:\n  test:\n    prompt: run tests\n",
        source_format="task-plan",
        task_id="task_123",
        driver="taskrunner",
    )
    await original.phase(run_id, "Planning")
    await original.pause(run_id)

    restored = WorkflowService(sessions=FakeSessions([]), store=store)
    task = asyncio.create_task(asyncio.sleep(0))
    try:
        assert await restored.rebind(run_id, task, task_id="task_123") is True
        await restored.phase(run_id, "Execution")
        await restored.finish(run_id, {"task_id": "task_123"})
    finally:
        await task

    snapshot = restored.result(run_id)
    assert snapshot["status"] == "finished"
    assert [event["seq"] for event in snapshot["events"]] == list(range(len(snapshot["events"])))
    assert snapshot["events"][-1]["type"] == "run_finished"


async def test_restored_running_host_run_reopens_with_the_same_identity(tmp_path) -> None:
    store = WorkflowRunStore(tmp_path / "store")
    original = WorkflowService(sessions=FakeSessions([]), store=store)
    run_id = await original.begin_host_run(
        name="Interrupted plan",
        source="agents:\n  test:\n    prompt: run tests\n",
        source_format="task-plan",
        task_id="task_123",
        driver="taskrunner",
    )

    restored = WorkflowService(sessions=FakeSessions([]), store=store)
    assert restored.status(run_id)["status"] == "failed"
    task = asyncio.create_task(asyncio.sleep(0))
    try:
        assert await restored.rebind(run_id, task, task_id="task_123") is True
    finally:
        await task

    assert restored.status(run_id)["status"] == "running"
    assert [run["run_id"] for run in restored.list_runs()] == [run_id]


async def test_retried_terminal_host_run_replaces_its_terminal_event(tmp_path) -> None:
    svc = WorkflowService(sessions=FakeSessions([]), persist=False)
    run_id = await svc.begin_host_run(
        name="Retried plan",
        source="agents:\n  test:\n    prompt: run tests\n",
        source_format="task-plan",
        task_id="task_123",
        driver="taskrunner",
    )
    await svc.phase(run_id, "Execution")
    await svc.fail(run_id, "interrupted")

    task = asyncio.create_task(asyncio.sleep(0))
    try:
        assert await svc.rebind(run_id, task, task_id="task_123") is True
        await svc.phase(run_id, "Execution")
        await svc.finish(run_id, {"task_id": "task_123"})
    finally:
        await task

    events = svc.result(run_id)["events"]
    terminal_types = {"run_finished", "run_failed", "run_cancelled"}
    assert [event["seq"] for event in events] == list(range(len(events)))
    assert [event["type"] for event in events if event["type"] in terminal_types] == [
        "run_finished"
    ]
    assert events[-1]["type"] == "run_finished"


async def test_host_step_result_summary_is_bounded(tmp_path) -> None:
    svc = WorkflowService(sessions=FakeSessions([]), persist=False)
    run_id = await svc.begin_host_run(
        name="Bounded plan",
        source_format="task-plan",
        task_id="task_123",
        driver="taskrunner",
    )

    await svc.step(run_id, 1, "Test", status="finished", result="x" * 1000)

    event = svc.result(run_id)["events"][-1]
    assert event["type"] == "agent_finished"
    assert event["data"]["result_summary"] == "x" * 120


async def test_host_source_persistence_runs_off_event_loop() -> None:
    store = BlockingSourceStore()
    svc = WorkflowService(sessions=FakeSessions([]), store=store)
    run_id = await svc.begin_host_run(
        name="Large plan",
        source_format="task-plan",
        task_id="task_123",
        driver="taskrunner",
    )
    loop_thread_id = threading.get_ident()
    source = "agents:\n  test:\n    prompt: " + "x" * (256 * 1024)

    update = asyncio.create_task(svc.set_source(run_id, source, source_format="task-plan"))
    try:
        assert await asyncio.to_thread(store.started.wait, 1)
        await asyncio.sleep(0)
        assert update.done() is False
        assert store.save_thread_id != loop_thread_id
    finally:
        store.release.set()

    assert await update is True
    assert svc.result(run_id)["source"] == source


async def test_host_registration_persistence_runs_off_event_loop() -> None:
    store = BlockingSourceStore()
    svc = WorkflowService(sessions=FakeSessions([]), store=store)
    loop_thread_id = threading.get_ident()
    source = "agents:\n  test:\n    prompt: " + "x" * (256 * 1024)

    registration = asyncio.create_task(
        svc.begin_host_run(
            name="Large saved plan",
            source=source,
            source_format="task-plan",
            task_id="task_456",
            driver="taskrunner",
        )
    )
    try:
        assert await asyncio.to_thread(store.started.wait, 1)
        await asyncio.sleep(0)
        assert registration.done() is False
        assert store.save_thread_id != loop_thread_id
    finally:
        store.release.set()

    run_id = await registration
    assert svc.result(run_id)["source"] == source


async def test_cancelled_host_registration_removes_the_partial_run() -> None:
    store = BlockingSourceStore()
    svc = WorkflowService(sessions=FakeSessions([]), store=store)
    source = "agents:\n  test:\n    prompt: " + "x" * (256 * 1024)
    registration = asyncio.create_task(
        svc.begin_host_run(
            name="Cancelled plan",
            source=source,
            source_format="task-plan",
            task_id="task_cancelled",
            driver="taskrunner",
        )
    )
    assert await asyncio.to_thread(store.started.wait, 1)

    registration.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await registration

    assert svc.list_runs() == []
    assert store.deleted == ["wf_000001"]


async def test_host_rebind_persistence_runs_off_event_loop() -> None:
    store = BlockingSourceStore(wait_timeout=0.1)
    store.release.set()
    svc = WorkflowService(sessions=FakeSessions([]), store=store)
    run_id = await svc.begin_host_run(
        name="Large resumable plan",
        source="agents:\n  test:\n    prompt: " + "x" * (256 * 1024),
        source_format="task-plan",
        task_id="task_rebind",
        driver="taskrunner",
    )
    store.started.clear()
    store.release.clear()
    store.save_thread_id = 0
    loop_thread_id = threading.get_ident()
    driver_task = asyncio.create_task(asyncio.sleep(10))

    checkpoint = asyncio.create_task(svc.rebind(run_id, driver_task, task_id="task_rebind"))
    try:
        assert await asyncio.to_thread(store.started.wait, 1)
        await asyncio.sleep(0)
        assert checkpoint.done() is False
        assert store.save_thread_id != loop_thread_id
    finally:
        store.release.set()
        driver_task.cancel()

    assert await checkpoint is True


async def test_promote_run_rejects_sensitive_raw_source(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    sensitive = GOOD_SCRIPT.replace("ctx.log('hi')", "ctx.log('AKIAIOSFODNN7EXAMPLE')")
    started = await svc.start(sensitive, name="Sensitive")
    await _wait_terminal(svc, started["run_id"])

    promoted = await svc.promote_run_definition(started["run_id"], name="Must not save")

    assert promoted["ok"] is False
    assert "sensitive data" in promoted["error"]
    assert library.list() == []


async def test_promote_run_rejects_redacted_source_restored_after_restart(tmp_path) -> None:
    store = WorkflowRunStore(tmp_path / "store")
    library = WorkflowDefinitionLibrary(tmp_path / "library")
    sensitive = GOOD_SCRIPT.replace("ctx.log('hi')", "ctx.log('AKIAIOSFODNN7EXAMPLE')")
    original = WorkflowService(sessions=FakeSessions([]), store=store, definition_library=library)
    started = await original.start(sensitive, name="Sensitive")
    await _wait_terminal(original, started["run_id"])

    restored = WorkflowService(sessions=FakeSessions([]), store=store, definition_library=library)
    promoted = await restored.promote_run_definition(
        started["run_id"], name="Must not save a redacted mutation"
    )

    assert promoted["ok"] is False
    assert promoted["source_not_original"] is True
    assert library.list() == []

    rerun = await restored.rerun_subtree(started["run_id"])
    await _wait_terminal(restored, rerun["run_id"])
    rerun_promoted = await restored.promote_run_definition(
        rerun["run_id"], name="Rerun must preserve provenance"
    )

    assert rerun_promoted["ok"] is False
    assert rerun_promoted["source_not_original"] is True
    assert library.list() == []


async def test_promote_run_accepts_exact_source_restored_after_restart(tmp_path) -> None:
    store = WorkflowRunStore(tmp_path / "store")
    library = WorkflowDefinitionLibrary(tmp_path / "library")
    original = WorkflowService(sessions=FakeSessions([]), store=store, definition_library=library)
    started = await original.start(GOOD_SCRIPT, name="Exact")
    await _wait_terminal(original, started["run_id"])

    restored = WorkflowService(sessions=FakeSessions([]), store=store, definition_library=library)
    promoted = await restored.promote_run_definition(started["run_id"], name="Still exact")

    assert promoted["ok"] is True
    assert promoted["definition"]["source"] == GOOD_SCRIPT


async def test_start_definition_runs_exact_saved_source_with_input(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    source = (
        'META = {"name": "echo", "description": "d"}\n'
        "async def workflow(ctx):\n"
        "    return {'input': ctx.args.get('input', '')}\n"
    )
    saved = library.create(source=source, name="Echo")
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)

    started = await svc.start_definition(saved["slug"], input_text="hello world")
    snap = await _wait_terminal(svc, started["run_id"])

    assert started["workflow_id"] == saved["id"]
    assert started["revision"] == 1
    assert snap["result"] == {"input": "hello world"}
    assert snap["workflow_id"] == saved["id"]
    assert snap["workflow_slug"] == saved["slug"]
    assert snap["workflow_revision"] == saved["revision"]

    promoted = await svc.promote_run_definition(started["run_id"], name="Duplicate")
    assert promoted["ok"] is False
    assert promoted["already_saved"] is True


async def test_unedited_saved_definition_rerun_preserves_provenance(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    source = (
        'META = {"name": "echo", "description": "d"}\n'
        "async def workflow(ctx):\n"
        "    return {'input': ctx.args.get('input', '')}\n"
    )
    saved = library.create(source=source, name="Echo")
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)
    started = await svc.start_definition(saved["slug"], input_text="hello world")
    await _wait_terminal(svc, started["run_id"])

    rerun = await svc.rerun_subtree(started["run_id"])
    snap = await _wait_terminal(svc, rerun["run_id"])

    assert snap["workflow_id"] == saved["id"]
    assert snap["workflow_slug"] == saved["slug"]
    assert snap["workflow_revision"] == saved["revision"]
    promoted = await svc.promote_run_definition(rerun["run_id"], name="Duplicate")
    assert promoted["ok"] is False
    assert promoted["already_saved"] is True


async def test_task_plan_definition_accepts_structured_input_arg(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    saved = library.create(
        source="agents:\n  test:\n    prompt: run tests\n",
        name="Test project",
        source_format="task-plan",
    )

    class FakeTaskRunner:
        async def start_workflow_definition(self, definition, **kwargs):
            assert definition == saved
            assert kwargs["input_text"] == "structured input"
            return {"task_id": "task_123", "run_id": "wf_task"}

    svc = WorkflowService(
        sessions=FakeSessions([]),
        persist=False,
        definition_library=library,
        task_runner=FakeTaskRunner(),
    )

    started = await svc.start_definition(saved["slug"], args={"input": "structured input"})
    assert started["run_id"] == "wf_task"


async def test_start_task_plan_definition_delegates_to_taskrunner_without_python_execution(
    tmp_path,
) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    saved = library.create(
        source="agents:\n  test:\n    prompt: run tests\n",
        name="Test project",
        source_format="task-plan",
    )

    class FakeTaskRunner:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def start_workflow_definition(self, definition, **kwargs):
            self.calls.append({"definition": definition, **kwargs})
            return {"task_id": "task_123", "run_id": "wf_task"}

    task_runner = FakeTaskRunner()
    svc = WorkflowService(
        sessions=FakeSessions([]),
        persist=False,
        definition_library=library,
        task_runner=task_runner,
    )

    started = await svc.start_definition(saved["slug"], input_text="from slash")

    assert started["run_id"] == "wf_task"
    assert started["task_id"] == "task_123"
    assert task_runner.calls == [
        {
            "definition": saved,
            "input_text": "from slash",
            "author": "",
            "session_key": "",
        }
    ]


async def test_start_definition_loads_saved_source_off_the_event_loop(
    monkeypatch, tmp_path
) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    saved = library.create(source=GOOD_SCRIPT, name="Demo")
    loop_thread = threading.get_ident()
    get_threads: list[int] = []
    real_get = library.get

    def observed_get(*args, **kwargs):
        get_threads.append(threading.get_ident())
        return real_get(*args, **kwargs)

    monkeypatch.setattr(library, "get", observed_get)
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)

    started = await svc.start_definition(saved["id"])
    await _wait_terminal(svc, started["run_id"])

    assert get_threads and get_threads[0] != loop_thread


# --------------------------------------------------------------------------- #
# start / status / result / on_done / cancel
# --------------------------------------------------------------------------- #


async def test_start_launches_run_and_injects_on_done(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["stub"])  # the workflow's ctx.agent uses this
    done: list[dict] = []
    svc = WorkflowService(
        sessions=FakeSessions([]),
        on_done=lambda rid, snap: done.append({"rid": rid, **snap}),
    )
    out = await svc.start(GOOD_SCRIPT, name="demo", session_key="slot:main")
    assert "run_id" in out
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "finished"
    assert snap["result"] == {"ok": True}
    # M6.4: on_done carried the originating session so the result routes to chat
    await asyncio.sleep(0.02)
    assert done and done[0]["session_key"] == "slot:main"


async def test_start_rejects_invalid_script() -> None:
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.start("import os\n")
    assert "error" in out and "run_id" not in out


async def test_result_and_list(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["stub"])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.start(GOOD_SCRIPT, name="demo")
    rid = out["run_id"]
    await _wait_terminal(svc, rid)
    full = svc.result(rid)
    assert full["run_id"] == rid and "events" in full
    runs = svc.list_runs()
    assert any(r["run_id"] == rid for r in runs)


async def test_run_ids_are_deterministic_monotonic() -> None:
    svc = WorkflowService(sessions=FakeSessions([]))
    a = svc._new_run_id()
    b = svc._new_run_id()
    assert a == "wf_000001" and b == "wf_000002"


async def test_concurrency_cap_flows_to_runner() -> None:
    """The service's concurrency cap must reach the runner (and thus bound
    parallel/pipeline fan-out) — without it, a fan-out workflow runs every agent
    at once and can overload the box."""
    svc = WorkflowService(sessions=FakeSessions([]), concurrency=5)
    runner = svc._runner("wf_x")
    assert runner._concurrency == 5


async def test_host_run_uses_the_shared_history_without_chat_completion_injection() -> None:
    done: list[dict] = []
    svc = WorkflowService(
        sessions=FakeSessions([]),
        persist=False,
        on_done=lambda rid, snap: done.append({"rid": rid, **snap}),
    )

    run_id = await svc.begin_host_run(
        name="Ship release",
        source="agents:\n  test:\n    prompt: run tests\n",
        source_format="task-plan",
        task_id="task_123",
        driver="taskrunner",
        capabilities=("pause", "cancel", "retry", "save"),
    )
    driver_task = asyncio.create_task(asyncio.sleep(0))
    await svc.bind_task(run_id, driver_task, task_id="task_123")
    await svc.phase(run_id, "Execution")
    await svc.step(run_id, 1, "Run tests", status="running")
    await svc.log(run_id, "Running the existing TaskRunner executor")
    await svc.step(run_id, 1, "Run tests", status="finished", result="passed")
    await svc.finish(run_id, {"task_id": "task_123", "status": "completed"})
    await driver_task

    snap = svc.result(run_id)
    assert snap is not None
    assert snap["status"] == "finished"
    assert snap["source_format"] == "task-plan"
    assert snap["driver"] == "taskrunner"
    assert snap["task_id"] == "task_123"
    assert snap["capabilities"] == ["pause", "cancel", "retry", "save"]
    assert [event["type"] for event in snap["events"]] == [
        "run_started",
        "phase_started",
        "agent_started",
        "log",
        "agent_finished",
        "run_finished",
    ]
    assert done == []


async def test_host_run_can_pause_and_rebind_the_same_run() -> None:
    svc = WorkflowService(sessions=FakeSessions([]), persist=False)
    run_id = await svc.begin_host_run(
        name="Reusable task plan",
        source_format="task-plan",
        task_id="task_456",
        driver="taskrunner",
    )
    first_task = asyncio.create_task(asyncio.sleep(0))
    await svc.bind_task(run_id, first_task, task_id="task_456")
    await svc.pause(run_id)
    await first_task

    assert svc.status(run_id)["status"] == "paused"

    resumed_task = asyncio.create_task(asyncio.sleep(0))
    await svc.rebind(run_id, resumed_task, task_id="task_456")
    await resumed_task

    assert svc.status(run_id)["status"] == "running"
    assert svc.registry.get(run_id).task is resumed_task


# --------------------------------------------------------------------------- #
# M6.7 — start_from_intent: author INSIDE the run (no synchronous-author block)
# --------------------------------------------------------------------------- #


async def test_start_from_intent_returns_run_id_then_authors_and_runs(monkeypatch) -> None:
    """workflow_run(intent=…) path: returns a run_id immediately, then the run
    authors its own script (visible Authoring phase) and executes to completion."""
    _patch_stream(monkeypatch, [GOOD_SCRIPT])  # authoring reply
    events: list[dict] = []
    svc = WorkflowService(
        sessions=FakeSessions([]),
        on_event=lambda rid, ev: events.append(ev),
    )
    out = await svc.start_from_intent("do a tiny thing", session_key="slot:main")
    # run_id is returned right away — authoring has NOT blocked this call.
    assert "run_id" in out
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "finished"
    assert snap["result"] == {"ok": True}
    # The stream shows an Authoring phase before the workflow body.
    titles = [e["data"]["title"] for e in events if e["type"] == "phase_started"]
    assert "Authoring" in titles
    # The authored source is persisted on the handle (for rerun/restart).
    h = svc.registry.get(out["run_id"])
    assert h is not None and "async def workflow" in h.source


async def test_start_from_intent_authoring_failure_is_failed_run(monkeypatch) -> None:
    """If the model never yields a valid script, the run ends 'failed' (not a
    crash, not a hang) with the authoring errors recorded."""
    _patch_stream(monkeypatch, ["import os\nasync def workflow(ctx):\n    return 1\n"])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.start_from_intent("nonsense")
    assert "run_id" in out
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "failed"


async def test_start_from_intent_requires_intent() -> None:
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.start_from_intent("   ")
    assert "error" in out and "run_id" not in out


# --------------------------------------------------------------------------- #
# View source + edit-and-rerun (FIX-11): snapshot exposes source; rerun_subtree
# can run an edited script (validated, fresh — no stale prefix replay).
# --------------------------------------------------------------------------- #


async def test_full_snapshot_exposes_source(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["stub"])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.start(GOOD_SCRIPT, name="demo")
    rid = out["run_id"]
    await _wait_terminal(svc, rid)
    full = svc.result(rid)  # full snapshot (include_events=True)
    assert "async def workflow" in full["source"]
    # compact list view stays light — no source there
    assert "source" not in svc.list_runs()[0]


async def test_rerun_with_edited_source_runs_fresh(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["stub"])
    svc = WorkflowService(sessions=FakeSessions([]))
    rid = (await svc.start(GOOD_SCRIPT, name="demo"))["run_id"]
    await _wait_terminal(svc, rid)
    edited = (
        'META = {"name": "demo2", "description": "edited"}\n'
        "async def workflow(ctx):\n"
        "    ctx.log('edited run')\n"
        "    return {'edited': True}\n"
    )
    out = await svc.rerun_subtree(rid, 0, source=edited)
    assert out.get("edited") is True and out["replayed_before"] == 0
    new_rid = out["run_id"]
    snap = await _wait_terminal(svc, new_rid)
    assert snap["status"] == "finished" and snap["result"] == {"edited": True}


async def test_rerun_with_invalid_edited_source_rejected(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["stub"])
    svc = WorkflowService(sessions=FakeSessions([]))
    rid = (await svc.start(GOOD_SCRIPT, name="demo"))["run_id"]
    await _wait_terminal(svc, rid)
    out = await svc.rerun_subtree(rid, 0, source="import os\n")
    assert "errors" in out and "run_id" not in out


# --------------------------------------------------------------------------- #
# Integration: a finished chat-linked run must (1) inject its result into the
# originating slot AND (2) auto-run an agent turn so the launching agent actually
# interprets the result. Drives the REAL WorkflowService -> runner -> on_done ->
# inject_workflow_result(on_injected=...) wiring; only _run_chat is stubbed (no
# model). Regression for "workflow result never reaches the agent to interpret".
# --------------------------------------------------------------------------- #


class _IntgSlot:
    """Slot double exposing the real enqueue-or-run contract used by the auto-turn."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.messages: list[dict] = []
        self.linked_session_key = ""
        self.title = ""
        self.running = False
        self.turns: list[str] = []  # prompts that started an agent turn

    def append(self, role, content, cls="", ts="", *, broadcast=True, meta=None):
        # Mirror the real ``_ChatSlot.append`` contract: the injector reads the
        # appended row off the return value.
        msg = {"role": role, "content": content}
        self.messages.append(msg)
        return msg

    def enqueue_or_run_prompt(self, prompt, run_chat_coro, state) -> bool:
        # Mirror the real state.py primitive: busy -> queue (False), else run (True).
        if self.running:
            return False
        self.append("user", prompt, "msg msg-u")
        self.turns.append(prompt)
        return True


class _IntgState:
    def __init__(self, slots) -> None:
        self._slots = dict(slots)
        self.conversation_log = None
        self.broadcasts: list = []
        self.slots_pushed = 0

    def get_slot(self, name):
        return self._slots.get(name)

    def get_or_create_slot(self, name, **kw):
        self._slots.setdefault(name, _IntgSlot(name))
        return self._slots[name]

    def broadcast_ws(self, kind, payload):
        self.broadcasts.append((kind, payload))

    def push_slots_update(self):
        self.slots_pushed += 1


async def test_finished_run_injects_result_and_autoruns_agent_turn(monkeypatch) -> None:
    from kiro_crew.dashboard.workflow_inject import inject_workflow_result

    _patch_stream(monkeypatch, ["stub"])
    origin = _IntgSlot("chat-1")
    dstate = _IntgState({"chat-1": origin})

    # Reproduce the gateway's _wf_on_done: inject, and on a fresh originating
    # inject, start an agent turn via the slot's enqueue-or-run primitive.
    def _auto_turn(slot, snap):
        prompt = f"[Workflow `{snap.get('name')}` finished] interpret the result above."
        slot.enqueue_or_run_prompt(prompt, lambda s, sl, m: None, dstate)
        dstate.push_slots_update()

    def _on_done(rid, snap):
        inject_workflow_result(dstate, rid, snap, on_injected=_auto_turn)

    svc = WorkflowService(sessions=FakeSessions([]), on_done=_on_done)
    out = await svc.start(GOOD_SCRIPT, name="demo", session_key="dashboard:chat-1")
    await _wait_terminal(svc, out["run_id"])
    await asyncio.sleep(0.05)  # let on_done fire

    # (1) result summary injected as an assistant message into the ORIGINATING slot
    assert any(m["role"] == "assistant" and "demo" in m["content"] for m in origin.messages)
    # (2) an agent turn was auto-started with a user-role prompt to interpret it
    assert len(origin.turns) == 1, origin.turns
    assert any(m["role"] == "user" and "interpret" in m["content"] for m in origin.messages)
    assert dstate.slots_pushed >= 1


async def test_finished_run_busy_slot_queues_turn(monkeypatch) -> None:
    """If the originating slot is mid-turn, the auto-turn queues (does not start),
    so we never stack a concurrent turn — mirrors enqueue_or_run_prompt semantics."""
    from kiro_crew.dashboard.workflow_inject import inject_workflow_result

    _patch_stream(monkeypatch, ["stub"])
    origin = _IntgSlot("chat-1")
    origin.running = True  # busy
    dstate = _IntgState({"chat-1": origin})
    started: list[bool] = []

    def _auto_turn(slot, snap):
        started.append(slot.enqueue_or_run_prompt("interpret", lambda s, sl, m: None, dstate))

    svc = WorkflowService(
        sessions=FakeSessions([]),
        on_done=lambda rid, snap: inject_workflow_result(dstate, rid, snap, on_injected=_auto_turn),
    )
    out = await svc.start(GOOD_SCRIPT, name="demo", session_key="dashboard:chat-1")
    await _wait_terminal(svc, out["run_id"])
    await asyncio.sleep(0.05)
    # Result still injected, but the turn was QUEUED (False), not started.
    assert any(m["role"] == "assistant" for m in origin.messages)
    assert started == [False]
    assert origin.turns == []


# --------------------------------------------------------------------------- #
# Warm-session pool wiring (loading-time win) — reachable in production?
# --------------------------------------------------------------------------- #


async def test_default_service_pools_agents() -> None:
    """The gateway constructs WorkflowService WITHOUT passing pool_agents
    (dashboard/server.py), so the pool must be ON by default. A pooled runner
    has an on_complete teardown (== pool.shutdown) wired; an un-pooled one does
    not. This guards the live wiring so the loading-time win can't silently
    regress to cold-start-per-call."""
    svc = WorkflowService(sessions=FakeSessions([]), concurrency=4)
    assert svc._pool_agents is True  # default engages in production
    runner = svc._runner("wf_probe")
    # on_complete is only set on the pooled path (service._runner wires pool.shutdown).
    assert runner._on_complete is not None


async def test_pool_agents_false_uses_per_call_sessions() -> None:
    """Opt-out restores the per-call agent path. Every runner still carries an
    ``on_complete`` teardown (it drains the run's ctx.nudge arms before the
    terminal transition) — pool_agents only controls the warm-pool wiring."""
    svc = WorkflowService(sessions=FakeSessions([]), pool_agents=False)
    assert svc._pool_agents is False
    runner = svc._runner("wf_probe")
    # Nudge-drain teardown is wired even without a pool; it must be awaitable
    # and a no-op when the run armed no nudges.
    assert runner._on_complete is not None
    await runner._on_complete()  # no nudge tasks → returns immediately
