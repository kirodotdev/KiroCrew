"""The harness layer's own contract.

Every seam a host can differ on is asserted here per host, so a backend added
later cannot half-implement the layer and still start. These are the assertions
that make the layer worth having: without them a new harness could inherit
kiro-cli's answer on a seam it actually differs on, and the first sign of it
would be a live session behaving wrongly.

The wire consequences of these answers -- the actual bytes -- are pinned
separately in ``test_acp_harness_wire_parity.py``.

Every stub below patches the module that DEFINES the helper, never a local
binding inside a harness: the harnesses call through their defining modules
precisely so a stub aimed at the definition reaches them, and a test that
patched a local name would leave the real filesystem work running.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import sandbox as sandbox_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import kas_agents as kas_agents_mod
from kiro_crew.acp.harness import (
    CodexHarness,
    HarnessAdapter,
    KasHarness,
    KiroHarness,
    ReclaimPolicy,
    SessionExtras,
    SpawnContext,
    harness_for,
)
from kiro_crew.acp.harness import kas as kas_mod
from kiro_crew.acp.harness._common import KIRO_FAMILY_ALIASES
from kiro_crew.acp.kas_transport import METHOD_KAS_AUTH_GET_ACCESS_TOKEN
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_CLIENT_CAPABILITIES,
    KAS_CLIENT_CAPABILITIES,
    METHOD_KAS_SESSION_DELETE,
    METHOD_SESSION_TERMINATE,
    METHOD_SESSION_UPDATE,
)
from kiro_crew.config import paths as paths_mod
from kiro_crew.mcp_gateway import session_servers as session_servers_mod

ALL_BACKENDS = [ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX]

#: The hosts reached through kiro-cli's own binary and ACP relay. They share a
#: notification vocabulary, an agent-spec permission routing and a spawn-time effort
#: overlay, so several assertions below are about the FAMILY rather than about every
#: harness. Kept as its own list so a family assertion cannot silently widen onto a
#: host it was never true of -- which is how a foreign harness inherits kiro-cli's
#: answer on the one seam it actually differs on.
KIRO_FAMILY_BACKENDS = [ACP_BACKEND_KIRO, ACP_BACKEND_KAS]


def _ctx(tmp_path, *, agent: str = "a", model: str | None = None) -> SpawnContext:
    return SpawnContext(
        agent=agent, work_dir=str(tmp_path), model=model, environ={}, home=Path(tmp_path)
    )


@pytest.fixture
def found_binary(monkeypatch):
    """Every spawn resolves a trusted binary; pin it so no host search runs."""

    async def _bin(*, environ, home):
        return "/pinned/kiro-cli"

    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _bin)


@pytest.fixture
def kiro_gates_pass(monkeypatch):
    """All three of the kiro spawn's pre-spawn gates answer "go"."""
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(sandbox_mod, "delegated_workspace_exposes_agents_dir", lambda work_dir: "")


@pytest.fixture
def kas_projection_stubbed(monkeypatch, tmp_path):
    """The KAS projection's disk reads, with the translation left to the caller."""
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(
        session_servers_mod, "injection_server_names", lambda overlay, agent: frozenset()
    )
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: tmp_path)


# ── Registry ──


@pytest.mark.parametrize(
    "backend,expected",
    [
        (ACP_BACKEND_KIRO, KiroHarness),
        (ACP_BACKEND_KAS, KasHarness),
        (ACP_BACKEND_CODEX, CodexHarness),
    ],
)
def test_harness_for_resolves_each_served_backend(backend, expected):
    harness = harness_for(backend)
    assert isinstance(harness, expected)
    assert harness.backend == backend


def test_the_registry_serves_every_backend_it_claims_to():
    """The list in the refusal message and the table it describes are the same list.

    A harness added to the table without the message being regenerated from it would
    tell an operator the runtime serves fewer hosts than it does.
    """
    from kiro_crew.acp.harness import _HARNESSES

    assert set(_HARNESSES) == set(ALL_BACKENDS)
    with pytest.raises(ValueError) as exc:
        harness_for("byo-harness")
    for backend in ALL_BACKENDS:
        assert repr(backend) in str(exc.value)


def test_harness_for_refuses_an_unserved_backend():
    """Refusing beats defaulting.

    A backend with no harness that silently got kiro-cli's would start fine and
    then send the wrong protocol version, the wrong teardown verb and an argv for
    a different binary.

    Spelled with an id no harness serves rather than a real one. It named ``codex``
    while codex had no harness; a registered host is the wrong probe for this,
    because the day it registers the test would pass only by refusing something it
    should serve.
    """
    with pytest.raises(ValueError, match="no ACP harness"):
        harness_for("byo-harness")


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_every_abstract_seam_is_implemented(backend):
    """No harness instantiates while a seam is still abstract.

    ABCMeta already refuses that at construction, so this test's value is the
    positive assertion that the abstract SET is non-empty -- a base class that
    lost its ``@abstractmethod`` decorators would make instantiation pass while
    checking nothing.
    """
    assert HarnessAdapter.__abstractmethods__
    harness = harness_for(backend)
    assert not getattr(type(harness), "__abstractmethods__", frozenset())


