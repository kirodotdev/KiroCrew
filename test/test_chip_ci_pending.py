"""The ``ciPending`` chip marker: a FAILED rollup whose checks are still running.

Failure wins the CI rollup, so ``ci`` alone reads ``failed`` both while other
checks are in flight and after every check finished. ``ciPending`` tells the
two apart; these tests pin that both writers project it identically, that a
degraded read carries it with ``ci``, and that it reaches the wire allowlists.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard import state as dashboard_state
from kiro_crew.dashboard import ws_event_scope
from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.dashboard.source_providers import chip_status, projection

PR_URL = "https://github.com/acme/repo/pull/12"


@pytest.fixture(autouse=True)
def _isolated_chip_cache(_floor_monkeypatch):
    """Fresh module caches, and no detached visibility task, for every test.

    Patched through the floor's own ``MonkeyPatch`` so a test that calls
    ``monkeypatch.undo()`` cannot lift this isolation mid-test.
    """
    mp = _floor_monkeypatch
    mp.setattr(chip_status, "_check_cache", {})
    mp.setattr(chip_status, "_check_flap", {})
    mp.setattr(chip_status, "_check_flap_damped", set())
    mp.setattr(chip_status, "_status_delta_sinks", [])
    mp.setattr(chip_status, "schedule_visibility_refresh", lambda *a, **k: None)


@pytest.mark.parametrize(
    ("buckets", "expected"),
    [
        (["failed", "pending", "passed"], {"ci": "failed", "ciPending": "1"}),
        (["failed", "passed"], {"ci": "failed"}),
        (["pending", "passed"], {"ci": "running"}),
        (["passed", "skipped"], {"ci": "passed"}),
        ([], {}),
    ],
)
def test_record_ci_marks_only_a_failed_rollup_with_checks_in_flight(buckets, expected) -> None:
    result: dict[str, str] = {}
    projection._record_ci(result, [{"bucket": bucket} for bucket in buckets])
    assert result == expected


def test_full_payload_projects_the_marker_like_the_chip_read() -> None:
    chip: dict[str, str] = {}
    projection._record_ci(chip, [{"bucket": "failed"}, {"bucket": "pending"}])

    projected = chip_status.status_from_full_payload(
        {
            "provider": "github",
            "state": "OPEN",
            "checks": [{"bucket": "failed"}, {"bucket": "pending"}],
        }
    )

    assert projected == {**chip, "state": "open"}


@pytest.mark.parametrize("provider", ["acme", ""])
def test_full_payload_of_another_provider_never_gets_the_marker(provider) -> None:
    """A plugin's chip read is ``ci`` and ``state`` only, so its write-through
    must not add a marker its next chip refresh would read as a change."""
    projected = chip_status.status_from_full_payload(
        {
            "provider": provider,
            "state": "open",
            "checks": [{"bucket": "failed"}, {"bucket": "pending"}],
        }
    )

    assert projected == {"ci": "failed", "state": "open"}


def test_the_marker_providers_are_github_bitbucket_server_and_gerrit() -> None:
    assert projection.CI_PENDING_PROVIDERS == {"github", "bitbucket_server", "gerrit"}


def test_gitlab_aggregate_never_gets_the_marker() -> None:
    """GitLab's pipeline aggregate is authoritative and already says ``running``
    while jobs are in flight, so its per-job buckets must not add a marker."""
    projected = chip_status.status_from_full_payload(
        {
            "state": "opened",
            "ciStatus": "failed",
            "checks": [{"bucket": "failed"}, {"bucket": "pending"}],
        }
    )

    assert projected == {"ci": "failed", "state": "open"}


def _rollup_reply(rows: list[dict], head: str = "abc123") -> dict:
    """A one-page `gh api graphql` rollup reply carrying *rows* at *head*."""
    nodes = [{"__typename": "CheckRun", **row} for row in rows]
    contexts = {
        "totalCount": len(nodes),
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": nodes,
    }
    commit = {"oid": head, "statusCheckRollup": {"contexts": contexts}}
    pull = {"headRefOid": head, "commits": {"nodes": [{"commit": commit}]}}
    return {"data": {"repository": {"pullRequest": pull}}}


@pytest.mark.asyncio
async def test_github_chip_read_marks_failed_rollup_still_running(monkeypatch) -> None:
    async def fake_run(*argv: str, **_kwargs: int):
        if "statusCheckRollup" in " ".join(argv):
            return _rollup_reply(
                [
                    {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
                    {"name": "test", "status": "IN_PROGRESS", "conclusion": None},
                ]
            )
        return {"state": "OPEN", "headRefOid": "abc123"}

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status(PR_URL)

    assert status == {"ci": "failed", "ciPending": "1", "state": "open"}


def test_carry_known_ci_moves_the_marker_with_ci() -> None:
    previous = {"ci": "failed", "ciPending": "1", "state": "open"}

    assert chip_status.carry_known_ci({"state": "open"}, previous) == previous
    assert chip_status.carry_known_ci({"state": "open"}, {"ci": "failed"}) == {
        "ci": "failed",
        "state": "open",
    }


def test_partial_full_payload_keeps_the_known_marker() -> None:
    """A degraded write-through that kept ``failed`` but dropped the marker would
    tell the sidebar the run had finished."""
    chip_status._check_cache[PR_URL] = (0.0, {"ci": "failed", "ciPending": "1", "state": "open"})

    chip_status.record_full_payload_status(
        PR_URL, {"state": "OPEN", "checks": [], "partialSections": ["checks"]}
    )

    assert chip_status._check_cache[PR_URL][1] == {
        "ci": "failed",
        "ciPending": "1",
        "state": "open",
    }


def test_marker_is_on_both_wire_allowlists() -> None:
    """Projected onto source links for dashboard users, and stripped for app tokens."""
    assert "ciPending" in dashboard_state._CHIP_STATUS_KEYS
    assert set(dashboard_state._CHIP_STATUS_KEYS) == set(ws_event_scope._SOURCE_LINK_STATUS_KEYS)
