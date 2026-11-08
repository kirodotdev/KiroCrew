"""The ``agent`` section: each field hydrates from config.json and survives a save.

The ``max_subagents`` auto sentinel, the sub-agent sizing fields and the spawn
memory floor; the unsandboxed-exec permission (resolved per platform while
undeclared, written back only once declared); the SSH agent forward that is
deliberately not a config field; the Stop-hook nudge cap; the fail-closed switches
(``session_control``, ``member_dispatch``, ``crew_panel``); the fallback models,
the log level and the soft-stop budget; app admission, trusted app sources and the
apps UI stream deadline; and the persisted model defaults.
"""

from __future__ import annotations

import json
import logging
import unittest.mock

import pytest
from config_loader_helpers import _load_from_dict, _load_from_dict_with_logs

from kiro_crew.config.loader import _HAS_JSONSCHEMA, AgentConfig, KiroCrewConfig

# Skip marker for tests that require jsonschema validation
_requires_jsonschema = pytest.mark.skipif(
    not _HAS_JSONSCHEMA,
    reason="jsonschema not available — validation tests require it",
)


def test_max_subagents_defaults_to_auto_sentinel() -> None:
    """Stage 2: max_subagents defaults to the 0 ('auto') sentinel — both on the
    bare dataclass and when hydrated from an empty config — and 0 resolves to a
    computed cap that never regresses below the legacy floor of 3. An explicit
    positive value is preserved verbatim.
    """
    from kiro_crew.subagent import resolve_max_subagents

    # Bare dataclass default.
    assert KiroCrewConfig().agent.max_subagents == 0
    # Hydrated-from-empty default (loader .get path).
    loaded = _load_from_dict({})
    assert loaded.agent.max_subagents == 0
    # 0 sentinel auto-computes; floor guarantees >= 3.
    assert resolve_max_subagents(loaded) >= 3
    # Explicit value is honored, not auto-computed.
    pinned = _load_from_dict({"agent": {"max_subagents": 7}})
    assert pinned.agent.max_subagents == 7
    assert resolve_max_subagents(pinned) == 7


def test_sandbox_allow_unsandboxed_exec_loads_from_config() -> None:
    # Direct construction resolves per platform too, so pin one. Both paths must
    # agree — see test_fresh_gateway_boot_does_not_write_itself_a_lockdown.
    with unittest.mock.patch("sys.platform", "linux"):
        assert KiroCrewConfig().agent.sandbox_allow_unsandboxed_exec is False
    # The undeclared case resolves per platform, so pin one: unpinned, this line
    # would assert the fail-closed default on Linux CI and the permitting one on a
    # Windows dev box. The platform matrix itself lives in
    # test_sandbox_allow_unsandboxed_exec_default_resolves_per_platform.
    with unittest.mock.patch("sys.platform", "linux"):
        assert _load_from_dict({}).agent.sandbox_allow_unsandboxed_exec is False
    enabled = _load_from_dict({"agent": {"sandbox_allow_unsandboxed_exec": True}})
    assert enabled.agent.sandbox_allow_unsandboxed_exec is True


def test_ssh_auth_sock_forward_is_not_an_agent_config_field() -> None:
    """The SSH_AUTH_SOCK forward enable is NOT read from config.json.

    The forward grants use of the operator's ssh-agent keys, so consent lives in the
    agent-nonwritable ``ssh_auth_sock_consent.json``. An enable in config.json would
    let a prompt-injected shell turn its own forwarding on.
    """
    import dataclasses

    from kiro_crew.config.sections import AgentConfig

    field_names = {f.name for f in dataclasses.fields(AgentConfig)}
    assert "sandbox_forward_ssh_auth_sock" not in field_names
    # A config.json that names the old key is ignored, not honoured: the loader
    # builds AgentConfig field-by-field and never reads it.
    cfg = _load_from_dict({"agent": {"sandbox_forward_ssh_auth_sock": True}})
    assert not hasattr(cfg.agent, "sandbox_forward_ssh_auth_sock")


