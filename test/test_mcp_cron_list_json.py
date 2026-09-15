"""``cron_list`` JSON mode.

This mode exists because a sandboxed skill script can read neither thing it needs.
The job store is only sandbox-visible as a carve-out for ``mcp_cron`` itself, and
reading it directly bypasses the ownership filter, so a non-owner sharing one data
home would see every participant's job metadata. The run history is masked
outright, and on Linux the mask is an empty writable directory -- so a direct read
reports zero runs for every job and is indistinguishable from a job that has never
fired. Only the gateway can see it.

So the tests here pin three things:

* Ownership still scopes the payload. The JSON mode must not become a way around
  the filter the text modes apply.
* An unreadable history is reported as UNKNOWN, never as zero runs. That is the
  masked-directory failure mode, and reporting it as an empty tally would turn a
  missing read into a confident "this job does nothing".
* The payload bounds itself and says so. The response-level cap is a blind tail
  slice, which would hand a consumer an unparseable JSON document.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiro_crew import mcp_cron
from kiro_crew.cron import CronJob, CronSchedule


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:json-slot")
    yield


def _job(**over) -> CronJob:
    fields = {
        "id": "j1",
        "name": "poller",
        "message": "Check the timestamp.",
        "schedule": CronSchedule(kind="every", every_secs=3600),
        "session_key": "dashboard:json-slot",
    }
    fields.update(over)
    return CronJob(**fields)


def _call(jobs, stats=None, reason=""):
    """Run the tool with a fixed job list and a stubbed history fetch."""
    with (
        patch.object(mcp_cron.CronService, "list_jobs", return_value=jobs),
        patch.object(mcp_cron, "_fetch_history_stats", return_value=(stats or {}, reason)),
    ):
        return mcp_cron._call_tool_inner("cron_list", {"json": True})


class TestShape:
    def test_returns_parseable_json_with_one_record_per_job(self) -> None:
        out = json.loads(_call([_job(), _job(id="j2", name="other")]))
        assert out["scanned"] == 2
        assert [r["id"] for r in out["jobs"]] == ["j1", "j2"]
        assert out["truncated"] is False

    def test_a_record_carries_what_a_classifier_needs(self) -> None:
        out = json.loads(_call([_job(minimal_context=True, persistent_session=False)]))
        rec = out["jobs"][0]
        for key in (
            "id",
            "name",
            "mode",
            "enabled",
            "schedule",
            "every_secs",
            "minimal_context",
            "persistent_session",
            "hide_in_chat",
            "message",
            "history",
        ):
            assert key in rec, key
        assert rec["minimal_context"] is True
        assert rec["persistent_session"] is False
        assert rec["mode"] == "agent"

    def test_mode_names_the_dispatch_shape(self) -> None:
        out = json.loads(_call([_job(script="crons/f.py:run", message="")]))
        assert out["jobs"][0]["mode"] == "script"


class TestOwnership:
    def test_another_sessions_job_is_absent(self) -> None:
        mine = _job()
        theirs = _job(id="j9", name="not-mine", session_key="dashboard:other-slot")
        out = json.loads(_call([mine, theirs]))
        assert [r["id"] for r in out["jobs"]] == ["j1"]
        assert "not-mine" not in json.dumps(out)

    def test_an_ownerless_job_is_absent(self) -> None:
        out = json.loads(_call([_job(), _job(id="j8", name="cli-made", session_key="")]))
        assert [r["id"] for r in out["jobs"]] == ["j1"]

    def test_an_unidentified_caller_gets_the_scoped_empty_sentence_not_json(
        self, monkeypatch
    ) -> None:
        """Prose here is correct: the consumer must not read an empty scope as an
        empty registry, and the script relays the sentence verbatim."""
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        with patch.object(mcp_cron, "_authz_session_key", return_value=""):
            out = _call([_job()])
        assert out == mcp_cron._SCOPED_EMPTY
        assert not out.startswith("{")

    def test_an_empty_registry_still_says_so(self) -> None:
        assert _call([]) == "No cron jobs."


class TestHistory:
    def test_folded_counts_ride_along(self) -> None:
        stats = {
            "j1": {
                "runs": 8,
                "failures": 0,
                "distinct_summaries": 1,
                "noop_runs": 8,
                "same_every_run": True,
            }
        }
        out = json.loads(_call([_job()], stats=stats))
        assert out["history_available"] is True
        assert out["jobs"][0]["history"]["runs"] == 8
        assert out["jobs"][0]["history"]["same_every_run"] is True

    def test_an_unreadable_history_is_null_and_the_reason_is_stated(self) -> None:
        """The masked-directory case. Null, never an empty tally."""
        out = json.loads(_call([_job()], stats={}, reason="the gateway did not answer"))
        assert out["history_available"] is False
        assert out["history_unavailable_reason"] == "the gateway did not answer"
        assert out["jobs"][0]["history"] is None

    def test_a_partial_fetch_leaves_the_unfetched_jobs_null(self) -> None:
        stats = {
            "j1": {
                "runs": 3,
                "failures": 0,
                "distinct_summaries": 3,
                "noop_runs": 0,
                "same_every_run": False,
            }
        }
        out = json.loads(_call([_job(), _job(id="j2")], stats=stats, reason="ran out of time"))
        by_id = {r["id"]: r["history"] for r in out["jobs"]}
        assert by_id["j1"]["runs"] == 3
        assert by_id["j2"] is None
        assert out["history_available"] is False

    def test_no_reason_key_when_every_job_was_read(self) -> None:
        stats = {
            "j1": {
                "runs": 1,
                "failures": 0,
                "distinct_summaries": 1,
                "noop_runs": 0,
                "same_every_run": False,
            }
        }
        out = json.loads(_call([_job()], stats=stats))
        assert "history_unavailable_reason" not in out


class TestBoundsAndSafety:
    def test_the_payload_caps_itself_and_flags_it(self) -> None:
        """The response cap is a blind tail slice, so the payload must not rely on it."""
        jobs = [_job(id=f"j{i:03d}", name=f"job-{i}") for i in range(mcp_cron._JSON_MAX_JOBS + 20)]
        out = json.loads(_call(jobs))
        assert out["truncated"] is True
        assert out["scanned"] == mcp_cron._JSON_MAX_JOBS
        assert len(out["jobs"]) == mcp_cron._JSON_MAX_JOBS

    def test_it_stays_under_the_response_ceiling_on_ASCII_prompts(self) -> None:
        from kiro_crew.validation import MAX_RESPONSE_LEN

        jobs = [
            _job(id=f"j{i:03d}", name=f"job-{i}" * 5, message="Check the timestamp. " * 40)
            for i in range(mcp_cron._JSON_MAX_JOBS)
        ]
        assert len(_call(jobs)) < MAX_RESPONSE_LEN

    def test_it_stays_under_the_ceiling_when_json_escaping_triples_the_text(self) -> None:
        """The case a count cap cannot bound, and an ASCII test cannot see.

        json.dumps escapes a non-ASCII character to a six-character \\uXXXX, so a
        400-character Chinese prompt serializes to ~2400. Measured: 100 such jobs
        under the old count-only cap produced 296,810 characters, almost three
        times the ceiling -- and the ceiling truncates with a blind tail slice that
        appends a notice OUTSIDE the JSON grammar, so the consumer would receive a
        document that does not parse at all.
        """
        from kiro_crew.validation import MAX_RESPONSE_LEN

        jobs = [
            _job(id=f"j{i:03d}", name="任务" * 20, message="检查时间戳是否变化。" * 60)
            for i in range(mcp_cron._JSON_MAX_JOBS)
        ]
        out = _call(jobs)
        assert len(out) < MAX_RESPONSE_LEN
        payload = json.loads(out)  # the point: it still parses
        assert payload["truncated"] is True
        assert 0 < len(payload["jobs"]) < mcp_cron._JSON_MAX_JOBS

    def test_the_size_bound_keeps_at_least_one_record(self) -> None:
        """A single oversize job must still be reported, not silently dropped."""
        out = json.loads(_call([_job(message="検査" * 5000)]))
        assert len(out["jobs"]) == 1

    def test_dropping_for_size_is_reported_as_truncated(self) -> None:
        jobs = [_job(id=f"j{i:03d}", message="時刻の確認。" * 60) for i in range(60)]
        payload = json.loads(_call(jobs))
        assert payload["truncated"] is True
        assert payload["scanned"] == len(payload["jobs"])

    def test_a_credential_in_a_prompt_is_redacted_before_truncation(self) -> None:
        """Truncate-then-sanitize leaves a credential's prefix in the surviving span."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        out = _call([_job(message=f"Check {secret} and report the timestamp.")])
        assert secret not in out
        for n in (8, 12, 16):
            assert secret[:n] not in out

    def test_a_long_prompt_is_bounded_and_says_it_was_cut(self) -> None:
        """The flag is what lets a consumer refuse to judge half a prompt."""
        out = json.loads(_call([_job(message="x" * 5000)]))
        rec = out["jobs"][0]
        assert len(rec["message"]) <= mcp_cron._JSON_MESSAGE_LEN
        assert rec["message_truncated"] is True

    def test_a_short_prompt_is_not_flagged(self) -> None:
        out = json.loads(_call([_job(message="Check the timestamp.")]))
        assert out["jobs"][0]["message_truncated"] is False

    def test_the_flag_is_measured_after_redaction_not_before(self) -> None:
        """Redaction changes the length, so measuring the raw text would lie."""
        out = json.loads(_call([_job(message="Check it.")]))
        assert out["jobs"][0]["message_truncated"] is False


