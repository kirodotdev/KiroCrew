"""``work_report`` summary over the cap is clamped and reported, not refused.

The caller is a model composing prose and cannot measure the field before it
calls, so a refusal is only ever discovered by violating it and costs a whole
round-trip to resend. The cap still binds and the overflow is cut rather than
rejected. Both halves of that are required for the cut to be honest:

* :func:`kiro_crew.validation.clamp_to_max_len` stamps the stored value, which is
  what the conductor reads;
* ``work_report``'s reply reads the stamp back with
  :func:`kiro_crew.validation.clamp_report`, which is the only way the WORKER can
  learn its text was cut -- the success frame carries no summary to read it off.

Scope: the clamp is on ``summary`` alone. ``artifacts``, ``pr`` and ``status``
are refused when wrong or oversized, because a truncated pointer is a broken
pointer while prose cut at the cap still reads. The end-to-end route and store
assertions live beside the rest of the route suite in
``test_work_ledger_tools.py``, which already owns that harness.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import mcp_work, validation
from kiro_crew import work_ledger as wl
from kiro_crew.validation import (
    WORK_REPORT_SCHEMA,
    ValidationError,
    clamp_report,
    clamp_to_max_len,
    validate_tool_args,
)

WORKER = "chat-clamp-worker"

#: The cap under test, read off the schema that enforces it.
CAP: int = next(f.max_len for f in WORK_REPORT_SCHEMA.fields if f.name == "summary")


# ── the cap still binds, and the boundary is untouched ────────────────────


def test_a_summary_exactly_at_the_cap_is_stored_verbatim():
    """The boundary value carries no stamp: only the overflow path clamps."""
    exact = "s" * CAP
    cleaned = validate_tool_args({"status": "progress", "summary": exact}, WORK_REPORT_SCHEMA)
    assert cleaned["summary"] == exact
    assert len(cleaned["summary"]) == CAP
    assert clamp_report(cleaned["summary"]) is None


def test_a_summary_one_over_the_cap_is_clamped_rather_than_refused():
    """One char over is a common overshoot and must not cost a retry."""
    cleaned = validate_tool_args(
        {"status": "progress", "summary": "x" * (CAP + 1)}, WORK_REPORT_SCHEMA
    )
    assert len(cleaned["summary"]) <= CAP
    assert clamp_report(cleaned["summary"]) is not None


@pytest.mark.parametrize("sent", [CAP + 1, CAP + 30, CAP + 804, CAP * 3])
def test_every_overshoot_in_the_measured_range_is_accepted_and_bounded(sent: int):
    """Real overshoots run from one char to several times the cap."""
    cleaned = validate_tool_args({"status": "progress", "summary": "y" * sent}, WORK_REPORT_SCHEMA)
    assert len(cleaned["summary"]) <= CAP
    reported = clamp_report(cleaned["summary"])
    assert reported is not None
    before, kept = reported
    assert before == sent
    assert 0 < kept < CAP


def test_a_required_empty_summary_is_still_refused():
    """Clamping widens what is accepted at the TOP of the range only."""
    with pytest.raises(ValidationError):
        validate_tool_args({"status": "progress", "summary": ""}, WORK_REPORT_SCHEMA)


# ── the stamp and its reader are one pair ─────────────────────────────────


@pytest.mark.parametrize("sent", [CAP + 1, CAP + 7, CAP + 500])
def test_clamp_report_reads_back_what_clamp_to_max_len_stamped(sent: int):
    """The reader is built from the stamp constant, so the two cannot drift."""
    stamped = clamp_to_max_len("z" * sent, CAP)
    assert len(stamped) <= CAP
    reported = clamp_report(stamped)
    assert reported is not None
    before, kept = reported
    assert before == sent
    # ``kept`` is the caller's own surviving text, so the stamp sits after it.
    assert stamped.startswith("z" * kept)
    assert kept + len(stamped[kept:]) == len(stamped)


def test_clamp_report_answers_none_for_a_value_nothing_clamped():
    """The common case: an in-budget summary is not described as truncated."""
    assert clamp_report("a short honest summary") is None
    assert clamp_report("") is None
    assert clamp_report("x" * CAP) is None


# ── the worker is told, which is what makes the cut honest ────────────────


def _stub_transport(monkeypatch, captured: dict[str, Any]) -> None:
    """Replace the loopback POST and the identity probe; the store has its own suite."""

    def _fake_post(path: str, payload: dict[str, Any], *, session_key: str) -> dict[str, Any]:
        captured.update(payload)
        return {"status": payload["status"], "item_id": "it_abcd1234"}

    monkeypatch.setattr(mcp_work, "_post", _fake_post)
    monkeypatch.setattr(mcp_work, "_strict_caller", lambda: (WORKER, ""))


def test_the_work_report_reply_names_both_lengths_when_the_summary_was_cut(monkeypatch):
    """The success frame carries no summary, so the reply is the worker's only signal."""
    sent = CAP + 30
    captured: dict[str, Any] = {}
    _stub_transport(monkeypatch, captured)

    cleaned = validate_tool_args({"status": "progress", "summary": "q" * sent}, WORK_REPORT_SCHEMA)
    reply = mcp_work._call_tool_inner("work_report", cleaned)

    assert "Recorded." in reply
    assert str(sent) in reply, reply
    assert str(CAP) in reply, reply
    # What reached the store is the clamped value, stamp and all.
    assert len(captured["summary"]) <= CAP
    assert clamp_report(captured["summary"]) is not None


