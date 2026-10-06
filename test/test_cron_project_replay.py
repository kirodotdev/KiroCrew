"""An unbound cron fire must not replay a project-bound run's owner-only turns.

A fresh cron session is seeded from the conversation log, and that replay keeps
no row provenance. After a job's project binding is cleared its output reaches
non-owners, so the replay is withheld whenever the log still carries a
project-bound row.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from kiro_crew.slack.gateway import (
    GatewayOrchestrator,
    _cron_history_override,
    _cron_replay_carries_project_output,
)


class _Log:
    def __init__(self, rows=None, error: Exception | None = None) -> None:
        self._rows = rows or []
        self._error = error
        self.keys: list[str] = []

    def read_messages(self, key: str) -> list[dict]:
        self.keys.append(key)
        if self._error is not None:
            raise self._error
        return list(self._rows)


def _row(role: str, project_bound: object = None) -> dict:
    meta = {} if project_bound is None else {"project_bound": project_bound}
    return {"role": role, "content": "text", "meta": meta}


def _gateway(log: _Log | None) -> SimpleNamespace:
    return SimpleNamespace(ctx_builder=SimpleNamespace(conversation_log=log))


def test_a_project_bound_row_anywhere_in_the_log_withholds_the_replay() -> None:
    log = _Log([_row("user", True), _row("assistant", True)] + [_row("user", False)] * 50)

    assert _cron_replay_carries_project_output(log, "cron:j1") is True
    assert log.keys == ["cron:j1"]


def test_unbound_and_unstamped_rows_keep_the_replay() -> None:
    log = _Log([_row("user", False), _row("assistant", False), _row("user")])

    assert _cron_replay_carries_project_output(log, "cron:j1") is False


def test_an_unreadable_log_withholds_the_replay() -> None:
    assert _cron_replay_carries_project_output(_Log(error=OSError("eio")), "cron:j1") is True


@pytest.mark.asyncio
async def test_an_unbound_fire_after_a_bound_run_suppresses_history() -> None:
    log = _Log([_row("assistant", True)])
    job = SimpleNamespace(project_path="")

    assert await _cron_history_override(_gateway(log), job, "cron:j1") == ""


@pytest.mark.asyncio
async def test_a_bound_fire_keeps_its_own_history_without_reading_the_log() -> None:
    log = _Log([_row("assistant", True)])
    job = SimpleNamespace(project_path="/work/repo")

    assert await _cron_history_override(_gateway(log), job, "cron:j1") is None
    assert log.keys == []


@pytest.mark.asyncio
async def test_an_unbound_job_with_a_clean_log_keeps_the_normal_fallback() -> None:
    job = SimpleNamespace(project_path="")

    assert await _cron_history_override(_gateway(_Log([_row("user", False)])), job, "k") is None
    assert await _cron_history_override(_gateway(None), job, "k") is None


def test_both_cron_fire_paths_pass_the_history_override_to_build_message() -> None:
    source = inspect.getsource(GatewayOrchestrator._init_cron)

    assert source.count("await _cron_history_override(") == 2
    assert source.count("compressed_history=_history_override") == 1
    assert source.count("compressed_history=_seq_history") == 1
