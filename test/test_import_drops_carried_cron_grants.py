"""An imported cron store loses the grants the writing install recorded.

A Bot's job carries its Profile grant in ``owner_approval_mode``, and a job made from
a person's tab carries ``person_owner_key``. Both were recorded on the install that
wrote the archive. Kept on import, the first lets the job auto-approve when the same
Bot is Trust here, and the second lets the job's cron session run after its tab closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kiro_crew.cron_service import owner_grant
from kiro_crew.portability_vetting import _sanitize_imported_crons


def _message_job(**extra) -> dict:
    return {
        "id": "id-nightly",
        "name": "nightly",
        "message": "check the build",
        "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        "enabled": True,
        **extra,
    }


def _import(tmp_path: Path, job: dict) -> dict:
    path = tmp_path / "crons.json"
    path.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    _sanitize_imported_crons(path)
    return json.loads(path.read_text(encoding="utf-8"))["jobs"][0]


def test_an_imported_job_loses_the_bots_carried_grant_and_keeps_its_owner(tmp_path):
    restored = _import(tmp_path, _message_job(owner_member_id="bot-1", owner_approval_mode="trust"))

    assert not restored.get("owner_approval_mode")
    assert restored["owner_member_id"] == "bot-1"
    assert restored["message"] == "check the build"


def test_an_imported_bot_job_no_longer_holds_the_owner_grant(tmp_path):
    restored = _import(tmp_path, _message_job(owner_member_id="bot-1", owner_approval_mode="trust"))
    execution = SimpleNamespace(template_id="worker", member_id="bot-1")

    with (
        patch.object(owner_grant, "_profile_mode", return_value="trust"),
        patch("kiro_crew.mate_worker.MATE_WORKER_AGENT", "worker"),
        patch("kiro_crew.dashboard.crewmate_approval.crewmate_name_for", return_value="Bot"),
    ):
        assert (
            owner_grant.owner_grant_holds(SimpleNamespace(**restored), execution, object()) is False
        )


def test_an_imported_job_loses_the_person_tab_mark(tmp_path):
    restored = _import(
        tmp_path,
        _message_job(session_key="dashboard:far-tab", person_owner_key="dashboard:far-tab"),
    )

    assert not restored.get("person_owner_key")


def test_a_job_with_no_grant_is_left_as_it_was(tmp_path):
    job = _message_job()
    path = tmp_path / "crons.json"
    text = json.dumps({"jobs": [job]})
    path.write_text(text, encoding="utf-8")

    _sanitize_imported_crons(path)

    assert path.read_text(encoding="utf-8") == text
