"""Per-agent default model: storage, precedence, and normalization.

The default model is stored per KiroCrew agent (``agents.<name>.model`` in
config.json), NOT on the kiro agent spec files under ``~/.kiro/agents`` — several
KiroCrew agents can bind the same spec, and KiroCrew regenerates most of those
files on every install. The global ``agent.model`` remains a fallback for agents
that pin nothing.

Precedence under test, highest first:
  1. the KiroCrew agent's own ``model``
  2. the bound kiro agent's pinned ``model`` (not for the built-in "kirocrew")
  3. the global ``agent.model``
  4. the installed agent file / bundled defaults
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    normalize_agent_model,
    resolve_agent_bindings,
    resolve_effective_model,
)
from kiro_crew.session import _session_model


def _load_from_dict(data: object) -> KiroCrewConfig:
    """Write *data* to a temp config file and load via KiroCrewConfig.load()."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=tmp,
        ):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


class TestNormalizeAgentModel:
    """"auto" and "" are the same "inherit" state and must store identically."""

    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("", ""),
            (None, ""),
            ("auto", ""),
            ("  auto  ", ""),
            ("claude-opus-5", "claude-opus-5"),
            ("  claude-opus-5  ", "claude-opus-5"),
        ],
    )
    def test_normalization(self, raw: str | None, want: str) -> None:
        assert normalize_agent_model(raw) == want

    def test_auto_never_survives_as_a_pin(self) -> None:
        """A pin of "auto" would shadow the tier below it instead of deferring."""
        assert normalize_agent_model("auto") == ""

    @pytest.mark.parametrize("bad", [123, 1.5, True, [], {}, object()])
    def test_non_string_is_treated_as_no_pin(self, bad: object) -> None:
        """config.json is hand-editable, so a non-string must not reach .strip().

        Regression: ``agents.<name>.model: 123`` loaded as an int, and the first
        resolver to normalize it raised AttributeError — surfacing as HTTP 500
        from /api/agents/resolved-model instead of simply being ignored.
        """
        assert normalize_agent_model(bad) == ""


class TestNonStringModelInConfig:
    """A malformed model in config.json is dropped at load, not carried."""

    @pytest.mark.parametrize("bad", [123, 1.5, [], {}, None])
    def test_non_string_model_loads_as_inherit(self, bad: object) -> None:
        cfg = _load_from_dict(
            {
                "agents": {"oncall": {"kiro_agent": "kirocrew", "model": bad}},
                "default_agent": "oncall",
            }
        )
        assert cfg.agents["oncall"].model == ""

    def test_resolution_survives_a_non_string_model(self) -> None:
        """The end-to-end path GPT flagged: load -> resolve, with no exception."""
        cfg = _load_from_dict(
            {
                "agents": {"oncall": {"kiro_agent": "kirocrew", "model": 123}},
                "default_agent": "oncall",
            }
        )
        assert resolve_agent_bindings(cfg, "oncall").model == ""
        # Must not raise; falls through to the tiers below.
        assert isinstance(resolve_effective_model(cfg, "oncall"), str)


class TestApiDoesNotStringifyModels:
    """The CRUD handlers must not coerce a non-string body value into an id.

    Regression: the handlers wrapped the value in ``str()`` before normalizing.
    Once ``normalize_agent_model`` became total, that wrapper actively defeated
    it — ``{"model": 123}`` became the literal ``"123"``, which IS a string, so
    it survived normalization and was persisted as a model id the backend then
    rejects (silently running its own default). Passing the raw value through
    lets the normalizer map it to "" (inherit).
    """

    @staticmethod
    def _handler_source() -> str:
        import inspect

        from kiro_crew.dashboard.handlers import agents as agents_mod

        return inspect.getsource(agents_mod)

    def test_no_str_coercion_around_the_normalizer(self) -> None:
        """Structural guard: str() must not wrap a normalize_agent_model arg."""
        src = self._handler_source()
        assert "normalize_agent_model(str(" not in src

    @pytest.mark.parametrize("bad", [123, 1.5, [], {}, None, True])
    def test_non_string_body_value_becomes_inherit(self, bad: object) -> None:
        assert normalize_agent_model(bad) == ""

    def test_a_stringified_number_would_have_been_persisted(self) -> None:
        """Pins the exact failure mode: str() made the bad value survive."""
        assert normalize_agent_model(str(123 or "")) == "123"
        assert normalize_agent_model(123) == ""


