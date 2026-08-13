"""Per-task-class model selection (agent.role_models).

Covers the config schema/coercion, the resolution chain, the background/subagent
wiring helpers, and the set-time entitlement validator.
"""

from __future__ import annotations

import pytest

from kiro_crew.config.loader import (
    DEFAULT_MODEL,
    AgentConfig,
    KiroCrewConfig,
    coerce_role_efforts,
    coerce_role_models,
)


# ── coercion ────────────────────────────────────────────────────────────────
class TestCoerceRoleModels:
    def test_keeps_only_known_roles_with_real_pins(self) -> None:
        out = coerce_role_models(
            {"background": "haiku-4.5", "subagent": "sonnet-4.6-1m", "bogus": "x"}
        )
        assert out == {"background": "haiku-4.5", "subagent": "sonnet-4.6-1m"}

    def test_auto_and_empty_collapse_to_inherit(self) -> None:
        # "auto"/""/non-str all mean "inherit" -> dropped from the stored map.
        assert coerce_role_models({"background": "auto", "subagent": ""}) == {}
        assert coerce_role_models({"background": 123, "subagent": None}) == {}

    def test_non_dict_is_empty(self) -> None:
        assert coerce_role_models(None) == {}
        assert coerce_role_models("nope") == {}


# ── resolution chain ──────────────────────────────────────────────────────────
class TestResolveModel:
    def test_unpinned_defaults_to_auto(self) -> None:
        a = AgentConfig()
        assert a.resolve_model("background") == DEFAULT_MODEL == "auto"
        assert a.resolve_model("subagent") == "auto"

    def test_role_pin_wins(self) -> None:
        a = AgentConfig(role_models={"background": "haiku-4.5"})
        assert a.resolve_model("background") == "haiku-4.5"

    def test_role_is_independent_of_chat_default(self) -> None:
        # Role models do NOT inherit agent.model — background must not ride the
        # chat flagship on unattended cycles. Pinned role wins; unpinned -> auto.
        a = AgentConfig(model="opus-4.8-1m", role_models={"background": "haiku-4.5"})
        assert a.resolve_model("background") == "haiku-4.5"
        assert a.resolve_model("subagent") == "auto"

    def test_unknown_role_defaults_to_auto(self) -> None:
        assert AgentConfig(model="sonnet-4.6-1m").resolve_model("nope") == "auto"

    def test_post_init_normalizes_directly_constructed(self) -> None:
        # A hand-constructed instance with "auto"/junk is normalized in __post_init__.
        a = AgentConfig(role_models={"background": "auto", "subagent": "haiku-4.5", "x": "y"})
        assert a.role_models == {"subagent": "haiku-4.5"}


# ── round-trip ───────────────────────────────────────────────────────────────────
def test_config_round_trip_preserves_role_models(tmp_path, monkeypatch) -> None:
    import json

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"agent": {"model": "auto", "role_models": {"background": "haiku-4.5"}}})
    )
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
    cfg = KiroCrewConfig.load()
    assert cfg.agent.role_models == {"background": "haiku-4.5"}
    # asdict-based to_dict surfaces it for the GET/save round-trip.
    assert cfg.to_dict()["agent"]["role_models"] == {"background": "haiku-4.5"}


# ── per-role effort ───────────────────────────────────────────────────────────
class TestCoerceRoleEfforts:
    def test_keeps_valid_levels_only(self) -> None:
        out = coerce_role_efforts({"background": "low", "subagent": "high", "bogus": "max"})
        assert out == {"background": "low", "subagent": "high"}

    def test_drops_empty_and_invalid(self) -> None:
        assert coerce_role_efforts({"background": "", "subagent": "nonsense"}) == {}
        assert coerce_role_efforts({"background": 5}) == {}

    def test_non_dict_is_empty(self) -> None:
        assert coerce_role_efforts(None) == {}


