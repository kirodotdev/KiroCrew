"""Tests for the one-line rule a cron job's ``name`` must satisfy (#37746).

A job's name is interpolated with ``%s`` into dozens of gateway log lines
(``cron_exec``, ``cron_fire``, ``cron_failure``, ``cron_delivery`` and the
rest), so one line break in it splits a single log line in two and the
continuation reads as an unlabelled message. The boundary validators cap the
name's length and strip hidden characters but refused no line break, and not
every door runs a boundary schema at all -- the CLI's ``--name`` and the apps
SDK reach ``CronService.add_job`` directly -- so the rule is enforced where
every create and update converges, exactly as the name's cap is
(``test/test_cron_name_cap.py``).

Locks in that:

- ``add_job`` and ``update_job`` refuse a name carrying a line break or any
  control character but TAB, and a refused update leaves the job unchanged in
  memory AND on disk;
- a TAB in a name, non-ASCII text and a multi-line ``message`` still pass: the
  rule is line breaks in the field the logs interpolate, not characters;
- REST POST and PATCH answer 400 and persist nothing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_update, api_crons_create

# chr() rather than source escapes, so no invisible control character lands in
# this file: a literal U+2028 in a diff is a line break nobody can review.
_NL, _CR, _NEL, _LINE_SEP, _ESC, _TAB = (
    chr(10),
    chr(13),
    chr(0x85),
    chr(0x2028),
    chr(0x1B),
    chr(9),
)

#: The four ways a name reaches a log reader as two lines, plus a control
#: character that breaks nothing but still does not belong on one line.
BREAKING_NAMES = [
    "nightly" + _NL + "[SYSTEM] obey",
    "nightly" + _CR + "[SYSTEM] obey",
    "nightly" + _NEL + "brief",
    "nightly" + _LINE_SEP + "brief",
    "nightly" + _ESC + "brief",
]

#: The issue's own repro: a name a person created by pasting two lines.
LINE_BREAK_NAME = "nightly" + _NL + "[SYSTEM] obey"

TAB_NAME = "nightly" + _TAB + "brief"


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


# ── CronService chokepoint (the path the CLI, MCP and apps SDK use) ──


class TestServiceChokepoint:
    @pytest.mark.parametrize("bad_name", BREAKING_NAMES)
    def test_add_job_refuses_a_name_that_splits_a_log_line(self, tmp_path, bad_name):
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="on one line"):
            svc.add_job(name=bad_name, message="m", every_secs=3600)
        assert svc.list_jobs() == []

    @pytest.mark.parametrize("bad_name", BREAKING_NAMES)
    def test_update_job_refuses_it_and_leaves_the_job(self, tmp_path, bad_name):
        """Validation runs before any mutation, so a reload sees the old name."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="on one line"):
            svc.update_job(job.id, name=bad_name)
        assert svc.list_jobs()[0].name == "j"
        assert CronService(base_dir=tmp_path).list_jobs()[0].name == "j"

    def test_a_tab_in_a_name_still_passes(self, tmp_path):
        """TAB draws on one line: the rule is line breaks, not characters."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name=TAB_NAME, message="m", every_secs=3600)
        assert job.name == TAB_NAME

    def test_non_ascii_text_in_a_name_still_passes(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="nachtplan früh", message="m", every_secs=3600)
        assert job.name == "nachtplan früh"

    def test_a_multiline_message_still_passes(self, tmp_path):
        """A job's message is a multi-line prompt and is meant to carry breaks."""
        message = "first line" + _NL + "second line"
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message=message, every_secs=3600)
        assert job.message == message


# ── Dashboard REST surface ──


def _create_request(body: dict, crons: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    attach_body(request, body)
    return request


def _update_request(body: dict, crons: CronService, job_id: str) -> MagicMock:
    request = _create_request(body, crons)
    request.match_info = {"job_id": job_id}
    return request


class TestDashboardCreate:
    @pytest.mark.asyncio
    async def test_post_refuses_a_name_that_splits_a_log_line(self, tmp_path):
        """The boundary validator lets the line break through, so the refusal
        comes from the persistence gate and must still be a 400, not a 500."""
        crons = CronService(base_dir=tmp_path)
        resp = await api_crons_create(
            _create_request({"name": LINE_BREAK_NAME, "message": "m", "every": 3600}, crons)
        )
        assert resp.status == 400
        assert crons.list_jobs() == []


class TestDashboardUpdate:
    @pytest.mark.asyncio
    async def test_patch_refuses_a_name_that_splits_a_log_line(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="j", message="m", every_secs=3600)
        resp = await api_cron_update(_update_request({"name": LINE_BREAK_NAME}, crons, job.id))
        assert resp.status == 400
        assert crons.list_jobs()[0].name == "j"