class TestPerAgentModelStorage:
    """The field round-trips through config.json and reaches ResolvedBindings."""

    def test_model_parsed_from_config(self) -> None:
        cfg = _load_from_dict(
            {
                "agents": {"oncall": {"kiro_agent": "kirocrew", "model": "claude-opus-5"}},
                "default_agent": "oncall",
            }
        )
        assert cfg.agents["oncall"].model == "claude-opus-5"

    def test_model_defaults_to_inherit_when_absent(self) -> None:
        """An agent written before this field exists must inherit, not break."""
        cfg = _load_from_dict(
            {"agents": {"oncall": {"kiro_agent": "kirocrew"}}, "default_agent": "oncall"}
        )
        assert cfg.agents["oncall"].model == ""

    def test_null_model_coerces_to_empty_string(self) -> None:
        cfg = _load_from_dict(
            {
                "agents": {"oncall": {"kiro_agent": "kirocrew", "model": None}},
                "default_agent": "oncall",
            }
        )
        assert cfg.agents["oncall"].model == ""

    def test_model_survives_save_round_trip(self) -> None:
        cfg = _load_from_dict(
            {
                "agents": {"oncall": {"kiro_agent": "kirocrew", "model": "claude-opus-5"}},
                "default_agent": "oncall",
            }
        )
        assert cfg.to_dict()["agents"]["oncall"]["model"] == "claude-opus-5"

    def test_bindings_expose_the_agent_model(self) -> None:
        cfg = _load_from_dict(
            {
                "agents": {"oncall": {"kiro_agent": "kirocrew", "model": "claude-opus-5"}},
                "default_agent": "oncall",
            }
        )
        assert resolve_agent_bindings(cfg, "oncall").model == "claude-opus-5"

    def test_bindings_normalize_an_auto_pin(self) -> None:
        """"auto" stored by an older write must still read as inherit."""
        cfg = _load_from_dict(
            {
                "agents": {"oncall": {"kiro_agent": "kirocrew", "model": "auto"}},
                "default_agent": "oncall",
            }
        )
        assert resolve_agent_bindings(cfg, "oncall").model == ""

    def test_two_agents_on_one_template_hold_distinct_models(self) -> None:
        """The case a spec-file pin cannot express, and the reason for this field."""
        cfg = _load_from_dict(
            {
                "agents": {
                    "a": {"kiro_agent": "kirocrew", "model": "claude-opus-5"},
                    "b": {"kiro_agent": "kirocrew", "model": "claude-sonnet-4.6"},
                },
                "default_agent": "a",
            }
        )
        assert resolve_agent_bindings(cfg, "a").model == "claude-opus-5"
        assert resolve_agent_bindings(cfg, "b").model == "claude-sonnet-4.6"


