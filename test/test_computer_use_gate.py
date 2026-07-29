"""The computer-use gate after the governance removal (``computer_use/gate.py``).

This file used to be ~1650 lines pinning eight governance scopes, an
unattended-surface refusal, an interactive-approval floor and an observation
ceiling. All of that is gone by product decision: computer use is ONE operator
opt-in, and after that the agent drives the desktop the way the operator would.

What is left to pin is small but worth pinning, because each item is a place where
a future edit could quietly reintroduce a refusal (or lose the audit):

* the gate PERMITS — including on surfaces that used to be refused outright
  (cron, subagent, taskrunner), which is the behaviour change users will actually
  notice;
* every call is AUDITED, since with the ceiling gone the SEL trail is the
  operator's only record of what the agent did to their desktop;
* the observation ceiling is a pass-through, so a renderer cannot lose fields to
  it;
* the one retained refusal (KiroCrew's own window) lives in ``policy``, NOT here —
  asserted so nobody moves it back into the gate.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.computer_use import gate
from kiro_crew.computer_use.types import (
    ALL_OBSERVATION_CHANNELS,
    ALL_TOOLS,
    TOOL_CLICK,
    TOOL_GET_STATE,
    TOOL_LIST_APPS,
)

# Surfaces that USED to be refused before any governance was even consulted. They
# are the headline behaviour change, so they are parametrized rather than asserted
# once — a partial revert would show up as one of these failing.
_FORMERLY_REFUSED = ("cron:nightly", "subagent:abc123", "taskrunner", "_bg", "_hb", "")


class TestTheGatePermits:
    @pytest.mark.parametrize("session_key", _FORMERLY_REFUSED)
    def test_every_formerly_unattended_surface_is_allowed(self, session_key):
        """The unattended-surface refusal is gone.

        A cron job driving the desktop is now a supported flow: the keystone enable
        is the operator saying yes, and it does not distinguish which surface asked.
        """
        assert (
            gate.require_computer_use(
                TOOL_CLICK, session_key=session_key, app_bundle_id="com.apple.Preview"
            )
            is None
        )

    @pytest.mark.parametrize("tool", ALL_TOOLS)
    def test_no_tool_is_refused(self, tool):
        assert gate.require_computer_use(tool, session_key="dashboard:main") is None

    def test_an_unresolvable_app_identity_is_allowed(self):
        """``requires_app_identity`` no longer refuses.

        It used to be the "an app we cannot name cannot be authorized" rule. With
        no per-app axes there is nothing to authorize against, and refusing would
        only break windows whose bundle id the OS did not report.
        """
        assert (
            gate.require_computer_use(
                TOOL_GET_STATE, session_key="dashboard:main", requires_app_identity=True
            )
            is None
        )

    def test_a_mutator_is_allowed_without_a_recorded_approval(self):
        """The ``interactive`` approval floor is gone.

        Previously this returned a refusal unless ``approval_recorded=True``, which
        made the policy row observation-only in practice. Both the row and the
        parameter's effect are removed; the parameter itself is kept for signature
        stability.
        """
        assert (
            gate.require_computer_use(
                TOOL_CLICK,
                session_key="dashboard:main",
                app_bundle_id="com.apple.Preview",
                approval_recorded=False,
            )
            is None
        )


class TestThePointerPath:
    def test_the_pointer_is_allowed_by_default(self):
        """One enable covers it — the separate ``allow_pointer_move`` opt-in is gone.

        The model must still NAME ``click_method: "global"`` for the pointer to move
        at all (``policy.resolve_click_method`` never resolves ``auto`` to it), so
        this does not make an ordinary click warp the cursor.
        """
        assert (
            gate.require_pointer_move(TOOL_CLICK, method="global", session_key="dashboard:main")
            is None
        )

    def test_an_in_process_caller_can_still_refuse_locally(self):
        """``pointer_enabled=False`` is retained as an opt-out for a direct caller."""
        assert (
            gate.require_pointer_move(
                TOOL_CLICK, method="global", session_key="dashboard:main", pointer_enabled=False
            )
            is not None
        )

    def test_a_pointer_gesture_is_audited_with_its_method(self):
        """The one record that says the operator's physical cursor was moved."""
        with patch("kiro_crew.sel.sel") as sel_factory:
            recorder = MagicMock()
            sel_factory.return_value = recorder
            gate.audit_pointer_move(
                TOOL_CLICK, method="global", session_key="dashboard:main", app_label="Preview"
            )
        kwargs = recorder.log_tool_invocation.call_args.kwargs
        assert "global" in kwargs["resources"]
        assert "Preview" in kwargs["resources"]


