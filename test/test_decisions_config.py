"""``DecisionsConfig``: the knobs that grant nothing, and the switch that is not here.

Consent -- the one value that lets a decision point send conversation state to a
third party -- lives on the KEYSTONE ``decisions_consent.json``, not in this
section (``test_decisions_consent.py`` covers it). The tests here pin the other
half of that design: this section has NO ``enabled`` field, nothing written into
``config.json`` under any spelling (``enabled``, the earlier ``preview`` +
``points.<name>.arm``) produces one, and the bucket and provider parse safely.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.config.sections import (
    DECISION_BUCKET_MAX,
    DECISION_BUCKET_MIN,
    DECISION_HISTORY_BUDGET_DEFAULT,
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DecisionProviderConfig,
    DecisionsConfig,
)


class TestDefaults:
    def test_fully_sampled_by_default(self):
        assert DecisionsConfig().bucket == DECISION_BUCKET_MAX

    def test_no_field_here_is_a_switch(self):
        """The arm/impl/points vocabulary is retired, and ``enabled`` never lands
        here: config.json is agent-writable, so consent is the keystone's. Every
        field is a bound or an address -- nothing here opens a destination the
        session does not already send to.

        ``nudge_wake`` is held to the same rule. Its fields are addresses (which
        provider lane answers the wake judge, and the model id that lane runs on)
        and a bound (how many quiet verdicts may pass before a tick fires anyway).
        None of them arms anything: the Jev lane still needs the keystone switch AND
        the ``nudge_evidence`` scope, checked by the seam, and the judge only runs at
        all for a loop carrying its own brief."""
        from dataclasses import fields

        assert {f.name for f in fields(DecisionsConfig)} == {
            "bucket",
            "history_budget_chars",
            "model_route",
            "nudge_wake",
            "provider",
        }

    def test_the_shipped_budget_covers_a_few_prior_turns(self):
        """The number asked for, not the number sent: the gate holds this against the
        consent keystone's ceiling, which is 0 until the owner reviews one."""
        assert DecisionsConfig().history_budget_chars == DECISION_HISTORY_BUDGET_DEFAULT == 2000

    def test_no_nested_section_here_is_a_switch_either(self):
        """The rule above has to hold one level down, or it guards nothing.

        A subsection is where a switch would hide most easily, so the field names of
        every nested section are pinned too. Adding one here is the same deliberate
        act as adding a top-level field: name it, and say which of the two kinds it
        is.
        """
        from dataclasses import fields, is_dataclass

        # Resolved off an INSTANCE, not off ``f.type``: this module is compiled with
        # ``from __future__ import annotations``, so a field's declared type is the
        # string "NudgeWakeConfig" and ``is_dataclass`` on it is False -- which would
        # make this assertion pass against an empty map and guard nothing.
        config = DecisionsConfig()
        nested = {
            f.name: {sub.name for sub in fields(getattr(config, f.name))}
            for f in fields(DecisionsConfig)
            if is_dataclass(getattr(config, f.name))
        }
        assert nested == {
            "provider": {"endpoint", "api_key", "model", "timeout_ms"},
            "nudge_wake": {"provider", "llm_model", "quiet_streak_floor"},
        }

    def test_the_provider_defaults_are_the_documented_ones(self):
        provider = DecisionsConfig().provider
        assert provider.endpoint == DECISION_PROVIDER_ENDPOINT_DEFAULT
        assert provider.model == "jev-latest"
        assert provider.timeout_ms == 1000

    def test_the_api_key_field_is_schema_sensitive(self):
        """What makes the masked GET cover it; pinned so a rewrite cannot drop it."""
        from dataclasses import fields

        api_key = next(f for f in fields(DecisionProviderConfig) if f.name == "api_key")
        assert api_key.metadata.get("sensitive") is True


class TestNoSwitchInConfigJson:
    @pytest.mark.parametrize("raw", [True, 1, "true", "yes", False, None])
    def test_an_enabled_key_is_read_past_not_honoured(self, raw):
        """Whatever an agent or a hand-edit writes as ``enabled``, the parsed
        section carries no such attribute for the gate to read."""
        parsed = DecisionsConfig.from_raw({"enabled": raw, "bucket": 25})
        assert not hasattr(parsed, "enabled")
        assert parsed.bucket == 25

    @pytest.mark.parametrize("section", [None, "enabled", 7, [], ["enabled"]])
    def test_a_section_that_is_not_a_dict_is_the_default(self, section):
        assert DecisionsConfig.from_raw(section) == DecisionsConfig()


