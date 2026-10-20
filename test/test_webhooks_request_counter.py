"""``kirocrew.webhook.requests`` over the WHOLE population it claims to count.

The series says it counts every inbound request the endpoint decides, and the
run-history ring is only part of that: ``api_hooks_agent`` turns a request away
in twenty-one places before any run exists to record. The branch that motivated
these tests is the per-source auth throttle -- a rotated credential produces a
few ``unauthorized`` runs, the throttle then trips, and if the throttled refusal
counted nothing the series would fall to zero while the integration was still
broken, which an alert reads as recovered.

Three layers:

* the fold and the enum -- refusal members exist, are disjoint from the recorded
  ones, and an unknown value lands on the remainder;
* ``count_request`` -- one increment, no store write, and no raise even when the
  recorder is broken, since a refusal path must answer its caller regardless;
* the endpoint -- each refusal branch driven through the real handler, asserting
  its outcome AND its unchanged HTTP answer, plus an AST sweep proving no branch
  in the function was missed. The sweep is the anti-regression: the defect this
  fixes was one forgotten branch among twenty-one, so "a human read them all" is
  not the bar.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import patch

import pytest
from webhooks_api_helpers import _payload, _req, wired_fixture  # noqa: F401

from kiro_crew import webhooks
from kiro_crew.dashboard.handlers import hooks as H
from kiro_crew.metrics import events as ev


class _CapturingRecorder:
    def __init__(self) -> None:
        self.counters: list = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "value": value, "attrs": dict(attrs or {})})

    def histogram(self, name, value, **kwargs) -> None:  # pragma: no cover - unused
        pass


@pytest.fixture
def rec():
    r = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


def _requests(rec):
    return [c for c in rec.counters if c["name"] == ev.WEBHOOK_REQUESTS]


def _one(rec):
    """The single request increment, asserting there is exactly one."""
    calls = _requests(rec)
    assert len(calls) == 1, f"expected exactly one increment, got {calls}"
    assert calls[0]["value"] == 1
    return calls[0]["attrs"]["outcome"]


# ---------------------------------------------------------------------------
# the enum and the fold
# ---------------------------------------------------------------------------


def test_refusal_members_are_not_recordable_outcomes():
    """A member that was both would let one request be counted twice: once by
    the endpoint's refusal and once by a run recorded under the same name."""
    assert webhooks.METRIC_REFUSAL_OUTCOMES & webhooks.VALID_OUTCOMES == frozenset()
    assert webhooks.METRIC_OUTCOME_OTHER not in webhooks.VALID_OUTCOMES


def test_the_metric_enum_is_every_half_and_nothing_else():
    """Widening either half without restating it in the metrics doc fails here."""
    assert webhooks.METRIC_OUTCOMES == (
        webhooks.VALID_OUTCOMES | webhooks.METRIC_REFUSAL_OUTCOMES | {webhooks.METRIC_OUTCOME_OTHER}
    )
    assert webhooks.METRIC_REFUSAL_OUTCOMES == {"throttled", "unavailable", "rejected_body"}


@pytest.mark.parametrize(
    "outcome",
    sorted(webhooks.VALID_OUTCOMES | webhooks.METRIC_REFUSAL_OUTCOMES),
)
def test_every_member_survives_the_fold(outcome):
    assert webhooks.metric_outcome(outcome) == outcome


@pytest.mark.parametrize("value", ["teleported", "", None, 7, object()])
def test_an_unknown_outcome_folds_to_other(value):
    assert webhooks.metric_outcome(value) == webhooks.METRIC_OUTCOME_OTHER


def test_count_request_is_the_only_emitter_of_the_series():
    """Both halves of the population go through one function, so they cannot
    drift apart in how they label or sanitise an outcome -- and no caller
    reaches ``emit_counter`` or the metric name behind its back."""
    import inspect

    record_src = inspect.getsource(webhooks.WebhookRunStore.record)
    assert "count_request" in record_src
    assert "emit_counter" not in record_src

    endpoint_src = inspect.getsource(H.api_hooks_agent)
    assert "count_request" in endpoint_src
    assert "emit_counter" not in endpoint_src
    assert "WEBHOOK_REQUESTS" not in endpoint_src


