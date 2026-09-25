"""The DescriptorHarness contract: every seam, driven from a descriptor.

``DescriptorHarness`` is the one concrete harness built from data rather than
hand-written per host, so these tests do for it what ``test_acp_harness_contract``
does for the bundled three: assert every seam answers correctly, and that the
answer comes from the DESCRIPTOR (or from the routing table it declares) rather
than from a hardcoded posture.

The two behaviours that vary with descriptor data get both branches:

* ``session_mcp_servers`` -- passthrough for ``agent_file`` delivery, narrowing
  for ``session_array``;
* ``verifies_agent_activation`` -- True only for an ``agent_spec`` descriptor that
  actually carries an agent_args block;
* ``resolve_spawn`` -- an empty mask for an ``agent_spec`` descriptor, a real mask
  (and a tier refusal) for a ``session_config`` one.

The registration seam it reads (``routing_for``) is process-global module state,
so the two tests that register a routed id restore it in a fixture, mirroring
``test_backend_registration``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.harness import codex as codex_mod
from kiro_crew.acp.harness.base import ReclaimPolicy, SessionExtras, SpawnContext
from kiro_crew.acp.harness.descriptor import (
    HarnessDescriptor,
    PermissionConfig,
)
from kiro_crew.acp.harness.operator import DescriptorHarness
from kiro_crew.acp.types import ACP_CLIENT_CAPABILITIES, METHOD_CANCEL, METHOD_SESSION_UPDATE
from kiro_crew.agent_sdk import backends as b


@pytest.fixture(autouse=True)
def _host_provenance_accepted(monkeypatch):
    """Stub executables live under the test's temp tree, whose ancestors are the
    host's business (a CI runner's home, a developer's /tmp), not this suite's.
    The provenance rule itself -- outside the agent-writable trees, owned by the
    gateway user or root, unwritable by others -- is ``github_runner``'s and is
    tested there; here it is accepted so the routing logic under test is what
    decides. The provenance tests in ``test_routing_verification`` re-patch it.
    """
    from kiro_crew import github_runner

    def _accept_relaxed(candidate, *, require_protected=False):
        # Provenance requires the strict form (a root-owned, gateway-unwritable
        # install) of every harness executable and of a launcher's interpreter; a
        # temp-tree stub cannot be one on this host, so the rule is answered
        # "accepted" for the stubs and the routing logic under test decides. The
        # provenance tests re-patch it to refuse.
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _accept_relaxed)


def _descriptor(**overrides) -> HarnessDescriptor:
    """A minimal valid descriptor; overrides tune one field per test."""
    base = dict(
        id="my-acp",
        display_name="My ACP",
        executable="my-acp",
        argv=("{executable}", "serve"),
    )
    base.update(overrides)
    return HarnessDescriptor(**base)  # type: ignore[arg-type]


def _ctx(
    tmp_path, *, agent: str = "a", model: str | None = None, sandbox_mode="auto"
) -> SpawnContext:
    return SpawnContext(
        agent=agent,
        work_dir=str(tmp_path),
        model=model,
        environ={},
        home=Path(tmp_path),
        sandbox_mode=sandbox_mode,
    )


@pytest.fixture
def registered_agent_spec():
    """Register ``my-acp`` as an AGENT_SPEC backend for the duration of a test."""
    b.register_known_backend("my-acp", label="My ACP", routing=b.Routing.AGENT_SPEC)
    try:
        yield
    finally:
        b._reset_registered_backends()


@pytest.fixture
def registered_session_config():
    """Register ``my-acp`` as a SESSION_CONFIG (enforced) backend for a test."""
    b.register_known_backend(
        "my-acp",
        label="My ACP",
        routing=b.Routing.SESSION_CONFIG,
        permission_config=("mode", "read-only"),
    )
    try:
        yield
    finally:
        b._reset_registered_backends()


@pytest.fixture
def found_exe(monkeypatch):
    """The descriptor's executable resolves to a pinned absolute path, and the
    spawn-path routing attestation check is satisfied (the path is fictional; the
    gate itself is covered by ``test_routing_verification`` and the refusal test
    below)."""
    monkeypatch.setattr(
        client_mod, "resolve_descriptor_executable", lambda exe: ("/pinned/my-acp", "/search")
    )
    from kiro_crew.acp.harness import routing_verification as rv

    monkeypatch.setattr(rv, "pin_verified_executable", lambda descriptor, exe, **kw: (exe, None))
    monkeypatch.setattr(rv, "agent_attestation_problem", lambda descriptor, agent, **kw: None)


@pytest.fixture
def empty_mask(monkeypatch):
    """resolve_spawn_masks resolves to no mask (the AGENT_SPEC answer)."""

    async def _masks(backend, mode):
        return ((), ())

    monkeypatch.setattr(codex_mod, "resolve_spawn_masks", _masks)


# ── Construction ──


def test_backend_is_the_descriptor_id():
    harness = DescriptorHarness(_descriptor(id="wire-host"))
    assert harness.backend == "wire-host"


def test_it_is_a_harness_adapter():
    from kiro_crew.acp.harness.base import HarnessAdapter

    assert isinstance(DescriptorHarness(_descriptor()), HarnessAdapter)


def test_every_abstract_seam_is_implemented():
    """A DescriptorHarness instantiates, so no abstract seam is left unfilled."""
    harness = DescriptorHarness(_descriptor())
    assert not getattr(type(harness), "__abstractmethods__", frozenset())


# ── Seam 1: spawn ──


@pytest.mark.asyncio
async def test_spawn_renders_the_argv_template(found_exe, empty_mask, tmp_path):
    """The resolved executable replaces {executable}; literals pass through."""
    harness = DescriptorHarness(_descriptor(argv=("{executable}", "serve", "--stdio")))
    plan = await harness.resolve_spawn(_ctx(tmp_path))
    assert plan.argv == ["/pinned/my-acp", "serve", "--stdio"]
    assert plan.host_auth is False


@pytest.mark.asyncio
async def test_spawn_emits_agent_and_model_blocks_only_when_selected(
    found_exe, empty_mask, tmp_path
):
    """agent_args/model_args render only when an agent/model is present."""
    harness = DescriptorHarness(
        _descriptor(agent_args=("--agent", "{agent}"), model_args=("--model", "{model}"))
    )
    with_both = await harness.resolve_spawn(_ctx(tmp_path, agent="a", model="m"))
    assert with_both.argv == ["/pinned/my-acp", "serve", "--agent", "a", "--model", "m"]

    no_model = await harness.resolve_spawn(_ctx(tmp_path, agent="a", model=None))
    assert "--model" not in no_model.argv
    assert no_model.argv[-2:] == ["--agent", "a"]


@pytest.mark.asyncio
async def test_spawn_aborts_on_a_missing_executable(monkeypatch, empty_mask, tmp_path):
    from kiro_crew.acp.session_handle import AcpRuntimeError

    monkeypatch.setattr(
        client_mod, "resolve_descriptor_executable", lambda exe: (None, "/searched")
    )
    harness = DescriptorHarness(_descriptor())
    with pytest.raises(AcpRuntimeError, match="not found"):
        await harness.resolve_spawn(_ctx(tmp_path))


@pytest.mark.asyncio
async def test_an_agent_spec_descriptor_spawns_with_no_mask(
    registered_agent_spec, found_exe, tmp_path
):
    """An AGENT_SPEC descriptor carries an empty mask -- routing keyed, not identity.

    resolve_spawn_masks is the REAL one here (not stubbed): it re-checks
    is_enforced and returns empty for an agent_spec routing, so the argv is
    unchanged and no credential mask rides on the plan.
    """
    harness = DescriptorHarness(_descriptor())
    plan = await harness.resolve_spawn(_ctx(tmp_path))
    assert plan.extra_hidden_dirs == ()
    assert plan.extra_expose_files == ()


@pytest.mark.asyncio
async def test_a_session_config_descriptor_carries_a_mask_on_the_plan(
    registered_session_config, monkeypatch, tmp_path
):
    """An enforced descriptor resolves the routing-keyed mask, exactly as codex does."""
    monkeypatch.setattr(
        client_mod, "resolve_descriptor_executable", lambda exe: ("/pinned/my-acp", "/s")
    )
    from kiro_crew.acp.harness import routing_verification as rv

    monkeypatch.setattr(rv, "pin_verified_executable", lambda descriptor, exe, **kw: (exe, None))
    monkeypatch.setattr(rv, "agent_attestation_problem", lambda descriptor, agent, **kw: None)

    async def _preflight(_fn, backend, mode):
        assert backend == "my-acp"
        return ("/h/.aws",)

    monkeypatch.setattr(client_mod, "_run_preflight_bounded", _preflight)
    monkeypatch.setattr(
        codex_mod.acp_tool_gate, "adapter_expose_files", lambda bkd, hidden: ("/h/.aws/config",)
    )
    ctx = dataclasses.replace(_ctx(tmp_path), sandbox_mode="standard")
    plan = await DescriptorHarness(_descriptor()).resolve_spawn(ctx)
    assert plan.extra_hidden_dirs == ("/h/.aws",)
    assert plan.extra_expose_files == ("/h/.aws/config",)


@pytest.mark.asyncio
async def test_a_session_config_descriptor_refuses_a_tier_that_drops_the_mask(
    registered_session_config, monkeypatch, tmp_path
):
    """An enforced descriptor on an unwrapped tier refuses, like codex."""
    monkeypatch.setattr(
        client_mod, "resolve_descriptor_executable", lambda exe: ("/pinned/my-acp", "/s")
    )
    from kiro_crew.acp.harness import routing_verification as rv

    monkeypatch.setattr(rv, "pin_verified_executable", lambda descriptor, exe, **kw: (exe, None))
    monkeypatch.setattr(rv, "agent_attestation_problem", lambda descriptor, agent, **kw: None)

    async def _refuse(*_a, **_k):
        raise RuntimeError("sandbox floor refused")

    monkeypatch.setattr(client_mod, "_run_preflight_bounded", _refuse)
    ctx = dataclasses.replace(_ctx(tmp_path), sandbox_mode="off")
    with pytest.raises(RuntimeError, match="sandbox floor refused"):
        await DescriptorHarness(_descriptor()).resolve_spawn(ctx)


def test_apply_spawn_env_strips_the_kiro_api_key(monkeypatch):
    """A foreign binary never receives kiro-cli's model credential."""
    from kiro_crew.config import loader as loader_mod

    calls: list[str] = []
    monkeypatch.setattr(
        loader_mod, "strip_kiro_cli_api_key", lambda env: calls.append("strip"), raising=False
    )
    DescriptorHarness(_descriptor()).apply_spawn_env({})
    assert calls == ["strip"]