def test_max_stop_hook_nudges_loads_from_config_and_round_trips() -> None:
    """The Stop-hook nudge cap is built field-by-field in load(), so an
    operator's value must hydrate and survive a to_dict() -> load() round-trip.
    Without the loader wiring, load() re-defaults it to 100 and save() then
    overwrites the operator's file value.
    """
    assert KiroCrewConfig().agent.max_stop_hook_nudges == 100
    assert _load_from_dict({}).agent.max_stop_hook_nudges == 100
    pinned = _load_from_dict({"agent": {"max_stop_hook_nudges": 7}})
    assert pinned.agent.max_stop_hook_nudges == 7
    # 0 = uncapped opt-in must survive too, not be re-defaulted to 100.
    uncapped = _load_from_dict({"agent": {"max_stop_hook_nudges": 0}})
    assert uncapped.agent.max_stop_hook_nudges == 0
    # Round-trip: save() (to_dict) -> load() preserves the pinned value.
    assert _load_from_dict(pinned.to_dict()).agent.max_stop_hook_nudges == 7


def test_sandbox_allow_unsandboxed_exec_default_resolves_per_platform(monkeypatch) -> None:
    """An UNDECLARED key resolves per platform, INTO the dataclass value.

    :meth:`KiroCrewConfig.save` strips this key unless the operator declared it;
    writing it would turn "never decided" into a declaration that outranks the
    platform default. A declared value wins in both directions on every platform.
    """
    monkeypatch.setattr("sys.platform", "win32")
    assert _load_from_dict({}).agent.sandbox_allow_unsandboxed_exec is True
    assert _load_from_dict(
        {"agent": {"approval_mode": "auto"}}
    ).agent.sandbox_allow_unsandboxed_exec
    for plat in ("linux", "darwin"):
        monkeypatch.setattr("sys.platform", plat)
        assert _load_from_dict({}).agent.sandbox_allow_unsandboxed_exec is False, plat
        with_section = _load_from_dict({"agent": {"approval_mode": "auto"}})
        assert with_section.agent.sandbox_allow_unsandboxed_exec is False, plat
    # A declaration outranks the platform in both directions. On Windows a stored
    # false is adopted once as the stale pre-0.6.0 default, so mark that one-shot
    # done: a false written after it is the owner's lockdown.
    from kiro_crew.config.superseded_defaults import record_adoptions

    record_adoptions({"agent.sandbox_allow_unsandboxed_exec": False})
    for plat in ("win32", "linux", "darwin"):
        monkeypatch.setattr("sys.platform", plat)
        declared_false = _load_from_dict({"agent": {"sandbox_allow_unsandboxed_exec": False}})
        assert declared_false.agent.sandbox_allow_unsandboxed_exec is False, plat
        declared_true = _load_from_dict({"agent": {"sandbox_allow_unsandboxed_exec": True}})
        assert declared_true.agent.sandbox_allow_unsandboxed_exec is True, plat


def _saved_agent_section(tmp_path, monkeypatch, cfg) -> dict:
    """Run ``cfg.save()`` against a tmp config dir and return the agent section written."""
    main = tmp_path / "config.json"
    local = tmp_path / "config.local.json"
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: main)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: local)
    cfg.save()
    return json.loads(main.read_text(encoding="utf-8")).get("agent", {})


def test_save_omits_the_exec_permission_when_it_was_never_declared(tmp_path, monkeypatch) -> None:
    """Absence is load-bearing, so a whole-document write must not materialize it.

    An absent ``sandbox_allow_unsandboxed_exec`` means "no decision": it selects the
    platform default and keeps the ``kirocrew setup`` prompt. Writing ``false`` or
    ``true`` would fake a decision; both directions are pinned.
    """
    monkeypatch.setattr("sys.platform", "win32")
    written = _saved_agent_section(tmp_path, monkeypatch, KiroCrewConfig())
    assert "sandbox_allow_unsandboxed_exec" not in written
    # The rest of the section still lands - the strip is one key, not the section.
    assert "approval_mode" in written


