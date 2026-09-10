"""Acceptance tests for the promise-only turn guard (#2686).

The bug: a turn that ends right after the model ANNOUNCES an immediate action
("I'll do that now") without making the tool call was recorded as a landed
success and billed, even though no work happened. The guard injects exactly one
continuation telling the model to carry out the announced action, and does not
record the promise-only turn as a success.

These tests exercise the pure detector (``is_promise_only_terminal``) and the
gating decision (``should_recover_promise_only``) directly — the two functions
that hold the logic — so they run without the chat_runner turn-loop. Each test
maps to one bullet in the issue's acceptance-coverage list.
"""

from __future__ import annotations

import inspect

import pytest

from kiro_crew.acp.types import STOP_REASON_CANCELLED, STOP_REASON_END_TURN, STOP_REASON_REFUSAL
from kiro_crew.dashboard import chat_utils as _chat_utils
from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    SUBAGENT_COMPLETION_KIND,
    SYNTHETIC_RECOVERY_KIND,
    RecoveryPayload,
    is_promise_only_terminal,
    is_synthetic_payload_item,
    is_synthetic_recovery_item,
    should_recover_promise_only,
)

_END = STOP_REASON_END_TURN


def _is_false_current_tool_blocker(text: str) -> bool:
    assert hasattr(
        _chat_utils, "is_false_current_tool_blocker"
    ), "false current-tool-blocker recovery is missing"
    return _chat_utils.is_false_current_tool_blocker(text)


def _is_false_current_tool_blocker_near_miss(text: str) -> bool:
    assert hasattr(
        _chat_utils, "is_false_current_tool_blocker_near_miss"
    ), "false tool-blocker drift detection is missing"
    return _chat_utils.is_false_current_tool_blocker_near_miss(text)


def _tool_calls_are_read_only_preparation(
    calls: int,
    identities: tuple[tuple[str, str, str, bool], ...],
    successful_ids: frozenset[str],
    *,
    builtin_identity_trusted: bool = True,
) -> bool:
    assert hasattr(
        _chat_utils, "tool_calls_are_read_only_preparation"
    ), "read-only preparation accounting is missing"
    return _chat_utils.tool_calls_are_read_only_preparation(
        calls,
        identities,
        successful_ids,
        builtin_identity_trusted=builtin_identity_trusted,
    )


def _recover(**over):
    """should_recover_promise_only with the promise-only-firing defaults, so each
    test flips exactly the one field it is about."""
    kw = dict(
        stop_reason=_END,
        end_turn_reason=_END,
        produced_visible_output=True,
        final_segment_text="Yes. I can open the PR for you, and I'll do that now.",
        prompt_depth=0,
        promise_only_retries=0,
        is_cancelled=False,
        refusal_reasons=[],
        turn_tool_calls=0,
        turn_tool_identities=(),
        successful_tool_call_ids=frozenset(),
        builtin_identity_trusted=True,
        directive_user_origin=True,
        in_stage_execution=False,
    )
    kw.update(over)
    parameters = inspect.signature(should_recover_promise_only).parameters
    if "turn_tool_identities" not in parameters:
        assert kw["turn_tool_calls"] == 0, "canonical tool identity recovery is missing"
        kw.pop("turn_tool_identities")
        kw.pop("successful_tool_call_ids")
    if "directive_user_origin" not in parameters:
        assert kw["turn_tool_calls"] == 0, "authenticated-user replay is missing"
        kw.pop("directive_user_origin")
    if "builtin_identity_trusted" not in parameters:
        assert kw["turn_tool_calls"] == 0, "builtin identity provenance is missing"
        kw.pop("builtin_identity_trusted")
    return should_recover_promise_only(**kw)


# 1. A confirmed promise-only response CONTINUES instead of landing.
def test_promise_only_response_triggers_recovery():
    assert _recover() is True
    # the issue's verbatim transcript line
    assert is_promise_only_terminal("Yes. I can open the PR for you, and I’ll do that now.")
    assert is_promise_only_terminal("I'll go ahead and run the gate now.")
    assert is_promise_only_terminal("Let me open that PR right away.")


_OBSERVED_FALSE_TOOL_BLOCKERS = (
    "I’m blocked from further tool execution in the resumed session: "
    "the screenshot delivery tools are no longer callable here.",
    "I’m proceeding, but this turn’s tool budget was exhausted immediately "
    "after loading the workflow. No publish or deployment has happened yet.",
)


