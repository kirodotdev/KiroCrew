"""kiro-cli's ``_kiro/customAgent/config_error``: logged once per process, never a drop.

The notification names an agent file KAS skipped. It arrives before the
``session/new`` response, so the session it names is not registered. Counted as an
unroutable frame, it would put the file and the reason in no log.
"""

import logging

import pytest
from acp_runtime_helpers import _drain, _feed, _make_runtime, _register, _start_reader, _stop_reader

from kiro_crew.acp import kas_agent_config_error
from kiro_crew.acp.kas_agent_config_error import METHOD_KAS_CUSTOM_AGENT_CONFIG_ERROR
from kiro_crew.testing.synthetic_credentials import github_classic

_PARAMS = {
    "path": "/home/u/.kiro/agents/bad.json",
    "reasonCode": "invalid_config",
    "error": "PromptFileError: Absolute file:// URIs are not allowed in agent prompts",
}


def _frame(session_id: str, **params) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": METHOD_KAS_CUSTOM_AGENT_CONFIG_ERROR,
        "params": {"sessionId": session_id, **_PARAMS, **params},
    }


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "could not load agent config" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_config_error_is_warned_once_and_never_counted_as_a_drop(caplog):
    rt, reader, _ = _make_runtime()
    queues = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.DEBUG):
            # Two session starts on one runtime, then a second runtime: each
            # session/new repeats the same report about the same file.
            _feed(reader, _frame("sess-new-1"))
            _feed(reader, _frame("sA"))
            await _drain(reader)
            rt2, reader2, _ = _make_runtime()
            task2 = await _start_reader(rt2)
            try:
                _feed(reader2, _frame("sess-new-2"))
                await _drain(reader2)
            finally:
                await _stop_reader(task2)
        assert rt._dropped_frames == {}
        assert rt2._dropped_frames == {}
        # Host state about a shared file, not session output.
        assert queues["sA"].empty()
        warnings = _warnings(caplog)
        assert len(warnings) == 1, warnings
        for part in _PARAMS.values():
            assert part in warnings[0]
    finally:
        await _stop_reader(task)


def test_a_new_error_for_the_same_file_is_warned_again(caplog):
    with caplog.at_level(logging.WARNING):
        assert kas_agent_config_error.report(_PARAMS) is True
        assert kas_agent_config_error.report({**_PARAMS, "error": "missing field `name`"})
    assert len(_warnings(caplog)) == 2


def _cap_warnings(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if "distinct agent config errors" in r.getMessage()
    ]


def test_reports_past_the_cap_are_refused_and_the_cap_is_warned_once(caplog, monkeypatch):
    monkeypatch.setattr(kas_agent_config_error, "_MAX_REPORTED", 1)
    with caplog.at_level(logging.DEBUG):
        assert kas_agent_config_error.report(_PARAMS) is True
        for name in ("other.json", "third.json"):
            refused = {**_PARAMS, "path": f"/home/u/.kiro/agents/{name}"}
            assert kas_agent_config_error.report(refused) is False
    assert len(_warnings(caplog)) == 1
    assert len(_cap_warnings(caplog)) == 1
    # A refused report gets no row, so the set stays at the cap.
    assert len(kas_agent_config_error._reported) == 1
    assert "other.json" not in caplog.text


@pytest.mark.asyncio
async def test_a_report_past_the_cap_is_a_counted_drop(monkeypatch):
    monkeypatch.setattr(kas_agent_config_error, "_MAX_REPORTED", 0)
    rt, reader, _ = _make_runtime()
    task = await _start_reader(rt)
    try:
        _feed(reader, _frame("sess-new-1"))
        await _drain(reader)
        assert rt._dropped_frames == {("sess-new-1", METHOD_KAS_CUSTOM_AGENT_CONFIG_ERROR): 1}
    finally:
        await _stop_reader(task)


def test_backend_text_is_redacted_and_odd_params_do_not_raise(caplog):
    token = github_classic()
    with caplog.at_level(logging.WARNING):
        kas_agent_config_error.report({**_PARAMS, "error": f"bad token {token}"})
        kas_agent_config_error.report(None)
        kas_agent_config_error.report({"path": 7, "error": ["x"]})
    assert token not in caplog.text
    assert len(_warnings(caplog)) == 3