# ── verifies_agent_activation ──


def test_agent_spec_with_agent_args_verifies_activation(registered_agent_spec):
    """AGENT_SPEC routing AND an agent_args block => activation is verified."""
    harness = DescriptorHarness(_descriptor(agent_args=("--agent", "{agent}")))
    assert harness.verifies_agent_activation is True


def test_agent_spec_without_agent_args_does_not_verify(registered_agent_spec):
    """AGENT_SPEC routing but no agent_args => nothing was selected at spawn to confirm."""
    harness = DescriptorHarness(_descriptor(agent_args=()))
    assert harness.verifies_agent_activation is False


def test_session_config_never_verifies_activation(registered_session_config):
    """A session_config descriptor selects no agent at spawn, so nothing to confirm."""
    harness = DescriptorHarness(_descriptor(agent_args=("--agent", "{agent}")))
    assert harness.verifies_agent_activation is False


def test_unregistered_descriptor_does_not_verify():
    """With no routing registered (UNVERIFIED), there is no agent-spec selection."""
    harness = DescriptorHarness(_descriptor(agent_args=("--agent", "{agent}")))
    assert harness.verifies_agent_activation is False


# ── Seam 2: initialize ──


def test_protocol_version_is_plain_acp_v1():
    version = DescriptorHarness(_descriptor()).protocol_version
    assert version == 1 and isinstance(version, int)


