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
        ]
        assert sum(held) == 1

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
    def test_acp_runtime_backend_answers_positive_set_membership(self, backend):
        """The property IS membership in ACP_BACKENDS_ACP_RUNTIME, over every known
        backend rather than a hand-picked three.

        The narrower form this replaces asserted the property equalled ``not
        is_claude_backend``, which holds only while claude is the single harness off
        the shared runtime. Several harnesses are off it, and one that is NOT claude
        is on it, so that equality is an accident of one snapshot of the table --
        and an accident is the thing a membership edit can satisfy without meaning
        to. Membership is the durable statement (harness-parity H5/H6).

        The expected members are spelled in ``ON_THE_ACP_RUNTIME`` above rather than
        read back off the set the property consults, so opting a harness in has to
        be written twice to land green; the equality assertion below is what ties
        the two spellings together.
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

        These are non-claude harnesses that are also off the shared runtime, so
        ``not is_claude_backend`` answers True where the property answers False.
        They are the evidence that the positive form carries information the
        negation does not -- without at least one of them the two spellings would be
        interchangeable and the block above would pin nothing.
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

        One codex-acp process serves N sessions and keeps them apart, which is the
        whole question ACP_BACKENDS_ACP_RUNTIME asks -- so the shared-runtime start
        path is the right transport for it, and a provider that answered False here
        would open one adapter per session.

        Transport membership does not by itself grant a teardown that disposes a
        session; that is ACP_BACKENDS_SESSION_EVICTION's question, and codex answers
        it on its own evidence: its teardown verb is ``session/close``, after which
        the sessionId stops answering (measured live). Both are pinned here to keep
        the two questions from collapsing into one: a call site that read the
        transport answer where the eviction answer belongs would admit the next
        runtime-capable harness whose teardown frees nothing.
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

    The provider constructs ``AcpRuntime`` at more than one point — the initial
    start and the respawn after the runtime dies mid ``session/load`` — and a
    site that omits the kwarg silently spawns kiro-cli for a KAS session, which
    then persists under the kiro label. An AST ratchet rather than a behavioral
    test because the respawn needs a process-death race to reach.
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
