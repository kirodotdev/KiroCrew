"""ACP backend selection: the ``kas`` (kiro-agent) backend identifier.

KAS is an ACP backend alongside kiro-cli and claude-agent-acp. An absent
``agent.acp_backend`` selects KAS, while every explicit choice remains stored.
These tests pin both the default and the selector reaching the client.

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
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_REPORT_USAGE,
    ACP_BACKENDS_SESSION_EVICTION,
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABEL_KAS,
)
from kiro_crew.acp_backends import STORED_BACKEND_KIRO as KIRO_STORED
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.providers import acp as providers_acp
from kiro_crew.providers.acp import AcpProvider, provider_label
from kiro_crew.session import SessionManager

#: The backends served by AcpRuntime rather than by one AcpClient per session,
#: stated independently of the frozenset the provider property reads. A test that
#: compared the property against that same frozenset would agree with itself no
#: matter which harnesses were in it.
ON_THE_ACP_RUNTIME = {ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX}

#: The backends whose turns land their spend in TurnUsage, spelled apart from the
#: set ``reports_turn_usage`` reads for the same reason.
REPORTS_TURN_USAGE = {
    ACP_BACKEND_KIRO,
    ACP_BACKEND_KAS,
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_GOOSE,
}


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
            provider.is_opencode_backend,
            provider.is_pi_backend,
            provider.is_goose_backend,
            provider.is_deepseek_backend,
            provider.is_copilot_backend,
            provider.is_strands_backend,
        ]
        assert sum(held) == 1

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
    def test_acp_runtime_backend_answers_positive_set_membership(self, backend):
        """The property IS membership in ACP_BACKENDS_ACP_RUNTIME, over every known
        backend rather than a hand-picked three.

        ``not is_claude_backend`` is not the same set (harness-parity H5/H6). Members are
        spelled in ``ON_THE_ACP_RUNTIME``, so opting a harness in is written twice.
        """
        provider = _build_provider(backend)
        assert provider.is_acp_runtime_backend is (backend in ON_THE_ACP_RUNTIME)
        assert ON_THE_ACP_RUNTIME == set(ACP_BACKENDS_ACP_RUNTIME)

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
    def test_reports_turn_usage_answers_positive_set_membership(self, backend):
        """A provider answering True without reporting usage would let memory
        consolidation take its billed empty replies as free and retry a span forever."""
        provider = _build_provider(backend)
        assert provider.reports_turn_usage is (backend in REPORTS_TURN_USAGE)
        assert REPORTS_TURN_USAGE == set(ACP_BACKENDS_REPORT_USAGE)

    def test_a_harness_off_the_runtime_need_not_be_claude(self):
        """The backends where the negation and the membership disagree.

        Without at least one non-claude harness off the runtime the test above would pin
        nothing.
        """
        off_the_runtime = sorted(ACP_BACKENDS_KNOWN - ON_THE_ACP_RUNTIME - {ACP_BACKEND_CLAUDE})
        assert off_the_runtime, (
            "every backend off the shared runtime is claude, so the property and "
            "``not is_claude_backend`` cannot be told apart -- pin membership against "
            "a harness that distinguishes them or this block is vacuous"
        )
        for backend in off_the_runtime:
            provider = _build_provider(backend)
            assert provider.is_claude_backend is False
            assert provider.is_acp_runtime_backend is False

    def test_codex_is_on_the_runtime_and_separately_in_the_eviction_set(self):
        """codex is a member that is not kiro-shaped, and two sets answer for it.

        One codex-acp process serves N sessions (ACP_BACKENDS_ACP_RUNTIME). Eviction is
        a separate question (ACP_BACKENDS_SESSION_EVICTION): ``session/close`` disposes the
        session. Both are pinned so neither answer stands in for the other.
        """
        assert _build_provider(ACP_BACKEND_CODEX).is_acp_runtime_backend is True
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_ACP_RUNTIME
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_EVICTION
        assert ACP_BACKEND_KIRO in ACP_BACKENDS_SESSION_EVICTION


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

    def test_default_config_is_kas(self):
        cfg = KiroCrewConfig()
        assert cfg.agent.acp_backend == ACP_BACKEND_KAS
        provider = cfg.create_provider_factory()(session_key="test:default", agent="")
        assert provider.is_kas_backend is True
        assert provider.is_kiro_backend is False

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

    ``load()`` lists every ``AgentConfig`` field explicitly, so a forgotten field drops to
    its default and the next ``save()`` overwrites the operator's setting.
    """

    def test_the_field_is_consumed_on_load(self, tmp_path):
        """The bug this class exists for: the value must reach the dataclass.

        Asserted through a selectable value, since an unselectable one degrades
        and so cannot distinguish 'consumed' from 'dropped'.
        """
        cfg = _load_agent_config({"acp_backend": KIRO_STORED, "streaming": False}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO
        assert cfg.agent.streaming is False

    def test_absent_key_loads_as_kas_and_every_reader_sees_it(self, tmp_path):
        """``to_dict()`` serves every reader, so it carries the RESOLVED harness.

        Only ``save()``'s disk write keeps an absent choice absent (see
        ``TestSaveKeepsAnAbsentChoiceAbsent``).
        """
        cfg = _load_agent_config({}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KAS
        assert cfg.to_dict()["agent"]["acp_backend"] == ACP_BACKEND_KAS

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

    def test_explicit_kiro_choice_survives_a_load_from_disk(self, tmp_path):
        """A stored "kiro" is the Kiro CLI choice: the runtime id in memory,
        and "kiro" again for every reader of ``to_dict()``."""
        cfg = _load_agent_config({"acp_backend": KIRO_STORED}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO
        assert cfg.to_dict()["agent"]["acp_backend"] == KIRO_STORED

    def test_a_stored_empty_string_is_no_choice_and_loads_kas_silently(self, tmp_path, caplog):
        """Every install before this change holds "", and it must follow the default."""
        caplog.set_level("WARNING")
        cfg = _load_agent_config({"acp_backend": "", "member_acp_backend": ""}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KAS
        assert cfg.agent.member_acp_backend == ACP_BACKEND_KAS
        assert cfg.to_dict()["agent"]["acp_backend"] == ACP_BACKEND_KAS
        assert "Ignoring agent." not in caplog.text

    def test_a_stored_kiro_member_backend_reads_as_kas(self, tmp_path):
        """Crewmates run on KAS: a member field set to "kiro" is KAS for every reader."""
        cfg = _load_agent_config({"member_acp_backend": KIRO_STORED}, tmp_path)
        assert cfg.agent.member_acp_backend == ACP_BACKEND_KAS
        assert cfg.to_dict()["agent"]["member_acp_backend"] == ACP_BACKEND_KAS
        assert _load_agent_config({}, tmp_path).agent.member_acp_backend == ACP_BACKEND_KAS

    def test_the_member_remap_is_logged_once(self, tmp_path, monkeypatch, caplog):
        """The remap is visible: one info line names the stored and the used value."""
        from kiro_crew.config import loader_builders

        monkeypatch.setattr(loader_builders, "_member_remap_logged", False)
        with caplog.at_level("INFO", logger="kiro_crew.config.loader"):
            _load_agent_config({"member_acp_backend": KIRO_STORED}, tmp_path)
            _load_agent_config({"member_acp_backend": KIRO_STORED}, tmp_path)
        lines = [r.getMessage() for r in caplog.records if "member_acp_backend" in r.getMessage()]
        assert len(lines) == 1
        assert repr(KIRO_STORED) in lines[0] and repr(ACP_BACKEND_KAS) in lines[0]

    def test_a_kiro_member_backend_starts_the_member_dm_on_kas(self, tmp_path):
        """The member DM gets KAS; a non-member session keeps the kiro-cli default."""
        cfg = _load_agent_config(
            {"acp_backend": KIRO_STORED, "member_acp_backend": KIRO_STORED}, tmp_path
        )
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO
        factory = cfg.create_provider_factory()
        member = factory(session_key="dashboard:member-oncall", agent="")
        assert member.is_kas_backend is True
        plain = factory(session_key="dashboard:chat-1", agent="")
        assert plain.is_kiro_backend is True

    def test_the_runtime_kiro_id_never_reaches_a_config_reader(self, tmp_path):
        """A Kiro CLI harness set in memory reads back as "kiro", never "" (no choice)."""
        cfg = _load_agent_config({}, tmp_path)
        cfg.agent.acp_backend = ACP_BACKEND_KIRO
        cfg.agent.member_acp_backend = ACP_BACKEND_KIRO
        agent = cfg.to_dict()["agent"]
        assert agent["acp_backend"] == KIRO_STORED
        assert agent["member_acp_backend"] == KIRO_STORED

    @pytest.mark.parametrize("bad", [7, [], None])
    def test_wrong_typed_explicit_values_take_the_kiro_floor(self, bad, tmp_path):
        """A malformed explicit value differs from an absent key.

        Validation keeps that distinction before its type pass, so a bad stored
        choice cannot silently become the KAS product default.
        """
        cfg = _load_agent_config({"acp_backend": bad}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO

    @pytest.mark.parametrize("bad", ["kiro-cli", "KAS", "claude_code", "byo-harness"])
    def test_unselectable_explicit_values_degrade_to_the_kiro_floor(self, bad, tmp_path):
        """A typo must not take the gateway down, and must not pick KAS.

        ``AcpProvider`` raises on an unknown backend, so an unnormalized value
        would turn a config typo into a startup crash. Only an ABSENT key earns
        the KAS default; a bad explicit value takes the Kiro CLI floor
        (harness-parity H3).
        """
        cfg = _load_agent_config({"acp_backend": bad}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO

    def test_masked_config_response_reports_the_resolved_default(self, tmp_path):
        """GET /api/config/kirocrew must name the harness actually in use.

        The dashboard reads ``agent.acp_backend`` from the masked response, which
        is built from ``to_dict()``.
        """
        from kiro_crew.dashboard.handlers.core import _masked_config_dict

        cfg = _load_agent_config({}, tmp_path)
        assert _masked_config_dict(cfg)["agent"]["acp_backend"] == ACP_BACKEND_KAS


class TestSaveKeepsAnAbsentChoiceAbsent:
    """An absent ``agent.acp_backend`` is "no choice", and only disk keeps it so.

    ``save()`` decides from the file on disk, not from an in-memory flag, so the
    resolved default never lands as a fake stored choice while every reader of
    ``to_dict()`` (``config get``, the dashboard) still sees the resolved value.
    """

    @pytest.fixture
    def home(self, tmp_path: Path):
        d = tmp_path / "crew"
        d.mkdir()
        with (
            patch("kiro_crew.config.loader.config_path", return_value=d / "config.json"),
            patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=d / "config.local.json",
            ),
            patch("kiro_crew.config.loader.config_dir", return_value=d),
            patch("kiro_crew.cli_config.config_path", return_value=d / "config.json"),
            patch(
                "kiro_crew.cli_config.config_local_path",
                return_value=d / "config.local.json",
            ),
            patch("kiro_crew.cli_config.sel"),
        ):
            yield d

    @staticmethod
    def _agent_on_disk(home: Path) -> dict:
        return json.loads((home / "config.json").read_text(encoding="utf-8")).get("agent", {})

    def test_config_get_reports_the_default_on_a_new_install(self, home, capsys):
        import argparse

        from kiro_crew.cli_config import _run_config_cmd

        (home / "config.json").write_text(json.dumps({"agent": {}}), encoding="utf-8")
        _run_config_cmd(argparse.Namespace(config_action="get", key="agent.acp_backend"))
        assert capsys.readouterr().out.strip() == ACP_BACKEND_KAS

    def test_the_boot_default_write_stores_no_choice(self, home):
        # No file yet: the boot path's ``KiroCrewConfig().save()``.
        KiroCrewConfig().save()
        assert "acp_backend" not in self._agent_on_disk(home)
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS

    def test_a_full_save_of_an_absent_choice_keeps_it_absent(self, home):
        (home / "config.json").write_text(
            json.dumps({"agent": {"streaming": False}}), encoding="utf-8"
        )
        cfg = KiroCrewConfig.load()
        assert cfg.to_dict()["agent"]["acp_backend"] == ACP_BACKEND_KAS
        cfg.save()
        agent = self._agent_on_disk(home)
        assert "acp_backend" not in agent
        assert agent["streaming"] is False

    def test_an_explicit_kiro_choice_survives_load_and_save(self, home):
        (home / "config.json").write_text(
            json.dumps({"agent": {"acp_backend": KIRO_STORED}}), encoding="utf-8"
        )
        KiroCrewConfig.load().save()
        assert self._agent_on_disk(home)["acp_backend"] == KIRO_STORED
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KIRO

    def test_a_kiro_member_backend_stays_kiro_on_disk(self, home):
        """Reading the member "kiro" as KAS never rewrites the stored value."""
        (home / "config.json").write_text(
            json.dumps({"agent": {"member_acp_backend": KIRO_STORED}}), encoding="utf-8"
        )
        cfg = KiroCrewConfig.load()
        assert cfg.agent.member_acp_backend == ACP_BACKEND_KAS
        cfg.save()
        assert self._agent_on_disk(home)["member_acp_backend"] == KIRO_STORED

    def test_a_full_save_of_a_stored_empty_string_keeps_it(self, home):
        """save() never turns "" (no choice) into a stored pick."""
        (home / "config.json").write_text(
            json.dumps({"agent": {"acp_backend": "", "member_acp_backend": ""}}),
            encoding="utf-8",
        )
        KiroCrewConfig.load().save()
        agent = self._agent_on_disk(home)
        assert agent["acp_backend"] == ""
        assert agent["member_acp_backend"] == ""

    @pytest.mark.parametrize("stored", [None, "", KIRO_STORED, ACP_BACKEND_KAS, "byo-harness"])
    @pytest.mark.parametrize("whole_agent_overlay", [False, True])
    def test_save_keeps_raw_harness_fields_after_overlay_subtraction(
        self, home, stored, whole_agent_overlay
    ):
        keys = ("acp_backend", "member_acp_backend")
        base_agent = {} if stored is None else dict.fromkeys(keys, stored)
        (home / "config.json").write_text(json.dumps({"agent": base_agent}), encoding="utf-8")
        overlay_agent = KiroCrewConfig.load().to_dict()["agent"] if whole_agent_overlay else {}
        overlay_agent.update(dict.fromkeys(keys, KIRO_STORED))
        overlay_agent["streaming"] = False
        (home / "config.local.json").write_text(
            json.dumps({"agent": overlay_agent}), encoding="utf-8"
        )

        KiroCrewConfig.load().save()

        saved = json.loads((home / "config.json").read_text(encoding="utf-8"))
        agent = saved.get("agent", {})
        assert {key: agent[key] for key in keys if key in agent} == base_agent
        assert "streaming" not in agent
        if whole_agent_overlay:
            assert agent == base_agent
            assert ("agent" in saved) is (stored is not None)

    def test_config_set_kiro_stores_kiro_and_get_prints_kiro(self, home, capsys):
        import argparse

        from kiro_crew.cli_config import _run_config_cmd

        (home / "config.json").write_text(json.dumps({"agent": {}}), encoding="utf-8")
        _run_config_cmd(
            argparse.Namespace(
                config_action="set", key="agent.acp_backend", value="kiro", file=None, local=False
            )
        )
        assert self._agent_on_disk(home)["acp_backend"] == KIRO_STORED
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KIRO
        capsys.readouterr()
        _run_config_cmd(argparse.Namespace(config_action="get", key="agent.acp_backend"))
        assert capsys.readouterr().out.strip() == KIRO_STORED

    def test_an_overlay_only_choice_counts_as_declared(self, home):
        """A pick in ``config.local.json`` is a choice; save() must not strip it.

        The overlay subtraction then keeps the overlay-owned value out of the
        base file, so the base stays free of a choice it never held.
        """
        (home / "config.json").write_text(json.dumps({"agent": {}}), encoding="utf-8")
        (home / "config.local.json").write_text(
            json.dumps({"agent": {"acp_backend": KIRO_STORED}}), encoding="utf-8"
        )
        cfg = KiroCrewConfig.load()
        assert cfg.to_dict()["agent"]["acp_backend"] == KIRO_STORED
        cfg.save()
        assert "acp_backend" not in self._agent_on_disk(home)
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KIRO


class TestBackendThreading:
    """Every runtime this provider spawns must carry the selected backend.

    A site that omits the kwarg spawns kiro-cli for a KAS session. An AST ratchet because
    the respawn site needs a process-death race to reach.
    """

    def test_every_runtime_construction_passes_the_backend(self):
        # The facade and the owner modules its members live in.
        trees = [
            ast.parse(Path(sys.modules[name].__file__).read_text(encoding="utf-8"))
            for name in (providers_acp.__name__, *providers_acp._ACP_PROVIDER_OWNERS)
        ]
        sites = [
            node
            for tree in trees
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

    Importing ``kiro_crew.acp.types`` here runs the package init, which imports this module
    back. A fresh interpreter is used because in-process tests already imported it.
    """

    def test_config_loader_imports_alone(self):
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", "import kiro_crew.config.loader"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert proc.returncode == 0, f"standalone import failed:\n{proc.stderr}"

    def test_the_backend_normalizer_works_in_that_interpreter(self):
        """Deferring the import must not break the value it resolves."""
        proc = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-c",
                "import kiro_crew.config.loader as m;"
                "print(m._normalize_acp_backend(''), m._normalize_acp_backend('nope'), sep='|')",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip().split("|") == ["", ""]


class TestAKiroCliPickRunsOnKasOnceV3IsConfirmed:
    """A stored Kiro CLI pick runs on KAS only once this process confirmed v3.

    kiro-cli serves a bare ``acp`` on its v3 (KAS) engine, and only the KAS path
    drives it; the gateway's probe is the evidence (the a-kiro-cli-choice-runs-on-kas
    entry in docs/decisions).
    """

    @pytest.fixture
    def registry(self, monkeypatch):
        from kiro_crew.agent_sdk import backends

        monkeypatch.setattr(backends, "_selectable", set(backends._selectable))
        monkeypatch.setattr(backends, "_host_unservable", {})
        monkeypatch.setattr(backends, "_kiro_cli_offers_kas", False)
        return backends

    def test_without_evidence_the_pick_keeps_kiro_cli(self, tmp_path, registry):
        cfg = _load_agent_config({"acp_backend": KIRO_STORED}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO
        assert cfg.to_dict()["agent"]["acp_backend"] == KIRO_STORED

    def test_a_confirmed_v3_kiro_cli_runs_the_pick_on_kas(self, tmp_path, registry):
        registry.confirm_kiro_cli_offers_kas()
        cfg = _load_agent_config({"acp_backend": KIRO_STORED}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KAS
        provider = cfg.create_provider_factory()(session_key="test:kiro-pick", agent="")
        assert provider.is_kas_backend is True

    def test_the_bundle_route_keeps_the_pick_on_kiro_cli(self, tmp_path, registry):
        """A v3 kiro-cli says nothing about the bundled engine, which may be absent."""
        registry.confirm_kiro_cli_offers_kas()
        cfg = _load_agent_config({"acp_backend": KIRO_STORED, "kas_bundle": True}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO
        provider = cfg.create_provider_factory()(session_key="test:bundle-pick", agent="")
        assert provider.is_kiro_backend is True

    def test_a_policy_that_denies_kas_keeps_kiro_cli(self, tmp_path, registry):
        registry.confirm_kiro_cli_offers_kas()
        registry.apply_selectable_denials({ACP_BACKEND_KAS})
        try:
            cfg = _load_agent_config({"acp_backend": KIRO_STORED}, tmp_path)
        finally:
            registry.apply_selectable_denials(set())
        assert cfg.agent.acp_backend == ACP_BACKEND_KIRO

    @pytest.mark.parametrize("bad", ["kiro-cli", "byo-harness", 7, True, [], None])
    def test_a_degraded_value_takes_the_floor_not_kas(self, bad, tmp_path, registry):
        """The floor a typo takes is not a pick (harness-parity H3)."""
        registry.confirm_kiro_cli_offers_kas()
        assert _load_agent_config({"acp_backend": bad}, tmp_path).agent.acp_backend == (
            ACP_BACKEND_KIRO
        )

    def test_a_malformed_value_is_warned_about_once(self, tmp_path, registry, caplog):
        caplog.set_level("WARNING")
        _load_agent_config({"acp_backend": 7}, tmp_path)
        assert caplog.text.count("Ignoring agent.acp_backend") == 1


class TestAProcessOutsideTheGatewayReachesTheSameVerdict:
    """A process that never runs the gateway floor still moves a Kiro CLI pick.

    ``ensure_kiro_cli_kas_verdict`` runs one bounded ``acp --help`` probe and promotes to
    KAS on positive evidence only (a-kiro-cli-choice-runs-on-kas in docs/decisions).
    On a defaulted or chosen KAS it reads the data home's record of a v3 start
    kiro-cli refused, as the floor does, so the load runs Kiro CLI instead.
    """

    @pytest.fixture
    def registry(self, monkeypatch):
        from kiro_crew.agent_sdk import backends

        monkeypatch.setattr(backends, "_selectable", set(backends._selectable))
        monkeypatch.setattr(backends, "_host_unservable", {})
        monkeypatch.setattr(backends, "_kiro_cli_offers_kas", False)
        monkeypatch.setattr(backends, "_kiro_cli_kas_probed", False)
        return backends

    def _stored_is_kiro(self, monkeypatch, is_kiro=True, bundle=False):
        """Stub the cheap stored-value reads the helper gates the probe on."""
        monkeypatch.setattr(
            "kiro_crew.config.loader_document.acp_backend_stored_as_kiro", lambda: is_kiro
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader_document.acp_backend_bundle_route", lambda: bundle
        )

    def _probe(self, monkeypatch, verdict):
        """Make the one bounded probe answer *verdict* without a real spawn."""
        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", lambda: verdict)

    @pytest.fixture(autouse=True)
    def _v3_engine_starts(self, _floor_monkeypatch):
        """The v3 start that follows a help verdict naming v3 answers without a refusal."""
        _floor_monkeypatch.setattr(
            "kiro_crew.agent_sdk.kas_engine_start_verdict",
            lambda binary=None, *, start=True: (True, None),
        )

    @staticmethod
    def _store(agent: dict, overlay: dict | None = None) -> None:
        """Write the data home's config documents, which the readers and the load both read."""
        from kiro_crew.config.loader_locations import config_local_path, config_path

        config_path().write_text(json.dumps({"agent": agent}), encoding="utf-8")
        if overlay is not None:
            config_local_path().write_text(json.dumps({"agent": overlay}), encoding="utf-8")

    @staticmethod
    def _record_start(tmp_path, monkeypatch, starts: bool) -> list[bool]:
        """Keep a real v3 start verdict for the pinned kiro-cli, as an earlier process would.

        Swaps the class's start stub for the real record reader behind a spy, so a
        test reads the data home's record and sees every read. Returns the ``start``
        argument of each read. Any spawn fails the test: these paths read only.
        """
        from kiro_crew import kiro_cli
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp import kas_engine_refusal
        from kiro_crew.agent_sdk.drivers import acp as acp_driver

        reads: list[bool] = []

        def _spy(binary=None, *, start=True):
            reads.append(start)
            return acp_driver.kas_engine_start_verdict(binary, start=start)

        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_start_verdict", _spy)
        binary = tmp_path / "kiro-cli"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        # The binary a session spawn runs, and the pinned install it is.
        monkeypatch.setattr(client_mod, "_resolve_kiro_bin", lambda **_kw: str(binary))
        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(binary), False))

        def _no_spawn(*_args, **_kwargs):
            raise AssertionError("a record-only read must start nothing")

        monkeypatch.setattr(kiro_cli, "kas_relay_start_output", _no_spawn)
        monkeypatch.setattr(kiro_cli, "kas_relay_help", _no_spawn)
        kas_engine_refusal.remember_start_verdict(str(binary), starts)
        return reads

    @staticmethod
    def _logged_a_floor_notice(text: str) -> bool:
        """Whether *text* holds the floor's default or chosen KAS notice, whatever the reason."""
        from kiro_crew.agent_sdk import backends

        return any(
            template.split("{reason}")[0] in text
            for template in (backends.KAS_CLI_FLOOR_DEFAULT_NOTICE, backends.KAS_CLI_FLOOR_NOTICE)
        )

    def test_a_v3_start_kiro_cli_refuses_keeps_the_pick_on_kiro_cli(
        self, tmp_path, registry, monkeypatch
    ):
        """Help that lists v3 is not proof: a refused start leaves the pick on v2."""
        from kiro_crew.acp.kas_engine_refusal import KAS_ENGINE_REFUSED_REASON

        self._stored_is_kiro(monkeypatch)
        self._probe(monkeypatch, (True, None))
        monkeypatch.setattr(
            "kiro_crew.agent_sdk.kas_engine_start_verdict",
            lambda binary=None, *, start=True: (False, KAS_ENGINE_REFUSED_REASON),
        )
        registry.ensure_kiro_cli_kas_verdict()
        assert registry.kiro_cli_offers_kas() is False
        assert _load_agent_config({"acp_backend": KIRO_STORED}, tmp_path).agent.acp_backend == (
            ACP_BACKEND_KIRO
        )

    def test_a_confirmed_v3_probe_moves_the_pick_onto_kas(self, tmp_path, registry, monkeypatch):
        """No floor ran, but the per-process probe confirms v3 and promotes."""
        self._stored_is_kiro(monkeypatch)
        self._probe(monkeypatch, (True, None))
        # Before the probe the pick would load as the v2 Kiro CLI launch.
        assert _load_agent_config({"acp_backend": KIRO_STORED}, tmp_path).agent.acp_backend == (
            ACP_BACKEND_KIRO
        )
        registry.ensure_kiro_cli_kas_verdict()
        assert registry.kiro_cli_offers_kas() is True
        cfg = _load_agent_config({"acp_backend": KIRO_STORED}, tmp_path)
        assert cfg.agent.acp_backend == ACP_BACKEND_KAS

    @pytest.mark.parametrize("stored", ["", ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])
    def test_a_non_kiro_stored_backend_spends_no_probe(self, registry, monkeypatch, stored):
        """A user who never picked Kiro CLI pays no spawn: the probe is not run.

        This is the existing-experience guard -- every ``kirocrew chat`` on a
        default / kas / claude / codex backend must start exactly as it did. A
        default or chosen KAS reads the v3 start record only (``start=False``);
        claude and codex read nothing.
        """
        self._store({"acp_backend": stored})

        def _boom():
            raise AssertionError(f"must not probe for a non-kiro stored backend {stored!r}")

        starts: list[bool] = []

        def _record_only(binary=None, *, start=True):
            starts.append(start)
            return (None, None)

        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", _boom)
        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_start_verdict", _record_only)
        registry.ensure_kiro_cli_kas_verdict()
        assert starts == ([False] if stored in ("", ACP_BACKEND_KAS) else [])
        assert registry.kiro_cli_offers_kas() is False
        assert registry.host_unservable_reason(ACP_BACKEND_KAS) is None
        # Not marked probed either, so a later load for a different config may re-check.
        assert registry._kiro_cli_kas_probed is False

    @pytest.mark.parametrize(
        ("agent", "notice"),
        [
            ({}, "default"),
            ({"acp_backend": ""}, "default"),
            ({"acp_backend": ACP_BACKEND_KAS}, "chosen"),
        ],
        ids=["absent", "empty", "chosen"],
    )
    def test_a_refusal_on_record_loads_kas_as_kiro_cli(
        self, tmp_path, registry, monkeypatch, caplog, agent, notice
    ):
        """A v3 refusal an earlier process kept moves a defaulted or chosen KAS off the relay.

        ``kirocrew chat``, ``run``, ``eval`` and ``consolidate`` never run the gateway
        floor. Without this read every invocation on a refusing kiro-cli spawned
        ``acp --agent-engine v3`` again and showed a refusal card promising a
        fallback it could not make. The record is read only: nothing is started.
        """
        from kiro_crew.acp.kas_engine_refusal import KAS_ENGINE_REFUSED_REASON

        # An earlier process met the refusal at a KAS start and kept it for this binary.
        reads = self._record_start(tmp_path, monkeypatch, False)
        self._store(agent)
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS, "precondition"
        caplog.set_level("WARNING")

        registry.ensure_kiro_cli_kas_verdict()

        assert reads == [False]
        assert registry.host_unservable_reason(ACP_BACKEND_KAS) == KAS_ENGINE_REFUSED_REASON
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KIRO
        template = (
            registry.KAS_CLI_FLOOR_DEFAULT_NOTICE
            if notice == "default"
            else registry.KAS_CLI_FLOOR_NOTICE
        )
        assert template.format(reason=KAS_ENGINE_REFUSED_REASON) in caplog.text

    def test_where_kas_is_the_floor_a_recorded_refusal_marks_nothing(
        self, tmp_path, registry, monkeypatch, caplog
    ):
        """As at the gateway: where KAS is the governance floor the record is not read.

        KAS keeps running there, so a notice saying Kiro Crew moved to kiro-cli
        would be false on every ``kirocrew chat``, ``run``, ``eval`` and
        ``consolidate``. The refusal on record is real, so only the floor guard
        keeps the read and the notice away.
        """
        reads = self._record_start(tmp_path, monkeypatch, False)
        self._store({})
        monkeypatch.setattr(registry, "governance_floor_backend", lambda: ACP_BACKEND_KAS)
        caplog.set_level("WARNING")

        registry.ensure_kiro_cli_kas_verdict()

        assert reads == []
        assert not self._logged_a_floor_notice(caplog.text)
        assert registry.host_unservable_reason(ACP_BACKEND_KAS) is None
        assert ACP_BACKEND_KAS in registry.selectable_backends()

    @pytest.mark.parametrize(
        "agent",
        [{"kas_bundle": True}, {"kas_bundle": True, "acp_backend": ACP_BACKEND_KAS}],
        ids=["default", "chosen"],
    )
    def test_on_the_bundle_route_a_recorded_refusal_marks_nothing(
        self, tmp_path, registry, monkeypatch, caplog, agent
    ):
        """KAS on the bundle route is Kiro Crew's own engine, so kiro-cli's refusal is moot.

        A refusal kiro-cli met earlier says nothing about the bundled engine, so a
        defaulted or chosen KAS there is neither read against the record nor
        marked, and it still loads as KAS.
        """
        reads = self._record_start(tmp_path, monkeypatch, False)
        self._store(agent)
        caplog.set_level("WARNING")

        registry.ensure_kiro_cli_kas_verdict()

        assert reads == []
        assert not self._logged_a_floor_notice(caplog.text)
        assert registry.host_unservable_reason(ACP_BACKEND_KAS) is None
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS

    @pytest.mark.parametrize(
        "agent", [{}, {"acp_backend": ACP_BACKEND_KAS}], ids=["default", "chosen"]
    )
    def test_a_started_record_keeps_kas(self, tmp_path, registry, monkeypatch, caplog, agent):
        """A v3 start on record that kiro-cli served leaves a defaulted or chosen KAS on KAS.

        The floor's pick start check and ``kirocrew doctor`` keep that record on
        every healthy host, so reading it must never move KAS off.
        """
        reads = self._record_start(tmp_path, monkeypatch, True)
        self._store(agent)
        caplog.set_level("WARNING")

        registry.ensure_kiro_cli_kas_verdict()

        assert reads == [False]
        assert not self._logged_a_floor_notice(caplog.text)
        assert registry.host_unservable_reason(ACP_BACKEND_KAS) is None
        assert ACP_BACKEND_KAS in registry.selectable_backends()
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS

    @pytest.mark.parametrize(
        ("agent", "overlay", "runs_kas"),
        [
            ({}, None, True),
            ({"acp_backend": ""}, None, True),
            ({"acp_backend": ACP_BACKEND_KAS}, None, True),
            ({"acp_backend": KIRO_STORED}, None, False),
            ({"acp_backend": ACP_BACKEND_CLAUDE}, None, False),
            ({"acp_backend": None}, None, False),
            ({"acp_backend": ACP_BACKEND_KAS}, {"acp_backend": ACP_BACKEND_CLAUDE}, False),
            ({"acp_backend": ACP_BACKEND_CLAUDE}, {"acp_backend": ""}, True),
        ],
        ids=["absent", "empty", "kas", "kiro", "claude", "null", "overlay-claude", "overlay-none"],
    )
    def test_the_stored_kas_read_agrees_with_the_load(self, registry, agent, overlay, runs_kas):
        """``acp_backend_stored_runs_kas`` answers what a load with no host verdict runs."""
        from kiro_crew.config.loader_document import acp_backend_stored_runs_kas

        self._store(agent, overlay)
        assert acp_backend_stored_runs_kas() is runs_kas
        assert (KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS) is runs_kas

    @pytest.mark.parametrize(
        "agent", [{}, {"acp_backend": ACP_BACKEND_KAS}], ids=["default", "chosen"]
    )
    def test_a_refusal_on_record_leaves_the_automatic_bundle_route_on_kas(
        self, tmp_path, registry, monkeypatch, caplog, agent
    ):
        """kiro-cli's relay refusal says nothing about Kiro Crew's own engine.

        kiro-cli refused its v3 engine earlier and is now signed out, with no
        ``kiro-cli-chat`` beside it, while Kiro Crew's own sign-in holds an identity:
        the next KAS spawn takes the bundle route automatically (``acp.kas_route``
        rule 4), which never runs kiro-cli. The record must not move KAS off.
        """
        from kiro_crew.acp import kas_route
        from kiro_crew.auth import bridge

        self._record_start(tmp_path, monkeypatch, False)
        # The suite pins the automatic switch closed (conftest); no Kiro CLI pick is
        # stored here, so the real rule would open it.
        monkeypatch.setattr(kas_route, "automatic_switch_allowed", lambda pin="": True)
        monkeypatch.setattr(kas_route, "kiro_cli_signed_out", lambda **_kw: True)
        # No ``kiro-cli-chat`` beside the launcher. On Windows the one kiro-cli.exe is
        # the chat binary, whose relay Crew's sign-in stands in for, so pin the answer.
        monkeypatch.setattr(kas_route, "crew_entry_skips_launcher", lambda *_a, **_kw: False)
        monkeypatch.setattr(bridge, "vault_holds_identity", lambda **_kw: True)
        self._store(agent)
        route = kas_route.resolve_kas_route()
        assert route.bundle and route.automatic, "precondition: the automatic bundle route"
        caplog.set_level("WARNING")

        registry.ensure_kiro_cli_kas_verdict()

        assert not self._logged_a_floor_notice(caplog.text)
        assert registry.host_unservable_reason(ACP_BACKEND_KAS) is None
        assert KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS

    @pytest.mark.parametrize("starts", [True, False], ids=["started", "refused"])
    def test_only_a_refusal_on_record_asks_the_route(self, tmp_path, registry, monkeypatch, starts):
        """The route reads the config and the sign-in stores, so a healthy host skips it.

        A refusal on the relay route is still followed: KAS is marked unservable.
        """
        from kiro_crew.acp.kas_route import RELAY
        from kiro_crew.agent_sdk import kas_bundle

        asked: list[str] = []

        def _route():
            asked.append("route")
            return RELAY

        self._record_start(tmp_path, monkeypatch, starts)
        monkeypatch.setattr(kas_bundle, "kas_route", _route)
        self._store({})

        registry.ensure_kiro_cli_kas_verdict()

        assert asked == ([] if starts else ["route"])
        assert (registry.host_unservable_reason(ACP_BACKEND_KAS) is None) is starts

    def test_the_bundle_route_spends_no_probe(self, registry, monkeypatch):
        """A Kiro CLI pick on the bundle route stays on kiro-cli, like the floor.

        KAS on the bundle route is Crew's own bundled engine, which kiro-cli's v3
        support says nothing about, so the gateway floor declines to promote there
        and this helper must too -- otherwise the CLI would launch the bundled
        engine while the gateway runs kiro-cli on the same config.
        """
        self._stored_is_kiro(monkeypatch, is_kiro=True, bundle=True)

        def _boom():
            raise AssertionError("must not probe on the bundle route")

        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", _boom)
        registry.ensure_kiro_cli_kas_verdict()
        assert registry.kiro_cli_offers_kas() is False
        assert registry._kiro_cli_kas_probed is False

    @pytest.mark.parametrize(
        "verdict",
        [(None, None), (False, "this kiro-cli does not offer engine v3")],
        ids=["unknown", "gap"],
    )
    def test_no_positive_evidence_keeps_the_pick_on_kiro_cli(
        self, tmp_path, registry, monkeypatch, verdict
    ):
        """An unknown probe or a v3 gap leaves the pick on the v2 launch."""
        self._stored_is_kiro(monkeypatch)
        self._probe(monkeypatch, verdict)
        registry.ensure_kiro_cli_kas_verdict()
        assert registry.kiro_cli_offers_kas() is False
        assert _load_agent_config({"acp_backend": KIRO_STORED}, tmp_path).agent.acp_backend == (
            ACP_BACKEND_KIRO
        )

    def test_the_probe_runs_at_most_once_per_process(self, registry, monkeypatch):
        """A gap is remembered, so a later config load does not re-spawn the probe."""
        self._stored_is_kiro(monkeypatch)
        calls = {"n": 0}

        def _verdict():
            calls["n"] += 1
            return (None, None)

        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", _verdict)
        registry.ensure_kiro_cli_kas_verdict()
        registry.ensure_kiro_cli_kas_verdict()
        assert calls["n"] == 1

    def test_an_existing_floor_verdict_is_not_re_probed(self, registry, monkeypatch):
        """The gateway already confirmed v3, so the helper spends no spawn."""
        registry.confirm_kiro_cli_offers_kas()
        self._stored_is_kiro(monkeypatch)

        def _boom():
            raise AssertionError("must not probe when evidence is already recorded")

        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", _boom)
        registry.ensure_kiro_cli_kas_verdict()
        assert registry.kiro_cli_offers_kas() is True

    def test_kas_not_selectable_spends_no_spawn(self, registry, monkeypatch):
        """A policy that denies KAS keeps the pick on v2 without probing."""
        self._stored_is_kiro(monkeypatch)
        registry.apply_selectable_denials({ACP_BACKEND_KAS})

        def _boom():
            raise AssertionError("must not probe when KAS is not selectable")

        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", _boom)
        try:
            registry.ensure_kiro_cli_kas_verdict()
        finally:
            registry.apply_selectable_denials(set())
        assert registry.kiro_cli_offers_kas() is False

    def test_concurrent_callers_probe_exactly_once(self, registry, monkeypatch):
        """Two threads reaching the helper at once still run the one spawn once."""
        import threading

        self._stored_is_kiro(monkeypatch)
        start = threading.Barrier(8, timeout=10)
        calls = {"n": 0}
        lock = threading.Lock()

        def _verdict():
            with lock:
                calls["n"] += 1
            return (True, None)

        monkeypatch.setattr("kiro_crew.agent_sdk.kas_engine_verdict", _verdict)

        def _run():
            # A bounded barrier: if a sibling thread failed to start, this raises
            # BrokenBarrierError on the timeout instead of waiting forever.
            start.wait()
            registry.ensure_kiro_cli_kas_verdict()

        threads = [threading.Thread(target=_run) for _ in range(8)]
        started: list[threading.Thread] = []
        try:
            for t in threads:
                t.start()
                started.append(t)
        finally:
            # A rejected Thread.start() must not strand the threads already parked
            # at the barrier: abort it so their bounded wait returns, then join each
            # with a timeout and assert none is left running.
            if len(started) != len(threads):
                start.abort()
            for t in started:
                t.join(timeout=15)
            assert not any(t.is_alive() for t in started), "a probe thread did not finish"
        assert len(started) == len(threads)
        assert calls["n"] == 1
        assert registry.kiro_cli_offers_kas() is True
