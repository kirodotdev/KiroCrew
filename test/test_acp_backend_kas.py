"""ACP backend selection: the ``kas`` (kiro-agent) backend identifier.

KAS is a third ACP backend alongside kiro-cli and claude-agent-acp. It is
selected by ``agent.acp_backend`` and is inert until explicitly configured, so
these tests pin two things: that the selector reaches the client, and that the
DEFAULT configuration still resolves to kiro-cli.

The validation test matters because an unrecognized backend string would pass
every ``_is_<backend>`` check and spawn kiro-cli — a typo'd config would drive
the wrong agent with no error at all.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABEL_KAS,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.providers import acp as providers_acp
from kiro_crew.providers.acp import AcpProvider, provider_label
from kiro_crew.session import SessionManager


def _build_provider(backend: str) -> AcpProvider:
    """Mirror test_acp_provider's helper: construct without spawning anything."""
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


class TestBackendPredicates:
    """The three predicates are mutually exclusive and total over the known set."""

    def test_empty_backend_is_kiro(self):
        provider = _build_provider(ACP_BACKEND_KIRO)
        assert provider.is_kiro_backend is True
        assert provider.is_kas_backend is False
        assert provider.is_claude_backend is False
        assert provider.is_acp_runtime_backend is True

    def test_kas_backend(self):
        provider = _build_provider(ACP_BACKEND_KAS)
        assert provider.is_kas_backend is True
        assert provider.is_kiro_backend is False
        assert provider.is_claude_backend is False
        assert provider.is_acp_runtime_backend is True

    def test_claude_backend_unchanged(self):
        provider = _build_provider(ACP_BACKEND_CLAUDE)
        assert provider.is_claude_backend is True
        assert provider.is_kiro_backend is False
        assert provider.is_kas_backend is False
        assert provider.is_acp_runtime_backend is False

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
    def test_exactly_one_predicate_holds_for_every_known_backend(self, backend):
        provider = _build_provider(backend)
        held = [
            provider.is_kiro_backend,
            provider.is_claude_backend,
            provider.is_kas_backend,
            provider.is_codex_backend,
        ]
        assert sum(held) == 1

    @pytest.mark.parametrize(
        "backend", sorted({ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS})
    )
    def test_acp_runtime_backend_is_the_positive_form_of_not_claude(self, backend):
        # The four provider sites that used to read ``not is_claude_backend``
        # now read ``is_acp_runtime_backend``; the two must stay equivalent for the
        # backends that conversion covered, so it is behavior-preserving.
        #
        # Scoped to those three deliberately, NOT to ACP_BACKENDS_KNOWN. The
        # equivalence is an artifact of a world with one non-runtime harness: any
        # further harness that is also off AcpRuntime (codex is the first) makes
        # "not claude" and "not on the runtime" different questions. Widening the
        # parametrisation would demand that every new harness be claude-shaped,
        # which is the negation-based reasoning H5/H6 exist to retire; membership in
        # ACP_BACKENDS_ACP_RUNTIME is the durable form and is pinned separately.
        provider = _build_provider(backend)
        assert provider.is_acp_runtime_backend is (not provider.is_claude_backend)

    def test_codex_is_not_on_the_acp_runtime(self):
        """The membership the narrowing above defers to, pinned so the claim holds.

        codex-acp is one process per session, so it belongs on AcpClient. Adding it
        to ACP_BACKENDS_ACP_RUNTIME would route an activated codex session onto
        AcpRuntime + AcpSessionHandle — the wrong transport for a per-session Node
        adapter — and without this assertion that edit would land green.
        """
        assert _build_provider(ACP_BACKEND_CODEX).is_acp_runtime_backend is False


class TestUnknownBackendRejected:
    """A typo must fail loudly rather than silently driving kiro-cli."""

    def test_unknown_backend_raises(self):
        with patch("kiro_crew.providers.acp.AcpClient"):
            with pytest.raises(ValueError) as exc:
                AcpProvider(acp_backend="bogus")
        assert "bogus" in str(exc.value)

    def test_error_names_the_accepted_values(self):
        with patch("kiro_crew.providers.acp.AcpClient"):
            with pytest.raises(ValueError) as exc:
                AcpProvider(acp_backend="Kas")  # case matters
        message = str(exc.value)
        assert ACP_BACKEND_KAS in message
        assert ACP_BACKEND_CLAUDE in message