class TestEveryCallIsAudited:
    """With the ceiling gone, the audit trail is the remaining accountability."""

    def test_an_allowed_call_records_the_tool_and_the_app(self):
        with patch("kiro_crew.sel.sel") as sel_factory:
            recorder = MagicMock()
            sel_factory.return_value = recorder
            gate.require_computer_use(
                TOOL_CLICK, session_key="dashboard:main", app_bundle_id="com.apple.Preview"
            )
        kwargs = recorder.log_tool_invocation.call_args.kwargs
        assert TOOL_CLICK in kwargs["tool_name"]
        assert kwargs["tool_kind"] == "computer_use"
        assert kwargs["session_key"] == "dashboard:main"
        assert "com.apple.Preview" in kwargs["resources"]

    def test_an_audit_failure_never_breaks_the_call(self):
        """Best-effort: a wedged SEL must not turn a permitted action into an error."""
        with patch("kiro_crew.sel.sel") as sel_factory:
            recorder = MagicMock()
            recorder.log_tool_invocation.side_effect = RuntimeError("sel is down")
            sel_factory.return_value = recorder
            assert gate.require_computer_use(TOOL_CLICK, session_key="dashboard:main") is None


class TestObservationsArePassedThrough:
    def test_every_channel_is_permitted(self):
        assert gate.permitted_observation_channels(session_key="dashboard:main") == frozenset(
            ALL_OBSERVATION_CHANNELS
        )

    def test_the_ceiling_does_not_alter_a_payload(self):
        """A renderer must not lose fields to a ceiling that no longer narrows.

        Uses the real payload keys, so a future edit that re-adds narrowing without
        updating the renderers fails here rather than silently blanking output.
        """
        payload = {
            gate.PAYLOAD_TEXT: "hello",
            gate.PAYLOAD_WINDOW_TITLE: "Documents",
            gate.PAYLOAD_ELEMENTS: ({gate.ELEMENT_TITLE_KEY: "Save"},),
            gate.PAYLOAD_SCREENSHOT: "/tmp/shot.jpeg",
            gate.PAYLOAD_NOTES: (),
        }
        assert gate.apply_observation_ceiling(payload, session_key="dashboard:main") == payload

    def test_the_targets_axis_reports_ungoverned(self):
        """``tools`` reads this to decide whether to DEMAND an element index.

        False means indexless keyboard input and coordinate gestures are both
        allowed again.
        """
        assert gate.targets_axis_is_governed(session_key="dashboard:main") is False


class TestAppDisclosure:
    def test_an_app_with_an_identity_is_disclosable(self):
        assert (
            gate.app_is_disclosable(bundle_id="com.apple.Terminal", display_name="Terminal") is True
        )

    def test_an_app_with_NEITHER_identity_is_not(self):
        """Nothing to show, rather than a policy refusal."""
        assert gate.app_is_disclosable(bundle_id="", display_name="") is False

    def test_a_formerly_denylisted_app_is_now_disclosable(self):
        """Terminals and password managers are no longer hidden from the list."""
        for bundle in ("com.apple.Terminal", "com.1password.app", "com.apple.systempreferences"):
            assert gate.app_is_disclosable(bundle_id=bundle, display_name="") is True


class TestTheOneRetainedRefusalIsNotHere:
    def test_kirocrew_self_is_refused_by_policy_not_by_the_gate(self):
        """The keystone's un-reachability is what that refusal protects.

        It lives in ``policy.check_app`` because that is the layer with the resolved
        ``AppRef``. Asserted from here so a future edit does not move it back into a
        gate that no longer makes decisions — and so the invariant itself has a test
        that names it.
        """
        from kiro_crew.computer_use import policy
        from kiro_crew.computer_use.types import AppRef, PolicyConfig

        ours = AppRef(name="Kiro Crew", pid=1, bundle_id="dev.kiro.crew")
        assert policy.check_app(ours, PolicyConfig()) is not None
        # And the gate itself has no opinion about it.
        assert gate.require_computer_use(TOOL_LIST_APPS, session_key="dashboard:main") is None

    def test_a_terminal_is_no_longer_refused_by_policy(self):
        from kiro_crew.computer_use import policy
        from kiro_crew.computer_use.types import AppRef, PolicyConfig

        term = AppRef(name="Terminal", pid=1, bundle_id="com.apple.Terminal")
        assert policy.check_app(term, PolicyConfig()) is None
