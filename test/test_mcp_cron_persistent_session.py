"""Tests for the `persistent_session` flag on the `cron_add` MCP tool.

Covers. The flag must be:
- Exposed in the tool input schema (discoverable by LLMs)
- Default True when omitted (backward-compat)
- Stored on the CronJob when False
"""

from __future__ import annotations

import uuid

import pytest

from kiro_crew.cron import CronService
from kiro_crew.mcp_cron import _call_tool_inner, _list_tools, _validate_args


def _unique_name() -> str:
    return f"psess-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    # Pinned rather than deleted: ``mcp_cron`` refuses a write from a caller it
    # cannot name, and every test here writes. A fixed value keeps the isolation
    # this fixture is for (no ambient key leaks in) while stating the identity
    # precondition these tests always assumed.
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:psess-slot")


class TestCronAddPersistentSession:
    def test_schema_declares_persistent_session(self):
        """cron_add tool schema must include the flag so LLMs can find it."""
        tools = _list_tools()
        cron_add = next(t for t in tools if t["name"] == "cron_add")
        props = cron_add["inputSchema"]["properties"]
        assert "persistent_session" in props
        assert props["persistent_session"]["type"] == "boolean"

    def test_default_is_persistent(self):
        """cron_add without the flag → job.persistent_session is True."""
        name = _unique_name()
        result = _call_tool_inner(
            "cron_add", {"name": name, "message": "hi", "every": 120}
        )
        assert "Added job" in result

        svc = CronService()
        jobs = [j for j in svc.list_jobs() if j.name == name]
        assert len(jobs) == 1
        assert jobs[0].persistent_session is True

    def test_false_flag_is_stored(self):
        """cron_add with persistent_session=False → stored as False."""
        name = _unique_name()
        result = _call_tool_inner(
            "cron_add",
            {
                "name": name,
                "message": "hi",
                "every": 120,
                "persistent_session": False,
            },
        )
        assert "Added job" in result

        svc = CronService()
        jobs = [j for j in svc.list_jobs() if j.name == name]
        assert len(jobs) == 1
        assert jobs[0].persistent_session is False

    def test_true_flag_is_explicit_noop(self):
        """cron_add with persistent_session=True → stored as True (explicit)."""
        name = _unique_name()
        _call_tool_inner(
            "cron_add",
            {
                "name": name,
                "message": "hi",
                "every": 120,
                "persistent_session": True,
            },
        )
        svc = CronService()
        jobs = [j for j in svc.list_jobs() if j.name == name]
        assert jobs[0].persistent_session is True


class TestCronAddPersistentSessionValidation:
    """validation.py enforces the flag's type before it reaches _call_tool_inner.

    security follow-up (review-bot comment on):
    raw LLM-supplied args must pass through CRON_ADD_SCHEMA, never be
    consumed directly. This class pins the validator's contract.
    """

    def test_schema_has_persistent_session_bool_field(self):
        """CRON_ADD_SCHEMA declares persistent_session with bool type."""
        from kiro_crew.validation import CRON_ADD_SCHEMA

        specs = {f.name: f for f in CRON_ADD_SCHEMA.fields}
        assert "persistent_session" in specs, (
            "persistent_session must be declared in CRON_ADD_SCHEMA "
            "so unknown-field rejection does not block valid callers"
        )
        assert specs["persistent_session"].type is bool

    def test_validator_accepts_true(self):
        """bool True passes through cleanly."""
        cleaned = _validate_args(
            "cron_add",
            {"name": "x", "message": "y", "every": 120, "persistent_session": True},
        )
        assert cleaned["persistent_session"] is True

    def test_validator_accepts_false(self):
        """bool False passes through cleanly."""
        cleaned = _validate_args(
            "cron_add",
            {"name": "x", "message": "y", "every": 120, "persistent_session": False},
        )
        assert cleaned["persistent_session"] is False

    def test_validator_rejects_non_bool_string(self):
        """LLM that sends the string "false" instead of False must be rejected."""
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError):
            _validate_args(
                "cron_add",
                {
                    "name": "x",
                    "message": "y",
                    "every": 120,
                    "persistent_session": "false",
                },
            )

    def test_validator_rejects_non_bool_int(self):
        """An int (even 0 or 1) is not a bool — reject so stored state stays typed."""
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError):
            _validate_args(
                "cron_add",
                {
                    "name": "x",
                    "message": "y",
                    "every": 120,
                    "persistent_session": 0,
                },
            )


class TestCronUpdatePersistentSession:
    """Tests for persistent_session on cron_update MCP tool (covers cron.py update_job + mcp_cron handler)."""

    def test_update_sets_persistent_session_false(self):
        """cron_update with persistent_session=False updates the job."""
        name = _unique_name()
        _call_tool_inner("cron_add", {"name": name, "message": "hi", "every": 120})
        svc = CronService()
        job = next(j for j in svc.list_jobs() if j.name == name)
        assert job.persistent_session is True

        _call_tool_inner("cron_update", {"job_id": job.id, "persistent_session": False})
        svc = CronService()
        job = next(j for j in svc.list_jobs() if j.name == name)
        assert job.persistent_session is False

    def test_update_sets_persistent_session_true(self):
        """cron_update can re-enable persistent_session."""
        name = _unique_name()
        _call_tool_inner(
            "cron_add", {"name": name, "message": "hi", "every": 120, "persistent_session": False}
        )
        svc = CronService()
        job = next(j for j in svc.list_jobs() if j.name == name)
        assert job.persistent_session is False

        _call_tool_inner("cron_update", {"job_id": job.id, "persistent_session": True})
        svc = CronService()
        job = next(j for j in svc.list_jobs() if j.name == name)
        assert job.persistent_session is True