def test_the_contract_declares_every_seam_this_suite_covers():
    """The abstract set and this suite's coverage list agree.

    A seam added to the base class without a test here would otherwise be
    unasserted for both hosts on the day it lands.
    """
    covered = {
        "resolve_spawn",
        "apply_spawn_env",
        "internal_sandbox",
        "pod_home_remap",
        "verifies_agent_activation",
        "protocol_version",
        "client_capabilities",
        "session_extras",
        "session_mcp_servers",
        "host_answered_methods",
        "answer_request",
        "notification_aliases",
        "teardown",
        "reclaim_policy",
    }
    assert HarnessAdapter.__abstractmethods__ == covered


# ── Seam 1: spawn ──


@pytest.mark.asyncio
async def test_kiro_spawn_argv_names_the_agent_and_the_model(
    found_binary, kiro_gates_pass, tmp_path
):
    plan = await harness_for(ACP_BACKEND_KIRO).resolve_spawn(_ctx(tmp_path, model="m"))
    assert plan.argv == ["/pinned/kiro-cli", "acp", "--agent", "a", "--model", "m"]
    assert plan.host_auth is False


@pytest.mark.asyncio
async def test_kiro_spawn_omits_the_model_flag_when_none_is_pinned(
    found_binary, kiro_gates_pass, tmp_path
):
    """An unpinned model leaves the flag OFF rather than sending an empty value.

    ``--model ''`` is not the same request as no flag: it overrides the agent
    config's own pin with nothing.
    """
    plan = await harness_for(ACP_BACKEND_KIRO).resolve_spawn(_ctx(tmp_path))
    assert "--model" not in plan.argv


@pytest.mark.asyncio
async def test_kiro_spawn_refuses_when_fork_governance_is_unresolved(
    found_binary, kiro_gates_pass, monkeypatch, tmp_path
):
    """A pending governance refresh ABORTS the spawn.

    A fork's on-disk allowedTools / autoApprove bypass the approval gate, so
    proceeding would run grants nobody checked.
    """
    from kiro_crew.acp.session_handle import AcpRuntimeError
    from kiro_crew.agent import ForkGovernanceUnresolved

    def _boom(agent, work_dir):
        raise ForkGovernanceUnresolved("governance pending")

    monkeypatch.setattr(agent_mod, "require_fork_governance", _boom)
    with pytest.raises(AcpRuntimeError, match="governance pending"):
        await harness_for(ACP_BACKEND_KIRO).resolve_spawn(_ctx(tmp_path))


@pytest.mark.asyncio
async def test_kiro_spawn_refuses_a_workspace_overlapping_the_agents_tree(
    found_binary, kiro_gates_pass, monkeypatch, tmp_path
):
    """The overlap is the one way left to rewrite a spec under delegated sandboxing."""
    from kiro_crew.acp.session_handle import AcpRuntimeError

    monkeypatch.setattr(
        sandbox_mod,
        "delegated_workspace_exposes_agents_dir",
        lambda work_dir: "overlaps agents dir",
    )
    with pytest.raises(AcpRuntimeError, match="overlaps agents dir"):
        await harness_for(ACP_BACKEND_KIRO).resolve_spawn(_ctx(tmp_path))


@pytest.mark.asyncio
async def test_kiro_spawn_survives_a_failed_materialization(
    found_binary, kiro_gates_pass, monkeypatch, tmp_path
):
    """Materialization is best-effort; a non-managed agent cannot be regenerated here."""

    def _boom(agent):
        raise OSError("read-only tree")

    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", _boom)
    plan = await harness_for(ACP_BACKEND_KIRO).resolve_spawn(_ctx(tmp_path))
    assert plan.argv[:2] == ["/pinned/kiro-cli", "acp"]


@pytest.mark.asyncio
async def test_kas_spawn_carries_no_agent_or_model_flag(found_binary, monkeypatch, tmp_path):
    """KAS takes both over the wire, so neither belongs on the command line."""

    async def _vault():
        return False

    monkeypatch.setattr(kas_mod, "vault_holds_identity_off_loop", _vault)
    plan = await harness_for(ACP_BACKEND_KAS).resolve_spawn(_ctx(tmp_path, model="m"))
    assert "--agent" not in plan.argv
    assert "--model" not in plan.argv


@pytest.mark.asyncio
@pytest.mark.parametrize("vault_has_identity", [True, False])
async def test_kas_spawn_reports_the_auth_owner_it_chose(
    found_binary, monkeypatch, tmp_path, vault_has_identity
):
    """The auth-owner decision rides on the PLAN, not on runtime state.

    That is what lets the reader loop answer the credential callback only on a
    process started expecting Crew to own it: a sign-out between spawns changes
    the next plan without touching the live one.
    """

    async def _vault():
        return vault_has_identity

    monkeypatch.setattr(kas_mod, "vault_holds_identity_off_loop", _vault)
    plan = await harness_for(ACP_BACKEND_KAS).resolve_spawn(_ctx(tmp_path))
    assert plan.host_auth is vault_has_identity


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ALL_BACKENDS)
async def test_a_missing_binary_aborts_the_spawn(monkeypatch, tmp_path, backend):
    from kiro_crew.acp.session_handle import AcpRuntimeError

    async def _no_bin(*, environ, home):
        return ""

    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _no_bin)
    monkeypatch.setattr(client_mod, "kiro_cli_not_found_message", lambda **kw: "not found")
    # Each host resolves its OWN binary, so every resolver is emptied and the
    # parametrisation stays over every harness. The invariant here is universal --
    # no harness may return an argv for a binary that is not there -- and narrowing
    # it to one family would leave the next host's spawn unasserted.
    monkeypatch.setattr(client_mod, "_resolve_codex_acp_bin", lambda: (None, "/nowhere"))
    with pytest.raises(AcpRuntimeError, match="not found"):
        await harness_for(backend).resolve_spawn(_ctx(tmp_path))