# ---------------------------------------------------------------------------
# count_request -- one increment, and nothing else
# ---------------------------------------------------------------------------


def test_count_request_adds_exactly_one(rec):
    webhooks.count_request(webhooks.METRIC_OUTCOME_THROTTLED)
    assert _one(rec) == "throttled"


def test_count_request_folds_an_unknown_outcome_rather_than_raising(rec):
    webhooks.count_request("not-a-member")
    assert _one(rec) == webhooks.METRIC_OUTCOME_OTHER


def test_count_request_records_nothing_to_the_run_store(rec, wired):  # noqa: F811
    """These branches exist because no run can be recorded -- the store may be
    the unreadable thing, or the throttle is refusing to do work at all."""
    webhooks.count_request(webhooks.METRIC_OUTCOME_UNAVAILABLE)
    assert webhooks.run_store().list_runs() == []
    assert not (wired / webhooks.RUNS_FILENAME).exists()


def test_count_request_never_raises_when_the_recorder_is_broken():
    """A refusal path must answer its caller even if telemetry is wedged."""
    with patch(
        "kiro_crew.metrics.provider.get_recorder", side_effect=RuntimeError("provider down")
    ):
        webhooks.count_request(webhooks.METRIC_OUTCOME_THROTTLED)


def test_nothing_is_recorded_with_telemetry_off():
    from kiro_crew.metrics.recorder import MetricsRecorder

    off = MetricsRecorder(None)
    assert off.enabled is False
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=off):
        webhooks.count_request(webhooks.METRIC_OUTCOME_THROTTLED)


# ---------------------------------------------------------------------------
# the endpoint's refusal branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_throttled_refusal_is_counted(rec, wired):  # noqa: F811
    """THE motivating branch. The throttle returns 429 without recording a run,
    so without its own member the series goes quiet on a broken integration."""
    source = "203.0.113.9"  # non-loopback: an external webhook caller
    for _ in range(webhooks._AUTH_FAIL_LIMIT):
        webhooks.record_auth_failure(source)
    # The throttle's own arithmetic is covered in test_webhooks_auth_surface;
    # what matters here is that the handler's throttled BRANCH counts. The
    # request's ``remote`` is a read-only cached property, so the source is
    # asserted through the predicate the handler actually calls.
    assert webhooks.auth_throttle_blocked(source) is True
    with patch.object(webhooks, "auth_throttle_blocked", return_value=True):
        resp = await H.api_hooks_agent(_req("POST", "/api/hooks/agent", {"message": "hi"}))

    assert _one(rec) == "throttled"
    assert resp.status == 429
    assert (await _payload(resp))["code"] == "auth_throttled"
    # The throttle exists to stop doing work, so it must not have written one.
    assert webhooks.run_store().list_runs() == []


@pytest.mark.asyncio
async def test_an_unreadable_store_refusal_is_counted(rec, wired):  # noqa: F811
    """The kill-switch read fails closed and deliberately records nothing --
    the store it would write to may be the unreadable one."""
    with patch.object(
        webhooks.WebhookTokenStore,
        "is_switch_on",
        side_effect=webhooks.WebhookStoreUnreadable("corrupt"),
    ):
        resp = await H.api_hooks_agent(_req("POST", "/api/hooks/agent", {"message": "hi"}))

    assert _one(rec) == "unavailable"
    assert resp.status == 503
    assert (await _payload(resp))["code"] == "webhooks_unavailable"
    assert webhooks.run_store().list_runs() == []


@pytest.mark.asyncio
async def test_a_rejected_body_refusal_is_counted(rec, wired):  # noqa: F811
    """A signed caller whose payload is the wrong type is refused on its body,
    after the run store has had nothing to say about it."""
    raw, secret, _entry = webhooks.token_store().create("Review Bot")
    req = _req(
        "POST",
        "/api/hooks/agent",
        {"message": 7},
        headers={"Authorization": f"Bearer {raw}"},
        sign_with=secret,
    )
    resp = await H.api_hooks_agent(req)

    assert _one(rec) == "rejected_body"
    assert resp.status == 400
    assert (await _payload(resp))["code"] == "message_not_a_string"