class TestMigrationFromThePreviewSpelling:
    @pytest.mark.parametrize(
        "legacy",
        [
            {"preview": True},
            {"preview": True, "points": {"skills.select": {"arm": "live"}}},
            {"preview": True, "points": {"skills.select": {"arm": "shadow", "impl": "jev"}}},
            {"points": {"skills.select": {"arm": "live", "bucket": 100}}},
            {"preview": True, "points": {"skills.dedupe": {"arm": "live"}}},
        ],
    )
    def test_a_legacy_config_parses_to_the_default_knobs(self, legacy):
        parsed = DecisionsConfig.from_raw(legacy)
        assert not hasattr(parsed, "enabled")
        assert parsed.bucket == DECISION_BUCKET_MAX

    def test_a_legacy_config_keeps_the_provider_it_configured(self):
        """Only the switch is dropped; an endpoint/key the operator set survives."""
        parsed = DecisionsConfig.from_raw(
            {
                "preview": True,
                "points": {"skills.select": {"arm": "live"}},
                "provider": {"model": "jev-nightly", "timeout_ms": 2500},
            }
        )
        assert parsed.provider.model == "jev-nightly"
        assert parsed.provider.timeout_ms == 2500

    def test_the_legacy_keys_do_not_survive_a_round_trip(self):
        """``asdict`` is what the save path writes, so a dropped key must be gone."""
        from dataclasses import asdict

        saved = asdict(DecisionsConfig.from_raw({"preview": True, "points": {"a": {"arm": "off"}}}))
        assert set(saved) == {
            "bucket",
            "history_budget_chars",
            "model_route",
            "nudge_wake",
            "provider",
        }
        assert "arm" not in json.dumps(saved)
        assert "enabled" not in json.dumps(saved)


class TestBucket:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (0, 0),
            (25, 25),
            (100, 100),
            ("25", 25),  # a numeric string from an older writer
            (25.0, 25),  # an integral float from an older writer
            (-1, DECISION_BUCKET_MIN),
            (500, DECISION_BUCKET_MAX),
        ],
    )
    def test_a_bucket_is_coerced_and_clamped(self, raw, expected):
        assert DecisionsConfig.from_raw({"bucket": raw}).bucket == expected

    @pytest.mark.parametrize("raw", ["lots", None, True, 12.5, {"pct": 10}])
    def test_an_unreadable_bucket_fails_closed_to_nobody(self, raw):
        """A hand-edit that does not parse must not open the seam to every session.

        Absent and malformed take different roads on purpose: absent is the
        shipped default (everybody), malformed is 0 (nobody). The switch stays
        `enabled`; a zero bucket is visible in the saved config and in the card.
        """
        assert DecisionsConfig.from_raw({"bucket": raw}).bucket == DECISION_BUCKET_MIN

    def test_an_absent_bucket_is_the_shipped_default(self):
        assert DecisionsConfig.from_raw({}).bucket == DECISION_BUCKET_MAX

    def test_the_saved_value_is_the_value_in_force(self):
        """An operator who wrote 500 reads back 100, not a number behaving as 100."""
        assert DecisionsConfig.from_raw({"bucket": 500}).bucket == 100


