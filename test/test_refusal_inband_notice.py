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

import pathlib
import re

import pytest

from kiro_crew.dashboard.chat_runner import (
    _refined_tool_row_content,
    _reject_hook_blocked,
    _reject_hook_error,
    _reject_invalid_tool,
    _steer_policy_notice,
)
from kiro_crew.dashboard.state import (
    DENY_CAUSE_HOOK_ERROR,
    DENY_CAUSE_INVALID_NAME,
    DENY_CAUSE_POLICY,
    REFUSAL_INBAND_RECOVERY_PREFIX,
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
            refusal_notices=None,
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


class TestCauseSpecificWording:
    """A deny the model can fix must not be worded as one it must route around."""

    def test_invalid_name_says_reissue_not_find_an_alternative(self):
        out = build_refusal_steer_notice("bash", "name too long", cause=DENY_CAUSE_INVALID_NAME)
        assert "failed validation" in out
        assert "reissue" in out.lower()
        # The policy guidance sends the model looking for a different approach.
        # Here the action was never judged, so that advice would abandon a call
        # nobody objected to.
        assert "allowed alternative" not in out
        assert "safety policy" not in out

    def test_hook_error_says_host_fault_not_a_verdict(self):
        out = build_refusal_steer_notice("bash", "hook exploded", cause=DENY_CAUSE_HOOK_ERROR)
        assert "host fault" in out
        assert "nothing judged the call" in out
        assert "safety policy" not in out

    def test_policy_wording_is_unchanged_by_default(self):
        # Every pre-existing caller passes no cause; the policy text must be
        # byte-identical to what shipped, or the model's correction changes
        # meaning on a path this change was not meant to touch.
        assert build_refusal_steer_notice("bash", "denied") == build_refusal_steer_notice(
            "bash", "denied", cause=DENY_CAUSE_POLICY
        )
        assert "was blocked by a Kiro Crew safety policy" in build_refusal_steer_notice(
            "bash", "denied"
        )

    def test_every_cause_keeps_the_invariant_half(self):
        # The half that does the actual work -- naming the string being corrected
        # and forbidding a hand-back -- must not vary with the cause.
        for cause in (DENY_CAUSE_POLICY, DENY_CAUSE_INVALID_NAME, DENY_CAUSE_HOOK_ERROR):
            out = build_refusal_steer_notice("bash", "why", cause=cause)
            assert "User denied tool execution" in out, cause
            assert "NOT a user action" in out, cause
            assert "same turn" in out, cause
            assert "do not ask the user" in out.lower(), cause

    def test_the_bracket_tag_is_cause_neutral(self):
        # The tag has to be true for all three causes. Saying "policy notice" above
        # a sentence that explains the call was NOT a policy matter contradicts the
        # body one line later, and the body is the part doing the correcting.
        for cause in (DENY_CAUSE_POLICY, DENY_CAUSE_INVALID_NAME, DENY_CAUSE_HOOK_ERROR):
            out = build_refusal_steer_notice("bash", "why", cause=cause)
            assert out.startswith("[Kiro Crew host notice]"), cause
            # "policy" may still appear in the POLICY cause's own clause; what must
            # not survive is the tag claiming every cause is one.
            assert "policy notice" not in out, cause

    def test_unknown_cause_degrades_to_policy_rather_than_raising(self):
        # Losing the notice would hand the model back kiro-cli's "user denied"
        # with nothing to correct it; a wrong noun is the cheaper failure.
        out = build_refusal_steer_notice("bash", "why", cause="not-a-cause")
        assert "NOT a user action" in out
        assert "safety policy" in out


class TestInvalidNameExplainsItself:
    """The one deny the model can fix, so the notice is worth the most here."""

    @pytest.mark.asyncio
    async def test_steer_precedes_reject_and_names_the_validation_failure(self):
        client = _SteerClient()
        notices: list[str] = []
        await _reject_invalid_tool(
            client,
            _Slot(),
            _Event(),
            session_key="s",
            error=ValueError("name too long"),
            refusal_notices=notices,
            refusal_reasons=[],
        )
        assert client.calls == ["steer", "reject"]
        assert "name too long" in client.steered[0]
        assert "reissue" in client.steered[0].lower()
        assert notices

    @pytest.mark.asyncio
    async def test_the_display_row_carries_the_cause_for_the_card(self):
        # The card's always-visible summary is keyed on this token. Without it the
        # card reads "safety policy blocked the call" for a deny no policy judged,
        # sending the reader to audit a rule that does not exist -- the same
        # cause-blind wording this change removes for the model, left for the human.
        slot = _Slot()
        await _reject_invalid_tool(
            _SteerClient(),
            slot,
            _Event(),
            session_key="s",
            error=ValueError("name too long"),
            refusal_notices=[],
            refusal_reasons=[],
        )
        rows = [c for _r, c in slot.rows if c.startswith(REFUSAL_INBAND_RECOVERY_PREFIX)]
        assert rows, "no display row was appended"
        assert rows[0].splitlines()[0].endswith(DENY_CAUSE_INVALID_NAME)

    @pytest.mark.asyncio
    async def test_reject_still_happens_without_a_notice_list(self):
        # In-band delivery is additive; a fallback-only caller must still deny.
        client = _SteerClient()
        slot = _Slot()
        await _reject_invalid_tool(
            client,
            slot,
            _Event(),
            session_key="s",
            error=ValueError("bad"),
            refusal_reasons=[],
            refusal_notices=None,
        )
        assert client.calls == ["reject"]
        assert any("invalid: bad" in row[1] for row in slot.rows)

    @pytest.mark.asyncio
    async def test_invalid_name_records_a_fallback_entry(self):
        # The notice is the primary path, never the only one: on a harness with
        # no steer, or a steer that was never folded in, this entry is what the
        # recovery continuation carries. Without it the deny reaches the model
        # through NO channel while the policy path still gets its continuation.
        reasons: list[tuple[str, str]] = []
        await _reject_invalid_tool(
            _SteerClient(),
            _Slot(),
            _Event(),
            session_key="s",
            error=ValueError("name too long"),
            refusal_reasons=reasons,
            refusal_notices=None,
        )
        assert reasons and reasons[0][1] == "name too long"


class TestHookErrorExplainsItself:
    """A hook that faulted judged nothing -- the model must not read a verdict."""

    @pytest.mark.asyncio
    async def test_steer_precedes_reject_and_frames_it_as_a_fault(self):
        client = _SteerClient()
        notices: list[str] = []
        await _reject_hook_error(
            client,
            _Slot(),
            _Event(),
            session_key="s",
            error="hook exploded",
            refusal_notices=notices,
            refusal_reasons=[],
        )
        assert client.calls == ["steer", "reject"]
        assert "host fault" in client.steered[0]
        assert notices

    @pytest.mark.asyncio
    async def test_hook_error_text_is_redacted_before_it_reaches_the_model(self):
        # Hooks are fired with the tool name and parsed input, so an exception
        # that wraps its inputs can carry credential material. The audit already
        # redacted it; the model-facing copy must not be the one exception.
        client = _SteerClient()
        notices: list[str] = []
        await _reject_hook_error(
            client,
            _Slot(),
            _Event(),
            session_key="s",
            error="hook saw AKIAIOSFODNN7EXAMPLE",
            refusal_notices=notices,
            refusal_reasons=[],
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in client.steered[0]

    @pytest.mark.asyncio
    async def test_hook_error_records_a_fallback_entry(self):
        # Same reason as the invalid-name path, and the redacted form must be the
        # one that survives into the fallback too -- the continuation is sent to
        # the model, so an unredacted entry would leak past the audit boundary.
        reasons: list[tuple[str, str]] = []
        await _reject_hook_error(
            _SteerClient(),
            _Slot(),
            _Event(),
            session_key="s",
            error="hook saw AKIAIOSFODNN7EXAMPLE",
            refusal_reasons=reasons,
            refusal_notices=None,
        )
        assert reasons
        assert "AKIAIOSFODNN7EXAMPLE" not in reasons[0][1]


class TestEveryHostDenyCallSiteIsWired:
    """Source-level guard: the coverage claim must be checkable, not asserted.

    Both review bots caught the same miss on the first revision of this change --
    the helpers steered only when handed a notice list, and four production call
    sites passed nothing, so those host denies still handed the model kiro-cli's
    "User denied tool execution". Making the parameters REQUIRED turns a future
    omission into a mypy error, and this test is the second half: it fails if a
    call site is added that threads neither list, which type-checking alone cannot
    catch once someone passes an explicit ``None`` to silence it.
    """

    RUNNER = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard/chat_runner.py"
    HELPERS = ("_reject_invalid_tool", "_reject_hook_error", "_reject_hook_blocked")
    #: A call OPENING in either shape black may produce: arguments on the following
    #: lines, or the whole call on one line. Anchoring to a line that ENDS in "("
    #: is how this guard would rot silently -- a reformat onto one line drops the
    #: site out of the scan while a floor assertion stays green, which is the same
    #: vacuity the interactive-branch pin below rejects.
    CALL = re.compile(r"^\s*await (?:%s)\(" % "|".join(HELPERS))

    def _src(self) -> str:
        return self.RUNNER.read_text(encoding="utf-8")

    def _call_blocks(self) -> list[tuple[int, str]]:
        lines = self._src().splitlines(keepends=True)
        blocks: list[tuple[int, str]] = []
        for i, line in enumerate(lines):
            if not self.CALL.match(line):
                continue
            depth = 0
            for j in range(i, min(i + 30, len(lines))):
                depth += lines[j].count("(") - lines[j].count(")")
                if depth == 0 and j > i:
                    blocks.append((i + 1, "".join(lines[i : j + 1])))
                    break
            else:
                # Never silently skip: an unbalanced walk means the scan cannot
                # speak for this site, and a guard that drops what it cannot parse
                # is the failure mode this class exists to prevent.
                blocks.append((i + 1, lines[i]))
        return blocks

    def test_the_scan_finds_every_call_site_the_source_contains(self):
        # Cross-checked against an INDEPENDENT locator, not a threshold: a bare
        # ">= N" floor stays green when one site drops out of the scan, and a site
        # the walker never sees is invisible to every assertion built on it.
        # Counting "await <helper>(" textually cannot miss a call shape the
        # walker's regex misses, so a divergence means the walker has rotted.
        src = self._src()
        textual = sum(src.count(f"await {name}(") for name in self.HELPERS)
        assert textual >= 10, f"expected the known call sites, textual count {textual}"
        found = len(self._call_blocks())
        assert found == textual, (
            f"the block scan found {found} of {textual} call sites -- its regex no "
            "longer matches every call shape, so the coverage assertion below is "
            "vacuous for the ones it missed"
        )

    def test_every_call_site_threads_both_lists(self):
        missing = [
            line
            for line, block in self._call_blocks()
            if "refusal_notices" not in block or "refusal_reasons" not in block
        ]
        assert not missing, (
            "these host-deny call sites reach the model through no channel -- "
            f"pass refusal_notices= and refusal_reasons=: lines {missing}"
        )

    def test_interactive_approved_path_is_wired(self):
        # The sharpest case: the person clicked APPROVE and the host denied
        # anyway, so "user denied tool execution" is not merely unhelpful but
        # false. Pinned separately because it is the one a reader most needs to
        # trust, and both its deny calls were among the four originally missed.
        src = self.RUNNER.read_text(encoding="utf-8")
        approved = src.split('if outcome == "approved":', 1)
        assert len(approved) == 2, "the interactive approved branch moved -- guard is stale"
        window = approved[1][:4000]
        # Count-matched, not ">= 1": this branch makes TWO host denies (invalid
        # name, hook fault) and a threshold assertion stays green when one of
        # them loses its notice, which is exactly the shape of the original miss.
        calls = len(re.findall(r"await _reject_(?:invalid_tool|hook_error|hook_blocked)\(", window))
        assert calls >= 3, f"expected the branch's three deny calls, saw {calls}"
        wired = window.count("refusal_notices=_refusal_notices")
        assert wired == calls, (
            f"{calls - wired} deny call(s) in the interactive approved branch do not "
            "steer -- the user approved, so 'user denied tool execution' is false there"
        )
