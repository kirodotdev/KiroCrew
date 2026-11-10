"""An unattended run waits for its agent spec's MCP servers, and names the missing ones.

The session's own init drain stops about a second after the first server reports,
so a slow server (a ``node``-launched one, say) can still be booting when the first
prompt goes out, and that turn runs without its tools. In chat a person notices and
retries; in a webhook, cron, workflow or heartbeat run nobody does. So an unattended
run holds its first prompt, bounded, until every server the agent spec declares has
reported, and a run that still lacks one says which, with a distinct webhook outcome.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _bounded, _make_runtime, _register

from kiro_crew import webhooks
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    EVENT_COMPLETE,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_TEXT_CHUNK,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk import mcp_problems
from kiro_crew.dashboard.handlers import hooks as H
from kiro_crew.testing.wait import await_until

_SPEC = {"mcpServers": {"atlassian": {"url": "x"}, "azure-devops": {"command": "node"}}}


def _initialized(session_id: str, server: str) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/mcp/server_initialized",
            "params": {"sessionId": session_id, "serverName": server},
        }
    )


# ── The report: what the spec declares ──────────────────────────────────────


def test_declared_servers_skip_disabled_ones_and_survive_until_the_next_session():
    report = McpSessionReport()
    report.begin_session([])
    report.record_declared(
        {"mcpServers": {"a": {}, "off": {"disabled": True}, "b": {"disabled": False}}}
    )

    assert report.declared == ("a", "b")
    assert report.declared_unreported() == ("a", "b")

    report.record_event(EVENT_MCP_SERVER_INITIALIZED, "a")
    report.record_event(EVENT_MCP_SERVER_INIT_FAILURE, "b", "boom")
    # Failed counts as reported (nothing more to wait for), not as ready.
    assert report.declared_unreported() == ()
    assert report.declared_not_ready() == ("b",)

    report.begin_session([])
    assert report.declared == ()


def test_servers_past_the_cap_are_counted_and_never_read_as_ready():
    from kiro_crew.acp.mcp_session_report import BUCKET_CAP

    report = McpSessionReport()
    report.begin_session([])
    names = [f"s{i}" for i in range(BUCKET_CAP + 1)]
    report.record_declared({"mcpServers": {name: {} for name in names}})
    for name in names[:BUCKET_CAP]:
        report.record_event(EVENT_MCP_SERVER_INITIALIZED, name)

    assert report.declared_omitted == 1
    assert report.declared_not_ready() == ("1 more declared server(s), not tracked",)

    report.begin_session([])
    assert report.declared_omitted == 0


def test_a_server_muted_with_a_non_boolean_disabled_is_not_waited_for():
    """The launch reads any ``disabled`` but absent or ``false`` as muted."""
    report = McpSessionReport()
    report.begin_session([])
    report.record_declared({"mcpServers": {"odd": {"disabled": "yes"}, "on": {"disabled": False}}})

    assert report.declared == ("on",)


def test_a_spec_with_no_server_mapping_declares_nothing():
    report = McpSessionReport()
    for spec in (None, {}, {"mcpServers": None}, {"mcpServers": ["a"]}):
        report.record_declared(spec)
        assert report.declared == ()


def test_the_runtime_records_the_spec_servers_on_a_host_that_reports_them():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.mcp_session_report().begin_session([])

    rt._guard_unresolved_mcp_refs(handle, _SPEC, "kirocrew", [])

    assert handle.mcp_session_report().declared == ("atlassian", "azure-devops")


def test_the_kas_runtime_declares_the_spec_servers_its_wire_array_carries():
    """KAS reports each wire server in ``_kiro/mcp/status``, so a cron waits for
    them; a spec server Crew held back never reports and is not waited for."""
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.types import ACP_BACKEND_KAS

    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_KAS)
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.mcp_session_report().begin_session([])

    rt._guard_unresolved_mcp_refs(handle, _SPEC, "kirocrew", [{"name": "azure-devops"}])

    assert handle.mcp_session_report().declared == ("azure-devops",)


def test_the_kas_runtime_declares_the_spec_servers_its_active_custom_agent_keeps():
    """A third-party server stays in the active agent's ``mcpServers`` on
    session/new rather than the wire array; KAS starts and reports it, so a cron
    waits for it too. Another agent's block delivers nothing to this session."""
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.types import ACP_BACKEND_KAS

    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_KAS)
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.mcp_session_report().begin_session([])
    handle.kas_projected_agent = "kirocrew"
    handle.kas_registered_agents = [
        {"id": "other", "mcpServers": {"azure-devops": {}}},
        {"id": "kirocrew", "mcpServers": {"atlassian": {"url": "x"}}},
    ]

    rt._guard_unresolved_mcp_refs(handle, _SPEC, "kirocrew", [])

    assert handle.mcp_session_report().declared == ("atlassian",)


