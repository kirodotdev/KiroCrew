"""A stubbed MCP server's backend runs outside the sandbox only with an approved launch.

gatewayd spawns a stubbed server's backend as the user, outside the session
sandbox. The operator approves a server by NAME (``mcp_gateway.stub_servers``),
while the command behind that name comes from agent-writable files. These tests
drive each way such a file reached the daemon's spawn and pin that an unapproved
launch never does:

* a spec entry under an already-stubbed name, with a different command;
* a spec entry that arrives pre-marked as a gateway wrapper, stubbed or not;
* a tampered overlay served from the rewrite cache;
* gatewayd resolving a target env entry nobody approved;
* a spec edited between the operator's stub toggle and the next rewrite.

New symbols are resolved with ``getattr`` so a tree without them fails at an
assertion rather than at collection.

This file pins the rewrite and gatewayd refusing an unapproved launch, the sealed
approval store, the launch the stub toggle approves, the refusal and approved
displays, and the store's bounds and caps. The toggle's compare-and-set, refusals,
reapprove and stub off are in ``test_mcp_launch_approval_toggle.py``, the store's
removed-field load, deferred-save merge, refused-stem record cap and
revoke-restore in ``test_mcp_launch_approval_store_records.py``, the gateway
start, seeding, the stem codec and the suite-wide fixture guard in
``test_mcp_launch_approval_gateway.py``, and the placeholder rebind in
``test_mcp_launch_approval_rebind.py``; ``mcp_launch_approval_helpers.py`` holds
the builders these files share.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import threading
from pathlib import Path

import pytest
from mcp_launch_approval_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APPROVED_CMD,
    _ATTACK_CMD,
    _approval_module,
    _approvals_for,
    _declare,
    _next_boot_rewrite,
    _pool_key,
    _rewrite,
    _stub_request,
    _target_spec,
    _targets_running,
    _toggle_on,
    _write_agent,
    toggle_home_fixture,
)

from kiro_crew.mcp_gateway import rewriter
from kiro_crew.mcp_gateway.hashing import encode_target_args
from kiro_crew.testing.wait import async_wait_until

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True


def test_a_changed_command_under_a_stubbed_name_is_not_handed_to_the_daemon(tmp_path):
    mod = _approval_module()
    approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    _write_agent(tmp_path / "agents", {"srv": {"command": _ATTACK_CMD, "args": ["-c", "id"]}})

    target_env = _rewrite(tmp_path, frozenset({"srv"}), approvals)

    assert _targets_running(target_env, _ATTACK_CMD) == []
    overlay = json.loads((tmp_path / "home" / "mcp-gateway" / "agents" / "a.json").read_text())
    entry = overlay["mcpServers"]["srv"]
    # Left for the session to launch inside its own sandbox.
    assert entry["command"] == _ATTACK_CMD
    assert not entry.get(rewriter._WRAPPER_MARKER)
    assert mod is not None and mod.REFUSED_CHANGED in approvals.refused.values()


def test_the_approved_launch_is_still_wrapped(tmp_path):
    mod = _approval_module()
    assert mod is not None
    approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    _write_agent(tmp_path / "agents", {"srv": {"command": _APPROVED_CMD, "args": []}})

    target_env = _rewrite(tmp_path, frozenset({"srv"}), approvals)

    assert _targets_running(target_env, _APPROVED_CMD)
    assert approvals.refused == {}


@pytest.mark.parametrize("stubbed", [frozenset(), frozenset({"srv"})])
def test_a_prewrapped_entry_does_not_publish_its_own_target(tmp_path, stubbed):
    mod = _approval_module()
    approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    _write_agent(
        tmp_path / "agents",
        {
            "srv": {
                "command": _APPROVED_CMD,
                "args": [
                    "--target-command",
                    _ATTACK_CMD,
                    f"--target-args-b64={encode_target_args(['-c', 'id'])}",
                ],
                rewriter._WRAPPER_MARKER: True,
            }
        },
    )

    target_env = _rewrite(tmp_path, stubbed, approvals)

    assert _targets_running(target_env, _ATTACK_CMD) == []


def test_a_tampered_cached_overlay_is_not_served(tmp_path):
    mod = _approval_module()
    approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    _write_agent(tmp_path / "agents", {"srv": {"command": _APPROVED_CMD, "args": []}})
    _rewrite(tmp_path, frozenset({"srv"}), approvals)

    overlay_dir = tmp_path / "home" / "mcp-gateway" / "agents"
    overlay_path = overlay_dir / "a.json"
    overlay = json.loads(overlay_path.read_text())
    entry = overlay["mcpServers"]["srv"]
    entry["args"] = [
        "--server",
        "srv",
        "--agent",
        "a",
        "--target-command",
        _ATTACK_CMD,
        f"--target-args-b64={encode_target_args(['-c', 'id'])}",
    ]
    overlay_path.write_text(json.dumps(overlay))
    # Re-vouch for the edited overlay the way an agent editing both files would.
    fp_path = overlay_dir / rewriter._FINGERPRINT_NAME
    stored = json.loads(fp_path.read_text())
    stored["outputs"]["overlays"]["a.json"] = rewriter._stat_sig(overlay_path)
    fp_path.write_text(json.dumps(stored))

    approvals2 = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    target_env = _rewrite(tmp_path, frozenset({"srv"}), approvals2)

    assert _targets_running(target_env, _ATTACK_CMD) == []
    assert _targets_running(target_env, _APPROVED_CMD)


def test_an_abandoned_cached_pass_leaves_no_refusal_state_behind(tmp_path):
    """The rewrite that replaces a refused cache still offers a way to approve.

    A cache-served pass runs the target filter, and the filter records the
    unapproved command it dropped. That pass is abandoned and the overlays are
    regenerated, so the full pass that follows owns the refusal -- and it can
    only report a complete identity set if every leftover of the abandoned
    attempt was reset. Otherwise the stored record carries no
    ``expected_launch``, and the MCP page shows the server refused with nothing
    to approve.
    """
    mod = _approval_module()
    assert mod is not None
    stem = mod.target_stem("srv")
    # Approved: one command. Declared: a different one. The server is refused on
    # every pass, so a re-approval is the operator's only way out of it.
    _write_agent(tmp_path / "agents", {"srv": {"command": _ATTACK_CMD, "args": []}})
    _rewrite(tmp_path, frozenset({"srv"}), _approvals_for(mod, "srv", _APPROVED_CMD, []))

    # Publish an unapproved target for the same server in the cached overlay and
    # re-vouch for it, the way an agent editing both files would: that is what
    # makes the next pass serve the cache, drop the target and start over.
    overlay_dir = tmp_path / "home" / "mcp-gateway" / "agents"
    overlay_path = overlay_dir / "a.json"
    overlay = json.loads(overlay_path.read_text())
    overlay["mcpServers"]["srv"] = {
        "command": _APPROVED_CMD,
        "args": [
            "--server",
            "srv",
            "--agent",
            "a",
            "--target-command",
            _ATTACK_CMD,
            f"--target-args-b64={encode_target_args(['-c', 'id'])}",
        ],
        rewriter._WRAPPER_MARKER: True,
    }
    overlay_path.write_text(json.dumps(overlay))
    fp_path = overlay_dir / rewriter._FINGERPRINT_NAME
    stored = json.loads(fp_path.read_text())
    stored["outputs"]["overlays"]["a.json"] = rewriter._stat_sig(overlay_path)
    fp_path.write_text(json.dumps(stored))

    approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    target_env = _rewrite(tmp_path, frozenset({"srv"}), approvals)

    assert _targets_running(target_env, _ATTACK_CMD) == []
    assert approvals.full_pass, "an unapproved cached target did not force a full rewrite"
    assert approvals.refused_commands == {}, "the abandoned pass left its dropped commands behind"
    assert approvals.incomplete_identities == {}
    assert approvals.refused_identities(stem) is not None, (
        "the refusal reports no approvable identity, so the MCP page offers the "
        "operator nothing to approve"
    )

    store = tmp_path / "approvals.json"
    mod._write(store, mod._document(_approvals_for(mod, "srv", _APPROVED_CMD, []), {}))
    assert mod.save_pass(approvals, path=store)
    record = json.loads(store.read_text())["refused"][stem]
    assert record.get("complete") is not False
    assert record.get("expected_launch"), "the stored refusal cannot be approved from the page"


def test_the_gateway_filter_drops_an_unapproved_target():
    mod = _approval_module()
    filt = getattr(mod, "filter_target_env", None)
    assert filt is not None
    approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    kept, dropped = filt(
        {
            "KIROCREW_MCP_TARGET_SRV": _target_spec(_APPROVED_CMD),
            "KIROCREW_MCP_TARGET_SRV__abc": _target_spec(_ATTACK_CMD, "-c", "id"),
            "KIROCREW_MCP_TARGET_OTHER": _target_spec(_ATTACK_CMD),
        },
        approvals,
    )
    assert kept == {"KIROCREW_MCP_TARGET_SRV": _target_spec(_APPROVED_CMD)}
    assert dropped == ["OTHER", "SRV"]


def test_gatewayd_refuses_to_resolve_an_unapproved_target(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway import gatewayd

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_MCP_TARGET_SRV", _target_spec(_ATTACK_CMD, "-c", "id"))
    mod = _approval_module()
    if mod is not None:
        approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
        mod._write(mod.approvals_path(), mod._document(approvals, {}))

    assert gatewayd.env_target_resolver(_pool_key("srv")) is None


def test_gatewayd_resolves_an_approved_target(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway import gatewayd

    mod = _approval_module()
    assert mod is not None
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_MCP_TARGET_SRV", _target_spec(_APPROVED_CMD))
    approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    mod._write(mod.approvals_path(), mod._document(approvals, {}))

    target = gatewayd.env_target_resolver(_pool_key("srv"))
    assert target is not None and target[0] == _APPROVED_CMD


def test_gatewayd_fails_closed_without_a_store(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway import gatewayd

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_MCP_TARGET_SRV", _target_spec(_APPROVED_CMD))

    assert gatewayd.env_target_resolver(_pool_key("srv")) is None


@pytest.mark.asyncio
async def test_gateway_resolution_reads_approval_store_off_loop(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway import gatewayd

    resolve = getattr(gatewayd, "_resolve_target_off_loop", None)
    assert resolve is not None, "gateway target resolution must have an async offload boundary"
    mod = _approval_module()
    assert mod is not None
    loop_thread = threading.get_ident()
    reader_threads = []

    def load_snapshot(path=None):
        reader_threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return mod.LaunchApprovals()

    monkeypatch.setattr(mod, "load_approvals", load_snapshot)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_MCP_TARGET_SRV", _target_spec(_APPROVED_CMD))

    assert await resolve(gatewayd.env_target_resolver, _pool_key("srv")) is None
    assert len(reader_threads) == 1


@pytest.mark.asyncio
async def test_gateway_resolution_fails_closed_before_approval_store_exists(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway import gatewayd

    resolve = getattr(gatewayd, "_resolve_target_off_loop", None)
    assert resolve is not None, "gateway target resolution must have an async offload boundary"
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_MCP_TARGET_SRV", _target_spec(_APPROVED_CMD))

    assert await resolve(gatewayd.env_target_resolver, _pool_key("srv")) is None


@pytest.mark.asyncio
async def test_gateway_resolution_reloads_a_new_approval_without_restart(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway import gatewayd

    resolve = getattr(gatewayd, "_resolve_target_off_loop", None)
    assert resolve is not None, "gateway target resolution must have an async offload boundary"
    mod = _approval_module()
    assert mod is not None
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_MCP_TARGET_SRV", _target_spec(_APPROVED_CMD))
    key = _pool_key("srv")

    assert await resolve(gatewayd.env_target_resolver, key) is None

    approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    mod._write(mod.approvals_path(), mod._document(approvals, {}))

    target = await resolve(gatewayd.env_target_resolver, key)
    assert target is not None and target[0] == _APPROVED_CMD


@pytest.mark.asyncio
async def test_gateway_rechecks_approval_after_spawn_queue_wait(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from kiro_crew.mcp_gateway import admission as admission_mod
    from kiro_crew.mcp_gateway import gatewayd, host_budget
    from kiro_crew.mcp_gateway.pool import BackendPool

    mod = _approval_module()
    assert mod is not None
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_MCP_TARGET_SRV", _target_spec(_APPROVED_CMD))
    approved = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    revoked = False
    loads = 0
    loop_thread = threading.get_ident()

    def load_snapshot(path=None):
        nonlocal loads
        loads += 1
        assert threading.get_ident() != loop_thread
        return mod.LaunchApprovals() if revoked else approved

    monkeypatch.setattr(mod, "load_approvals", load_snapshot)
    spawned = AsyncMock(side_effect=AssertionError("spawned after approval revocation"))
    monkeypatch.setattr(gatewayd, "spawn_backend", spawned)
    pool = BackendPool(max_backends=2)
    admission = admission_mod.Admission(
        gate=admission_mod.SpawnGate(1),
        budget=host_budget.HostBudget(host_budget.HostBudgetLimits(max_procs=2)),
        initialize_timeout_secs=1.0,
        spawn_queue_wait_secs=30.0,
    )
    blocker = await admission.gate.acquire(label="blocker")
    task = asyncio.create_task(
        gatewayd._acquire_backend(
            pool,
            _pool_key("srv"),
            gatewayd.env_target_resolver,
            admission=admission,
        )
    )
    try:
        await async_wait_until(
            lambda: admission.gate.queued == 1, describe=lambda: admission.gate.queued
        )
        revoked = True
        blocker.release()
        with pytest.raises(gatewayd._TargetUnknown, match="approval changed"):
            await task
    finally:
        blocker.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    spawned.assert_not_awaited()
    assert loads == 2
    assert pool.resident_pending == 0
    assert admission.gate.in_flight == 0
    assert admission.budget.procs_in_use == 0


@pytest.mark.parametrize("contents", [None, "", "{}"])
def test_an_absent_or_empty_store_approves_nothing(tmp_path, contents):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    if contents is not None:
        store.write_text(contents)
    approvals = mod.load_approvals(store)
    _write_agent(tmp_path / "agents", {"srv": {"command": _APPROVED_CMD, "args": []}})

    target_env = _rewrite(tmp_path, frozenset({"srv"}), approvals)

    assert _targets_running(target_env, _APPROVED_CMD) == []
    assert approvals.refused == {"SRV": mod.REFUSED_UNAPPROVED}


def test_approval_store_is_inside_the_sealed_directory(tmp_path):
    mod = _approval_module()
    assert mod is not None

    assert mod.approvals_path(tmp_path) == tmp_path / "mcp-launch-approvals" / "approvals.json"


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_the_first_write_creates_the_sealed_directory_owner_only_under_any_umask(tmp_path):
    """The store's directory is born ``0700`` even under a permissive umask.

    The directory is the store's seal, and on a host that has not spawned a
    sandboxed process yet the first ``approve``/``save_pass`` is what creates
    it, so its mode cannot be left to the umask or to the sandbox's precreate.
    """
    import stat

    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "home" / "mcp-launch-approvals" / "approvals.json"
    launches = {
        "srv": [
            mod.ResolvedLaunch(
                mod.launch_fingerprint(_APPROVED_CMD, []),
                _APPROVED_CMD,
                (),
                frozenset({mod.env_fingerprint({})}),
            )
        ]
    }
    previous = os.umask(0o022)
    try:
        mod.approve(launches, path=store)
    finally:
        os.umask(previous)

    assert stat.S_IMODE(store.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "leaf",
    [
        "mcp-launch-approvals/approvals.json",
        "mcp/resolved/0123456789abcdef/record.json",
    ],
)
def test_agent_tools_cannot_write_the_launch_inputs(leaf):
    from kiro_crew.security.paths import is_sensitive_path, is_sensitive_write_path

    path = str(Path.home() / ".kiro" / "crew" / leaf)
    assert is_sensitive_write_path(path)
    # Write-protected, not hidden: reading them decides nothing.
    assert not is_sensitive_path(path)


def test_launch_approval_inputs_are_read_only_in_the_sandbox(tmp_path, monkeypatch):
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
    dirs, files = sandbox._sealable_absent_ceilings()
    approvals_dir = tmp_path / "mcp-launch-approvals"

    assert "mcp-launch-approvals" in sandbox._CREW_READONLY_LEAVES
    assert "mcp-launch-approvals" in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert "mcp-launch-approvals" in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    assert str(approvals_dir) in dirs
    assert not any(path.endswith("approvals.json") for path in files)
    assert "mcp/resolved" in sandbox._CREW_READONLY_LEAVES
    for leaf in ("mcp-gateway/agents", "mcp-gateway/stubs"):
        assert leaf not in sandbox._CREW_READONLY_LEAVES


@pytest.mark.parametrize("leaf", ["mcp-launch-approvals", "mcp/resolved"])
def test_launch_artifact_symlinks_are_refused_before_the_sandbox_mount(tmp_path, monkeypatch, leaf):
    from kiro_crew import sandbox

    target = tmp_path / leaf
    target.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(sandbox, "_sealable_absent_ceilings", lambda: ([str(target)], []))

    with pytest.raises(sandbox.SandboxCeilingUnsealable):
        sandbox._materialize_sealable_ceilings()


def test_nested_resolved_launch_overlap_uses_its_named_reason(tmp_path, monkeypatch):
    from kiro_crew import sandbox

    home = tmp_path / "crew"
    target = home / "mcp" / "resolved"
    target.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
    monkeypatch.setattr(sandbox.sys, "platform", "win32")

    reason = sandbox.delegated_workspace_exposes_sealed_target(str(target))

    assert reason is not None
    assert "sealed resolved MCP launches" in reason
    assert "executable" in reason


# ---------------------------------------------------------------------------
# The stub toggle approves the launch it resolves at the click
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["agent spec", "settings mcp.json"])
async def test_a_spec_edited_after_the_toggle_is_not_approved(tmp_path, toggle_home, where):
    _declare(toggle_home, where, _APPROVED_CMD, [])
    status, body = await _toggle_on()
    assert status == 200, body

    # An agent rewrites the server's launch before the next gateway start.
    _declare(toggle_home, where, _ATTACK_CMD, ["-c", "id"])
    target_env, approvals = _next_boot_rewrite(tmp_path, toggle_home)

    assert (
        _targets_running(target_env, _ATTACK_CMD) == []
    ), "the launch an agent wrote after the operator's click was approved"
    mod = _approval_module()
    assert approvals.refused.get("SRV") == mod.REFUSED_CHANGED


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["agent spec", "settings mcp.json"])
async def test_the_launch_resolved_at_the_toggle_is_approved(tmp_path, toggle_home, where):
    _declare(toggle_home, where, _APPROVED_CMD, [])
    status, body = await _toggle_on()
    assert status == 200, body

    mod = _approval_module()
    stored = mod.load_approvals()
    assert stored.admits_command(
        "SRV", rewriter.hash_command(_APPROVED_CMD, [])
    ), "the click recorded no approved launch for the server it stubbed"
    assert "approved_launches" not in body
    target_env, approvals = _next_boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD)
    assert approvals.refused == {}


def test_command_and_derived_env_are_approved_as_one_pair(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    admits_launch = getattr(mod.LaunchApprovals, "admits_launch", None)
    assert admits_launch is not None, "launch approvals must expose a pair check"

    env_a = mod.env_fingerprint({"MODE": "a"})
    env_b = mod.env_fingerprint({"MODE": "b"})
    launches = {
        "srv": [
            mod.ResolvedLaunch(
                mod.launch_fingerprint(_APPROVED_CMD, [], {"MODE": "a"}),
                _APPROVED_CMD,
                (),
                frozenset({env_a}),
            ),
            mod.ResolvedLaunch(
                mod.launch_fingerprint(_ATTACK_CMD, ["-c", "id"], {"MODE": "b"}),
                _ATTACK_CMD,
                ("-c", "id"),
                frozenset({env_b}),
            ),
        ]
    }
    store = tmp_path / "approvals.json"
    mod.approve(launches, path=store)
    stored = mod.load_approvals(store)
    command_a = rewriter.hash_command(_APPROVED_CMD, [])

    assert stored.admits_launch("SRV", command_a, env_a)
    assert not stored.admits_launch("SRV", command_a, env_b)

    from kiro_crew.mcp_gateway import gatewayd

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_MCP_TARGET_SRV", _target_spec(_APPROVED_CMD))
    key = _pool_key("srv")
    key = type(key)(
        **{
            **{field: getattr(key, field) for field in inspect.signature(type(key)).parameters},
            "effective_env_hash": env_b,
        }
    )
    assert gatewayd.env_target_resolver(key) is None


@pytest.mark.asyncio
async def test_status_reports_stored_refusal_without_resolving_again(toggle_home, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    mod = _approval_module()
    assert mod is not None
    approvals = mod.LaunchApprovals()
    approvals.stored_refused = {
        "SRV": {
            "name": "srv",
            "reason": mod.REFUSED_CHANGED,
            "commands": [[_ATTACK_CMD, "-c", "id"]],
            "envs": [[]],
            "expected_launch": "command-hash:env-hash",
        }
    }
    mod._write(mod.approvals_path(), mod._document(approvals, approvals.stored_refused))
    monkeypatch.setattr(
        mcp_mod.launch_resolve,
        "resolve_launches",
        lambda names: pytest.fail("status must not resolve agent specs"),
    )

    response = await mcp_mod.api_mcp_gateway_status(_stub_request({}))
    body = json.loads(response.body)

    assert body["launch_refused"] == {
        "srv": {
            "reason": mod.REFUSED_CHANGED,
            "commands": [[_ATTACK_CMD, "-c", "id"]],
            "envs": [[]],
            "complete": True,
            "expected_launch": "command-hash:env-hash",
        }
    }


@pytest.mark.asyncio
async def test_status_names_a_refused_share_by_its_configured_name(tmp_path, monkeypatch):
    """A slash-named share whose refusal is stored under its alias is still named."""
    from types import SimpleNamespace

    from kiro_crew.config import loader
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    mod = _approval_module()
    assert mod is not None
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    approvals = mod.LaunchApprovals()
    approvals.stored_refused = {
        "PLAYWRIGHT-MCP": {"name": "playwright-mcp", "reason": mod.REFUSED_UNAPPROVED},
        "GONE-MCP": {"name": "gone-mcp", "reason": mod.REFUSED_UNAPPROVED},
    }
    mod._write(mod.approvals_path(), mod._document(approvals, approvals.stored_refused))
    cfg = SimpleNamespace(
        mcp_gateway=SimpleNamespace(enabled=True, stub_servers=["npm:@playwright/mcp", "beta-mcp"])
    )
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))

    response = await mcp_mod.api_mcp_gateway_status(_stub_request({}))
    body = json.loads(response.body)

    assert body["share_refused"] == {"npm:@playwright/mcp": mod.REFUSED_UNAPPROVED}


@pytest.mark.asyncio
async def test_refused_display_argv_is_redacted_in_storage_and_status(tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    mod = _approval_module()
    assert mod is not None
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    approvals = mod.LaunchApprovals()
    secret = "sk-proj-" + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGH"
    fingerprint = mod.launch_fingerprint(_ATTACK_CMD, [f"--token={secret}"], {})

    assert not approvals.admit(
        "srv",
        fingerprint,
        launch=(_ATTACK_CMD, [f"--token={secret}"]),
        env={},
        derived_env_hash=mod.env_fingerprint({}),
    )
    approvals.full_pass = True
    assert mod.save_pass(approvals)
    stored = mod.approvals_path().read_text(encoding="utf-8")
    response = await mcp_mod.api_mcp_gateway_status(_stub_request({}))
    payload = response.body.decode("utf-8")

    assert secret not in stored
    assert secret not in payload
    assert "[REDACTED" in stored
    assert "[REDACTED" in payload


def test_refusal_display_argv_is_bounded_and_not_approvable(tmp_path):
    mod = _approval_module()
    assert mod is not None
    max_args = getattr(mod, "_MAX_DISPLAY_ARGS")
    budget = getattr(mod, "_MAX_DISPLAY_TOTAL_CHARS")
    huge_arg = "x" * 10_000
    args = [huge_arg] * 10_000
    env_hash = mod.env_fingerprint({})
    fingerprint = mod.launch_fingerprint(_ATTACK_CMD, args, {})
    approvals = mod.LaunchApprovals()

    assert not approvals.admit(
        "srv",
        fingerprint,
        launch=(_ATTACK_CMD, args),
        env={},
        derived_env_hash=env_hash,
    )
    approvals.full_pass = True
    store = tmp_path / "approvals.json"
    assert mod.save_pass(approvals, path=store)
    document = json.loads(store.read_text(encoding="utf-8"))
    refusal = document["refused"]["SRV"]

    assert len(refusal["commands"][0]) <= max_args
    assert sum(len(arg) for arg in refusal["commands"][0]) <= budget
    assert "partial" in refusal["commands"][0][-1]
    assert refusal["envs"] == [[]]
    assert refusal["complete"] is False
    assert "expected_launch" not in refusal

    mod.approve(
        {
            "srv": [
                mod.ResolvedLaunch(
                    fingerprint,
                    _ATTACK_CMD,
                    tuple(args),
                    frozenset({env_hash}),
                )
            ]
        },
        path=store,
    )
    stored = mod.load_approvals(store)
    assert stored.admits_launch("SRV", rewriter.hash_command(_ATTACK_CMD, args), env_hash)


#: One ``--skill-paths`` value: a comma-joined list of per-package skill
#: directories, held in a single argv entry, with further entries around it.
#: A server that passes its skill directories this way puts thousands of
#: characters in one entry while its whole argv stays small.
_LONG_SKILL_PATHS = ",".join(
    f"/home/u/pkgs/pkg{index:02d}-1.0/build-{index}/skills" for index in range(40)
)
_LONG_ARGV = [
    "--include-tools",
    "ToolA,ToolB,ToolC,ToolD",
    "--skill-paths",
    _LONG_SKILL_PATHS,
    "--skill-name-filter",
    "some-skill",
    "--agent-sop-filter",
    "*",
]


def test_a_long_argv_entry_within_the_total_budget_is_shown_whole_and_approvable(tmp_path):
    """One argv entry longer than a per-entry cap is still fully displayable.

    A server that passes every skill directory in one ``--skill-paths`` value
    meets a per-entry character cap that cuts the display of a launch whose
    whole argv is far smaller than the display budget. A cut display cannot be
    approved, so such a server stays unapprovable and every session starts its
    own copy.
    """
    mod = _approval_module()
    assert mod is not None
    budget = getattr(mod, "_MAX_DISPLAY_TOTAL_CHARS")
    assert len(_LONG_SKILL_PATHS) > 512, "the fixture must exceed a per-entry cap"
    assert sum(len(part) for part in (_ATTACK_CMD, *_LONG_ARGV)) < budget
    env_hash = mod.env_fingerprint({})
    fingerprint = mod.launch_fingerprint(_ATTACK_CMD, _LONG_ARGV, {})
    approvals = mod.LaunchApprovals()

    assert not approvals.admit(
        "srv",
        fingerprint,
        launch=(_ATTACK_CMD, _LONG_ARGV),
        env={},
        derived_env_hash=env_hash,
    )
    assert approvals.incomplete_identities == {}
    assert approvals.refused_identities("SRV")
    approvals.full_pass = True
    store = tmp_path / "approvals.json"
    assert mod.save_pass(approvals, path=store)

    refusal = mod.refused_servers(store)["srv"]
    assert refusal["commands"][0] == [_ATTACK_CMD, *_LONG_ARGV]
    assert refusal.get("complete") is not False
    assert refusal["expected_launch"]


def test_an_argv_over_the_total_budget_stays_unapprovable(tmp_path):
    """A launch too large to show in full fails closed, with the cut marked."""
    mod = _approval_module()
    assert mod is not None
    budget = getattr(mod, "_MAX_DISPLAY_TOTAL_CHARS")
    args = ["y" * (budget // 2) for _ in range(4)]
    env_hash = mod.env_fingerprint({})
    approvals = mod.LaunchApprovals()

    assert not approvals.admit(
        "srv",
        mod.launch_fingerprint(_ATTACK_CMD, args, {}),
        launch=(_ATTACK_CMD, args),
        env={},
        derived_env_hash=env_hash,
    )
    assert approvals.refused_identities("SRV") is None
    approvals.full_pass = True
    store = tmp_path / "approvals.json"
    assert mod.save_pass(approvals, path=store)

    refusal = mod.refused_servers(store)["srv"]
    assert sum(len(arg) for arg in refusal["commands"][0]) <= budget
    assert "partial" in refusal["commands"][0][-1]
    assert refusal["complete"] is False
    assert "expected_launch" not in refusal


def test_store_refuses_a_new_stem_at_the_server_record_cap(tmp_path, caplog):
    mod = _approval_module()
    assert mod is not None
    cap = getattr(mod, "_MAX_SERVER_RECORDS")
    env_hash = mod.env_fingerprint({})
    approvals = mod.LaunchApprovals()
    fingerprint = mod.launch_fingerprint(_APPROVED_CMD, [], {})

    for index in range(cap):
        assert approvals.admit(
            f"approved-{index:04d}",
            fingerprint,
            managed=True,
            launch=(_APPROVED_CMD, []),
            env={},
            derived_env_hash=env_hash,
        )
    assert not approvals.admit(
        "overflow-server",
        fingerprint,
        managed=True,
        launch=(_APPROVED_CMD, []),
        env={},
        derived_env_hash=env_hash,
    )
    overflow_stem = mod.target_stem("overflow-server")
    retained = (
        approvals.approved,
        approvals.approved_pairs,
        approvals.captured,
        approvals.captured_launches,
        approvals.names,
        approvals.launch_identities,
        approvals.refused_commands,
    )
    assert all(overflow_stem not in population for population in retained)
    assert "overflow-server" in caplog.text

    approvals.full_pass = True
    store = tmp_path / "approvals.json"
    assert mod.save_pass(approvals, path=store)
    document = json.loads(store.read_text(encoding="utf-8"))
    assert len(document["servers"]) == cap
    assert overflow_stem not in document["servers"]

    mod.approve(
        {
            "overflow-server": [
                mod.ResolvedLaunch(fingerprint, _APPROVED_CMD, (), frozenset({env_hash}))
            ]
        },
        path=store,
    )
    document = json.loads(store.read_text(encoding="utf-8"))
    assert len(document["servers"]) == cap
    assert overflow_stem not in document["servers"]


def test_oversized_store_display_fields_are_bounded_on_read(tmp_path):
    mod = _approval_module()
    assert mod is not None
    max_args = getattr(mod, "_MAX_DISPLAY_ARGS")
    budget = getattr(mod, "_MAX_DISPLAY_TOTAL_CHARS")
    max_name_chars = getattr(mod, "_MAX_NAME_CHARS")
    oversized = "z" * budget
    command = [oversized] * (max_args * 2)
    store = tmp_path / "approvals.json"
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": {},
                "refused": {
                    "SRV": {
                        "name": "n" * (max_name_chars * 2),
                        "reason": mod.REFUSED_UNAPPROVED,
                        "commands": [command],
                        "envs": [[]],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    refusal = mod.refused_servers(store)
    name, record = next(iter(refusal.items()))

    assert len(name) <= max_name_chars
    assert len(record["commands"][0]) <= max_args
    assert sum(len(arg) for arg in record["commands"][0]) <= budget
    assert "partial" in record["commands"][0][-1]
    assert record["envs"] == [[]]


def test_a_stem_past_the_name_bound_is_not_stored(tmp_path):
    mod = _approval_module()
    assert mod is not None
    max_name_chars = getattr(mod, "_MAX_NAME_CHARS")
    long_name = "s" * (max_name_chars + 1)
    env_hash = mod.env_fingerprint({})
    approvals = mod.LaunchApprovals()
    assert not approvals.admit(
        long_name,
        mod.launch_fingerprint(_APPROVED_CMD, [], {}),
        managed=True,
        launch=(_APPROVED_CMD, []),
        env={},
        derived_env_hash=env_hash,
    )
    assert not approvals.admit(
        "l" * (max_name_chars + 1),
        mod.launch_fingerprint(_ATTACK_CMD, ["-c", "id"], {}),
        launch=(_ATTACK_CMD, ["-c", "id"]),
        env={},
        derived_env_hash=env_hash,
    )
    retained = (
        approvals.approved,
        approvals.approved_pairs,
        approvals.captured,
        approvals.captured_launches,
        approvals.names,
        approvals.launch_identities,
        approvals.refused_commands,
    )
    assert all(mod.target_stem(long_name) not in population for population in retained)

    store = tmp_path / "approvals.json"
    oversized = {
        "version": 1,
        "servers": {mod.target_stem(long_name): {"fingerprints": ["a:b"], "pairs": ["a:b"]}},
        "refused": {"L" * (max_name_chars + 1): {"reason": mod.REFUSED_UNAPPROVED}},
    }
    store.write_text(json.dumps(oversized), encoding="utf-8")
    loaded = mod.load_approvals(store)
    assert loaded.approved == {}
    assert loaded.stored_refused == {}


def test_new_launch_is_refused_at_the_per_server_cap_and_expected_launch_survives(
    tmp_path,
):
    mod = _approval_module()
    assert mod is not None
    cap = getattr(mod, "_MAX_SERVER_LAUNCHES")
    env_hash = mod.env_fingerprint({})
    approvals = mod.LaunchApprovals()
    for index in range(cap):
        assert approvals.admit(
            "srv",
            mod.launch_fingerprint(_APPROVED_CMD, [str(index)], {}),
            managed=True,
            launch=(_APPROVED_CMD, [str(index)]),
            env={},
            derived_env_hash=env_hash,
        )
    assert not approvals.admit(
        "srv",
        mod.launch_fingerprint(_APPROVED_CMD, ["overflow"], {}),
        managed=True,
        launch=(_APPROVED_CMD, ["overflow"]),
        env={},
        derived_env_hash=env_hash,
    )
    assert approvals.refused["SRV"] == getattr(mod, "REFUSED_TOO_MANY")
    assert all(
        len(population["SRV"]) == cap
        for population in (
            approvals.approved,
            approvals.approved_pairs,
            approvals.captured,
            approvals.captured_launches,
            approvals.launch_identities,
        )
    )

    approvals.full_pass = True
    store = tmp_path / "approvals.json"
    assert mod.save_pass(approvals, path=store)
    document = json.loads(store.read_text(encoding="utf-8"))
    refusal = document["refused"]["SRV"]
    assert refusal["reason"] == mod.REFUSED_TOO_MANY
    assert len(refusal["expected_launch"].split(",")) == cap
    assert mod.refused_servers(store) == {}

    record = document["servers"]["SRV"]
    record["fingerprints"] = [f"{index:0129d}" for index in range(cap * 2)]
    record["pairs"] = [f"{index:0129d}" for index in range(cap * 2)] + ["x" * 10_000]
    store.write_text(json.dumps(document), encoding="utf-8")
    loaded = mod.load_approvals(store)
    assert len(loaded.approved["SRV"]) == cap
    assert len(loaded.approved_pairs["SRV"]) == cap


def test_operator_approval_refuses_a_launch_set_over_the_per_server_cap(tmp_path):
    mod = _approval_module()
    assert mod is not None
    cap = getattr(mod, "_MAX_SERVER_LAUNCHES")
    env_hash = mod.env_fingerprint({})
    launches = [
        mod.ResolvedLaunch(
            mod.launch_fingerprint(_APPROVED_CMD, [str(index)], {}),
            _APPROVED_CMD,
            (str(index),),
            frozenset({env_hash}),
        )
        for index in range(cap + 1)
    ]
    store = tmp_path / "approvals.json"

    mod.approve({"srv": launches}, path=store)

    document = json.loads(store.read_text(encoding="utf-8"))
    assert document["servers"] == {}
    assert document["refused"]["SRV"]["reason"] == getattr(mod, "REFUSED_TOO_MANY")


def test_stored_refusal_drops_expected_launch_when_command_count_mismatches(tmp_path):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": {},
                "refused": {
                    "SRV": {
                        "name": "srv",
                        "reason": mod.REFUSED_UNAPPROVED,
                        "commands": [[_ATTACK_CMD, "-c", "id"]],
                        "envs": [[]],
                        "expected_launch": "identity-a,identity-b",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    refusal = mod.refused_servers(store)["srv"]

    assert refusal["commands"] == [[_ATTACK_CMD, "-c", "id"]]
    assert "expected_launch" not in refusal


def test_stored_refusal_commands_are_bounded(tmp_path):
    mod = _approval_module()
    assert mod is not None
    cap = getattr(mod, "_MAX_SERVER_LAUNCHES")
    store = tmp_path / "approvals.json"
    commands = [[_ATTACK_CMD, str(index)] for index in range(cap + 2)]
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": {},
                "refused": {
                    "SRV": {
                        "name": "srv",
                        "reason": mod.REFUSED_UNAPPROVED,
                        "commands": commands,
                        "envs": [[] for _command in commands],
                        "expected_launch": ",".join(f"identity-{index}" for index in range(cap)),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    refusal = mod.refused_servers(store)["srv"]

    assert len(refusal["commands"]) == cap
    assert len(refusal["envs"]) == cap
    assert "expected_launch" not in refusal


def test_unchanged_argv_with_changed_env_renders_a_new_identity(tmp_path):
    mod = _approval_module()
    assert mod is not None
    before = {"MODE": "before"}
    after = {"MODE": "after"}
    command_hash = rewriter.hash_command(_APPROVED_CMD, [])
    before_identity = mod.launch_pair(
        command_hash,
        mod.env_fingerprint(rewriter._expand_env_map(before)),
    )
    approvals = mod.LaunchApprovals(
        approved={"SRV": {mod.launch_fingerprint(_APPROVED_CMD, [], before)}},
        approved_pairs={"SRV": {before_identity}},
        names={"SRV": "srv"},
    )
    _write_agent(
        tmp_path / "agents",
        {"srv": {"command": _APPROVED_CMD, "args": [], "env": after}},
    )

    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    approvals.full_pass = True
    store = tmp_path / "approvals.json"
    assert mod.save_pass(approvals, path=store)
    refusal = mod.refused_servers(store)["srv"]

    assert refusal["commands"] == [[_APPROVED_CMD]]
    assert refusal["envs"][0][0].startswith("MODE=<hidden sha256:")
    assert refusal["expected_launch"] != before_identity
    # This approval was recorded with no display, so no old launch is shown.
    assert "approved_commands" not in refusal
    assert "approved_envs" not in refusal


def test_a_placeholder_env_value_is_displayed_as_declared(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    monkeypatch.setenv("STUBPIN_HOST_SECRET", "resolved-host-value")
    _write_agent(
        tmp_path / "agents",
        {"srv": {"command": _APPROVED_CMD, "args": [], "env": {"TOKEN": "${STUBPIN_HOST_SECRET}"}}},
    )
    approvals = mod.LaunchApprovals()

    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    approvals.full_pass = True
    store = tmp_path / "approvals.json"
    assert mod.save_pass(approvals, path=store)
    refusal = mod.refused_servers(store)["srv"]

    assert refusal["envs"] == [["TOKEN=${STUBPIN_HOST_SECRET}"]]
    assert "resolved-host-value" not in store.read_text()


def test_an_identity_without_an_env_display_cannot_be_reapproved(tmp_path):
    mod = _approval_module()
    assert mod is not None
    env_hash = mod.env_fingerprint({})
    approvals = mod.LaunchApprovals()
    assert not approvals.admit(
        "srv",
        mod.launch_fingerprint(_ATTACK_CMD, ["-c", "id"], {}),
        launch=(_ATTACK_CMD, ["-c", "id"]),
        derived_env_hash=env_hash,
    )
    approvals.full_pass = True
    store = tmp_path / "approvals.json"
    assert mod.save_pass(approvals, path=store)

    refusal = mod.refused_servers(store)["srv"]
    assert refusal["commands"] == []
    assert refusal["envs"] == []
    assert "expected_launch" not in refusal


def test_refusal_envs_are_bounded_and_redacted(tmp_path):
    mod = _approval_module()
    assert mod is not None
    max_args = getattr(mod, "_MAX_DISPLAY_ARGS")
    budget = getattr(mod, "_MAX_DISPLAY_TOTAL_CHARS")
    secret = "sk-proj-" + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGH"
    env = {f"KEY_{index:03d}": "x" * budget for index in range(max_args + 4)}
    env["API_TOKEN"] = secret
    env_hash = mod.env_fingerprint(env)
    approvals = mod.LaunchApprovals()
    assert not approvals.admit(
        "srv",
        mod.launch_fingerprint(_ATTACK_CMD, [], env),
        launch=(_ATTACK_CMD, []),
        env=env,
        derived_env_hash=env_hash,
    )
    approvals.full_pass = True
    store = tmp_path / "approvals.json"
    assert mod.save_pass(approvals, path=store)

    refusal = mod.refused_servers(store)["srv"]
    displayed = refusal["envs"][0]
    assert len(displayed) <= max_args
    assert sum(len(pair) for pair in displayed) <= budget
    assert secret not in json.dumps(refusal)
    assert any("<hidden sha256:" in pair for pair in displayed)
    assert "partial" in displayed[-1]


@pytest.mark.asyncio
async def test_a_changed_launch_refusal_shows_the_readable_approved_launch(tmp_path, toggle_home):
    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["--safe"])
    assert (await _toggle_on())[0] == 200

    _declare(toggle_home, "agent spec", _ATTACK_CMD, ["-c", "id"])
    _target_env, refused = _next_boot_rewrite(tmp_path, toggle_home)
    assert refused.refused == {"SRV": mod.REFUSED_CHANGED}
    assert mod.save_pass(refused)
    refusal = mod.refused_servers()["srv"]

    assert refusal["commands"] == [[_ATTACK_CMD, "-c", "id"]]
    assert refusal["approved_commands"] == [[_APPROVED_CMD, "--safe"]]
    assert refusal["approved_envs"] == [[]]


@pytest.mark.asyncio
async def test_the_stored_approved_display_is_redacted(tmp_path, toggle_home):
    mod = _approval_module()
    assert mod is not None
    secret = "sk-proj-" + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGH"
    (toggle_home / "a.json").write_text(
        json.dumps(
            {
                "name": "a",
                "mcpServers": {
                    "srv": {"command": _APPROVED_CMD, "args": [], "env": {"API_TOKEN": secret}}
                },
            }
        )
    )
    assert (await _toggle_on())[0] == 200
    assert secret not in mod.approvals_path().read_text(encoding="utf-8")

    _declare(toggle_home, "agent spec", _ATTACK_CMD, ["-c", "id"])
    _target_env, refused = _next_boot_rewrite(tmp_path, toggle_home)
    assert mod.save_pass(refused)
    refusal = mod.refused_servers()["srv"]

    assert refusal["approved_commands"] == [[_APPROVED_CMD]]
    assert secret not in json.dumps(refusal)
    assert any("<hidden sha256:" in line for line in refusal["approved_envs"][0])


def test_opaque_env_values_are_hidden_in_live_and_persisted_displays(tmp_path):
    mod = _approval_module()
    assert mod is not None
    oauth_secret = "o" * 20
    api_secret = "m" * 20
    visible_library = "/opt/mcp/visible.so"
    env = {
        "OAUTH_TOKEN": oauth_secret,
        "MY_API_THING": api_secret,
        "LD_PRELOAD": visible_library,
    }
    env_hash = mod.env_fingerprint(env)
    launch = mod.ResolvedLaunch(
        mod.launch_fingerprint(_APPROVED_CMD, [], env),
        _APPROVED_CMD,
        (),
        frozenset({env_hash}),
        tuple(env.items()),
    )

    display = mod.display_launches("srv", [launch])
    rendered = json.dumps(display)
    assert oauth_secret not in rendered
    assert api_secret not in rendered
    assert f"LD_PRELOAD={visible_library}" in display["envs"][0]
    assert sum("<hidden sha256:" in pair for pair in display["envs"][0]) == 2

    store = tmp_path / "approvals.json"
    mod.approve({"srv": [launch]}, path=store)
    stored_text = store.read_text(encoding="utf-8")
    assert oauth_secret not in stored_text
    assert api_secret not in stored_text
    persisted = mod.load_approvals(store)
    pair = next(iter(launch.approval_identities))
    assert persisted.approved_displays["SRV"][pair][1] == display["envs"][0]


def test_a_stored_approved_display_is_bounded_on_read(tmp_path):
    mod = _approval_module()
    assert mod is not None
    max_args = getattr(mod, "_MAX_DISPLAY_ARGS")
    budget = getattr(mod, "_MAX_DISPLAY_TOTAL_CHARS")
    pair = mod.launch_pair(rewriter.hash_command(_APPROVED_CMD, []), mod.env_fingerprint({}))
    store = tmp_path / "approvals.json"
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": {
                    "SRV": {
                        "name": "srv",
                        "fingerprints": [mod.launch_fingerprint(_APPROVED_CMD, [], {})],
                        "pairs": [pair],
                        "displays": {
                            pair: {"command": ["z" * budget] * (max_args * 2), "env": []},
                            "not-an-approved-pair": {"command": [_ATTACK_CMD], "env": []},
                        },
                    }
                },
                "refused": {},
            }
        ),
        encoding="utf-8",
    )

    stored = mod.load_approvals(store)
    command, env = stored.approved_displays["SRV"][pair]

    assert list(stored.approved_displays["SRV"]) == [pair]
    assert len(command) <= max_args
    assert sum(len(arg) for arg in command) <= budget
    assert env == []