class TestTheHistoryBudget:
    """The one char budget: what it bounds, and which way it fails.

    It decides how much conversation leaves the machine, so -- like ``bucket`` --
    an unreadable value fails CLOSED. Its default is already the closed value.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [(0, 0), (500, 500), ("500", 500), (500.0, 500), (-1, 0), (10**9, 10**9)],
    )
    def test_it_is_coerced_and_floored(self, raw, expected):
        assert DecisionsConfig.from_raw({"history_budget_chars": raw}).history_budget_chars == (
            expected
        )

    @pytest.mark.parametrize("raw", ["lots", None, True, 12.5, {"chars": 10}])
    def test_an_unreadable_value_reads_as_the_shipped_default(self, raw):
        """Not a fail-open: the keystone ceiling still caps what the gate sends."""
        parsed = DecisionsConfig.from_raw({"history_budget_chars": raw})
        assert parsed.history_budget_chars == DECISION_HISTORY_BUDGET_DEFAULT == 2000

    def test_an_absent_key_reads_as_the_shipped_default(self):
        assert DecisionsConfig.from_raw({"bucket": 10}).history_budget_chars == 2000

    def test_a_negative_value_cannot_read_as_unbounded(self):
        assert DecisionsConfig.from_raw({"history_budget_chars": -5}).history_budget_chars == 0

    def test_an_owner_who_opts_in_gets_what_they_asked_for(self):
        assert DecisionsConfig.from_raw({"history_budget_chars": 2000}).history_budget_chars == 2000

    def test_the_gate_takes_the_smaller_of_config_and_the_consented_ceiling(self, monkeypatch):
        """Two files, two questions: the config asks, the keystone authorizes.

        `config.json` is agent-writable, so a budget recorded only there could be
        raised by the very agent whose conversation would then be sent. The ceiling
        lives on the sealed keystone, and the gate takes `min` -- so lowering stays
        an ordinary config edit and raising past what was reviewed takes a consent.
        """
        from kiro_crew.decisions import consent as consent_mod
        from kiro_crew.decisions import gate

        class _Cfg:
            decisions = DecisionsConfig.from_raw({"history_budget_chars": 42})

        monkeypatch.setattr(consent_mod, "consented_history_budget", lambda *a, **k: 100)
        assert gate.history_budget_chars(_Cfg()) == 42, "the config asks for less: it wins"

        monkeypatch.setattr(consent_mod, "consented_history_budget", lambda *a, **k: 10)
        assert gate.history_budget_chars(_Cfg()) == 10, "the ceiling is lower: it wins"

    def test_an_agent_raising_the_config_cannot_widen_egress(self, monkeypatch):
        """The whole point of the ceiling, stated as the attack it refuses."""
        from kiro_crew.decisions import consent as consent_mod
        from kiro_crew.decisions import gate

        class _AgentRaised:
            decisions = DecisionsConfig.from_raw({"history_budget_chars": 100_000})

        # The owner consented before the ceiling existed, or to no prior turns.
        monkeypatch.setattr(consent_mod, "consented_history_budget", lambda *a, **k: 0)
        assert gate.history_budget_chars(_AgentRaised()) == 0

    def test_an_unreadable_keystone_sends_no_prior_turns(self, monkeypatch):
        from kiro_crew.decisions import consent as consent_mod
        from kiro_crew.decisions import gate

        class _Cfg:
            decisions = DecisionsConfig.from_raw({"history_budget_chars": 2000})

        def _boom(*_a, **_k):
            raise OSError("keystone unreadable")

        monkeypatch.setattr(consent_mod, "consented_history_budget", _boom)
        assert gate.history_budget_chars(_Cfg()) == 0

    def test_a_zero_config_asks_for_nothing_and_reads_no_keystone(self, monkeypatch):
        """An owner who lowers the budget to 0 costs no keystone read on the turn path."""
        from kiro_crew.decisions import consent as consent_mod
        from kiro_crew.decisions import gate

        reads = []
        monkeypatch.setattr(
            consent_mod, "consented_history_budget", lambda *a, **k: reads.append(1) or 100
        )

        class _Cfg:
            decisions = DecisionsConfig.from_raw({"history_budget_chars": 0})

        assert gate.history_budget_chars(_Cfg()) == 0
        assert reads == [], "nothing asked for, so nothing authorized to check"

    def test_a_config_whose_reads_raise_falls_back_to_the_shipped_default(self, monkeypatch):
        """And the keystone still decides: at a ceiling of 0 the gate sends nothing."""
        from kiro_crew.decisions import consent as consent_mod
        from kiro_crew.decisions import gate

        class _Hostile:
            @property
            def decisions(self):
                raise RuntimeError("no")

        monkeypatch.setattr(consent_mod, "consented_history_budget", lambda *a, **k: 10_000)
        assert gate.history_budget_chars(_Hostile()) == DECISION_HISTORY_BUDGET_DEFAULT == 2000
        monkeypatch.setattr(consent_mod, "consented_history_budget", lambda *a, **k: 0)
        assert gate.history_budget_chars(_Hostile()) == 0

    def test_the_retired_state_budget_is_gone(self):
        """It only fed a split the shipped menu ceiling made unreachable."""
        from dataclasses import fields

        from kiro_crew.decisions import gate

        assert "state_budget_chars" not in {f.name for f in fields(DecisionsConfig)}
        assert not hasattr(gate, "state_budget_chars")


class TestProvider:
    @pytest.mark.parametrize("raw", [None, "endpoint", 5, []])
    def test_a_provider_that_is_not_a_dict_is_the_default(self, raw):
        assert DecisionsConfig.from_raw({"provider": raw}).provider == DecisionProviderConfig()

    def test_an_empty_string_field_falls_back_to_its_default(self):
        parsed = DecisionsConfig.from_raw({"provider": {"endpoint": "", "model": ""}}).provider
        assert parsed.endpoint == DECISION_PROVIDER_ENDPOINT_DEFAULT
        assert parsed.model == "jev-latest"

    @pytest.mark.parametrize("raw", ["soon", None, True, {"ms": 10}])
    def test_an_unreadable_timeout_falls_back_to_the_default(self, raw):
        parsed = DecisionsConfig.from_raw({"provider": {"timeout_ms": raw}}).provider
        assert parsed.timeout_ms == DecisionProviderConfig.timeout_ms

    def test_a_hand_edited_section_never_stops_the_load(self):
        """The parse normalizes; it does not reject, so a typo cannot block boot."""
        DecisionsConfig.from_raw(
            {"enabled": "maybe", "bucket": "most", "provider": {"timeout_ms": "quick"}}
        )


class TestTheLoaderWiresTheSection:
    def test_a_full_config_load_carries_the_section(self, tmp_path, monkeypatch):
        from kiro_crew.config.loader import KiroCrewConfig

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"decisions": {"enabled": True, "bucket": 10}}), "utf-8")
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
        cfg = KiroCrewConfig.load()
        assert cfg.decisions.bucket == 10
        # The switch someone wrote there is inert: the parsed section has no such
        # attribute for the gate to read. (The loader's round trip may keep the
        # raw key in the file, as it does for any unknown key; nothing reads it.)
        assert not hasattr(cfg.decisions, "enabled")

    def test_the_section_name_is_known_so_the_loader_does_not_warn_it_away(self):
        from kiro_crew.config.resolution import _KNOWN_CONFIG_SECTIONS

        assert "decisions" in _KNOWN_CONFIG_SECTIONS