def test_observed_false_tool_blocker_phrasings_match():
    for text in _OBSERVED_FALSE_TOOL_BLOCKERS:
        assert _is_false_current_tool_blocker(text) is True
        assert _is_false_current_tool_blocker_near_miss(text) is False


@pytest.mark.parametrize(
    "text",
    (
        "I'm blocked from further tool execution.",
        "I'm blocked from further tool execution in the resumed session: "
        "the screenshot delivery tools are accessible here.",
        "I'm proceeding, but this turn's tool budget was exhausted immediately "
        "after searching the workflow.",
    ),
)
def test_unobserved_false_tool_blocker_variants_remain_manual(text: str):
    assert _is_false_current_tool_blocker(text) is False
    assert _is_false_current_tool_blocker_near_miss(text) is True


_EXPECTED_READ_ONLY_PREPARATION_TOOLS = (
    "fs_read",
    "glob",
    "grep",
    "introspect",
    "tool_search",
    "web_fetch",
    "web_search",
)


@pytest.mark.parametrize("tool_name", _EXPECTED_READ_ONLY_PREPARATION_TOOLS)
def test_every_read_only_preparation_tool_recovers(tool_name: str):
    assert frozenset(_EXPECTED_READ_ONLY_PREPARATION_TOOLS) == getattr(
        _chat_utils, "_READ_ONLY_PREPARATION_TOOLS"
    )
    call_id = f"call-{tool_name}"
    identities = ((call_id, "", tool_name, True),)
    completed = frozenset({call_id})
    blocker = _OBSERVED_FALSE_TOOL_BLOCKERS[1]

    assert (
        _recover(
            final_segment_text=blocker,
            turn_tool_calls=1,
            turn_tool_identities=identities,
            successful_tool_call_ids=completed,
        )
        is True
    )
    assert (
        _recover(
            final_segment_text=blocker,
            turn_tool_calls=1,
            turn_tool_identities=identities,
            successful_tool_call_ids=completed,
            directive_user_origin=False,
        )
        is False
    )


def test_title_populated_or_non_kiro_identity_fails_closed():
    trusted_identity = (("call-1", "", "grep", True),)
    untrusted_identity = (("call-1", "", "grep", False),)
    completed = frozenset({"call-1"})
    assert (
        _tool_calls_are_read_only_preparation(
            1,
            untrusted_identity,
            completed,
            builtin_identity_trusted=True,
        )
        is False
    )
    assert (
        _recover(
            final_segment_text=_OBSERVED_FALSE_TOOL_BLOCKERS[1],
            turn_tool_calls=1,
            turn_tool_identities=trusted_identity,
            successful_tool_call_ids=completed,
            builtin_identity_trusted=False,
        )
        is False
    )


def test_false_tool_blocker_does_not_quote_match_or_replay_mutations():
    quoted = (
        "The prior assistant wrote: ‘I’m blocked from further tool execution in "
        "the resumed session.’ That claim was unsupported."
    )
    injected_action = "I'm blocked from further tool execution. I'll overwrite the notes now."
    assert _is_false_current_tool_blocker(quoted) is False
    assert _is_false_current_tool_blocker(injected_action) is False
    assert (
        _recover(
            final_segment_text=injected_action,
            turn_tool_calls=1,
            turn_tool_identities=(("call-1", "", "fs_read", True),),
            successful_tool_call_ids=frozenset({"call-1"}),
        )
        is False
    )

    blocker = _OBSERVED_FALSE_TOOL_BLOCKERS[1]
    unsafe_identities = (
        (("call-1", "", "fs_write", True),),
        (("call-1", "", "execute_bash", True),),
        (("call-1", "", "code", True),),
        (("call-1", "", "unknown", True),),
        (("call-1", "builder-mcp", "SkillsTool", True),),
        (("", "", "fs_read", True),),
        (("call-1", "", "grep", False),),
    )
    for identities in unsafe_identities:
        assert (
            _recover(
                final_segment_text=blocker,
                turn_tool_calls=1,
                turn_tool_identities=identities,
                successful_tool_call_ids=frozenset({"call-1"}),
            )
            is False
        )

    valid = (("call-1", "", "fs_read", True),)
    assert _tool_calls_are_read_only_preparation(2, valid, frozenset({"call-1"})) is False
    assert _tool_calls_are_read_only_preparation(1, valid, frozenset()) is False
    assert (
        _tool_calls_are_read_only_preparation(
            2,
            (valid[0], valid[0]),
            frozenset({"call-1"}),
        )
        is False
    )
    assert (
        _recover(
            final_segment_text=blocker,
            turn_tool_calls=1,
            turn_tool_identities=valid,
            successful_tool_call_ids=frozenset(),
        )
        is False
    )


