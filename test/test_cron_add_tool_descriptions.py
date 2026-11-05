"""The ``cron_add`` tool describes what its ``command`` and ``timeout`` really do.

A command job runs ``job.command`` as written: the gateway never hands the job's
``message`` to it (``slack/gateway_runtime/cron_exec.py`` calls
``run_command_sandboxed`` with the command text and no message). And the tool's
own schema bounds ``timeout``, so the description states that range, read from
``CRON_ADD_SCHEMA`` here so the two cannot drift.
"""

from __future__ import annotations

import inspect

from kiro_crew.cron_script import run_command_sandboxed
from kiro_crew.mcp_cron_tool_list import _list_tools
from kiro_crew.validation_parts.cron_schemas import CRON_ADD_SCHEMA


def _cron_add_properties() -> dict:
    tool = next(t for t in _list_tools() if t["name"] == "cron_add")
    return tool["inputSchema"]["properties"]


def test_the_command_runner_takes_no_message():
    assert "message" not in inspect.signature(run_command_sandboxed).parameters


def test_the_command_description_does_not_promise_the_message_as_arguments():
    text = _cron_add_properties()["command"]["description"]
    assert "passed as arguments" not in text
    assert "nor passed to the command" in text


def test_the_timeout_description_states_the_schema_range():
    spec = next(s for s in CRON_ADD_SCHEMA.fields if s.name == "timeout")
    text = _cron_add_properties()["timeout"]["description"]
    assert f"{int(spec.min_val)}..{int(spec.max_val)}" in text
