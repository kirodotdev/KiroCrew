"""The app-facing audit seam.

The properties worth pinning are the ones an app cannot check for itself: that
attribution is minted rather than passed, that a record never takes down the
operation it only describes, and that the outcome it states is the outcome stored.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.audit_sdk import AuditSDK
from kiro_crew.apps.context import build_app_context


class _Sink:
    """Captures what reached `sel().log_api_access`."""

    def __init__(self, boom: bool = False) -> None:
        self.calls: list[dict] = []
        self._boom = boom

    def log_api_access(self, **kw: object) -> None:
        if self._boom:
            raise OSError("sel unwritable")
        self.calls.append(dict(kw))


@pytest.fixture()
def sink(monkeypatch):
    s = _Sink()
    monkeypatch.setattr("kiro_crew.apps.audit_sdk.sel", lambda: s)
    return s


def test_record_attributes_the_event_to_the_app(sink) -> None:
    AuditSDK("doc-store").record("publish", "success", resources="proj~doc")
    (call,) = sink.calls
    # `app:<name>` matches cron_sdk's ownership tag, so one convention identifies
    # app-originated rows across both.
    assert call["caller"] == "app:doc-store"
    assert call["operation"] == "doc-store.publish"
    assert call["outcome"] == "success"
    assert call["source"] == "app"
    assert call["resources"] == "proj~doc"


def test_there_is_no_caller_parameter_to_get_wrong() -> None:
    # Attribution is minted from the context's app name, so the common failure --
    # a caller passing the wrong constant -- has nowhere to happen. It is
    # cooperative rather than unforgeable: hook code runs in-process and could
    # construct AuditSDK("other-app") or reach sel() directly, which no in-process
    # API can prevent. What this pins is that the accidental path is closed.
    import inspect

    params = inspect.signature(AuditSDK.record).parameters
    assert "caller" not in params
    assert "source" not in params


def test_operation_is_namespaced_so_two_apps_cannot_collide(sink) -> None:
    AuditSDK("alpha").record("publish", "success")
    AuditSDK("beta").record("publish", "success")
    assert [c["operation"] for c in sink.calls] == ["alpha.publish", "beta.publish"]


def test_record_never_raises_when_the_sink_fails(monkeypatch) -> None:
    # Auditing must not break the operation it only describes: an app that let this
    # raise would fail a user's publish because logging failed.
    monkeypatch.setattr("kiro_crew.apps.audit_sdk.sel", lambda: _Sink(boom=True))
    AuditSDK("doc-store").record("publish", "success")  # must not raise


@pytest.mark.parametrize(
    "outcome", ["success", "denied", "error", "ok", "completed", "rejected", "allowed"]
)
def test_a_real_outcome_reaches_the_log_unaltered(sink, outcome) -> None:
    # Not narrowed to a vocabulary: the gateway's own writers use `ok`, `completed`
    # and `rejected` among others, and nothing filters the log by outcome, so
    # constraining an app to a subset would make its rows less precise than its
    # siblings' -- and rewriting an unrecognised value would mislabel the fact
    # being recorded. Redaction is the identity function on all of these.
    AuditSDK("doc-store").record("op", outcome)
    assert sink.calls[-1]["outcome"] == outcome


def test_an_unrecognised_outcome_is_kept_rather_than_coerced(sink) -> None:
    # A bespoke spelling is still the app's statement of what happened. Rewriting
    # it to `error` would record a failure where the app reported a success.
    AuditSDK("doc-store").record("op", "partially-applied")
    assert sink.calls[-1]["outcome"] == "partially-applied"


def test_the_sdk_adds_no_scrubbing_of_its_own(sink) -> None:
    # Scrubbing `outcome` lives in `log_api_access`, which is the boundary EVERY
    # filler crosses -- see test_sel.py::test_log_api_access_redacts_and_clips_outcome.
    # Doing it here as well would make the seam depend on a private sibling symbol
    # and leave the same hole open for the next caller. So the SDK hands the value
    # over untouched, and this pins that placement rather than re-testing the scrub.
    AuditSDK("doc-store").record("op", "failed for AKIAIOSFODNN7EXAMPLE")
    assert sink.calls[-1]["outcome"] == "failed for AKIAIOSFODNN7EXAMPLE"


def test_every_app_gets_audit_with_no_permission_declared(tmp_path) -> None:
    ctx = build_app_context("doc-store", tmp_path, permissions={})
    assert isinstance(ctx.audit, AuditSDK)
    assert ctx.cron is None, "a capability SDK still requires its permission"
