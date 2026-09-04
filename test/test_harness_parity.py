"""Structural pins for the harness-parity invariants.

Kiro Crew drives one first-class harness, ``kiro-cli``, and adapts the others.
Each test here closes one invariant from
``docs/system-specs/modules/harness-parity.md`` by its id, so a change that
degrades the Kiro path goes red here rather than at an operator's first message.

Two invariants (H13, H14) are properties of a *change* rather than of a tree and
have no deterministic form; they are carried by the ``harness-parity`` rule in
``AUTOSDE.yaml``. The added-line half of H5 lives in
``scripts/check_harness_parity.py`` and is exercised by its ``--test`` mode,
which :func:`test_added_line_gate_self_test_passes` runs.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys
from dataclasses import fields
from unittest.mock import MagicMock

import pytest

from kiro_crew import acp_backends
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import runtime as acp_runtime
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
    ACP_CLIENT_CAPABILITIES,
    KAS_CLIENT_CAPABILITIES,
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_CODEX,
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABEL_KAS,
)
from kiro_crew.acp_backends import (
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_KIRO_SLASH_COMMANDS,
    ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    BASELINE_SELECTABLE_BACKENDS,
    selectable_backends,
)
from kiro_crew.config.loader import AgentConfig, _normalize_acp_backend
from kiro_crew.providers import acp as providers_acp

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GATE_PATH = os.path.join(_REPO_ROOT, "scripts", "check_harness_parity.py")


def _field_default(name: str) -> object:
    for f in fields(AgentConfig):
        if f.name == name:
            return f.default
    raise AssertionError(f"AgentConfig has no field {name!r}")


def _field_enum(name: str) -> object:
    for f in fields(AgentConfig):
        if f.name == name:
            return f.metadata.get("enum")
    raise AssertionError(f"AgentConfig has no field {name!r}")


# ---------------------------------------------------------------------------
# Group A: Kiro is the default and the floor
# ---------------------------------------------------------------------------


def test_kiro_is_the_default_backend() -> None:
    """H1: configuring nothing yields the Kiro harness."""
    assert _field_default("acp_backend") == ACP_BACKEND_KIRO


def test_kiro_is_always_selectable() -> None:
    """H1: the Kiro harness is never gated behind a preview flag or an edition.

    Every other member is a policy decision; this one is the floor. Without it
    an operator can persist a configuration in which no harness is selectable.

    Reads the registry, not a frozen constant: the selectable set is now extended
    at boot by an edition, so a snapshot taken at import would not be the set the
    dashboard offers. The floor is a property of the BASELINE, which is what makes
    it independent of whatever an edition registers on top.
    """
    assert ACP_BACKEND_KIRO in BASELINE_SELECTABLE_BACKENDS
    assert ACP_BACKEND_KIRO in selectable_backends()


def test_provider_enum_is_acp_only() -> None:
    """H2: a harness is chosen at ``acp_backend``, never as a second provider.

    A second ``agent.provider`` value would build its factory outside
    ``create_provider_factory`` and route around every invariant below it.
    """
    assert _field_enum("provider") == ["acp"]
    assert _field_default("provider") == "acp"


@pytest.mark.parametrize("persisted", ["", "kas", "byo-harness", "claude", None, 7])
def test_unselectable_backend_degrades_to_kiro(persisted: object) -> None:
    """H3: an unusable persisted value degrades to Kiro and never raises.

    Includes the non-string shapes a hand-edited config.json can hold: a gate
    that raises here turns a typo into a gateway that will not boot.

    ``claude`` is in the list on purpose, and now for the opposite reason: it ships in
    the public baseline, so it must SURVIVE rather than degrade. The assertion below is
    conditional on membership precisely so this row proves the gate reads the registry
    instead of hardcoding a verdict. ``byo-harness`` covers the unknown-id case, and a
    known id that policy has denied is covered in
    ``test_agent_backend_governance.py``.
    """
    resolved = _normalize_acp_backend(persisted)
    assert resolved in selectable_backends()
    if persisted not in selectable_backends():
        assert resolved == ACP_BACKEND_KIRO


def test_registering_a_backend_makes_it_survive_load() -> None:
    """H3 + H8: the gate reads the registry per call, so registration is the seam.

    This is the whole point of the registry: an edition calls
    ``register_selectable_backend`` and the SAME persisted value that degraded a
    moment ago now survives, with no second gate and no code change anywhere else.
    Ordering is the edition's to get right -- registration must precede the first
    config load.

    Claude Code ships in the public baseline, so the degrading starting state is
    constructed here rather than borrowed from it. Both module sets are snapshotted:
    ``register_selectable_backend`` writes the baseline too, and restoring only the
    effective set would leak a widened baseline into the rest of the run.
    """
    baseline_before = set(acp_backends._baseline)
    before = set(acp_backends._selectable)
    try:
        acp_backends._baseline.discard(ACP_BACKEND_CLAUDE)
        acp_backends._selectable.discard(ACP_BACKEND_CLAUDE)
        assert _normalize_acp_backend(ACP_BACKEND_CLAUDE) == ACP_BACKEND_KIRO

        acp_backends.register_selectable_backend(ACP_BACKEND_CLAUDE)
        assert _normalize_acp_backend(ACP_BACKEND_CLAUDE) == ACP_BACKEND_CLAUDE
    finally:
        acp_backends._baseline.clear()
        acp_backends._baseline.update(baseline_before)
        acp_backends._selectable.clear()
        acp_backends._selectable.update(before)
    assert _normalize_acp_backend(ACP_BACKEND_CLAUDE) == ACP_BACKEND_CLAUDE


def test_config_load_never_reads_the_platform_context(monkeypatch) -> None:
    """H3: the load path must not reach the platform context, at all.

    ``current_context()``'s lazy branch LOADS CONFIG, so any lookup that reaches it
    from inside ``KiroCrewConfig.load()`` re-enters that load and recurses to the
    stack limit — and a broad ``except`` around it does not save the caller, it
    downgrades the crash to a silently wrong backend.

    Nothing in the current load path reaches it, which is exactly why this guard is
    worth pinning: the natural next feature here is a per-deployment policy on which
    backend may run, and resolving a policy is precisely the call that would
    reintroduce the cycle.

    RECORDS the reach with a spy rather than raising on it. A raising stub cannot
    prove this: ``resolve_selected_backend``'s callers catch broadly, so an
    ``AssertionError`` is swallowed and the fallback returns the value the test
    would then assert — passing against the very implementation it rejects.
    """
    from kiro_crew.platform import context as pc

    reached: list = []
    monkeypatch.setattr(pc, "current_context", lambda: reached.append("current_context"))
    monkeypatch.setattr(pc, "installed_context", lambda: reached.append("installed_context"))

    for value in ("", "kas", "byo-harness", "claude", None, 7):
        assert _normalize_acp_backend(value) in ACP_BACKENDS_KNOWN

    assert reached == [], f"config normalization reached the platform context: {reached}"


def test_selectability_has_one_logged_gate() -> None:
    """H4: ``resolve_selected_backend`` is the ONLY gate, and it logs.

    This replaces the previous two-mechanism guarantee, deliberately. The old
    contract kept a static ``enum`` on the field as a second, SILENT gate:
    ``validate_config_data`` deletes an out-of-enum value before the loader sees
    it, and the degrade log only fires on a non-empty value, so a backend an
    edition had legitimately registered was stripped from config.json with no log
    line at all — the exact failure the old H4 text described as a hazard and did
    not prevent. Removing the enum makes the logged degrade the single gate.

    Pinned here rather than left to prose because re-adding ``enum=`` would look
    like a harmless tidy-up and would silently restore the strip.
    """
    assert _field_enum("acp_backend") is None, (
        "acp_backend must NOT declare a static enum: it is frozen at import, "
        "before an edition registers its backends, and validate_config_data "
        "deletes out-of-enum values silently"
    )


# ---------------------------------------------------------------------------
# Group B: identity is tested positively
# ---------------------------------------------------------------------------


def test_session_sharing_is_opt_in() -> None:
    """H6: session-sharing eligibility is membership, not the absence of claude.

    The property must read the set, so a harness added to ``ACP_BACKENDS_KNOWN``
    and nowhere else is ineligible by default instead of inheriting eligibility.
    """
    source = inspect.getsource(providers_acp.AcpProvider.is_session_sharing_eligible.fget)
    assert "ACP_BACKENDS_SESSION_SHARING" in source
    assert "not " not in source.split('"""')[-1], "eligibility derived from a negation"

    assert ACP_BACKEND_KIRO in ACP_BACKENDS_SESSION_SHARING
    # claude-agent-acp runs one process per session (AcpClient), so it cannot
    # host a multiplexed subagent session however the call site is written.
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_SESSION_SHARING


