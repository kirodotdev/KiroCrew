"""The ACP backend-failover policy: the pure candidate walk, config parsing, and
the load-time reader.

These are the leaf-level units behind the spawn-time failover endpoint tests in
``test_spawn_backend.py``. No gateway or event loop.
"""

from __future__ import annotations

import json
import logging

import pytest

from kiro_crew.subagent_parts.backend_failover import (
    FAILOVER_TRIGGERS,
    AcpFailoverConfig,
    failover_policy,
    next_failover_backend,
)


def _next(current: str, chain, verdict: str = "backend_not_installed", triggers=None):
    return next_failover_backend(
        current=current,
        chain=chain,
        triggers=FAILOVER_TRIGGERS if triggers is None else triggers,
        verdict=verdict,
    )


class TestNextFailoverBackend:
    def test_it_hops_to_the_next_member(self):
        assert _next("codex", ("codex", "claude", "kiro")) == "claude"

    def test_the_last_member_has_no_successor(self):
        assert _next("claude", ("codex", "claude")) is None

    def test_a_backend_outside_the_chain_has_no_successor(self):
        assert _next("opencode", ("codex", "claude")) is None

    def test_a_verdict_the_policy_omits_does_not_hop(self):
        assert _next("codex", ("codex", "claude"), triggers=("backend_restart_required",)) is None

    @pytest.mark.parametrize("verdict", ["usage_limit", "auth", "throttle", "http_5xx"])
    def test_a_verdict_this_phase_does_not_consume_never_hops_even_if_listed(self, verdict):
        assert _next("codex", ("codex", "claude"), verdict=verdict, triggers=(verdict,)) is None


class TestFromRaw:
    def test_none_is_disabled(self):
        assert AcpFailoverConfig.from_raw(None) == AcpFailoverConfig.disabled()

    def test_a_full_block_parses(self):
        cfg = AcpFailoverConfig.from_raw(
            {
                "enabled": True,
                "chain": ["codex", "kiro"],
                "triggers": ["backend_restart_required"],
            }
        )
        assert cfg == AcpFailoverConfig(
            enabled=True, chain=("codex", "kiro"), triggers=("backend_restart_required",)
        )

    def test_omitted_triggers_are_every_active_trigger(self):
        cfg = AcpFailoverConfig.from_raw({"enabled": True, "chain": ["codex", "claude"]})
        assert cfg.triggers == FAILOVER_TRIGGERS

    def test_only_the_spawn_time_refusals_are_triggers_in_this_phase(self):
        assert set(FAILOVER_TRIGGERS) == {"backend_not_installed", "backend_restart_required"}

    @pytest.mark.parametrize("bad", [[], "codex", 3, True])
    def test_a_non_object_block_is_rejected(self, bad):
        with pytest.raises(ValueError):
            AcpFailoverConfig.from_raw(bad)

    def test_a_non_bool_enabled_is_rejected(self):
        with pytest.raises(ValueError):
            AcpFailoverConfig.from_raw({"enabled": "true", "chain": ["codex"]})

    @pytest.mark.parametrize("bad_chain", [["codex", ""], ["codex", 3], "codex", {"codex": 1}])
    def test_a_malformed_chain_is_rejected(self, bad_chain):
        with pytest.raises(ValueError):
            AcpFailoverConfig.from_raw({"enabled": True, "chain": bad_chain})

    def test_a_repeated_chain_name_is_rejected(self):
        with pytest.raises(ValueError, match="more than once"):
            AcpFailoverConfig.from_raw({"enabled": True, "chain": ["pi", "claude", "pi"]})

    @pytest.mark.parametrize("trigger", ["usage_limit", "auth"])
    def test_a_phase_two_trigger_is_rejected_as_not_active(self, trigger):
        with pytest.raises(ValueError, match="not active yet"):
            AcpFailoverConfig.from_raw({"enabled": True, "chain": ["codex"], "triggers": [trigger]})

    @pytest.mark.parametrize("trigger", ["throttle", "http_5xx", "connection", "retryable"])
    def test_a_transient_trigger_is_rejected(self, trigger):
        with pytest.raises(ValueError):
            AcpFailoverConfig.from_raw({"enabled": True, "chain": ["codex"], "triggers": [trigger]})

    def test_the_trigger_list_must_be_strings(self):
        with pytest.raises(ValueError):
            AcpFailoverConfig.from_raw({"enabled": True, "chain": ["codex"], "triggers": [3]})


