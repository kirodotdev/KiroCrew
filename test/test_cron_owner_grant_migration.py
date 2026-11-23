"""The one-time stamp of a Bot's grant on jobs it made before ``cron_add`` stored one.

``owner_grant.migrate_owner_grants`` runs at start. It stamps a job only when the
record proves the Bot made it, runs once (a marker), and the fire-time check of
the Bot's live Profile still decides every run.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.cron import CronService
from kiro_crew.cron_service import owner_grant
from kiro_crew.mate_worker import MATE_WORKER_AGENT

_MEMBER = "m-slack-1"
_BOT_CHAT = "dashboard:member-slack"


def _config(mode: str = "") -> SimpleNamespace:
    return SimpleNamespace(agents={"slack": SimpleNamespace(approval_mode=mode)})


def _execution(member: str) -> SimpleNamespace:
    return SimpleNamespace(template_id=MATE_WORKER_AGENT, member_id=member, selection_kind="member")


@pytest.fixture(autouse=True)
def _world(_floor_monkeypatch):
    """One Bot, ``slack``; its chat session names it, any other session names a person."""
    mp = _floor_monkeypatch
    mp.setattr(
        "kiro_crew.dashboard.crewmate_approval.crewmate_name_for",
        lambda config, execution: (
            "slack" if getattr(execution, "member_id", None) == _MEMBER else ""
        ),
    )
    mp.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key: _execution(_MEMBER if key == _BOT_CHAT else "m-person"),
    )
    mp.setattr("kiro_crew.approval_mode_policy.session_mode_permitted", lambda m: True)
    return {"cfg": _config()}


def _add(tmp_path, name: str, **record: object) -> str:
    """A job as an older build left it, with *record* written over its row."""
    job = CronService(base_dir=tmp_path).add_job(name=name, message="go", every_secs=3600)
    path = tmp_path / "crons.json"
    data = json.loads(path.read_text())
    for row in data["jobs"]:
        if row["id"] == job.id:
            row.update(record)
    path.write_text(json.dumps(data))
    return job.id


def _bot_job(tmp_path, name: str = "retro") -> str:
    return _add(
        tmp_path,
        name,
        member_id=_MEMBER,
        session_key=_BOT_CHAT,
        execution_context={"member_id": _MEMBER},
    )


def _row(tmp_path, job_id: str) -> dict:
    data = json.loads((tmp_path / "crons.json").read_text())
    return next(r for r in data["jobs"] if r["id"] == job_id)


def _as_job(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["id"],
        approval_mode=row.get("approval_mode", ""),
        owner_member_id=row.get("owner_member_id", ""),
        owner_approval_mode=row.get("owner_approval_mode", ""),
    )


def test_a_job_its_bot_made_gets_the_bots_mode(tmp_path, _world) -> None:
    job_id = _bot_job(tmp_path)

    assert owner_grant.migrate_owner_grants(tmp_path, _world["cfg"]) == [job_id]

    row = _row(tmp_path, job_id)
    assert (row["owner_member_id"], row["owner_approval_mode"]) == (_MEMBER, "trust")
    job = _as_job(row)
    assert owner_grant.fire_approval_mode(job, _execution(_MEMBER), _world["cfg"]) == "auto"


def test_the_stamp_reloads_through_the_store(tmp_path, _world) -> None:
    job_id = _bot_job(tmp_path)
    owner_grant.migrate_owner_grants(tmp_path, _world["cfg"])

    job = CronService(base_dir=tmp_path).get_job(job_id)

    assert job is not None and (job.owner_member_id, job.owner_approval_mode) == (_MEMBER, "trust")


@pytest.mark.parametrize(
    "record",
    [
        # made in a person's chat, not the Bot's
        {
            "member_id": _MEMBER,
            "session_key": "dashboard:chat-1",
            "execution_context": {"member_id": _MEMBER},
        },
        # no maker recorded
        {"member_id": _MEMBER, "session_key": "", "execution_context": {"member_id": _MEMBER}},
        # the capture names another member than the job does
        {"member_id": _MEMBER, "session_key": _BOT_CHAT, "execution_context": {"member_id": "m-x"}},
        # made in the Bot's chat, but it runs as another member
        {"member_id": "m-x", "session_key": _BOT_CHAT, "execution_context": {"member_id": "m-x"}},
        # no capture at all
        {"member_id": _MEMBER, "session_key": _BOT_CHAT},
        # an app's job
        {
            "member_id": _MEMBER,
            "session_key": _BOT_CHAT,
            "execution_context": {"member_id": _MEMBER},
            "created_by": "app:radar",
        },
        # no member: a person's own job
        {"session_key": _BOT_CHAT},
    ],
)
def test_a_job_whose_maker_is_unknown_or_another_is_left_asking(tmp_path, record, _world) -> None:
    job_id = _add(tmp_path, "other", **record)

    assert owner_grant.migrate_owner_grants(tmp_path, _world["cfg"]) == []

    row = _row(tmp_path, job_id)
    assert not row.get("owner_member_id") and not row.get("owner_approval_mode")


def test_a_second_start_stamps_nothing(tmp_path, _world) -> None:
    owner_grant.migrate_owner_grants(tmp_path, _world["cfg"])
    assert (tmp_path / owner_grant.MIGRATION_MARKER).exists()
    late = _bot_job(tmp_path, "late")
    before = (tmp_path / "crons.json").read_text()

    assert owner_grant.migrate_owner_grants(tmp_path, _world["cfg"]) == []

    assert (tmp_path / "crons.json").read_text() == before
    assert not _row(tmp_path, late).get("owner_member_id")


def test_a_job_with_a_grant_already_is_not_restamped(tmp_path, _world) -> None:
    job_id = _add(
        tmp_path,
        "made-now",
        member_id=_MEMBER,
        session_key=_BOT_CHAT,
        execution_context={"member_id": _MEMBER},
        owner_member_id=_MEMBER,
        owner_approval_mode="normal",
    )

    assert owner_grant.migrate_owner_grants(tmp_path, _world["cfg"]) == []
    assert _row(tmp_path, job_id)["owner_approval_mode"] == "normal"


def test_a_lowered_profile_still_asks_after_the_stamp(tmp_path, _world) -> None:
    job_id = _bot_job(tmp_path)
    owner_grant.migrate_owner_grants(tmp_path, _world["cfg"])
    job = _as_job(_row(tmp_path, job_id))

    assert owner_grant.fire_approval_mode(job, _execution(_MEMBER), _config("normal")) == ""


def test_a_bot_not_on_trust_at_migration_keeps_its_jobs_asking(tmp_path, _world) -> None:
    _world["cfg"] = _config("normal")
    job_id = _bot_job(tmp_path)

    owner_grant.migrate_owner_grants(tmp_path, _world["cfg"])

    job = _as_job(_row(tmp_path, job_id))
    assert job.owner_approval_mode == "normal"
    assert owner_grant.fire_approval_mode(job, _execution(_MEMBER), _config("trust")) == ""
