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

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp import runtime as acp_runtime
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SELECTABLE,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
    ACP_CLIENT_CAPABILITIES,
    KAS_CLIENT_CAPABILITIES,
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABEL_KAS,
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
    """
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_SELECTABLE


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
    """
    resolved = _normalize_acp_backend(persisted)
    assert resolved in ACP_BACKENDS_SELECTABLE
    if persisted not in ACP_BACKENDS_SELECTABLE:
        assert resolved == ACP_BACKEND_KIRO


def test_enum_and_selectability_are_separate() -> None:
    """H4: the config enum is the survival domain, not the selection domain.

    ``validate_config_data`` DELETES an out-of-enum value before the loader sees
    it, and the degrade log only fires on a non-empty value — so a preview
    harness missing from the enum vanishes with no log line at all. Everything
    the enum admits must therefore still pass ``_normalize_acp_backend``.
    """
    enum = _field_enum("acp_backend")
    assert isinstance(enum, list) and enum, "acp_backend must declare an enum"
    assert ACP_BACKEND_KIRO in enum
    for value in enum:
        assert _normalize_acp_backend(value) in ACP_BACKENDS_SELECTABLE


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
        ("ACP_BACKENDS_SELECTABLE", ACP_BACKENDS_SELECTABLE),
        ("ACP_BACKENDS_SESSION_SHARING", ACP_BACKENDS_SESSION_SHARING),
        ("ACP_BACKENDS_STEER", ACP_BACKENDS_STEER),
        ("ACP_BACKENDS_INTERNAL_SANDBOX", ACP_BACKENDS_INTERNAL_SANDBOX),
        ("ACP_BACKENDS_ACP_RUNTIME", ACP_BACKENDS_ACP_RUNTIME),
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
    source = inspect.getsource(acp_runtime.AcpRuntime.spawn)
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
    }
    assert set(labels) == set(ACP_BACKENDS_KNOWN), (
        "a known backend has no PROVIDER_LABEL_* of its own, so it would persist "
        "under the kiro label — add one in acp/types.py and a branch in "
        "providers.acp.provider_label"
    )
    assert len(set(labels.values())) == len(labels), "two backends share a label"


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