def test_kiro_injects_the_api_key_and_kas_strips_it(monkeypatch):
    """Opposite actions on the same variable, which is why this is a seam.

    A harness that inherited the other's answer would either withhold the key
    kiro-cli needs or hand KAS a credential of the wrong token type.
    """
    from kiro_crew.config import loader as loader_mod

    calls: list[str] = []
    monkeypatch.setattr(
        loader_mod, "inject_kiro_cli_api_key", lambda env: calls.append("inject"), raising=False
    )
    monkeypatch.setattr(
        loader_mod, "strip_kiro_cli_api_key", lambda env: calls.append("strip"), raising=False
    )

    harness_for(ACP_BACKEND_KIRO).apply_spawn_env({})
    harness_for(ACP_BACKEND_KAS).apply_spawn_env({})
    assert calls == ["inject", "strip"]


# ── Seam 2: initialize ──


def test_protocol_versions_differ_in_type_not_just_value():
    """KAS numbers revisions; kiro-cli dates them.

    Pinned as a TYPE assertion because sending the other spelling is rejected
    outright rather than negotiated, and ``1 == "1"`` is false in a way a value
    comparison alone would not surface if either side started stringifying.
    """
    kiro_version = harness_for(ACP_BACKEND_KIRO).protocol_version
    kas_version = harness_for(ACP_BACKEND_KAS).protocol_version
    assert isinstance(kiro_version, str) and kiro_version == "2025-08-22"
    assert isinstance(kas_version, int) and kas_version == 1
    assert not isinstance(kas_version, str)


def test_client_capabilities_are_the_shared_constants():
    """The harness hands over the SAME objects the pre-extraction code sent."""
    assert harness_for(ACP_BACKEND_KIRO).client_capabilities == ACP_CLIENT_CAPABILITIES
    assert harness_for(ACP_BACKEND_KAS).client_capabilities == KAS_CLIENT_CAPABILITIES


def test_kas_capabilities_open_only_the_settings_channel():
    """KAS's extra capability is the settings channel and nothing else.

    Every other ``_meta.kiro`` capability is a callback Crew does not implement,
    so declaring one would invite a request with no handler.
    """
    kas = harness_for(ACP_BACKEND_KAS).client_capabilities
    assert kas["_meta"] == {"kiro": {"settings": {}}}
    assert {k: v for k, v in kas.items() if k != "_meta"} == ACP_CLIENT_CAPABILITIES


# ── Seam 3: session extras ──


@pytest.mark.asyncio
async def test_kiro_sends_no_session_extras(tmp_path):
    extras = await harness_for(ACP_BACKEND_KIRO).session_extras("a", work_dir=str(tmp_path))
    assert extras == SessionExtras(custom_agents=None)


@pytest.mark.asyncio
async def test_kas_projects_the_agent_spec(kas_projection_stubbed, monkeypatch, tmp_path):
    projected = [{"name": "a"}]
    monkeypatch.setattr(
        kas_agents_mod,
        "build_kas_custom_agents",
        lambda d, a, *, stub_server_names, member_dispatch: projected,
    )
    extras = await harness_for(ACP_BACKEND_KAS).session_extras("a", work_dir=str(tmp_path))
    assert extras.custom_agents == projected


@pytest.mark.asyncio
async def test_kas_projects_nothing_without_an_agent(tmp_path):
    """No agent means nothing to register, not an empty registration."""
    extras = await harness_for(ACP_BACKEND_KAS).session_extras("", work_dir=str(tmp_path))
    assert extras.custom_agents is None


