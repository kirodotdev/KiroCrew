"""The ACP backend-failover policy: the pure resolver, config parsing, and the
one registry-backed config validation.

These are the leaf-level units behind the spawn-time failover endpoint test in
``test_spawn_backend.py``: the pure next-backend walk, the ``from_raw`` shape
and trigger-set rules, and the ``normalize_acp_failover`` selectable-chain
check. No gateway or event loop.
"""

from __future__ import annotations

import pytest

from kiro_crew.subagent_parts.backend_failover import (
    FAILOVER_TRIGGERS,
    AcpFailoverConfig,
    next_failover_backend,
)

_TRIGGERS = frozenset({"backend_not_installed", "backend_restart_required", "usage_limit", "auth"})


class TestNextFailoverBackend:
    def test_a_configured_trigger_hops_to_the_next_member(self):
        assert (
            next_failover_backend(
                current="codex",
                chain=("codex", "claude"),
                triggers=_TRIGGERS,
                verdict="usage_limit",
            )
            == "claude"
        )

    def test_it_hops_to_the_immediate_successor_only(self):
        assert (
            next_failover_backend(
                current="codex",
                chain=("codex", "claude", "kiro"),
                triggers=_TRIGGERS,
                verdict="backend_not_installed",
            )
            == "claude"
        )

    def test_the_last_member_has_no_successor(self):
        assert (
            next_failover_backend(
                current="claude",
                chain=("codex", "claude"),
                triggers=_TRIGGERS,
                verdict="usage_limit",
            )
            is None
        )

    def test_a_verdict_outside_the_configured_triggers_does_not_hop(self):
        assert (
            next_failover_backend(
                current="codex",
                chain=("codex", "claude"),
                triggers=frozenset({"auth"}),
                verdict="usage_limit",
            )
            is None
        )

    @pytest.mark.parametrize("verdict", ["throttle", "http_5xx", "connection", "unknown"])
    def test_a_non_terminal_verdict_never_hops_even_if_listed(self, verdict):
        # Defence in depth: even if a caller somehow passes a transient verdict
        # as a trigger, the resolver re-checks against FAILOVER_TRIGGERS.
        assert (
            next_failover_backend(
                current="codex",
                chain=("codex", "claude"),
                triggers=frozenset({verdict}),
                verdict=verdict,
            )
            is None
        )

    def test_a_current_backend_outside_the_chain_has_no_successor(self):
        assert (
            next_failover_backend(
                current="opencode",
                chain=("codex", "claude"),
                triggers=_TRIGGERS,
                verdict="usage_limit",
            )
            is None
        )

    def test_it_hops_after_the_first_occurrence(self):
        assert (
            next_failover_backend(
                current="codex",
                chain=("codex", "claude", "codex", "kiro"),
                triggers=_TRIGGERS,
                verdict="auth",
            )
            == "claude"
        )


class TestFromRaw:
    def test_none_is_disabled(self):
        assert AcpFailoverConfig.from_raw(None) == AcpFailoverConfig.disabled()

    def test_an_already_parsed_instance_passes_through(self):
        cfg = AcpFailoverConfig(enabled=True, chain=("codex",), triggers=frozenset({"auth"}))
        assert AcpFailoverConfig.from_raw(cfg) is cfg

    def test_a_full_block_parses(self):
        cfg = AcpFailoverConfig.from_raw(
            {"enabled": True, "chain": ["codex", "claude"], "triggers": ["usage_limit", "auth"]}
        )
        assert cfg.enabled is True
        assert cfg.chain == ("codex", "claude")
        assert cfg.triggers == frozenset({"usage_limit", "auth"})

    def test_omitted_triggers_default_to_the_full_terminal_set(self):
        cfg = AcpFailoverConfig.from_raw({"enabled": True, "chain": ["codex", "claude"]})
        assert cfg.triggers == FAILOVER_TRIGGERS

    @pytest.mark.parametrize("bad", [[], "codex", 3, True])
    def test_a_non_object_block_is_rejected(self, bad):
        if bad is None:
            pytest.skip("None is the disabled case")
        with pytest.raises(ValueError):
            AcpFailoverConfig.from_raw(bad)

    def test_a_non_bool_enabled_is_rejected(self):
        with pytest.raises(ValueError):
            AcpFailoverConfig.from_raw({"enabled": "yes", "chain": ["codex"]})

    @pytest.mark.parametrize("bad_chain", [["codex", ""], ["codex", 3], "codex", {"codex": 1}])
    def test_a_malformed_chain_is_rejected(self, bad_chain):
        with pytest.raises(ValueError):
            AcpFailoverConfig.from_raw({"enabled": True, "chain": bad_chain})

    @pytest.mark.parametrize("bad_trigger", ["throttle", "http_5xx", "connection", "retryable"])
    def test_a_transient_trigger_is_rejected(self, bad_trigger):
        # The core invariant: failover must never fire on a transient fault, so a
        # config naming one fails to load rather than being silently ignored.
        with pytest.raises(ValueError):
            AcpFailoverConfig.from_raw(
                {"enabled": True, "chain": ["codex"], "triggers": [bad_trigger]}
            )

    def test_the_trigger_list_must_be_strings(self):
        with pytest.raises(ValueError):
            AcpFailoverConfig.from_raw({"enabled": True, "chain": ["codex"], "triggers": [3]})


class TestNormalizeAcpFailover:
    """The one validation that needs the backend registry: every chain id must
    be currently selectable."""

    def _patch_selectable(self, monkeypatch, values):
        from kiro_crew.config import sections_agent_fields

        monkeypatch.setattr(
            sections_agent_fields, "selectable_backend_values", lambda: sorted(values)
        )

    def test_absent_is_disabled(self, monkeypatch):
        from kiro_crew.config.sections_agent_fields import normalize_acp_failover

        self._patch_selectable(monkeypatch, ["", "codex", "claude"])
        assert normalize_acp_failover(None) == AcpFailoverConfig.disabled()

    def test_a_selectable_chain_is_accepted(self, monkeypatch):
        from kiro_crew.config.sections_agent_fields import normalize_acp_failover

        self._patch_selectable(monkeypatch, ["", "codex", "claude"])
        cfg = normalize_acp_failover({"enabled": True, "chain": ["codex", "claude"]})
        assert cfg.chain == ("codex", "claude")

    def test_a_chain_naming_a_non_selectable_backend_is_rejected(self, monkeypatch):
        from kiro_crew.config.sections_agent_fields import normalize_acp_failover

        self._patch_selectable(monkeypatch, ["", "codex"])
        with pytest.raises(ValueError):
            normalize_acp_failover({"enabled": True, "chain": ["codex", "claude"]})
