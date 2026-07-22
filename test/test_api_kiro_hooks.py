"""Tests for GET /api/kiro-hooks handler.

Covers the 5 paths identified in review:
1. OSError/JSONDecodeError on agent_cfg.read_text() → empty hooks
2. _shipped_defaults() returning non-existent path → bundled fallback
3. Malformed JSON where raw is not a dict → isinstance guard
4. Unknown event filtering → events not in _VALID_HOOK_EVENTS dropped
5. Redaction → redact() called on command/matcher values
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.hooks import api_kiro_hooks

# Handler does `from kiro_crew.agent import KIRO_AGENTS_DIR, _shipped_defaults`
# and `from kiro_crew.security import redact` inside the function body each call,
# so we patch at the source module where the names are defined.
_P_AGENTS_DIR = "kiro_crew.agent.KIRO_AGENTS_DIR"
_P_DEFAULTS = "kiro_crew.agent._shipped_defaults"
_P_REDACT = "kiro_crew.security.redact"


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/kiro-hooks", api_kiro_hooks)
    return app


@pytest.fixture
def kiro_dir(tmp_path: Path) -> Path:
    """Temp directory standing in for KIRO_AGENTS_DIR."""
    return tmp_path / "agents"


def _write_agent_cfg(kiro_dir: Path, data: object) -> None:
    kiro_dir.mkdir(parents=True, exist_ok=True)
    (kiro_dir / "kirocrew.json").write_text(json.dumps(data))


def _write_defaults(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class TestApiKiroHooks:
    """Unit tests for api_kiro_hooks handler."""

    @pytest.mark.asyncio
    async def test_missing_agent_cfg_returns_empty(self, kiro_dir: Path, tmp_path: Path) -> None:
        """Path 1: OSError when kirocrew.json doesn't exist → empty hooks."""
        defaults = tmp_path / "defaults.json"
        _write_defaults(defaults, {"hooks": {}})
        with patch(_P_AGENTS_DIR, kiro_dir), patch(_P_DEFAULTS, return_value=defaults):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                assert (await resp.json()) == {"hooks": {}}

    @pytest.mark.asyncio
    async def test_malformed_json_in_agent_cfg(self, kiro_dir: Path, tmp_path: Path) -> None:
        """Path 1: JSONDecodeError in kirocrew.json → empty hooks."""
        kiro_dir.mkdir(parents=True, exist_ok=True)
        (kiro_dir / "kirocrew.json").write_text("{not valid json")
        defaults = tmp_path / "defaults.json"
        _write_defaults(defaults, {"hooks": {}})
        with patch(_P_AGENTS_DIR, kiro_dir), patch(_P_DEFAULTS, return_value=defaults):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                assert (await resp.json()) == {"hooks": {}}

    @pytest.mark.asyncio
    async def test_missing_defaults_file(self, kiro_dir: Path, tmp_path: Path) -> None:
        """Path 2: _shipped_defaults() points to non-existent file → all tagged 'user'."""
        _write_agent_cfg(kiro_dir, {
            "hooks": {"preToolUse": [{"command": "echo hi", "matcher": ""}]},
        })
        missing = tmp_path / "no_such_defaults.json"
        with patch(_P_AGENTS_DIR, kiro_dir), patch(_P_DEFAULTS, return_value=missing):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                hooks = (await resp.json())["hooks"]
                assert "preToolUse" in hooks
                assert hooks["preToolUse"][0]["source"] == "user"

    @pytest.mark.asyncio
    async def test_raw_not_dict_returns_empty(self, kiro_dir: Path, tmp_path: Path) -> None:
        """Path 3: kirocrew.json contains a list → isinstance(raw, dict) guard."""
        _write_agent_cfg(kiro_dir, ["not", "a", "dict"])
        defaults = tmp_path / "defaults.json"
        _write_defaults(defaults, {"hooks": {}})
        with patch(_P_AGENTS_DIR, kiro_dir), patch(_P_DEFAULTS, return_value=defaults):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                assert (await resp.json()) == {"hooks": {}}

    @pytest.mark.asyncio
    async def test_unknown_events_dropped(self, kiro_dir: Path, tmp_path: Path) -> None:
        """Path 4: Events not in _VALID_HOOK_EVENTS are filtered out."""
        _write_agent_cfg(kiro_dir, {
            "hooks": {
                "preToolUse": [{"command": "echo valid"}],
                "evilInjectedEvent": [{"command": "echo bad"}],
            },
        })
        defaults = tmp_path / "defaults.json"
        _write_defaults(defaults, {"hooks": {}})
        with patch(_P_AGENTS_DIR, kiro_dir), patch(_P_DEFAULTS, return_value=defaults):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                hooks = (await resp.json())["hooks"]
                assert "preToolUse" in hooks
                assert "evilInjectedEvent" not in hooks

    @pytest.mark.asyncio
    async def test_redaction_applied(self, kiro_dir: Path, tmp_path: Path) -> None:
        """Path 5: redact() is called on command and matcher values."""
        _write_agent_cfg(kiro_dir, {
            "hooks": {"postToolUse": [{"command": "echo secret", "matcher": "tool_*"}]},
        })
        defaults = tmp_path / "defaults.json"
        _write_defaults(defaults, {"hooks": {}})
        with (
            patch(_P_AGENTS_DIR, kiro_dir),
            patch(_P_DEFAULTS, return_value=defaults),
            patch(_P_REDACT, side_effect=lambda t: f"[R:{t}]") as mock_redact,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                entry = (await resp.json())["hooks"]["postToolUse"][0]
                assert entry["command"] == "[R:echo secret]"
                assert entry["matcher"] == "[R:tool_*]"
                assert mock_redact.call_count == 2

    @pytest.mark.asyncio
    async def test_bundled_vs_user_tagging(self, kiro_dir: Path, tmp_path: Path) -> None:
        """Hooks matching bundled defaults tagged 'bundled', others 'user'."""
        bundled_cmd = "aim agents publish-metrics || true"
        user_cmd = "echo custom"
        _write_agent_cfg(kiro_dir, {
            "hooks": {
                "userPromptSubmit": [
                    {"command": bundled_cmd, "matcher": ""},
                    {"command": user_cmd, "matcher": ""},
                ],
            },
        })
        defaults = tmp_path / "defaults.json"
        _write_defaults(defaults, {
            "hooks": {"userPromptSubmit": [{"command": bundled_cmd, "matcher": ""}]},
        })
        with patch(_P_AGENTS_DIR, kiro_dir), patch(_P_DEFAULTS, return_value=defaults):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                entries = (await resp.json())["hooks"]["userPromptSubmit"]
                assert entries[0]["source"] == "bundled"
                assert entries[1]["source"] == "user"

    @pytest.mark.asyncio
    async def test_non_dict_entries_skipped(self, kiro_dir: Path, tmp_path: Path) -> None:
        """Non-dict entries in hook arrays are silently skipped."""
        _write_agent_cfg(kiro_dir, {
            "hooks": {"preToolUse": ["not-a-dict", 42, {"command": "echo ok"}]},
        })
        defaults = tmp_path / "defaults.json"
        _write_defaults(defaults, {"hooks": {}})
        with patch(_P_AGENTS_DIR, kiro_dir), patch(_P_DEFAULTS, return_value=defaults):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                entries = (await resp.json())["hooks"]["preToolUse"]
                assert len(entries) == 1
                assert "echo ok" in entries[0]["command"]

    @pytest.mark.asyncio
    async def test_non_list_event_value_skipped(self, kiro_dir: Path, tmp_path: Path) -> None:
        """Event value that is not a list produces no entries."""
        _write_agent_cfg(kiro_dir, {"hooks": {"preToolUse": "not-a-list"}})
        defaults = tmp_path / "defaults.json"
        _write_defaults(defaults, {"hooks": {}})
        with patch(_P_AGENTS_DIR, kiro_dir), patch(_P_DEFAULTS, return_value=defaults):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                assert (await resp.json()) == {"hooks": {}}
