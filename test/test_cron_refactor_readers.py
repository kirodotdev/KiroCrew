"""The serviceless ``crons.json`` readers and the run-identity helpers, through ``kiro_crew.cron``.

``kirocrew doctor``, the Agent templates delete guard, the skill lifecycle and the
dashboard count read the store with no scheduler running
(:mod:`kiro_crew.cron_service.readers`). Each case writes a store, points
``kiro_crew.cron.config_dir`` at it -- the seam those readers go through -- and
asks the reader what a caller would.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.cron as cron
from kiro_crew.cron import CronJob, CronStoreUnreadable


def _record(job_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "name": f"job {job_id}",
        "message": "m",
        "schedule": {"kind": "every"},
        **fields,
    }


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cron, "config_dir", lambda: tmp_path)

    def write(records: Any) -> Path:
        path = tmp_path / "crons.json"
        path.write_text(json.dumps(records if isinstance(records, dict) else {"jobs": records}))
        return path

    return write


class TestDispatchedAgents:
    def test_each_record_reports_the_agents_dispatch_runs(self, store) -> None:
        store(
            [
                _record(
                    "seq", agent_sequence=["planner", "", "writer", "planner"], agent_id="dormant"
                ),
                _record("tpl", agent_id="dormant", execution_context={"template_id": "templated"}),
                _record("legacy", agent_id="helper"),
                _record("script", script="x.py:f", agent_id="ignored"),
                _record("command", command="true", agent_id="ignored"),
                _record("badseq", agent_sequence="planner"),
                _record("badagent", agent_id=7),
                {"id": "noschedule", "name": "", "message": "m", "agent_id": "orphan"},
            ]
        )
        assert cron.dispatched_agents_from_disk(loadable_only=False) == [
            ("seq", "job seq", "planner"),
            ("seq", "job seq", "writer"),
            ("tpl", "job tpl", "templated"),
            ("legacy", "job legacy", "helper"),
            ("noschedule", "noschedule", "orphan"),
        ]
        # The delete guard counts only what the scheduler could build.
        assert ("noschedule", "noschedule", "orphan") not in cron.dispatched_agents_from_disk(
            loadable_only=True
        )

    def test_an_unreadable_store_raises_rather_than_reading_as_empty(self, store) -> None:
        store({"jobs": None})
        with pytest.raises(CronStoreUnreadable):
            cron.dispatched_agents_from_disk(loadable_only=True)
        assert cron.job_agent_names_from_disk() == []

    def test_the_doctor_view_names_the_job_by_its_label(self, store) -> None:
        store(
            [
                _record("a", agent_id="helper"),
                {"id": "", "name": "", "message": "m", "agent_id": "x"},
            ]
        )
        assert cron.job_agent_names_from_disk() == [("job a", "helper"), ("<unnamed job>", "x")]


class TestUnhealthyJobs:
    def test_auto_pause_wins_and_a_user_pause_is_not_a_health_signal(self, store) -> None:
        store(
            [
                _record("auto", auto_paused=True, last_status="error"),
                _record("err", last_status="error"),
                _record("user", user_paused=True, last_status="error"),
                _record("legacy_off", enabled=False, last_status="error"),
                _record("ok", last_status="ok"),
                {"id": "bad", "name": "x", "message": "m"},
            ]
        )
        auto, errored, loadable = cron.unhealthy_jobs_from_disk()
        assert auto == [("auto", "job auto")]
        assert errored == [("err", "job err")]
        assert loadable is True

    def test_a_store_nothing_loads_from_is_reported(self, store) -> None:
        store([{"id": "bad"}])
        assert cron.unhealthy_jobs_from_disk() == ([], [], False)

    def test_pause_state_per_job(self, store) -> None:
        store([_record("u", user_paused=True), _record("a", auto_paused=True), _record("e")])
        assert cron.job_pause_state_from_disk("u") == "paused by the user"
        assert cron.job_pause_state_from_disk("a") == "auto-paused"
        assert cron.job_pause_state_from_disk("e") == "enabled"
        assert cron.job_pause_state_from_disk("missing") is None


class TestCountsAndSkillReferences:
    def test_enabled_count_skips_unloadable_and_paused_records(self, store) -> None:
        path = store(
            [_record("a"), _record("b", user_paused=True), _record("c", auto_paused=True), {"x": 1}]
        )
        assert cron.enabled_count_from_disk(path) == (1, True)
        assert cron.enabled_count_from_disk(path.with_name("absent.json")) == (0, True)

    def test_skill_tokens_are_read_from_every_message(self, store) -> None:
        store([_record("a", message="use $auto/deploy-helper and $x1 but not $123 or a$b")])
        assert cron.referenced_skill_names() == {"auto/deploy-helper", "deploy-helper", "x1"}

    def test_a_corrupt_store_yields_no_references(self, store, tmp_path: Path) -> None:
        (tmp_path / "crons.json").write_bytes(b"\xff\xfe not json")
        assert cron.referenced_skill_names() == set()


class TestRunIdentity:
    def test_a_persistent_key_is_stable_and_carries_the_last_result(self) -> None:
        job = CronJob(id="j1", name="n", message="go", last_result="x" * 2100, minimal_context=True)
        key, prompt = cron.build_cron_session_context(job)
        assert key == "cron:j1"
        assert prompt.startswith(
            "[Previous run result — do NOT repeat the same content]\n[truncated]…"
        )
        assert prompt.endswith("go") and cron.cron_session_key_is_stable(job)

    def test_a_stateless_key_is_fresh_per_run_unless_a_sequence_dispatches(self) -> None:
        job = CronJob(id="j1", name="n", message="go", persistent_session=False)
        first, prompt = cron.build_cron_session_context(job)
        second, _ = cron.build_cron_session_context(job)
        assert first != second and first.startswith("cron:j1:") and prompt == "go"
        assert not cron.cron_session_key_is_stable(job)
        job.agent_sequence = ["a", "b"]
        assert cron.cron_session_key_is_stable(job)

    def test_the_key_parser_and_owner_matcher(self) -> None:
        assert cron.cron_job_id_from_session_key("cron:abc:run") == "abc"
        assert cron.cron_job_id_from_session_key(["cron:abc"]) == ""
        assert cron.cron_owner_matches("cron:abc:agent", "cron:abc")
        assert not cron.cron_owner_matches("dashboard:a", "dashboard:b")

    def test_a_schedule_without_a_captured_execution_resolves_or_refuses(self) -> None:
        assert cron.resolve_cron_memory(CronJob(id="j", name="n", message="m", agent_id="t")) == (
            "",
            "t",
        )
        malformed = CronJob(id="j", name="n", message="m")
        malformed.member_id = None  # type: ignore[assignment]
        with pytest.raises(ValueError, match="malformed schedule identity"):
            cron.resolve_cron_memory(malformed)
        with pytest.raises(ValueError, match="no canonical execution context"):
            cron.resolve_cron_memory(CronJob(id="j", name="n", message="m", member_id="m1"))
