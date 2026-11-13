"""What the SEL record of a delegated kiro-cli spawn claims about isolation.

On Windows the delegation is granted by the caller's ``is_kiro_cli=True`` classification
alone: it deliberately does not read kiro-cli's internal-sandbox setting, because a
fresh install has no settings file (``test_windows_fail_closed_optin.py`` pins that).
So the Windows record must not claim kiro-cli's sandbox is on. The macOS branch only
delegates after reading that setting as true, so its record may say so.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from kiro_crew import sandbox


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    # Patched through the ``kiro_crew.sandbox`` facade, which mirrors each name onto
    # every owner module that binds it.
    log = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: log)
    monkeypatch.setattr(sandbox, "_kiro_delegation_warned", True)
    monkeypatch.setattr(sandbox, "_sandbox_env_unset_args", lambda *args: [])
    return log


def _delegated_resources(audit: MagicMock) -> str:
    (call,) = audit.log_tool_invocation.call_args_list
    assert call.kwargs["outcome"] == "delegated"
    assert call.kwargs["critical"] is True
    return call.kwargs["resources"]


def test_windows_delegation_record_does_not_claim_isolation(
    audit: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    argv = ["kiro-cli", "acp"]

    assert sandbox._delegate_to_kiro_internal_sandbox(argv, "standard") == (argv, None)

    resources = _delegated_resources(audit)
    assert "kiro_internal_sandbox unconfirmed" in resources
    assert "owns" not in resources


def test_macos_delegation_record_keeps_its_confirmed_wording(
    audit: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    argv = ["kiro-cli", "acp"]

    assert sandbox._delegate_to_kiro_internal_sandbox(argv, "standard") == (argv, None)

    resources = _delegated_resources(audit)
    assert "kiro internal sandbox on" in resources
    assert "unconfirmed" not in resources