class TestFailoverPolicy:
    @pytest.mark.parametrize("raw", [None, {}])
    def test_absent_is_off(self, raw):
        assert failover_policy(raw) == AcpFailoverConfig.disabled()

    def test_an_invalid_block_is_off_and_logged(self, caplog):
        with caplog.at_level(logging.WARNING):
            policy = failover_policy({"enabled": True, "chain": ["pi", "pi"]})
        assert policy.enabled is False
        assert "agent.acp_failover ignored" in caplog.text


def _patch_selectable(monkeypatch, values):
    from kiro_crew.config import sections_agent_fields

    monkeypatch.setattr(sections_agent_fields, "selectable_backend_values", lambda: sorted(values))


class TestNormalizeAcpFailover:
    """The load path never raises; what it cannot use, it drops or turns off."""

    @pytest.mark.parametrize("raw", [None, ["codex"]])
    def test_absent_or_not_an_object_loads_as_off(self, raw):
        from kiro_crew.config.sections_agent_fields import normalize_acp_failover

        assert normalize_acp_failover(raw) == {}

    def test_a_non_selectable_member_is_dropped_with_a_warning(self, monkeypatch, caplog):
        from kiro_crew.config.sections_agent_fields import normalize_acp_failover

        _patch_selectable(monkeypatch, ["", "claude"])
        with caplog.at_level(logging.WARNING):
            loaded = normalize_acp_failover({"enabled": True, "chain": ["codex", "claude"]})
        assert loaded == {"enabled": True, "chain": ["claude"]}
        assert "codex" in caplog.text

    def test_kiro_is_selectable_by_its_config_spelling(self, monkeypatch):
        from kiro_crew.config.sections_agent_fields import normalize_acp_failover

        _patch_selectable(monkeypatch, ["", "claude"])
        block = {"enabled": True, "chain": ["claude", "kiro"]}
        assert normalize_acp_failover(block) == block

    @pytest.mark.parametrize(
        "block",
        [
            {"enabled": True, "chain": ["pi", "pi"]},
            {"enabled": True, "chain": ["pi"], "triggers": ["usage_limit"]},
            {"enabled": "true", "chain": ["pi"]},
        ],
        ids=["repeated-name", "phase2-trigger", "string-enabled"],
    )
    def test_an_invalid_block_loads_as_off_with_a_warning(self, block, caplog):
        from kiro_crew.config.sections_agent_fields import normalize_acp_failover

        with caplog.at_level(logging.WARNING):
            assert normalize_acp_failover(block) == {}
        assert "agent.acp_failover ignored" in caplog.text


class TestValidateAcpFailover:
    """The write path refuses what the load path would only log."""

    def test_a_valid_block_is_stored_as_written(self, monkeypatch):
        from kiro_crew.config.sections_agent_fields import validate_acp_failover

        _patch_selectable(monkeypatch, ["", "claude", "codex"])
        block = {"enabled": True, "chain": ["codex", "kiro"]}
        assert validate_acp_failover(block) == block

    @pytest.mark.parametrize(
        "block, reason",
        [
            ("codex", "must be an object"),
            ({"enabled": True, "chain": ["codex", "codex"]}, "more than once"),
            ({"enabled": True, "chain": ["codex"], "triggers": ["auth"]}, "not active yet"),
            ({"enabled": True, "chain": ["codex", "goose"]}, "cannot be selected"),
        ],
        ids=["not-an-object", "repeated-name", "phase2-trigger", "not-selectable"],
    )
    def test_an_unusable_block_is_refused(self, monkeypatch, block, reason):
        from kiro_crew.config.sections_agent_fields import validate_acp_failover

        _patch_selectable(monkeypatch, ["", "claude", "codex"])
        with pytest.raises(ValueError, match=reason):
            validate_acp_failover(block)


