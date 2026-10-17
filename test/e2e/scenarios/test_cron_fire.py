"""Scenario: a cron created through the API actually fires and records a run.

The flow: create a scheduled job, have it execute, see the run in its history.
This is the whole point of the scheduler, and the failure it catches is the one
that reads as green everywhere else -- a job that is stored, listed and enabled
but never executes.

The pod boots with ``--crons`` so its scheduler is on. The fire itself is
triggered rather than waited out, because the smallest real interval is still
long enough to make the scenario a timeout risk, and the path under test is
``run_job`` either way.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.timeout(600)

# Wide enough that the SCHEDULER never fires this job on its own during the
# scenario: a second, concurrent run would 409 the trigger and make the history
# assertion ambiguous about which run it read.
EVERY_SECS = 86_400
FIRE_TIMEOUT = 240.0


def test_cron_created_via_api_fires_and_records_a_run(pod, wait_for) -> None:
    created = pod.api(
        "POST",
        "crons",
        {
            # An agent-message job, not a command or script one: POST /api/crons
            # takes `name` + `message` + a schedule and has no `command` /
            # `script` field at all, so the message kind is the only kind this
            # platform's API can create. The pod's gateway spawns the pinned fake
            # ACP backend, which makes the turn deterministic and offline.
            "name": "e2e-scenario-fire-once",
            "message": "Reply with the single word ready.",
            "every": EVERY_SECS,
            "hide_in_chat": True,
        },
    )
    assert isinstance(created, dict) and created.get("ok") is True, f"create refused: {created!r}"
    job_id = created.get("id")
    assert isinstance(job_id, str) and job_id, f"create returned no job id: {created!r}"

    listed = pod.api("GET", "crons")
    ids = {j.get("id") for j in (listed.get("jobs") or [])}
    assert job_id in ids, f"the created job is not in GET /api/crons: {sorted(str(i) for i in ids)}"

    run = pod.api("POST", f"crons/{job_id}/run", {})
    assert isinstance(run, dict) and run.get("ok") is True, f"trigger refused: {run!r}"

    def _history() -> dict:
        body = pod.api("GET", f"crons/{job_id}/history?limit=5")
        return body if isinstance(body, dict) else {}

    history = wait_for(FIRE_TIMEOUT, _history, lambda h: int(h.get("total") or 0) >= 1)
    total = int(history.get("total") or 0)
    assert total >= 1, (
        f"the cron never recorded a run within {FIRE_TIMEOUT:.0f}s "
        f"(history {history!r})\n{pod.logs()}"
    )

    runs = history.get("runs") or []
    assert runs, f"history reports total={total} but returned no rows: {history!r}"
    # A recorded run must say HOW it ended. A row with neither is the silent
    # half-write this scenario exists to catch: the scheduler noted an attempt
    # and nothing ever completed it.
    first = runs[0]
    assert any(
        first.get(k) for k in ("status", "summary", "error", "finished_at")
    ), f"the run record carries no outcome field: {first!r}"

    pod.api("DELETE", f"crons/{job_id}")