class TestResolveEffort:
    def test_unpinned_defaults_to_model_default(self) -> None:
        # Independent of chat: does NOT inherit agent.reasoning_effort.
        assert AgentConfig(reasoning_effort="high").resolve_effort("background") == ""
        assert AgentConfig().resolve_effort("subagent") == ""

    def test_role_pin_wins(self) -> None:
        a = AgentConfig(reasoning_effort="high", role_efforts={"background": "low"})
        assert a.resolve_effort("background") == "low"
        assert a.resolve_effort("subagent") == ""  # independent of chat default

    def test_post_init_normalizes(self) -> None:
        a = AgentConfig(role_efforts={"background": "low", "subagent": "", "x": "high"})
        assert a.role_efforts == {"background": "low"}


def test_subagent_effort_helper(monkeypatch) -> None:
    from kiro_crew import subagent

    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        classmethod(
            lambda cls: KiroCrewConfig(agent=AgentConfig(role_efforts={"subagent": "low"}))
        ),
    )
    assert subagent._subagent_default_effort() == "low"


# ── background wiring (agent.py) ──────────────────────────────────────────────
class TestBackgroundWiring:
    def test_background_model_defaults_auto(self, monkeypatch) -> None:
        from kiro_crew import agent

        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            classmethod(lambda cls: KiroCrewConfig(agent=AgentConfig())),
        )
        assert agent._background_agent_model() == "auto"
        # CC seam can't use "auto" -> cheap fallback constant.
        assert agent._background_cc_model() == agent._BACKGROUND_CC_MODEL

    def test_background_pin_flows_to_spec_and_cc(self, monkeypatch) -> None:
        from kiro_crew import agent

        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            classmethod(
                lambda cls: KiroCrewConfig(agent=AgentConfig(role_models={"background": "haiku-4.5"}))
            ),
        )
        assert agent._background_agent_model() == "haiku-4.5"
        assert agent._background_cc_model() == "haiku-4.5"

    def test_resolve_failure_is_safe(self, monkeypatch) -> None:
        from kiro_crew import agent

        def _boom(cls):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(_boom)
        )
        assert agent._background_agent_model() == "auto"
        assert agent._background_cc_model() == agent._BACKGROUND_CC_MODEL


# ── subagent wiring ───────────────────────────────────────────────────────────
class TestSubagentWiring:
    def test_unpinned_defers_to_provider_default(self, monkeypatch) -> None:
        from kiro_crew import subagent

        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            classmethod(lambda cls: KiroCrewConfig(agent=AgentConfig())),
        )
        # "auto" collapses to "" so the caller omits the kwarg (unchanged behavior).
        assert subagent._subagent_default_model() == ""

    def test_pin_is_used(self, monkeypatch) -> None:
        from kiro_crew import subagent

        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            classmethod(
                lambda cls: KiroCrewConfig(agent=AgentConfig(role_models={"subagent": "haiku-4.5"}))
            ),
        )
        assert subagent._subagent_default_model() == "haiku-4.5"


# ── set-time validation (core.py) ────────────────────────────────────────────
class TestValidateRoleModel:
    def _req(self):  # minimal stand-in; validator only touches app["state"]
        return object()

    def test_auto_and_empty_always_allowed(self) -> None:
        from kiro_crew.dashboard.handlers import core

        assert core._validate_role_model("", self._req()) is None
        assert core._validate_role_model("auto", self._req()) is None

    def test_rejects_provider_display_only_key(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_active_advertised_ids", lambda req: None)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason",
            lambda m: "display-only key" if m == "fable-5-1m" else None,
        )
        assert core._validate_role_model("fable-5-1m", self._req()) == "display-only key"

    def test_accepts_when_entitlement_unknown(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason", lambda m: None
        )
        monkeypatch.setattr(core, "_active_advertised_ids", lambda req: None)
        # No advertised set -> don't accuse on no evidence.
        assert core._validate_role_model("opus-4.8-1m", self._req()) is None

    def test_rejects_unentitled_when_advertised_known(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason", lambda m: None
        )
        monkeypatch.setattr(
            core, "_active_advertised_ids", lambda req: ["sonnet-4.6-1m"]
        )
        reason = core._validate_role_model("opus-4.8-1m", self._req())
        assert reason is not None and "not available" in reason

    def test_accepts_entitled_model(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason", lambda m: None
        )
        monkeypatch.setattr(
            core, "_active_advertised_ids", lambda req: ["sonnet-4.6-1m"]
        )
        assert core._validate_role_model("sonnet-4.6-1m", self._req()) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