def test_client_capabilities_are_the_shared_constant():
    assert DescriptorHarness(_descriptor()).client_capabilities == ACP_CLIENT_CAPABILITIES


# ── Seam 3: session extras + mcp array ──


@pytest.mark.asyncio
async def test_session_extras_are_empty(tmp_path):
    extras = await DescriptorHarness(_descriptor()).session_extras("a", work_dir=str(tmp_path))
    assert extras == SessionExtras(custom_agents=None)


def test_agent_file_delivery_passes_the_array_through_unchanged():
    """Default (agent_file) delivery is a passthrough -- identity, byte-for-byte."""
    harness = DescriptorHarness(_descriptor())  # mcp_delivery defaults to agent_file
    requested = [{"name": "kirocrew-core", "command": "x"}]
    out = harness.session_mcp_servers(requested, agent_capabilities={})
    assert out is requested


def test_session_array_delivery_narrows_against_advertised_transports():
    """session_array delivery drops an element whose transport was not advertised."""
    harness = DescriptorHarness(_descriptor(mcp_delivery="session_array"))
    requested = [{"name": "keep", "url": "http://keep"}, {"name": "drop", "type": "sse"}]
    out = harness.session_mcp_servers(
        requested, agent_capabilities={"mcpCapabilities": {"http": True, "sse": False}}
    )
    assert [s["name"] for s in out] == ["keep"]
    assert out is not requested


