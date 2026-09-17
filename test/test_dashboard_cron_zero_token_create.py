"""POST /api/crons accepts a zero-token (script/command) job with no message.

A ``script=`` or ``command=`` cron is a deterministic wake that bypasses the
LLM, so it carries no prompt. The dashboard create handler historically hard-
required ``message`` ("name and message required"); this relaxes that to
"name and (message OR script OR command)", matching the ``cron_add`` tool and
the store's ``_build_job`` (which requires ``name`` but treats an empty
``message`` as valid). The body is vetted with the same shell/script security
gates the tool path applies, and the resolved ``script``/``command`` is
forwarded to ``add_job_async`` so the persisted job actually runs code.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.dashboard.handlers.cron as cron_handler
from kiro_crew.cron import CronJob
from kiro_crew.dashboard.handlers.cron import api_crons_create
from test.body_stream_helpers import attach_body

pytestmark = pytest.mark.asyncio


def _job(**over) -> CronJob:
    fields = {"id": "j1", "name": "poller", "message": ""}
    fields.update(over)
    return CronJob(**fields)


def _request(body: dict):
    add = AsyncMock(return_value=_job())
    state = SimpleNamespace(
        crons=SimpleNamespace(add_job_async=add),
        push_refresh=MagicMock(),
    )
    request = MagicMock()
    request.app = {"state": state}
    request.get = lambda k, d=None: d  # no internal_auth
    request.headers = {}
    attach_body(request, body)
    return request, add


async def test_command_job_with_no_message_is_accepted(monkeypatch) -> None:
    # Stub the POSIX-strict shell probe (exercised on its own in the Phase-4
    # shell_refused tests): this test pins the message-relaxation contract, not
    # the host's shell, and the probe spawns a sandboxed child that a unit test
    # must not run.
    monkeypatch.setattr(cron_handler, "_resolve_command_shell", lambda: "/bin/sh")
    req, add = _request({"name": "backup", "command": "echo hi", "every": 3600})
    resp = await api_crons_create(req)
    assert resp.status == 200
    # The command reached the store as a real kwarg (job runs code, not an LLM).
    assert add.await_args.kwargs["command"] == "echo hi"
    assert add.await_args.kwargs["script"] == ""
    # message positional stays empty — a zero-token job has no prompt.
    assert add.await_args.args[1] == ""


async def test_script_job_with_no_message_is_accepted(monkeypatch) -> None:
    # The script file need not exist in the test process: stub resolution + vet
    # (both exercised end-to-end in the tool-path tests) so this test pins the
    # HANDLER contract — relax + forward — not the filesystem.
    monkeypatch.setattr(cron_handler, "resolve_script_path", lambda s: ("/tmp/ok.py", "run"))
    monkeypatch.setattr(cron_handler, "_vet_script_file", lambda p: None)
    req, add = _request({"name": "poll", "script": "~/.kiro/crew/crons/x.py:run", "every": 3600})
    resp = await api_crons_create(req)
    assert resp.status == 200
    assert add.await_args.kwargs["script"] == "~/.kiro/crew/crons/x.py:run"
    assert add.await_args.kwargs["command"] == ""


async def test_body_with_name_only_and_no_runnable_is_rejected() -> None:
    req, add = _request({"name": "poller", "every": 3600})
    resp = await api_crons_create(req)
    assert resp.status == 400
    assert "message, script, or command" in json.loads(resp.body)["error"]
    add.assert_not_awaited()


async def test_missing_name_still_rejected_even_with_command() -> None:
    req, add = _request({"command": "echo hi", "every": 3600})
    resp = await api_crons_create(req)
    assert resp.status == 400
    add.assert_not_awaited()


async def test_message_job_unchanged_still_forwards_empty_script_command() -> None:
    # A normal agent (message) job keeps working, with empty script/command.
    req, add = _request({"name": "digest", "message": "Summarize.", "every": 3600})
    resp = await api_crons_create(req)
    assert resp.status == 200
    assert add.await_args.args[1] == "Summarize."
    assert add.await_args.kwargs["script"] == ""
    assert add.await_args.kwargs["command"] == ""


async def test_dangerous_command_is_rejected_before_the_store() -> None:
    # A command the shell-vet deny-lists never reaches add_job_async.
    req, add = _request({"name": "evil", "command": "cat ~/.aws/credentials", "every": 3600})
    resp = await api_crons_create(req)
    assert resp.status == 400
    add.assert_not_awaited()
