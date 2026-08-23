"""In-band delivery of a tool-deny reason (steer-before-reject).

kiro-cli reports every rejected permission to the model as the fixed tool result
"User denied tool execution" — ACP's permission response carries only
``outcome``/``optionId``, so the host has no protocol field for a reason. The
agent therefore concluded the USER cancelled and yielded, and the reason only
reached it via a second, billed recovery turn.

These tests pin the primary path that removes that second turn: the deny site
steers a policy notice into the still-running turn BEFORE answering the
permission request, and the recovery continuation degrades to a fallback that
fires only when the notice could not be delivered.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.chat_runner import (
    _refined_tool_row_content,
    _reject_hook_blocked,
    _steer_policy_notice,
)
from kiro_crew.dashboard.state import (
    build_refusal_steer_notice,
    should_queue_refusal_recovery,
)


class _SteerClient:
    """Minimal permission-answering double recording steer/reject ORDER.

    Order is the mechanism under test, not an implementation detail: the steer
    must be written while the permission request is still unanswered, because
    that is what proves the turn is in flight and gets the notice queued instead
    of dropped.
    """

    def __init__(self, *, supports_steer: bool = True, steer_result: bool = True):
        self.supports_steer = supports_steer
        self._steer_result = steer_result
        self.calls: list[str] = []
        self.steered: list[str] = []

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        self.steered.append(message)
        return self._steer_result

    async def reject_tool(self, request_id) -> None:
        self.calls.append("reject")


class _RaisingSteerClient(_SteerClient):
    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        raise RuntimeError("transport gone")


class _Slot:
    """Enough of a slot for the reject helper: it appends one blocked row."""

    def __init__(self):
        self.agent = "kirocrew"
        self.rows: list[tuple[str, str]] = []
        self._app = ""

    def append(self, role, content, cls, **kw):
        self.rows.append((role, content))


class _Event:
    request_id = 7
    title = "bash"
    tool_kind = "execute"


class TestBuildRefusalSteerNotice:
    """The notice has to overwrite a conclusion the model already holds."""

    def test_names_the_generic_string_it_is_correcting(self):
        out = build_refusal_steer_notice("bash", "denied by policy")
        # Without naming kiro-cli's own wording the model has two claims and no
        # reason to prefer ours.
        assert "User denied tool execution" in out

    def test_attributes_the_block_to_policy_not_the_user(self):
        out = build_refusal_steer_notice("bash", "denied by policy")
        assert "NOT a user action" in out
        assert "did not cancel" in out

    def test_carries_title_and_reason(self):
        out = build_refusal_steer_notice("bash", "unsafe shell pattern")
        assert "bash" in out
        assert "unsafe shell pattern" in out

    def test_directs_the_model_to_continue_in_the_same_turn(self):
        # The whole point is avoiding a second turn, so the notice must not
        # invite the model to hand the decision back to the user.
        out = build_refusal_steer_notice("bash", "denied")
        assert "same turn" in out
        assert "do not ask the user" in out.lower()

    def test_title_only_still_produces_a_notice(self):
        assert "some_tool" in build_refusal_steer_notice("some_tool", "")

    def test_blank_input_yields_empty_so_caller_falls_back(self):
        assert build_refusal_steer_notice("", "") == ""
        assert build_refusal_steer_notice("   ", "  ") == ""


class TestSteerPolicyNotice:
    @pytest.mark.asyncio
    async def test_records_the_notice_it_wrote(self):
        client = _SteerClient()
        notices: list[str] = []
        assert await _steer_policy_notice(client, "bash", "denied by policy", notices) is True
        assert client.calls == ["steer"]
        assert notices and "User denied tool execution" in notices[0]

    @pytest.mark.asyncio
    async def test_backend_without_steer_writes_nothing(self):
        client = _SteerClient(supports_steer=False)
        notices: list[str] = []
        assert await _steer_policy_notice(client, "bash", "denied", notices) is False
        assert client.calls == []
        assert notices == []

    @pytest.mark.asyncio
    async def test_client_missing_the_attribute_is_treated_as_unsupported(self):
        class _Bare:
            async def steer(self, message: str) -> bool:  # pragma: no cover - never reached
                raise AssertionError("must not be called")

        notices: list[str] = []
        assert await _steer_policy_notice(_Bare(), "bash", "denied", notices) is False
        assert notices == []

    @pytest.mark.asyncio
    async def test_refused_steer_is_not_recorded(self):
        # A False return means nothing was written, so recording it would
        # suppress the fallback for a notice the model never received.
        client = _SteerClient(steer_result=False)
        notices: list[str] = []
        assert await _steer_policy_notice(client, "bash", "denied", notices) is False
        assert notices == []

    @pytest.mark.asyncio
    async def test_transport_failure_degrades_instead_of_raising(self):
        # Steering is an optimisation over a working fallback: it must never
        # turn a clean policy block into a turn error.
        client = _RaisingSteerClient()
        notices: list[str] = []
        assert await _steer_policy_notice(client, "bash", "denied", notices) is False
        assert notices == []

    @pytest.mark.asyncio
    async def test_blank_reason_and_title_sends_no_steer(self):
        client = _SteerClient()
        notices: list[str] = []
        assert await _steer_policy_notice(client, "", "", notices) is False
        assert client.calls == []


class TestRejectHookBlockedOrdering:
    """The steer must be written while the permission request is unanswered.

    This is the load-bearing ordering in the whole change: measured against
    kiro-cli 2.19.1, a steer queued BEFORE the rejection is folded in at the
    boundary after the tool fails (``AgentExecutionSteeringInjected``), while the
    turn moving on first leaves nothing to fold into.
    """

    @pytest.mark.asyncio
    async def test_steer_precedes_reject(self):
        client = _SteerClient()
        notices: list[str] = []
        await _reject_hook_blocked(
            client,
            _Slot(),
            _Event(),
            session_key="s",
            pre_hook_results=["BLOCKED: unsafe shell pattern"],
            refusal_reasons=[],
            refusal_notices=notices,
        )
        assert client.calls == ["steer", "reject"]
        assert notices

    @pytest.mark.asyncio
    async def test_reject_still_happens_when_no_notice_list_is_passed(self):
        # Fallback-only callers (and this helper's own older tests) must keep
        # denying the tool; in-band delivery is additive, never a precondition.
        client = _SteerClient()
        reasons: list[tuple[str, str]] = []
        await _reject_hook_blocked(
            client,
            _Slot(),
            _Event(),
            session_key="s",
            pre_hook_results=["BLOCKED: unsafe shell pattern"],
            refusal_reasons=reasons,
        )
        assert client.calls == ["reject"]
        assert reasons and reasons[0][0] == "bash"

    @pytest.mark.asyncio
    async def test_reject_still_happens_when_the_steer_fails(self):
        client = _RaisingSteerClient()
        reasons: list[tuple[str, str]] = []
        await _reject_hook_blocked(
            client,
            _Slot(),
            _Event(),
            session_key="s",
            pre_hook_results=["BLOCKED: unsafe shell pattern"],
            refusal_reasons=reasons,
            refusal_notices=[],
        )
        # The block is a security decision; a failed optimisation cannot skip it.
        assert client.calls == ["steer", "reject"]
        assert reasons


class TestFloorDenialExplainsItself:
    """A floor hit must not hand the model a pattern the input cannot match.

    The refusal's first line names the rule's catalog regex so reason and SEL
    event map back to a rule id. For an argv-structural hit that regex is
    routinely NOT what matched, and since the reason is now steered to the model
    in-band, a bare misleading identifier misdirects the agent's next attempt.
    """

    MINT_PATTERN_TAIL = "\\btoken\\b"

    def _deny(self, command: str) -> str:
        from kiro_crew.security import is_denied

        return is_denied(command) or ""

    def test_inline_import_is_denied_at_all(self):
        # Guards the premise of every assertion below.
        assert self._deny('python -c "import kiro_crew"')

    def test_reported_pattern_cannot_match_the_command(self):
        # The exact trap: the first line requires a `token` word this command
        # does not contain, so the identifier alone reads as a false reason.
        command = 'python -c "import kiro_crew"'
        first_line = self._deny(command).splitlines()[0]
        assert self.MINT_PATTERN_TAIL in first_line
        assert "token" not in command

    def test_second_line_says_the_match_was_structural(self):
        lines = self._deny('python -c "import kiro_crew"').splitlines()
        assert len(lines) >= 2, "floor denial must carry an explanation line"
        assert "structurally" in lines[1]
        assert "argv" in lines[1]

    def test_explanation_names_the_import_gate(self):
        # What the agent needs in order to adapt: it is the IMPORT that is
        # gated, so retrying with a differently-worded command is futile.
        note = self._deny('python -c "import kiro_crew"').splitlines()[1]
        assert "import" in note

    def test_first_line_stays_single_line_and_prefixed(self):
        # RecoveryCard.tsx extracts the pattern with a per-line end-anchored
        # regex, so anything appended to line 1 would be read as the pattern.
        out = self._deny('python -c "import kiro_crew"')
        assert out.startswith("Blocked by security policy: ")
        assert "\n" not in out.splitlines()[0]

    def test_regex_tier_denial_carries_no_explanation_line(self):
        # Unchanged for a real pattern match: there the identifier IS accurate.
        out = self._deny("rm -rf /")
        assert out
        assert len(out.splitlines()) == 1


class TestRefusalRowKeepsItsReason:
    """A later title refinement must not erase a refusal row's explanation.

    kiro-cli sends a ``tool_call_update`` carrying the resolved title after the
    permission is answered. The refinement rewrites the row as
    ``f"{icon} {title}"``, which for a refusal row silently deletes the
    ``— <reason>`` tail the user's only visible explanation lives in — while the
    model HAS been told in-band, producing the worst split: the human sees a
    blocked row with no reason and the agent acts on one they cannot see.
    """

    def test_refusal_row_is_left_alone(self):
        assert (
            _refined_tool_row_content(
                "🚫 Running: bash -c x — Blocked by security policy: rule\nwhy", "bash -c x"
            )
            is None
        )

    def test_running_row_is_still_refined(self):
        # The refinement is useful on a live row; only refusals are exempt.
        assert _refined_tool_row_content("🔧 old title", "new title") == "🔧 new title"

    def test_completed_row_is_still_refined(self):
        assert _refined_tool_row_content("✅ old title", "new title") == "✅ new title"

    def test_unprefixed_row_gets_the_running_icon(self):
        assert _refined_tool_row_content("bare text", "new title") == "🔧 new title"


class TestRecoveryIsNowAFallback:
    """The extra turn fires only when in-band delivery did not happen."""

    REFUSALS = [("bash", "denied by policy")]

    def test_confirmed_in_band_delivery_skips_the_extra_turn(self):
        assert not should_queue_refusal_recovery(
            self.REFUSALS,
            stopping=False,
            needs_reset=False,
            stop_reason="end_turn",
            notices_sent=1,
            notices_pending=0,
        )

    def test_unconfirmed_notice_still_queues_the_fallback(self):
        # No steering_consumed echo covered it — the turn may have ended before
        # any model-inference boundary, so the model was told nothing.
        assert should_queue_refusal_recovery(
            self.REFUSALS,
            stopping=False,
            needs_reset=False,
            stop_reason="end_turn",
            notices_sent=1,
            notices_pending=1,
        )

    def test_partially_covered_refusals_still_queue(self):
        # Two denies, one notice: the uncovered one has no other way to be told.
        assert should_queue_refusal_recovery(
            [("bash", "denied"), ("fs_write", "blocked")],
            stopping=False,
            needs_reset=False,
            stop_reason="end_turn",
            notices_sent=1,
            notices_pending=0,
        )

    def test_defaults_preserve_pre_existing_behaviour(self):
        # A caller that knows nothing about notices (harness without steer, and
        # every existing call site) must behave exactly as before.
        assert should_queue_refusal_recovery(
            self.REFUSALS, stopping=False, needs_reset=False, stop_reason="end_turn"
        )

    def test_user_cancel_still_wins_over_in_band_accounting(self):
        assert not should_queue_refusal_recovery(
            self.REFUSALS,
            stopping=False,
            needs_reset=False,
            stop_reason="cancelled",
            notices_sent=0,
            notices_pending=0,
        )

    def test_no_refusals_never_queues_even_with_notices(self):
        assert not should_queue_refusal_recovery(
            [], stopping=False, needs_reset=False, stop_reason="end_turn", notices_sent=3
        )
