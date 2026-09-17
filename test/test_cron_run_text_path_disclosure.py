"""A run's error text must not carry a host path to a non-owner dashboard token.

``last_error`` is deliberately READABLE past the owner boundary — for the three
project-bound skips it is the only diagnosis there is, and those skips spend no
auto-pause strike, so nothing else escalates. What it must not carry is the
folder itself. The credential/exfiltration passes every sibling serialized field
takes do not strip a local filesystem path, and one writer composes one: the
fail-closed macOS voice-runtime spawn guard raises a ``RuntimeError`` naming the
agent's workspace, which for a project-bound job IS ``job.project_path``, and the
LLM fire path stores ``str(exc)`` verbatim. So the folder the project-path owner
gate exists to keep from a non-owner rode out through this field (CWE-209).

One string, four routes: the list serializer plus the three history endpoints,
whose rows are BUILT from ``last_error``. Each is pinned here, for both readers —
the owner's bytes must not change at all, or a script job's traceback loses its
most useful line to a fix aimed at someone else.
"""

import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import CronSchedule
from kiro_crew.dashboard.handlers.cron import (
    api_cron_history,
    api_cron_history_all,
    api_cron_history_detail,
    api_crons,
)

# The exact shape the spawn guard emits: `RuntimeError(f"... {workspace!r} ...")`
# where the workspace is the job's bound project directory.
GUARD_ERROR = (
    "macOS agent workspace '/Users/alice/projects/repo' overlaps Kiro Crew's "
    "protected voice runtime '/Users/alice/.kiro/crew/run/voice-runtime': the "
    "workspace contains the voice runtime / data home. Pick a project "
    "subdirectory that does not contain the Kiro Crew data home."
)

# A path-free skip reason: the member-shadow refusal. Must survive verbatim for
# BOTH readers — it is the whole signal a skipped job leaves.
SKIP_REASON = (
    "This job is bound to a Crew Member, and its project directory defines an "
    "agent with the same name ('default'). Rename the project's agent or "
    "unbind the member."
)


def _job(**overrides):
    """A fully-stubbed job: the list payload serializes fields RAW, so any
    attribute it reads and this factory does not name reaches json as a
    MagicMock and raises."""
    job = MagicMock()
    job.id = "j1"
    job.name = "nightly"
    job.message = "msg"
    job.enabled = True
    job.user_paused = False
    job.created_by = ""
    job.created_ts = None
    job.persistent_session = True
    job.minimal_context = False
    job.last_status = "error"
    job.last_error = ""
    job.last_result = ""
    job.last_run_ts = None
    job.last_retry_count = 0
    job.last_retry_run_ts = 0.0
    job.agent_id = ""
    job.member_id = ""
    job.memory_store = ""
    job.model = ""
    job.channel = None
    job.approval_mode = ""
    job.silent = False
    job.strict_schedule = False
    job.hide_in_chat = False
    job.schedule = CronSchedule(kind="every", every_secs=300)
    job.timezone = ""
    job.skip_dates = []
    job.script = ""
    job.command = ""
    job.secret_env = {}
    job.secret_env_pending = {}
    job.secret_env_pending_ts = 0.0
    job.folder_id = ""
    job.chat_folder_id = ""
    job.session_key = ""
    job.source_preset = ""
    job.source_template_prompt = ""
    job.project_path = "/Users/alice/projects/repo"
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


def _request(job=None, *, runs=None, detail=None):
    state = MagicMock()
    state.has_slot.return_value = False
    state.crons.list_jobs.return_value = [job] if job else []
    state.crons.list_jobs_async = AsyncMock(return_value=[job] if job else [])
    state.crons.running_since.return_value = None
    state.crons.is_running.return_value = False
    history = MagicMock()
    # Deep-copied per call: the handlers redact history rows IN PLACE (the store
    # hands them a freshly-decoded dict), so a shared fixture would let the
    # non-owner read mutate the rows the owner read then asserts on.
    history.get_job_history = AsyncMock(
        side_effect=lambda *a, **k: (copy.deepcopy(list(runs or [])), len(runs or []))
    )
    history.get_all_history = AsyncMock(
        side_effect=lambda *a, **k: (copy.deepcopy(list(runs or [])), len(runs or []))
    )
    history.get_run_detail = AsyncMock(
        side_effect=lambda *a, **k: copy.deepcopy(detail) if detail else None
    )
    state.crons.get_history.return_value = history
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"job_id": "j1", "run_id": "r1"}
    request.query = {}
    return request


def _as(owner: bool):
    return patch(
        "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
        lambda request: owner,
    )


