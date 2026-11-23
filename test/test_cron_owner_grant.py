"""A Bot's scheduled job keeps the approval mode its Profile gave it (``owner_grant``).

The job stores the Bot's Profile mode when ``cron_add`` makes it, and each fire
reads that together with the Bot's live Profile. These pin: the grant survives a
reload of the store (a gateway restart), a lowered Profile takes it away, a job
made while the Bot was not on Trust keeps asking, and anything missing asks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.cron import CronService
from kiro_crew.cron_service import owner_grant
from kiro_crew.cron_service.fields import auto_job_approver
from kiro_crew.mate_worker import MATE_WORKER_AGENT

_MEMBER = "m-slack-1"


def _config(mode: str = "") -> SimpleNamespace:
    """A live config with one Bot, ``slack``, whose Profile pins *mode* ("" = unset)."""
    return SimpleNamespace(agents={"slack": SimpleNamespace(approval_mode=mode)})


def _worker(member: str = _MEMBER, template: str = MATE_WORKER_AGENT) -> SimpleNamespace:
    return SimpleNamespace(template_id=template, member_id=member, selection_kind="template")


@pytest.fixture(autouse=True)
def _bot_named_by_member(_floor_monkeypatch):
    """``crewmate_name_for`` answers ``slack`` for the Bot's member id only."""
    monkeypatch = _floor_monkeypatch
    monkeypatch.setattr(
        "kiro_crew.dashboard.crewmate_approval.crewmate_name_for",
        lambda config, execution: (
            "slack" if getattr(execution, "member_id", None) == _MEMBER else ""
        ),
    )
    monkeypatch.setattr("kiro_crew.approval_mode_policy.session_mode_permitted", lambda m: True)


def _job(tmp_path, grant: tuple[str, str]):
    svc = CronService(base_dir=tmp_path)
    return svc.add_job(name="retro", message="go", every_secs=3600, owner_grant=grant)


def test_the_grant_survives_a_restart(tmp_path) -> None:
    """A fresh store read (the next process) still carries the Bot's grant."""
    job = _job(tmp_path, (_MEMBER, "trust"))

    reloaded = CronService(base_dir=tmp_path).get_job(job.id)

    assert reloaded is not None
    assert (reloaded.owner_member_id, reloaded.owner_approval_mode) == (_MEMBER, "trust")
    assert owner_grant.fire_approval_mode(reloaded, _worker(), _config()) == "auto"


def test_lowering_the_bots_profile_takes_the_grant_away(tmp_path) -> None:
    job = _job(tmp_path, (_MEMBER, "trust"))

    assert owner_grant.fire_approval_mode(job, _worker(), _config("normal")) == ""
    assert owner_grant.fire_approval_mode(job, _worker(), _config("trust_reads")) == ""


def test_a_lowered_profile_mid_turn_sends_the_next_call_to_the_prompt(tmp_path) -> None:
    """The per-request re-check reads the live Profile, not the turn's start."""
    job = _job(tmp_path, (_MEMBER, "trust"))
    live = {"cfg": _config()}
    asked: list[object] = []

    async def _prompt(event):
        asked.append(event)
        return False

    approve = auto_job_approver(
        _prompt,
        holds=lambda: owner_grant.fire_approval_mode(job, _worker(), live["cfg"]) == "auto",
    )
    assert asyncio.run(approve("first")) is True
    live["cfg"] = _config("normal")
    assert asyncio.run(approve("second")) is False
    assert asked == ["second"]


def test_a_job_made_while_the_bot_was_not_on_trust_keeps_asking(tmp_path) -> None:
    """Raising the Profile later does not lift a job above its creator's mode."""
    job = _job(tmp_path, (_MEMBER, "normal"))

    assert owner_grant.fire_approval_mode(job, _worker(), _config("trust")) == ""


@pytest.mark.parametrize(
    "grant, execution",
    [
        (("", ""), _worker()),  # no stored grant: a job made before this, or by a person
        ((_MEMBER, ""), _worker()),  # a stored member with no mode
        ((_MEMBER, "trust"), _worker(member="m-other")),  # another Bot's worker
        ((_MEMBER, "trust"), _worker(template="kirocrew")),  # not the Bot's worker
        ((_MEMBER, "trust"), None),  # no execution record
    ],
)
def test_anything_missing_or_different_asks(tmp_path, grant, execution) -> None:
    job = _job(tmp_path, grant)

    assert owner_grant.fire_approval_mode(job, execution, _config()) == ""


def test_a_policy_that_denies_trust_asks(tmp_path, monkeypatch) -> None:
    job = _job(tmp_path, (_MEMBER, "trust"))
    monkeypatch.setattr(
        "kiro_crew.approval_mode_policy.session_mode_permitted", lambda m: m != "trust"
    )

    assert owner_grant.fire_approval_mode(job, _worker(), _config()) == ""


def test_the_jobs_own_auto_still_runs_auto(tmp_path) -> None:
    job = CronService(base_dir=tmp_path).add_job(
        name="owner-auto", message="go", every_secs=3600, approval_mode="auto"
    )

    assert owner_grant.fire_approval_mode(job, None, None) == "auto"


@pytest.mark.parametrize(
    "profile, expected",
    [("", (_MEMBER, "trust")), ("normal", (_MEMBER, "normal")), ("trust", (_MEMBER, "trust"))],
)
def test_the_creator_grant_is_the_bots_profile_now(monkeypatch, profile, expected) -> None:
    monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", lambda key: _worker())

    assert owner_grant.creator_grant("dashboard:bot", _config(profile)) == expected


def test_a_session_that_is_no_bots_grants_nothing(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key: _worker(member="m-person"),
    )

    assert owner_grant.creator_grant("dashboard:chat-1", _config()) == ("", "")


def test_an_unreadable_creator_grants_nothing(monkeypatch) -> None:
    def _boom(key):
        raise OSError("unreadable")

    monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", _boom)

    assert owner_grant.creator_grant("dashboard:bot", _config()) == ("", "")


def test_cron_add_stores_the_creators_grant(tmp_path, monkeypatch) -> None:
    """The tool path is the one that records it, from the verified caller session."""
    from kiro_crew import mcp_cron
    from kiro_crew.mcp_caller import CallerContext, set_current_caller

    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seen: list[str] = []

    def _grant(key):
        seen.append(key)
        return _MEMBER, "trust"

    monkeypatch.setattr("kiro_crew.cron_service.owner_grant.creator_grant", _grant)
    set_current_caller(CallerContext(session_key="dashboard:bot", session_type="dashboard"))
    try:
        result = mcp_cron._call_tool_inner("cron_add", {"name": "r", "message": "go", "every": 120})
    finally:
        set_current_caller(None)
    assert "Added job" in result, result
    job = CronService(base_dir=tmp_path).get_job(result.split()[2])
    assert seen == ["dashboard:bot"]
    assert job is not None and (job.owner_member_id, job.owner_approval_mode) == (_MEMBER, "trust")