def test_steer_is_opt_in() -> None:
    """H6: the ``_session/steer`` extension is claimed by membership."""
    source = inspect.getsource(acp_client.AcpClient.supports_steer.fget)
    assert "ACP_BACKENDS_STEER" in source
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_STEER
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_STEER


def test_mcp_config_hot_reload_is_opt_in() -> None:
    """H6: skipping the post-sync session reset is claimed by membership.

    The gate must read the set — a harness added to ``ACP_BACKENDS_KNOWN`` must
    not inherit the skip, because a wrong member leaves a freshly installed
    server unmounted with nothing red to say why. KAS receives its servers on
    ``session/new`` and claude reads no agent file, so neither is a member.
    """
    from kiro_crew import mcp_hot_reload

    source = inspect.getsource(mcp_hot_reload.mcp_hot_reload_supported)
    assert "ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD" in source
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD
    assert ACP_BACKEND_KAS not in ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD


def test_steer_capability_declares_its_stamp() -> None:
    """H15: a provider that can steer must also report WHEN it steered.

    The pair is load-bearing because the failure of the second half is silent.
    A sleeping ``wait`` is one of the few regions where a steer cannot be
    injected — the backend needs a model-inference boundary and an in-flight
    tool call is the absence of one — so the keepalive route ends the sleep by
    comparing ``last_steer_monotonic`` against the reading taken when the sleep
    began. A provider that overrides ``supports_steer`` and inherits the default
    stamp accepts steers correctly and never interrupts a wait, with nothing
    raised and nothing logged.

    The route reads the stamp defensively (a keepalive must not fail on the ping
    that stops the watchdog killing the session mid-sleep), which is exactly why
    the guarantee has to live here instead: a defensive read cannot tell "this
    backend does not steer" from "this backend forgot the stamp".
    """
    from kiro_crew.acp.session_provider import AcpSessionProvider  # noqa: F401
    from kiro_crew.providers.acp import AcpProvider  # noqa: F401
    from kiro_crew.providers.base import LLMProvider

    def _walk(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from _walk(sub)

    checked = []
    for cls in _walk(LLMProvider):
        if cls.supports_steer is LLMProvider.supports_steer:
            continue  # cannot steer, so has nothing to stamp
        assert cls.last_steer_monotonic is not LLMProvider.last_steer_monotonic, (
            f"{cls.__name__} overrides supports_steer but inherits the default "
            "last_steer_monotonic, so a steer it accepts can never end a sleeping wait"
        )
        checked.append(cls.__name__)

    # Fail-closed: an import that stopped registering the subclasses would make
    # the loop vacuous and the ratchet a no-op.
    assert len(checked) >= 2, f"expected at least 2 steer-capable providers, saw {checked}"


def test_is_kiro_cli_is_positive() -> None:
    """H7: the sandbox-delegation flag is membership at every spawn site.

    This is the one identity test that fails OPEN. ``wrap_argv`` treats it as
    "this harness carries its own internal sandbox, which cannot nest inside
    ours, so skip ours" — granted to a harness without one, it leaves the agent
    process unconfined. A negative form grants it to every future harness.
    """
    for spawn in (acp_runtime.AcpRuntime.spawn, acp_client.AcpClient.ensure_ready):
        source = inspect.getsource(spawn)
        for line in source.splitlines():
            if "is_kiro_cli=" not in line:
                continue
            value = line.split("is_kiro_cli=", 1)[1]
            assert (
                "not " not in value and "!=" not in value
            ), f"{spawn.__qualname__} derives is_kiro_cli from a negation: {line.strip()}"
            assert "ACP_BACKENDS_INTERNAL_SANDBOX" in value or value.strip().startswith(
                ("True", "False")
            ), f"{spawn.__qualname__} must use membership or a literal: {line.strip()}"

    assert ACP_BACKENDS_INTERNAL_SANDBOX == frozenset({ACP_BACKEND_KIRO}), (
        "only kiro-cli ships an internal OS sandbox; adding a member here waives "
        "Kiro Crew's own seatbelt for that harness on macOS"
    )


def test_capability_sets_are_subsets_of_known_backends() -> None:
    """H8: a capability cannot be granted to an identifier nothing recognizes.

    A member that is not in ``ACP_BACKENDS_KNOWN`` is dead config at best and a
    typo that silently grants nothing at worst.
    """
    for name, members in (
        # The registry, not a constant: ``register_selectable_backend`` already
        # refuses an unknown id, so this is the belt to that braces — a member
        # arriving some other way still has to be a backend the code recognizes.
        ("selectable_backends()", selectable_backends()),
        ("ACP_BACKENDS_SESSION_SHARING", ACP_BACKENDS_SESSION_SHARING),
        ("ACP_BACKENDS_STEER", ACP_BACKENDS_STEER),
        ("ACP_BACKENDS_INTERNAL_SANDBOX", ACP_BACKENDS_INTERNAL_SANDBOX),
        ("ACP_BACKENDS_ACP_RUNTIME", ACP_BACKENDS_ACP_RUNTIME),
        ("ACP_BACKENDS_COMPACT", ACP_BACKENDS_COMPACT),
        ("ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD", ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD),
    ):
        assert members <= ACP_BACKENDS_KNOWN, f"{name} names an unknown backend"


def test_unknown_backend_rejected_at_construction() -> None:
    """H8: an unrecognized harness id is refused, not silently spawned as Kiro.

    ``ACP_BACKEND_KIRO`` is the empty string, so a value that falls through every
    identity check spawns kiro-cli under a foreign label. Construction is where
    that has to stop.
    """
    with pytest.raises(ValueError, match="acp_backend"):
        providers_acp.AcpProvider(acp_backend="byo-harness")


# ---------------------------------------------------------------------------
# Group C: the Kiro path keeps its own machinery
# ---------------------------------------------------------------------------


def test_kiro_spawn_argv_keeps_its_own_branch() -> None:
    """H9: the Kiro branch keeps agent materialization and the model pin.

    kiro-cli discovers selectable modes from ``~/.kiro/agents/*.json`` at
    startup, so a missing agent file makes a later ``set_mode`` fail with "Mode
    not found"; and ``--model`` at spawn is the only way to run a model outside
    the agent's own provider. A dict-of-builders refactor that treats Kiro as one
    entry among N drops both without failing anything else.
    """
    source = inspect.getsource(acp_runtime.AcpRuntime._resolve_spawn_argv)
    assert "ensure_agent_materialized" in source
    assert '"--model"' in source
    assert '"--agent"' in source


def test_handshake_is_per_backend() -> None:
    """H10: no lowest-common-denominator handshake.

    Collapsing the two capability dicts into one every harness accepts silently
    downgrades what the Kiro session declares.
    """
    source = "\n".join(
        (
            inspect.getsource(acp_runtime.AcpRuntime.spawn),
            inspect.getsource(acp_runtime.AcpRuntime._spawn_admitted),
        )
    )
    assert "KAS_CLIENT_CAPABILITIES" in source and "ACP_CLIENT_CAPABILITIES" in source
    assert KAS_CLIENT_CAPABILITIES != ACP_CLIENT_CAPABILITIES


def test_every_known_backend_has_a_label() -> None:
    """H11: the provider label is a closed mapping and Kiro is its default.

    The label indexes resume compatibility, session-map persistence, and
    session-file cleanup routing. A harness with no label of its own persists as
    a Kiro session, and the map then prunes its id for want of a Kiro transcript.
    """
    labels = {
        ACP_BACKEND_KIRO: PROVIDER_LABEL_DEFAULT,
        ACP_BACKEND_CLAUDE: PROVIDER_LABEL_CLAUDE,
        ACP_BACKEND_KAS: PROVIDER_LABEL_KAS,
        ACP_BACKEND_CODEX: PROVIDER_LABEL_CODEX,
    }
    assert set(labels) == set(ACP_BACKENDS_KNOWN), (
        "a known backend has no PROVIDER_LABEL_* of its own, so it would persist "
        "under the kiro label — add one in acp/types.py and a branch in "
        "providers.acp.provider_label"
    )
    assert len(set(labels.values())) == len(labels), "two backends share a label"


def test_codex_is_selectable_and_answerable() -> None:
    """H1/H8: a harness may only be offered once the build can answer for it.

    Codex was withheld for one stated reason -- ``backend_install.py`` had no probe,
    so its install row could only read ``unknown`` and a failed session arrived with
    nothing to act on. The probe closes that, which is what makes the switch
    honest rather than merely present.

    Asserted TOGETHER on purpose: selectability without a probe is the exact state
    the withholding existed to prevent, so a future change that removed the probe
    while leaving the baseline entry would fail here rather than silently ship a
    switch with nothing behind it.
    """
    from kiro_crew.agent_sdk.backend_install import _PROBES

    assert ACP_BACKEND_CODEX in ACP_BACKENDS_KNOWN
    assert ACP_BACKEND_CODEX in BASELINE_SELECTABLE_BACKENDS
    assert ACP_BACKEND_CODEX in selectable_backends()
    assert ACP_BACKEND_CODEX in _PROBES, (
        "codex is offered in the switch, so backend_install must be able to say "
        "what is missing when a session fails to start"
    )


def test_codex_tool_calls_are_gated_before_it_is_offered() -> None:
    """A selectable harness must route its tool calls, or the switch is a trap.

    This is the invariant that makes admission mean something: the picker offering
    an id and the gate being armed for it are separate facts, and selectability
    without routing would put the operator's narrowing silently out of circuit.
    """
    from kiro_crew import acp_tool_gate

    verdict, _reason = acp_tool_gate.routing_verdict(ACP_BACKEND_CODEX)
    assert verdict is acp_tool_gate.Verdict.ROUTED
    assert acp_tool_gate.is_enforced(ACP_BACKEND_CODEX) is True
    assert acp_tool_gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX), (
        "ACP v1 cannot require a prompt for a passive read, so the credential "
        "homes must be denied at the OS boundary instead"
    )


