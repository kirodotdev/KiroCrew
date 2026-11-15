"""The claude notice naming a named agent's unapplied ``tools`` limit and ``model`` pin."""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk import spec_builtin_tools
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.agent_sdk.claude_worker_tools import CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL

_CLAUDE_NS = capabilities_for(ACP_BACKEND_CLAUDE).model_id_namespace
_EVERY_ROW = list(CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL)


def _ungranted(tools):
    spec = {} if tools is None else {"tools": tools}
    ungranted, _ = spec_builtin_tools.spec_unapplied_builtin_limits(spec, namespace=_CLAUDE_NS)
    return ungranted


def test_a_read_only_list_names_the_shell_and_write_tools():
    ungranted = _ungranted(["fs_read"])
    assert "execute_bash" in ungranted and "fs_write" in ungranted
    assert "fs_read" not in ungranted


@pytest.mark.parametrize(
    "tools",
    [["read", "*"], ["@builtin"], ["@builtin/*"], [], [1], None, "read", _EVERY_ROW],
)
def test_no_builtin_is_named_when_the_list_limits_none(tools):
    assert _ungranted(tools) == ()


def test_a_bot_worker_gets_no_line_at_all(monkeypatch):
    monkeypatch.setattr("kiro_crew.model_scope.pin_applies", lambda pin, namespace: False)
    assert spec_builtin_tools.spec_unapplied_builtin_limits(
        {"tools": ["fs_read"], "model": "other-id"}, namespace=_CLAUDE_NS, worker_session=True
    ) == ((), False)


def test_an_unpinned_model_is_never_named():
    for spec in (
        {"tools": ["*"]},
        {"tools": ["*"], "model": ""},
        {"tools": ["*"], "model": "auto"},
    ):
        _, unapplied = spec_builtin_tools.spec_unapplied_builtin_limits(spec, namespace=_CLAUDE_NS)
        assert unapplied is False


def test_a_model_pin_the_namespace_scopes_out_is_named(monkeypatch):
    monkeypatch.setattr("kiro_crew.model_scope.pin_applies", lambda pin, namespace: False)
    _, unapplied = spec_builtin_tools.spec_unapplied_builtin_limits(
        {"model": "some-other-harness-id"}, namespace=_CLAUDE_NS
    )
    assert unapplied is True


def test_the_notice_is_claude_only_and_echoes_no_spec_text(monkeypatch):
    spec = {"tools": ["fs_read", "IGNORE-PREVIOUS"], "model": "pin-from-spec"}
    monkeypatch.setattr("kiro_crew.agent_sdk.spec_hooks._agent_spec", lambda *a, **k: spec)
    monkeypatch.setattr("kiro_crew.model_scope.pin_applies", lambda pin, namespace: False)

    text = spec_builtin_tools.spec_builtin_tools_notice(
        "decoy", None, backend=ACP_BACKEND_CLAUDE, namespace=_CLAUDE_NS
    )
    assert "does not apply the spec's `tools` list" in text and "full built-in" in text
    assert "`model` pin" in text
    assert "IGNORE-PREVIOUS" not in text and "pin-from-spec" not in text

    crew_written = ("kirocrew", "kirocrew-worker", "kirocrew-conductor")
    for backend, agent in (
        (ACP_BACKEND_KIRO, "decoy"),
        *((ACP_BACKEND_CLAUDE, a) for a in crew_written),
    ):
        assert (
            spec_builtin_tools.spec_builtin_tools_notice(
                agent, None, backend=backend, namespace=_CLAUDE_NS
            )
            == ""
        )


def test_a_spec_the_session_applies_gets_no_notice(monkeypatch):
    monkeypatch.setattr("kiro_crew.agent_sdk.spec_hooks._agent_spec", lambda *a, **k: {"tools": []})
    assert (
        spec_builtin_tools.spec_builtin_tools_notice(
            "lite", None, backend=ACP_BACKEND_CLAUDE, namespace=_CLAUDE_NS
        )
        == ""
    )