@pytest.mark.parametrize(
    "capabilities",
    [{}, {"mcpCapabilities": {}}, {"mcpCapabilities": None}, {"mcpCapabilities": "http"}],
)
def test_session_array_narrows_nothing_on_an_unknown_handshake(capabilities):
    """Empty means 'nothing is known', never 'nothing supported' -- pass through."""
    harness = DescriptorHarness(_descriptor(mcp_delivery="session_array"))
    requested = [{"name": "a", "type": "sse"}, {"name": "b", "command": "x"}]
    out = harness.session_mcp_servers(requested, agent_capabilities=capabilities)
    assert out is requested


# ── Seam 4: host-answered requests ──


def test_host_answered_methods_are_empty():
    assert DescriptorHarness(_descriptor()).host_answered_methods == ()


@pytest.mark.asyncio
async def test_answer_request_raises():
    with pytest.raises(NotImplementedError):
        await DescriptorHarness(_descriptor()).answer_request("some/method")


# ── Seam 5: notification aliases ──


def test_notification_aliases_are_plain_acp():
    aliases = DescriptorHarness(_descriptor()).notification_aliases
    assert aliases.session_update == (METHOD_SESSION_UPDATE,)
    assert aliases.subagent_list_update == ""
    assert aliases.mcp_init == ()
    assert not aliases.mcp_readiness


def test_notification_aliases_are_not_the_kiro_family():
    from kiro_crew.acp.harness._common import KIRO_FAMILY_ALIASES

    assert DescriptorHarness(_descriptor()).notification_aliases is not KIRO_FAMILY_ALIASES


# ── Seam 6: teardown ──


def test_teardown_is_a_cancel_notification():
    teardown = DescriptorHarness(_descriptor()).teardown
    assert teardown.method == METHOD_CANCEL
    assert teardown.notification is True


# ── Membership seams (inherited) fail safe ──


def test_membership_seams_default_off_for_an_unregistered_id():
    """A descriptor id in no capability set answers False / passthrough thresholds."""
    harness = DescriptorHarness(_descriptor())
    assert harness.internal_sandbox is False
    assert harness.pod_home_remap is False
    assert harness.reads_markdown_agent_specs is False
    assert harness.reclaim_policy(max_age_secs=1.0, max_rss_mb=2.0) == ReclaimPolicy(
        max_age_secs=1.0, max_rss_mb=2.0
    )


# ── The descriptor is the only state, and it is frozen ──


def test_the_only_instance_state_is_the_descriptor():
    """A DescriptorHarness holds its descriptor and nothing else.

    Unlike the bundled harnesses (which are stateless), this one MUST carry the
    descriptor -- the backend id cannot reconstruct the argv template or routing.
    Nothing else is stored.
    """
    d = _descriptor()
    harness = DescriptorHarness(d)
    assert vars(harness) == {"_descriptor": d, "backend": d.id}


def test_resolve_spawn_reads_only_its_context():
    import inspect

    sig = inspect.signature(DescriptorHarness.resolve_spawn)
    assert list(sig.parameters) == ["self", "ctx"]


def test_the_operator_module_does_not_import_the_runtime():
    from kiro_crew.acp.harness import operator as operator_mod

    source = Path(operator_mod.__file__).read_text(encoding="utf-8")
    assert "kiro_crew.acp.runtime" not in source