class TestTextModesUnchanged:
    """The JSON mode is additive: the two text shapes must be byte-identical."""

    def test_compact_is_still_the_default(self) -> None:
        with patch.object(mcp_cron.CronService, "list_jobs", return_value=[_job()]):
            out = mcp_cron._call_tool_inner("cron_list", {})
        assert out.startswith("1 cron job(s): 1 active, 0 paused\n")
        assert not out.startswith("{")

    def test_verbose_is_still_text(self) -> None:
        with patch.object(mcp_cron.CronService, "list_jobs", return_value=[_job()]):
            out = mcp_cron._call_tool_inner("cron_list", {"verbose": True})
        assert "• poller (✅ active) [agent]" in out

    def test_json_wins_over_the_verbose_that_ids_forces_on(self) -> None:
        """ids implies verbose; a machine consumer still needs a document."""
        out = _call([_job()])
        assert out.startswith("{")
        with (
            patch.object(mcp_cron.CronService, "list_jobs", return_value=[_job()]),
            patch.object(mcp_cron, "_fetch_history_stats", return_value=({}, "")),
        ):
            drilled = mcp_cron._call_tool_inner("cron_list", {"ids": ["j1"], "json": True})
        assert json.loads(drilled)["jobs"][0]["id"] == "j1"


class TestValidationSurface:
    def test_the_flag_survives_validation(self) -> None:
        assert mcp_cron._validate_args("cron_list", {"json": True})["json"] is True

    def test_a_non_bool_flag_is_rejected(self) -> None:
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError):
            mcp_cron._validate_args("cron_list", {"json": "yes"})

    def test_the_tool_advertises_the_flag(self) -> None:
        spec = next(t for t in mcp_cron._list_tools() if t["name"] == "cron_list")
        assert "json" in spec["inputSchema"]["properties"]
        assert "history_available" in spec["inputSchema"]["properties"]["json"]["description"]
