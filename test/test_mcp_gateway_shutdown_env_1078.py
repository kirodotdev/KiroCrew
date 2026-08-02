"""Regression tests for issue #1078.

Two independent MCP gateway defects:

Gap 1 - gatewayd was SIGKILLed on every restart with attached stubs, because
the supervisor's SIGTERM->SIGKILL grace (5s) was shorter than the daemon's own
graceful drain (10s) + pool.shutdown_all() (5s), and the drain waited on the
(never-empty) pooled connection set instead of on real in-flight work.

Gap 2 - a pooled server's declared, non-secret env was never forwarded to the
shared backend; there was no env analogue of the command's
MC_MCP_TARGET_<SERVER>__<hash> reverse-lookup.

The tests here never spawn or kill a real process tree - the process layer is
mocked and only call ORDER is asserted.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway import manager as manager_mod
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.gatewayd import env_target_resolver
from kiro_crew.mcp_gateway.hashing import hash_effective_env, is_scrubbed_env_key
from kiro_crew.mcp_gateway.manager import GatewayManager, GatewaySpec
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey
from kiro_crew.mcp_gateway.rewriter import rewrite_agents

# --- Gap 1: shutdown budgets + drain condition ------------------------------


def test_shutdown_grace_covers_daemon_drain_and_pool_budget() -> None:
    """The supervisor grace MUST always cover the daemon's drain window plus
    pool.shutdown_all(). This is the exact inversion that produced the SIGKILL
    on every restart; deriving grace from the two budgets makes it impossible
    to reintroduce by editing one number."""
    assert (
        manager_mod._SHUTDOWN_GRACE_SECS
        >= manager_mod._SHUTDOWN_DRAIN_SECS + manager_mod._POOL_SHUTDOWN_SECS
    )


def test_gatewayd_shares_the_same_budgets_as_the_supervisor() -> None:
    """gatewayd must not carry its own copies of the drain / pool budgets -
    they are imported from manager so writer and reader can never drift."""
    from kiro_crew.mcp_gateway import gatewayd as gatewayd_mod

    assert gatewayd_mod._SHUTDOWN_DRAIN_SECS is manager_mod._SHUTDOWN_DRAIN_SECS
    assert gatewayd_mod._POOL_SHUTDOWN_SECS is manager_mod._POOL_SHUTDOWN_SECS


@dataclass
class _FakePoolKey:
    server_name: str = "kirocrew-core"
    agent_name: str = "kirocrew"

    def human_readable(self) -> str:
        return f"{self.agent_name}:{self.server_name}"


def _make_backend() -> Backend:
    from unittest.mock import MagicMock

    process = MagicMock()
    process.returncode = None
    process.pid = 9999
    return Backend(
        pool_key=_FakePoolKey(),  # type: ignore[arg-type]
        process=process,
        stdin=MagicMock(),
        stdout=MagicMock(),
        created_at=0.0,
        last_used_at=0.0,
    )


def test_backend_has_inflight_requests_reflects_pending_and_init() -> None:
    from kiro_crew.mcp_gateway.backend import _PendingRequest

    backend = _make_backend()
    assert backend.has_inflight_requests is False

    backend._pending_requests["gw-1"] = _PendingRequest(  # type: ignore[call-arg]
        original_id=1, stub_uuid="stub-A", method="tools/call"
    )
    assert backend.has_inflight_requests is True

    backend._pending_requests.clear()
    assert backend.has_inflight_requests is False

    backend._init_state = "in_flight"
    assert backend.has_inflight_requests is True


def test_pool_has_inflight_requests_aggregates_over_backends(monkeypatch) -> None:
    pool = BackendPool(max_backends=4)

    # No backends -> nothing in flight: an idle daemon drains at once.
    monkeypatch.setattr(pool, "all_backends", lambda: [])
    assert pool.has_inflight_requests() is False

    @dataclass
    class _B:
        has_inflight_requests: bool

    monkeypatch.setattr(pool, "all_backends", lambda: [_B(False), _B(False)])
    assert pool.has_inflight_requests() is False

    monkeypatch.setattr(pool, "all_backends", lambda: [_B(False), _B(True)])
    assert pool.has_inflight_requests() is True


class _FakeProc:
    """Records the order of SIGTERM/SIGKILL without touching a real process."""

    def __init__(self, *, exits_on_term: bool) -> None:
        self.returncode = None
        self.pid = 4242
        self.calls: list[str] = []
        self._exits_on_term = exits_on_term
        self._killed = False

    def send_signal(self, sig) -> None:  # noqa: ANN001 - sig type is signal.Signals
        self.calls.append("SIGTERM")
        if self._exits_on_term:
            self.returncode = 0

    def kill(self) -> None:
        self.calls.append("SIGKILL")
        self._killed = True
        self.returncode = -9

    async def wait(self) -> int:
        while self.returncode is None and not self._killed:
            await asyncio.sleep(0.005)
        return self.returncode if self.returncode is not None else 0


def _manager(tmp_path: Path) -> GatewayManager:
    spec = GatewaySpec(socket_path=tmp_path / "gw.sock")
    return GatewayManager(spec)


@pytest.mark.asyncio
async def test_terminate_sends_sigterm_first_and_skips_kill_when_graceful(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path)
    proc = _FakeProc(exits_on_term=True)
    mgr._process = proc  # type: ignore[assignment]

    await mgr._terminate_process(grace_secs=0.1)

    # Graceful: the daemon exited on SIGTERM, so we never escalate to SIGKILL.
    assert proc.calls == ["SIGTERM"]


@pytest.mark.asyncio
async def test_terminate_escalates_to_sigkill_only_after_sigterm_times_out(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path)
    proc = _FakeProc(exits_on_term=False)
    mgr._process = proc  # type: ignore[assignment]

    await mgr._terminate_process(grace_secs=0.05)

    # SIGTERM is always attempted BEFORE any escalation.
    assert proc.calls == ["SIGTERM", "SIGKILL"]


# --- Gap 2: forward declared non-secret env to pooled backends --------------


def _write_agent(source_dir: Path, name: str, servers: dict) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / f"{name}.json").write_text(
        json.dumps({"name": name, "mcpServers": servers}), encoding="utf-8"
    )


def _run_rewriter(tmp_path: Path, forward: bool) -> dict:
    source_dir = tmp_path / "agents"
    # Two poolable servers with DIFFERENT declared env, so we can assert one
    # server's env never bleeds into the other's forwarding entry.
    _write_agent(
        source_dir,
        "agent-a",
        {
            "srv-one": {
                "command": "/bin/echo",
                "args": ["--stdio"],
                "poolable": True,
                # placeholder feature flag (non-secret) + a placeholder secret
                "env": {"TOOL_FLAG_ONE": "on", "AWS_SECRET_PLACEHOLDER": "REDACTED"},
            },
            "srv-two": {
                "command": "/bin/echo",
                "args": ["--stdio"],
                "poolable": True,
                "env": {"TOOL_FLAG_TWO": "two"},
            },
        },
    )
    _rewrite, target_env = rewrite_agents(
        source_dir=source_dir,
        overlay_dir=tmp_path / "overlay",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        forward_declared_env=forward,
    )
    return target_env


def test_rewriter_does_not_forward_env_when_flag_disabled(tmp_path: Path) -> None:
    target_env = _run_rewriter(tmp_path, forward=False)
    assert not any(k.startswith("MC_MCP_ENV_") for k in target_env), target_env


def test_rewriter_forwards_only_nonsecret_env_when_enabled(tmp_path: Path) -> None:
    target_env = _run_rewriter(tmp_path, forward=True)

    env_keys = {k: v for k, v in target_env.items() if k.startswith("MC_MCP_ENV_")}
    assert env_keys, "expected a forwarded env entry per server"

    # srv-one: the non-secret flag is forwarded; the secret-prefixed key is not.
    one = json.loads(next(v for k, v in env_keys.items() if "SRV_ONE" in k))
    assert one == {"TOOL_FLAG_ONE": "on"}
    assert not any(is_scrubbed_env_key(k) for k in one)

    # The forwarded set is provably identical to the hashed set: the key's hash
    # suffix equals hash_effective_env(forwarded) and no leakage from srv-two.
    key_one = next(k for k in env_keys if "SRV_ONE" in k)
    assert key_one.endswith("__" + hash_effective_env({"TOOL_FLAG_ONE": "on"}))
    assert "TOOL_FLAG_TWO" not in one

    two = json.loads(next(v for k, v in env_keys.items() if "SRV_TWO" in k))
    assert two == {"TOOL_FLAG_TWO": "two"}


def _pool_key(server: str, env_hash: str) -> PoolKey:
    return PoolKey(
        server_name=server,
        agent_name="agent-a",
        command_args_hash="cah",
        effective_env_hash=env_hash,
        work_dir="/tmp/wd",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="aah",
        approval_mode="reads",
        trust_all_tools=False,
        user_identity="tester",
        channel_id=None,
        config_snapshot_hash="csh",
    )


def test_resolver_applies_forwarded_env_to_the_right_server_only(monkeypatch) -> None:
    # srv-one has a published command AND a forwarded non-secret env.
    monkeypatch.setenv("MC_MCP_TARGET_SRV_ONE", "/bin/echo --stdio")
    monkeypatch.setenv(
        "MC_MCP_ENV_SRV_ONE__hashone",
        json.dumps({"TOOL_FLAG_ONE": "on", "AWS_SECRET_PLACEHOLDER": "REDACTED"}),
    )
    # srv-two has a command but NO forwarded env entry.
    monkeypatch.setenv("MC_MCP_TARGET_SRV_TWO", "/bin/echo --stdio")

    resolved_one = env_target_resolver(_pool_key("srv-one", "hashone"))
    assert resolved_one is not None
    _cmd, _args, env_one, _wd = resolved_one
    # Declared non-secret key reaches its OWN backend...
    assert env_one.get("TOOL_FLAG_ONE") == "on"
    # ...but a secret-prefixed key is dropped even if it slipped into the JSON.
    assert "AWS_SECRET_PLACEHOLDER" not in env_one

    # A different server's backend must NOT inherit srv-one's declared env.
    resolved_two = env_target_resolver(_pool_key("srv-two", "hashtwo"))
    assert resolved_two is not None
    _c2, _a2, env_two, _w2 = resolved_two
    assert "TOOL_FLAG_ONE" not in env_two
