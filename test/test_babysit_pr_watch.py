"""Tests for the babysit skill's zero-token PR watch script cron.

Pins the watch contract: silent (Skip) while nothing needs a brain, one wake
(Report) per anomaly per head, terminal Done on merge/close, per-head alert
reset on force-push, known-inherited reds filtered, gh failures quiet until
the consecutive-error alert, and hostile check names sanitized before they
reach a wake brief.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import ModuleType

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew import irq
from kiro_crew.cron_script import Done, Report, Skip

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "babysit"
    / "scripts"
    / "pr_watch.py"
)


class _Job:
    id = "job-e2e-1"


class _Ctx:
    def __init__(self, message: str) -> None:
        self.message = message
        self.job = _Job()


def _check(name: str, conclusion: str = "", status: str = "COMPLETED") -> dict:
    return {"name": name, "conclusion": conclusion, "status": status}


def _payload(
    checks: list[dict],
    *,
    state: str = "OPEN",
    merged_at: str | None = None,
    mergeable: str = "MERGEABLE",
    merge_state: str = "BLOCKED",
    head: str = "a" * 40,
) -> dict:
    return {
        "state": state,
        "mergedAt": merged_at,
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
        "headRefOid": head,
        "statusCheckRollup": checks,
    }


@pytest.fixture()
def module(monkeypatch, tmp_path) -> ModuleType:
    mod = load_skill_script("babysit_pr_watch", SCRIPT)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    return mod


def _wire(monkeypatch, module: ModuleType, payload: dict | None) -> None:
    def _fake_run_gh(args):
        if payload is None:
            return 1, ""
        return 0, json.dumps(payload)

    monkeypatch.setattr(module, "_run_gh", _fake_run_gh)


def _msg(**overrides) -> str:
    # coalesce_secs=0 pins the fire-on-first-anomaly contract this suite
    # was written against, which is still supported and is the documented
    # migration setting. The coalescing window has its own tests in
    # test/test_irq.py, plus the two probe-level cases at the end here.
    base = {"repo": "acme/widgets", "pr": 42, "coalesce_secs": 0}
    base.update(overrides)
    return json.dumps(base)


def _tick(module: ModuleType, message: str):
    return module.watch(_Ctx(message))


# ── terminal states ───────────────────────────────────────────────────────


def test_merged_pr_completes_the_watch(monkeypatch, module):
    _wire(monkeypatch, module, _payload([], merged_at="2026-08-23T00:00:00Z"))
    with pytest.raises(Done, match="MERGED"):
        _tick(module, _msg())


def test_closed_unmerged_completes_the_watch(monkeypatch, module):
    _wire(monkeypatch, module, _payload([], state="CLOSED"))
    with pytest.raises(Done, match="CLOSED"):
        _tick(module, _msg())


def test_invalid_message_is_terminal_not_a_retry_loop(monkeypatch, module):
    _wire(monkeypatch, module, _payload([]))
    with pytest.raises(Done, match="not valid JSON"):
        _tick(module, "{oops")
    with pytest.raises(Done, match="JSON object"):
        _tick(module, "[]")  # valid JSON, wrong shape — must not crash-loop
    with pytest.raises(Done, match="needs"):
        _tick(module, json.dumps({"repo": "no-slash", "pr": 1}))
    with pytest.raises(Done, match="needs"):
        _tick(module, json.dumps({"repo": "a/b", "pr": "not-an-int"}))


# ── quiet paths ───────────────────────────────────────────────────────────


def test_pending_checks_skip_silently(monkeypatch, module):
    _wire(
        monkeypatch,
        module,
        _payload([_check("CI", status="IN_PROGRESS"), _check("Lint", "SUCCESS")]),
    )
    with pytest.raises(Skip):
        _tick(module, _msg())


def test_known_inherited_red_never_wakes_while_others_pend(monkeypatch, module):
    checks = [_check("Frontend Tests (4)", "FAILURE"), _check("CI", status="QUEUED")]
    _wire(monkeypatch, module, _payload(checks))
    with pytest.raises(Skip):
        _tick(module, _msg(known_reds=["Frontend Tests (4)"]))


def test_cancelled_runs_are_noise_not_failures(monkeypatch, module):
    checks = [_check("GPT Review", "CANCELLED"), _check("CI", "SUCCESS")]
    _wire(monkeypatch, module, _payload(checks))
    # CANCELLED neither fails nor pends: with everything else green this is
    # the ready wake, not a new-red wake.
    with pytest.raises(Report, match="all checks green"):
        _tick(module, _msg())


# ── wake paths, deduped per head ──────────────────────────────────────────


def test_conflict_wakes_once_per_head(monkeypatch, module):
    payload = _payload([_check("CI", status="QUEUED")], mergeable="CONFLICTING")
    _wire(monkeypatch, module, payload)
    with pytest.raises(Report, match="merge conflict"):
        _tick(module, _msg())
    with pytest.raises(Skip):
        _tick(module, _msg())


def test_alert_rearms_after_the_dedupe_window(monkeypatch, module):
    """Dedupe is time-bounded, not a permanent acknowledgement: a delivery
    lost to a gateway failure must cost a bounded delay, never a permanently
    suppressed signal."""
    payload = _payload([_check("CI", status="QUEUED")], mergeable="CONFLICTING")
    _wire(monkeypatch, module, payload)
    t = [1_000_000.0]
    monkeypatch.setattr(irq.time, "time", lambda: t[0])
    with pytest.raises(Report):
        _tick(module, _msg())
    with pytest.raises(Skip):
        _tick(module, _msg())
    t[0] += irq.DEFAULT_REALERT_SECS + 1
    with pytest.raises(Report):  # condition persists -> re-delivered
        _tick(module, _msg())


def test_same_check_name_across_workflows_keeps_distinct_identity(monkeypatch, module):
    """Allowlisting one workflow's 'test' must not silence another
    workflow's failing 'test'."""
    checks = [
        dict(_check("test", "FAILURE"), workflowName="Alpha", startedAt="2026-08-23T10:00:00Z"),
        dict(_check("test", "FAILURE"), workflowName="Beta", startedAt="2026-08-23T10:00:00Z"),
    ]
    _wire(monkeypatch, module, _payload(checks))
    with pytest.raises(Report, match="Beta / test"):
        _tick(module, _msg(known_reds=["Alpha / test"]))


