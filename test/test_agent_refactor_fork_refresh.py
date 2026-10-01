"""The fork refresh fails closed on every branch that cannot vouch for a fork.

A fork carries ``allowedTools`` and ``autoApprove`` grants that never reach the
PreToolUse gate, so a fork whose last refresh did not re-filter them must not start a
session. ``_fork_refresh_failed`` is how the refresh says so: ``"*"`` when the pass
died before per-fork accounting, the fork's name when that one fork could not be
refreshed. These pin each branch, and that the spawn gate's settled event is set
again whenever no pass is left to set it.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pytest

from kiro_crew import agent, agent_state
from kiro_crew.agent_materialization import fork_refresh
from kiro_crew.config.loader import KiroCrewConfig


class _Boom(Exception):
    pass


def _raise(*_args: object, **_kwargs: object) -> None:
    raise _Boom("refresh died")


@pytest.fixture(autouse=True)
def _restore_refresh_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts settled with no failures, and leaves it that way."""
    monkeypatch.setattr(agent, "_fork_refresh_failed", frozenset())
    monkeypatch.setattr(agent, "_fork_refresh_pending", 0)
    fork_refresh._fork_refresh_settled.set()
    yield
    fork_refresh._fork_refresh_settled.set()


class _InlineThread:
    """Runs its target on ``start`` so the deferred pass finishes before the assert."""

    def __init__(self, *, target: Callable[[], None], name: str, daemon: bool) -> None:
        assert name == "fork-refresh" and daemon
        self._target = target

    def start(self) -> None:
        self._target()


class _UnstartableThread(_InlineThread):
    def start(self) -> None:
        raise RuntimeError("can't start new thread")


def _threads(monkeypatch: pytest.MonkeyPatch, thread: type) -> None:
    monkeypatch.setattr(fork_refresh, "threading", SimpleNamespace(Thread=thread))


def test_a_deferred_pass_that_dies_blocks_every_fork(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _threads(monkeypatch, _InlineThread)
    monkeypatch.setattr(agent, "_refresh_forked_templates", _raise)
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        fork_refresh.refresh_after_rebuild("defer", frozenset())
    assert agent._fork_refresh_failed == frozenset({"*"})
    assert "deferred fork refresh failed" in caplog.text
    # The deferral cleared the event and only a finished pass re-sets it.
    assert not fork_refresh._fork_refresh_settled.is_set()


def test_a_deferred_pass_whose_thread_never_starts_fails_closed_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _threads(monkeypatch, _UnstartableThread)
    with pytest.raises(RuntimeError, match="can't start"):
        fork_refresh.refresh_after_rebuild("defer", frozenset())
    assert agent._fork_refresh_failed == frozenset({"*"})
    assert fork_refresh._fork_refresh_settled.is_set()


def test_a_synchronous_pass_that_dies_does_not_fail_the_rebuild(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(agent, "_refresh_forked_templates", _raise)
    with caplog.at_level(logging.DEBUG, logger=agent.logger.name):
        fork_refresh.refresh_after_rebuild(True, frozenset())
    assert "forked template refresh failed" in caplog.text


def test_no_refresh_at_all_when_the_caller_opts_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "_refresh_forked_templates", _raise)
    fork_refresh.refresh_after_rebuild(False, frozenset())
    assert agent._fork_refresh_failed == frozenset()


def test_a_pass_that_dies_before_accounting_records_the_wildcard_and_re_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "_refresh_forked_templates_locked", _raise)
    with pytest.raises(_Boom):
        agent._refresh_forked_templates(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset({"*"})
    assert agent._fork_refresh_pending == 0
    assert fork_refresh._fork_refresh_settled.is_set()


def _forks(monkeypatch: pytest.MonkeyPatch, forks: dict[str, dict[str, Any]]) -> None:
    monkeypatch.setattr(agent_state, "all_fork_info", lambda: forks)
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _name: None)


def _bindings(monkeypatch: pytest.MonkeyPatch, bound: dict[str, str]) -> None:
    agents = {crew: SimpleNamespace(kiro_agent=name) for crew, name in bound.items()}
    monkeypatch.setattr(
        KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace(agents=agents))
    )


def test_no_forks_clears_the_failure_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "_fork_refresh_failed", frozenset({"stale"}))
    _forks(monkeypatch, {})
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset()


def test_an_unreadable_config_corroborates_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _forks(monkeypatch, {"crewfork": {"private_to": "crew", "forked_from": "kirocrew"}})
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_raise))
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset({"*"})
    assert "config unreadable" in caplog.text


@pytest.fixture
def one_fork(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One corroborated fork of the owned template, and an owned spec listed as a fork."""
    _forks(
        monkeypatch,
        {
            "crewfork": {"private_to": "crew", "forked_from": "kirocrew"},
            "kirocrew-lite": {"private_to": "other", "forked_from": "kirocrew"},
        },
    )
    _bindings(monkeypatch, {"crew": "crewfork", "other": "kirocrew-lite"})
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    return tmp_path


def test_a_fork_with_no_spec_on_disk_has_nothing_to_refresh(
    one_fork: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: None)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset()


def test_a_fork_resolving_to_a_markdown_spec_stays_blocked(
    one_fork: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spec = one_fork / "crewfork.md"
    spec.write_text("---\nname: crewfork\n---\n", encoding="utf-8")
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset({"crewfork"})
    assert "markdown spec" in caplog.text


def test_a_fork_whose_spec_does_not_read_as_an_object_stays_blocked(
    one_fork: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = one_fork / "crewfork.json"
    spec.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
    monkeypatch.setattr(agent, "_load_json", lambda _path: [])
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset({"crewfork"})


def test_a_plumbing_failure_still_writes_the_governance_passes(
    one_fork: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spec = one_fork / "crewfork.json"
    spec.write_text(json.dumps({"name": "crewfork", "tools": [], "allowedTools": []}))
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
    monkeypatch.setattr(agent, "_refresh_dynamic_fields", _raise)
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(agent, "_atomic_json_write", lambda _path, config: written.append(config))
    with caplog.at_level(logging.DEBUG, logger=agent.logger.name):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert "refresh failed for forked template 'crewfork'" in caplog.text
    assert [config["name"] for config in written] == ["crewfork"]
    assert agent._fork_refresh_failed == frozenset()


def test_the_settled_event_stays_cleared_for_the_whole_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn cannot consume grants a running pass has not re-filtered yet."""
    release = threading.Event()
    entered = threading.Event()
    calls = 0

    def slow_then_fast(*, gated_off: frozenset[str] | None = None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(timeout=10)

    monkeypatch.setattr(agent, "_refresh_forked_templates_locked", slow_then_fast)
    first = threading.Thread(target=agent._refresh_forked_templates)
    first.start()
    try:
        assert entered.wait(timeout=10)
        assert not fork_refresh._fork_refresh_settled.is_set()
    finally:
        release.set()
        first.join(timeout=10)
    assert not first.is_alive()
    assert fork_refresh._fork_refresh_settled.is_set()
    assert agent._fork_refresh_pending == 0
