"""Pure-unit pieces of workflow failure handling: ceiling bounds + diagnostics.

Companion to ``test_workflows_resilience.py`` (which exercises the same behaviour
through a live run). Split out because this module is synchronous, matching the
repo convention of all-async or all-sync test modules.
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.registry import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_FINISHED,
    STATUS_RUNNING,
    RunHandle,
    RunRegistry,
)
from kiro_crew.workflows.runner import (
    DEFAULT_RUN_TIMEOUT_SECS,
    MAX_AGENT_ERROR_CHARS,
    MAX_RUN_TIMEOUT_SECS,
    MIN_RUN_TIMEOUT_SECS,
    clamp_run_timeout,
    describe_agent_error,
)

# --------------------------------------------------------------------------- #
# the ceiling is configurable but never removable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "given,expected",
    [
        (None, DEFAULT_RUN_TIMEOUT_SECS),  # no override → service/engine default
        (0, DEFAULT_RUN_TIMEOUT_SECS),  # 0 must NOT be read as "unlimited"
        (-1, DEFAULT_RUN_TIMEOUT_SECS),
        ("nonsense", DEFAULT_RUN_TIMEOUT_SECS),
        (None, DEFAULT_RUN_TIMEOUT_SECS),
        (1, MIN_RUN_TIMEOUT_SECS),  # floored: a run needs time to author itself
        (7200, 7200),  # a genuinely long investigation is allowed
        (99999999, MAX_RUN_TIMEOUT_SECS),  # but still bounded
    ],
)
def test_clamp_run_timeout(given, expected) -> None:
    assert clamp_run_timeout(given) == expected


def test_clamp_run_timeout_honors_a_service_default() -> None:
    """A deployment default applies when the caller supplies no per-run value."""
    assert clamp_run_timeout(None, default=1800) == 1800


def test_clamp_run_timeout_bounds_a_service_default_too() -> None:
    """Even a configured default cannot escape the bounds."""
    assert clamp_run_timeout(99999999, default=99999999) == MAX_RUN_TIMEOUT_SECS


# --------------------------------------------------------------------------- #
# failure diagnostics
# --------------------------------------------------------------------------- #


def test_describe_agent_error_names_the_type_and_message() -> None:
    assert describe_agent_error(TimeoutError("too slow")) == "TimeoutError: too slow"


def test_describe_agent_error_is_bounded() -> None:
    described = describe_agent_error(ValueError("y" * 10_000))
    assert len(described) <= MAX_AGENT_ERROR_CHARS + len("…")


def test_describe_agent_error_scrubs_recognized_credentials() -> None:
    """The reason is persisted to the run record and later surfaced over HTTP and
    into chat, so known credential formats are scrubbed at capture time.

    Only formats ``redact_credentials`` recognizes are caught (provider token
    shapes, cloud keys) — this is defense in depth on top of the egress-side
    redaction, not a guarantee about arbitrary secret-looking text.
    """
    token = "ghp_16C7e42F292c6912E7710c838347Ae178B4a"
    described = describe_agent_error(RuntimeError(f"push rejected using {token}"))
    assert described.startswith("RuntimeError: ")
    assert token not in described


def test_describe_agent_error_redacts_before_truncating() -> None:
    """A secret sitting past the truncation boundary must still be scrubbed.

    Truncating first slices the token in half, which destroys the pattern the
    redactors match on — and the surviving prefix is still real key material.
    """
    token = "ghp_16C7e42F292c6912E7710c838347Ae178B4a"
    # Push the token so it STRADDLES the cut: 20 chars of it land inside the
    # first MAX_AGENT_ERROR_CHARS, the rest past it.
    prefix_len = MAX_AGENT_ERROR_CHARS - len("RuntimeError: ") - 20
    described = describe_agent_error(RuntimeError("z" * prefix_len + token))
    assert token not in described
    assert token[:20] not in described


def test_describe_agent_error_never_includes_a_traceback() -> None:
    """Frames add bulk without adding diagnosis, and carry local paths."""
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        described = describe_agent_error(exc)
    assert "\n" not in described
    assert "File \"" not in described


# --------------------------------------------------------------------------- #
# per-agent diagnostics persist
# --------------------------------------------------------------------------- #


def test_registry_records_a_reason_even_when_none_is_supplied() -> None:
    """A failed call must never persist as a silent hole — that is exactly what
    made wf_000032's two failed agents unrecoverable."""
    registry = RunRegistry()
    registry.register(RunHandle(run_id="wf_r", name="r"))
    registry.record_agent_result("wf_r", 0, result=None, ok=False, error="")
    handle = registry.get("wf_r")
    assert handle is not None
    assert handle.agent_errors[0] == "agent call failed (no reason recorded)"


def test_record_agent_result_for_unknown_run_is_a_noop() -> None:
    RunRegistry().record_agent_result("nope", 0, result="x")  # must not raise


