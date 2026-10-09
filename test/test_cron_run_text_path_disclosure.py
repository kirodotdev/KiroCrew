"""A run's outcome text must not carry more than its own field allows to a
non-owner dashboard token.

``last_error`` is deliberately READABLE past the owner boundary — for the
three project-bound skips it is the only diagnosis there is, and those skips
spend no auto-pause strike, so nothing else escalates. What it must not carry
is the folder itself. The credential/exfiltration passes every sibling
serialized field takes do not strip a local filesystem path, and one writer
composes one: the fail-closed macOS voice-runtime spawn guard raises a
``RuntimeError`` naming the agent's workspace, which for a project-bound job
IS ``job.project_path``, and the LLM fire path stores ``str(exc)`` verbatim.
So the folder the project-path owner gate exists to keep from a non-owner
rode out through this field (CWE-209).

``last_result`` (and the history rows built from it, ``summary``/``trace``)
takes a DIFFERENT pass: it is the agent's own reply, composed while the agent
ran with the bound project directory as its cwd, so it can quote arbitrary
content read from a private repository — a path-strip alone leaves that
content on the wire. For a non-owner on a project-bound job (``job.project_path``
truthy — the same boundary the field's own owner gate already uses) it is
withheld outright rather than merely stripped.

One string per field, four routes: the list serializer plus the three history
endpoints, whose rows are BUILT from ``last_error``/``last_result``. Each is
pinned here, for both readers — the owner's bytes must not change at all, or
a script job's traceback loses its most useful line to a fix aimed at someone
else, and a NON-project-bound job's `last_result` must not change either, or
the fence is over-broad.
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
    # An unconfigured MagicMock attribute is truthy, so an unset stamp would
    # make every job here read as holding a project-bound result.
    job.last_result_project_bound = False
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
    # api_cron_history / api_cron_history_detail look the job up by id (for
    # `last_result`'s project-bound withholding); list/get_all already carry
    # a job in `runs`/`list_jobs`. Defaults to None (unbound) so a caller that
    # doesn't pass `job` gets the pre-existing path-strip-only behavior.
    state.crons.get_job.return_value = job
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
    async def test_last_result_is_withheld_outright_for_a_non_owner_on_a_project_bound_job(self):
        """Different pass from `last_error`: the agent's own reply can quote
        arbitrary content read from the bound directory, not just name it, so
        a path-strip alone still leaks that content — it must be withheld."""
        reply = "Updated /Users/alice/projects/repo/src/app.py and ran the tests."
        request = _request(_job(last_status="ok", last_result=reply))
        with _as(False):
            non_owner = json.loads((await api_crons(request)).body)["jobs"][0]
        with _as(True):
            owner = json.loads((await api_crons(request)).body)["jobs"][0]
        assert non_owner["last_result"] is None
        assert owner["last_result"] == reply

    @pytest.mark.asyncio
    async def test_last_result_is_unchanged_for_a_non_owner_on_a_non_project_bound_job(self):
        """MANDATORY negative pin: the withholding is scoped to a project-bound
        job. A job with no `project_path` must see the SAME path-strip-only
        pass it always has — the fence must not be over-broad."""
        reply = "Wrote /tmp/scratch/notes.txt and summarized the run."
        # A never-bound job that RAN has a row stamped unbound. With retained
        # output and NO row the gate cannot tell and withholds, so the pin has to
        # describe a real unbound run rather than an absent history.
        request = _request(
            _job(last_status="ok", last_result=reply, project_path=""),
            runs=[{"project_bound": False}],
        )
        with _as(False):
            non_owner = json.loads((await api_crons(request)).body)["jobs"][0]
        with _as(True):
            owner = json.loads((await api_crons(request)).body)["jobs"][0]
        assert "/tmp/scratch" not in non_owner["last_result"]
        assert "[redacted-path]" in non_owner["last_result"]
        assert non_owner["last_result"]  # present, just path-stripped, not withheld
        assert owner["last_result"] == reply

    @pytest.mark.asyncio
    async def test_last_result_withheld_after_owner_clears_binding_of_a_bound_run(self):
        """THE FINDING (GPT 5.6): the list serializer decided withholding from
        the LIVE field. Owner binds a job, it fires, then the owner CLEARS
        ``project_path`` while the reply stays in ``last_result``. The retained
        reply — composed inside the once-private directory — must still be
        withheld from a non-owner, decided from the stamp that travelled with
        the text.

        The history row here deliberately says ``project_bound=False`` -- what a
        later result-less run would write -- so this also pins that the row is
        not the source.
        """
        reply = "Read /Users/alice/projects/repo/config.py and applied the patch."
        # The retained result was produced bound; the live job has since been
        # unbound, and a later row describes only that later run.
        runs = [{"run_id": "r1", "status": "success", "summary": reply, "project_bound": False}]
        request = _request(
            _job(
                last_status="ok",
                last_result=reply,
                project_path="",
                last_result_project_bound=True,
            ),
            runs=runs,
        )
        with _as(False):
            non_owner = json.loads((await api_crons(request)).body)["jobs"][0]
        with _as(True):
            owner = json.loads((await api_crons(request)).body)["jobs"][0]
        assert non_owner["last_result"] is None
        assert owner["last_result"] == reply

    @pytest.mark.asyncio
    async def test_last_result_stays_readable_when_unbound_run_row_says_not_bound(self):
        """Companion negative pin for the EITHER/OR decision: an unbound live
        job whose newest retained run also stamped ``project_bound=False`` must
        NOT be withheld -- neither source claims a binding, so the fence must
        not close. Guards against the precompute defaulting to withhold.
        """
        reply = "Wrote /tmp/scratch/out.txt and summarized."
        runs = [{"run_id": "r1", "status": "success", "summary": reply, "project_bound": False}]
        request = _request(_job(last_status="ok", last_result=reply, project_path=""), runs=runs)
        with _as(False):
            non_owner = json.loads((await api_crons(request)).body)["jobs"][0]
        assert "/tmp/scratch" not in non_owner["last_result"]
        assert non_owner["last_result"]  # present, path-stripped, not withheld


class TestTheHistoryRoutes:
    """`error=terminal.last_error` and the reaper/cancel `summary=`/`error=`
    writes put the same string in a history row, and these three routes carry
    no owner gate of their own. `summary`/`trace` also carry `run_result`
    verbatim on a successful run — the agent's own reply, same risk class as
    `last_result` in the list serializer — so they take the withhold pass on
    a project-bound job while `error` keeps the path-strip-only pass."""

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

    # The three routes' withheld-on-bound / unchanged-on-non-bound control
    # cases are IDENTICAL in shape (only the handler, request-building kwarg,
    # and whether `trace` is a field on the row differ) -- each one is also
    # separately proven over the real persisted-provenance stamp (through the
    # genuine CronService/CronRunRecord/HTTP stack) by
    # TestHistoryRoutesWithholdOnPersistedProvenance in
    # test_cron_project_bound_history_provenance.py. Parametrized here rather
    # than duplicated per route so the mocked-handler-logic mechanism (as
    # opposed to that file's persisted-stamp mechanism) still proves `trace`
    # on run-detail and unified-history, which the sibling file does not
    # exercise.
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,has_trace,request_kwargs,row_getter",
        [
            (
                api_cron_history,
                False,
                lambda row: {
                    "job": _job(project_path="/Users/alice/projects/repo"),
                    "runs": [row],
                },
                lambda body: body["runs"][0],
            ),
            (
                api_cron_history_detail,
                True,
                lambda row: {
                    "job": _job(project_path="/Users/alice/projects/repo"),
                    "detail": row,
                },
                lambda body: body,
            ),
            (
                api_cron_history_all,
                True,
                lambda row: {
                    "job": _job(project_path="/Users/alice/projects/repo"),
                    "runs": [row],
                },
                lambda body: body["runs"][0],
            ),
        ],
        ids=["paginated_history", "run_detail", "unified_history"],
    )
    async def test_summary_and_trace_are_withheld_on_a_project_bound_job(
        self, handler, has_trace, request_kwargs, row_getter
    ):
        reply = "Read /Users/alice/projects/repo/notes.md before replying."
        row = {"run_id": "r1", "status": "success", "summary": reply, "error": ""}
        if handler is api_cron_history_all:
            row["job_id"] = "j1"
        if has_trace:
            row["trace"] = reply
        # Provenance lives on the ROW, not the live job: the route withholds
        # on what the fire stamped, so a row must say so.
        row["project_bound"] = True
        request = _request(**request_kwargs(row))
        with _as(False):
            non_owner = row_getter(json.loads((await handler(request)).body))
        with _as(True):
            owner = row_getter(json.loads((await handler(request)).body))
        assert non_owner["summary"] == ""
        assert owner["summary"] == reply
        if has_trace:
            assert non_owner["trace"] == ""
            assert owner["trace"] == reply

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,has_trace,request_kwargs,row_getter",
        [
            (
                api_cron_history,
                False,
                lambda row: {"job": _job(project_path=""), "runs": [row]},
                lambda body: body["runs"][0],
            ),
            (
                api_cron_history_detail,
                True,
                lambda row: {"job": _job(project_path=""), "detail": row},
                lambda body: body,
            ),
            (
                api_cron_history_all,
                True,
                lambda row: {"job": _job(project_path=""), "runs": [row]},
                lambda body: body["runs"][0],
            ),
        ],
        ids=["paginated_history", "run_detail", "unified_history"],
    )
    async def test_summary_and_trace_are_unchanged_on_a_non_project_bound_job(
        self, handler, has_trace, request_kwargs, row_getter
    ):
        """MANDATORY negative pin, all three history routes: a row stamped
        ``project_bound=False`` -- the fire ran outside a project directory --
        must not have its ``summary``/``trace`` withheld, only path-stripped."""
        reply = "Wrote /tmp/scratch/out.txt."
        row = {"run_id": "r1", "status": "success", "summary": reply, "error": ""}
        if handler is api_cron_history_all:
            row["job_id"] = "j1"
        if has_trace:
            row["trace"] = reply
        # Never bound — the fire stamped this row as running outside a
        # project directory, so the withhold pass must not touch it.
        row["project_bound"] = False
        request = _request(**request_kwargs(row))
        with _as(False):
            non_owner = row_getter(json.loads((await handler(request)).body))
        assert "/tmp/scratch" not in non_owner["summary"]
        assert non_owner["summary"]
        if has_trace:
            assert "/tmp/scratch" not in non_owner["trace"]
            assert non_owner["trace"]

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