@pytest.mark.asyncio
async def test_a_recorded_refusal_is_counted_once_not_twice(rec, wired):  # noqa: F811
    """An invalid bearer DOES record a run, so it must count through ``record``
    alone -- a second increment beside its return would double-count it."""
    resp = await H.api_hooks_agent(
        _req(
            "POST",
            "/api/hooks/agent",
            {"message": "hi"},
            headers={"Authorization": "Bearer kc_whk_nope"},
        )
    )
    assert _one(rec) == "unauthorized"
    assert resp.status == 401
    assert [r["outcome"] for r in webhooks.run_store().list_runs()] == ["unauthorized"]


# ---------------------------------------------------------------------------
# completeness -- the guard against the defect recurring
# ---------------------------------------------------------------------------


def _endpoint_tree():
    path = pathlib.Path(H.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "api_hooks_agent"
    )


def _counter_lines(fn):
    """Lines in *fn* that increment the request series."""
    return sorted(
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "count_request"
    )


def _record_lines(fn):
    """Lines in *fn* that write a run, which counts for itself."""
    return sorted(
        n.lineno for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr == "record"
    )


def test_every_refusal_branch_is_counted():
    """Every way the endpoint can answer reaches the counter: directly through
    ``count_request``, or through the ``record`` call that counts for itself.
    The accepted path is the one permitted exception.

    Read off the function's own syntax tree rather than from a list kept here,
    because the defect this closes was ONE forgotten branch among twenty-one. A
    guarantee resting on a reviewer having checked them all reproduces it; this
    fails the moment a new branch answers without counting.
    """
    fn = _endpoint_tree()
    counters, records = _counter_lines(fn), _record_lines(fn)
    source = pathlib.Path(H.__file__).read_text(encoding="utf-8").splitlines()

    uncounted = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Call)):
            continue
        if getattr(node.value.func, "attr", None) != "json_response":
            continue
        # The increment sits immediately above its own return; a recorded
        # refusal counts inside ``record``, a few lines further up.
        if any(0 < node.lineno - line <= 3 for line in counters):
            continue
        if any(0 < node.lineno - line < 14 for line in records):
            continue
        uncounted.append(node.lineno)

    accepted = [ln for ln in uncounted if "accepted" in source[ln - 1]]
    assert len(accepted) == 1, "the accepted path should be the one uncounted answer"
    assert (
        uncounted == accepted
    ), "these refusal branches answer without reaching the counter: " + ", ".join(
        f"{ln}: {source[ln - 1].strip()[:60]}" for ln in uncounted if ln not in accepted
    )


def test_every_counted_refusal_names_a_declared_member():
    """No branch may invent an outcome string: the series key stays closed, and
    naming a constant rather than a literal keeps the enum in one place."""
    fn = _endpoint_tree()
    seen = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) != "count_request":
            continue
        (arg,) = node.args
        assert isinstance(arg, ast.Attribute), f"line {node.lineno}: outcome must be a constant"
        value = getattr(webhooks, arg.attr)
        assert value in webhooks.METRIC_OUTCOMES
        seen.append(value)

    assert len(seen) == 21, f"expected 21 counted refusals, found {len(seen)}"
    # Every refusal member is reached by some branch: a declared member no
    # branch emits would be a documented series that can never appear.
    assert set(seen) == webhooks.METRIC_REFUSAL_OUTCOMES | {webhooks.OUTCOME_UNAUTHORIZED}


def test_the_accepted_path_is_not_counted_at_admission():
    """An accepted request records its own terminal outcome when the run ends,
    so counting it at admission too would count that request twice."""
    fn = _endpoint_tree()
    source = pathlib.Path(H.__file__).read_text(encoding="utf-8").splitlines()
    accepted = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and '"accepted"' in source[node.lineno - 1]
    ]
    assert len(accepted) == 1, "the accepted response moved; re-point this guard"
    assert not any(0 < accepted[0] - line <= 3 for line in _counter_lines(fn))
