"""Regression: `pod_up` must mint the pod token in the gateway, not in the
sandboxed `pod up` child.

The bug: from a crew-member session, `pod_up` failed with HTTP 403
``member_owner_token_refused`` even though the pod came up healthy. The pod
route ``/api/token/local`` gates on ``local_owner_bootstrap_allowed``, which on
Linux requires the CALLER to share the gateway's user + mount namespaces (or be
a gateway-spawned app backend). The token was minted by the ``kirocrew pod up``
child, and that child is spawned through the sandbox (``sandboxed_spawn_argv``),
so on Linux it runs in its OWN user namespace -- a namespace the pod gateway (a
host-namespace ``systemd --user`` unit) cannot certify as the local owner.

The fix moves the mint into the gateway process, which shares the pod gateway's
host namespace and IS accepted -- the shape ``agent_pod_api`` documents as
"minting happens here, in the gateway". The `pod up` child is booted with
``--no-token`` so it never dials the pod's token route at all.

No process here detaches, enters a namespace, starts a gateway or mints a real
token: every process tree and kernel response is synthetic.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew import member_memory_auth as auth
from kiro_crew import platform_compat as pc
from kiro_crew.apps.builtins.dev_fleet import worktree_ops


# --------------------------------------------------------------------------- #
# Step 1 -- the differentiator, at the gate that actually refused.
# --------------------------------------------------------------------------- #
@pytest.fixture
def linux_owner_gate(monkeypatch):
    """Drive ``local_owner_bootstrap_allowed`` on a synthetic Linux host.

    The pod gateway is pid 100 in the host namespace. Callers are judged by
    whether their kernel namespaces match it.
    """
    namespaces = {100: "host", 200: "host", 300: "member-sandbox"}
    monkeypatch.setattr(auth, "os", SimpleNamespace(**{**vars(os), "getpid": lambda: 100}))
    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: f"start-{pid}")
    monkeypatch.setattr(
        pc, "process_namespaces_match", lambda pid, other: namespaces[pid] == namespaces[other]
    )
    # No app backend is registered on the pod gateway, so _gateway_spawned_app_backend
    # answers False for every pid -- the pod gateway spawned no app backend.
    monkeypatch.setattr(auth, "_gateway_spawned_app_backend", lambda pid: False)

    def _request_for(pid):
        monkeypatch.setattr(auth, "_request_peer_pid", lambda request: pid)
        return SimpleNamespace(get={"internal_auth": True}.get, headers={})

    return SimpleNamespace(namespaces=namespaces, request_for=_request_for)


def test_the_gateway_process_is_certified_as_the_local_owner(linux_owner_gate):
    """The gateway (host namespace, pid 200) IS the local owner.

    This is the process that mints after the fix, and the pod accepts it.
    """
    assert auth.local_owner_bootstrap_allowed(linux_owner_gate.request_for(200)) is True


def test_a_sandboxed_member_child_is_refused_as_the_local_owner(linux_owner_gate):
    """A `pod up` child in the member sandbox's own namespace (pid 300) is refused.

    This IS the bug: minting from that process yields member_owner_token_refused.
    The fix stops minting there, it does NOT widen this gate -- the gate's answer
    for a foreign-namespace process is unchanged.
    """
    assert auth.local_owner_bootstrap_allowed(linux_owner_gate.request_for(300)) is False


# --------------------------------------------------------------------------- #
# Step 2 -- the fix: _pod_up mints in the gateway and boots the child --no-token.
# --------------------------------------------------------------------------- #
@pytest.fixture
def pod_up_stubs(monkeypatch):
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(worktree_ops.runtime, "_warm_build_path", AsyncMock(return_value=None))
    monkeypatch.setattr(worktree_ops.runtime, "_find_cli", lambda: ["kirocrew"])
    monkeypatch.setattr(worktree_ops.runtime, "_load_cfg", lambda: object())
    monkeypatch.setattr(worktree_ops.runtime, "_POD_AVAILABLE", True)
    monkeypatch.setattr(worktree_ops, "_pod_env", lambda: {})
    monkeypatch.setattr(worktree_ops.repository, "_repo", lambda: "/repo")
    monkeypatch.setattr(
        worktree_ops.repository,
        "_find_worktree",
        AsyncMock(return_value=({"path": "/repo/kc-wt-x"}, None)),
    )
    state = SimpleNamespace(pin=(True, "/repo/kc-wt-x"), locked=False, thread=None, audit=Mock())

    @contextmanager
    def _mutex(cfg, name):
        assert not state.locked
        state.locked = True
        state.thread = threading.get_ident()
        try:
            yield
        finally:
            state.locked = False

    def _read_pin(cfg, name):
        assert state.locked
        assert state.thread == threading.get_ident()
        if isinstance(state.pin, Exception):
            raise state.pin
        return state.pin

    monkeypatch.setattr(worktree_ops.runtime.rt, "pod_name_mutex", _mutex)
    monkeypatch.setattr(worktree_ops, "_read_pin_strict", _read_pin)
    monkeypatch.setattr(worktree_ops.runtime.rt, "derive_port", lambda cfg, name: 7100)
    monkeypatch.setattr(worktree_ops.runtime, "_sel", lambda: state.audit)
    return state


@pytest.mark.asyncio
async def test_pod_up_mints_the_token_in_the_gateway(monkeypatch, pod_up_stubs):
    """The member-session path yields a token: the gateway mints it in-process.

    The child is booted with --no-token (empty token in its --json), and the
    handle's token is the gateway's own mint -- so the sandboxed child never
    dials the pod's token route and the member_owner_token_refused path is gone.
    """
    run_cmd = AsyncMock(
        return_value=(0, '{"port": 7100, "base_url": "http://127.0.0.1:7100", "token": ""}', "")
    )
    monkeypatch.setattr(worktree_ops.runtime, "_run_cmd", run_cmd)
    monkeypatch.setattr(worktree_ops.runtime.rt, "active_names", lambda cfg: {"kc-wt-x"})
    minted = SimpleNamespace(calls=[])

    def _mint(cfg, name, ttl):
        assert pod_up_stubs.locked
        assert pod_up_stubs.thread == threading.get_ident() != caller_thread
        minted.calls.append((name, ttl))
        return "tok-gateway"

    caller_thread = threading.get_ident()
    monkeypatch.setattr(worktree_ops.runtime.rt, "mint_token", _mint)

    result = await worktree_ops._pod_up("kc-wt-x")

    assert result["ok"] is True
    assert result["token"] == "tok-gateway"
    assert result["port"] == 7100
    # The child is told --no-token, so it never touches the pod's token route.
    assert "--no-token" in run_cmd.await_args.args[0]
    # The gateway is the process that minted.
    assert minted.calls == [("kc-wt-x", "2h")]
    assert pod_up_stubs.locked is False
    pod_up_stubs.audit.log_api_access.assert_called_once_with(
        caller="dev_fleet",
        operation="pod.token",
        outcome="allowed",
        source="app",
        resources="name=kc-wt-x port=7100 ttl=2h",
        error="",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pin", "code", "error"),
    [
        ((True, "/other/kc-wt-x"), "pod_checkout_mismatch", "different checkout"),
        ((False, None), "pod_checkout_mismatch", "checkout pin"),
        ((True, None), "pod_checkout_mismatch", "checkout pin"),
        (OSError("unreadable pin"), "pod_token_mint_failed", "unreadable pin"),
    ],
)
async def test_pod_up_rechecks_pin_after_boot(monkeypatch, pod_up_stubs, pin, code, error):
    """A name replaced or unattributable after boot never supplies a credential."""

    async def _boot(*args, **kwargs):
        pod_up_stubs.pin = pin
        return 0, '{"port": 7100, "token": ""}', ""

    monkeypatch.setattr(worktree_ops.runtime, "_run_cmd", _boot)
    monkeypatch.setattr(worktree_ops.runtime.rt, "active_names", lambda cfg: {"kc-wt-x"})
    mint = Mock(return_value="foreign-token")
    monkeypatch.setattr(worktree_ops.runtime.rt, "mint_token", mint)

    result = await worktree_ops._pod_up("kc-wt-x")

    assert result["ok"] is False
    assert result["code"] == code
    assert error in result["error"]
    assert "token" not in result
    mint.assert_not_called()
    row = pod_up_stubs.audit.log_api_access.call_args.kwargs
    assert row["operation"] == "pod.token"
    assert row["outcome"] == ("failure" if isinstance(pin, Exception) else "denied")
    assert "foreign-token" not in str(row)


@pytest.mark.asyncio
async def test_pod_up_audit_failure_preserves_mint_result(monkeypatch, pod_up_stubs, caplog):
    """Best-effort audit failures are visible without losing a valid handle."""
    monkeypatch.setattr(
        worktree_ops.runtime, "_run_cmd", AsyncMock(return_value=(0, '{"port": 7100}', ""))
    )
    monkeypatch.setattr(worktree_ops.runtime.rt, "active_names", lambda cfg: {"kc-wt-x"})
    monkeypatch.setattr(worktree_ops.runtime.rt, "mint_token", lambda *args: "tok-gateway")
    pod_up_stubs.audit.log_api_access.side_effect = RuntimeError("audit unavailable")

    result = await worktree_ops._pod_up("kc-wt-x")

    assert result["ok"] is True
    assert result["token"] == "tok-gateway"
    assert "SEL audit failed for pod.token" in caplog.text
    assert "tok-gateway" not in caplog.text


@pytest.mark.asyncio
async def test_pod_up_reports_a_mint_failure_without_claiming_success(monkeypatch, pod_up_stubs):
    """A pod that booted but whose token could not be minted is not a success.

    The mint failure is surfaced as an error rather than returning a handle with
    no usable credential.
    """
    monkeypatch.setattr(
        worktree_ops.runtime, "_run_cmd", AsyncMock(return_value=(0, '{"port": 7100}', ""))
    )
    monkeypatch.setattr(worktree_ops.runtime.rt, "active_names", lambda cfg: {"kc-wt-x"})

    def _boom(cfg, name, ttl):
        raise RuntimeError("socket gone")

    monkeypatch.setattr(worktree_ops.runtime.rt, "mint_token", _boom)

    result = await worktree_ops._pod_up("kc-wt-x")

    assert result["ok"] is False
    assert "token mint failed" in result["error"]
    row = pod_up_stubs.audit.log_api_access.call_args.kwargs
    assert row["outcome"] == "failure"
    assert row["resources"] == "name=kc-wt-x port=7100 ttl=2h"
    assert row["error"] == "mint failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("pod_available", [False, True])
async def test_pod_up_without_cfg_keeps_cli_minting(monkeypatch, pod_up_stubs, pod_available):
    """No gateway config leaves token minting with the CLI, including Windows."""
    monkeypatch.setattr(worktree_ops.runtime, "_load_cfg", lambda: None)
    monkeypatch.setattr(worktree_ops.runtime, "_POD_AVAILABLE", pod_available)
    run_cmd = AsyncMock(return_value=(0, '{"port": 7100, "token": "tok-cli"}', ""))
    monkeypatch.setattr(worktree_ops.runtime, "_run_cmd", run_cmd)

    def _no_gateway_mint(*args):
        pytest.fail("a missing gateway config must leave minting to the CLI")

    monkeypatch.setattr(worktree_ops.runtime.rt, "mint_token", _no_gateway_mint)
    result = await worktree_ops._pod_up("kc-wt-x")

    assert result == {"ok": True, "port": 7100, "token": "tok-cli"}
    assert "--no-token" not in run_cmd.await_args.args[0]


@pytest.mark.asyncio
async def test_pod_up_withholds_token_when_ownership_is_unproven(monkeypatch, pod_up_stubs):
    """Unproven ownership withholds the credential, not the successful boot."""
    monkeypatch.setattr(
        worktree_ops.runtime,
        "_run_cmd",
        AsyncMock(
            return_value=(0, '{"port": 7100, "base_url": "http://127.0.0.1:7100", "token": ""}', "")
        ),
    )
    monkeypatch.setattr(worktree_ops.runtime.rt, "active_names", lambda cfg: {"kc-wt-x"})

    def _unproven(cfg, name, ttl):
        raise worktree_ops.runtime.rt.PodOwnershipUnproven("could not prove port ownership")

    monkeypatch.setattr(worktree_ops.runtime.rt, "mint_token", _unproven)
    result = await worktree_ops._pod_up("kc-wt-x")

    assert result["ok"] is True
    assert result["token"] == ""
    assert result["port"] == 7100
    assert result["base_url"] == "http://127.0.0.1:7100"
    assert "token withheld" in result["warning"]
    assert "could not prove port ownership" in result["warning"]
    assert "error" not in result
    row = pod_up_stubs.audit.log_api_access.call_args.kwargs
    assert row["outcome"] == "denied"
    assert row["resources"] == "name=kc-wt-x port=7100 ttl=2h"
    assert row["error"] == "ownership unprovable; credential withheld"


# --------------------------------------------------------------------------- #
# The `pod up --no-token` CLI switch the gateway relies on.
# --------------------------------------------------------------------------- #
class _CfgStub:
    """A PodConfig stand-in exposing only the attributes `_up` reads directly."""

    live_port = 0


@pytest.mark.parametrize("json_output", [True, False])
def test_pod_up_no_token_skips_the_mint_and_emits_empty_token(monkeypatch, capsys, json_output):
    """`pod up --no-token --json` boots the pod but never mints a token.

    The child that would otherwise dial the pod's token route is exactly the
    sandboxed process the pod refuses; --no-token stops it dialling at all. The
    handle's token is empty (the gateway supplies its own).
    """
    import argparse
    import json as _json
    from pathlib import Path

    from kiro_crew.pod import cli as pod_cli
    from kiro_crew.pod import runtime as rt

    class _Mutex:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pod_cli, "_require_up_user_bus", lambda name: None)
    monkeypatch.setattr(pod_cli, "_resolve_or_die", lambda c, n: Path("/co"))
    monkeypatch.setattr(pod_cli.prov, "has_venv", lambda c: True)
    monkeypatch.setattr(pod_cli.prov, "has_dist", lambda c: True)
    monkeypatch.setattr(rt, "derive_port", lambda c, n: 7100)
    monkeypatch.setattr(rt, "is_scenario_ref", lambda s: False)
    monkeypatch.setattr(rt, "pod_name_mutex", lambda c, n: _Mutex())
    monkeypatch.setattr(rt, "pod_plane_mutex", lambda c: _Mutex())
    monkeypatch.setattr(rt, "pin_checkout", lambda *a, **k: None)
    # An already-active pod is a restart: keeps the derived port and skips
    # allocation, so the boot path needs no port-probe stubbing.
    monkeypatch.setattr(rt, "is_active", lambda c, n: True)
    monkeypatch.setattr(pod_cli, "_home_holds_state", lambda c, n: True)
    monkeypatch.setattr(rt, "write_env_file", lambda *a, **k: None)
    monkeypatch.setattr(rt, "read_env_file", lambda *a, **k: {})
    monkeypatch.setattr(rt, "embeddings_disabled", lambda env: False)
    monkeypatch.setattr(pod_cli, "_wait_healthy", lambda *a, **k: 200)
    monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

    def _no_mint(*_a, **_k):
        pytest.fail("--no-token must not mint a pod token")

    monkeypatch.setattr(rt, "mint_token", _no_mint)

    args = argparse.Namespace(
        name="demo",
        json=json_output,
        no_token=True,
        ttl="2h",
        seed="",
        provision=False,
        approval=None,
        crons=False,
        no_embeddings=False,
        wait_secs=None,
    )
    pod_cli._up(_CfgStub(), args)

    captured = capsys.readouterr()
    assert captured.err == ""
    if json_output:
        handle = _json.loads(captured.out)
        assert handle["status"] == "up"
        assert handle["token"] == ""
    else:
        assert "token    : (skipped by request: --no-token)" in captured.out
        assert "ownership" not in captured.out
        assert "open     :" not in captured.out
