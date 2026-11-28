"""Runtime ``agent.acp_failover`` (RFC Phase 2): a first request refused for a spent
allowance or a rejected login moves the run to the next chain member.

The gate (``acp_failover_chain.runtime_hop_verdict``) is pinned as a pure function,
and the respawn through the sub-agent manager with a scripted ``_run_inner``: the
first attempt raises the vendor's refusal, the replacement must start on the next
member and record the hop. The config read and the backend probe are the module's
two seams, so no config file or installed harness is involved.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_turn_resilience_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _close_subagent_managers,
    _manager,
    _mock_sessions,
)

from kiro_crew import acp_failover_chain
from kiro_crew.acp.client import AcpError
from kiro_crew.acp_failover_chain import runtime_hop_verdict, runtime_verdict
from kiro_crew.subagent import SubagentInfo

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_CHAIN = {"enabled": True, "chain": ["codex", "claude"]}


def _refusal(kind: str) -> AcpError:
    error = AcpError("The monthly usage limit has been reached", transient=False)
    if kind == "usage_limit":
        error.usage_limit = True
    else:
        error.sign_in_needed = True
    return error


def _info(**fields: object) -> SubagentInfo:
    info = SubagentInfo(id="hop", task="t", parent_session_key="", backend="codex")
    for name, value in fields.items():
        setattr(info, name, value)
    return info


@pytest.fixture
def block(monkeypatch):
    """Serve *value* as the loaded ``agent.acp_failover``; returns the setter."""
    current: dict = {"value": dict(_CHAIN)}
    monkeypatch.setattr(acp_failover_chain, "_failover_block", lambda: current["value"])
    return lambda value: current.update(value=value)


class TestRuntimeVerdict:
    def test_a_spent_allowance_is_usage_limit(self):
        assert runtime_verdict(_refusal("usage_limit")) == "usage_limit"

    def test_a_rejected_login_is_auth(self):
        assert runtime_verdict(_refusal("auth")) == "auth"

    def test_a_harness_signed_out_at_start_is_auth(self):
        from kiro_crew.acp.transport_errors import AcpAuthRequired

        assert runtime_verdict(AcpAuthRequired("sign in to codex", backend="codex")) == "auth"

    def test_any_other_error_is_no_verdict(self):
        assert runtime_verdict(AcpError("boom", transient=False)) is None
        assert runtime_verdict(RuntimeError("boom")) is None


class TestRuntimeHopVerdict:
    def test_an_untouched_named_run_hops(self, block):
        assert runtime_hop_verdict(_info(), _refusal("usage_limit"), shutting_down=False) == (
            "usage_limit"
        )

    @pytest.mark.parametrize(
        "value",
        [{}, {"enabled": False, "chain": ["codex", "claude"]}],
        ids=["no-block", "disabled"],
    )
    def test_without_an_enabled_block_nothing_changes(self, block, value):
        block(value)
        assert runtime_hop_verdict(_info(), _refusal("usage_limit"), shutting_down=False) is None

    def test_a_trigger_the_block_omits_does_not_hop(self, block):
        block({**_CHAIN, "triggers": ["backend_not_installed"]})
        assert runtime_hop_verdict(_info(), _refusal("usage_limit"), shutting_down=False) is None

    def test_the_last_member_does_not_hop(self, block):
        info = _info(backend="claude")
        assert runtime_hop_verdict(info, _refusal("usage_limit"), shutting_down=False) is None

    @pytest.mark.parametrize(
        "fields",
        [
            {"backend": None},
            {"turns": 1},
            {"tool_count": 1},
            {"streaming_text": "partial"},
            {"result": "done"},
            {"conversation_key": "conv"},
            {"model": "some-model"},
            {"routed_model": "routed"},
            {"requested_model": "role-pinned-model"},
            {"user_stopped": True},
            {"_reap_started": True},
        ],
        ids=[
            "default-backend",
            "a-turn",
            "a-tool",
            "text",
            "result",
            "continuation",
            "pinned-model",
            "routed-model",
            "role-pinned-model",
            "stopped",
            "reaping",
        ],
    )
    def test_a_run_that_cannot_be_replayed_safely_does_not_hop(self, block, fields):
        assert runtime_hop_verdict(_info(**fields), _refusal("auth"), shutting_down=False) is None

    def test_shutdown_does_not_hop(self, block):
        assert runtime_hop_verdict(_info(), _refusal("auth"), shutting_down=True) is None

    def test_an_unpinned_requested_model_still_hops(self, block):
        info = _info(requested_model="auto")
        assert runtime_hop_verdict(info, _refusal("auth"), shutting_down=False) == "auth"

    def test_an_auto_model_still_hops(self, block):
        assert runtime_hop_verdict(_info(model="auto"), _refusal("auth"), shutting_down=False) == (
            "auth"
        )


async def _run_refused_first(
    mgr,
    *,
    refusal: Exception,
    before: dict | None = None,
    session_id: str = "refused-sid",
    state_written: bool = True,
    mapped_sid: str | None = "refused-sid",
) -> tuple[SubagentInfo, list]:
    """Spawn on codex; the first attempt sets *before* on the run, then raises
    *refusal*; a second attempt succeeds."""
    attempts: list[tuple[str | None, str | None]] = []

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        attempts.append((info.backend, info.failover_from))
        if len(attempts) == 1:
            info._session_id = session_id
            for name, value in (before or {}).items():
                setattr(info, name, value)
            raise refusal
        info.result = "done on the next backend"

    mgr._run_inner = AsyncMock(side_effect=run_inner)
    mgr._sessions.forget_conversation_if_sid = MagicMock(
        return_value=(mapped_sid is not None and mapped_sid == session_id, mapped_sid)
    )
    mgr._sessions.aflush = AsyncMock()
    mgr._write_state_off_loop = AsyncMock(return_value=state_written)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("t", backend="codex")
        assert info is not None
        # flake-ok: a scripted _run_inner that returns at once; bounded by the test timeout
        await mgr._tasks[info.id]
        recovery = mgr._tasks.get(f"{info.id}:recovery")
        if recovery is not None:
            # flake-ok: the recovery task of the same scripted run; bounded by the test timeout
            await recovery
        replacement = mgr._tasks.get(info.id)
        if replacement is not None and not replacement.done():
            # flake-ok: the replacement of the same scripted run; bounded by the test timeout
            await replacement
    return info, attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["usage_limit", "auth"])
async def test_a_refused_first_request_restarts_on_the_next_member(block, monkeypatch, kind):
    monkeypatch.setattr(acp_failover_chain, "_available_backends", lambda: ["codex", "claude"])
    mgr = _manager(_mock_sessions(lambda _msg: None))

    info, attempts = await _run_refused_first(mgr, refusal=_refusal(kind))

    assert attempts == [("codex", None), ("claude", "codex")]
    assert (info.backend, info.failover_from) == ("claude", "codex")
    # The refused backend's session is retired, so the new backend never loads it.
    mgr._sessions.forget_conversation_if_sid.assert_called_once_with(
        f"subagent:{info.id}", "refused-sid"
    )
    mgr._write_state_off_loop.assert_any_await(info, "failover backend", backend="claude")
    assert info.error in (None, "")
    assert info.result == "done on the next backend"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before",
    [{"streaming_text": "partial answer"}, {"tool_count": 1}],
    ids=["after-text", "after-a-tool"],
)
async def test_a_run_that_produced_output_is_never_respawned(block, monkeypatch, before):
    probe = MagicMock(return_value=["codex", "claude"])
    monkeypatch.setattr(acp_failover_chain, "_available_backends", probe)
    mgr = _manager(_mock_sessions(lambda _msg: None))

    info, attempts = await _run_refused_first(mgr, refusal=_refusal("usage_limit"), before=before)

    assert attempts == [("codex", None)]
    assert (info.backend, info.failover_from) == ("codex", None)
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_a_transient_failure_never_moves_the_run(block, monkeypatch):
    probe = MagicMock(return_value=["codex", "claude"])
    monkeypatch.setattr(acp_failover_chain, "_available_backends", probe)
    mgr = _manager(_mock_sessions(lambda _msg: None))

    info, attempts = await _run_refused_first(
        mgr, refusal=AcpError("503 Service Unavailable", transient=True)
    )

    assert attempts == [("codex", None)]
    assert info.backend == "codex"
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_without_the_block_the_refusal_ends_the_run_as_today(block, monkeypatch):
    block({})
    probe = MagicMock(return_value=["codex", "claude"])
    monkeypatch.setattr(acp_failover_chain, "_available_backends", probe)
    mgr = _manager(_mock_sessions(lambda _msg: None))

    info, attempts = await _run_refused_first(mgr, refusal=_refusal("usage_limit"))

    assert attempts == [("codex", None)]
    assert (info.backend, info.failover_from) == ("codex", None)
    assert "usage limit" in (info.error or "")
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_no_member_that_can_start_fails_with_the_refusal(block, monkeypatch):
    monkeypatch.setattr(acp_failover_chain, "_available_backends", lambda: ["codex"])
    mgr = _manager(_mock_sessions(lambda _msg: None))

    info, attempts = await _run_refused_first(mgr, refusal=_refusal("usage_limit"))

    assert attempts == [("codex", None)]
    assert info.backend == "codex"
    assert info.done is True
    assert "usage limit" in (info.error or "")
    assert "agent.acp_failover could not start the run on another backend" in (info.error or "")


def test_the_respawn_failure_text_keeps_each_reason():
    from kiro_crew.subagent_manager.recovery_reasons import recovery_failed

    info = SimpleNamespace(_failover_detail="limit reached")
    assert recovery_failed("unexpected_cancel", info) == (
        "cancelled (recovery failed)",
        "cancelled",
    )
    assert recovery_failed("context_overflow", info) == (
        "agent context exceeded the model window and the dedicated-session "
        "recovery could not start",
        "error",
    )
    assert recovery_failed("session_not_found", info) == (
        "the backend lost this run's session and the fresh-runtime re-run could not start",
        "error",
    )
    assert recovery_failed("backend_failover", info)[0].startswith("limit reached")


@pytest.mark.asyncio
async def test_a_harness_signed_out_at_session_start_still_hops(block, monkeypatch):
    # The refusal comes from ``provider.start()``, before any session id is
    # captured, and no mapping exists for the run's key.
    from kiro_crew.acp.transport_errors import AcpAuthRequired

    monkeypatch.setattr(acp_failover_chain, "_available_backends", lambda: ["codex", "claude"])
    mgr = _manager(_mock_sessions(lambda _msg: None))

    info, attempts = await _run_refused_first(
        mgr,
        refusal=AcpAuthRequired("sign in to codex", backend="codex"),
        session_id="",
        mapped_sid=None,
    )

    assert attempts == [("codex", None), ("claude", "codex")]
    assert info.result == "done on the next backend"


@pytest.mark.asyncio
async def test_an_unknown_sid_with_a_mapped_session_fails_closed(block, monkeypatch):
    probe = MagicMock(return_value=["codex", "claude"])
    monkeypatch.setattr(acp_failover_chain, "_available_backends", probe)
    mgr = _manager(_mock_sessions(lambda _msg: None))

    info, attempts = await _run_refused_first(
        mgr, refusal=_refusal("auth"), session_id="", mapped_sid="someone-else"
    )

    assert attempts == [("codex", None)]
    assert info.backend == "codex" and info.done is True
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_a_hop_the_run_record_did_not_take_never_runs(block, monkeypatch):
    monkeypatch.setattr(acp_failover_chain, "_available_backends", lambda: ["codex", "claude"])
    mgr = _manager(_mock_sessions(lambda _msg: None))

    info, attempts = await _run_refused_first(
        mgr, refusal=_refusal("usage_limit"), state_written=False
    )

    assert attempts == [("codex", None)]
    assert (info.backend, info.failover_from) == ("codex", None)
    assert info.done is True
    assert "agent.acp_failover could not start the run on another backend" in (info.error or "")


def test_the_spawn_probe_and_the_hop_share_one_availability_rule():
    from kiro_crew.agent_sdk.backend_install import MISSING

    assert acp_failover_chain.can_start(SimpleNamespace(installed="ok", restart_required=False))
    assert not acp_failover_chain.can_start(
        SimpleNamespace(installed=MISSING, restart_required=False)
    )
    assert not acp_failover_chain.can_start(SimpleNamespace(installed="ok", restart_required=True))
