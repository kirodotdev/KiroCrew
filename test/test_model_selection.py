"""Tests for model selection: spawn_run model param."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.mcp_core import _call_tool
from kiro_crew.validation import SPAWN_RUN_SCHEMA, ValidationError, validate_tool_args


class TestModelValidation:
    """Model name pattern validation in SPAWN_RUN_SCHEMA."""

    def test_valid_model_names(self):
        for name in ["claude-opus-4.8", "deepseek-3.2", "auto", "claude-haiku-4.5"]:
            result = validate_tool_args({"task": "x", "model": name}, SPAWN_RUN_SCHEMA)
            assert result["model"] == name

    def test_empty_model_passes(self):
        result = validate_tool_args({"task": "x", "model": ""}, SPAWN_RUN_SCHEMA)
        assert result.get("model") == ""

    def test_invalid_model_rejected(self):
        for bad in ["'; drop table", "../etc/passwd", "model name with spaces"]:
            with pytest.raises(ValidationError):
                validate_tool_args({"task": "x", "model": bad}, SPAWN_RUN_SCHEMA)


class TestSpawnRunModelParam:
    """Model param is threaded through to the POST body."""

    def test_model_passed_in_post_body(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCREW_SESSION_KEY": "sess"}
        ):
            mock_post.return_value = {"id": "agent1"}
            _call_tool("spawn_run", {"task": "test", "model": "deepseek-3.2"})
            body = mock_post.call_args[0][1]
            assert body["model"] == "deepseek-3.2"

    def test_no_model_omits_from_body(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCREW_SESSION_KEY": "sess"}
        ):
            mock_post.return_value = {"id": "agent1"}
            _call_tool("spawn_run", {"task": "test"})
            body = mock_post.call_args[0][1]
            assert "model" not in body
