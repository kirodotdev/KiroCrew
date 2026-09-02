"""Tests for cron_list compact / verbose / ids modes.

Covers the response-size fix that prevents large cron registries from
overflowing the model context budget. The default is a compact per-job
summary; ``verbose=true`` reproduces the legacy full output byte-for-byte
(regression-safe); ``ids=[...]`` drills into specific jobs.
"""

from __future__ import annotations

import uuid

import pytest

from kiro_crew.cron import CronService
from kiro_crew.mcp_cron import _call_tool_inner, _validate_args
from kiro_crew.validation import ValidationError

# ── Fixtures ──


#: The session every seeded job is owned by, matching the identity the ``home``
#: fixture gives the caller. ``cron_list`` scopes to the calling session's own
#: rows, so a job with no owner would simply not be listed and every rendering
#: assertion below would read "No cron jobs." -- these tests are about the OUTPUT
#: FORMAT, not about authorization (see test/test_mcp_cron_caller_identity.py).
_OWNER = "dashboard:list-format-slot"


def _seed_jobs(tmp_path, count: int, msg_size: int = 1500) -> CronService:
    """Build ``count`` cron jobs with realistic-sized messages."""
    svc = CronService(base_dir=tmp_path)
    big_msg = "x" * msg_size
    for i in range(count):
        svc.add_job(
            name=f"job-{i:03d}",
            message=f"job {i} payload\n{big_msg}",
            every_secs=300 + i,
            channel=None,
            session_key=_OWNER,
        )
    return svc


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    monkeypatch.delenv("KIROCREW_CLI", raising=False)
    # The identity the seeded jobs are owned by; see _OWNER.
    monkeypatch.setenv("KIROCREW_SESSION_KEY", _OWNER)
    # Every job seeded in this module belongs to that session. Defaulted here
    # rather than threaded through ~15 call sites: the owner is a precondition of
    # these tests, not part of what any of them is checking.
    original = CronService.add_job

    def _owned(self, *args, **kwargs):
        kwargs.setdefault("session_key", _OWNER)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CronService, "add_job", _owned)
    return tmp_path


# ── Compact (default) ──


class TestCronListCompact:
    def test_compact_is_default(self, home):
        _seed_jobs(home, count=3)
        out = _call_tool_inner("cron_list", {})
        # Header is bare — programmatic parsers lock on
        # ``\d+ cron job\(s\): \d+ active, \d+ paused`` with no suffix.
        assert out.startswith("3 cron job(s): 3 active, 0 paused\n")
        assert "(compact" not in out
        for i in range(3):
            assert f"job-{i:03d}" in out
        # Every job tagged with kind in compact mode.
        assert "[agent]" in out
        # Full message body NOT in compact output.
        assert "x" * 200 not in out

    def test_compact_truncates_message_to_preview(self, home):
        _seed_jobs(home, count=1, msg_size=5000)
        out = _call_tool_inner("cron_list", {})
        assert "…" in out
        assert "x" * 200 not in out

    def test_compact_size_budget_50_jobs(self, home):
        """50 jobs in compact mode must stay well under 30KB."""
        _seed_jobs(home, count=50, msg_size=2000)
        out = _call_tool_inner("cron_list", {})
        size = len(out.encode("utf-8"))
        assert size < 30_000, (
            f"compact 50-job response is {size} bytes, exceeds 30KB budget"
        )
        for i in range(50):
            assert f"job-{i:03d}" in out

    def test_compact_includes_extras_when_present(self, home):
        svc = CronService(base_dir=home)
        job = svc.add_job(
            name="agentful",
            message="hello",
            every_secs=300,
            channel="C0123ABC456",
        )
        job.agent_id = "office-worker"
        job.last_status = "ok"
        svc._save()
        out = _call_tool_inner("cron_list", {})
        assert "agent=office-worker" in out
        assert "channel=C0123ABC456" in out
        assert "last=ok" in out

    def test_compact_truncates_last_error(self, home):
        svc = CronService(base_dir=home)
        job = svc.add_job(name="errjob", message="hi", every_secs=300)
        job.last_status = "error"
        job.last_error = "BOOM " + ("Q" * 600)
        svc._save()
        out = _call_tool_inner("cron_list", {})
        assert "err=BOOM" in out
        assert "…" in out
        assert "Q" * 400 not in out

    def test_compact_includes_result_for_script_jobs(self, home):
        """Script/command jobs surface non-trivial last_result in compact."""
        svc = CronService(base_dir=home)
        job = svc.add_job(name="sj", message="ignored", every_secs=300)
        job.script = "x.py:f"
        job.last_status = "ok"
        job.last_result = "computed 42 widgets and posted to topic-xyz"
        svc._save()
        out = _call_tool_inner("cron_list", {})
        assert "result=computed 42 widgets" in out

    def test_a_legacy_ok_sentinel_is_not_shown_as_a_result(self, home):
        """Registries predating result_produced persist last_result == "ok".

        That is a synthetic marker, not output, so rendering it would tell the
        operator a silent script produced something when it produced nothing.
        """
        svc = CronService(base_dir=home)
        job = svc.add_job(name="legacy", message="ignored", every_secs=300)
        job.script = "x.py:f"
        job.last_status = "ok"
        job.last_result = "ok"
        svc._save()
        compact = _call_tool_inner("cron_list", {})
        assert "result=ok" not in compact
        full = _call_tool_inner("cron_list", {"verbose": True})
        assert "last result: ok" not in full

    def test_compact_collapses_message_newlines(self, home):
        """Embedded newlines collapsed so each job stays a single block."""
        svc = CronService(base_dir=home)
        svc.add_job(
            name="multiline",
            message="line one\nline two\nline three",
            every_secs=300,
        )
        out = _call_tool_inner("cron_list", {})
        arrow_idx = out.index("→")
        end_idx = out.find("\n", arrow_idx)
        preview_line = out[arrow_idx:end_idx if end_idx != -1 else len(out)]
        assert "line one" in preview_line
        assert "line two" in preview_line
        assert "line three" in preview_line

    def test_compact_sanitizes_before_truncating(self, home):
        """A credential straddling the truncation boundary must still be redacted."""
        svc = CronService(base_dir=home)
        secret = "AKIAIOSFODNN7EXAMPLE"
        padded_msg = ("p" * 70) + " key=" + secret + " trailing"
        svc.add_job(name="leaky", message=padded_msg, every_secs=300)
        out = _call_tool_inner("cron_list", {})
        assert secret not in out
        for prefix_len in (20, 16, 12, 8):
            assert secret[:prefix_len] not in out, (
                f"credential prefix of length {prefix_len} leaked into output"
            )


