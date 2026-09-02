"""The cron failure-alert mechanism must have exactly ONE spelling.

Two call sites alert on a failed cron run -- the script/command helper
(``_alert_cron_failure``) and the message path's own ``except`` block. They
legitimately differ in control flow, in who owns ``record_failure()``, and in
wording. The mechanism underneath must not differ: the dedup window, the
Slack-sink hardening, and when the dedup anchor advances.

That used to rest on a docstring promising the two "cannot drift", which is prose
rather than a mechanism -- and it drifted once already, with the message path's DM
left saying only "check logs" while the helper carried the reason. Review caught
it, not a test. These tests are the mechanism: the shared helpers are unit-tested,
and a source scan pins that neither call site grows a second copy.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.slack.gateway import _FAILURE_REMINDER_SECS, GatewayOrchestrator


def _source() -> str:
    import kiro_crew.slack.gateway as mod

    return Path(mod.__file__).read_text(encoding="utf-8")


def _job(**kw) -> CronJob:
    job = CronJob(
        id="j1",
        name="nightly",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=60),
    )
    for key, value in kw.items():
        setattr(job, key, value)
    return job


# ── the shared dedup window ─────────────────────────────────────────────────


def test_duplicate_inside_the_reminder_window_is_suppressed() -> None:
    import time

    job = _job(last_failure_hash="abc", last_failure_at=time.time())

    assert GatewayOrchestrator._failure_alert_is_duplicate(object(), job, "abc") is True


def test_duplicate_past_the_reminder_window_alerts_again() -> None:
    """A still-failing job re-alerts once per window, not once per fire."""
    import time

    job = _job(
        last_failure_hash="abc",
        last_failure_at=time.time() - _FAILURE_REMINDER_SECS - 1,
    )

    assert GatewayOrchestrator._failure_alert_is_duplicate(object(), job, "abc") is False


def test_a_different_reason_is_never_a_duplicate() -> None:
    import time

    job = _job(last_failure_hash="abc", last_failure_at=time.time())

    assert GatewayOrchestrator._failure_alert_is_duplicate(object(), job, "xyz") is False


# ── the shared Slack-sink hardening ─────────────────────────────────────────


def test_slack_safe_escapes_entity_markup() -> None:
    """A job named `<!channel>` would page a whole channel on failure."""
    out = GatewayOrchestrator._slack_safe_fenced(object(), "<!channel> broke")

    assert "<!channel>" not in out


def test_slack_safe_neutralizes_a_code_fence() -> None:
    """Escaping alone does not stop a fence closing early and leaking markup."""
    out = GatewayOrchestrator._slack_safe_fenced(object(), "boom ``` *not italic*")

    assert "```" not in out
    assert "'''" in out


# ── the shared advance rule ─────────────────────────────────────────────────


def test_anchor_advances_when_the_bell_was_the_only_surface() -> None:
    """No channel resolved is a SKIP, not a failure, or a Slack-less install
    re-notifies the dashboard on every fire."""
    job = _job(last_failure_hash="", last_failure_at=0.0)

    GatewayOrchestrator._advance_failure_dedup(
        object(), job, "abc", channel_delivered=False, slack_failed=False
    )

    assert job.last_failure_hash == "abc"
    assert job.last_failure_at > 0


def test_anchor_advances_on_a_confirmed_channel_delivery_despite_slack() -> None:
    """The reason reached the user even though the Slack leg threw."""
    job = _job(last_failure_hash="", last_failure_at=0.0)

    GatewayOrchestrator._advance_failure_dedup(
        object(), job, "abc", channel_delivered=True, slack_failed=True
    )

    assert job.last_failure_hash == "abc"


def test_anchor_is_held_back_when_nothing_reached_anyone() -> None:
    """A real Slack exception with no channel leg means try again next fire."""
    job = _job(last_failure_hash="", last_failure_at=0.0)

    GatewayOrchestrator._advance_failure_dedup(
        object(), job, "abc", channel_delivered=False, slack_failed=True
    )

    assert job.last_failure_hash == ""
    assert job.last_failure_at == 0.0


# ── one spelling, pinned by construction ────────────────────────────────────


def test_the_dedup_window_has_one_spelling() -> None:
    """A second inline window check is how the two surfaces drift apart."""
    hits = re.findall(r"time\.time\(\)\s*-\s*job\.last_failure_at", _source())

    assert len(hits) == 1, (
        f"the failure dedup window is spelled {len(hits)} times; it belongs only "
        "in _failure_alert_is_duplicate"
    )


def test_the_fence_neutralizer_has_one_spelling() -> None:
    hits = re.findall(r'\.replace\(\s*"```"\s*,\s*"\'\'\'"\s*\)', _source())

    assert len(hits) == 1, (
        f"the Slack fence neutralizer is spelled {len(hits)} times; it belongs "
        "only in _slack_safe_fenced"
    )


def test_the_dedup_anchor_is_written_in_one_place() -> None:
    """Any surface that writes the anchor itself can suppress the next alert."""
    hits = re.findall(r"job\.last_failure_hash\s*=\s*", _source())

    assert len(hits) == 1, (
        f"job.last_failure_hash is assigned {len(hits)} times; it belongs only in "
        "_advance_failure_dedup"
    )


def test_the_failure_dm_is_opened_in_one_place() -> None:
    """Both alert surfaces must share the one-surface delivery rule.

    ``_open_dm_with_retry`` also serves the RESULT delivery leg, which is a
    different pipeline, so this counts the failure-alert legs by pinning that
    ``_deliver_failure_alert`` is the only failure-path caller.
    """
    src = _source()
    # The two call sites reach Slack only through the shared helper.
    assert src.count("await self._deliver_failure_alert(") == 2
    # And the helper is the only thing that posts a failure alert.
    assert src.count("_deliver_failure_alert") == 3  # def + two calls
