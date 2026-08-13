"""Tests for the session-summary config section.

The feature spends tokens on a turn the user did not ask to pay for, so the
defaults matter as much as the parsing: a fresh install must be inert, and a
malformed section must degrade to defaults rather than raising during load.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from kiro_crew.config import loader as L
from kiro_crew.config.loader import KiroCrewConfig, SessionSummaryConfig

# Sentinel for "the section is absent from config.json entirely", which is a
# different case from an empty dict.
_ABSENT = object()


class TestSessionSummaryDefaults:
    def test_disabled_on_a_fresh_config(self):
        """Merging PR 1 must change nothing for anyone until the flag flips."""
        assert KiroCrewConfig().session_summary.enabled is False

    def test_documented_defaults(self):
        cfg = SessionSummaryConfig()
        assert cfg.min_user_turns == 2
        assert cfg.regenerate_after_turns == 1
        assert cfg.max_intents == 8
        assert cfg.max_constraints == 5
        assert cfg.assistant_excerpt_chars == 400

    def test_every_field_carries_ui_metadata(self):
        """Config surfaces render label/help from field metadata."""
        for f in SessionSummaryConfig.__dataclass_fields__.values():
            meta = f.metadata.get("x-meta") or f.metadata
            assert meta, f"{f.name} has no metadata"


class TestSessionSummaryClamping:
    def test_below_range_values_are_clamped_not_raised(self):
        cfg = SessionSummaryConfig(
            min_user_turns=0,
            regenerate_after_turns=0,
            max_intents=0,
            max_constraints=-3,
            assistant_excerpt_chars=10,
        )
        assert cfg.min_user_turns == 1
        assert cfg.regenerate_after_turns == 1
        assert cfg.max_intents == 1
        assert cfg.max_constraints == 0
        assert cfg.assistant_excerpt_chars == 80

    def test_zero_constraints_is_allowed(self):
        """Suppressing project notes entirely is a legitimate choice, not an error."""
        assert SessionSummaryConfig(max_constraints=0).max_constraints == 0

    def test_in_range_values_are_untouched(self):
        cfg = SessionSummaryConfig(
            min_user_turns=4,
            regenerate_after_turns=3,
            max_intents=12,
            max_constraints=2,
            assistant_excerpt_chars=800,
        )
        assert asdict(cfg) == {
            "enabled": False,
            "min_user_turns": 4,
            "regenerate_after_turns": 3,
            "max_intents": 12,
            "max_constraints": 2,
            "assistant_excerpt_chars": 800,
        }


class TestSessionSummaryParsing:
    """The section is parsed by ``KiroCrewConfig.load()``, which reads config.json."""

    @staticmethod
    def _load(tmp_path, monkeypatch, section):
        cfgp = tmp_path / "config.json"
        payload = {"agent": {"provider": "acp"}}
        if section is not _ABSENT:
            payload["session_summary"] = section
        cfgp.write_text(json.dumps(payload))
        monkeypatch.setattr(L, "config_path", lambda: cfgp)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
        return KiroCrewConfig.load()

    def test_load_reads_the_section(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"enabled": True, "max_intents": 3})
        assert cfg.session_summary.enabled is True
        assert cfg.session_summary.max_intents == 3

    def test_absent_section_yields_defaults(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, _ABSENT)
        assert cfg.session_summary == SessionSummaryConfig()

    def test_non_dict_section_degrades_to_defaults(self, tmp_path, monkeypatch):
        """A hand-edited config.json must not break gateway start."""
        for junk in ("nope", 7, [], None):
            cfg = self._load(tmp_path, monkeypatch, junk)
            assert cfg.session_summary == SessionSummaryConfig()

    def test_non_numeric_values_fall_back_to_defaults(self, tmp_path, monkeypatch):
        cfg = self._load(
            tmp_path, monkeypatch, {"max_intents": "lots", "assistant_excerpt_chars": None}
        )
        assert cfg.session_summary.max_intents == 8
        assert cfg.session_summary.assistant_excerpt_chars == 400

    def test_out_of_range_values_from_disk_are_clamped(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"max_intents": 0, "min_user_turns": -5})
        assert cfg.session_summary.max_intents == 1
        assert cfg.session_summary.min_user_turns == 1

    def test_section_is_serialized_so_save_round_trips(self):
        assert "session_summary" in KiroCrewConfig().to_dict()

    def test_round_trips_through_to_dict(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"enabled": True, "regenerate_after_turns": 5})
        assert cfg.to_dict()["session_summary"] == asdict(cfg.session_summary)

    def test_section_is_known_not_an_unknown_passthrough(self, tmp_path, monkeypatch):
        """A known section must be parsed, not captured as an edition extra."""
        cfg = self._load(tmp_path, monkeypatch, {"enabled": True})
        assert isinstance(cfg.session_summary, SessionSummaryConfig)
        assert "session_summary" not in cfg._extra_sections

    def test_section_is_registered_in_the_config_schema(self):
        """Config surfaces (CLI, baseline) enumerate SCHEMA_REGISTRY."""
        from kiro_crew.config import schema

        paths = {e.path for e in schema.SCHEMA_REGISTRY}
        assert "session_summary" in paths
        assert "session_summary.enabled" in paths