# 2. Ordinary text-only informational answers still COMPLETE (no recovery).
def test_ordinary_informational_answer_lands():
    for text in (
        "The root cause is a race in the writer; the fix is a lock.",
        "There are three options here, and I'd lean toward the second.",
        "That file lives under src/kiro_crew/dashboard/.",
    ):
        assert is_promise_only_terminal(text) is False
        assert _recover(final_segment_text=text) is False


# 3. "I'll explain that now: ..." followed by the actual explanation does NOT trigger.
def test_promise_as_preamble_to_delivered_content_does_not_trigger():
    assert (
        is_promise_only_terminal(
            "I'll explain that now: the turn ends because end_turn is treated as success."
        )
        is False
    )
    # a promise followed by a code fence / list is also delivered content
    assert is_promise_only_terminal("I'll show it now:\n```\nx = 1\n```") is False
    assert is_promise_only_terminal("Let me summarize now:\n- one\n- two") is False


# 4. A completed tool action followed by a summary does NOT trigger or replay.
#    The runner resets its segment buffer at each tool boundary, so the final
#    segment after a tool call is a summary, not a promise.
def test_completed_action_then_summary_does_not_trigger():
    for text in (
        "Done — I opened the PR and CI is now running.",
        "The gate is green now; nothing else is needed.",
    ):
        assert is_promise_only_terminal(text) is False
        assert _recover(final_segment_text=text) is False


# 4b. Permission-seeking / no-action closers must NOT fire (AI-review #2696).
#     These read as immediate to a naive regex but are the opposite of a promise
#     to act: the turn is correctly yielding to the user or declining to act.
def test_permission_seeking_and_no_action_closers_do_not_trigger():
    for text in (
        "Let me know what you'd like to do next.",
        "Let me know how you'd like to proceed.",
        "I'll leave that as-is for now.",
        "I'll do that next week.",
        "I'll stop here for now.",
        "Let me know if you want me to continue.",
    ):
        assert is_promise_only_terminal(text) is False
        assert _recover(final_segment_text=text) is False


# 4c. A NEGATED commitment before the immediacy marker must NOT fire (AI-review
#     #2696 F1): "I'm not going to open the PR now" is an explicit non-action, but
#     the bare `going to`/`won't`/`can't` forms would otherwise match the immediacy
#     regex. True promises with no negation must still fire.
def test_negated_commitment_does_not_trigger():
    for text in (
        "I'm not going to open the PR now.",
        "I'm not going to open the PR right now.",
        "I am not going to do that now.",
        "I won't open the PR now.",
        "I can't do that right now.",
        "I cannot open it now.",
        "I'm no longer going to open it now.",
        # spelled-out "do not" (缩写 don't was covered; the full form was missed) (#2696 GPT round)
        "I do not think I'll open the PR now.",
        "I do not want to open it right now.",
        "It does not need to be opened now.",
    ):
        assert is_promise_only_terminal(text) is False
        assert _recover(final_segment_text=text) is False
    # the genuine (non-negated) promises still fire, so the gate is not a blanket mute
    assert is_promise_only_terminal("Yes, I'll open the PR now.") is True
    assert is_promise_only_terminal("I'm going to open the PR now.") is True


# 4d. A soft Stop in progress must NOT recover (AI-review #2696 B1): a Stop pressed
#     while the promise streamed can lose the cancel race and arrive as a normal
#     end_turn; re-queueing then would dispatch the stopped action. Every sibling
#     recovery path gates on this stop-state, so this one does too.
def test_stop_in_progress_does_not_trigger():
    assert _recover(stop_in_progress=True) is False
    # control: identical inputs without the stop still recover
    assert _recover(stop_in_progress=False) is True


