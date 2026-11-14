"""A degraded governance resolve names the app in its ``governance_degraded`` record.

``audit_governance_degraded`` takes ``app`` so the SEL record can say WHICH
sandboxed app's narrowing was bypassed, and the messaging, channels and
memory chokepoints pass ``app=_governance_app()`` into ``governance_permits``
for exactly that. Both ``governance_permits`` and ``governance_floor_ordinal``
dropped it when they called the audit, so every such record had ``app: ""``.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kiro_crew.platform import governance_profiles as gp


def _boom(*_args, **_kwargs):
    raise RuntimeError("transient profile-load error")


@pytest.mark.parametrize(
    "call",
    [
        lambda: gp.governance_permits(
            "channels", "slack", session_key="slack:T1:C1", app="file-explorer", log_warning=False
        ),
        lambda: gp.governance_floor_ordinal(
            "sandbox.min_level", session_key="slack:T1:C1", app="file-explorer", log_warning=False
        ),
    ],
    ids=["governance_permits", "governance_floor_ordinal"],
)
def test_the_degraded_record_names_the_app(monkeypatch, call):
    monkeypatch.setattr(gp, "resolve_active_scope", _boom)
    fake_sel = mock.MagicMock()
    with mock.patch("kiro_crew.sel.sel", return_value=fake_sel):
        call()

    fake_sel.log_governance_degraded.assert_called_once()
    assert fake_sel.log_governance_degraded.call_args.kwargs.get("app") == "file-explorer"


def test_the_spawn_gate_degraded_record_names_the_app(monkeypatch):
    # The spawn gate fails closed and audits its own degrade; it has the app too.
    from kiro_crew.subagent_parts import agent_policy

    monkeypatch.setattr(gp, "governance_permits", _boom)
    fake_sel = mock.MagicMock()
    with mock.patch("kiro_crew.sel.sel", return_value=fake_sel):
        reason = agent_policy._vet_spawn_governance("slack:T1:C1", "helper", app="file-explorer")

    assert reason is not None
    fake_sel.log_governance_degraded.assert_called_once()
    assert fake_sel.log_governance_degraded.call_args.kwargs.get("app") == "file-explorer"