class TestConfigDocument:
    """The block survives every place config is serialized."""

    def test_the_default_config_serializes(self):
        from kiro_crew.config import KiroCrewConfig

        json.dumps(KiroCrewConfig().to_dict())

    def test_a_configured_block_round_trips_through_to_dict(self):
        import dataclasses

        from kiro_crew.config import KiroCrewConfig

        block = {"enabled": True, "chain": ["codex", "kiro"]}
        cfg = KiroCrewConfig()
        cfg.agent = dataclasses.replace(cfg.agent, acp_failover=block)
        doc = json.loads(json.dumps(cfg.to_dict()))
        assert doc["agent"]["acp_failover"] == block

    def test_the_schema_entry_serializes(self):
        # What GET /api/config/schema emits, one entry per path.
        from kiro_crew.config.schema import SCHEMA_REGISTRY, config_entry_to_dict

        [entry] = [e for e in SCHEMA_REGISTRY if e.path == "agent.acp_failover"]
        doc = json.loads(json.dumps(config_entry_to_dict(entry)))
        assert doc["defaultValue"] == {}

    def test_a_denied_chain_member_still_loads(self, tmp_path):
        # The design review's pin: a ceiling denying a chain member must not stop
        # the config from loading, and the block is kept as written.
        from unittest.mock import patch

        from kiro_crew.agent_sdk import backends
        from kiro_crew.config.loader import KiroCrewConfig

        block = {"enabled": True, "chain": ["codex", "claude"]}
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"agent": {"acp_failover": block}}), encoding="utf-8")
        before = set(backends._selectable)
        try:
            backends.apply_selectable_denials({"codex"})
            assert "codex" not in backends._selectable
            with (
                patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
                patch(
                    "kiro_crew.config.loader.config_local_path",
                    return_value=tmp_path / "config.local.json",
                ),
                patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            ):
                loaded = KiroCrewConfig.load()
        finally:
            backends._selectable.clear()
            backends._selectable.update(before)
        assert loaded.agent.acp_failover == {"enabled": True, "chain": ["claude"]}
        # The file still holds what the operator wrote.
        assert json.loads(cfg_file.read_text(encoding="utf-8"))["agent"]["acp_failover"] == block

    def test_a_save_keeps_a_dropped_member_on_disk(self, tmp_path):
        from unittest.mock import patch

        from kiro_crew.agent_sdk import backends
        from kiro_crew.config.loader import KiroCrewConfig

        block = {"enabled": True, "chain": ["codex", "claude"]}
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"agent": {"acp_failover": block}}), encoding="utf-8")
        before = set(backends._selectable)
        try:
            backends.apply_selectable_denials({"codex"})
            with (
                patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
                patch(
                    "kiro_crew.config.loader.config_local_path",
                    return_value=tmp_path / "config.local.json",
                ),
                patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
            ):
                loaded = KiroCrewConfig.load()
                assert loaded.agent.acp_failover["chain"] == ["claude"]
                loaded.save()
        finally:
            backends._selectable.clear()
            backends._selectable.update(before)
        assert json.loads(cfg_file.read_text(encoding="utf-8"))["agent"]["acp_failover"] == block


class TestSpawnRunReceipt:
    """The ``spawn_run`` tool tells the calling agent where a hopped run went."""

    def _receipt(self, responses: list[dict]) -> str:
        from unittest.mock import MagicMock, patch

        from kiro_crew import mcp_core

        replies = iter(responses)

        def _fake_post(path: str, body: dict) -> dict:
            return next(replies) if path == "/api/spawn" else {}

        with (
            patch.object(mcp_core, "_post", side_effect=_fake_post),
            patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
            patch.object(mcp_core, "sel", MagicMock()),
        ):
            return mcp_core._call_tool("spawn_run", {"tasks": ["t"] * len(responses)})

    def test_a_hop_names_both_backends(self):
        result = self._receipt([{"id": "a0", "backend": "claude", "failover_from": "codex"}])
        assert "a0: backend 'codex' could not start; running on 'claude' instead" in result

    def test_no_hop_adds_no_line(self):
        result = self._receipt([{"id": "a0", "backend": "claude"}])
        assert "could not start" not in result