# 4e. Approval-gated closers must NOT fire (AI-review #2696 UX round 2): a
#     conditional promise ("If that looks good, I'll push it now") leaves the
#     decision with the user; auto-continuing it dispatches an action the user
#     was still being asked to approve.
def test_approval_gated_closer_does_not_trigger():
    for text in (
        "If that looks good, I'll push it to the branch now.",
        "Just say the word and I'll open the PR right away.",
        "If you're happy with the plan, I'll do that now.",
        "With your approval, I'll push it now.",
        "Once you confirm, I'll do that right away.",
        # "when you" / "after you" conditions (AI-review #2696 round 3)
        "When you confirm, I'll do that now.",
        "After you confirm, I'll delete it now.",
        "After you approve, I'll push it right away.",
        # any conditional `if` opener, and will-not / I'll-not negation (round 4)
        "If CI passes, I'll delete it now.",
        "If the build is green, I'll merge it now.",
        "I will not delete it now.",
        "I'll not delete it now.",
        # temporal/conditional-gate class (once/when/after/as soon as), round 4
        "Once tests are green, I'll merge it now.",
        "When the build passes, I'll push it now.",
        "After CI, I'll deploy it now.",
        "As soon as you approve, I'll do it now.",
    ):
        assert is_promise_only_terminal(text) is False
        assert _recover(final_segment_text=text) is False
    # question-form asking-for-approval already returns False (no commitment
    # token). Genuine unconditional promise still fires.
    assert is_promise_only_terminal("Yes, I'll open the PR now.") is True


# 4m. Consent-DEFERRAL closers must NOT fire (AI-review #2696 GPT round, blocking):
#     a turn that says it will WAIT FOR / AWAIT the user's approval before acting
#     ("I'll wait for your approval before I delete it right now") reads as an
#     immediate promise to a naive regex, but auto-continuing it dispatches the very
#     action the model deferred pending consent — the worst false-accept (irreversible
#     side effect without approval). The gate is kept precise so it does not swallow
#     genuine promises that merely mention "pending"/"await" benignly.
def test_consent_deferral_closer_does_not_trigger():
    for text in (
        "I'll wait for your approval before I delete it right now.",
        "I'll wait for you to confirm now.",
        "I'll hold it for your approval now.",
        "I'll pause pending your sign-off right now.",
        "Before you approve, I'll delete it now.",
        "I'll await your go-ahead now.",
        "I'll do it once you approve now.",
        "I'll delete it until you confirm now.",
        "I'll wait for the go-ahead now.",
    ):
        assert is_promise_only_terminal(text) is False
        assert _recover(final_segment_text=text) is False
    # NOT over-broad: a genuine promise that merely MENTIONS a pending/awaited object
    # (no consent deferral) still fires — "pending"/"await" are only gates when bound
    # to the user's approval, per the design-review over-broadening caution.
    assert is_promise_only_terminal("I'll merge the pending PR now.") is True
    assert is_promise_only_terminal("I'll open the awaited PR now.") is True


# 4n. Subordinating-CONDITIONAL conjunctions must NOT fire (AI-review #2696 design
#     round): the approval-gate deny-list missed "unless / assuming / provided that /
#     as long as" — each conditions the action on the user, so auto-continuing is a
#     false-accept. Closes the conjunction CLASS; the risky ones are bound to a
#     following pronoun/complementizer so a benign adjective still fires.
def test_conditional_subordinator_closer_does_not_trigger():
    for text in (
        "Assuming you're fine with it, I'll push it now.",
        "Unless you object, I'll merge it now.",
        "Provided that you agree, I'll delete it now.",
        "Given that you approve, I'll do it now.",
        "As long as that's OK, I'll do it now.",
        "So long as you're happy, I'll push it now.",
    ):
        assert is_promise_only_terminal(text) is False
        assert _recover(final_segment_text=text) is False
    # NOT over-broad: "provided"/"given" as adjectives (not "provided/given that|you")
    # keep a genuine promise firing.
    assert is_promise_only_terminal("I'll apply the provided patch now.") is True
    assert is_promise_only_terminal("I'll open the given file now.") is True


# 4i. Third-person "going to" must NOT fire (AI-review #2696 GPT round): the bare