def test_new_red_wakes_and_names_the_check_then_goes_quiet(monkeypatch, module):
    checks = [_check("Backend Tests (3.12, 2)", "FAILURE"), _check("CI", "SUCCESS")]
    _wire(monkeypatch, module, _payload(checks))
    with pytest.raises(Report, match=r"Backend Tests \(3.12, 2\)"):
        _tick(module, _msg())
    with pytest.raises(Skip):
        _tick(module, _msg())


def test_second_distinct_red_wakes_again(monkeypatch, module):
    _wire(monkeypatch, module, _payload([_check("A", "FAILURE")]))
    with pytest.raises(Report):
        _tick(module, _msg())
    _wire(
        monkeypatch,
        module,
        _payload([_check("A", "FAILURE"), _check("B", "TIMED_OUT")]),
    )
    with pytest.raises(Report, match="B"):
        _tick(module, _msg())


def test_green_wakes_once_and_respects_known_red_filter(monkeypatch, module):
    checks = [_check("Frontend Tests (4)", "FAILURE"), _check("CI", "SUCCESS")]
    _wire(monkeypatch, module, _payload(checks))
    with pytest.raises(Report, match="review-ready"):
        _tick(module, _msg(known_reds=["Frontend Tests (4)"]))
    with pytest.raises(Skip):
        _tick(module, _msg(known_reds=["Frontend Tests (4)"]))


def test_wake_on_green_false_stays_quiet(monkeypatch, module):
    _wire(monkeypatch, module, _payload([_check("CI", "SUCCESS")]))
    with pytest.raises(Skip):
        _tick(module, _msg(wake_on_green=False))


