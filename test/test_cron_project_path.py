"""A cron job's optional project directory (``project_path``).

An agent job may name the directory its session runs in, so that repo's own
``.kiro/steering/`` and ``AGENTS.md`` load. Empty means the cwd a fire picked
before the field existed; a record written before it loads unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from slack_gateway_cron_exec_coverage_helpers import (
    _cron_message_cb,
    _job,
    _make_orchestrator,
    _message_arm_sessions,
    _mock_dashboard_state,
)

from kiro_crew.cli_commands import _cron
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig
from kiro_crew.cron import CronService, build_job
from kiro_crew.cron_service.fields import cron_session_cwd, resolve_project_path
from kiro_crew.testing.links import make_dir_link

# A record exactly as a build before this field wrote it: no "project_path" key.
_PRE_CHANGE_RECORD = {
    "id": "legacy01",
    "name": "nightly",
    "message": "check the build",
    "schedule": {"kind": "every", "every_secs": 3600},
    "enabled": True,
    "agent_id": "",
    "persistent_session": True,
    "hide_in_chat": False,
    "folder_id": "",
    "chat_folder_id": "",
    "model": "",
    "created_ts": 1700000000.0,
}


def _write_store(path: Path, jobs: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "jobs": jobs}), encoding="utf-8")


def _repo(tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    (repo / ".kiro" / "steering").mkdir(parents=True)
    return os.path.realpath(repo)


def _alias_cfg(ws_dir: str) -> KiroCrewConfig:
    return KiroCrewConfig(
        agents={
            "in-3d": KiroCrewAgentConfig(
                kiro_agent="kirocrew", workspace="in-3d", memory_store="default"
            )
        },
        workspaces={"in-3d": WorkspaceConfig(dir=ws_dir)},
    )


# ── Store: old records, round trip ──────────────────────────────────────────


def test_a_record_written_before_the_field_loads_unchanged(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    _write_store(svc._path, [_PRE_CHANGE_RECORD])
    svc._load()
    (job,) = svc._jobs
    assert job.id == "legacy01"
    assert job.project_path == ""
    # It fires in the cwd the agent resolution chose, exactly as before.
    assert cron_session_cwd(job, None) is None
    assert cron_session_cwd(job, "/ws/in-3d") == "/ws/in-3d"


def test_project_path_round_trips_through_the_store(tmp_path) -> None:
    repo = _repo(tmp_path)
    svc = CronService(base_dir=tmp_path / "home")
    job = svc.add_job("r", "go", every_secs=3600, project_path=repo)
    assert job.project_path == repo
    reread = CronService(base_dir=tmp_path / "home").get_job(job.id)
    assert reread is not None and reread.project_path == repo


# ── Validation ──────────────────────────────────────────────────────────────


def test_empty_is_no_project_directory() -> None:
    assert resolve_project_path("") == ""
    assert build_job("j", "m", every_secs=60).project_path == ""


def test_the_stored_path_is_resolved(tmp_path) -> None:
    repo = _repo(tmp_path)
    link = tmp_path / "link"
    make_dir_link(link, Path(repo))
    assert build_job("j", "m", every_secs=60, project_path=str(link)).project_path == repo


@pytest.mark.parametrize(
    "raw, needle",
    [
        ("relative/dir", "absolute"),
        ("//server/share/repo", "network share"),
    ],
)
def test_bad_paths_are_refused(raw, needle) -> None:
    with pytest.raises(ValueError, match=needle):
        build_job("j", "m", every_secs=60, project_path=raw)


def test_a_missing_directory_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="existing directory"):
        build_job("j", "m", every_secs=60, project_path=str(tmp_path / "never-made"))


def test_a_sensitive_directory_is_refused(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_folders.canonical_path_refusal",
        lambda p: f"Blocked: access to sensitive path: {p}" if p == repo else None,
    )
    with pytest.raises(ValueError, match="sensitive"):
        resolve_project_path(repo)


def test_script_and_command_jobs_refuse_a_project_path(tmp_path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="agent"):
        build_job("j", "", every_secs=60, command="df -h /", project_path=repo)


def test_update_sets_and_clears_it_and_a_refusal_leaves_the_job_untouched(tmp_path) -> None:
    repo = _repo(tmp_path)
    svc = CronService(base_dir=tmp_path / "home")
    job = svc.add_job("r", "go", every_secs=3600)
    assert svc.update_job(job.id, project_path=repo).project_path == repo
    with pytest.raises(ValueError):
        svc.update_job(job.id, name="renamed", project_path=str(tmp_path / "never-made"))
    kept = svc.get_job(job.id)
    assert (kept.name, kept.project_path) == ("r", repo)
    assert svc.update_job(job.id, project_path="").project_path == ""


# ── Fire: the session runs in the project directory ─────────────────────────


@pytest.mark.asyncio
async def test_a_fire_runs_the_session_in_the_project_directory(tmp_path, monkeypatch) -> None:
    """The project directory outranks the alias workspace as the session cwd."""
    from kiro_crew.slack import gateway as _gw

    monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
    repo = _repo(tmp_path)
    orch = _make_orchestrator()
    orch._cfg = _alias_cfg(str(tmp_path / "in-3d-ws"))
    orch.sessions = _message_arm_sessions()
    orch.dashboard_state = _mock_dashboard_state()
    job = _job(id="jp1", name="repo probe", agent_id="in-3d", project_path=repo)

    async with _cron_message_cb(orch, result_text="ok") as callback:
        await callback(job)

    kw = orch.sessions.get_or_create.await_args.kwargs
    assert kw.get("cwd") == repo
    # Only the cwd changes: the alias still picks the agent and crew identity.
    assert kw.get("agent") == "kirocrew"
    assert kw.get("crew_agent") == "in-3d"


@pytest.mark.asyncio
async def test_a_fire_without_a_project_directory_keeps_the_alias_workspace(
    tmp_path, monkeypatch
) -> None:
    from kiro_crew.slack import gateway as _gw

    monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
    ws_dir = str(tmp_path / "in-3d-ws")
    orch = _make_orchestrator()
    orch._cfg = _alias_cfg(ws_dir)
    orch.sessions = _message_arm_sessions()
    orch.dashboard_state = _mock_dashboard_state()
    job = _job(id="jp2", name="no-project probe", agent_id="in-3d")

    async with _cron_message_cb(orch, result_text="ok") as callback:
        await callback(job)

    assert orch.sessions.get_or_create.await_args.kwargs.get("cwd") == ws_dir


@pytest.mark.asyncio
async def test_a_project_directory_that_is_gone_fails_the_run(tmp_path, monkeypatch) -> None:
    """Never a silent fall back to a cwd without the project's rules."""
    from kiro_crew.slack import gateway as _gw

    monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
    orch = _make_orchestrator()
    orch._cfg = KiroCrewConfig()
    orch.sessions = _message_arm_sessions()
    orch.dashboard_state = _mock_dashboard_state()
    job = _job(id="jp3", name="gone probe", project_path=str(tmp_path / "deleted"))

    # The callback raises, which is how the cron service records a failed run.
    async with _cron_message_cb(orch, result_text="ok") as callback:
        with pytest.raises(RuntimeError, match="project_path"):
            await callback(job)

    orch.sessions.get_or_create.assert_not_awaited()


def test_the_fire_time_refusal_is_not_transient(tmp_path) -> None:
    job = _job(id="jp4", project_path=str(tmp_path / "deleted"))
    with pytest.raises(RuntimeError) as exc_info:
        cron_session_cwd(job, None)
    assert exc_info.value.transient is False  # type: ignore[attr-defined]
    assert "--project-path ''" in str(exc_info.value)


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_add_and_update_carry_project_path(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    with patch("kiro_crew.cli_commands.sel"):
        _cron(
            argparse.Namespace(
                cron_action="add",
                name="repo-job",
                message="hi",
                every=300,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="",
                project_path=repo,
            )
        )
    (job,) = CronService(base_dir=tmp_path / "home").list_jobs(include_disabled=True)
    assert job.project_path == repo
    with patch("kiro_crew.cli_commands.sel"):
        _cron(argparse.Namespace(cron_action="list"))
    assert f"project: {repo}" in capsys.readouterr().out
    with patch("kiro_crew.cli_commands.sel"):
        _cron(argparse.Namespace(cron_action="update", job_id=job.id, project_path=""))
    (job,) = CronService(base_dir=tmp_path / "home").list_jobs(include_disabled=True)
    assert job.project_path == ""