class TestProviderLabel:
    """``provider_label`` is the single producer of the persisted backend key."""

    def test_labels_each_backend(self):
        assert provider_label(_build_provider(ACP_BACKEND_KIRO)) == PROVIDER_LABEL_DEFAULT
        assert provider_label(_build_provider(ACP_BACKEND_CLAUDE)) == PROVIDER_LABEL_CLAUDE
        assert provider_label(_build_provider(ACP_BACKEND_KAS)) == PROVIDER_LABEL_KAS

    def test_non_acp_provider_falls_back_to_the_default(self):
        assert provider_label(object()) == PROVIDER_LABEL_DEFAULT

    def test_spec_mock_with_kiro_backend_is_not_read_as_claude(self):
        """Resolve from the backend STRING, not the ``is_*_backend`` properties.

        ``MagicMock(spec=AcpProvider)`` constrains attribute names but not their
        values, so every ``is_*_backend`` property on a spec'd mock is a truthy
        Mock. Reading properties here would label every mocked kiro session in
        the suite as claude — silently corrupting session-map persistence.
        """
        mock_provider = MagicMock(spec=AcpProvider)
        mock_provider.client = MagicMock()
        mock_provider.client.backend = ACP_BACKEND_KIRO
        assert provider_label(mock_provider) == PROVIDER_LABEL_DEFAULT


class TestProviderLabelAfterStartup:
    """The label must survive the client swap a successful start performs.

    ``_start_kiro_runtime_impl`` replaces the placeholder ``AcpClient`` with an
    ``AcpSessionProvider``, so a provider only looks like its constructor
    arguments until it actually starts. Constructing a provider and reading the
    label without starting it — as the tests above do — cannot see that.
    """

    @staticmethod
    def _started(backend: str) -> AcpProvider:
        provider = _build_provider(backend)
        runtime = MagicMock(spec=AcpRuntime)
        runtime.acp_backend = backend
        session_provider = AcpSessionProvider(MagicMock(), runtime)
        provider._client = session_provider  # type: ignore[assignment]
        return provider

    def test_started_kas_provider_keeps_the_kas_label(self):
        assert provider_label(self._started(ACP_BACKEND_KAS)) == PROVIDER_LABEL_KAS

    def test_started_kiro_provider_keeps_the_default_label(self):
        assert provider_label(self._started(ACP_BACKEND_KIRO)) == PROVIDER_LABEL_DEFAULT

    def test_a_bare_session_provider_resolves_too(self):
        """The shape ``_create_shared_session`` hands out for subagents."""
        runtime = MagicMock(spec=AcpRuntime)
        runtime.acp_backend = ACP_BACKEND_KAS
        assert provider_label(AcpSessionProvider(MagicMock(), runtime)) == PROVIDER_LABEL_KAS


class TestConfigThreading:
    """The config value must reach the provider through the REAL factory.

    Asserting on the factory (not on a hand-built provider) is what stops a
    future refactor from dropping the kwarg silently.
    """

    def test_default_config_is_kiro(self):
        cfg = KiroCrewConfig()
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO
        provider = cfg.create_provider_factory()(session_key="test:default", agent="")
        assert provider.is_kiro_backend is True
        assert provider.is_kas_backend is False

    def test_configured_kas_reaches_the_provider(self):
        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        provider = cfg.create_provider_factory()(session_key="test:kas", agent="")
        assert provider.is_kas_backend is True
        assert provider.client.backend == ACP_BACKEND_KAS


def _load_agent_config(agent_data: dict, tmp_path: Path) -> KiroCrewConfig:
    """Load a real config file, so the load constructor itself is exercised.

    Written under pytest's ``tmp_path`` so an interrupted worker cannot strand
    the file in the host temp dir.
    """
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"agent": agent_data}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        return KiroCrewConfig.load()