@pytest.mark.asyncio
async def test_kas_projection_refuses_ungoverned_fork_grants(monkeypatch, tmp_path):
    """The projection TRANSMITS the spec's grants, so it gets the spawn's gate."""
    from kiro_crew.acp.session_handle import AcpRuntimeError
    from kiro_crew.agent import ForkGovernanceUnresolved

    def _boom(agent, work_dir):
        raise ForkGovernanceUnresolved("governance pending")

    monkeypatch.setattr(agent_mod, "require_fork_governance", _boom)
    with pytest.raises(AcpRuntimeError, match="governance pending"):
        await harness_for(ACP_BACKEND_KAS).session_extras("a", work_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_kas_projection_refuses_an_untranslatable_spec(
    kas_projection_stubbed, monkeypatch, tmp_path
):
    """Continuing would leave the session on KAS's own default mode.

    For a restricted agent that runs a BROADER agent than the caller asked for,
    so the failure is loud rather than a fallback.
    """
    from kiro_crew.acp.kas_agents import KasAgentTranslationError
    from kiro_crew.acp.session_handle import AcpRuntimeError

    def _boom(d, a, *, stub_server_names, member_dispatch):
        raise KasAgentTranslationError("unreadable spec")

    monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", _boom)
    with pytest.raises(AcpRuntimeError, match="cannot project agent"):
        await harness_for(ACP_BACKEND_KAS).session_extras("a", work_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_kas_projection_survives_an_unreadable_overlay(
    kas_projection_stubbed, monkeypatch, tmp_path
):
    """An unreadable overlay must not cost the session its agent.

    Empty is the safe direction: a stubbed server gets declared twice (the
    injection still wins) rather than withheld with nothing else to supply it.
    """
    seen: list[frozenset] = []

    def _boom(overlay, agent):
        raise OSError("overlay unreadable")

    def _build(d, a, *, stub_server_names, member_dispatch):
        seen.append(frozenset(stub_server_names))
        return [{"name": a}]

    monkeypatch.setattr(session_servers_mod, "injection_server_names", _boom)
    monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", _build)

    extras = await harness_for(ACP_BACKEND_KAS).session_extras("a", work_dir=str(tmp_path))
    assert extras.custom_agents == [{"name": "a"}]
    assert seen == [frozenset()]


@pytest.mark.asyncio
async def test_kas_member_dispatch_subtracts_the_dashboard_server(
    kas_projection_stubbed, monkeypatch, tmp_path
):
    """The member's server arrives as a session-level entry, so it is subtracted.

    Left in, an identity-less spec declaration could shadow the member-keyed one.
    """
    from kiro_crew.members import MEMBER_DISPATCH_SERVER

    seen: list[frozenset] = []

    def _build(d, a, *, stub_server_names, member_dispatch):
        seen.append(frozenset(stub_server_names))
        return []

    monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", _build)
    await harness_for(ACP_BACKEND_KAS).session_extras(
        "a", work_dir=str(tmp_path), member_dispatch=True
    )
    assert seen == [frozenset({MEMBER_DISPATCH_SERVER})]


# ── Seam 4: host-answered requests ──


def test_only_kas_asks_crew_for_a_credential():
    assert harness_for(ACP_BACKEND_KIRO).host_answered_methods == ()
    assert harness_for(ACP_BACKEND_KAS).host_answered_methods == (METHOD_KAS_AUTH_GET_ACCESS_TOKEN,)


def test_kas_host_answered_methods_track_the_membership_set(monkeypatch):
    """Drop KAS from the callback set and the harness stops claiming the method.

    The harness and the runtime's reader-loop guard read the same set, so they
    cannot disagree about who answers what.
    """
    monkeypatch.setattr(kas_mod, "ACP_BACKENDS_HOST_AUTH_CALLBACK", frozenset())
    assert harness_for(ACP_BACKEND_KAS).host_answered_methods == ()


@pytest.mark.asyncio
async def test_kas_answers_the_token_callback_from_the_vault(monkeypatch):
    async def _answer():
        return {"expiresAt": "pinned"}

    monkeypatch.setattr(kas_mod, "answer_get_access_token", _answer)
    result = await harness_for(ACP_BACKEND_KAS).answer_request(METHOD_KAS_AUTH_GET_ACCESS_TOKEN)
    assert result == {"expiresAt": "pinned"}


def test_the_reader_loop_answers_through_the_harness_that_claimed_the_method():
    """The answer comes from the host that named the method, not a fixed answerer.

    The guard upstream accepts ANY method a host claims in
    ``host_answered_methods``. With a hardcoded answerer, a host that claimed a
    different method would pass that guard and be handed the credential another
    host's harness built -- the mistaken-identity failure this layer exists to
    prevent, and one no test of either host alone would catch.
    """
    import inspect as _inspect

    from kiro_crew.acp.runtime import AcpRuntime

    reader = _inspect.getsource(AcpRuntime._reader_loop)
    assert "self._answer_host_request(msg.id, msg.method" in reader

    answerer = _inspect.getsource(AcpRuntime._answer_host_request)
    assert "self._harness.answer_request(method)" in answerer


@pytest.mark.asyncio
async def test_kas_refuses_a_method_it_never_claimed():
    with pytest.raises(NotImplementedError):
        await harness_for(ACP_BACKEND_KAS).answer_request("some/other")


@pytest.mark.asyncio
async def test_kiro_answers_nothing():
    with pytest.raises(NotImplementedError):
        await harness_for(ACP_BACKEND_KIRO).answer_request(METHOD_KAS_AUTH_GET_ACCESS_TOKEN)


# ── Seam 5: notification aliases ──


@pytest.mark.parametrize("backend", KIRO_FAMILY_BACKENDS)
def test_both_kiro_family_hosts_share_one_vocabulary(backend):
    """KAS is reached THROUGH kiro-cli's relay, so it speaks kiro-cli's aliases."""
    assert harness_for(backend).notification_aliases is KIRO_FAMILY_ALIASES


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_every_host_accepts_the_standard_session_update_spelling(backend):
    """The one alias fact that IS universal.

    A host may add its own spelling and may announce no subagents, but a session
    update that does not arrive under the standard method is a frame counted as
    ``other`` and dropped -- so no harness may leave this out, whatever else it
    declares.
    """
    assert METHOD_SESSION_UPDATE in harness_for(backend).notification_aliases.session_update


def test_a_host_outside_the_family_declares_its_own_vocabulary(backend=ACP_BACKEND_CODEX):
    """codex is not reached through kiro-cli, so it must not inherit ``_kiro.dev/*``.

    Inheriting it would make the demux accept methods the host never sends, which
    costs nothing -- and would hide the real question, which is whether anyone
    checked. The assertion is that the answer was WRITTEN for this host.
    """
    aliases = harness_for(backend).notification_aliases
    assert aliases is not KIRO_FAMILY_ALIASES
    assert aliases.session_update == (METHOD_SESSION_UPDATE,)
    assert aliases.subagent_list_update == ""
    assert aliases.mcp_init == ()


def test_session_update_accepts_both_spellings():
    """Accepting only one silently drops half the updates.

    kiro-cli sends the standard method and its ``_kiro.dev`` alias for the same
    event, and which one arrives depends on the CLI version.
    """
    from kiro_crew.acp.types import METHOD_KIRO_SESSION_UPDATE, METHOD_SESSION_UPDATE

    assert set(KIRO_FAMILY_ALIASES.session_update) == {
        METHOD_SESSION_UPDATE,
        METHOD_KIRO_SESSION_UPDATE,
    }


def test_all_three_mcp_init_methods_are_staged():
    """A missing one is dropped as ownerless instead of staged for the session."""
    from kiro_crew.acp.types import (
        METHOD_MCP_OAUTH_REQUEST,
        METHOD_MCP_SERVER_INIT_FAILURE,
        METHOD_MCP_SERVER_INITIALIZED,
    )

    assert set(KIRO_FAMILY_ALIASES.mcp_init) == {
        METHOD_MCP_OAUTH_REQUEST,
        METHOD_MCP_SERVER_INITIALIZED,
        METHOD_MCP_SERVER_INIT_FAILURE,
    }


# ── Seam 6: teardown ──


def test_teardown_verbs_are_per_host():
    """kiro-cli evicts; KAS deletes.

    Sending one host's verb to the other either leaves a session live in a shared
    process (an RSS leak nothing reclaims) or destroys a record the caller asked
    to keep.
    """
    assert harness_for(ACP_BACKEND_KIRO).teardown.method == METHOD_SESSION_TERMINATE
    assert harness_for(ACP_BACKEND_KAS).teardown.method == METHOD_KAS_SESSION_DELETE


# ── The seams deliberately NOT on the contract ──


def test_the_contract_mirrors_no_membership_table():
    """No harness member restates a table both drivers already read.

    How a live session's model changes, and how privileged tools are made to ask,
    are answered by ``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION`` and
    ``ACP_BACKEND_ROUTING``, and both drivers read those directly. A harness member
    mirroring one would be a second declaration free to disagree with the one that
    decides -- the exact drift this layer promises to remove, arriving as a feature.

    A member belongs here when a CALLER asks the harness for it. Both of these are
    looked up, so the honest surface is smaller.
    """
    assert not hasattr(HarnessAdapter, "model_switch")
    assert not hasattr(HarnessAdapter, "effort_switch")
    assert not hasattr(HarnessAdapter, "permission_routing")
    assert not hasattr(HarnessAdapter, "permission_config")


@pytest.mark.parametrize("backend", KIRO_FAMILY_BACKENDS)
def test_the_kiro_family_still_asks_by_construction(backend):
    """Both kiro-family hosts route through their agent spec, read from the table.

    Asserted against ``acp_tool_gate`` rather than a harness member, because that
    is who the drivers ask.
    """
    from kiro_crew import acp_tool_gate

    assert acp_tool_gate.routing_for(backend) is acp_tool_gate.Routing.AGENT_SPEC
    assert acp_tool_gate.permission_config_for(backend) == ("", "")


def test_codex_is_made_to_ask_by_a_session_write():
    """The counterexample the family assertion above must not swallow.

    codex has no agent spec and no settings file Crew writes, so the session's own
    option is the only boundary. ``read-only`` still permits passive reads; what
    makes that survivable is the OS-boundary credential mask, not this option.
    """
    from kiro_crew import acp_tool_gate

    routing = acp_tool_gate.routing_for(ACP_BACKEND_CODEX)
    assert routing is acp_tool_gate.Routing.SESSION_CONFIG
    assert acp_tool_gate.permission_config_for(ACP_BACKEND_CODEX) == ("mode", "read-only")


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_no_host_is_left_unverified(backend):
    """``UNVERIFIED`` refuses, so a host resolving to it cannot start a session."""
    from kiro_crew import acp_tool_gate

    assert acp_tool_gate.routing_for(backend) is not acp_tool_gate.Routing.UNVERIFIED


# ── Seam 9: reclaim ──


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_reclaim_thresholds_pass_the_operator_configuration_through(backend):
    """A harness narrows these for a leaky host; nothing in the kiro family does."""
    policy = harness_for(backend).reclaim_policy(max_age_secs=123.0, max_rss_mb=45.0)
    assert policy == ReclaimPolicy(max_age_secs=123.0, max_rss_mb=45.0)


# ── The Kiro path gains no failure mode (harness-parity H13) ──


def test_the_kiro_lookup_is_total():
    """Kiro's harness lookup CANNOT fail, so the default path gains no failure mode.

    The shared-process spawn asks a table which host it is starting. If that
    lookup could raise for Kiro, every ordinary session would have gained a way
    to fail before its process exists -- a new failure mode on the default path
    in service of adapter support, which is precisely what H13 forbids.

    Total by construction on three counts, each asserted rather than assumed: the
    id is a key of the table, the table is a literal in the same module as the
    class it names, and the class takes no constructor argument that could be
    missing or malformed.
    """
    import inspect

    from kiro_crew.acp import harness as pkg
    from kiro_crew.acp.harness import _HARNESSES

    assert ACP_BACKEND_KIRO in _HARNESSES
    assert _HARNESSES[ACP_BACKEND_KIRO] is KiroHarness
    # No __init__ of its own, so instantiation reduces to object.__new__.
    assert KiroHarness.__init__ is object.__init__
    # The table is a literal beside the class, not built from configuration or
    # from a registry another module can empty.
    source = inspect.getsource(pkg)
    assert "_HARNESSES: dict[str, type[HarnessAdapter]] = {" in source


def test_the_runtime_resolves_the_kiro_harness_without_spawning():
    """A Kiro runtime answers every per-host question with no process and no I/O.

    This is the H13 property from the runtime's side: constructing a Kiro runtime
    and reading its host's answers must not raise, must not require an argument
    the caller did not have before, and must not touch the filesystem.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_KIRO)
    harness = rt._harness
    assert isinstance(harness, KiroHarness)
    assert harness.teardown.method == METHOD_SESSION_TERMINATE
    assert harness.protocol_version == "2025-08-22"
    assert harness.verifies_agent_activation is True


def test_a_projection_only_bare_runtime_still_resolves_its_host():
    """The agent projection is reachable on a runtime built without __init__.

    Some callers need only the projection and construct a bare runtime with
    ``object.__new__``, setting the two fields they care about. The harness lookup
    has to survive that, or those callers would have gained a failure mode too.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    rt = object.__new__(AcpRuntime)
    rt._acp_backend = ACP_BACKEND_KAS
    assert isinstance(rt._harness, KasHarness)


# ── Structural: no runtime coupling ──


@pytest.mark.parametrize("module", ["base", "_common", "kiro", "kas", "codex", "__init__"])
def test_no_harness_module_imports_the_runtime(module):
    """The harness layer never reaches back into ``AcpRuntime``.

    A harness that held a runtime could grow a hidden coupling, and the next
    backend's author would have to reproduce it without being told. The one
    exception is the error TYPE, which lives in ``session_handle`` and is imported
    inside function bodies rather than at module scope.
    """
    from kiro_crew.acp import harness as pkg

    path = Path(pkg.__file__).parent / f"{module}.py"
    source = path.read_text(encoding="utf-8")
    assert "kiro_crew.acp.runtime" not in source, f"{module} imports the runtime"


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_no_harness_holds_state_beyond_its_backend_id(backend):
    """A fresh harness is interchangeable with any other for the same backend.

    Instance state here would make the runtime's answer depend on WHICH harness
    object it happened to hold, which is the coupling this layer removes.
    """
    harness = harness_for(backend)
    assert vars(harness) == {}


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_resolve_spawn_reads_only_its_context(backend):
    """The spawn seam takes one argument, so its argv is a function of it."""
    sig = inspect.signature(type(harness_for(backend)).resolve_spawn)
    assert list(sig.parameters) == ["self", "ctx"]


@pytest.mark.parametrize("module", ["kiro", "kas"])
def test_a_harness_reaches_its_helpers_through_their_defining_module(module):
    """Pre-spawn helpers are called as ``<module>.<name>``, never as a local name.

    A local binding cannot be patched at the site that defines it, so a test
    aiming there would run the real filesystem work and the real credential store
    while believing it had stubbed them.
    """
    from kiro_crew.acp import harness as pkg

    source = (Path(pkg.__file__).parent / f"{module}.py").read_text(encoding="utf-8")
    for helper in ("ensure_agent_materialized", "require_fork_governance"):
        assert f"agent_mod.{helper}" in source, f"{module}: {helper}"
        assert f"\n    {helper}," not in source, f"{module}: {helper} bound locally"


# ── Seam 3b: the session's MCP array ──


@pytest.mark.parametrize("backend", KIRO_FAMILY_BACKENDS)
def test_the_kiro_family_passes_the_requested_mcp_array_through(backend):
    """The caller's list reaches the wire unchanged on both shipped hosts.

    This is what keeps their session/new byte-identical to a request built before
    the seam existed. Identity, not equality: an equal copy would also pass a
    byte compare today and would hide a host that started rebuilding the list.
    """
    requested = [{"name": "kirocrew-core", "command": "x"}]
    out = harness_for(backend).session_mcp_servers(requested, agent_capabilities={})
    assert out is requested


@pytest.mark.parametrize("backend", KIRO_FAMILY_BACKENDS)
def test_the_kiro_family_ignores_the_advertised_capabilities(backend):
    """A kiro-family host accepts every transport Crew injects.

    Its answer must not move with what the handshake advertised, or a host that
    happened to advertise a narrow set would silently lose servers it can serve.
    """
    requested = [{"name": "a", "type": "sse"}, {"name": "b", "command": "x"}]
    harness = harness_for(backend)
    for capabilities in ({}, {"mcpCapabilities": {"http": True}}, {"mcpCapabilities": {}}):
        assert harness.session_mcp_servers(requested, agent_capabilities=capabilities) is requested


def test_codex_narrows_the_array_against_what_the_handshake_advertised():
    """The counterexample the two family assertions above must not swallow.

    codex reads no agent spec, so this array IS the session's tool surface, and one
    element whose transport the adapter never advertised fails the WHOLE
    ``session/new`` with ``-32600``.
    """
    harness = harness_for(ACP_BACKEND_CODEX)
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
def test_an_unknown_handshake_narrows_nothing_even_on_a_narrowing_host(capabilities):
    """Empty means "nothing is known", never "nothing is supported".

    A host that narrowed to nothing here would strip every tool from every session
    on an adapter build that simply did not advertise.
    """
    requested = [{"name": "a", "type": "sse"}, {"name": "b", "command": "x"}]
    out = harness_for(ACP_BACKEND_CODEX).session_mcp_servers(
        requested, agent_capabilities=capabilities
    )
    assert out is requested


@pytest.mark.asyncio
async def test_codex_spawns_with_a_credential_mask_on_the_plan(monkeypatch, tmp_path):
    """An enforced host carries the mask its routing cannot compensate for itself.

    The same assertion the registry-wide ratchet makes structurally, made here
    against a real plan so a harness that names the field without filling it fails.
    """
    from kiro_crew import acp_tool_gate as gate_mod
    from kiro_crew.acp import client as client_mod
    from kiro_crew.acp.harness import codex as codex_mod

    monkeypatch.setattr(
        client_mod, "_resolve_codex_acp_bin", lambda: (["/n/node", "/p/index.js"], "/s")
    )

    async def _preflight(_fn, backend, mode):
        assert (backend, mode) == (ACP_BACKEND_CODEX, "standard")
        return ("/h/.aws",)

    monkeypatch.setattr(client_mod, "_run_preflight_bounded", _preflight)
    monkeypatch.setattr(
        codex_mod.acp_tool_gate, "adapter_expose_files", lambda b, h: ("/h/.aws/config",)
    )
    ctx = dataclasses.replace(_ctx(tmp_path), sandbox_mode="standard")
    plan = await harness_for(ACP_BACKEND_CODEX).resolve_spawn(ctx)
    assert plan.extra_hidden_dirs == ("/h/.aws",)
    assert plan.extra_expose_files == ("/h/.aws/config",)
    assert gate_mod.routing_for(ACP_BACKEND_CODEX) is not gate_mod.Routing.AGENT_SPEC


def test_the_mcp_seam_is_a_transform_not_an_addition():
    """The seam takes the caller's list IN, which is what lets a host narrow it.

    A host with no agent spec has nothing but this array describing its tool
    surface, and one element whose transport it never advertised can cost the
    whole session rather than that one server. A field on SessionExtras could
    only ADD, so such a host could not be served at all.
    """
    sig = inspect.signature(HarnessAdapter.session_mcp_servers)
    assert list(sig.parameters) == ["self", "requested", "agent_capabilities"]
    assert sig.parameters["agent_capabilities"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_runtime_asks_the_host_on_both_session_start_paths():
    """Both session-start requests go through the seam, not just session/new.

    session/load RE-initializes the session's servers, so an array that host
    refuses does not merely fail to add tools -- it takes them away from a
    conversation that already had them.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    for method in (AcpRuntime.create_session, AcpRuntime.load_session):
        source = inspect.getsource(method)
        assert "self._harness.session_mcp_servers(" in source, method.__name__
        assert "agent_capabilities=self._agent_capabilities" in source, method.__name__


def test_the_runtime_retains_the_handshakes_agent_capabilities():
    """The narrowing input is captured at handshake and defaults to empty.

    Empty must read as "nothing is known", never as "nothing is supported": a
    harness that narrowed to nothing on an unknown host would strip every tool
    from every session.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_KIRO)
    assert rt._agent_capabilities == {}


# ── Seam 1b: the spawn's credential mask ──


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", KIRO_FAMILY_BACKENDS)
async def test_the_kiro_family_needs_no_credential_mask(
    found_binary, kiro_gates_pass, monkeypatch, tmp_path, backend
):
    """Both shipped hosts spawn with an empty mask, which is why their argv is unchanged.

    Their privileged tools ask by construction, so there is nothing for an OS
    boundary to compensate for.
    """
    from kiro_crew.acp.harness import kas as kas_module

    async def _vault():
        return False

    monkeypatch.setattr(kas_module, "vault_holds_identity_off_loop", _vault)
    plan = await harness_for(backend).resolve_spawn(_ctx(tmp_path))
    assert plan.extra_hidden_dirs == ()
    assert plan.extra_expose_files == ()


def test_the_spawn_mask_reaches_the_sandbox():
    """The mask is applied, not merely returned.

    A plan that carried a mask the sandbox call never read would look correct in
    every harness test and still spawn the process unmasked.
    """
    import inspect as _inspect

    from kiro_crew.acp.runtime import AcpRuntime

    source = _inspect.getsource(AcpRuntime._spawn_admitted)
    call = source.split("wrap_argv_async(")[1].split(")")[0]
    assert "extra_hidden_dirs=plan.extra_hidden_dirs" in call
    assert "extra_expose_files=plan.extra_expose_files" in call


def test_an_enforced_host_may_not_spawn_without_a_mask():
    """A host this core's tool gate ENFORCES must carry a credential mask.

    Vacuous while every host the shared-process runtime serves asks by
    construction, and that is the point: it arms itself the moment an enforced
    host joins. For such a host the mask is the ONLY thing between a third-party
    binary and the operator's credential homes, because ACP cannot make it ask
    about a passive read -- so an empty mask there is a missing control, not a
    simplification.

    Read off the class rather than by spawning: resolving a real mask touches the
    filesystem, and this asks a question about the harness's contract.
    """
    import inspect as _inspect

    from kiro_crew import acp_tool_gate
    from kiro_crew.acp.harness import _HARNESSES

    for backend, harness_cls in _HARNESSES.items():
        routing = acp_tool_gate.routing_for(backend)
        source = _inspect.getsource(harness_cls.resolve_spawn)
        if routing is acp_tool_gate.Routing.AGENT_SPEC:
            # Asks by construction: no mask needed, and none claimed.
            assert "extra_hidden_dirs" not in source, backend
            continue
        assert "extra_hidden_dirs=" in source, (
            f"{backend} is not AGENT_SPEC-routed, so its harness must resolve an "
            f"OS credential mask and put it on the plan"
        )


# ── Seam 1c: the spawn sees the sandbox tier ──


def test_the_spawn_context_carries_the_sandbox_tier():
    """A host must be able to see the tier its spawn will use.

    Two tiers hand back an UNWRAPPED child and drop the credential mask on the
    floor. A host whose privileged tools this core enforces has to REFUSE there
    rather than start unmasked, and it cannot tell without the tier.
    """
    fields = {f.name for f in dataclasses.fields(SpawnContext)}
    assert "sandbox_mode" in fields


def test_the_runtime_passes_its_own_configured_tier():
    """The tier handed over is the runtime's, not a default the harness assumes.

    A harness that fell back to "auto" would answer the refusal question about a
    tier the spawn is not using -- and "auto" is the permissive answer, so the
    mistake fails OPEN.
    """
    source = inspect.getsource(_runtime_module().AcpRuntime._resolve_spawn_plan)
    assert "sandbox_mode=self._sandbox_mode" in source


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", KIRO_FAMILY_BACKENDS)
async def test_the_kiro_family_ignores_the_sandbox_tier(
    found_binary, kiro_gates_pass, monkeypatch, tmp_path, backend
):
    """Neither shipped host reads the tier, so its argv cannot move with it.

    Both ask by construction and carry no mask, so there is nothing for a tier to
    invalidate -- and a spawn that changed shape with the tier would be a wire
    difference this PR does not have.
    """
    from kiro_crew.acp.harness import kas as kas_module

    async def _vault():
        return False

    monkeypatch.setattr(kas_module, "vault_holds_identity_off_loop", _vault)
    harness = harness_for(backend)
    plans = []
    for mode in ("off", "auto", "standard", "strict"):
        ctx = dataclasses.replace(_ctx(tmp_path), sandbox_mode=mode)
        plans.append(await harness.resolve_spawn(ctx))
    assert all(p.argv == plans[0].argv for p in plans)
    assert all(p.extra_hidden_dirs == () for p in plans)


@pytest.mark.asyncio
async def test_codex_refuses_a_tier_that_would_drop_its_mask(monkeypatch, tmp_path):
    """The counterexample the family assertion above must not swallow.

    An enforced host DOES read the tier, and on one that hands back an unwrapped
    child it must refuse rather than spawn: the mask is its only compensating
    control, and `wrap_argv` would drop it on the floor.
    """
    from kiro_crew.acp import client as client_mod

    async def _refuse(*_args, **_kwargs):
        raise RuntimeError("sandbox floor refused")

    monkeypatch.setattr(
        client_mod, "_resolve_codex_acp_bin", lambda: (["/n/node", "/p/index.js"], "/s")
    )
    monkeypatch.setattr(client_mod, "_run_preflight_bounded", _refuse)
    ctx = dataclasses.replace(_ctx(tmp_path), sandbox_mode="off")
    with pytest.raises(RuntimeError, match="sandbox floor refused"):
        await harness_for(ACP_BACKEND_CODEX).resolve_spawn(ctx)


def test_an_enforced_host_must_consult_the_sandbox_tier():
    """A host carrying a mask must also refuse where the mask would be dropped.

    Resolving a mask is not enough on its own: ``wrap_argv`` discards it on the
    ``off`` tier and on a backend-less host with unsandboxed exec opted in, so a
    harness that only resolved one would spawn a third-party binary with the
    operator's credential homes readable and nothing compensating.

    Vacuous while every host here asks by construction, and armed the moment one
    does not. Read off the class because the property is that the source consults
    the tier, which a passing spawn on this machine cannot show.
    """
    from kiro_crew import acp_tool_gate
    from kiro_crew.acp.harness import _HARNESSES

    for backend, harness_cls in _HARNESSES.items():
        if acp_tool_gate.routing_for(backend) is acp_tool_gate.Routing.AGENT_SPEC:
            continue
        source = inspect.getsource(harness_cls.resolve_spawn)
        assert "sandbox_mode" in source, (
            f"{backend} carries a credential mask, so its harness must consult "
            f"ctx.sandbox_mode and refuse where the mask would be dropped"
        )


def _runtime_module():
    """Imported lazily: the runtime pulls in the client, and this file is a leaf."""
    from kiro_crew.acp import runtime as runtime_mod

    return runtime_mod