def test_save_preserves_the_exec_permission_once_declared(tmp_path, monkeypatch) -> None:
    """An operator's recorded decision must survive a whole-document write.

    Both directions: a declared ``false`` is the lockdown that outranks a permitting
    platform, and a declared ``true`` is the opt-in on a platform that refuses by
    default. Dropping either would silently hand the host back to the platform.
    """
    monkeypatch.setattr("sys.platform", "win32")
    for declared in (True, False):
        main = tmp_path / f"config-{declared}.json"
        local = tmp_path / f"config-local-{declared}.json"
        main.write_text(
            json.dumps({"agent": {"sandbox_allow_unsandboxed_exec": declared}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda m=main: m)
        monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda ll=local: ll)
        cfg = KiroCrewConfig()
        cfg.agent.sandbox_allow_unsandboxed_exec = declared
        cfg.save()
        written = json.loads(main.read_text(encoding="utf-8"))["agent"]
        assert written["sandbox_allow_unsandboxed_exec"] is declared, declared


def test_fresh_boot_write_then_load_keeps_the_platform_default(tmp_path, monkeypatch) -> None:
    """The first-run path is construct -> save -> load, and it must not flip anything.

    ``cli_server._gateway`` creates the default config by constructing
    ``KiroCrewConfig()`` directly, ``save()``-ing it, and reloading. With the key
    stripped the fresh file carries no decision, so the reload resolves the platform
    default and the host stays both usable and undeclared.
    """
    monkeypatch.setattr("sys.platform", "win32")
    written = _saved_agent_section(tmp_path, monkeypatch, KiroCrewConfig())
    assert "sandbox_allow_unsandboxed_exec" not in written
    assert _load_from_dict({"agent": written}).agent.sandbox_allow_unsandboxed_exec is True

    monkeypatch.setattr("sys.platform", "linux")
    assert _load_from_dict({"agent": written}).agent.sandbox_allow_unsandboxed_exec is False


def test_agent_spawn_min_memory_gb_parsed_and_round_trips():
    # Regression: agent.spawn_min_memory_gb was declared + consumed (the
    # subagent admission gate) and emitted by to_dict(), but NOT parsed in
    # load(), so an operator value was silently ignored (stuck at the 4.0
    # default) and overwritten on the next save().
    loaded = _load_from_dict({"agent": {"spawn_min_memory_gb": 9.5}})
    assert loaded.agent.spawn_min_memory_gb == 9.5
    # Survives a to_dict() -> load() round-trip.
    reloaded = _load_from_dict(loaded.to_dict())
    assert reloaded.agent.spawn_min_memory_gb == 9.5


class TestSessionControlLoad:
    """agent.session_control load-time coercion.

    A missing key defaults to true; a malformed value (such as a quoted `"false"`)
    coerces to FALSE so a botched opt-out still withdraws cross-session control.
    """

    def test_missing_key_defaults_true(self) -> None:
        assert _load_from_dict({}).agent.session_control is True

    def test_explicit_true_and_false(self) -> None:
        assert _load_from_dict({"agent": {"session_control": True}}).agent.session_control is True
        assert _load_from_dict({"agent": {"session_control": False}}).agent.session_control is False

    def test_quoted_false_coerces_to_false_not_true(self) -> None:
        # The fail-open this locks shut: `"false"` is not a bool, and defaulting
        # it to true keeps the capability on against the operator's intent.
        assert (
            _load_from_dict({"agent": {"session_control": "false"}}).agent.session_control is False
        )

    def test_quoted_true_also_coerces_to_false(self) -> None:
        # A non-bool is an explicit opt-out, the same rule `member_dispatch`
        # follows: a value the operator quoted is not a value to trust.
        assert (
            _load_from_dict({"agent": {"session_control": "true"}}).agent.session_control is False
        )

    def test_any_present_non_bool_coerces_to_false(self) -> None:
        for bad in ("false", "true", "yes", 1, 0, {}, [], None):
            assert (
                _load_from_dict({"agent": {"session_control": bad}}).agent.session_control is False
            ), bad

    def test_round_trips_through_to_dict(self) -> None:
        loaded = _load_from_dict({"agent": {"session_control": "false"}})
        reloaded = _load_from_dict(loaded.to_dict())
        assert reloaded.agent.session_control is False

    def test_coercion_says_so_in_the_log(self) -> None:
        """Silence is half the defect: an operator who meant ON gets told."""
        cfg, logs = _load_from_dict_with_logs({"agent": {"session_control": "true"}})
        assert cfg.agent.session_control is False
        said = [m for m in logs if "agent.session_control" in m and "not a boolean" in m]
        assert said, logs
        line = said[0]
        # Names the type it found and the JSON that chooses, so the operator can act.
        assert "str" in line
        assert "false" in line

    def test_a_real_bool_draws_no_coercion_line(self) -> None:
        # The superseded-default notice for a stored ``false`` is a different
        # line and belongs to a different mechanism; only the coercion line is
        # this loop's to emit.
        for good in (True, False):
            _, logs = _load_from_dict_with_logs({"agent": {"session_control": good}})
            assert not [m for m in logs if "not a boolean" in m], (good, logs)


class TestMemberDispatchLoad:
    """agent.member_dispatch load-time coercion.

    A missing key defaults to true; a malformed value (such as a quoted `"false"`)
    coerces to FALSE so this governance ceiling fails closed.
    """

    def test_missing_key_defaults_true(self) -> None:
        assert _load_from_dict({}).agent.member_dispatch is True

    def test_explicit_true_and_false(self) -> None:
        assert _load_from_dict({"agent": {"member_dispatch": True}}).agent.member_dispatch is True
        assert _load_from_dict({"agent": {"member_dispatch": False}}).agent.member_dispatch is False

    def test_quoted_false_coerces_to_false_not_true(self) -> None:
        # The GPT-flagged fail-open: `"false"` is not a bool, and defaulting it
        # to true would keep the bypass authorized against the operator's intent.
        assert (
            _load_from_dict({"agent": {"member_dispatch": "false"}}).agent.member_dispatch is False
        )

    def test_any_present_non_bool_coerces_to_false(self) -> None:
        # A present but malformed value fails to the SAFE direction (bypass off),
        # including a truthy-looking string that must not be trusted.
        for bad in ("true", "yes", 1, 0, {}, [], None):
            assert (
                _load_from_dict({"agent": {"member_dispatch": bad}}).agent.member_dispatch is False
            ), bad

    def test_round_trips_through_to_dict(self) -> None:
        loaded = _load_from_dict({"agent": {"member_dispatch": False}})
        reloaded = _load_from_dict(loaded.to_dict())
        assert reloaded.agent.member_dispatch is False

    def test_coercion_says_so_in_the_log(self) -> None:
        """The shared loop covers this key too, so the signal does as well."""
        cfg, logs = _load_from_dict_with_logs({"agent": {"member_dispatch": "true"}})
        assert cfg.agent.member_dispatch is False
        assert [m for m in logs if "agent.member_dispatch" in m and "not a boolean" in m], logs


class TestCrewPanelLoad:
    """agent.crew_panel load-time coercion.

    A missing key defaults to true; a malformed value (such as a quoted `"false"`)
    coerces to FALSE so a botched opt-out still withdraws the webview grant.
    """

    def test_missing_key_defaults_true(self) -> None:
        assert _load_from_dict({}).agent.crew_panel is True

    def test_explicit_true_and_false(self) -> None:
        assert _load_from_dict({"agent": {"crew_panel": True}}).agent.crew_panel is True
        assert _load_from_dict({"agent": {"crew_panel": False}}).agent.crew_panel is False

    def test_quoted_false_coerces_to_false_not_true(self) -> None:
        assert _load_from_dict({"agent": {"crew_panel": "false"}}).agent.crew_panel is False

    def test_any_present_non_bool_coerces_to_false(self) -> None:
        for bad in ("false", "true", "yes", 1, 0, {}, [], None):
            assert _load_from_dict({"agent": {"crew_panel": bad}}).agent.crew_panel is False, bad

    def test_round_trips_through_to_dict(self) -> None:
        loaded = _load_from_dict({"agent": {"crew_panel": False}})
        reloaded = _load_from_dict(loaded.to_dict())
        assert reloaded.agent.crew_panel is False

    def test_coercion_says_so_in_the_log(self) -> None:
        """The shared loop covers this key too, so the signal does as well."""
        cfg, logs = _load_from_dict_with_logs({"agent": {"crew_panel": "true"}})
        assert cfg.agent.crew_panel is False
        assert [m for m in logs if "agent.crew_panel" in m and "not a boolean" in m], logs

    def test_the_three_switches_share_one_loop(self) -> None:
        """One loop, so no switch can keep the guard while another loses it."""
        cfg = _load_from_dict(
            {
                "agent": {
                    "session_control": "false",
                    "member_dispatch": "false",
                    "crew_panel": "false",
                }
            }
        )
        assert cfg.agent.session_control is False
        assert cfg.agent.member_dispatch is False
        assert cfg.agent.crew_panel is False


class TestFallbackModelLoad:
    """agent.fallback_model flows through the explicit load() kwargs."""

    def test_load_coerces_registry_alias(self) -> None:
        loaded = _load_from_dict({"agent": {"fallback_model": "opus-4.8-1m"}})
        assert loaded.agent.fallback_model == "claude-opus-4.8"

    def test_load_default_is_auto(self) -> None:
        # DEFAULT PIN: a config without the key loads "auto" — fallback ON via
        # the backend's availability-aware routing.
        assert _load_from_dict({}).agent.fallback_model == "auto"

    def test_load_explicit_empty_disables(self) -> None:
        # ROLLBACK PIN: fallback_model "" is the opt-out — pre-feature behavior.
        assert _load_from_dict({"agent": {"fallback_model": ""}}).agent.fallback_model == ""

    def test_load_malformed_value_never_crashes(self) -> None:
        loaded = _load_from_dict({"agent": {"fallback_model": {"not": "a string"}}})
        assert loaded.agent.fallback_model == "auto"

    def test_round_trips_through_to_dict(self) -> None:
        loaded = _load_from_dict({"agent": {"fallback_model": "claude-opus-5"}})
        assert loaded.to_dict()["agent"]["fallback_model"] == "claude-opus-5"


class TestRefusalFallbackModelLoad:
    """agent.refusal_fallback_model flows through the explicit load() kwargs."""

    def test_load_coerces_registry_alias(self) -> None:
        loaded = _load_from_dict({"agent": {"refusal_fallback_model": "opus-4.8-1m"}})
        assert loaded.agent.refusal_fallback_model == "claude-opus-4.8"

    def test_load_default_is_disabled(self) -> None:
        # DEFAULT PIN: a config without the key loads "" — the refusal retry
        # is OFF and a refusal surfaces exactly as before the feature.
        assert _load_from_dict({}).agent.refusal_fallback_model == ""

    def test_load_auto_defers_to_recommendation(self) -> None:
        loaded = _load_from_dict({"agent": {"refusal_fallback_model": "auto"}})
        assert loaded.agent.refusal_fallback_model == "auto"

    def test_load_malformed_value_never_crashes(self) -> None:
        loaded = _load_from_dict({"agent": {"refusal_fallback_model": {"not": "a string"}}})
        assert loaded.agent.refusal_fallback_model == ""

    def test_round_trips_through_to_dict(self) -> None:
        loaded = _load_from_dict({"agent": {"refusal_fallback_model": "claude-opus-5"}})
        assert loaded.to_dict()["agent"]["refusal_fallback_model"] == "claude-opus-5"


class TestPersistentLogLevel:
    """Tests for the persistent log_level config field."""

    def test_default_log_level_is_warning(self) -> None:
        """When no log_level is specified, default is WARNING."""
        cfg = _load_from_dict({})
        assert cfg.agent.log_level == "WARNING"

    def test_log_level_loaded_from_config(self) -> None:
        """log_level is read from agent section."""
        cfg = _load_from_dict({"agent": {"log_level": "DEBUG"}})
        assert cfg.agent.log_level == "DEBUG"

    def test_log_level_case_insensitive(self) -> None:
        """log_level is uppercased on load."""
        cfg = _load_from_dict({"agent": {"log_level": "info"}})
        assert cfg.agent.log_level == "INFO"

    def test_log_level_round_trips_through_to_dict(self) -> None:
        """log_level survives save/load round-trip."""
        cfg = _load_from_dict({"agent": {"log_level": "ERROR"}})
        d = cfg.to_dict()
        assert d["agent"]["log_level"] == "ERROR"


# Phase 3: Soft-Stop Config Field Tests


class TestSoftStopBudget:
    """Tests for agent.soft_stop_budget_secs config field."""

    def test_soft_stop_budget_default(self) -> None:
        """Default AgentConfig has soft_stop_budget_secs == 10.0."""
        cfg = AgentConfig()
        assert cfg.soft_stop_budget_secs == 10.0

    def test_soft_stop_budget_valid_range(self) -> None:
        """AgentConfig accepts soft_stop_budget_secs within [0.5, 60.0]."""
        cfg = AgentConfig(soft_stop_budget_secs=10.0)
        assert cfg.soft_stop_budget_secs == 10.0

    def test_soft_stop_budget_too_low(self, caplog) -> None:
        """AgentConfig clamps soft_stop_budget_secs below 0.5 to 0.5 with a warning."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            cfg = AgentConfig(soft_stop_budget_secs=0.1)
        assert cfg.soft_stop_budget_secs == 0.5
        assert "out of range" in caplog.text

    def test_soft_stop_budget_too_high(self, caplog) -> None:
        """AgentConfig clamps soft_stop_budget_secs above 60.0 to 60.0 with a warning."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            cfg = AgentConfig(soft_stop_budget_secs=120.0)
        assert cfg.soft_stop_budget_secs == 60.0
        assert "out of range" in caplog.text

    def test_soft_stop_budget_appears_in_schema(self) -> None:
        """Generated config baseline includes soft_stop_budget_secs."""
        from kiro_crew.config.schema import SCHEMA_REGISTRY

        paths = [e.path for e in SCHEMA_REGISTRY]
        assert "agent.soft_stop_budget_secs" in paths

        entry = next(e for e in SCHEMA_REGISTRY if e.path == "agent.soft_stop_budget_secs")
        assert entry.type == "number"
        assert entry.default_value == 10.0


# Dynamic sub-agent sizing config fields (dynamic-subagent-sizing.md §6)


class TestDynamicSubagentSizingFields:
    """The 5 auto-sizing config fields load, default, and round-trip."""

    def test_defaults(self) -> None:
        cfg = _load_from_dict({})
        a = cfg.agent
        assert a.max_subagents == 0  # auto-size sentinel (0 = auto; Stage 2)
        assert a.subagent_mem_buffer_pct == 20
        assert a.subagent_cost_gb == 0.5
        assert a.subagent_cpu_cost_cores == 1.0
        assert a.subagent_auto_max == 32
        assert a.subagent_spawn_stagger_secs == 0.25

    def test_explicit_values_load(self) -> None:
        cfg = _load_from_dict(
            {
                "agent": {
                    "max_subagents": 0,  # auto sentinel
                    "subagent_mem_buffer_pct": 30,
                    "subagent_cost_gb": 0.4,
                    "subagent_cpu_cost_cores": 0.8,
                    "subagent_auto_max": 24,
                    "subagent_spawn_stagger_secs": 1.5,
                }
            }
        )
        a = cfg.agent
        assert a.max_subagents == 0
        assert a.subagent_mem_buffer_pct == 30
        assert a.subagent_cost_gb == 0.4
        assert a.subagent_cpu_cost_cores == 0.8
        assert a.subagent_auto_max == 24
        assert a.subagent_spawn_stagger_secs == 1.5

    @_requires_jsonschema
    def test_deprecated_warning_is_once_per_value(self, monkeypatch, caplog) -> None:
        from kiro_crew.config import validation

        monkeypatch.setattr(validation, "_REPORTED_DEPRECATED_FIELDS", set())

        def notices() -> list[str]:
            return [
                record.getMessage()
                for record in caplog.records
                if record.name == "kiro_crew.config.loader"
                and "deprecated field 'agent.subagent_cpu_cost_cores'" in record.getMessage()
            ]

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            _load_from_dict({"agent": {"subagent_cpu_cost_cores": 0.8}})
            assert len(notices()) == 1

            caplog.clear()
            _load_from_dict({"agent": {"subagent_cpu_cost_cores": 0.8}})
            assert notices() == []

            _load_from_dict({"agent": {"subagent_cpu_cost_cores": 0.9}})
            assert len(notices()) == 1

            caplog.clear()
            _load_from_dict({"agent": {"subagent_cpu_cost_cores": 0.9}})
            assert notices() == []

            _load_from_dict({"agent": {"subagent_cpu_cost_cores": 0.8}})
            assert notices() == []

    @_requires_jsonschema
    @pytest.mark.parametrize("value", [1.0, 1], ids=["float", "int"])
    def test_the_fields_own_default_is_not_announced(self, monkeypatch, caplog, value) -> None:
        """A config Kiro Crew just created carries the default; warning on it is
        the loader scolding its own write."""
        from kiro_crew.config import validation

        monkeypatch.setattr(validation, "_REPORTED_DEPRECATED_FIELDS", set())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            _load_from_dict({"agent": {"subagent_cpu_cost_cores": value}})
        assert not [r for r in caplog.records if "deprecated field" in r.getMessage()]

    @_requires_jsonschema
    def test_a_fresh_default_config_loads_without_a_deprecation_notice(
        self, monkeypatch, caplog
    ) -> None:
        """The document the first ``kirocrew gateway`` writes, read straight back."""
        from kiro_crew.config import validation

        monkeypatch.setattr(validation, "_REPORTED_DEPRECATED_FIELDS", set())
        fresh = KiroCrewConfig().to_dict()
        assert fresh["agent"]["subagent_cpu_cost_cores"] == 1.0
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            _load_from_dict(fresh)
        assert not [r for r in caplog.records if "deprecated field" in r.getMessage()]

    def test_a_bool_is_never_a_numeric_default(self) -> None:
        """``False == 0`` in Python; a deprecated flag set to ``False`` was chosen."""
        from kiro_crew.config.validation import _is_own_default

        assert _is_own_default(1, 1.0)
        assert _is_own_default(False, False)
        assert not _is_own_default(False, 0)
        assert not _is_own_default(True, 1.0)
        assert not _is_own_default(0.8, 1.0)

    def test_to_dict_round_trip(self) -> None:
        cfg = _load_from_dict(
            {
                "agent": {
                    "max_subagents": 0,
                    "subagent_mem_buffer_pct": 25,
                    "subagent_cost_gb": 0.6,
                    "subagent_cpu_cost_cores": 0.9,
                    "subagent_auto_max": 12,
                    "subagent_spawn_stagger_secs": 3.0,
                }
            }
        )
        agent_dict = cfg.to_dict()["agent"]
        for key in (
            "subagent_mem_buffer_pct",
            "subagent_cost_gb",
            "subagent_cpu_cost_cores",
            "subagent_auto_max",
            "subagent_spawn_stagger_secs",
        ):
            assert key in agent_dict, f"{key} missing from to_dict()"

        # Re-load the serialized form and confirm values survive the round-trip.
        reloaded = _load_from_dict(cfg.to_dict())
        a = reloaded.agent
        assert a.max_subagents == 0
        assert a.subagent_mem_buffer_pct == 25
        assert a.subagent_cost_gb == 0.6
        assert a.subagent_cpu_cost_cores == 0.9
        assert a.subagent_auto_max == 12
        assert a.subagent_spawn_stagger_secs == 3.0


class TestAppsAllowThirdParty:
    """agent.apps_allow_third_party execution admission (CSE SEC-012)."""

    def test_defaults_to_false(self) -> None:
        """Fresh dataclass and empty-config load both fail closed."""
        assert AgentConfig().apps_allow_third_party is False
        cfg = _load_from_dict({})
        assert cfg.agent.apps_allow_third_party is False

    def test_true_round_trips_from_config(self) -> None:
        """Only an explicit boolean true enables third-party execution."""
        cfg = _load_from_dict({"agent": {"apps_allow_third_party": True}})
        assert cfg.agent.apps_allow_third_party is True
        assert cfg.to_dict()["agent"]["apps_allow_third_party"] is True

    @pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
    def test_non_boolean_values_fail_closed(self, value) -> None:
        cfg = _load_from_dict({"agent": {"apps_allow_third_party": value}})
        assert cfg.agent.apps_allow_third_party is False


class TestTrustedAppRepositories:
    def test_defaults_to_empty(self) -> None:
        assert AgentConfig().apps_trusted_repositories == {}
        assert _load_from_dict({}).agent.apps_trusted_repositories == {}

    def test_string_bindings_round_trip_and_junk_is_dropped(self) -> None:
        cfg = _load_from_dict(
            {
                "agent": {
                    "apps_trusted_repositories": {
                        "good-app": "https://example.test/owner/repo",
                        "bad-value": 5,
                    }
                }
            }
        )
        assert cfg.agent.apps_trusted_repositories == {
            "good-app": "https://example.test/owner/repo"
        }
        assert cfg.to_dict()["agent"]["apps_trusted_repositories"] == {
            "good-app": "https://example.test/owner/repo"
        }


class TestTrustedLocalApps:
    def test_defaults_to_empty(self) -> None:
        assert AgentConfig().apps_trusted_local == []
        assert _load_from_dict({}).agent.apps_trusted_local == []

    def test_string_markers_round_trip_and_junk_is_dropped(self) -> None:
        cfg = _load_from_dict({"agent": {"apps_trusted_local": ["local-app", 5, "", None]}})
        assert cfg.agent.apps_trusted_local == ["local-app"]
        assert cfg.to_dict()["agent"]["apps_trusted_local"] == ["local-app"]


class TestAppsUiStreamTimeout:
    """``agent.apps_ui_stream_timeout_secs`` is the body-transfer deadline on the
    unauthenticated ``/apps/{name}/ui/`` route, so an unusable value must resolve
    to a usable deadline rather than to none."""

    def test_defaults_to_thirty_seconds(self) -> None:
        assert AgentConfig().apps_ui_stream_timeout_secs == 30
        assert _load_from_dict({}).agent.apps_ui_stream_timeout_secs == 30

    def test_an_in_range_value_round_trips(self) -> None:
        cfg = _load_from_dict({"agent": {"apps_ui_stream_timeout_secs": 45}})
        assert cfg.agent.apps_ui_stream_timeout_secs == 45
        assert cfg.to_dict()["agent"]["apps_ui_stream_timeout_secs"] == 45

    @pytest.mark.parametrize(
        "written,loaded",
        [(4, 5), (0, 5), (-30, 5), (601, 600), (86400, 600)],
    )
    def test_a_value_outside_the_range_is_clamped_to_the_bound(
        self, written: int, loaded: int
    ) -> None:
        """The declared range is [5, 600]: below 5 a client cannot finish a real
        transfer, and above 600 the permit is held long enough that eight such
        clients are the head-of-line wedge the deadline exists to stop."""
        cfg = _load_from_dict({"agent": {"apps_ui_stream_timeout_secs": written}})
        assert cfg.agent.apps_ui_stream_timeout_secs == loaded

    def test_a_numeric_string_from_an_older_writer_still_loads(self) -> None:
        """``_safe_int`` accepts the numeric-string form every other bounded
        agent key accepts, so a hand-edited ``"60"`` is 60 seconds, not 30."""
        cfg = _load_from_dict({"agent": {"apps_ui_stream_timeout_secs": "60"}})
        assert cfg.agent.apps_ui_stream_timeout_secs == 60

    @pytest.mark.parametrize("value", ["soon", "", None, True, False, 4.5, [], {}])
    def test_a_value_that_is_not_a_whole_number_loads_the_default(self, value) -> None:
        cfg = _load_from_dict({"agent": {"apps_ui_stream_timeout_secs": value}})
        assert cfg.agent.apps_ui_stream_timeout_secs == 30


class TestAgentDefaultsRoundTrip:
    """``agent.model`` / ``agent.reasoning_effort`` survive a real load() from disk.

    A field missing from load()'s kwarg list falls back to ``""`` and the next save()
    erases the stored value; only a load from disk catches that.
    """

    def test_reasoning_effort_is_hydrated_from_disk(self) -> None:
        cfg = _load_from_dict({"agent": {"reasoning_effort": "xhigh"}})
        assert cfg.agent.reasoning_effort == "xhigh"

    def test_reasoning_effort_defaults_to_empty_when_absent(self) -> None:
        cfg = _load_from_dict({"agent": {}})
        assert cfg.agent.reasoning_effort == ""

    def test_model_is_hydrated_from_disk(self) -> None:
        cfg = _load_from_dict({"agent": {"model": "claude-opus-4.8"}})
        assert cfg.agent.model == "claude-opus-4.8"

    def test_defaults_survive_a_save_reload_round_trip(self) -> None:
        """to_dict() -> load() must not lose either default."""
        cfg = _load_from_dict({"agent": {"model": "claude-sonnet-4.5", "reasoning_effort": "high"}})
        round_tripped = _load_from_dict(cfg.to_dict())
        assert round_tripped.agent.model == "claude-sonnet-4.5"
        assert round_tripped.agent.reasoning_effort == "high"