def test_permission_config_descriptor_carries_through():
    """A session_config descriptor keeps its permission_config on the frozen record."""
    d = _descriptor(
        routing="session_config", permission_config=PermissionConfig("mode", "read-only")
    )
    harness = DescriptorHarness(d)
    assert harness._descriptor.permission_config == PermissionConfig("mode", "read-only")


def test_directly_resolved_executable_is_canonicalized_to_absolute(tmp_path, monkeypatch):
    """A relative descriptor executable resolves to an ABSOLUTE path, so the
    attested file cannot diverge from the exec'd file when the spawn later runs
    under a different CWD (a session working directory).

    Runs on every platform: the stub is never executed, only RESOLVED, and
    ``platform_compat.is_executable_file`` judges runnability by the execute
    bit on POSIX and by a known script extension on Windows (there is no
    execute bit there). So the fixture carries both -- a ``.cmd`` name and the
    bit -- and the assertion is platform-independent.
    """
    import os
    import stat
    import sys

    name = "my-host.cmd" if sys.platform == "win32" else "my-host"
    exe = tmp_path / name
    exe.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.chdir(tmp_path)

    resolved, _search = client_mod.resolve_descriptor_executable(f"./{name}")

    assert resolved is not None
    assert os.path.isabs(resolved), f"expected an absolute path, got {resolved!r}"
    assert os.path.realpath(resolved) == os.path.realpath(str(exe))


@pytest.mark.asyncio
async def test_spawn_refuses_when_the_verified_binary_changed_and_revokes(
    empty_mask, tmp_path, monkeypatch
):
    """The last read before exec re-checks the attestation against the bytes
    about to run: a mismatch refuses THIS spawn and withdraws selectability, so
    a binary swapped under an unchanged path never serves as a verified backend."""
    import stat

    from kiro_crew.acp import harness as harness_pkg
    from kiro_crew.acp.harness import operator_registry as reg
    from kiro_crew.acp.harness import routing_verification as rv
    from kiro_crew.acp.session_handle import AcpRuntimeError
    from kiro_crew.agent_sdk import backends as b

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    exe = tmp_path / "bin" / "my-acp"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(client_mod, "resolve_descriptor_executable", lambda e: (str(exe), "/s"))

    baseline, selectable = set(b._baseline), set(b._selectable)
    try:
        (tmp_path / "home" / "harnesses.json").write_text(
            json.dumps(
                {
                    "my-acp": {
                        "executable": str(exe),
                        "argv": ["{executable}", "serve"],
                        "agent_args": ["--agent", "{agent}"],
                        "routing": "agent_spec",
                    }
                }
            ),
            encoding="utf-8",
        )
        d = _descriptor(
            executable=str(exe), agent_args=("--agent", "{agent}"), routing="agent_spec"
        )
        rv.record_attestation(d, mechanism="agent_spec", evidence={}, agent="a")
        reg.load_and_register_operator_descriptors()
        assert "my-acp" in b.selectable_backends()
        harness = DescriptorHarness(reg.registered_operator_descriptor("my-acp"))
        # Unchanged binary: the spawn goes through, execing the operator's path in
        # place (provenance requires a protected install, which the stub passes
        # through the autouse fixture; the judged bytes are the bytes that run).
        plan = await harness.resolve_spawn(_ctx(tmp_path))
        assert plan.argv[0] == str(exe)
        judged = rv.executable_digest(str(exe))
        # Replaced binary, same path: refused, and not selectable.
        exe.write_text("#!/bin/sh\nexec evil\n", encoding="utf-8")
        with pytest.raises(AcpRuntimeError, match="changed since its routing was verified"):
            await harness.resolve_spawn(_ctx(tmp_path))
        assert judged != rv.executable_digest(str(exe))
        assert "my-acp" not in b.selectable_backends()
        assert "my-acp" in reg.unverified_operator_harnesses()
        assert rv.load_attestations() == {}
    finally:
        b._reset_registered_backends()
        harness_pkg._reset_operator_register()
        reg._reset_operator_diagnostics()
        b._baseline.clear()
        b._baseline.update(baseline)
        b._selectable.clear()
        b._selectable.update(selectable)