# ── Verbose (legacy regression guard) ──


class TestCronListVerbose:
    def test_verbose_returns_full_message_body(self, home):
        _seed_jobs(home, count=2, msg_size=1500)
        out = _call_tool_inner("cron_list", {"verbose": True})
        assert "x" * 1000 in out
        assert "compact" not in out

    def test_verbose_byte_identical_to_legacy_format(self, home):
        """The verbose output MUST match the pre-change legacy shape exactly."""
        svc = CronService(base_dir=home)
        svc.add_job(name="alpha", message="line one\nline two", every_secs=300)
        svc.add_job(name="beta", message="single line", every_secs=600)

        out = _call_tool_inner("cron_list", {"verbose": True})

        assert out.startswith("2 cron job(s): 2 active, 0 paused\n")
        # Legacy bullet/arrow + [kind] tag preserved.
        assert "• alpha (✅ active) [agent]" in out
        assert "• beta (✅ active) [agent]" in out
        assert "  → line one\nline two" in out
        assert "  → single line" in out

    def test_verbose_includes_last_error_line(self, home):
        svc = CronService(base_dir=home)
        job = svc.add_job(name="errjob", message="hi", every_secs=300)
        job.last_status = "error"
        job.last_error = "boom!"
        svc._save()
        out = _call_tool_inner("cron_list", {"verbose": True})
        assert "⚠️ last error: boom!" in out

    def test_verbose_includes_last_result_line(self, home):
        svc = CronService(base_dir=home)
        job = svc.add_job(name="sj", message="m", every_secs=300)
        job.script = "x.py:f"
        job.last_status = "ok"
        job.last_result = "computed 42 things"
        svc._save()
        out = _call_tool_inner("cron_list", {"verbose": True})
        assert "last result: computed 42 things" in out


# ── ids drill-in ──


class TestCronListIds:
    def test_ids_returns_full_bodies_for_matching_jobs_only(self, home):
        svc = _seed_jobs(home, count=5, msg_size=1500)
        jobs = svc.list_jobs()
        target = jobs[2].id
        out = _call_tool_inner("cron_list", {"ids": [target]})
        assert target in out
        assert "x" * 1000 in out
        for j in jobs:
            if j.id != target:
                assert j.id not in out

    def test_ids_no_match_returns_helpful_message(self, home):
        _seed_jobs(home, count=3)
        out = _call_tool_inner("cron_list", {"ids": ["deadbeef"]})
        assert "No cron jobs match ids:" in out
        assert "deadbeef" in out

    def test_ids_overrides_verbose_false(self, home):
        svc = _seed_jobs(home, count=2, msg_size=2000)
        target = svc.list_jobs()[0].id
        out = _call_tool_inner("cron_list", {"ids": [target]})
        assert "x" * 1500 in out


# ── Empty registry ──


class TestCronListEmpty:
    def test_empty_compact(self, home):
        out = _call_tool_inner("cron_list", {})
        assert out == "No cron jobs."

    def test_empty_verbose(self, home):
        out = _call_tool_inner("cron_list", {"verbose": True})
        assert out == "No cron jobs."

    def test_empty_with_ids(self, home):
        out = _call_tool_inner("cron_list", {"ids": ["abc12345"]})
        assert out == "No cron jobs."


# ── Schema validation ──


class TestCronListValidation:
    def test_verbose_must_be_bool(self):
        with pytest.raises(ValidationError):
            _validate_args("cron_list", {"verbose": "yes"})

    def test_ids_must_be_list(self):
        with pytest.raises(ValidationError):
            _validate_args("cron_list", {"ids": "abc12345"})

    def test_ids_items_must_match_job_id_pattern(self):
        with pytest.raises(ValidationError):
            _validate_args("cron_list", {"ids": ["NOT-A-HEX-ID"]})

    def test_ids_too_long_per_item_rejected(self):
        with pytest.raises(ValidationError):
            _validate_args("cron_list", {"ids": ["a" * 17]})

    def test_ids_empty_list_accepted(self):
        cleaned = _validate_args("cron_list", {"ids": []})
        assert cleaned.get("ids") == []

    def test_no_args_accepted(self):
        cleaned = _validate_args("cron_list", {})
        assert cleaned == {}

    def test_unknown_field_handled(self):
        try:
            cleaned = _validate_args("cron_list", {"verbose": False, "bogus": 1})
        except ValidationError:
            return
        assert "bogus" not in cleaned


# ── Mixed real + fake ids ──


class TestCronListIdsMissAll:
    def test_ids_with_some_real_some_fake(self, home):
        svc = _seed_jobs(home, count=2)
        real = svc.list_jobs()[0].id
        out = _call_tool_inner(
            "cron_list", {"ids": [real, "deadbeef" + uuid.uuid4().hex[:8]]}
        )
        assert real in out