def test_record_agent_result_does_not_write_to_the_store_per_call() -> None:
    """Guard against reintroducing an O(N) synchronous write per agent call.

    ``store.save`` re-serializes and re-redacts the WHOLE run record on the
    gateway event loop, so a write per settled call would stall the loop once per
    call over a record that grows with every payload. Durability rides the write
    this module already performs (throttled ``record_event`` + the guaranteed
    ``mark_terminal`` flush), which is asserted below.
    """

    class _CountingStore:
        def __init__(self) -> None:
            self.saves = 0

        def save(self, run_id: str, obj: dict) -> None:
            self.saves += 1

        def delete(self, run_id: str) -> None:
            pass

        def load_all(self) -> list:
            return []

    store = _CountingStore()
    registry = RunRegistry(store=store)
    registry.register(RunHandle(run_id="wf_w", name="w"))
    after_register = store.saves

    for i in range(10):
        registry.record_agent_result("wf_w", i, result=f"payload-{i}")
    assert store.saves == after_register, "checkpointing must not write per call"

    # The payloads are on the handle, and the terminal flush puts them on disk.
    handle = registry.get("wf_w")
    assert handle is not None
    assert len(handle.agent_results) == 10
    registry.mark_terminal("wf_w", STATUS_FAILED, error="timeout")
    assert store.saves == after_register + 1


def test_agent_errors_round_trip_through_the_store_form() -> None:
    handle = RunHandle(run_id="wf_s", name="s")
    handle.agent_results[1] = "payload"
    handle.agent_errors[2] = "TimeoutError: slow"
    restored = RunHandle.from_store_json(handle.to_store_json())
    assert restored.agent_results == {1: "payload"}
    assert restored.agent_errors == {2: "TimeoutError: slow"}


def test_restored_record_without_agent_errors_still_loads() -> None:
    """Records written before this field existed must keep deserializing."""
    legacy = {
        "run_id": "wf_old",
        "name": "old",
        "status": "failed",
        "agent_results": {"0": "kept"},
    }
    restored = RunHandle.from_store_json(legacy)
    assert restored.agent_results == {0: "kept"}
    assert restored.agent_errors == {}


def test_redaction_covers_dict_keys_not_just_values() -> None:
    """A credential can arrive as a mapping KEY, because agent output is parsed
    straight into the structures these surfaces serve.

    Both the HTTP route and the workflow_result MCP tool walk the payload with a
    `_redact_obj` helper; a values-only walk let a credential-shaped key through
    into `result`, `events`, and (new here) `partial_results`.
    """
    from kiro_crew.dashboard.handlers.workflows import _redact_obj

    token = "ghp_16C7e42F292c6912E7710c838347Ae178B4a"
    payload = {
        "partial_results": {"0": {token: "value-side-ok"}},
        "events": [{"data": {token: token}}],
    }
    out = _redact_obj(payload)
    flat = repr(out)
    assert token not in flat, "credential survived redaction"
    # The structure is preserved, just scrubbed.
    assert "partial_results" in out and "events" in out


def test_partial_results_only_appear_when_a_run_ended_without_a_result() -> None:
    """Keyed on status, not `result is None`.

    `result is None` is also true for a run that FINISHED and legitimately
    returned None, and for a run still RUNNING — reporting partials for either
    tells the reader work was cut short when it wasn't, and re-sends every
    payload on every poll of a live run.
    """
    handle = RunHandle(run_id="wf_p", name="p")
    handle.agent_results[0] = "payload"

    # Still running: no result yet, but nothing was lost.
    handle.status = STATUS_RUNNING
    assert "partial_results" not in handle.snapshot(include_events=True)
    assert "partial_result_count" not in handle.snapshot(include_events=False)

    # Finished, and the script legitimately returned None — not an interruption.
    handle.status = STATUS_FINISHED
    assert "partial_results" not in handle.snapshot(include_events=True)
    assert "partial_result_count" not in handle.snapshot(include_events=False)

    # Finished with a real result: the synthesis covers it, don't double the payload.
    handle.result = {"synthesis": "done"}
    assert "partial_results" not in handle.snapshot(include_events=True)

    # Ended without a usable return value: partials are the only output.
    for terminal in (STATUS_FAILED, STATUS_CANCELLED):
        handle.status = terminal
        handle.result = None
        assert handle.snapshot(include_events=True)["partial_results"] == {"0": "payload"}
        assert handle.snapshot(include_events=False)["partial_result_count"] == 1


def test_snapshot_tolerates_a_non_integer_call_index() -> None:
    """A restored key that failed int-coercion must not break the sort."""
    handle = RunHandle(run_id="wf_mixed", name="m", status=STATUS_FAILED)
    handle.agent_results.update({1: "one", "weird": "two"})
    snap = handle.snapshot(include_events=True)
    assert snap["partial_results"] == {"1": "one", "weird": "two"}