@pytest.fixture
def specs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A kiro agent spec dir with one template that pins its own model."""
    d = tmp_path / "agents"
    d.mkdir()
    (d / "pinned.json").write_text(
        json.dumps({"name": "pinned", "model": "claude-sonnet-4.6"}),
        encoding="utf-8",
    )
    (d / "unpinned.json").write_text(json.dumps({"name": "unpinned"}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.kiro_agents_dir", lambda: d)
    return d


def _cfg(agents: dict, global_model: str) -> KiroCrewConfig:
    cfg = _load_from_dict({"agents": {"seed": {}}, "default_agent": "seed"})
    cfg.agent.model = global_model
    cfg.agents = {n: KiroCrewAgentConfig(**a) for n, a in agents.items()}
    cfg.default_agent = next(iter(agents))
    return cfg


class TestEffectiveModelPrecedence:
    """One resolver owns the chain, so display and execution cannot diverge."""

    def test_agent_model_outranks_the_global(self, specs_dir: Path) -> None:
        cfg = _cfg({"crew": {"kiro_agent": "kirocrew", "model": "claude-opus-5"}}, "claude-haiku-4.5")
        assert resolve_effective_model(cfg, "crew") == "claude-opus-5"

    def test_agent_model_outranks_a_template_pin(self, specs_dir: Path) -> None:
        cfg = _cfg({"crew": {"kiro_agent": "pinned", "model": "claude-opus-5"}}, "")
        assert resolve_effective_model(cfg, "crew") == "claude-opus-5"

    def test_template_pin_applies_when_the_agent_defers(self, specs_dir: Path) -> None:
        cfg = _cfg({"crew": {"kiro_agent": "pinned", "model": ""}}, "claude-haiku-4.5")
        assert resolve_effective_model(cfg, "crew") == "claude-sonnet-4.6"

    def test_global_is_the_fallback_when_nothing_pins(self, specs_dir: Path) -> None:
        cfg = _cfg({"crew": {"kiro_agent": "kirocrew", "model": ""}}, "claude-haiku-4.5")
        assert resolve_effective_model(cfg, "crew") == "claude-haiku-4.5"

    def test_global_reaches_a_named_template_that_pins_nothing(self, specs_dir: Path) -> None:
        """Previously the global was skipped entirely for non-kirocrew templates,
        so it was not really a global default. It is now a real fallback."""
        cfg = _cfg({"crew": {"kiro_agent": "unpinned", "model": ""}}, "claude-haiku-4.5")
        assert resolve_effective_model(cfg, "crew") == "claude-haiku-4.5"

    def test_auto_global_is_never_returned_verbatim(self, specs_dir: Path) -> None:
        """"auto" is the inherit spelling; returning it would pin the chip to a
        value no tier actually chose."""
        cfg = _cfg({"crew": {"kiro_agent": "kirocrew", "model": ""}}, "auto")
        assert resolve_effective_model(cfg, "crew") != "auto"

    def test_unknown_agent_falls_back_to_the_default_agent(self, specs_dir: Path) -> None:
        cfg = _cfg({"crew": {"kiro_agent": "kirocrew", "model": "claude-opus-5"}}, "")
        assert resolve_effective_model(cfg, "no-such-agent") == "claude-opus-5"

    def test_blank_agent_resolves_the_default_agent(self, specs_dir: Path) -> None:
        cfg = _cfg({"crew": {"kiro_agent": "kirocrew", "model": "claude-opus-5"}}, "")
        assert resolve_effective_model(cfg, None) == "claude-opus-5"


class TestSessionModelCoversEverySurface:
    """The crew tier must apply to Slack / cron / spawn, not just dashboard chat.

    Those surfaces reach ``SessionManager.get_or_create`` directly, which used to
    resolve only the kiro pin and the global — so a crew pinned in the Crews
    table still ran the template/global model there, and the same crew ran
    different models per surface.

    ``_session_model`` is the shared resolver ``get_or_create`` now uses. Callers
    are inconsistent about what they pass as ``agent`` (the dashboard passes a
    resolved kiro template name; Slack threads and cron jobs pass a KiroCrew
    agent name), so both namespaces must work.
    """

    def test_crew_name_resolves_its_own_model(self, specs_dir: Path) -> None:
        cfg = _cfg({"oncall": {"kiro_agent": "kirocrew", "model": "claude-opus-5"}}, "claude-haiku-4.5")
        assert _session_model(cfg, "oncall") == "claude-opus-5"

    def test_crew_pin_outranks_the_bound_template_pin(self, specs_dir: Path) -> None:
        cfg = _cfg({"oncall": {"kiro_agent": "pinned", "model": "claude-opus-5"}}, "")
        assert _session_model(cfg, "oncall") == "claude-opus-5"

    def test_crew_deferring_falls_through_to_its_template(self, specs_dir: Path) -> None:
        """An unpinned crew must not shadow the template it binds: returning None
        lets the provider factory resolve that template's own model."""
        cfg = _cfg({"oncall": {"kiro_agent": "pinned", "model": ""}}, "claude-haiku-4.5")
        assert _session_model(cfg, "oncall") is None

    def test_crew_deferring_with_unpinned_template_reaches_the_global(
        self, specs_dir: Path
    ) -> None:
        cfg = _cfg({"oncall": {"kiro_agent": "unpinned", "model": ""}}, "claude-haiku-4.5")
        assert _session_model(cfg, "oncall") == "claude-haiku-4.5"

    def test_template_name_still_behaves_as_before(self, specs_dir: Path) -> None:
        """Callers passing a kiro template name (the dashboard) are unaffected:
        a template pin returns None so the factory resolves it natively."""
        cfg = _cfg({"oncall": {"kiro_agent": "kirocrew", "model": "claude-opus-5"}}, "")
        assert _session_model(cfg, "pinned") is None

    def test_unknown_name_falls_back_to_the_global(self, specs_dir: Path) -> None:
        cfg = _cfg({"oncall": {"kiro_agent": "kirocrew", "model": "claude-opus-5"}}, "claude-haiku-4.5")
        assert _session_model(cfg, "no-such-thing") == "claude-haiku-4.5"

    def test_auto_global_yields_none_so_kiro_resolves(self, specs_dir: Path) -> None:
        cfg = _cfg({"oncall": {"kiro_agent": "unpinned", "model": ""}}, "auto")
        assert _session_model(cfg, "oncall") is None

    def test_non_string_crew_model_does_not_crash_the_session_path(
        self, specs_dir: Path
    ) -> None:
        cfg = _load_from_dict(
            {
                "agents": {"oncall": {"kiro_agent": "unpinned", "model": 123}},
                "default_agent": "oncall",
            }
        )
        cfg.agent.model = "claude-haiku-4.5"
        assert _session_model(cfg, "oncall") == "claude-haiku-4.5"
