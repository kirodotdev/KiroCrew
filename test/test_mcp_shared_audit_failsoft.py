"""An audit that cannot be written must never decide the answer it records.

Under an enforced harness's credential mask the SEL trust root is withheld, so
``sel()`` raises before it writes anything. These audits are diagnostic only, and
two response paths wrote them unguarded: the policy resolver (a masked child turned
"the policy is unresolved" into a broken ``tools/list``) and the tool-call wrapper
(a finished tool's answer was replaced by ``FileExistsError`` on ``trust/``). Both
surfaced as a directory error against a healthy directory, which is what an
operator then chased.
"""

from __future__ import annotations

import email.message
import urllib.error

import pytest

from kiro_crew import mcp_shared_calls, mcp_shared_policy
from kiro_crew.mcp_shared_listing import listable_tools
from kiro_crew.validation import ValidationError


@pytest.fixture(autouse=True)
def _fresh_policy_state(monkeypatch: pytest.MonkeyPatch):
    """Reset the module's process-global negative cache around every test."""
    for name, value in (
        ("_last_failure_time", 0.0),
        ("_last_probe_time", 0.0),
        ("_last_startup_race_time", 0.0),
        ("_last_startup_race_key", ""),
        ("_failure_count", 0),
        ("_last_failure_class", ""),
    ):
        monkeypatch.setattr(mcp_shared_policy, name, value)
    mcp_shared_policy._excluded_tools_by_session.clear()
    yield


def _masked_sel(*_args: object, **_kwargs: object) -> None:
    """What ``sel()`` does in a child the credential mask withholds ``trust/`` from."""
    raise FileExistsError(17, "File exists", "/home/test/.kiro/crew/trust")


def test_an_unwritable_audit_does_not_break_the_unreachable_gateway_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catch-all arm audits a gateway it could not reach; that audit may fail."""

    def _unreachable(*_args: object, **_kwargs: object) -> None:
        raise OSError("gateway unreachable")

    monkeypatch.setattr(mcp_shared_policy, "loopback_urlopen", _unreachable)
    monkeypatch.setattr(mcp_shared_policy, "sel", _masked_sel)

    # pytest.fail rather than letting the exception escape: an escaping
    # FileExistsError is an accident of the bug, not a test judgement, and
    # prove.py credits only the latter when it reverts the fix.
    try:
        policy = mcp_shared_policy._resolve_tool_policy("dashboard:audit-failsoft")
    except OSError as exc:
        pytest.fail(f"an unwritable audit must not become the answer: {exc!r}")

    assert policy.unresolved == "resolution_failed"
    assert policy.excluded == frozenset()


def test_an_unwritable_audit_does_not_break_the_forbidden_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 is the gateway ANSWERING, and its audit may fail the same way."""

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.HTTPError(
            "http://127.0.0.1:5476/api/session-tool-policy",
            403,
            "Forbidden",
            email.message.Message(),
            None,
        )

    monkeypatch.setattr(mcp_shared_policy, "loopback_urlopen", _forbidden)
    monkeypatch.setattr(mcp_shared_policy, "sel", _masked_sel)

    try:
        policy = mcp_shared_policy._resolve_tool_policy("dashboard:audit-forbidden")
    except OSError as exc:
        pytest.fail(f"an unwritable audit must not become the answer: {exc!r}")

    assert policy.unresolved == "policy_forbidden"
    assert policy.excluded == frozenset()


def test_a_masked_sel_still_lists_every_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tools/list`` is not an enforcement point, so it must survive the audit."""

    def _unreachable(*_args: object, **_kwargs: object) -> None:
        raise OSError("gateway unreachable")

    monkeypatch.setattr(mcp_shared_policy, "loopback_urlopen", _unreachable)
    monkeypatch.setattr(mcp_shared_policy, "sel", _masked_sel)

    try:
        listed = listable_tools(
            "kirocrew-dashboard",
            lambda: [{"name": "session_status"}, {"name": "session_read_message"}],
            None,
            applies_policy=True,
        )
    except OSError as exc:
        pytest.fail(f"tools/list must survive an unwritable audit: {exc!r}")

    assert [tool["name"] for tool in listed] == ["session_status", "session_read_message"]


def test_a_masked_sel_does_not_replace_the_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invocation row is written AFTER the tool ran, so its failure must lose."""
    monkeypatch.setattr(mcp_shared_calls, "sel", _masked_sel)

    try:
        result = mcp_shared_calls.call_tool_with_logging(
            "session_status",
            {"slot": "chat-1"},
            lambda _name, raw: raw,
            lambda _name, _args: "the tool's real answer",
            "dashboard:audit-tool",
            "kirocrew-dashboard",
        )
    except OSError as exc:
        pytest.fail(f"the audit must not replace the tool's answer: {exc!r}")

    assert result == "the tool's real answer"


def test_a_masked_sel_does_not_replace_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected call still reports its rejection, not the audit's failure."""

    def _reject(_name: str, _raw: dict[str, object]) -> dict[str, object]:
        raise ValidationError("slot", "is required")

    monkeypatch.setattr(mcp_shared_calls, "sel", _masked_sel)

    try:
        result = mcp_shared_calls.call_tool_with_logging(
            "session_status",
            {},
            _reject,
            lambda _name, _args: "unreachable",
            "dashboard:audit-invalid",
            "kirocrew-dashboard",
        )
    except OSError as exc:
        pytest.fail(f"the audit must not replace the rejection: {exc!r}")

    assert isinstance(result, str)
    assert result.startswith("Error:")