def test_codex_carries_its_own_provider_label() -> None:
    """H11: the label is what keeps a codex session out of the kiro namespace.

    Resume compatibility, session-map persistence and session-file cleanup all index
    this key, so a codex session labelled ``acp`` would be resumed as kiro and then
    pruned for want of a kiro transcript.
    """
    client = MagicMock()
    client.backend = ACP_BACKEND_CODEX
    provider = MagicMock(spec=providers_acp.AcpProvider)
    provider.client = client
    assert providers_acp.provider_label(provider) == PROVIDER_LABEL_CODEX
    assert PROVIDER_LABEL_CODEX != PROVIDER_LABEL_DEFAULT


def test_model_switch_channel_is_opt_in() -> None:
    """H6: the config-option model channel is granted by membership, not negation.

    kiro-cli switches models with ``session/set_model``; the claude and codex
    adapters implement no such request and expose the model as a session config
    option instead. Read as ``not is_kiro`` this would hand the config-option path
    to every harness added later, and a harness that implements neither would
    silently no-op its model switch.
    """
    assert ACP_BACKEND_CLAUDE in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert ACP_BACKEND_KIRO not in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION <= ACP_BACKENDS_KNOWN
    source = "\n".join(
        (
            inspect.getsource(acp_client.AcpClient.set_model),
            inspect.getsource(acp_client.AcpClient._apply_startup_model),
        )
    )
    assert (
        "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION" in source
    ), "the model switch must read the membership set, not a per-backend literal"