def test_empty_rollup_never_reports_ready(monkeypatch, module):
    """A PR whose checks have not dispatched yet has an empty rollup — that
    is 'nothing ran', not 'everything passed'."""
    _wire(monkeypatch, module, _payload([]))
    with pytest.raises(Skip):
        _tick(module, _msg())


def test_force_push_resets_alert_memory(monkeypatch, module):
    _wire(
        monkeypatch,
        module,
        _payload([_check("A", "FAILURE")], head="a" * 40),
    )
    with pytest.raises(Report):
        _tick(module, _msg())
    _wire(
        monkeypatch,
        module,
        _payload([_check("A", "FAILURE")], head="b" * 40),
    )
    with pytest.raises(Report, match="A"):
        _tick(module, _msg())


def test_unknown_conclusion_vocabulary_wakes_a_brain(monkeypatch, module):
    _wire(monkeypatch, module, _payload([_check("Odd", "SOMETHING_NEW")]))
    with pytest.raises(Report, match="Odd"):
        _tick(module, _msg())


# ── watch health ──────────────────────────────────────────────────────────


def test_gh_failures_stay_quiet_then_alert_once(monkeypatch, module):
    _wire(monkeypatch, module, None)
    for _ in range(irq.DEFAULT_MAX_CONSECUTIVE_ERRORS - 1):
        with pytest.raises(Skip):
            _tick(module, _msg())
    with pytest.raises(Report, match="consecutive"):
        _tick(module, _msg())
    with pytest.raises(Skip):
        _tick(module, _msg())


def test_blind_alert_rearms_after_the_dedupe_window(monkeypatch, module):
    """A lost delivery of the threshold alert must cost a bounded delay, not
    the signal: with the count PAST the threshold (the state a swallowed
    delivery leaves behind), an expired dedupe window re-fires the alert."""
    _wire(monkeypatch, module, None)
    for _ in range(irq.DEFAULT_MAX_CONSECUTIVE_ERRORS - 1):
        with pytest.raises(Skip):
            _tick(module, _msg())
    with pytest.raises(Report, match="re-alert"):
        _tick(module, _msg())
    # Within the window: deduped even though errors keep climbing past the
    # threshold (the old == gate would have gone silent here forever).
    with pytest.raises(Skip, match="deduped"):
        _tick(module, _msg())
    # Expire the window: the alert re-arms while the condition persists.
    spath = irq.state_path("gh-pr", "acme/widgets#42", "job-e2e-1")
    st = json.loads(spath.read_text(encoding="utf-8"))
    st["alerted"]["blind"] -= irq.DEFAULT_REALERT_SECS + 1
    spath.write_text(json.dumps(st), encoding="utf-8")
    with pytest.raises(Report, match="re-alert"):
        _tick(module, _msg())


def test_recovery_clears_the_blind_marker_for_the_next_streak(monkeypatch, module):
    """A new failure streak after a recovery alerts promptly instead of
    inheriting the previous streak's dedupe window."""
    _wire(monkeypatch, module, None)
    for _ in range(irq.DEFAULT_MAX_CONSECUTIVE_ERRORS - 1):
        with pytest.raises(Skip):
            _tick(module, _msg())
    with pytest.raises(Report, match="re-alert"):
        _tick(module, _msg())
    _wire(monkeypatch, module, _payload([_check("CI", status="QUEUED")]))
    with pytest.raises(Skip):  # recovery tick resets streak + blind marker
        _tick(module, _msg())
    _wire(monkeypatch, module, None)
    for _ in range(irq.DEFAULT_MAX_CONSECUTIVE_ERRORS - 1):
        with pytest.raises(Skip):
            _tick(module, _msg())
    with pytest.raises(Report, match="re-alert"):  # new streak alerts promptly
        _tick(module, _msg())


def test_future_dedupe_timestamp_reads_as_stale_not_fresh_forever(monkeypatch, module):
    """A future timestamp in the alert memory (clock rollback, corrupt state)
    must not suppress alerts indefinitely: elapsed is bounded below by zero."""
    _wire(monkeypatch, module, _payload([_check("A", "FAILURE")]))
    with pytest.raises(Report):
        _tick(module, _msg())
    spath = irq.state_path("gh-pr", "acme/widgets#42", "job-e2e-1")
    st = json.loads(spath.read_text(encoding="utf-8"))
    for k in st.get("alerted", {}):
        st["alerted"][k] = time.time() + 10 * irq.DEFAULT_REALERT_SECS  # far future
    spath.write_text(json.dumps(st), encoding="utf-8")
    with pytest.raises(Report):  # future stamp = stale, alert fires again
        _tick(module, _msg())


