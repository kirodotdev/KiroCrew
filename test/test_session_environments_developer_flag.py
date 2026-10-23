"""Session Environments are behind the developer flag, and a corrupt choices file
refuses only the sessions that were bound."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import session_environment as se
from kiro_crew.coder.session_launch import SessionEnvironmentChoices
from kiro_crew.providers import acp_runtime_start
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.slack import gateway


@pytest.fixture
def choices(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: tmp_path)
    monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
    store = SessionEnvironmentChoices(se.choices_path())
    store.set("dashboard:remote", "dev")
    return store


def test_flag_off_means_every_session_runs_here(choices, monkeypatch):
    assert choices.resolve("dashboard:remote") == ("dev", "dashboard:remote")
    monkeypatch.delenv("KIROCREW_DEV_MODE")
    assert not se.session_environments_enabled()
    assert choices.resolve("dashboard:remote") is None
    assert not se.session_runs_remotely("dashboard:remote")
    assert not se.session_attached_remotely("dashboard:remote")
    assert not acp_runtime_start._choices_file_may_exist()
    # A child inherits nothing, so it runs here too.
    assert choices.inherit("subagent:child", "dashboard:remote") is None


def test_flag_off_starts_no_coder_subsystem(monkeypatch):
    monkeypatch.delenv("KIROCREW_DEV_MODE", raising=False)
    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    cfg = SimpleNamespace(session_environments=SimpleNamespace(coder_url="https://c.example"))
    gateway._start_coder_retention(cfg)  # no running loop needed: it returns first
    assert gateway._CODER_RETENTION_TASK is None


def test_a_corrupt_file_refuses_only_bound_sessions(choices):
    choices.inherit("subagent:child", "dashboard:remote")
    choices.path.write_text("{not json", encoding="utf-8")
    for bound in ("dashboard:remote", "subagent:child"):
        with pytest.raises(SessionEnvironmentUnavailable):
            choices.resolve(bound)
        assert se.session_runs_remotely(bound)
    assert choices.resolve("dashboard:never-bound") is None
    assert not se.session_runs_remotely("dashboard:never-bound")


def test_clearing_a_choice_removes_its_marker(choices):
    choices.set("dashboard:remote", None)
    choices.path.write_text("{not json", encoding="utf-8")
    assert choices.resolve("dashboard:remote") is None


def test_an_invalid_utf8_file_refuses_only_bound_sessions(choices):
    choices.path.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(SessionEnvironmentUnavailable):
        choices.resolve("dashboard:remote")
    assert choices.resolve("dashboard:never-bound") is None


def test_a_corrupt_file_does_not_stop_a_local_sessions_subagent(choices, monkeypatch):
    choices.path.write_text("{not json", encoding="utf-8")
    assert choices.inherit("subagent:child", "dashboard:local-parent") is None
    with pytest.raises(SessionEnvironmentUnavailable):
        choices.inherit("subagent:child-2", "dashboard:remote")
    monkeypatch.delenv("KIROCREW_DEV_MODE")
    assert choices.inherit("subagent:child-3", "dashboard:remote") is None


@pytest.mark.parametrize("lost", ["file", "row"])
def test_a_bound_session_whose_choice_is_lost_is_refused_not_run_here(choices, lost):
    """GPT 6.1 on 1d19219382: a bound session keeps its marker, so losing the choices
    file or just its row refuses it rather than sending its writes to this machine."""
    choices.set("dashboard:other", "dev")
    if lost == "file":
        se.choices_path().unlink()
    else:
        sessions = choices._read()
        del sessions["dashboard:remote"]
        choices._write(sessions)  # no ``before``: the row vanished, its marker stayed
    with pytest.raises(SessionEnvironmentUnavailable):
        choices.resolve("dashboard:remote")
    assert se.session_runs_remotely("dashboard:remote")
    # The start-up fast path reads the marker when the file is gone.
    assert acp_runtime_start._choices_file_may_exist("dashboard:remote")
    assert acp_runtime_start._choices_file_may_exist("dashboard:never") is (lost == "row")
    assert choices.resolve("dashboard:never") is None


def test_every_way_a_row_leaves_takes_its_marker(choices, monkeypatch):
    """A row ends by being cleared, by an inherited row ageing out, or by a child
    whose parent runs here; none of them may leave a marker that later refuses."""
    from kiro_crew.coder import session_launch

    choices.inherit("dashboard:child", "dashboard:remote")
    assert choices.may_be_bound("dashboard:child")
    monkeypatch.setattr(session_launch, "ENDED_ROW_TTL_SECS", -1)
    assert choices.forget_inherited("dashboard:child")  # ended, and at once aged out
    assert not choices.may_be_bound("dashboard:child")
    assert choices.resolve("dashboard:child") is None
    choices.set("dashboard:remote", None)
    assert not choices.may_be_bound("dashboard:remote")