def test_effort_channel_is_opt_in() -> None:
    """H6: the effort channel is granted by membership, not by "not claude".

    The two channels are separate opt-ins because a harness can have neither. Read
    as ``not is_claude_backend``, an adapter harness is handed kiro's ``/effort``
    slash command, which rides ``_kiro.dev/commands/execute`` — a verb it does not
    implement — so the push fails -32601 and the dashboard resets the session.
    """
    assert ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION <= ACP_BACKENDS_KNOWN
    assert ACP_BACKENDS_KIRO_SLASH_COMMANDS <= ACP_BACKENDS_KNOWN
    # Disjoint: a harness must not be told to push effort down both channels.
    assert not (ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION & ACP_BACKENDS_KIRO_SLASH_COMMANDS)
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_KIRO_SLASH_COMMANDS
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_KIRO_SLASH_COMMANDS
    source = "\n".join(
        (
            inspect.getsource(providers_acp.AcpProvider.change_effort),
            inspect.getsource(providers_acp.AcpProvider.clear_effort),
            inspect.getsource(providers_acp.AcpProvider._apply_effort_overlay),
            inspect.getsource(providers_acp.AcpProvider._apply_tool_search_overlay),
            inspect.getsource(providers_acp.AcpProvider.stream_command),
        )
    )
    assert "is_claude_backend" not in source, (
        "the effort, overlay and slash-command seams must read a membership set; "
        "a claude test here decides the path for every harness added later"
    )


