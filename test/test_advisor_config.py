"""Advisor effective enablement: global default composed with the slot override.

Contract under test (see docs/system-specs/modules/advisor.md):

- The per-session override is one of ``inherit`` / ``on`` / ``off``;
  ``inherit`` defers to the global ``advisor.enabled`` setting.
- ``on``/``off`` win over the global value in both directions.
- Unknown or empty override values behave as ``inherit`` (fail toward the
  configured default, never toward silently enabling).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.advisor.service import (
    OVERRIDE_INHERIT,
    OVERRIDE_OFF,
    OVERRIDE_ON,
    configure_from_config,
    resolve_effective_enabled,
)


@pytest.fixture(autouse=True)
def _hook_self_test_passes(monkeypatch):
    """The installer executes the real hook command as a self-test; these tests
    exercise the installer's other refusals, so the self-test is stubbed green
    (its own contract is covered by test_advisor_read_gate.py)."""
    from kiro_crew.advisor import read_gate

    monkeypatch.setattr(read_gate, "self_test", lambda cwd: "")


class TestEffectiveEnablement:
    @pytest.mark.parametrize(
        ("global_enabled", "override", "expected"),
        [
            (False, OVERRIDE_INHERIT, False),
            (True, OVERRIDE_INHERIT, True),
            (False, OVERRIDE_ON, True),
            (True, OVERRIDE_ON, True),
            (False, OVERRIDE_OFF, False),
            (True, OVERRIDE_OFF, False),
        ],
    )
    def test_truth_table(self, global_enabled, override, expected):
        assert resolve_effective_enabled(global_enabled, override) is expected

    @pytest.mark.parametrize("bogus", ["", "banana", None, 42, "ON "])
    def test_unrecognized_override_behaves_as_inherit(self, bogus):
        assert resolve_effective_enabled(True, bogus) is True
        assert resolve_effective_enabled(False, bogus) is False


class TestAdvisorConfigSection:
    """The advisor.* config section loads, defaults, and round-trips."""

    def _load(self, data):
        import json
        import tempfile
        import unittest.mock
        from pathlib import Path

        from kiro_crew.config.loader import KiroCrewConfig

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                return KiroCrewConfig.load()
        finally:
            tmp.unlink(missing_ok=True)

    def test_absent_section_uses_defaults(self):
        cfg = self._load({})
        assert cfg.advisor.enabled is False
        assert cfg.agent.resolve_model("advisor") == "auto"

    def test_section_values_are_parsed(self):
        cfg = self._load(
            {
                "agent": {"role_models": {"advisor": "reviewer-x"}},
                "advisor": {
                    "enabled": True,
                },
            }
        )
        assert cfg.advisor.enabled is True
        assert cfg.agent.role_models["advisor"] == "reviewer-x"
        assert cfg.agent.resolve_model("advisor") == "reviewer-x"

    def test_bad_values_fall_back_without_crashing(self):
        cfg = self._load(
            {
                "agent": {"role_models": {"advisor": 42}},
                "advisor": {
                    "enabled": "banana",
                    "model": 42,
                },
            }
        )
        assert cfg.advisor.enabled is False
        assert cfg.agent.resolve_model("advisor") == "auto"

    def test_to_dict_emits_the_section(self):
        cfg = self._load({"advisor": {"enabled": True}})
        assert cfg.to_dict()["advisor"]["enabled"] is True


class TestOverrideAwareAttach:
    def test_slot_on_override_attaches_despite_disabled_global(self):
        from kiro_crew.advisor.service import OVERRIDE_ON, AdvisorService

        service = AdvisorService(enabled=False, reviewer_available=True)
        observer = service.attach("dashboard:a", override=OVERRIDE_ON)
        assert observer is not None

    def test_slot_off_override_refuses_despite_enabled_global(self):
        from kiro_crew.advisor.service import OVERRIDE_OFF, AdvisorService

        service = AdvisorService(enabled=True, reviewer_available=True)
        assert service.attach("dashboard:a", override=OVERRIDE_OFF) is None
        assert service.observer_count() == 0

    def test_default_attach_keeps_inherit_semantics(self):
        from kiro_crew.advisor.service import AdvisorService

        assert AdvisorService(enabled=False).attach("dashboard:a") is None
        assert (
            AdvisorService(enabled=True, reviewer_available=True).attach("dashboard:a") is not None
        )


class TestAdvisorConfigPatchSurface:
    """User customization: every advisor.* key is editable from the dashboard
    settings PATCH surface, and a patch re-applies to the live service."""

    def test_all_advisor_keys_are_patch_editable(self):
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        for key in (
            "advisor.enabled",
            "agent.role_models.advisor",
        ):
            assert key in _EDITABLE_CONFIG, f"{key} missing from the PATCH allowlist"


def _fresh():
    import kiro_crew.advisor.service as service_mod
    from kiro_crew.advisor.service import AdvisorService

    service_mod._service = AdvisorService(enabled=False)
    return service_mod._service


class TestPoolBindingFollowsEnablement:
    """Round-6: a disabled advisor constructs NOTHING at startup. The pool
    binds inside configure_from_config only when enabled, and unbinds (with
    a scheduled shutdown) when disabled -- so a settings toggle governs the
    whole lifecycle."""

    def test_disabled_config_binds_no_pool(self):
        service = _fresh()
        cfg = SimpleNamespace(
            advisor=SimpleNamespace(enabled=False),
            agent=SimpleNamespace(role_models={"advisor": ""}),
        )
        configure_from_config(cfg)
        assert getattr(service, "_pool", None) is None

    def test_enabled_config_binds_the_pool(self, monkeypatch):
        service = _fresh()
        built = {}

        def fake_build(model, work_dir=None):
            built["model"] = model
            return object()

        monkeypatch.setattr("kiro_crew.advisor.composition.build_reviewer_runtime", fake_build)
        cfg = SimpleNamespace(
            advisor=SimpleNamespace(enabled=True),
            agent=SimpleNamespace(role_models={"advisor": "rev-x"}),
        )
        configure_from_config(cfg)
        assert service._pool is not None
        assert built["model"] == "rev-x"

    def test_non_kiro_backend_binds_no_pool(self, monkeypatch, caplog):
        """Round-76 (Design): the packaged reviewer spec is a kiro-cli agent
        definition, so the reviewer runtime is kiro-cli only. On a gateway whose
        ``agent.acp_backend`` is another harness the pool must NOT bind (which
        would spawn a harness the operator never selected); say so once."""
        import logging

        service = _fresh()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.build_reviewer_runtime",
            lambda model, work_dir=None: object(),
        )
        cfg = SimpleNamespace(
            advisor=SimpleNamespace(enabled=True),
            agent=SimpleNamespace(role_models={"advisor": ""}, acp_backend="claude"),
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.advisor.service"):
            configure_from_config(cfg)
        assert service._pool is None
        assert any("kiro-cli" in r.getMessage() for r in caplog.records)

    def test_reviewer_is_unavailable_until_configuration_confirms_the_backend(self, monkeypatch):
        """Round-83 (GPT, fenced): the service must default CLOSED. Between bind
        and the post-bind configure a stale per-session `on` must not attach a
        kiro-cli reviewer under a backend the operator never selected; only a
        positive kiro confirmation in configure_from_config opens it."""
        service = _fresh()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.build_reviewer_runtime",
            lambda model, work_dir=None: object(),
        )
        assert service.reviewer_available is False
        assert service.attach("s1", override=OVERRIDE_ON) is None
        assert service._pool is None
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=False),
                agent=SimpleNamespace(role_models={"advisor": ""}, acp_backend=""),
            )
        )
        assert service.reviewer_available is True
        assert service.attach("s1", override=OVERRIDE_ON) is not None

    def test_unmaskable_sandbox_keeps_the_reviewer_unavailable(self, monkeypatch):
        """The startup probe records whether the strict sandbox can mask
        credentials; when it cannot, configure_from_config leaves the reviewer
        unavailable (composed into every enablement decision) even on the kiro
        backend, so the toggle and control show the reason up front."""
        service = _fresh()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.build_reviewer_runtime",
            lambda model, work_dir=None: object(),
        )
        service.sandbox_available = False
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=True),
                agent=SimpleNamespace(role_models={"advisor": ""}, acp_backend=""),
            )
        )
        assert service.reviewer_available is False
        assert service.attach("s1", override=OVERRIDE_ON) is None

    def test_non_kiro_backend_defeats_a_per_session_on_override(self, monkeypatch):
        """Round-78 (GPT): a per-session `on` must not bypass the backend guard
        -- attach() would otherwise build the kiro-cli reviewer and egress the
        session to a harness the operator never selected. A backend switch
        also drops an observer that was already opted in."""
        service = _fresh()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.build_reviewer_runtime",
            lambda model, work_dir=None: object(),
        )
        kiro = SimpleNamespace(
            advisor=SimpleNamespace(enabled=True),
            agent=SimpleNamespace(role_models={"advisor": ""}, acp_backend=""),
        )
        configure_from_config(kiro)
        assert service.attach("s1", override=OVERRIDE_ON) is not None
        claude = SimpleNamespace(
            advisor=SimpleNamespace(enabled=True),
            agent=SimpleNamespace(role_models={"advisor": ""}, acp_backend="claude"),
        )
        gen_before = service._boundary_gen.get("s1", 0)
        configure_from_config(claude)
        assert "s1" not in service._observers  # opted-in observer dropped
        assert service._boundary_gen["s1"] == gen_before + 1  # in-flight review discarded
        assert service.attach("s1", override=OVERRIDE_ON) is None
        assert service.attach("s2", override=OVERRIDE_ON) is None
        assert service._pool is None

    def test_disabling_unbinds_the_pool(self, monkeypatch):
        service = _fresh()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.build_reviewer_runtime",
            lambda model, work_dir=None: object(),
        )
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=True),
                agent=SimpleNamespace(role_models={"advisor": ""}),
            )
        )
        assert service._pool is not None
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=False),
                agent=SimpleNamespace(role_models={"advisor": ""}),
            )
        )
        assert getattr(service, "_pool", None) is None


class TestPoolReplacedOnModelChange:
    """Round-7 gpt: a live reviewer-model change must replace the pool, or
    cards keep getting labeled with the new model while the old runtime
    still serves them."""

    def test_model_change_rebuilds_the_pool(self, monkeypatch):
        service = _fresh()
        built = []

        def fake_build(model, work_dir=None):
            pool = SimpleNamespace(model=model, shut=False)

            async def shutdown():
                pool.shut = True

            pool.shutdown = shutdown
            built.append(pool)
            return pool

        monkeypatch.setattr("kiro_crew.advisor.composition.build_reviewer_runtime", fake_build)
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=True),
                agent=SimpleNamespace(role_models={"advisor": "A"}),
            )
        )
        first = service._pool
        assert first.model == "A"
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=True),
                agent=SimpleNamespace(role_models={"advisor": "B"}),
            )
        )
        assert service._pool is not first
        assert service._pool.model == "B"

    def test_same_model_keeps_the_pool(self, monkeypatch):
        service = _fresh()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.build_reviewer_runtime",
            lambda model, work_dir=None: SimpleNamespace(model=model),
        )
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=True),
                agent=SimpleNamespace(role_models={"advisor": "A"}),
            )
        )
        first = service._pool
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=True),
                agent=SimpleNamespace(role_models={"advisor": "A"}),
            )
        )
        assert service._pool is first


class TestSessionOnUnderGlobalOff:
    """A session explicitly opted in must get the full advisor lifecycle even
    when the global default is off: a bound pool and processed boundaries."""

    def test_attach_binds_the_pool_when_effectively_enabled(self, monkeypatch):
        service = _fresh()
        monkeypatch.setattr(
            "kiro_crew.advisor.composition.build_reviewer_runtime",
            lambda model, work_dir=None: SimpleNamespace(model=model),
        )
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=False),
                agent=SimpleNamespace(role_models={"advisor": "X"}),
            )
        )
        assert service._pool is None  # global off constructs nothing eagerly
        observer = service.attach("dashboard:s1", override="on")
        assert observer is not None
        assert service._pool is not None and service._pool.model == "X"

    def test_boundary_processes_while_globally_disabled(self):
        service = _fresh()
        service._enabled = False
        service.reviewer_available = True  # kiro confirmed; only the global default is off
        observer = service.attach("dashboard:s2", override="on")
        assert observer is not None
        service.notify_boundary("dashboard:s2", "close")
        assert service._observers.get("dashboard:s2") is None


class TestDisableDetachesInheritedObservers:
    """A global disable must stop inherited observation NOW, not at the next
    attach -- checkpoints between disable and next turn would otherwise keep
    reaching the reviewer. Explicit per-session `on` survives by design."""

    def test_inherited_detached_explicit_on_kept(self):
        service = _fresh()
        service._enabled = True
        service.reviewer_available = True
        inherited = service.attach("dashboard:a", override="inherit")
        explicit = service.attach("dashboard:b", override="on")
        assert inherited is not None and explicit is not None
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=False),
                agent=SimpleNamespace(role_models={"advisor": ""}),
            )
        )
        assert service._observers.get("dashboard:a") is None
        assert service._observers.get("dashboard:b") is explicit


@pytest.fixture(autouse=True)
def _masking_host(monkeypatch):
    """Model a host whose strict sandbox can mask credentials (the install
    refuses on any other host); the refusal has its own test below."""
    from kiro_crew.advisor import composition

    monkeypatch.setattr(composition, "credential_mask_applies", lambda mode, **_kw: True)


class TestSpecIsAlwaysMaterializedFromThePackage:
    """The reviewer spec is a managed artifact: on every launch the PACKAGED
    spec is parsed, ceiling-filtered and written over whatever is on disk.
    Nothing on disk is ever read, so a stale grant, a hand edit or a planted
    link cannot widen the read-only boundary (this replaces the earlier
    preserve-user-edits apparatus and its symlink/hardlink/no-follow cases)."""

    def test_stale_managed_spec_is_overwritten_not_read(self, tmp_path):
        from kiro_crew.advisor.composition import (
            ADVISOR_AGENT_NAME,
            MANAGED_DESCRIPTION_PREFIX,
            ensure_advisor_agent_installed,
        )

        target = tmp_path / f"{ADVISOR_AGENT_NAME}.json"
        target.write_text(
            json.dumps(
                {
                    "name": ADVISOR_AGENT_NAME,
                    "description": MANAGED_DESCRIPTION_PREFIX + " (older build)",
                    "tools": ["execute_bash"],
                    "token": "leak",
                }
            )
        )
        ensure_advisor_agent_installed(tmp_path)
        spec = json.loads(target.read_text())
        assert spec["tools"] == ["fs_read", "grep"]
        assert "token" not in spec and "execute_bash" not in json.dumps(spec)

    @pytest.mark.parametrize(
        "body", ['{"name": "kirocrew-advisor", "prompt": "mine"}', "{not json"]
    )
    def test_user_file_squatting_the_reserved_name_is_refused_not_overwritten(self, tmp_path, body):
        """A regular file at the reserved path that is NOT the managed reviewer
        spec is somebody's configuration: the install refuses with a
        name-collision error and leaves the file byte-for-byte intact."""
        from kiro_crew.advisor import composition

        target = tmp_path / f"{composition.ADVISOR_AGENT_NAME}.json"
        target.write_text(body)
        with pytest.raises(composition.AdvisorSpecError, match="collision"):
            composition.ensure_advisor_agent_installed(tmp_path)
        assert target.read_text() == body

    def test_symlink_at_the_spec_path_is_replaced_and_its_target_untouched(self, tmp_path):
        from kiro_crew.advisor.composition import ADVISOR_AGENT_NAME, ensure_advisor_agent_installed

        secret = tmp_path / "secret.json"
        secret.write_text(json.dumps({"token": "do-not-read"}))
        agents = tmp_path / "agents"
        agents.mkdir()
        target = agents / f"{ADVISOR_AGENT_NAME}.json"
        target.symlink_to(secret)
        ensure_advisor_agent_installed(agents)
        assert not target.is_symlink(), "the link must be replaced by a regular file"
        assert json.loads(target.read_text())["tools"] == ["fs_read", "grep"]
        assert json.loads(secret.read_text()) == {"token": "do-not-read"}, "never written through"

    def test_install_fails_closed_without_a_credential_masking_sandbox(self, tmp_path, monkeypatch):
        """kiro-cli auto-approves builtin reads, so the strict sandbox's
        credential mask is the reviewer's read boundary. A host that cannot
        apply it (no namespace backend, sandbox off) gets no reviewer at all
        rather than one whose fs_read can reach ~/.ssh."""
        from kiro_crew.advisor import composition

        asked = []

        def no_mask(mode, **_kw):
            asked.append(mode)
            return False

        monkeypatch.setattr(composition, "credential_mask_applies", no_mask)
        with pytest.raises(composition.AdvisorSpecError, match="sandbox"):
            composition.ensure_advisor_agent_installed(tmp_path)
        assert asked == ["strict"]
        assert not (tmp_path / f"{composition.ADVISOR_AGENT_NAME}.json").exists()

    def test_unreadable_package_fails_closed(self, tmp_path, monkeypatch):
        from kiro_crew.advisor import composition

        monkeypatch.setattr(composition.Path, "read_text", lambda *a, **k: "{not json")
        with pytest.raises(composition.AdvisorSpecError):
            composition.ensure_advisor_agent_installed(tmp_path)


class TestStartupConfigureDiscardsStaleSnapshot:
    """Round-23 B2: a startup config snapshot finishing AFTER a live PATCH
    must be discarded -- configure_from_config bumps a config epoch, and the
    startup task re-checks it after the threaded load."""

    def test_configure_from_config_bumps_epoch(self):
        import kiro_crew.advisor.service as service_mod
        from kiro_crew.advisor.service import AdvisorService, configure_from_config

        service = service_mod._service = AdvisorService(enabled=False)

        class _Advisor:
            enabled = False

        class _Cfg:
            advisor = _Advisor()

        before = service._config_epoch
        configure_from_config(_Cfg())
        assert service._config_epoch == before + 1


class TestReviewerModelRidesRoleModels:
    """Round-72 (First Principles): ``agent.role_models`` is the repository's
    ONLY sanctioned place to pin a model for a class of work, so the reviewer
    pin is ``agent.role_models.advisor`` -- not a second ``advisor.model``
    spelling with a weaker grammar and no entitlement validation."""

    def test_advisor_is_a_role_model_key(self):
        from kiro_crew.config.sections import ROLE_MODEL_KEYS, coerce_role_models

        assert "advisor" in ROLE_MODEL_KEYS
        assert coerce_role_models({"advisor": "rev-x"}) == {"advisor": "rev-x"}
        assert coerce_role_models({"advisor": "auto"}) == {}

    def test_service_resolves_reviewer_model_from_the_role_pin(self):
        from kiro_crew.advisor.service import configure_from_config, get_advisor_service

        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=False),
                agent=SimpleNamespace(role_models={"advisor": "rev-x"}),
            )
        )
        assert get_advisor_service().reviewer_model == "rev-x"
        # "auto" / unpinned resolves to "" -- the runtime default, as before.
        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=False),
                agent=SimpleNamespace(role_models={"advisor": "auto"}),
            )
        )
        assert get_advisor_service().reviewer_model == ""

    def test_advisor_model_is_not_a_config_key(self):
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        assert "advisor.model" not in _EDITABLE_CONFIG
        assert "agent.role_models.advisor" in _EDITABLE_CONFIG
        assert _EDITABLE_CONFIG["agent.role_models.advisor"].get("validate_fn") is not None

    def test_pin_the_runtime_grammar_rejects_falls_back_to_default(self):
        """The role gate admits display-only canonical keys (brackets) that the
        runtime's MODEL_ID_RE rejects at construction; the service must not
        hand such a pin to the pool -- it falls back to the runtime default."""
        from kiro_crew.advisor.service import configure_from_config, get_advisor_service

        configure_from_config(
            SimpleNamespace(
                advisor=SimpleNamespace(enabled=False),
                agent=SimpleNamespace(role_models={"advisor": "opus[1m]"}),
            )
        )
        assert get_advisor_service().reviewer_model == ""
