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
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import os
import shlex
import sys
import threading
from pathlib import Path

import pytest

from kiro_crew import agent_discovery
from kiro_crew.mcp_gateway import rewriter
from kiro_crew.mcp_gateway.hashing import encode_target_args

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True

_APPROVED_CMD = sys.executable
_ATTACK_CMD = os.environ.get("COMSPEC") or ("cmd.exe" if os.name == "nt" else "/bin/sh")


def _target_spec(command: str, *args: str) -> str:
    return " ".join(shlex.quote(part) for part in (command, *args))


def _approval_module():
    try:
        return importlib.import_module("kiro_crew.mcp_gateway.launch_approval")
    except ImportError:
        return None


def _write_agent(source: Path, servers: dict) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "a.json").write_text(json.dumps({"name": "a", "mcpServers": servers}))


def _rewrite(tmp_path: Path, stub_servers: frozenset[str], approvals) -> dict[str, str]:
    kwargs = {}
    if "approvals" in inspect.signature(rewriter.rewrite_agents).parameters:
        kwargs["approvals"] = approvals
    _results, target_env = rewriter.rewrite_agents(
        source_dir=tmp_path / "agents",
        overlay_dir=tmp_path / "home" / "mcp-gateway" / "agents",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=stub_servers,
        **kwargs,
    )
    return target_env


def _approvals_for(mod, name: str, command: str, args: list[str]):
    """An approval store holding exactly one approved launch for *name*."""
    if mod is None:
        return None
    approvals = mod.LaunchApprovals()
    stem = mod.target_stem(name)
    fingerprint = mod.launch_fingerprint(command, args, {})
    approvals.approved[stem] = {fingerprint}
    approvals.approved_pairs[stem] = {
        mod.launch_pair(rewriter.hash_command(command, args), mod.env_fingerprint({}))
    }
    return approvals


def _targets_running(target_env: dict[str, str], command: str) -> list[str]:
    return [k for k, v in target_env.items() if shlex.split(v)[0] == command]


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


def _pool_key(server: str, command: str = _APPROVED_CMD, args: list[str] | None = None):
    from kiro_crew.mcp_gateway.pool import PoolKey

    params = inspect.signature(PoolKey).parameters
    values = {name: "" for name in params}
    values.update(
        server_name=server,
        command_args_hash=rewriter.hash_command(command, args or []),
        effective_env_hash=_approval_module().env_fingerprint({}),
    )
    return PoolKey(**values)


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
        for _ in range(100):
            await asyncio.sleep(0.005)
            if admission.gate.queued == 1:
                break
        assert admission.gate.queued == 1
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
    # The launch-approval INPUT is sealed via its read-only directory, so no file
    # under it is sealed individually. Match the launch-approval path specifically
    # rather than any ``*approvals.json``: an unrelated top-level secret leaf such
    # as ``app-unit-approvals.json`` is a sealed FILE by design and legitimately
    # appears in ``files``.
    assert not any(path.endswith("mcp-launch-approvals/approvals.json") for path in files)
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


def _stub_request(body: dict):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from aiohttp import web
    from body_stream_helpers import attach_body
    from dashboard_owner_helpers import owner_claims

    request = MagicMock(spec=web.Request)
    request.app = {"state": SimpleNamespace(_mcp_gateway_apply_stub=None)}
    attach_body(request, body)
    return owner_claims(request)


@pytest.fixture
def toggle_home(tmp_path, monkeypatch):
    """A config file, an approval store and an agents dir the toggle and the rewrite share."""
    import kiro_crew.config.loader as loader_mod
    import kiro_crew.config.paths as paths_mod
    from kiro_crew import agent
    from kiro_crew.config.paths import kiro_agents_dir
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(loader_mod, "config_path", lambda: cfg)
    monkeypatch.setattr(mcp_mod, "_local_overlay_section", lambda: {})
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    # The agent spec IS the rebuilt config here; rebuilding it would need the
    # whole managed-server install this hermetic home does not carry.
    monkeypatch.setattr(agent, "rebuild_agent_config", lambda *a, **k: None)
    # Per test: the settings file beside the agents dir is read too.
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: tmp_path / "kiro" / "agents")
    agents = kiro_agents_dir()
    assert agents == tmp_path / "kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    return agents


def _declare(agents: Path, where: str, command: str, args: list[str]) -> None:
    entry = {"command": command, "args": args}
    if where == "agent spec":
        (agents / "a.json").write_text(json.dumps({"name": "a", "mcpServers": {"srv": entry}}))
    else:
        (agents / "a.json").write_text(json.dumps({"name": "a", "mcpServers": {}}))
        settings = agents.parent / "settings" / "mcp.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"mcpServers": {"srv": entry}}))


def _next_boot_rewrite(tmp_path: Path, agents: Path):
    """The rewrite the next gateway start runs, against the persisted store."""
    mod = _approval_module()
    approvals = mod.load_approvals()
    _results, target_env = rewriter.rewrite_agents(
        source_dir=agents,
        overlay_dir=tmp_path / "boot" / "mcp-gateway" / "agents",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=frozenset({"srv"}),
        approvals=approvals,
    )
    return target_env, approvals


_AUTO_EXPECTED = object()


