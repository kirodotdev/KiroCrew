"""Tests for the recipes section of kiro_crew.apps.manifest.

Covers SlackRecipe, CronRecipe, and RecipesConfig
dataclasses plus AppManifest._validate_recipes integration.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.manifest import (
    AppManifest,
    CronRecipe,
    RecipesConfig,
    SlackRecipe,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_app(**overrides) -> dict:
    base = {
        "name": "test-app",
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app",
    }
    base.update(overrides)
    return base


def _valid_slack_recipe(**overrides) -> dict:
    base = {
        "name": "task-intake",
        "description": "Task intake channel",
        "channelNamePart": "intake",
        "agent": "task-intake",
        "activation": "always",
    }
    base.update(overrides)
    return base


def _valid_cron_recipe(**overrides) -> dict:
    base = {
        "name": "daily-briefing",
        "description": "Morning briefing",
        "schedule": "0 12 * * 1-5",
        "agent": "gpu-comms",
        "promptText": "Run daily briefing",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# SlackRecipe
# ---------------------------------------------------------------------------


class TestSlackRecipeRoundTrip:
    def test_minimal_round_trip(self):
        original = SlackRecipe.from_dict(_valid_slack_recipe())
        restored = SlackRecipe.from_dict(original.to_dict())
        assert restored == original

    def test_with_purpose_round_trip(self):
        d = _valid_slack_recipe(purpose="custom channel purpose")
        original = SlackRecipe.from_dict(d)
        assert original.purpose == "custom channel purpose"
        restored = SlackRecipe.from_dict(original.to_dict())
        assert restored == original

    def test_purpose_omitted_when_empty(self):
        d = SlackRecipe.from_dict(_valid_slack_recipe()).to_dict()
        assert "purpose" not in d


# ---------------------------------------------------------------------------
# CronRecipe
# ---------------------------------------------------------------------------


class TestCronRecipeRoundTrip:
    def test_schedule_based_round_trip(self):
        original = CronRecipe.from_dict(_valid_cron_recipe())
        restored = CronRecipe.from_dict(original.to_dict())
        assert restored == original

    def test_every_secs_round_trip(self):
        d = _valid_cron_recipe(schedule="", everySecs=300)
        original = CronRecipe.from_dict(d)
        assert original.everySecs == 300
        assert original.schedule == ""
        restored = CronRecipe.from_dict(original.to_dict())
        assert restored == original

    def test_prompt_file_round_trip(self):
        d = _valid_cron_recipe(promptText="", promptFile="prompts/daily.md")
        original = CronRecipe.from_dict(d)
        assert original.promptFile == "prompts/daily.md"
        assert original.promptText == ""
        restored = CronRecipe.from_dict(original.to_dict())
        assert restored == original

    def test_channel_name_part_round_trip(self):
        d = _valid_cron_recipe(channelNamePart="briefing")
        original = CronRecipe.from_dict(d)
        assert original.channelNamePart == "briefing"
        restored = CronRecipe.from_dict(original.to_dict())
        assert restored == original

    def test_persistent_session_default_omitted(self):
        # Default True should not appear in serialized form
        d = CronRecipe.from_dict(_valid_cron_recipe()).to_dict()
        assert "persistentSession" not in d

    def test_persistent_session_false_serialized(self):
        d = _valid_cron_recipe()
        d["persistentSession"] = False
        original = CronRecipe.from_dict(d)
        assert original.persistentSession is False
        out = original.to_dict()
        assert out["persistentSession"] is False

    def test_silent_default_omitted(self):
        d = CronRecipe.from_dict(_valid_cron_recipe()).to_dict()
        assert "silent" not in d

    def test_silent_true_serialized(self):
        d = _valid_cron_recipe()
        d["silent"] = True
        original = CronRecipe.from_dict(d)
        assert original.silent is True
        assert original.to_dict()["silent"] is True


# ---------------------------------------------------------------------------
# RecipesConfig
# ---------------------------------------------------------------------------


class TestRecipesConfigRoundTrip:
    def test_empty_to_dict(self):
        assert RecipesConfig().to_dict() == {}

    def test_slack_only_round_trip(self):
        original = RecipesConfig.from_dict({"slack": [_valid_slack_recipe()]})
        assert len(original.slack) == 1
        assert len(original.crons) == 0
        restored = RecipesConfig.from_dict(original.to_dict())
        assert restored == original

    def test_crons_only_round_trip(self):
        original = RecipesConfig.from_dict({"crons": [_valid_cron_recipe()]})
        assert len(original.slack) == 0
        assert len(original.crons) == 1
        restored = RecipesConfig.from_dict(original.to_dict())
        assert restored == original

    def test_mixed_round_trip(self):
        original = RecipesConfig.from_dict(
            {
                "slack": [_valid_slack_recipe()],
                "crons": [_valid_cron_recipe()],
            }
        )
        restored = RecipesConfig.from_dict(original.to_dict())
        assert restored == original

    def test_non_dict_entries_skipped(self):
        # Defensive: malformed array entries must not crash discovery
        cfg = RecipesConfig.from_dict(
            {
                "slack": [_valid_slack_recipe(), "not a dict", 42],
                "crons": [_valid_cron_recipe(), None],
            }
        )
        assert len(cfg.slack) == 1
        assert len(cfg.crons) == 1


# ---------------------------------------------------------------------------
# AppManifest integration — recipes section parses, validates, round-trips
# ---------------------------------------------------------------------------


class TestAppManifestRecipesIntegration:
    def test_manifest_with_recipes_round_trip(self):
        data = _valid_app(
            recipes={
                "slack": [_valid_slack_recipe()],
                "crons": [_valid_cron_recipe()],
            }
        )
        m = AppManifest.from_dict(data)
        assert len(m.recipes.slack) == 1
        assert len(m.recipes.crons) == 1
        assert m.recipes.slack[0].name == "task-intake"
        assert m.recipes.crons[0].name == "daily-briefing"
        # Round-trip preserves the section
        m2 = AppManifest.from_dict(m.to_dict())
        assert m2.recipes == m.recipes

    def test_manifest_without_recipes_omits_section(self):
        m = AppManifest.from_dict(_valid_app())
        assert m.recipes.slack == []
        assert m.recipes.crons == []
        assert "recipes" not in m.to_dict()

    def test_recipes_field_not_in_extra(self):
        # Forward-compat: 'recipes' is now a known field, must not leak into extra
        data = _valid_app(recipes={"slack": [_valid_slack_recipe()]})
        m = AppManifest.from_dict(data)
        assert "recipes" not in m.extra


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestRecipesValidation:
    def _validate(self, **recipes_overrides) -> list[str]:
        data = _valid_app(recipes=recipes_overrides)
        return AppManifest.from_dict(data).validate()

    # ---- Slack recipes ----

    def test_valid_slack_recipe_passes(self):
        errors = self._validate(slack=[_valid_slack_recipe()])
        assert errors == []

    def test_slack_missing_name(self):
        errors = self._validate(slack=[_valid_slack_recipe(name="")])
        assert any("recipes.slack entry missing required field: name" in e for e in errors)

    def test_slack_non_kebab_name(self):
        errors = self._validate(slack=[_valid_slack_recipe(name="Not_Kebab")])
        assert any("kebab-case" in e for e in errors)

    def test_slack_missing_description(self):
        errors = self._validate(slack=[_valid_slack_recipe(description="")])
        assert any("description" in e for e in errors)

    def test_slack_missing_channel_name_part(self):
        errors = self._validate(slack=[_valid_slack_recipe(channelNamePart="")])
        assert any("channelNamePart" in e for e in errors)

    def test_slack_channel_name_part_too_long(self):
        errors = self._validate(slack=[_valid_slack_recipe(channelNamePart="this-is-way-too-long")])
        assert any("≤15" in e for e in errors)

    def test_slack_missing_agent(self):
        errors = self._validate(slack=[_valid_slack_recipe(agent="")])
        assert any("agent" in e for e in errors)

    def test_slack_invalid_activation(self):
        errors = self._validate(slack=[_valid_slack_recipe(activation="invalid")])
        assert any("activation" in e for e in errors)

    @pytest.mark.parametrize("activation", ["always", "mention", "observe"])
    def test_slack_valid_activations(self, activation):
        errors = self._validate(slack=[_valid_slack_recipe(activation=activation)])
        assert errors == []

    def test_slack_duplicate_name(self):
        errors = self._validate(
            slack=[
                _valid_slack_recipe(name="dup"),
                _valid_slack_recipe(name="dup", channelNamePart="other"),
            ]
        )
        assert any("duplicate" in e for e in errors)

    # ---- Cron recipes ----

    def test_valid_cron_recipe_schedule_passes(self):
        errors = self._validate(crons=[_valid_cron_recipe()])
        assert errors == []

    def test_valid_cron_recipe_every_secs_passes(self):
        errors = self._validate(crons=[_valid_cron_recipe(schedule="", everySecs=300)])
        assert errors == []

    def test_cron_missing_name(self):
        errors = self._validate(crons=[_valid_cron_recipe(name="")])
        assert any("recipes.crons entry missing required field: name" in e for e in errors)

    def test_cron_non_kebab_name(self):
        errors = self._validate(crons=[_valid_cron_recipe(name="BadName")])
        assert any("kebab-case" in e for e in errors)

    def test_cron_missing_agent(self):
        errors = self._validate(crons=[_valid_cron_recipe(agent="")])
        assert any("agent" in e for e in errors)

    def test_cron_missing_schedule_and_every_secs(self):
        errors = self._validate(crons=[_valid_cron_recipe(schedule="", everySecs=0)])
        assert any("schedule" in e or "everySecs" in e for e in errors)

    def test_cron_both_schedule_and_every_secs(self):
        errors = self._validate(crons=[_valid_cron_recipe(schedule="0 12 * * *", everySecs=300)])
        assert any("not both" in e for e in errors)

    def test_cron_missing_prompt(self):
        errors = self._validate(crons=[_valid_cron_recipe(promptText="", promptFile="")])
        assert any("promptFile" in e or "promptText" in e for e in errors)

    def test_cron_both_prompt_file_and_text(self):
        errors = self._validate(
            crons=[_valid_cron_recipe(promptFile="prompts/x.md", promptText="inline")]
        )
        assert any("not both" in e for e in errors)

    def test_cron_prompt_file_path_traversal(self):
        errors = self._validate(
            crons=[_valid_cron_recipe(promptText="", promptFile="../../etc/passwd")]
        )
        assert any("path traversal" in e for e in errors)

    def test_cron_channel_name_part_too_long(self):
        errors = self._validate(crons=[_valid_cron_recipe(channelNamePart="this-is-way-too-long")])
        assert any("≤15" in e for e in errors)

    def test_cron_channel_name_part_valid_when_omitted(self):
        # channelNamePart is OPTIONAL on cron recipes (no output channel)
        errors = self._validate(crons=[_valid_cron_recipe(channelNamePart="")])
        assert errors == []

    def test_cron_duplicate_name(self):
        errors = self._validate(
            crons=[
                _valid_cron_recipe(name="dup"),
                _valid_cron_recipe(name="dup"),
            ]
        )
        assert any("duplicate" in e for e in errors)

    # ---- Combined ----

    def test_slack_and_cron_can_share_name(self):
        # Cross-type duplication is fine (different namespaces)
        errors = self._validate(
            slack=[_valid_slack_recipe(name="thing")],
            crons=[_valid_cron_recipe(name="thing")],
        )
        assert errors == []

    def test_existing_app_manifest_validation_still_works(self):
        # Assert: pre-existing manifest validation paths unaffected by recipes addition
        m = AppManifest.from_dict(_valid_app(name="Bad_Name"))
        errors = m.validate()
        assert any("kebab-case" in e for e in errors)
