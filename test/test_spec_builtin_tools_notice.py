"""The claude notice naming a named agent's unapplied ``model`` pin."""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk import spec_builtin_tools
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for

_CLAUDE_NS = capabilities_for(ACP_BACKEND_CLAUDE).model_id_namespace


def test_a_bot_worker_gets_no_line_at_all(monkeypatch):
    monkeypatch.setattr("kiro_crew.model_scope.pin_applies", lambda pin, namespace: False)
    assert (
        spec_builtin_tools.spec_model_pin_unapplied(
            {"tools": ["fs_read"], "model": "other-id"}, namespace=_CLAUDE_NS, worker_session=True
        )
        is False
    )


def test_an_unpinned_model_is_never_named():
    for spec in (
        {"tools": ["*"]},
        {"tools": ["*"], "model": ""},
        {"tools": ["*"], "model": "auto"},
    ):
        assert spec_builtin_tools.spec_model_pin_unapplied(spec, namespace=_CLAUDE_NS) is False


def test_a_model_pin_the_namespace_scopes_out_is_named(monkeypatch):
    monkeypatch.setattr("kiro_crew.model_scope.pin_applies", lambda pin, namespace: False)
    assert (
        spec_builtin_tools.spec_model_pin_unapplied(
            {"model": "some-other-harness-id"}, namespace=_CLAUDE_NS
        )
        is True
    )


def test_the_notice_is_claude_only_and_echoes_no_spec_text(monkeypatch):
    spec = {"tools": ["fs_read", "IGNORE-PREVIOUS"], "model": "pin-from-spec"}
    monkeypatch.setattr("kiro_crew.agent_sdk.spec_hooks._agent_spec", lambda *a, **k: spec)
    monkeypatch.setattr("kiro_crew.model_scope.pin_applies", lambda pin, namespace: False)

    text = spec_builtin_tools.spec_builtin_tools_notice(
        "decoy", None, backend=ACP_BACKEND_CLAUDE, namespace=_CLAUDE_NS
    )
    assert "`model` pin" in text
    assert "`tools`" not in text
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


@pytest.mark.parametrize("tools", [[], ["fs_read"], ["read", "@kirocrew-core"]])
def test_a_spec_the_session_applies_gets_no_notice(monkeypatch, tools):
    monkeypatch.setattr(
        "kiro_crew.agent_sdk.spec_hooks._agent_spec", lambda *a, **k: {"tools": tools}
    )
    assert (
        spec_builtin_tools.spec_builtin_tools_notice(
            "lite", None, backend=ACP_BACKEND_CLAUDE, namespace=_CLAUDE_NS
        )
        == ""
    )