def test_only_overlay_readers_are_written_to() -> None:
    """H6: the cli.json overlay is written only for the harnesses that read it.

    The clear side (``_clear_cli_overlay_effort``) is membership-gated, so a write
    gated on anything wider leaves a stale overlay in the user's workspace that no
    later clear can reach — and the overlay names an effort level, so a harness
    that DOES read the file later inherits a level nobody set for it.
    """
    for fn in (
        providers_acp.AcpProvider._apply_effort_overlay,
        providers_acp.AcpProvider._apply_tool_search_overlay,
    ):
        source = inspect.getsource(fn)
        assert (
            "ACP_BACKENDS_KIRO_SLASH_COMMANDS" in source
        ), f"{fn.__name__}: overlay write is not scoped to the overlay's readers"


def test_codex_spawn_keeps_its_own_branch() -> None:
    """H9/H10: codex resolves its own adapter and declares its own handshake.

    Falling through to the kiro branch would spawn kiro-cli under a codex label —
    the exact failure ACP_BACKENDS_KNOWN's rejection exists to prevent one step
    earlier — and folding its protocol version into the claude literal would make a
    future divergence a silent downgrade for whichever harness moved first.
    """
    spawn_source = inspect.getsource(acp_client.AcpClient._spawn)
    assert "_is_codex" in spawn_source
    assert "_resolve_codex_acp_bin" in spawn_source
    assert acp_client.PROTOCOL_VERSION_CODEX is not None
    assert "PROTOCOL_VERSION_CODEX" in inspect.getsource(acp_client.AcpClient._initialize_session)


