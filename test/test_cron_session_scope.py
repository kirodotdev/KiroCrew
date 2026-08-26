"""Tests for session-scoped cron_remove_all."""

from __future__ import annotations

import uuid

import pytest

from kiro_crew.cron import CronService
from kiro_crew.mcp_cron import _call_tool_inner


def _unique_name() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    """Route all CronService() instances to tmp_path for test isolation."""
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)


class TestCronAddSessionKey:
    def test_captures_session_key_from_env(self, monkeypatch):
        """cron_add tags job with KIROCREW_SESSION_KEY."""
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-abc")
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = _unique_name()
        result = _call_tool_inner("cron_add", {"name": name, "message": "hi", "every": 120})
        assert "Added job" in result

        svc = CronService()
        jobs = [j for j in svc.list_jobs() if j.name == name]
        assert jobs[0].session_key == "sess-abc"

    def test_an_unidentified_caller_cannot_create_an_ownerless_job(self, monkeypatch):
        """Without a resolvable identity, ``cron_add`` refuses instead of storing "".

        This used to store an ownerless row, and on a pooled backend -- where no
        identity was resolvable at all -- that was EVERY row, which is what made
        the ownership gate unenforceable. Refusing stops this server from adding
        to the ownerless set; the CLI and the importer still write to it, and such
        rows are visible-but-not-mutable through MCP. See
        ``test/test_mcp_cron_caller_identity.py`` for the full rule.
        """
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        monkeypatch.delenv("KIROCREW_CLI", raising=False)
        monkeypatch.setattr("kiro_crew.mcp_cron._authz_session_key", lambda: "")
        name = _unique_name()
        result = _call_tool_inner("cron_add", {"name": name, "message": "hi", "every": 120})

        assert "cannot determine which session is calling" in result
        svc = CronService()
        assert [j for j in svc.list_jobs() if j.name == name] == []


class TestCronRemoveAllScoped:
    def test_removes_only_own_session_jobs(self, monkeypatch):
        """With session key, cron_remove_all only removes jobs from that session."""
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        monkeypatch.delenv("KIROCREW_CLI", raising=False)
        n1, n2 = _unique_name(), _unique_name()

        # Create job as session A
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-A")
        _call_tool_inner("cron_add", {"name": n1, "message": "a", "every": 120})

        # Create job as session B
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-B")
        _call_tool_inner("cron_add", {"name": n2, "message": "b", "every": 120})

        # Remove all as session A — should only remove n1
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-A")
        result = _call_tool_inner("cron_remove_all", {})
        assert "Removed 1 job(s)" in result

        svc = CronService()
        remaining = [j for j in svc.list_jobs() if j.name in (n1, n2)]
        assert len(remaining) == 1
        assert remaining[0].name == n2

    def test_cli_removes_all(self, monkeypatch):
        """With KIROCREW_CLI=1, cron_remove_all removes everything."""
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        n1, n2 = _unique_name(), _unique_name()

        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-X")
        _call_tool_inner("cron_add", {"name": n1, "message": "a", "every": 120})

        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-Y")
        _call_tool_inner("cron_add", {"name": n2, "message": "b", "every": 120})

        # CLI admin removes everything
        monkeypatch.setenv("KIROCREW_CLI", "1")
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        result = _call_tool_inner("cron_remove_all", {})
        assert "Removed 2 job(s)" in result

    def test_no_session_key_no_cli_returns_error(self, monkeypatch):
        """Without session key or CLI flag, returns error.

        Still an error, now the shared one every mutating tool gives an
        unidentifiable caller rather than a message unique to this tool.
        """
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.delenv("KIROCREW_CLI", raising=False)
        name = _unique_name()

        # Create a job via CLI so it exists
        monkeypatch.setenv("KIROCREW_CLI", "1")
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-owner")
        _call_tool_inner("cron_add", {"name": name, "message": "a", "every": 120})

        # Try to remove without session key or CLI
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.delenv("KIROCREW_CLI", raising=False)
        monkeypatch.setattr("kiro_crew.mcp_cron._authz_session_key", lambda: "")
        result = _call_tool_inner("cron_remove_all", {})
        assert "cannot determine which session is calling" in result

    def test_no_matching_jobs_returns_message(self, monkeypatch):
        """Session with no owned jobs gets appropriate message."""
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        monkeypatch.delenv("KIROCREW_CLI", raising=False)
        name = _unique_name()

        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-owner")
        _call_tool_inner("cron_add", {"name": name, "message": "a", "every": 120})

        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-other")
        result = _call_tool_inner("cron_remove_all", {})
        assert "No cron jobs owned by this session" in result


class TestSessionKeyPersistence:
    def test_session_key_survives_reload(self, tmp_path):
        """session_key field persists through save/load cycle."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="persist", message="test", every_secs=300)
        job.session_key = "sess-persist"
        svc._save()

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        assert svc2.list_jobs()[0].session_key == "sess-persist"

    def test_missing_session_key_defaults_empty(self, tmp_path):
        """Old crons.json without session_key defaults to empty string."""
        import json

        data = {
            "version": 2,
            "jobs": [
                {
                    "id": "abc123",
                    "name": "legacy",
                    "message": "hi",
                    "schedule": {"kind": "every", "every_secs": 300},
                }
            ],
        }
        (tmp_path / "crons.json").write_text(json.dumps(data))
        svc = CronService(base_dir=tmp_path)
        svc._load()
        assert svc.list_jobs()[0].session_key == ""
