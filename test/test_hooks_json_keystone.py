"""The hook store is executable state, so the agent's file tools must not reach it."""

from __future__ import annotations

from kiro_crew import sandbox
from kiro_crew.security import _CREW_SECRET_LEAVES, is_sensitive_path


def test_hooks_json_is_fenced_on_both_home_spellings() -> None:
    assert "hooks.json" in _CREW_SECRET_LEAVES
    assert is_sensitive_path("~/.kiro/crew/hooks.json") is True
    assert is_sensitive_path("~/.kirocrew/hooks.json") is True


def test_hooks_json_artifacts_are_fenced() -> None:
    # ``atomic_write`` publishes beside the leaf, so the lock inherits the fence.
    assert is_sensitive_path("~/.kiro/crew/hooks.json.lock") is True


def test_hooks_json_stays_writable_for_in_sandbox_registration() -> None:
    # ``register_hook`` writes the store from inside the sandbox, directly rather
    # than through the gate, so a mask would break hook registration.
    assert "hooks.json" in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
    assert "hooks.json" not in sandbox._CREW_HIDDEN_LEAVES
    # A foreign harness's child gets no read of the commands the host trusts.
    assert "hooks.json" in sandbox._CREW_CHILD_WITHHELD_LEAVES
