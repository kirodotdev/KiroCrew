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
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DecisionProviderConfig,
    DecisionsConfig,
)


class TestDefaults:
    def test_fully_sampled_by_default(self):
        assert DecisionsConfig().bucket == DECISION_BUCKET_MAX

    def test_the_section_is_two_fields_and_neither_is_a_switch(self):
        """The arm/impl/points vocabulary is retired, and ``enabled`` never lands
        here: config.json is agent-writable, so consent is the keystone's."""
        from dataclasses import fields

        assert {f.name for f in fields(DecisionsConfig)} == {"bucket", "provider"}

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
        assert set(saved) == {"bucket", "provider"}
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