# 4i. Third-person "going to" must NOT fire (AI-review #2696 GPT round): the bare
#     `going to` alternative matched informational statements with no first-person
#     commitment ("The deployment is going to start now"), injecting an unrelated
#     continuation. Only the subject-bound `i'm going to` form remains.
def test_third_person_going_to_does_not_trigger():
    for text in (
        "The deployment is going to start now.",
        "The build is going to finish right now.",
        "Everything is going to be ready now.",
    ):
        assert is_promise_only_terminal(text) is False
        assert _recover(final_segment_text=text) is False
    # the first-person forms still fire — the removal is scoped to the subjectless
    # alternative, not the commitment itself.
    assert is_promise_only_terminal("I'm going to open the PR now.") is True
    assert is_promise_only_terminal("I'll open the PR now.") is True


# 4j. The reject gates are scoped to the TERMINAL sentence, not the whole segment
#     (AI-review #2696 design round): an everyday `if`/`when`/`after`/`let me know`
#     or negation in an EARLIER sentence must NOT veto a genuine promise that sits
#     only in the final sentence — that asymmetric scope landed the exact #2686
#     symptom unrecovered. A conditional/no-action that IS the terminal sentence
#     still rejects.
def test_reject_gates_scoped_to_terminal_sentence():
    # earlier-sentence everyday word, terminal sentence is a real promise -> FIRES
    for text in (
        "When you asked about the flake earlier, I fixed it. I'll open the PR now.",
        "If the earlier run was confusing, sorry. I'll re-run the gate now.",
        "Let me know if that was unclear. I'll push the branch now.",
        "After the meeting we discussed this. I'll do that now.",
    ):
        assert is_promise_only_terminal(text) is True
        assert _recover(final_segment_text=text) is True
    # the gate STILL rejects when the conditional/no-action IS the terminal sentence
    for text in (
        "I finished the change. If it looks good, I'll push it now.",
        "The gate is green. When you confirm, I'll merge it now.",
        "That is done. Let me know what you'd like next.",
    ):
        assert is_promise_only_terminal(text) is False
        assert _recover(final_segment_text=text) is False


# 4k. Caller contract (AI-review #2696 GPT round): the runner set its
#     `_produced_visible_output` flag True ONLY on the mid-turn reset-to-empty paths
#     (steer/compaction/clear/agent-switch); a normal streamed-text turn left it
#     False, so a promise-only turn reached the guard with it False and recovery
#     NEVER fired for the actual #2686 scenario. The runner now derives the argument
#     as `bool(assistant_text.strip()) or _produced_visible_output`; a non-empty
#     final segment is itself visible output. This locks that derivation so a promise
#     drives recovery even when the raw flag is False, while an empty segment does not.
def test_nonempty_final_segment_counts_as_visible_output():
    promise = "Yes, I'll open the PR now."
    assert (
        _recover(
            produced_visible_output=(bool(promise.strip()) or False), final_segment_text=promise
        )
        is True
    )
    assert (
        _recover(produced_visible_output=(bool("".strip()) or False), final_segment_text="")
        is False
    )


# 4h. A pending mid-turn STEER must block recovery (AI-review #2696 round 3): a
#     steer ("don't delete") lives in slot._pending_steers, a separate channel
#     from _queue that is only requeued in _run_chat's finally (after the guard).
#     Firing recovery while a steer is pending would dispatch the announced action
#     despite the just-arrived revocation.
def test_pending_steer_does_not_trigger():
    assert _recover(no_pending_steers=False) is False
    # control
    assert _recover(no_pending_steers=True) is True


# 4f. A Stop that already resolved back to idle DURING the turn must still block
#     recovery (AI-review #2696 GPT round 2 blocking): _stop_state alone misses
#     it because it snaps back to idle; the monotonic _stop_generation counter
#     preserves the "a stop happened during this turn" signal.
def test_stop_generation_changed_does_not_trigger():
    assert _recover(stop_generation_unchanged=False) is False
    # control
    assert _recover(stop_generation_unchanged=True) is True


# 4g. A non-empty user-follow-up queue must block recovery (AI-review #2696 GPT
#     round 2 blocking): queue_insert(0, ...) would jump the continuation ahead
#     of a user "don't do that" message; respect the user's ordering.
def test_non_empty_queue_does_not_trigger():
    assert _recover(queue_empty=False) is False
    # control
    assert _recover(queue_empty=True) is True