class TestConfigRoundTrip:
    """What an operator persists must survive an actual load from disk.

    Setting ``cfg.agent.acp_backend`` in memory (as the tests above do) proves
    nothing about persistence: ``load()`` builds ``AgentConfig`` by listing every
    field explicitly, so a field it forgets is silently dropped to its default —
    and because ``to_dict`` serializes generically, the next ``save()`` then
    writes that default back over the operator's setting.
    """

    def test_the_field_is_consumed_on_load(self, tmp_path):
        """The bug this class exists for: the value must reach the dataclass.

        Asserted through a selectable value, since an unselectable one degrades
        and so cannot distinguish 'consumed' from 'dropped'.
        """
        cfg = _load_agent_config({"acp_backend": ACP_BACKEND_KIRO, "streaming": False}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO
        assert cfg.agent.streaming is False

    def test_absent_key_loads_as_the_default(self, tmp_path):
        assert _load_agent_config({}, tmp_path).agent.acp_backend == ACP_BACKEND_KIRO

    def test_kas_survives_a_load_from_disk(self, tmp_path):
        """The selectable value must reach the provider, not degrade.

        This is the round trip the field exists for: ``load()`` lists every
        field explicitly, so one it forgets is dropped to the default and the
        next ``save()`` writes that default back over the operator's setting.
        """
        cfg = _load_agent_config({"acp_backend": ACP_BACKEND_KAS}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KAS
        assert cfg.to_dict()["agent"]["acp_backend"] == ACP_BACKEND_KAS
        provider = cfg.create_provider_factory()(session_key="test:rt", agent="")
        assert provider.is_kas_backend is True

    @pytest.mark.parametrize("bad", ["kiro-cli", "KAS", "claude_code", 7, None, []])
    def test_unselectable_values_degrade_to_the_default(self, bad, tmp_path):
        """A typo must not take the gateway down.

        ``AcpProvider`` raises on an unknown backend, so an unnormalized value
        would turn a config typo into a startup crash.
        """
        cfg = _load_agent_config({"acp_backend": bad}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO


class TestBackendThreading:
    """Every runtime this provider spawns must carry the selected backend.

    The provider constructs ``AcpRuntime`` at more than one point — the initial
    start and the respawn after the runtime dies mid ``session/load`` — and a
    site that omits the kwarg silently spawns kiro-cli for a KAS session, which
    then persists under the kiro label. An AST ratchet rather than a behavioral
    test because the respawn needs a process-death race to reach.
    """

    def test_every_runtime_construction_passes_the_backend(self):
        source = Path(providers_acp.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "AcpRuntime"
        ]
        assert sites, "expected at least one AcpRuntime construction to guard"
        missing = [
            node.lineno
            for node in sites
            if not any(kw.arg == "acp_backend" for kw in node.keywords)
        ]
        assert not missing, f"AcpRuntime built without acp_backend at lines {missing}"


class TestCompanionRuntimeInheritsBackend:
    """A companion subagent runtime shares its parent's process, so it must
    share the parent's backend — resolving it independently would spawn a
    different agent than the session the subagent belongs to."""

    def test_parent_kwargs_carry_the_backend(self):
        mgr = MagicMock()
        provider = MagicMock()
        provider.client = MagicMock()
        provider.client.backend = ACP_BACKEND_KAS
        for attr in (
            "_sandbox_mode",
            "_extra_env",
            "_mcp_gateway_overlay",
            "_mcp_gateway_socket",
        ):
            setattr(provider.client, attr, None)
        mgr.get_provider.return_value = provider
        kwargs = SessionManager._parent_runtime_kwargs(mgr, "parent:key")
        assert kwargs["acp_backend"] == ACP_BACKEND_KAS


class TestNoImportCycle:
    """``config.loader`` must import standalone, before anything ACP.

    The gateway and desktop entrypoints import the config module first, so a
    module-scope import of ``kiro_crew.acp.types`` from here is a cycle: reaching
    that module executes the ``kiro_crew.acp`` package init, which imports the
    ACP client and runtime, which import this module back. In-process tests
    cannot see it — by the time they run, something has usually imported
    ``kiro_crew.acp`` already — so this asserts it in a fresh interpreter.
    """

    def test_config_loader_imports_alone(self):
        proc = subprocess.run(
            [sys.executable, "-c", "import kiro_crew.config.loader"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"standalone import failed:\n{proc.stderr}"

    def test_the_backend_normalizer_works_in_that_interpreter(self):
        """Deferring the import must not break the value it resolves."""
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import kiro_crew.config.loader as m;"
                "print(m._normalize_acp_backend(''), m._normalize_acp_backend('nope'), sep='|')",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip().split("|") == ["", ""]