def test_the_work_report_reply_stays_terse_when_nothing_was_cut(monkeypatch):
    """An in-budget report keeps the terse reply, so the note means something."""
    captured: dict[str, Any] = {}
    _stub_transport(monkeypatch, captured)

    cleaned = validate_tool_args({"status": "progress", "summary": "fits"}, WORK_REPORT_SCHEMA)
    reply = mcp_work._call_tool_inner("work_report", cleaned)

    assert reply == "Recorded. status=progress item=it_abcd1234"


def test_the_reply_cap_is_read_off_the_schema_that_enforces_it():
    """A second copy of the number could quote a cap the validator does not apply."""
    assert mcp_work.SUMMARY_CAP == CAP == wl.MAX_SUMMARY_CHARS


def test_the_published_description_promises_a_cut_rather_than_a_refusal():
    """The schema text is the contract a worker reads before it writes a summary."""
    spec = next(t for t in mcp_work._tool_definitions() if t["name"] == "work_report")
    described = spec["inputSchema"]["properties"]["summary"]["description"]
    assert "cut to the cap, not refused" in described
    assert "how much was dropped" in described
    assert str(CAP) in described


# ── the scope guard: siblings still refuse ────────────────────────────────


def test_summary_is_the_only_clamped_field_on_the_worker_schema():
    """A truncated pointer is a broken pointer, so only prose is clamped."""
    assert {f.name for f in WORK_REPORT_SCHEMA.fields if f.clamp_to_max} == {"summary"}


def test_an_oversized_artifacts_value_is_still_refused():
    """``artifacts`` carries pointers the conductor follows, so it keeps refusing."""
    with pytest.raises(ValidationError):
        validate_tool_args(
            {
                "status": "progress",
                "summary": "ok",
                "artifacts": {"k": "v" * (wl.MAX_ARTIFACT_VALUE_CHARS + 1)},
            },
            WORK_REPORT_SCHEMA,
        )


def test_the_conductor_side_caps_are_untouched():
    """Only the worker's prose field changes; the conductor's fields still refuse."""
    record = validation.WORK_LEDGER_RECORD_SCHEMA
    assert not any(f.clamp_to_max for f in record.fields)
    with pytest.raises(ValidationError):
        validate_tool_args({"action": "create", "title": "t" * (wl.MAX_TITLE_CHARS + 1)}, record)


def test_the_session_ledger_fields_still_refuse():
    """``event``/``next`` share the cause and have a cap that refuses, not clamps."""
    schema = validation.SESSION_LEDGER_RECORD_SCHEMA
    for name in ("event", "next"):
        spec = next(f for f in schema.fields if f.name == name)
        assert spec.clamp_to_max is False, name
        with pytest.raises(ValidationError):
            validate_tool_args({name: "e" * (spec.max_len + 1)}, schema)


def test_the_store_still_refuses_an_oversized_summary_of_its_own():
    """The store's cap is a backstop for a caller that bypasses the schema."""
    with pytest.raises(wl.WorkLedgerError):
        wl._require_text("x" * (wl.MAX_SUMMARY_CHARS + 1), wl.MAX_SUMMARY_CHARS, "summary")