def test_same_named_checks_from_different_apps_keep_distinct_identity(monkeypatch, module):
    """Two workflow-less app checks sharing a name must not collapse: app B's
    newer green would swallow app A's red into a false all-green. The stable
    detailsUrl prefix (host + first path segment) discriminates apps, while a
    rerun by the SAME app (same prefix, deeper run id) still collapses."""
    rows = [
        dict(
            _check("build", "FAILURE"),
            workflowName=None,
            detailsUrl="https://scanner.example/acme/runs/101",
            startedAt="2026-08-24T01:00:00Z",
        ),
        dict(
            _check("build", "SUCCESS"),
            workflowName=None,
            detailsUrl="https://coverage.example/acme/runs/202",
            startedAt="2026-08-24T02:00:00Z",
        ),
    ]
    _wire(monkeypatch, module, _payload(rows))
    with pytest.raises(Report, match="failing"):  # the red survives collapse
        _tick(module, _msg())
    # Same app, rerun green (same host/prefix, newer): DOES collapse to green.
    rows2 = [
        dict(
            _check("build", "FAILURE"),
            workflowName=None,
            detailsUrl="https://scanner.example/acme/runs/101",
            startedAt="2026-08-24T01:00:00Z",
        ),
        dict(
            _check("build", "SUCCESS"),
            workflowName=None,
            detailsUrl="https://scanner.example/acme/runs/303",
            startedAt="2026-08-24T03:00:00Z",
        ),
    ]
    _wire(monkeypatch, module, _payload(rows2))
    with pytest.raises(Report, match="green"):  # rerun green supersedes
        _tick(module, json.dumps({"repo": "acme/widgets", "pr": 43, "coalesce_secs": 0}))


def test_gh_recovery_resets_the_error_streak(monkeypatch, module):
    _wire(monkeypatch, module, None)
    with pytest.raises(Skip):
        _tick(module, _msg())
    _wire(monkeypatch, module, _payload([_check("CI", status="QUEUED")]))
    with pytest.raises(Skip):
        _tick(module, _msg())
    _wire(monkeypatch, module, None)
    # Streak restarted at 1, not continuing from 2.
    with pytest.raises(Skip):
        _tick(module, _msg())


# ── hygiene ───────────────────────────────────────────────────────────────


def test_hostile_check_names_are_sanitized_in_the_brief(monkeypatch, module):
    evil = "Evil\ncheck\x1b[31m<script>"
    _wire(monkeypatch, module, _payload([_check(evil, "FAILURE")]))
    with pytest.raises(Report) as exc:
        _tick(module, _msg())
    text = str(exc.value)
    assert "\x1b" not in text
    assert "<script>" not in text
    assert "Evil" in text


def test_state_survives_corrupt_state_file(monkeypatch, module, tmp_path):
    _wire(monkeypatch, module, _payload([_check("A", "FAILURE")]))
    with pytest.raises(Report):
        _tick(module, _msg())
    spath = irq.state_path("gh-pr", "acme/widgets#42", "job-e2e-1")
    spath.write_text("{broken", encoding="utf-8")
    # Corrupt state reads as fresh: the red alerts again rather than crashing.
    with pytest.raises(Report):
        _tick(module, _msg())
    # A valid-JSON state whose integer literal exceeds CPython's int-str
    # conversion limit raises a BARE ValueError from json.loads (not
    # JSONDecodeError); deep nesting raises RecursionError. Both must read
    # as fresh state, never crash the tick into the auto-pause path.
    spath.write_text('{"errors": ' + "9" * 5000 + "}", encoding="utf-8")
    with pytest.raises(Report):
        _tick(module, _msg())
    spath.write_text("[" * 10000 + "]" * 10000, encoding="utf-8")
    with pytest.raises(Report):
        _tick(module, _msg())


