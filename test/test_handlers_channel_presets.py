"""Tests for /api/channels/presets handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers_channel import _DEFAULT_PRESETS, api_channel_presets


class TestChannelPresets:
    @pytest.mark.asyncio
    async def test_returns_defaults_when_config_missing(self, tmp_path, monkeypatch):
        """When config.json doesn't exist, the built-in defaults are returned."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers_channel.config_path",
            lambda: tmp_path / "missing.json",
        )
        request = MagicMock()
        resp = await api_channel_presets(request)
        body = json.loads(resp.body)
        assert body["presets"] == _DEFAULT_PRESETS

    @pytest.mark.asyncio
    async def test_returns_defaults_when_config_has_no_presets_key(
        self, tmp_path, monkeypatch
    ):
        """A config without ``channel_presets`` falls back to defaults."""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"unrelated": "value"}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.dashboard.handlers_channel.config_path", lambda: cfg)
        request = MagicMock()
        resp = await api_channel_presets(request)
        body = json.loads(resp.body)
        assert body["presets"] == _DEFAULT_PRESETS

    @pytest.mark.asyncio
    async def test_returns_custom_presets_from_config(self, tmp_path, monkeypatch):
        """Custom ``channel_presets`` in config.json are returned to the client."""
        custom = [
            {
                "id": "custom-hub",
                "label": "Custom Hub",
                "agents": [
                    {"role": "Lead", "is_orchestrator": True, "task": "Coordinate {topic}"},
                    {"role": "Helper", "task": "Assist with {topic}"},
                ],
            }
        ]
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"channel_presets": custom}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.dashboard.handlers_channel.config_path", lambda: cfg)
        request = MagicMock()
        resp = await api_channel_presets(request)
        body = json.loads(resp.body)
        assert body["presets"] == custom

    @pytest.mark.asyncio
    async def test_falls_back_to_defaults_on_malformed_config(
        self, tmp_path, monkeypatch
    ):
        """A config.json with invalid JSON does not crash the handler."""
        cfg = tmp_path / "config.json"
        cfg.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr("kiro_crew.dashboard.handlers_channel.config_path", lambda: cfg)
        request = MagicMock()
        resp = await api_channel_presets(request)
        body = json.loads(resp.body)
        assert body["presets"] == _DEFAULT_PRESETS

    @pytest.mark.asyncio
    async def test_falls_back_to_defaults_on_non_dict_json(
        self, tmp_path, monkeypatch
    ):
        """A config.json containing a non-dict (e.g. an array) does not crash."""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.dashboard.handlers_channel.config_path", lambda: cfg)
        request = MagicMock()
        resp = await api_channel_presets(request)
        body = json.loads(resp.body)
        assert body["presets"] == _DEFAULT_PRESETS
