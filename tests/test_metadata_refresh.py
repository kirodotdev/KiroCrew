from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from kiro_byo.discovery import DiscoveredModel
from kiro_byo.metadata_refresh import refresh_stale_model_metadata
from kiro_byo.paths import use_app_home


def test_refresh_repairs_stale_root_default_even_when_first_provider_had_no_default(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    path = config_dir / "providers.json"
    path.write_text(json.dumps({
        "credential_schema_version": 1,
        "default_model": "refresh/old",
        "providers": [
            {
                "id": "manual", "name": "Manual", "type": "openai-compatible",
                "preset_id": "custom", "base_url": "https://manual.invalid/v1",
                "auto_discover_models": False,
                "models": [{"id": "manual-a", "name": "Manual A"}],
            },
            {
                "id": "refresh", "name": "Refresh", "type": "openai-compatible",
                "preset_id": "custom", "base_url": "https://refresh.invalid/v1",
                "auto_discover_models": True, "metadata_schema_version": 0,
                "default_model": "old",
                "models": [{"id": "old", "name": "Old"}],
            },
        ],
    }), encoding="utf-8")

    discovered = (
        "refresh", "Refresh",
        [DiscoveredModel("new", "New", catalog_source="provider-discovery")],
    )
    with use_app_home(tmp_path), patch("kiro_byo.metadata_refresh.discover_models", return_value=discovered):
        assert refresh_stale_model_metadata()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["providers"][1]["default_model"] == "new"
    assert raw["providers"][0]["default_model"] == "manual-a"
    assert raw["default_model"] == "manual/manual-a"


def test_refresh_does_not_overwrite_newer_explicit_config_change(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    path = config_dir / "providers.json"
    path.write_text(json.dumps({
        "credential_schema_version": 1,
        "providers": [{
            "id": "refresh", "name": "Refresh", "type": "openai-compatible",
            "preset_id": "custom", "base_url": "https://refresh.invalid/v1",
            "auto_discover_models": True, "metadata_schema_version": 0,
            "default_model": "old",
            "models": [{"id": "old", "name": "Old"}],
        }],
    }), encoding="utf-8")

    discovered = (
        "refresh", "Refresh",
        [DiscoveredModel("new", "New", catalog_source="provider-discovery")],
    )

    def discover_after_user_edit(*args, **kwargs):
        # Simulate a foreground UI/CLI write landing while the background
        # discovery request is in flight. The refresh must never replace it.
        newer = json.loads(path.read_text(encoding="utf-8"))
        newer["foreground_edit"] = "keep-me"
        path.write_text(json.dumps(newer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return discovered

    with use_app_home(tmp_path), patch(
        "kiro_byo.metadata_refresh.discover_models", side_effect=discover_after_user_edit
    ):
        assert refresh_stale_model_metadata() is False

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["foreground_edit"] == "keep-me"
    assert raw["providers"][0]["models"][0]["id"] == "old"


def test_refresh_preserves_explicit_thinking_preference_by_model_id(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    path = config_dir / "providers.json"
    path.write_text(json.dumps({
        "credential_schema_version": 1,
        "providers": [{
            "id": "refresh", "name": "Refresh", "type": "openai-compatible",
            "preset_id": "custom", "base_url": "https://refresh.invalid/v1",
            "auto_discover_models": True, "metadata_schema_version": 0,
            "default_model": "m",
            "models": [{
                "id": "m", "name": "Old M", "thinking_preference": "off",
                "reasoning": {
                    "mode": "levels", "supported": True, "can_disable": True,
                    "disable_value": "none", "effort": {"levels": ["low", "high"]},
                },
            }],
        }],
    }), encoding="utf-8")

    discovered = (
        "refresh", "Refresh",
        [DiscoveredModel(
            "m", "Fresh M", reasoning_mode="levels", reasoning_supported=True,
            reasoning_can_disable=True, reasoning_disable_value="none",
            effort_supported_levels=("low", "high"), catalog_source="provider-discovery",
        )],
    )
    with use_app_home(tmp_path), patch("kiro_byo.metadata_refresh.discover_models", return_value=discovered):
        assert refresh_stale_model_metadata()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["providers"][0]["models"][0]["id"] == "m"
    assert raw["providers"][0]["models"][0]["thinking_preference"] == "off"