def test_malformed_state_field_types_read_as_fresh(monkeypatch, module):
    _wire(monkeypatch, module, _payload([_check("A", "FAILURE")]))
    with pytest.raises(Report):
        _tick(module, _msg())
    spath = irq.state_path("gh-pr", "acme/widgets#42", "job-e2e-1")
    spath.write_text(json.dumps({"head": 7, "alerted": "yes", "errors": "x"}), encoding="utf-8")
    with pytest.raises(Report):  # wrong types coerce to fresh, never crash
        _tick(module, _msg())


def test_huge_or_nonfinite_timestamps_drop_entry_not_crash(monkeypatch, module):
    """json.loads yields arbitrary-precision ints (float() -> OverflowError)
    and accepts Infinity/NaN literals; both must drop the one bad entry --
    a duplicate wake -- never crash the tick, and never poison siblings."""
    _wire(monkeypatch, module, _payload([_check("A", "FAILURE")]))
    with pytest.raises(Report):
        _tick(module, _msg())
    spath = irq.state_path("gh-pr", "acme/widgets#42", "job-e2e-1")
    huge = int("9" * 4001)
    spath.write_text(
        '{"alerted": {"bad-huge": %d, "bad-nan": NaN, "bad-inf": Infinity, "good": 1.0}}' % huge,
        encoding="utf-8",
    )
    state = irq.load_state(spath)
    assert state["alerted"] == {"good": 1.0}  # bad entries dropped, sibling kept
    with pytest.raises(Report):  # and the tick still runs (re-alert, no crash)
        _tick(module, _msg())


def test_malformed_known_reds_parameter_is_terminal(monkeypatch, module):
    _wire(monkeypatch, module, _payload([]))
    with pytest.raises(Done, match="known_reds"):
        _tick(module, _msg(known_reds=1))


def test_boolean_and_nonpositive_pr_numbers_are_terminal(monkeypatch, module):
    _wire(monkeypatch, module, _payload([]))
    with pytest.raises(Done, match="positive int"):
        _tick(module, _msg(pr=True))  # bool passes isinstance(int) checks
    with pytest.raises(Done, match="positive int"):
        _tick(module, _msg(pr=0))
    # A host segment in `repo` is refused by the repo guard, which carries its
    # own message: enterprise hosts come from the operator's trusted gh config
    # (GH_HOST), never from cron-message data.
    with pytest.raises(Done, match="owner/name"):
        _tick(module, json.dumps({"repo": "host/owner/name", "pr": 1}))
    with pytest.raises(Done, match="owner/name"):
        _tick(module, json.dumps({"repo": "ghe.corp.example/o/r", "pr": 1}))


def test_queued_rerun_without_timestamp_blocks_false_ready(monkeypatch, module):
    """A just-queued rerun row has no startedAt; it must not lose to the
    older green row and produce a false all-checks-green wake."""
    checks = [
        dict(_check("CI", "SUCCESS"), startedAt="2026-08-23T10:00:00Z", workflowName="CI"),
        dict(_check("CI", status="QUEUED", conclusion=""), startedAt="", workflowName="CI"),
    ]
    _wire(monkeypatch, module, _payload(checks))
    with pytest.raises(Skip):  # pending, not ready
        _tick(module, _msg())


def test_double_failure_alerts_immediately(monkeypatch, module):
    """gh failing AND state unwritable: the counted threshold can never fire,
    so the watch says it is inoperative on the first tick."""
    _wire(monkeypatch, module, None)
    monkeypatch.setattr(irq, "save_state", lambda *a, **k: False)
    with pytest.raises(Report, match="inoperative"):
        _tick(module, _msg())


def test_two_watches_on_one_pr_keep_independent_state(monkeypatch, module):
    """One session's alert must not suppress the other's delivery."""
    _wire(monkeypatch, module, _payload([_check("A", "FAILURE")]))
    with pytest.raises(Report):
        _tick(module, _msg())

    class OtherJob:
        id = "job-other-2"

    class OtherCtx:
        message = _msg()
        job = OtherJob()

    with pytest.raises(Report):  # second watch alerts independently
        module.watch(OtherCtx())