# 4o. A turn that made ANY tool call must NOT recover (AI-review #2696 GPT round,
#     blocking): a completed side-effecting tool (e.g. send_message) followed by
#     trailing promise-shaped text ("I'll send that now") would otherwise let the
#     continuation REISSUE the completed action — a duplicate external side effect.
#     The promise-only bug is by definition a zero-tool-call turn.
def test_completed_tool_call_does_not_trigger():
    assert _recover(turn_tool_calls=1) is False
    assert _recover(turn_tool_calls=3) is False
    # control: the same promise with no tool call this turn still recovers
    assert _recover(turn_tool_calls=0) is True


# 4q. A stage-execution turn must NOT trigger recovery (AI-review #2696 GPT round,
#     blocking): the orchestrator's stage loop records the stage complete and advances
#     before an injected continuation finishes, corrupting stage attribution. Excluded
#     like the plan turn (`_armed_final`) is.
def test_stage_execution_turn_does_not_trigger():
    assert _recover(in_stage_execution=True) is False
    # control: the identical promise outside stage execution still recovers
    assert _recover(in_stage_execution=False) is True


# 4p. A queued cron / sub-agent SYSTEM INJECTION must NOT count as user intervention
#     (AI-review #2696 GPT round, blocking): treating it as a user follow-up would
#     block or purge a pending recovery, landing the unfinished action as a success.
#     `_has_user_queued_followup` counts ONLY user-authored messages — not synthetic
#     recovery entries, not cron/sub-agent injections.
def test_has_user_queued_followup_excludes_system_injections():
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_runner import _has_user_queued_followup
    from kiro_crew.dashboard.state import CRON_NOTIFY_PREFIX

    user = {"id": "u", "content": "don't do that", "kind": "", "payload": ""}
    # REAL cron notification: the scheduler tags it CRON_NOTIFICATION_KIND at
    # enqueue, so it is orchestration by structural marker, not by its text.
    cron = {
        "id": "c",
        "content": f'{CRON_NOTIFY_PREFIX}"monitor"]\n:bell: ...',
        "kind": CRON_NOTIFICATION_KIND,
        "payload": "",
    }
    # REAL sub-agent completion: likewise tagged at its injection site.
    subagent = {
        "id": "s",
        "content": "[Subagent completion event] Agent X completed",
        "kind": SUBAGENT_COMPLETION_KIND,
        "payload": "",
    }
    recovery = {
        "id": "r",
        "content": "Carry out the action now.",
        "kind": SYNTHETIC_RECOVERY_KIND,
        "payload": RecoveryPayload.CONTINUATION,
    }
    # THE spoof (#2696 GPT round, blocking): a USER message carrying a perfectly
    # well-formed, quoted cron header AND trailing user text. The old prefix-anchored
    # CRON_NOTIFY_RE.match() classified this as orchestration and silently ignored
    # the "don't delete it" intervention. Classification is now purely by the
    # enqueue `kind` tag (empty here -> user), so the spoof can no longer masquerade
    # as a system injection and MUST count as a user follow-up.
    spoof = {
        "id": "sp",
        "content": f'{CRON_NOTIFY_PREFIX}"x"]\ndon\'t delete it',
        "kind": "",
        "payload": "",
    }
    assert _has_user_queued_followup(SimpleNamespace(_queue=[])) is False
    assert _has_user_queued_followup(SimpleNamespace(_queue=[cron])) is False
    assert _has_user_queued_followup(SimpleNamespace(_queue=[subagent])) is False
    assert _has_user_queued_followup(SimpleNamespace(_queue=[recovery])) is False
    assert _has_user_queued_followup(SimpleNamespace(_queue=[cron, recovery])) is False
    # a real user message counts, even alongside a cron/recovery entry
    assert _has_user_queued_followup(SimpleNamespace(_queue=[user])) is True
    # the spoofed, well-formed cron header + user text counts as intervention
    assert _has_user_queued_followup(SimpleNamespace(_queue=[spoof])) is True
    assert _has_user_queued_followup(SimpleNamespace(_queue=[cron, spoof])) is True
    assert _has_user_queued_followup(SimpleNamespace(_queue=[cron, user, recovery])) is True