async def _toggle_on(name: str = "srv", *, expected_launch: object = _AUTO_EXPECTED):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    body = {"name": name, "stub": True}
    if expected_launch is _AUTO_EXPECTED:
        launches = await asyncio.to_thread(
            mcp_mod.launch_resolve.resolve_launches, [name], refresh_specs=False
        )
        if launches.get(name):
            expected_launch = mcp_mod.display_launches(name, launches[name]).get("expected_launch")
    if expected_launch is not None and expected_launch is not _AUTO_EXPECTED:
        body["expected_launch"] = expected_launch
    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request(body))
    return resp.status, json.loads(resp.body)


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


def _declare_two(agents: Path, first: list[str], second: list[str]) -> None:
    for agent_name, args in (("a", first), ("b", second)):
        entry = {"command": _ATTACK_CMD, "args": args}
        (agents / f"{agent_name}.json").write_text(
            json.dumps({"name": agent_name, "mcpServers": {"srv": entry}})
        )


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


@pytest.mark.asyncio
async def test_first_toggle_requires_the_displayed_launch(toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    response = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": True}))

    assert response.status == 409
    assert json.loads(response.body)["code"] == "expected_launch_required"


@pytest.mark.asyncio
async def test_launch_endpoint_returns_the_first_toggle_compare_and_set(toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    request = _stub_request({})
    request.query = {"name": "srv"}
    response = await mcp_mod.api_mcp_gateway_server_launch(request)
    body = json.loads(response.body)

    assert response.status == 200
    assert body["name"] == "srv"
    assert body["commands"] == [[_APPROVED_CMD]]
    assert body["envs"] == [[]]
    assert body["complete"] is True
    assert body["expected_launch"]


@pytest.mark.asyncio
async def test_partial_launch_display_cannot_be_approved(toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    padded = [str(index) for index in range(getattr(_approval_module(), "_MAX_DISPLAY_ARGS") + 1)]
    _declare(toggle_home, "agent spec", _APPROVED_CMD, padded)
    launches = await asyncio.to_thread(
        mcp_mod.launch_resolve.resolve_launches, ["srv"], refresh_specs=False
    )
    identities = {identity for launch in launches["srv"] for identity in launch.approval_identities}
    display = mcp_mod.display_launches("srv", launches["srv"])

    assert display["complete"] is False
    assert "expected_launch" not in display
    assert "partial" in display["commands"][0][-1]
    response = await mcp_mod.api_mcp_gateway_set_stub(
        _stub_request(
            {
                "name": "srv",
                "stub": True,
                "expected_launch": _approval_module().serialize_identities(identities),
            }
        )
    )
    assert response.status == 409
    assert json.loads(response.body)["code"] == "launch_display_incomplete"


@pytest.mark.asyncio
async def test_batch_toggle_on_requires_individual_launch_displays(toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    response = await mcp_mod.api_mcp_gateway_set_stub(
        _stub_request({"names": ["srv"], "stub": True})
    )

    assert response.status == 400
    assert json.loads(response.body)["code"] == "batch_stub_requires_individual"


@pytest.mark.asyncio
async def test_a_refusal_with_two_launch_identities_round_trips_through_reapprove(
    tmp_path, toggle_home
):
    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    _declare_two(toggle_home, ["-c", "id"], ["-c", "whoami"])
    _target_env, refused = _next_boot_rewrite(tmp_path, toggle_home)
    assert refused.refused == {"SRV": mod.REFUSED_CHANGED}
    assert mod.save_pass(refused)
    refusal = mod.refused_servers()["srv"]
    expected_launch = refusal["expected_launch"]
    assert len(expected_launch.split(",")) == 2
    assert sorted(refusal["commands"]) == sorted(
        [
            [_ATTACK_CMD, "-c", "id"],
            [_ATTACK_CMD, "-c", "whoami"],
        ]
    )
    assert refusal["envs"] == [[], []]

    status, body = await _toggle_on(expected_launch=expected_launch)

    assert status == 200, body
    stored = mod.load_approvals()
    assert stored.stored_refused == {}
    assert stored.admits_command("SRV", rewriter.hash_command(_ATTACK_CMD, ["-c", "id"]))
    assert stored.admits_command("SRV", rewriter.hash_command(_ATTACK_CMD, ["-c", "whoami"]))


@pytest.mark.asyncio
async def test_a_changed_launch_identity_set_still_rejects_reapprove(tmp_path, toggle_home):
    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    _declare_two(toggle_home, ["-c", "id"], ["-c", "whoami"])
    _target_env, refused = _next_boot_rewrite(tmp_path, toggle_home)
    assert mod.save_pass(refused)
    expected_launch = mod.refused_servers()["srv"]["expected_launch"]
    _declare_two(toggle_home, ["-c", "id"], ["-c", "uname"])

    status, body = await _toggle_on(expected_launch=expected_launch)

    assert status == 409
    assert body["code"] == "launch_changed_since_display"


@pytest.mark.asyncio
async def test_toggle_reads_the_refusal_store_off_the_event_loop(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    event_loop_thread = threading.get_ident()
    real_refused_servers = mcp_mod.refused_servers

    def _checked_refused_servers():
        assert threading.get_ident() != event_loop_thread
        return real_refused_servers()

    monkeypatch.setattr(mcp_mod, "refused_servers", _checked_refused_servers)
    status, body = await _toggle_on()

    assert status == 200, body


def test_removed_refusal_fields_are_not_loaded_or_emitted(tmp_path):
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
                        "reason": mod.REFUSED_CHANGED,
                        "command": [_ATTACK_CMD],
                        "approved_command": [_APPROVED_CMD],
                        "refused_at": "2020-01-02T03:04:05Z",
                        "commands": [[_ATTACK_CMD]],
                        "envs": [[]],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    mod._write(
        store, mod._document(mod.load_approvals(store), mod.load_approvals(store).stored_refused)
    )
    document = json.loads(store.read_text(encoding="utf-8"))
    refusal = mod.refused_servers(store)["srv"]

    assert not {"command", "approved_command", "refused_at"} & document["refused"]["SRV"].keys()
    assert refusal == {
        "reason": mod.REFUSED_CHANGED,
        "commands": [[_ATTACK_CMD]],
        "envs": [[]],
        "complete": True,
    }


@pytest.mark.parametrize("full_pass", [True, False])
def test_an_approval_landing_before_deferred_save_suppresses_that_refusal(tmp_path, full_pass):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    env_hash = mod.env_fingerprint({})
    fingerprint = mod.launch_fingerprint(_ATTACK_CMD, ["-c", "id"], {})
    snapshot = mod.LaunchApprovals()
    assert not snapshot.admit(
        "srv",
        fingerprint,
        launch=(_ATTACK_CMD, ["-c", "id"]),
        env={},
        derived_env_hash=env_hash,
    )
    snapshot.full_pass = full_pass
    launch = mod.ResolvedLaunch(
        fingerprint,
        _ATTACK_CMD,
        ("-c", "id"),
        frozenset({env_hash}),
    )

    mod.approve({"srv": [launch]}, path=store)
    assert not mod.save_pass(snapshot, path=store)

    assert mod.refused_servers(store) == {}
    stored = mod.load_approvals(store)
    assert stored.admits_launch("SRV", rewriter.hash_command(_ATTACK_CMD, ["-c", "id"]), env_hash)


_GATEWAY_LAUNCH_FIXTURE_EXEMPTIONS = frozenset(
    {
        "test_adaptive_controller.py",
        "test_mcp_gateway_cluster_fixes.py",
        "test_mcp_gateway_control_plane_repair.py",
        "test_mcp_gateway_shutdown_and_env.py",
        "test_mcp_gateway_spawn_gate.py",
        "test_mcp_gateway_stub_session_token.py",
        "test_mcp_gatewayd_coverage.py",
        "test_runloop_integration.py",
    }
)


def test_gateway_spawn_and_env_tests_choose_launch_approval_fixture():
    def sensitive(name: str) -> bool:
        return "spawn" in name or name == "env_target_resolver" or name.startswith("_declared_env")

    offenders = []
    for path in Path(__file__).parent.glob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        if "gatewayd" not in text or not any(
            token in text for token in ("spawn", "declared_env", "env_target_resolver")
        ):
            continue
        tree = ast.parse(text)
        module_aliases = set()
        imported_symbols = set()
        enforced = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.mcp_gateway":
                module_aliases.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "gatewayd"
                )
            elif (
                isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.mcp_gateway.gatewayd"
            ):
                imported_symbols.update(alias.name for alias in node.names if sensitive(alias.name))
            elif isinstance(node, ast.Assign):
                enforced = enforced or any(
                    isinstance(target, ast.Name)
                    and target.id == "ENFORCE_LAUNCH_APPROVAL"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is True
                    for target in node.targets
                )
        imported_symbols.update(
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
            and sensitive(node.attr)
        )
        if (
            imported_symbols
            and not enforced
            and path.name not in _GATEWAY_LAUNCH_FIXTURE_EXEMPTIONS
        ):
            offenders.append(path.name)
    assert offenders == []


def test_gatewayd_stems_share_the_approval_store_codec():
    mod = _approval_module()
    assert mod is not None
    codec = getattr(mod, "target_env_stem", None)
    assert codec is not None, "launch_approval must define the single target-stem codec"
    from kiro_crew.mcp_gateway import gatewayd

    env = {
        "KIROCREW_MCP_TARGET_SRV": "x",
        "MC_MCP_TARGET_OLD__abc": "x",
        "KIROCREW_MCP_TARGET_": "x",
        "OTHER": "x",
    }
    assert gatewayd.resolvable_target_stems(env) == ["OLD", "SRV"]
    assert [codec(k) for k in env] == ["SRV", "OLD", None, None]


@pytest.mark.asyncio
async def test_a_toggle_with_no_launch_to_approve_changes_nothing(tmp_path, toggle_home):
    (toggle_home / "a.json").write_text(json.dumps({"name": "a", "mcpServers": {}}))

    status, body = await _toggle_on()

    assert status == 409
    assert body["code"] == "launch_unresolved"
    assert body["unresolved"] == ["srv"]
    written = json.loads((tmp_path / "config.json").read_text()).get("mcp_gateway", {})
    assert "srv" not in (written.get("stub_overrides") or {})
    assert "srv" not in (written.get("stub_servers") or [])
    assert not _approval_module().approvals_path().exists()


def test_an_over_record_cap_stem_is_absent_from_every_structure(tmp_path):
    mod = _approval_module()
    assert mod is not None
    cap = getattr(mod, "_MAX_SERVER_RECORDS")
    env_hash = mod.env_fingerprint({})
    approvals = mod.LaunchApprovals()
    for index in range(cap):
        assert not approvals.admit(
            f"refused-{index:04d}",
            mod.launch_fingerprint(_ATTACK_CMD, [str(index)], {}),
            launch=(_ATTACK_CMD, [str(index)]),
            env={},
            derived_env_hash=env_hash,
        )
    assert not approvals.admit(
        "overflow",
        mod.launch_fingerprint(_ATTACK_CMD, ["overflow"], {}),
        launch=(_ATTACK_CMD, ["overflow"]),
        env={},
        derived_env_hash=env_hash,
    )
    _kept, dropped = mod.filter_target_env(
        {"KIROCREW_MCP_TARGET_OVERFLOW": _target_spec(_ATTACK_CMD, "-c", "id")}, approvals
    )
    assert dropped == ["OVERFLOW"]
    populations = (
        approvals.approved,
        approvals.approved_pairs,
        approvals.names,
        approvals.captured,
        approvals.captured_launches,
        approvals.refused,
        approvals.refused_commands,
        approvals.launch_identities,
        approvals.stored_refused,
    )
    assert all("OVERFLOW" not in population for population in populations)
    assert len(approvals.refused) == cap

    approvals.full_pass = True
    store = tmp_path / "approvals.json"
    assert mod.save_pass(approvals, path=store)
    document = json.loads(store.read_text(encoding="utf-8"))
    assert len(document["refused"]) == cap
    assert "OVERFLOW" not in document["refused"]

    later = mod.LaunchApprovals()
    assert not later.admit(
        "later",
        mod.launch_fingerprint(_ATTACK_CMD, ["later"], {}),
        launch=(_ATTACK_CMD, ["later"]),
        env={},
        derived_env_hash=env_hash,
    )
    mod.save_pass(later, path=store)
    document = json.loads(store.read_text(encoding="utf-8"))
    assert len(document["refused"]) == cap
    assert "LATER" not in document["refused"]


@pytest.mark.asyncio
async def test_a_toggle_whose_launches_exceed_the_cap_changes_nothing(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.mcp_gateway import launch_resolve

    mod = _approval_module()
    cap = getattr(mod, "_MAX_SERVER_LAUNCHES")
    for index in range(cap + 1):
        entry = {"command": _APPROVED_CMD, "args": [str(index)]}
        (toggle_home / f"a{index:03d}.json").write_text(
            json.dumps({"name": f"a{index:03d}", "mcpServers": {"srv": entry}})
        )
    resolved = launch_resolve.resolve_launches(["srv"], refresh_specs=False)
    assert "srv" in getattr(resolved, "over_cap", ())
    assert "srv" not in resolved

    events = []
    monkeypatch.setattr(
        mcp_mod,
        "sel",
        lambda: type("_Sel", (), {"log_api_access": lambda _self, **kw: events.append(kw)})(),
    )

    status, body = await _toggle_on()

    assert status == 409
    assert body["code"] == "launch_over_cap"
    assert body["over_cap"] == ["srv"]
    written = json.loads((tmp_path / "config.json").read_text()).get("mcp_gateway", {})
    assert "srv" not in (written.get("stub_overrides") or {})
    assert "srv" not in (written.get("stub_servers") or [])
    assert not _approval_module().approvals_path().exists()
    assert [(e["operation"], e["outcome"]) for e in events] == [
        ("mcp_stub_rejected_launch_over_cap", "denied")
    ]


@pytest.mark.asyncio
async def test_a_toggle_at_the_store_record_cap_changes_neither_store(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    approvals = mod.LaunchApprovals()
    env_hash = mod.env_fingerprint({})
    for index in range(getattr(mod, "_MAX_SERVER_RECORDS")):
        name = f"kept-{index:04d}"
        stem = mod.target_stem(name)
        args = [str(index)]
        fingerprint = mod.launch_fingerprint(_APPROVED_CMD, args, {})
        approvals.approved[stem] = {fingerprint}
        approvals.approved_pairs[stem] = {
            mod.launch_pair(rewriter.hash_command(_APPROVED_CMD, args), env_hash)
        }
        approvals.names[stem] = name
    store = mod.approvals_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    mod._write(store, mod._document(approvals, {}))
    config = tmp_path / "config.json"
    mcp_mod.KiroCrewConfig.load()
    before_config = config.read_bytes()
    before_store = store.read_bytes()
    events = []
    monkeypatch.setattr(
        mcp_mod,
        "sel",
        lambda: type("_Sel", (), {"log_api_access": lambda _self, **kw: events.append(kw)})(),
    )

    status, body = await _toggle_on()

    assert status == 409
    assert body["code"] == "launch_over_cap"
    assert body["over_cap"] == ["srv"]
    assert config.read_bytes() == before_config
    assert store.read_bytes() == before_store
    assert [(event["operation"], event["outcome"]) for event in events] == [
        ("mcp_stub_rejected_launch_over_cap", "denied")
    ]


@pytest.mark.asyncio
async def test_an_approval_write_failure_does_not_admit_the_launch(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    monkeypatch.setattr(mcp_mod, "approve", lambda launches: (_ for _ in ()).throw(OSError()))

    status, body = await _toggle_on()

    assert status == 503
    assert body["code"] == "approval_write_failed"
    target_env, approvals = _next_boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD) == []
    assert approvals.refused == {"SRV": _approval_module().REFUSED_UNAPPROVED}


@pytest.mark.asyncio
async def test_reapprove_rejects_a_launch_changed_after_it_was_shown(
    tmp_path, toggle_home, monkeypatch
):
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    audit = MagicMock()
    monkeypatch.setattr(mcp_mod, "sel", lambda: audit)
    _declare(toggle_home, "agent spec", _ATTACK_CMD, ["-c", "id"])
    _target_env, refused = _next_boot_rewrite(tmp_path, toggle_home)
    assert refused.refused == {"SRV": mod.REFUSED_CHANGED}
    assert mod.save_pass(refused)
    expected_launch = mod.refused_servers()["srv"]["expected_launch"]
    approval_before = mod.approvals_path().read_bytes()
    config_before = (tmp_path / "config.json").read_bytes()

    # The operator still sees the refused shell command, but an agent changes the
    # declaration before the click reaches the handler.
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    status, body = await _toggle_on(expected_launch=expected_launch)

    assert status == 409
    assert body["code"] == "launch_changed_since_display"
    event = audit.log_api_access.call_args.kwargs
    assert event["operation"] == "mcp_stub_rejected_launch_changed_since_display"
    assert event["outcome"] == "denied"
    assert event["resources"] == "code=launch_changed_since_display names=srv"
    assert mod.approvals_path().read_bytes() == approval_before
    assert (tmp_path / "config.json").read_bytes() == config_before


@pytest.mark.asyncio
async def test_reapprove_matching_the_shown_launch_replaces_the_approval(tmp_path, toggle_home):
    mod = _approval_module()
    assert mod is not None
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200

    _declare(toggle_home, "agent spec", _ATTACK_CMD, ["-c", "id"])
    _target_env, refused = _next_boot_rewrite(tmp_path, toggle_home)
    assert refused.refused == {"SRV": mod.REFUSED_CHANGED}
    assert mod.save_pass(refused)
    expected_launch = mod.refused_servers()["srv"]["expected_launch"]

    status, body = await _toggle_on(expected_launch=expected_launch)

    assert status == 200, body
    stored = mod.load_approvals()
    assert stored.stored_refused == {}
    assert stored.admits_command("SRV", rewriter.hash_command(_ATTACK_CMD, ["-c", "id"]))
    assert not stored.admits_command("SRV", rewriter.hash_command(_APPROVED_CMD, []))


def test_restore_keeps_a_reapproval_that_lands_after_revoke(tmp_path):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    env_hash = mod.env_fingerprint({})
    old_launch = mod.ResolvedLaunch(
        mod.launch_fingerprint(_APPROVED_CMD, ["old"], {}),
        _APPROVED_CMD,
        ("old",),
        frozenset({env_hash}),
    )
    new_launch = mod.ResolvedLaunch(
        mod.launch_fingerprint(_APPROVED_CMD, ["new"], {}),
        _APPROVED_CMD,
        ("new",),
        frozenset({env_hash}),
    )

    mod.approve({"srv": [old_launch]}, path=store)
    snapshot = mod.revoke(["srv"], path=store)
    mod.restore(snapshot, path=store)
    restored = mod.load_approvals(store)
    assert restored.admits_command("SRV", rewriter.hash_command(_APPROVED_CMD, ["old"]))

    snapshot = mod.revoke(["srv"], path=store)
    mod.approve({"srv": [new_launch]}, path=store)
    mod.restore(snapshot, path=store)
    current = mod.load_approvals(store)
    old_hash = rewriter.hash_command(_APPROVED_CMD, ["old"])
    new_hash = rewriter.hash_command(_APPROVED_CMD, ["new"])
    new_pair = next(iter(new_launch.approval_identities))
    assert current.admits_command("SRV", new_hash)
    assert not current.admits_command("SRV", old_hash)
    assert current.approved_displays["SRV"][new_pair][0] == [_APPROVED_CMD, "new"]


@pytest.mark.asyncio
async def test_turning_the_stub_off_forgets_the_approval(tmp_path, toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200
    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))
    assert resp.status == 200

    assert _approval_module().load_approvals().approved == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["config_read", "config_write", "config_lock", "section_not_object"],
)
async def test_stub_off_restores_approval_when_config_does_not_commit(
    tmp_path, toggle_home, monkeypatch, failure
):
    import kiro_crew.config.loader as loader_mod
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200
    approval_mod = _approval_module()
    store_before = approval_mod.approvals_path().read_bytes()
    config_path = tmp_path / "config.json"
    if failure == "section_not_object":
        config_path.write_text(json.dumps({"mcp_gateway": "invalid"}))
    config_before = config_path.read_bytes()
    errors = {
        "config_read": loader_mod.ConfigReadError("unreadable"),
        "config_write": loader_mod.ConfigWriteRefused("refused"),
        "config_lock": OSError("locked"),
    }
    if failure in errors:
        error = errors[failure]

        def _fail(*args, **kwargs):
            raise error

        monkeypatch.setattr(loader_mod, "update_config_locked", _fail)

    response = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))

    assert response.status != 200
    assert config_path.read_bytes() == config_before
    assert approval_mod.approvals_path().read_bytes() == store_before


@pytest.mark.asyncio
async def test_stub_off_revocation_failure_preserves_config_and_approval(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert (await _toggle_on())[0] == 200
    approval_mod = _approval_module()
    stored_before = approval_mod.load_approvals()
    config_path = tmp_path / "config.json"
    config_before = config_path.read_bytes()
    monkeypatch.setattr(
        mcp_mod,
        "revoke",
        lambda names: (_ for _ in ()).throw(OSError("approval store unavailable")),
    )

    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "approval_write_failed"
    assert config_path.read_bytes() == config_before
    stored_after = approval_mod.load_approvals()
    assert stored_after.approved == stored_before.approved
    assert stored_after.approved_pairs == stored_before.approved_pairs


def test_boot_persists_launch_state_only_after_process_readiness(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    cfg = KiroCrewConfig()
    cfg.mcp_gateway.stub_servers = ["srv"]
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    order: list[str] = []

    async def _start() -> bool:
        order.append("bind")
        return True

    manager = MagicMock()
    manager.start = AsyncMock(side_effect=_start)

    def _save(_approvals) -> bool:
        order.append("persist")
        return True

    async def _run() -> None:
        await orch._init_mcp_gateway()
        assert order == ["bind"]
        persist = next(
            task
            for task in orch._background_tasks
            if task.get_name() == "mcp-launch-approval-persist"
        )
        orch._mcp_launch_approval_ready.set()
        await persist

    with (
        patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
        patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
        patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
        patch("kiro_crew.slack.gateway.save_pass", side_effect=_save),
    ):
        asyncio.run(_run())

    assert order == ["bind", "persist"]


def test_gateway_rewrite_and_target_filter_share_a_maintenance_worker(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    cfg = KiroCrewConfig()
    cfg.mcp_gateway.stub_servers = ["srv"]
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    manager = MagicMock()
    manager.start = AsyncMock(return_value=False)
    event_loop_thread = threading.get_ident()
    calls: list[tuple[str, int]] = []

    def _rewrite(**_kwargs):
        calls.append(("rewrite", threading.get_ident()))
        return None, {}

    def _filter(target_env, _approvals):
        calls.append(("filter", threading.get_ident()))
        return target_env, []

    with (
        patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
        patch("kiro_crew.slack.gateway.rewrite_agents", side_effect=_rewrite),
        patch("kiro_crew.slack.gateway.filter_target_env", side_effect=_filter),
        patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
    ):
        asyncio.run(orch._init_mcp_gateway())

    assert [name for name, _thread in calls] == ["rewrite", "filter"]
    assert calls[0][1] == calls[1][1]
    assert calls[0][1] != event_loop_thread


def test_the_toggle_resolves_with_the_inputs_the_gateway_start_rewrites_with(tmp_path):
    """One derivation of the rewrite inputs, or the click approves a different launch."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    resolve = importlib.import_module("kiro_crew.mcp_gateway.launch_resolve")
    cfg = KiroCrewConfig()
    cfg.mcp_gateway.stub_servers = ["srv"]
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    manager = MagicMock()
    manager.start = AsyncMock(return_value=False)
    with (
        patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
        patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})) as rw,
        patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager),
    ):
        asyncio.run(orch._init_mcp_gateway())

    called = {k: v for k, v in rw.call_args.kwargs.items() if k != "approvals"}
    assert called == resolve.rewrite_kwargs(cfg, frozenset({"srv"}))


def test_seeding_routes_the_name_without_approving_its_launch(tmp_path, toggle_home):
    from kiro_crew.mcp_gateway import verdict_cache as vc
    from kiro_crew.mcp_gateway.seed import SeedPlan, apply_seed

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    assert apply_seed(SeedPlan(add_stub=("srv",)), {}, vc.VerdictCache(vc.cache_path(tmp_path)))

    target_env, approvals = _next_boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD) == []
    assert approvals.refused == {"SRV": _approval_module().REFUSED_UNAPPROVED}
    assert _approval_module().load_approvals().approved == {}


def test_a_warm_boot_with_approvals_is_served_from_the_rewrite_cache(tmp_path):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    approvals = _approvals_for(mod, "srv", _APPROVED_CMD, [])
    mod._write(store, mod._document(approvals, {}))
    _write_agent(tmp_path / "agents", {"srv": {"command": _APPROVED_CMD, "args": []}})

    first = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), first)
    assert first.full_pass
    mod.save_pass(first, path=store)

    again = mod.load_approvals(store)
    fingerprint = tmp_path / "home" / "mcp-gateway" / "agents" / rewriter._FINGERPRINT_NAME
    stored = json.loads(fingerprint.read_text())
    assert stored["inputs"]["launch_approvals"] == again.digest()
    target_env = _rewrite(tmp_path, frozenset({"srv"}), again)

    assert not again.full_pass, "a boot over unchanged inputs and approvals rewrote everything"
    assert _targets_running(target_env, _APPROVED_CMD)


@pytest.mark.asyncio
async def test_re_approving_a_running_server_keeps_its_declared_env_forwarded(
    tmp_path, toggle_home
):
    from kiro_crew.mcp_gateway import gatewayd
    from kiro_crew.mcp_gateway.hashing import hash_effective_env

    declared = {"FOO": "bar"}
    (toggle_home / "a.json").write_text(
        json.dumps(
            {
                "name": "a",
                "mcpServers": {"srv": {"command": _APPROVED_CMD, "args": [], "env": declared}},
            }
        )
    )
    assert (await _toggle_on())[0] == 200
    # The gateway start the running daemon came from: it writes the env sidecar
    # gatewayd reads at cold spawn and derives the env it may forward.
    mod = _approval_module()
    approvals = mod.load_approvals()
    rewriter.rewrite_agents(
        source_dir=toggle_home,
        overlay_dir=rewriter.resolve_overlay_dir(),
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=frozenset({"srv"}),
        approvals=approvals,
    )
    mod.save_pass(approvals)

    # The operator turns the stub on again; the broker is not restarted.
    assert (await _toggle_on())[0] == 200

    identity_keys = rewriter.pool_identity_env_keys()
    key = _pool_key("srv")
    key = type(key)(
        **{
            **{f: getattr(key, f) for f in inspect.signature(type(key)).parameters},
            "agent_name": "a",
            "effective_env_hash": hash_effective_env(declared, identity_keys=identity_keys),
        }
    )
    assert (
        gatewayd._declared_env_pairs(key, identity_keys) == declared
    ), "the next cold spawn after the toggle would start the backend without its declared env"


def _approve_as_the_toggle_does(tmp_path: Path, mod, store: Path) -> None:
    """Resolve what ``srv`` runs now and approve exactly that, as the MCP page does."""
    probe = mod.LaunchApprovals(probe=True)
    _rewrite(tmp_path, frozenset({"srv"}), probe)
    launches = list(probe.captured_launches.get("SRV", {}).values())
    assert launches, "the probe pass resolved no launch for srv"
    mod.approve({"srv": launches}, path=store)


def _rewrite_and_persist(tmp_path: Path, mod, store: Path):
    """One gateway-start pass against the stored approvals, persisted after."""
    approvals = mod.load_approvals(store)
    target_env = _rewrite(tmp_path, frozenset({"srv"}), approvals)
    mod.save_pass(approvals, path=store)
    return approvals, target_env


def _placeholder_agent(tmp_path: Path, value_ref: str = "${KC_TEST_TOKEN}") -> None:
    _write_agent(
        tmp_path / "agents",
        {"srv": {"command": _APPROVED_CMD, "args": ["-V"], "env": {"TOKEN": value_ref}}},
    )


def test_a_changed_placeholder_value_keeps_the_approval(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    monkeypatch.setenv("KC_TEST_TOKEN", "rotated")
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)

    assert approvals.refused == {}
    assert _targets_running(target_env, _APPROVED_CMD)
    stored = mod.load_approvals(store)
    command_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])
    rotated = mod.env_fingerprint({"TOKEN": "rotated"})
    first = mod.env_fingerprint({"TOKEN": "first"})
    # gatewayd checks the pair the store holds, so the new expansion must be there
    # and the one it replaced must not linger.
    assert stored.admits_launch("SRV", command_hash, rotated)
    assert not stored.admits_launch("SRV", command_hash, first)
    assert stored.stored_refused == {}
    # The "Approved before" view keeps a readable display of the live pair.
    displays = stored.approved_displays.get("SRV", {})
    assert mod.launch_pair(command_hash, rotated) in displays
    assert "TOKEN=${KC_TEST_TOKEN}" in displays[mod.launch_pair(command_hash, rotated)][1]


def test_a_failed_sidecar_commit_does_not_persist_a_rebind(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)
    command_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])
    first = mod.env_fingerprint({"TOKEN": "first"})
    rotated = mod.env_fingerprint({"TOKEN": "rotated"})

    monkeypatch.setenv("KC_TEST_TOKEN", "rotated")
    real_commit = rewriter._SidecarLedger.commit
    monkeypatch.setattr(rewriter._SidecarLedger, "commit", lambda self: False)
    approvals = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    rebind_incomplete = getattr(approvals, "rebind_incomplete", None)
    assert rebind_incomplete is True, (
        "LaunchApprovals exposes no rebind_incomplete set by a pass whose "
        "environment sidecar did not publish"
    )
    mod.save_pass(approvals, path=store)

    stored = mod.load_approvals(store)
    assert stored.admits_launch("SRV", command_hash, first)
    assert not stored.admits_launch("SRV", command_hash, rotated)

    monkeypatch.setattr(rewriter._SidecarLedger, "commit", real_commit)
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)
    assert _targets_running(target_env, _APPROVED_CMD)
    stored = mod.load_approvals(store)
    assert stored.admits_launch("SRV", command_hash, rotated)
    assert not stored.admits_launch("SRV", command_hash, first)


def test_a_rebind_is_audited_as_an_approved_launch_not_a_managed_one(tmp_path, monkeypatch, caplog):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    monkeypatch.setenv("KC_TEST_TOKEN", "rotated")
    caplog.clear()
    with caplog.at_level("WARNING"):
        _rewrite_and_persist(tmp_path, mod, store)

    audit = [r.getMessage() for r in caplog.records if "recorded" in r.getMessage()]
    assert audit, "the save wrote the rebound pair without an audit line"
    # The operator approved this declaration; calling it managed states the
    # opposite provenance of what was admitted.
    assert not any("managed launch" in line for line in audit), audit
    assert any("${VAR} value changed" in line for line in audit), audit


def test_a_changed_declared_env_text_is_still_refused(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    monkeypatch.setenv("KC_OTHER", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    # Same expansion, different declared text: the agent-writable part moved.
    _placeholder_agent(tmp_path, "${KC_OTHER}")
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)

    assert approvals.refused == {"SRV": mod.REFUSED_CHANGED}
    assert not _targets_running(target_env, _APPROVED_CMD)


def test_a_changed_command_with_a_placeholder_is_still_refused(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    monkeypatch.setenv("KC_TEST_TOKEN", "rotated")
    _write_agent(
        tmp_path / "agents",
        {
            "srv": {
                "command": _ATTACK_CMD,
                "args": ["-c", "id"],
                "env": {"TOKEN": "${KC_TEST_TOKEN}"},
            }
        },
    )
    approvals, target_env = _rewrite_and_persist(tmp_path, mod, store)

    assert approvals.refused == {"SRV": mod.REFUSED_CHANGED}
    assert _targets_running(target_env, _ATTACK_CMD) == []


def test_repeated_placeholder_changes_never_reach_the_launch_cap(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "v0")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    for i in range(1, getattr(mod, "_MAX_SERVER_LAUNCHES") + 5):
        monkeypatch.setenv("KC_TEST_TOKEN", f"v{i}")
        approvals, _target_env = _rewrite_and_persist(tmp_path, mod, store)
        assert approvals.refused == {}, f"refused after {i} changes"

    assert len(mod.load_approvals(store).approved_pairs["SRV"]) == 1


def test_a_rebind_does_not_restore_an_approval_revoked_during_the_pass(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "first")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    monkeypatch.setenv("KC_TEST_TOKEN", "rotated")
    approvals = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    # The operator turns the stub off before the pass is persisted.
    mod.revoke(["srv"], path=store)
    mod.save_pass(approvals, path=store)

    stored = mod.load_approvals(store)
    command_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])
    assert not stored.approved.get("SRV")
    assert not stored.admits_launch("SRV", command_hash, mod.env_fingerprint({"TOKEN": "rotated"}))


def test_a_rebind_does_not_overwrite_an_approval_made_during_the_pass(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "v0")
    _placeholder_agent(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)

    monkeypatch.setenv("KC_TEST_TOKEN", "v1")
    approvals = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    # The value moves again and the operator approves it before the pass is saved.
    monkeypatch.setenv("KC_TEST_TOKEN", "v2")
    _approve_as_the_toggle_does(tmp_path, mod, store)
    mod.save_pass(approvals, path=store)

    stored = mod.load_approvals(store)
    command_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])
    assert stored.admits_launch("SRV", command_hash, mod.env_fingerprint({"TOKEN": "v2"}))
    assert not stored.admits_launch("SRV", command_hash, mod.env_fingerprint({"TOKEN": "v1"}))
    assert not stored.admits_launch("SRV", command_hash, mod.env_fingerprint({"TOKEN": "v0"}))


def test_two_declarations_of_one_launch_never_reach_the_launch_cap(tmp_path, monkeypatch):
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "v0")
    _placeholder_agent(tmp_path)
    # A second agent declares the same command and args with other env text.
    (tmp_path / "agents" / "b.json").write_text(
        json.dumps(
            {
                "name": "b",
                "mcpServers": {
                    "srv": {
                        "command": _APPROVED_CMD,
                        "args": ["-V"],
                        "env": {"TOKEN": "${KC_TEST_TOKEN}", "LEVEL": "debug"},
                    }
                },
            }
        )
    )
    _approve_as_the_toggle_does(tmp_path, mod, store)

    for i in range(1, getattr(mod, "_MAX_SERVER_LAUNCHES") + 5):
        monkeypatch.setenv("KC_TEST_TOKEN", f"v{i}")
        approvals, _target_env = _rewrite_and_persist(tmp_path, mod, store)
        assert approvals.refused == {}, f"refused after {i} changes"

    assert len(mod.load_approvals(store).approved_pairs["SRV"]) == 2


def _second_agent_declaring_the_same_launch(tmp_path: Path) -> None:
    """Agent ``b``: same command and args as ``a``, different declared env text."""
    (tmp_path / "agents" / "b.json").write_text(
        json.dumps(
            {
                "name": "b",
                "mcpServers": {
                    "srv": {
                        "command": _APPROVED_CMD,
                        "args": ["-V"],
                        "env": {"TOKEN": "${KC_TEST_TOKEN}", "LEVEL": "debug"},
                    }
                },
            }
        )
    )


def test_a_rebind_keeps_the_pair_of_an_agent_whose_source_could_not_be_read(tmp_path, monkeypatch):
    """A pass that kept an overlay retires nothing under the rebound command.

    The keep skips the spec read, so that agent's launches are never admitted
    and the pass's live set is missing them. Retiring what the pass did not see
    would drop the kept overlay's own approved pair, and the backend behind the
    overlay and sidecar still on disk would fail gatewayd's approval check.
    """
    mod = _approval_module()
    assert mod is not None
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("KC_TEST_TOKEN", "v0")
    _placeholder_agent(tmp_path)
    _second_agent_declaring_the_same_launch(tmp_path)
    _approve_as_the_toggle_does(tmp_path, mod, store)
    command_hash = rewriter.hash_command(_APPROVED_CMD, ["-V"])
    kept_pair_env = mod.env_fingerprint({"TOKEN": "v0", "LEVEL": "debug"})
    assert mod.load_approvals(store).admits_launch("SRV", command_hash, kept_pair_env)

    # The value rotates, and b's source cannot be read on this pass: its
    # previous overlay and sidecar stay in effect, still carrying TOKEN=v0.
    monkeypatch.setenv("KC_TEST_TOKEN", "v1")
    real_read = agent_discovery.read_agent_spec_strict

    def _read_failing_for_b(spec_path, **kwargs):
        if spec_path.stem == "b":
            raise OSError("spec unreadable on this pass")
        return real_read(spec_path, **kwargs)

    monkeypatch.setattr(agent_discovery, "read_agent_spec_strict", _read_failing_for_b)
    approvals = mod.load_approvals(store)
    _rewrite(tmp_path, frozenset({"srv"}), approvals)
    mod.save_pass(approvals, path=store)

    overlay_dir = tmp_path / "home" / "mcp-gateway" / "agents"
    assert (overlay_dir / "b.json").is_file(), "the kept overlay was pruned"
    stored = mod.load_approvals(store)
    assert stored.admits_launch("SRV", command_hash, mod.env_fingerprint({"TOKEN": "v1"}))
    assert stored.admits_launch("SRV", command_hash, kept_pair_env), (
        "the kept overlay's approved pair was retired as unseen, so the backend "
        "behind it fails the approval check at spawn"
    )
    live_incomplete = getattr(approvals, "live_incomplete", None)
    assert (
        live_incomplete is True
    ), "LaunchApprovals exposes no live_incomplete set by a pass that kept an overlay"