def test_unwritable_state_degrades_to_repeats_not_removal(monkeypatch, module):
    """An unwritable state dir must not remove the watch (later signals would
    be lost) and must not silence it: the alert still fires, carrying the
    degraded-dedupe warning, and repeats on the next tick."""
    _wire(monkeypatch, module, _payload([_check("A", "FAILURE")]))
    monkeypatch.setattr(irq, "save_state", lambda *a, **k: False)
    with pytest.raises(Report, match="unwritable"):
        _tick(module, _msg())
    with pytest.raises(Report):  # duplicate wake, never a lost signal
        _tick(module, _msg())


def test_rerun_green_supersedes_stale_red_row_of_same_name(monkeypatch, module):
    checks = [
        dict(_check("CI", "FAILURE"), startedAt="2026-08-23T10:00:00Z", workflowName="CI"),
        dict(_check("CI", "SUCCESS"), startedAt="2026-08-23T11:00:00Z", workflowName="CI"),
    ]
    _wire(monkeypatch, module, _payload(checks))
    # The stale red row must not wake; with the rerun green this is ready.
    with pytest.raises(Report, match="all checks green"):
        _tick(module, _msg())


def test_rerun_red_supersedes_stale_green_row_of_same_name(monkeypatch, module):
    """Recency arbitrates BOTH directions: an older success must not mask a
    newer failing rerun into a false-ready."""
    checks = [
        dict(_check("CI", "SUCCESS"), startedAt="2026-08-23T10:00:00Z", workflowName="CI"),
        dict(_check("CI", "FAILURE"), startedAt="2026-08-23T11:00:00Z", workflowName="CI"),
    ]
    _wire(monkeypatch, module, _payload(checks))
    with pytest.raises(Report, match="new failing check"):
        _tick(module, _msg())


# -- coalescing, at probe level (default window ON) ------------------------


def _msg_coalescing(**overrides) -> str:
    base = {"repo": "acme/widgets", "pr": 42, "coalesce_secs": 0.01}
    base.update(overrides)
    return json.dumps(base)


def test_default_window_holds_a_red_while_checks_still_run(monkeypatch, module):
    """With coalescing on, a red arriving while checks are pending does not
    wake: the turn it would schedule could not decide anything yet."""
    _wire(
        monkeypatch,
        module,
        _payload([_check("lint", "FAILURE"), _check("unit", "", "IN_PROGRESS")]),
    )
    with pytest.raises(Skip):
        _tick(module, _msg_coalescing())


def test_staggered_reds_arrive_as_one_wake(monkeypatch, module):
    """Two reds landing on one head minutes apart must produce ONE wake, not
    one each -- they are fixed by a single edit and a single push."""
    _wire(
        monkeypatch,
        module,
        _payload([_check("lint", "FAILURE"), _check("unit", "", "IN_PROGRESS")]),
    )
    with pytest.raises(Skip):
        _tick(module, _msg_coalescing())

    time.sleep(0.05)
    _wire(
        monkeypatch,
        module,
        _payload([_check("lint", "FAILURE"), _check("unit", "FAILURE")]),
    )
    with pytest.raises(Report) as caught:
        _tick(module, _msg_coalescing())
    body = str(caught.value)
    assert "lint" in body and "unit" in body


def test_conflict_is_an_nmi_and_ignores_the_window(monkeypatch, module):
    """A dirty PR dispatches no checks, so pending never drains and waiting
    observes nothing: the conflict must fire despite an open window."""
    _wire(
        monkeypatch,
        module,
        _payload(
            [_check("unit", "", "IN_PROGRESS")],
            mergeable="CONFLICTING",
            merge_state="DIRTY",
        ),
    )
    with pytest.raises(Report, match="CONFLICTING"):
        _tick(module, _msg_coalescing(coalesce_secs=9999))


