"""Tests for GET /api/kiro-hooks handler.

Covers the 5 paths identified in review:
1. OSError/JSONDecodeError on agent_cfg.read_text(encoding="utf-8") → empty hooks
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

import kiro_crew
from kiro_crew.agent import _VALID_HOOK_EVENTS
from kiro_crew.dashboard.handlers.hooks import api_kiro_hooks

# The handler imports `_shipped_defaults` at module scope (hoisted), so
# it is patched in the handler's namespace.  `KIRO_AGENTS_DIR` stays patched at
# the SOURCE module on purpose: `kiro_agents_dir_path()` reads it from `agent`'s
# globals at call time, so the source-module patch is unaffected by the hoist —
# do NOT retarget it to the handler namespace (the name does not exist there).
# `redact` is likewise patched where it is defined.
_P_AGENTS_DIR = "kiro_crew.agent.KIRO_AGENTS_DIR"
_P_DEFAULTS = "kiro_crew.dashboard.handlers.hooks._shipped_defaults"
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
                assert (await resp.json()) == {"hooks": {}, "unsent": []}

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
                assert (await resp.json()) == {"hooks": {}, "unsent": []}

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
                assert (await resp.json()) == {"hooks": {}, "unsent": []}

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
                # Count the HANDLER's calls, not every call in the process. The patch
                # target is the process-wide ``security.redact``, and the SEL routes
                # every field it persists through the same function -- so any audit row
                # written while the platform context composes on first use (a tier
                # record from the policy ladder, say) would otherwise be counted
                # against this handler and make the test assert on unrelated
                # subsystems.
                own = [
                    c for c in mock_redact.call_args_list if c.args[0] in ("echo secret", "tool_*")
                ]
                assert len(own) == 2

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
                assert (await resp.json()) == {"hooks": {}, "unsent": []}

    @pytest.mark.asyncio
    async def test_bundled_defaults_read_as_utf8_under_cjk_locale(
        self, kiro_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shipped defaults.json decodes as UTF-8 whatever the locale code page.

        ``Path.read_text()`` without ``encoding=`` decodes with the locale code
        page. The patch stands in for a cp932 (Japanese Windows) locale there, so
        only a read that names its encoding sees the file's real text.
        """
        shipped = Path(kiro_crew.__file__).parent / "config" / "defaults.json"
        shipped_bytes = shipped.read_bytes()
        with pytest.raises(UnicodeDecodeError):
            shipped_bytes.decode("cp932")  # precondition: the file is not cp932-clean
        bundled_hooks = json.loads(shipped_bytes.decode("utf-8"))["hooks"]
        event, entries = next(
            (k, v) for k, v in bundled_hooks.items() if k in _VALID_HOOK_EVENTS and v
        )
        bundled = entries[0]
        _write_agent_cfg(kiro_dir, {"hooks": {event: [bundled]}})

        real_read_text = Path.read_text

        def _cp932_locale_read_text(
            self: Path, encoding: str | None = None, errors: str | None = None
        ) -> str:
            return real_read_text(self, encoding=encoding or "cp932", errors=errors)

        monkeypatch.setattr(Path, "read_text", _cp932_locale_read_text)
        with patch(_P_AGENTS_DIR, kiro_dir), patch(_P_DEFAULTS, return_value=shipped):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                entry = (await resp.json())["hooks"][event][0]
                assert entry["source"] == "bundled"

    @pytest.mark.asyncio
    async def test_undecodable_defaults_degrade_to_user(
        self, kiro_dir: Path, tmp_path: Path
    ) -> None:
        """A defaults file that is not valid UTF-8 means no bundled tags, not a 500."""
        _write_agent_cfg(
            kiro_dir, {"hooks": {"preToolUse": [{"command": "echo hi", "matcher": ""}]}}
        )
        defaults = tmp_path / "defaults.json"
        defaults.write_bytes(b'{"hooks": {"preToolUse": [{"command": "\xff"}]}}')
        with patch(_P_AGENTS_DIR, kiro_dir), patch(_P_DEFAULTS, return_value=defaults):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                assert (await resp.json())["hooks"]["preToolUse"][0]["source"] == "user"