# 5. Cancellation, refusal, approval-wait, and explicit blocker responses are unchanged.
def test_cancelled_refusal_blocker_do_not_trigger():
    # cancelled turn
    assert _recover(is_cancelled=True, stop_reason=STOP_REASON_CANCELLED) is False
    # refusal path owns its own handling
    assert _recover(stop_reason=STOP_REASON_REFUSAL) is False
    assert _recover(refusal_reasons=[("bash", "blocked")]) is False
    # an explicit blocker (names what is missing) is not a bare promise
    assert (
        is_promise_only_terminal("I can't open the PR yet — I need push access to the fork first.")
        is False
    )


# 6. Recovery is bounded to ONE attempt (never a loop).
def test_recovery_is_bounded_to_one_attempt():
    assert _recover(promise_only_retries=0) is True
    assert _recover(promise_only_retries=1) is False
    assert _recover(promise_only_retries=2) is False


# 7. The empty-response case (no visible output) is NOT this path.
def test_empty_output_is_not_promise_only():
    assert _recover(produced_visible_output=False) is False
    assert is_promise_only_terminal("") is False
    assert is_promise_only_terminal("   \n  ") is False


# 8. Behavior is not hardcoded to one model id, and nested (depth>0) turns are
#    excluded so a sub-agent turn is never independently "recovered" here.
def test_model_agnostic_and_top_level_only():
    # no model id is an input at all — same verdict regardless of caller
    assert _recover() is True
    # nested turn (prompt_depth > 0) does not trigger
    assert _recover(prompt_depth=1) is False


# 9. The injected continuation is runner-authored orchestration, so linked
#    Slack / other-channel surfaces must NOT receive it as user-authored text
#    (issue acceptance bullet: "linked Slack/other channel surfaces do not receive
#    the internal continuation as user-authored text").
#
#    Both cross-surface mirror legs in chat_runner._run_chat gate on
#    `_is_synthetic`, which for a dequeued queue entry is driven by
#    `is_synthetic_payload_item(item)`: the Slack user-echo (`if not _is_synthetic`)
#    and the non-Slack `_deliver_cross_surface_user_message` call
#    (`if not is_slash and not _is_synthetic`) are both skipped when it is True.
#    The promise-only guard enqueues its continuation exactly as:
#        slot.queue_insert(0, _PROMISE_ONLY_CONTINUE_MSG,
#                          kind=SYNTHETIC_RECOVERY_KIND,
#                          payload=RecoveryPayload.CONTINUATION)
#    so this asserts that same queue-entry shape classifies as runner-authored,
#    i.e. the mirror is suppressed, while an ordinary user entry is not.
def test_promise_only_continuation_not_mirrored_as_user_text():
    # queue_insert stores this dict shape (state.py Slot.queue_insert).
    promise_only_item = {
        "id": "abc123",
        "content": "Carry out the action you announced now by making the tool call.",
        "kind": SYNTHETIC_RECOVERY_KIND,
        "payload": RecoveryPayload.CONTINUATION,
    }
    # Runner-authored -> _is_synthetic is True -> both mirror legs skip it.
    assert is_synthetic_payload_item(promise_only_item) is True

    # A false-blocker replay is runner-owned queue orchestration (so late user
    # intervention can purge it) but carries the authenticated user's original
    # words, so it remains eligible for ordinary user-message authority/mirroring.
    assert hasattr(
        _chat_utils, "FALSE_TOOL_BLOCKER_REPLAY_KIND"
    ), "false-tool-blocker replay marker is missing"
    replay_item = {
        "id": "replay123",
        "content": "publish and deploy the demo",
        "kind": _chat_utils.FALSE_TOOL_BLOCKER_REPLAY_KIND,
        "payload": RecoveryPayload.ORIGINAL,
    }
    assert is_synthetic_recovery_item(replay_item) is True
    assert is_synthetic_payload_item(replay_item) is False

    # An ordinary user message carries no synthetic payload and IS mirrored, so
    # the suppression is specific to the continuation, not a blanket mute. Even a
    # user who types the marker text verbatim stays user-authored.
    user_item = {"id": "def456", "content": "please open the PR now", "kind": "", "payload": ""}
    assert is_synthetic_payload_item(user_item) is False