def test_nonfinite_window_is_terminal_not_a_crash_loop(monkeypatch, module):
    """json.loads turns 1e309 into inf; an infinite window would raise
    OverflowError every tick, and a cron that raises every tick is auto-paused
    -- the watch would die silently from a config typo."""
    _wire(monkeypatch, module, _payload([]))
    with pytest.raises(Done, match="finite"):
        _tick(module, '{"repo": "acme/widgets", "pr": 42, "coalesce_secs": 1e309}')


def test_oversized_json_integer_is_terminal_not_a_crash_loop(monkeypatch, module):
    """CPython raises a BARE ValueError past the int-str conversion limit (~4300
    digits), which is not a JSONDecodeError. It does NOT escape: the kernel's
    identity() wrapper converts every ValueError into Done, so the watch removes
    itself with a reason instead of raising on every tick.

    This pins the mechanism a review round claimed was broken.
    """
    _wire(monkeypatch, module, _payload([]))
    huge = "9" * 5000
    with pytest.raises(Done):
        _tick(module, '{"repo": "acme/widgets", "pr": ' + huge + "}")


def test_deeply_nested_message_is_terminal_not_a_crash_loop(monkeypatch, module):
    """The fourth hostile shape for one field: json.loads blows the interpreter
    stack on deeply nested input and raises RecursionError, which is not a
    JSONDecodeError -- so it would escape identity() uncaught rather than
    becoming a Done, crashing every tick and auto-pausing the watch."""
    _wire(monkeypatch, module, _payload([]))
    nested = "[" * 20000 + "]" * 20000
    with pytest.raises(Done, match="valid JSON"):
        _tick(module, nested)


def test_deeply_nested_gh_response_reads_as_unobservable(monkeypatch, module):
    """A pathologically nested API response must read as 'could not observe',
    which feeds the error backstop, rather than raise out of the tick."""

    def _nested_run_gh(args):
        return 0, "[" * 20000 + "]" * 20000

    monkeypatch.setattr(module, "_run_gh", _nested_run_gh)
    with pytest.raises(Skip):
        _tick(module, _msg())


def test_oversized_window_integer_is_terminal_not_a_crash_loop(monkeypatch, module):
    """The third hostile shape json.loads produces for one field: an
    arbitrary-precision int. float() on it raises OverflowError, which is not a
    ValueError and so would escape identity() uncaught rather than becoming a
    Done -- crashing every tick and auto-pausing the watch."""
    _wire(monkeypatch, module, _payload([]))
    huge = "1" + "0" * 400
    with pytest.raises(Done, match="too large"):
        _tick(module, '{"repo": "acme/widgets", "pr": 42, "coalesce_secs": ' + huge + "}")


def test_known_reds_match_the_bare_name_operators_actually_write(monkeypatch, module):
    """`known_reds` is written by hand from what GitHub's UI shows -- the BARE
    check name -- while the dedupe identity is workflow-qualified. Matching
    only the qualified spelling would suppress nothing, wake on every
    inherited red, and never let `ready` fire.

    `wake_on_green` is off so the assertion isolates the SUPPRESSION: with it
    on, a fully-filtered rollup correctly reports review-ready, which would
    mask whether the bare name matched at all.
    """
    checks = [
        {
            "name": "Frontend Tests (4)",
            "workflowName": "CI",
            "conclusion": "FAILURE",
            "status": "COMPLETED",
        }
    ]
    _wire(monkeypatch, module, _payload(checks))
    with pytest.raises(Skip):
        _tick(module, _msg(known_reds=["Frontend Tests (4)"], wake_on_green=False))


def test_unfiltered_qualified_red_still_wakes(monkeypatch, module):
    """The mirror of the case above: a red NOT in the allow-list must wake, and
    the brief names it in its qualified spelling so two workflows sharing a
    check name stay distinguishable."""
    checks = [
        {
            "name": "Frontend Tests (4)",
            "workflowName": "CI",
            "conclusion": "FAILURE",
            "status": "COMPLETED",
        }
    ]
    _wire(monkeypatch, module, _payload(checks))
    with pytest.raises(Report, match="CI / Frontend Tests"):
        _tick(module, _msg(known_reds=["something else"]))