class TestTheListSerializer:
    @pytest.mark.asyncio
    async def test_a_non_owner_read_strips_the_bound_folder_from_last_error(self):
        request = _request(_job(last_error=GUARD_ERROR))
        with _as(False):
            body = json.loads((await api_crons(request)).body)
        text = body["jobs"][0]["last_error"]
        assert "/Users/alice/projects/repo" not in text
        assert "/Users/alice/.kiro/crew" not in text
        assert "[redacted-path]" in text
        # The diagnosis and its remedy survive: the point is to strip the path,
        # not to withhold the reason from the reader whose job failed.
        assert "overlaps Kiro Crew's protected voice runtime" in text
        assert "Pick a project subdirectory" in text
        # And the field is NOT owner-gated away, which would reverse the
        # recorded decision that a skip's reason stays readable.
        assert body["jobs"][0]["last_error"]

    @pytest.mark.asyncio
    async def test_an_owner_read_keeps_the_error_verbatim(self):
        request = _request(_job(last_error=GUARD_ERROR))
        with _as(True):
            body = json.loads((await api_crons(request)).body)
        assert body["jobs"][0]["last_error"] == GUARD_ERROR

    @pytest.mark.asyncio
    async def test_a_script_jobs_traceback_keeps_its_paths_for_the_owner(self):
        """The owner's debugging surface must not pay for the non-owner fix."""
        trace = 'File "/home/alice/repo/x.py", line 3\nValueError: boom'
        request = _request(_job(script="~/.kiro/crew/crons/x.py:run", last_error=trace))
        with _as(True):
            body = json.loads((await api_crons(request)).body)
        assert body["jobs"][0]["last_error"] == trace

    @pytest.mark.asyncio
    async def test_a_path_free_skip_reason_is_untouched_for_both_readers(self):
        request = _request(_job(last_error=SKIP_REASON))
        with _as(False):
            non_owner = json.loads((await api_crons(request)).body)["jobs"][0]
        with _as(True):
            owner = json.loads((await api_crons(request)).body)["jobs"][0]
        assert non_owner["last_error"] == SKIP_REASON
        assert owner["last_error"] == SKIP_REASON

    @pytest.mark.asyncio
    async def test_last_result_takes_the_same_pass(self):
        """Same dict, same ungated boundary: the agent runs WITH the bound
        folder as its cwd, so its own reply names that folder routinely."""
        reply = "Updated /Users/alice/projects/repo/src/app.py and ran the tests."
        request = _request(_job(last_status="ok", last_result=reply))
        with _as(False):
            non_owner = json.loads((await api_crons(request)).body)["jobs"][0]
        with _as(True):
            owner = json.loads((await api_crons(request)).body)["jobs"][0]
        assert "/Users/alice/projects/repo" not in non_owner["last_result"]
        assert "[redacted-path]" in non_owner["last_result"]
        assert owner["last_result"] == reply


class TestTheHistoryRoutes:
    """`error=terminal.last_error` and the reaper/cancel `summary=`/`error=`
    writes put the same string in a history row, and these three routes carry
    no owner gate of their own."""

    @pytest.mark.asyncio
    async def test_paginated_history_strips_for_a_non_owner_and_not_for_an_owner(self):
        runs = [{"run_id": "r1", "status": "error", "summary": GUARD_ERROR, "error": GUARD_ERROR}]
        with _as(False):
            body = json.loads((await api_cron_history(_request(runs=runs))).body)
        row = body["runs"][0]
        assert "/Users/alice/projects/repo" not in row["error"]
        assert "[redacted-path]" in row["summary"]
        with _as(True):
            owner_row = json.loads((await api_cron_history(_request(runs=runs))).body)["runs"][0]
        assert owner_row["error"] == GUARD_ERROR

    @pytest.mark.asyncio
    async def test_run_detail_strips_summary_trace_and_error_for_a_non_owner(self):
        detail = {
            "run_id": "r1",
            "summary": GUARD_ERROR,
            "error": GUARD_ERROR,
            "trace": "cwd /Users/alice/projects/repo",
        }
        with _as(False):
            body = json.loads((await api_cron_history_detail(_request(detail=detail))).body)
        assert "/Users/alice/projects/repo" not in body["error"]
        assert "/Users/alice/projects/repo" not in body["trace"]
        assert "[redacted-path]" in body["summary"]
        with _as(True):
            owner = json.loads((await api_cron_history_detail(_request(detail=detail))).body)
        assert owner["error"] == GUARD_ERROR
        assert owner["trace"] == "cwd /Users/alice/projects/repo"

    @pytest.mark.asyncio
    async def test_unified_history_strips_for_a_non_owner(self):
        runs = [
            {
                "job_id": "j1",
                "run_id": "r1",
                "summary": GUARD_ERROR,
                "error": GUARD_ERROR,
                "trace": "",
            }
        ]
        request = _request(_job(), runs=runs)
        with _as(False):
            row = json.loads((await api_cron_history_all(request)).body)["runs"][0]
        assert "/Users/alice/projects/repo" not in row["error"]
        assert "[redacted-path]" in row["error"]
        # The enrichment still happens; only the outcome text takes the pass.
        assert row["job_name"] == "nightly"

    @pytest.mark.asyncio
    async def test_unified_history_keeps_the_error_verbatim_for_an_owner(self):
        runs = [
            {
                "job_id": "j1",
                "run_id": "r1",
                "summary": GUARD_ERROR,
                "error": GUARD_ERROR,
                "trace": "",
            }
        ]
        request = _request(_job(), runs=runs)
        with _as(True):
            row = json.loads((await api_cron_history_all(request)).body)["runs"][0]
        assert row["error"] == GUARD_ERROR
