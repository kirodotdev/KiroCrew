"""A tool gate that raises refuses the call and says why.

The gate parses untrusted command text. When a parser raised on input nobody
anticipated (a NUL byte once made the inline-payload lexer raise
``SystemError``), the exception escaped the gate and each
caller handled it its own way; on the dashboard the refused call was reported
as aborted by the user. These tests pin the one shared outcome: a security
deny whose reason names the crash.
"""

from __future__ import annotations

import inspect

import pytest

from kiro_crew import hooks as hooks_mod
from kiro_crew.hooks import TOOL_DENY, HookManager
from kiro_crew.platform.context import PlatformCompositionError


def _crash_the_deny_tier(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """Make the security authority's deny check raise ``exc``."""

    def boom(*_args: object, **_kwargs: object) -> str | None:
        raise exc

    authority = hooks_mod.current_context().security
    monkeypatch.setattr(authority, "is_denied", boom)


def test_a_crashing_gate_refuses_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _crash_the_deny_tier(monkeypatch, SystemError("returned a result with an exception set"))
    result = HookManager().on_tool_call("Running: ls", command="ls", is_shell=True)
    assert result.action == TOOL_DENY
    assert result.security_deny is True


def test_the_refusal_names_the_crash_not_a_user_action(monkeypatch: pytest.MonkeyPatch) -> None:
    _crash_the_deny_tier(monkeypatch, SystemError("boom"))
    reason = HookManager().on_tool_call("Running: ls", command="ls", is_shell=True).reason
    assert "safety check crashed" in reason
    assert "SystemError" in reason
    assert "not a user action" in reason


def test_the_refusal_matches_the_sentence_the_dashboard_keys_on() -> None:
    # The dashboard's Output panel (`denyReason.ts` GATE_CRASH_NOTICE) finds this
    # reason on the `🚫 <title> — <reason>` row by matching this exact sentence
    # at the row's end, since the reason carries no DENY_REASON_PREFIX on purpose
    # (no policy rule fired) and the title is model-authored. Rewording it here
    # without updating that regex makes the panel fall back to a localized
    # "blocked by security policy" line -- the exact claim this sentence disclaims.
    assert hooks_mod.GATE_CRASH_REASON == (
        "Blocked: the safety check crashed while judging this call ({error}), so the "
        "call was refused and nothing ran. This is a Kiro Crew bug, not a policy rule "
        "and not a user action."
    )
    assert "Blocked by security policy" not in hooks_mod.GATE_CRASH_REASON


def test_a_composition_error_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mis-composed host is re-raised on purpose; this change must not swallow it.
    _crash_the_deny_tier(monkeypatch, PlatformCompositionError("no platform"))
    with pytest.raises(PlatformCompositionError):
        HookManager().on_tool_call("Running: ls", command="ls", is_shell=True)


def test_the_wrapper_keeps_the_gate_signature_visible() -> None:
    # ``functools.wraps`` keeps the judged call's signature readable through the
    # wrapper, so ``inspect`` and the type checker see the real parameters.
    assert list(inspect.signature(HookManager.judge).parameters)[:2] == ["self", "call"]
    params = inspect.signature(HookManager.on_tool_call).parameters
    assert "command" in params and "classifier_only" in params