def test_each_mcp_seam_is_spliced_only_for_its_own_harness() -> None:
    """H6: a per-harness hook must not reach a session of a different harness.

    Both defaults return ``[]``, so an ungated splice is inert in this tree — but an
    edition that overrides both hooks would hand a claude session codex's server
    entries and vice versa, and an entry whose transport the adapter does not
    advertise fails the whole ``session/new`` rather than being skipped. Pinned at
    the source, in the file's existing idiom, because the splice sits inside an
    async session-setup path with no unit-level seam.
    """
    for fn in (
        acp_client.AcpClient._new_session_following_substitution,
        acp_client.AcpClient._initialize_session,
    ):
        source = inspect.getsource(fn)
        if "_codex_session_mcp_servers" not in source:
            continue
        assert "if self._is_codex" in source, f"{fn.__name__}: codex seam spliced ungated"
        assert "if self._is_claude" in source, f"{fn.__name__}: claude seam spliced ungated"


def test_codex_mcp_seam_defaults_to_empty() -> None:
    """The public core sends no mcpServers for codex, exactly as for claude.

    kiro-cli receives its servers through ``--agent``; an edition overrides the seam.
    A non-empty default here would put servers on a public session that the adapter
    was never configured for.
    """
    client = acp_client.AcpClient.__new__(acp_client.AcpClient)
    assert client._codex_session_mcp_servers() == []