def test_the_runtime_records_nothing_when_it_expects_no_mcp_reports():
    """No frame would ever come, so a wait would only burn the bound."""
    rt, _, _ = _make_runtime()
    rt._expect_mcp_reports = False
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.mcp_session_report().begin_session([])

    rt._guard_unresolved_mcp_refs(handle, _SPEC, "kirocrew", [])

    assert handle.mcp_session_report().declared == ()


def test_the_client_records_the_spec_servers_only_on_a_reporting_host(tmp_path):
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

    kiro = AcpClient(work_dir=tmp_path)
    kiro._mcp_ref_spec = _SPEC
    kiro._mcp_report.begin_session([])
    kiro._guard_unresolved_mcp_refs([])
    assert kiro._mcp_report.declared == ("atlassian", "azure-devops")

    claude = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    claude._mcp_ref_spec = _SPEC
    claude._mcp_report.begin_session([])
    claude._guard_unresolved_mcp_refs([])
    assert claude._mcp_report.declared == ()


# ── Servers the spec mounts from mcp.json ───────────────────────────────────

#: A spec that defines no server of its own and opts in to the user's mcp.json.
_OPTED_IN = {"includeMcpJson": True, "mcpServers": {}}


def _global_mcp_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: dict) -> None:
    """A kiro home of the test's own whose ``settings/mcp.json`` holds *servers*."""
    home = tmp_path / "kiro-home"
    (home / "settings").mkdir(parents=True)
    (home / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")


def test_the_report_also_waits_for_the_servers_named_from_mcp_json():
    report = McpSessionReport()
    report.begin_session([])
    report.record_declared({"mcpServers": {"own": {}}}, mcp_json=("slow", "own"))

    assert report.declared == ("own", "slow")
    report.record_event(EVENT_MCP_SERVER_INITIALIZED, "own")
    assert report.declared_unreported() == ("slow",)


def test_the_client_waits_for_a_server_only_the_global_mcp_json_defines(tmp_path, monkeypatch):
    _global_mcp_json(tmp_path, monkeypatch, {"slow": {"command": "npx"}})
    monkeypatch.setattr(client_mod, "agent_spec_snapshot", lambda _a, **_k: dict(_OPTED_IN))
    client = AcpClient(work_dir=tmp_path / "work", agent="kirocrew")

    client._prepare_spawn_workspace()
    client._mcp_report.begin_session([])
    client._guard_unresolved_mcp_refs([])

    assert client._mcp_report.declared_unreported() == ("slow",)
    client._mcp_report.record_event(EVENT_MCP_SERVER_INITIALIZED, "slow")
    assert client._mcp_report.declared_unreported() == ()


def test_the_runtime_waits_for_a_server_only_the_global_mcp_json_defines(tmp_path, monkeypatch):
    _global_mcp_json(tmp_path, monkeypatch, {"slow": {"command": "npx"}})
    monkeypatch.setattr(runtime_mod, "agent_spec_snapshot", lambda _a, **_k: dict(_OPTED_IN))
    monkeypatch.setattr(runtime_mod, "pooled_session_servers", lambda *_a, **_k: [])
    servers, spec, mcp_json = runtime_mod._pooled_session_servers_and_ref_spec(
        None, "kirocrew", ACP_BACKEND_KIRO, tmp_path / "work"
    )
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.mcp_session_report().begin_session(servers)

    rt._guard_unresolved_mcp_refs(handle, spec, "kirocrew", servers, mcp_json)

    assert handle.mcp_session_report().declared_unreported() == ("slow",)


def test_without_the_opt_in_the_global_mcp_json_is_not_waited_for(tmp_path, monkeypatch):
    _global_mcp_json(tmp_path, monkeypatch, {"slow": {"command": "npx"}})
    monkeypatch.setattr(
        client_mod,
        "agent_spec_snapshot",
        lambda _a, **_k: {**_OPTED_IN, "includeMcpJson": False},
    )
    client = AcpClient(work_dir=tmp_path / "work", agent="kirocrew")

    client._prepare_spawn_workspace()
    client._mcp_report.begin_session([])
    client._guard_unresolved_mcp_refs([])

    assert client._mcp_report.declared == ()


# ── The shared-runtime session handle ───────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_holds_the_first_prompt_for_a_slow_declared_server():
    """drain_init gives up on the slow server; the unattended wait does not."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    report = handle.mcp_session_report()
    report.begin_session([])
    report.record_declared(_SPEC)

    q["sA"].put_nowait(_initialized("sA", "atlassian"))
    await handle.drain_init(duration=0.2, idle_exit=0.01, no_report_ceiling=0.05)
    # The defect: the session would now prompt without the slow server.
    assert report.declared_unreported() == ("azure-devops",)

    waiter = asyncio.ensure_future(handle.wait_for_declared_mcp(60.0))
    q["sA"].put_nowait(_initialized("sA", "azure-devops"))
    missing = await await_until(waiter, what="the wait to end once the server reported")

    assert missing == ()
    assert report.declared_unreported() == ()


@pytest.mark.asyncio
async def test_handle_declared_wait_counts_an_owed_compaction_terminal():
    from kiro_crew.acp import stale_compaction
    from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    report = handle.mcp_session_report()
    report.begin_session([])
    report.record_declared({"mcpServers": {"atlassian": {}}})
    stale_compaction.discard_stale_compaction_status(handle, within_secs=0.0)
    q["sA"].put_nowait(
        JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "failed"}},
        )
    )
    q["sA"].put_nowait(_initialized("sA", "atlassian"))

    missing = await handle.wait_for_declared_mcp(1.0)

    assert missing == ()
    assert handle._unresolved_compaction_statuses is None
    assert handle._compaction_failed_at is None


@pytest.mark.asyncio
async def test_handle_wait_is_bounded_and_names_the_server_that_never_reported():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    report = handle.mcp_session_report()
    report.begin_session([])
    report.record_declared(_SPEC)
    q["sA"].put_nowait(_initialized("sA", "atlassian"))

    missing = await _bounded(handle.wait_for_declared_mcp(0.05), what="the bounded wait")

    assert missing == ("azure-devops",)


@pytest.mark.asyncio
async def test_handle_wait_returns_at_once_when_nothing_is_declared():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.mcp_session_report().begin_session([])

    # A long bound that the wait must not spend: nothing is outstanding.
    missing = await _bounded(handle.wait_for_declared_mcp(3600.0), what="an empty wait")

    assert missing == ()


@pytest.mark.asyncio
async def test_handle_wait_repoisons_a_dead_runtime():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.mcp_session_report().begin_session([])
    handle.mcp_session_report().record_declared(_SPEC)
    q["sA"].put_nowait(None)

    missing = await _bounded(handle.wait_for_declared_mcp(3600.0), what="a dead runtime")

    assert missing == ("atlassian", "azure-devops")
    assert q["sA"].get_nowait() is None, "the next consumer must still see the death"


# ── The dedicated-transport client ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_keeps_draining_past_the_idle_window_for_a_declared_server(tmp_path):
    client = AcpClient(work_dir=tmp_path)
    client._mcp_notifications = []
    client._mcp_report.begin_session([])
    client._mcp_report.record_declared({"mcpServers": {"azure-devops": {}}})
    reads = {"n": 0}

    async def quiet_then_report(timeout=2.0):
        # Several empty reads first: a booting server is quiet, which must not
        # end the wait the way it ends the ordinary drain.
        reads["n"] += 1
        return _initialized("s", "azure-devops") if reads["n"] == 4 else None

    client._read_message = quiet_then_report

    missing = await _bounded(client.wait_for_declared_mcp(3600.0), what="the client wait")

    assert missing == ()
    assert reads["n"] == 4, "the wait should end on the report, not read past it"


@pytest.mark.asyncio
async def test_client_wait_is_bounded(tmp_path):
    client = AcpClient(work_dir=tmp_path)
    client._mcp_notifications = []
    client._mcp_report.begin_session([])
    client._mcp_report.record_declared({"mcpServers": {"azure-devops": {}}})
    client._read_message = AsyncMock(return_value=None)

    missing = await _bounded(client.wait_for_declared_mcp(0.05), what="the bounded client wait")

    assert missing == ("azure-devops",)


# ── A webhook run ───────────────────────────────────────────────────────────

_SESSION_KEY = f"{H._HOOK_SESSION_PREFIX}mcp-wait"


class _FakeClient:
    """A session whose spec declares two servers; one reports only if waited for."""

    def __init__(self, *, slow_reports: bool) -> None:
        self._report = McpSessionReport()
        self._report.begin_session([])
        self._report.record_declared(_SPEC)
        self._report.record_event(EVENT_MCP_SERVER_INITIALIZED, "atlassian")
        self._slow_reports = slow_reports
        self.waits: list[float] = []
        self.prompted_with_tools: bool | None = None
        self._agent = "kirocrew"
        self._model = "test-model"

    def mcp_session_report(self) -> McpSessionReport:
        return self._report

    async def wait_for_declared_mcp(self, timeout: float) -> tuple[str, ...]:
        self.waits.append(timeout)
        if self._slow_reports:
            self._report.record_event(EVENT_MCP_SERVER_INITIALIZED, "azure-devops")
        return self._report.declared_not_ready()

    async def stream(self, _message: str):
        self.prompted_with_tools = not self._report.declared_not_ready()
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="the agent answer")
        yield AcpEvent(kind=EVENT_COMPLETE)


def _state(client: _FakeClient, *, is_new: bool = True) -> MagicMock:
    state = MagicMock(memory_startup_task=None)
    state.context_builder = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, is_new, False))
    state.sessions.record_success = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.owner_id = None
    state.slack_client = None
    state.notify = MagicMock()
    return state


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(webhooks, "config_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(H, "_HOOK_STORE_PATH", Path(tmp_path) / "hooks.json")
    monkeypatch.setattr(H, "_sel", lambda: MagicMock())
    from kiro_crew.dashboard.handlers import usage

    monkeypatch.setattr(usage, "_write_token_record", lambda *_a, **_k: None)
    H._reset_hook_inflight()
    yield
    H._reset_hook_inflight()


async def _run(state: MagicMock) -> dict:
    await H._hook_semaphore.acquire()
    await H._run_hook_agent(state, _SESSION_KEY, "hi", "Bot", None, True, 600)
    return webhooks.run_store().list_runs()[0]


@pytest.mark.asyncio
async def test_a_webhook_run_waits_for_its_slow_server_before_prompting(wired):
    client = _FakeClient(slow_reports=True)
    row = await _run(_state(client))

    assert client.waits == [mcp_problems.UNATTENDED_MCP_WAIT_SECS]
    assert client.prompted_with_tools is True
    assert row["outcome"] == webhooks.OUTCOME_COMPLETED
    assert row["detail"] == "Delivered to notifications"


@pytest.mark.asyncio
async def test_a_webhook_run_still_missing_a_server_is_degraded_and_names_it(wired):
    state = _state(_FakeClient(slow_reports=False))
    row = await _run(state)

    assert row["outcome"] == webhooks.OUTCOME_DEGRADED
    assert row["missing_servers"] == ["azure-devops"]
    assert "azure-devops" in row["detail"]
    assert "atlassian" not in row["detail"]
    assert "Delivered to notifications" in row["detail"]
    assert "azure-devops" in state.notify.call_args.args[2]


@pytest.mark.asyncio
async def test_a_reused_session_does_not_wait_again(wired):
    client = _FakeClient(slow_reports=True)
    await _run(_state(client, is_new=False))

    assert client.waits == []


@pytest.mark.asyncio
async def test_a_failed_declared_server_is_named_once_in_the_run_detail(wired):
    """The failure line already names it with its reason; the notice does not repeat it."""
    client = _FakeClient(slow_reports=False)
    client._report.record_event(EVENT_MCP_SERVER_INIT_FAILURE, "azure-devops", "keytar missing")
    row = await _run(_state(client))

    assert row["outcome"] == webhooks.OUTCOME_DEGRADED
    assert row["detail"].count("azure-devops") == 1
    assert "keytar missing" in row["detail"]


def test_degraded_is_a_recordable_outcome():
    assert webhooks.OUTCOME_DEGRADED in webhooks.VALID_OUTCOMES


# ── A cron run ──────────────────────────────────────────────────────────────


def _cron_gateway(client: _FakeClient):
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
    gw.slack.post_blocks = AsyncMock(return_value="ts1")
    gw.conv_log = None
    gw.dashboard_state = MagicMock()
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._cron_runs_holding = set()
    gw._no_crons = False
    gw.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.cancel_current = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


def _run_cron(client: _FakeClient, *, sequence: list[str] | None = None):
    from kiro_crew.cron import CronJob, CronSchedule

    job = CronJob(
        id="j1",
        name="poller",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=300),
        approval_mode="auto",
        channel="C123",
        agent_sequence=sequence or [],
    )
    gw = _cron_gateway(client)

    calls = {"n": 0}

    async def fake_stream(provider, msg, **kwargs):
        calls["n"] += 1
        if sequence and calls["n"] == 2:
            # The server is up by the second step: only step 1 lacked it.
            provider.mcp_session_report().record_event(EVENT_MCP_SERVER_INITIALIZED, "azure-devops")
        provider.prompted_with_tools = not provider.mcp_session_report().declared_not_ready()
        return "the agent answer" if calls["n"] == 1 or not sequence else "the second answer"

    captured_cb = None
    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", fake_stream),
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            await captured_cb(job)

        asyncio.run(_init_and_run())
    return job


def test_a_cron_run_waits_for_its_slow_server_before_prompting():
    client = _FakeClient(slow_reports=True)
    job = _run_cron(client)

    assert client.waits == [mcp_problems.UNATTENDED_MCP_WAIT_SECS]
    assert client.prompted_with_tools is True
    assert job.last_result == "the agent answer"


def test_a_cron_run_still_missing_a_server_names_it_in_the_result():
    job = _run_cron(_FakeClient(slow_reports=False))

    assert "azure-devops" in job.last_result
    assert job.last_result.endswith("the agent answer")


def test_a_sequence_cron_keeps_an_early_steps_missing_server_note():
    """The next step's reply replaces result_text; the stored result keeps the note."""
    job = _run_cron(_FakeClient(slow_reports=False), sequence=["kirocrew", "kirocrew"])

    assert "azure-devops" in job.last_result
    assert job.last_result.endswith("the second answer")


# ── A workflow agent step ───────────────────────────────────────────────────


class _WorkflowClient(_FakeClient):
    """A step session; a reuse's fresh conversation starts its servers again."""

    def __init__(self) -> None:
        super().__init__(slow_reports=True)

    async def new_conversation(self) -> None:
        # A fresh session/new: the slow server has not reported to it yet.
        self.__init__()

    def is_process_alive(self) -> bool:
        return True

    def set_delete_delay(self, _seconds: float) -> None:
        return None


class _WorkflowSessions:
    def __init__(self, client: _WorkflowClient) -> None:
        self.client = client
        self.seen: set[str] = set()

    async def get_or_create(self, key, **_kw):
        is_new = key not in self.seen
        self.seen.add(key)
        return self.client, is_new, False

    async def run_identity_check(self):
        return None

    def is_retiring(self, key):
        return False

    def release(self, key, *, cleanup=False):
        return None

    async def destroy(self, key):
        return None


@pytest.fixture()
def workflow_stream(monkeypatch):
    """Each prompt's view: the waits its session had made by then."""
    waits: list[list[float]] = []

    async def fake_stream(provider, _prompt, **_kw):
        waits.append(list(provider.waits))
        return "the agent answer"

    monkeypatch.setattr("kiro_crew.workflows.agent_exec.stream_and_collect", fake_stream)
    monkeypatch.setattr("kiro_crew.workflows.agent_pool.stream_and_collect", fake_stream)
    return waits


@pytest.mark.asyncio
async def test_a_workflow_step_waits_on_a_new_session_only(workflow_stream):
    from kiro_crew.workflows.agent_exec import build_agent_fn

    client = _WorkflowClient()
    agent_fn = build_agent_fn(_WorkflowSessions(client), run_id="wf1")
    await agent_fn("first", {"session": "chain"})
    await agent_fn("second", {"session": "chain"})

    assert workflow_stream[0] == [mcp_problems.UNATTENDED_MCP_WAIT_SECS]
    assert client.waits == [mcp_problems.UNATTENDED_MCP_WAIT_SECS]


@pytest.mark.asyncio
async def test_a_pooled_workflow_step_waits_on_each_fresh_conversation(workflow_stream):
    from kiro_crew.workflows.agent_pool import build_pooled_agent_fn

    client = _WorkflowClient()
    agent_fn, pool = build_pooled_agent_fn(
        _WorkflowSessions(client), run_id="wf2", max_workers=1, max_starting=1
    )
    try:
        await agent_fn("first", {})
        await agent_fn("second", {})
    finally:
        await pool.shutdown()

    assert workflow_stream == [[mcp_problems.UNATTENDED_MCP_WAIT_SECS]] * 2


@pytest.mark.asyncio
async def test_an_unpooled_workflow_chain_waits_on_its_new_session_only(workflow_stream):
    from kiro_crew.workflows.agent_pool import build_pooled_agent_fn

    client = _WorkflowClient()
    agent_fn, pool = build_pooled_agent_fn(_WorkflowSessions(client), run_id="wf3")
    try:
        await agent_fn("first", {"session": "chain"})
        await agent_fn("second", {"session": "chain"})
    finally:
        await pool.shutdown()

    assert workflow_stream[0] == [mcp_problems.UNATTENDED_MCP_WAIT_SECS]
    assert client.waits == [mcp_problems.UNATTENDED_MCP_WAIT_SECS]


# ── A heartbeat turn ────────────────────────────────────────────────────────


async def _heartbeat_result(client: _FakeClient, monkeypatch, *, is_new: bool = True) -> str:
    import kiro_crew.slack.gateway as gw_mod

    orch = gw_mod.GatewayOrchestrator.__new__(gw_mod.GatewayOrchestrator)
    orch.sessions = MagicMock()
    orch.sessions.get_or_create = AsyncMock(return_value=(client, is_new, False))
    orch.ctx_builder = MagicMock(conversation_log=None)
    orch.ctx_builder.build_message = MagicMock(return_value=("full message", None))
    orch.consolidator = None
    orch._heartbeat_approval = AsyncMock(return_value=True)
    orch._deliver_result = AsyncMock()

    async def fake_stream(provider, _message, **_kw):
        provider.prompted_with_tools = not provider.mcp_session_report().declared_not_ready()
        return "the agent answer"

    started = {}

    async def fake_start(svc):
        started["on_task"] = svc._on_task

    monkeypatch.setattr(gw_mod, "stream_and_collect", fake_stream)
    monkeypatch.setattr(gw_mod, "persist_token_record_async", AsyncMock())
    monkeypatch.setattr(gw_mod.HeartbeatService, "start", fake_start)
    await orch._init_heartbeat()
    return await started["on_task"]("check the queue", "")


@pytest.mark.asyncio
async def test_a_heartbeat_turn_waits_for_its_slow_server_before_prompting(monkeypatch):
    client = _FakeClient(slow_reports=True)
    result = await _heartbeat_result(client, monkeypatch)

    assert client.waits == [mcp_problems.UNATTENDED_MCP_WAIT_SECS]
    assert client.prompted_with_tools is True
    assert result == "the agent answer"


@pytest.mark.asyncio
async def test_a_heartbeat_still_missing_a_server_names_it_in_the_result(monkeypatch):
    result = await _heartbeat_result(_FakeClient(slow_reports=False), monkeypatch)

    assert "azure-devops" in result
    assert result.endswith("the agent answer")


@pytest.mark.asyncio
async def test_a_reused_heartbeat_session_does_not_wait_again(monkeypatch):
    client = _FakeClient(slow_reports=True)
    await _heartbeat_result(client, monkeypatch, is_new=False)

    assert client.waits == []