def test_model_preflight_allows_unknown_advertised_set() -> None:
    """H12: an empty or unknown advertised set means allow.

    Harnesses advertise model ids in their own spelling. A membership test that
    treats "not in this list" as unusable withholds every legitimate model the
    moment a second namespace exists.
    """
    assert acp_client.model_is_unusable("anything", set()) is False
    assert acp_client.model_is_unusable("anything", None) is False
    assert acp_client.model_is_unusable("absent", {"present"}) is True


# ---------------------------------------------------------------------------
# The added-line gate
# ---------------------------------------------------------------------------


def test_added_line_gate_self_test_passes() -> None:
    """H5: the diff-scoped gate still detects every shape it claims to.

    A gate that has silently stopped matching reads as a green signal, which is
    worse than no gate. CI runs this same self-test before the real check; this
    test makes a local ``pytest`` run catch a broken rule too.
    """
    result = subprocess.run(
        [sys.executable, _GATE_PATH, "--test"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_added_line_gate_reports_without_enforcing() -> None:
    """H5: with no base ref the gate reports and exits 0.

    The tree carries pre-existing negative tests in the dormant claude seam.
    Enforcing whole-tree would fail every PR until those are converted and charge
    the break to whoever pushed next, so the backlog is a report.
    """
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_BASE_REF"}
    result = subprocess.run(
        [sys.executable, _GATE_PATH],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "harness gate" in result.stdout


def test_added_line_gate_flags_a_planted_negative_test(tmp_path, monkeypatch) -> None:
    """H5: a violation in an explicitly-scanned file exits 1.

    Covers the exit-code contract the script's own ``--test`` mode cannot reach,
    since that mode only exercises the rule engine. The probe is planted in a
    temp tree with ``REPO_ROOT`` repointed at it — writing into the real
    ``src/`` would leave a stray module behind for every later test in the
    session if this one failed mid-way.
    """
    spec = importlib.util.spec_from_file_location("check_harness_parity", _GATE_PATH)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    sys.modules["check_harness_parity"] = gate
    spec.loader.exec_module(gate)
    monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))

    planted = "probe_harness.py"
    (tmp_path / planted).write_text(
        "def eligible(self):\n    return not self.is_claude_backend\n",
        encoding="utf-8",
    )
    assert gate.main([planted]) == 1

    (tmp_path / planted).write_text(
        "def eligible(self):\n    return self.is_kiro_backend\n",
        encoding="utf-8",
    )
    assert gate.main([planted]) == 0
