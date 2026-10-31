"""The time budgets of one ``browser`` op, in ONE place for both of its processes.

An op crosses a process boundary: the ``browser`` MCP tool (``mcp_tools/browser.py``,
the stdio shim) posts it to ``POST /api/browser/op`` (``dashboard/handlers/
browser_ops.py``, the gateway), which waits for the browser to come up, binds the
caller's tab and runs the op. If the shim's HTTP timeout were the shorter of the
two, the shim would report "the gateway did not answer" while the gateway was
still waiting on a browser start, the gateway would go on to land the click
(aiohttp keeps a handler running after its client left), and the model, told the
click failed, would click again.

So the shim's budget is DERIVED here from the gateway's, with a margin, and
``test_browser_ops_route`` pins the inequality. The gateway, for its part, bounds its
own wait for the cross-chat lock so that a fast op queued behind another chat's slow
one answers ``browser_busy`` inside the shim's budget instead of landing after it.

Pure constants: this module imports nothing, so the shim can import it without
pulling the dashboard in.
"""

from __future__ import annotations

#: An ordinary op (click, type, snapshot): one or two CDP round trips.
OP_TIMEOUT_S = 20.0
#: An op that waits on the page (navigate, back, wait_for).
SLOW_OP_TIMEOUT_S = 75.0
#: The optional decision-model hooks' own ceilings (``browser_harness.decide``):
#: everything before the op, and the step check after it. The route adds their sum
#: to every op's budget, so a slow hook can never eat into the time the op itself
#: is given -- an op that succeeded is never reported as a timeout.
DECISION_PRE_OP_S = 6.0
DECISION_POST_OP_S = 6.0
DECISION_HEADROOM_S = DECISION_PRE_OP_S + DECISION_POST_OP_S
#: How long an op waits for the browser to come up (Kiro's Chromium launch, or
#: the owner's Allow click) before answering ``browser_error``.
ENSURE_TIMEOUT_S = 60.0
#: How long an op waits for another chat's op to leave the gateway-wide lock
#: before answering ``browser_busy``. One slow op's worth, so a fast op behind a
#: navigate is served; two would push the total past the shim's budget.
LOCK_WAIT_S = SLOW_OP_TIMEOUT_S + DECISION_HEADROOM_S
#: Added to the gateway's worst case so the shim always hears the gateway's
#: answer rather than inventing its own.
SHIM_MARGIN_S = 10.0

#: How long the TOOL holds an op while the person is in control before answering
#: ``user_in_control``. The hold is the Manus-style flow the owner asked for: the
#: person takes over to sign in, hands back, and the agent's pending op simply
#: runs; the agent never has to be told to continue. Ten minutes covers a sign-in
#: with 2FA; past it the agent is told to stop and wait for the person's word.
#: The hold lives in the SHIM, as a parked ``DeferredTool`` that polls
#: ``/api/browser/handback`` -- never as one blocking HTTP call: the shim runs one
#: worker thread, and the ACP tool-stall watchdog kills a tool call silent for
#: 600 s, so the hold is sliced and each slice pings the session keepalive.
HOLD_TIMEOUT_S = 10 * 60.0
#: A takeover that outlived its turn (held past the turn's end, or taken on a
#: reopened view) is handed back after this long with no sign the card is still
#: open: a claim, an input event, a frame ack, the card scrolling into view. The
#: claim is plain HTTP with no socket to watch, so without this a card closed
#: mid-takeover would hold the chat's browser for good and every later turn's
#: ops would sit out the whole ``HOLD_TIMEOUT_S``. Shorter than that hold, so a
#: parked op is freed by this bound first.
TAKEOVER_IDLE_S = 5 * 60.0
#: Seconds between two hand-back polls from the parked tool.
HANDBACK_POLL_S = 5.0
#: How long one ``/api/browser/handback`` call may wait on the release signal
#: before answering "still held" (a short long-poll: shorter than the shim's step
#: budget, long enough that a release lands within a second or two).
HANDBACK_WAIT_S = 3.0
#: The shim's HTTP timeout for one hand-back poll.
HANDBACK_CALL_TIMEOUT_S = HANDBACK_WAIT_S + SHIM_MARGIN_S

#: A quick ask (the ``ask`` op, ``browser_harness.quick_ask``) waits this long for
#: the person's answer; a human_check takeover is bounded tighter. The agent's
#: tool parks on ``/api/browser/ask/poll`` like the hand-back hold, each poll
#: waiting at most ``ASK_WAIT_S`` on the ask's own settle signal.
ASK_TIMEOUT_S = 10 * 60.0
HUMAN_CHECK_TIMEOUT_S = 5 * 60.0
ASK_WAIT_S = HANDBACK_WAIT_S
ASK_CALL_TIMEOUT_S = ASK_WAIT_S + SHIM_MARGIN_S

#: The credential re-pull before a ``navigate`` or ``tabs`` op
#: (``HarnessSupervisor.refresh_credentials_if_source_changed``): the provider's
#: ``fingerprint()``, the wait for a start in flight, then the re-pull itself.
CREDENTIAL_FINGERPRINT_S = 2.0
CREDENTIAL_LOCK_WAIT_S = 5.0
CREDENTIAL_REPULL_S = 8.0
CREDENTIAL_WORST_S = CREDENTIAL_FINGERPRINT_S + CREDENTIAL_LOCK_WAIT_S + CREDENTIAL_REPULL_S

#: The gateway's worst case for one op: browser start, the lock, the credential
#: re-pull, the tab bind (one ordinary op's budget), then the op itself. The hold
#: is not in it: a held op answers at once with ``hold: true`` and the shim waits
#: between calls.
GATEWAY_FAST_WORST_S = (
    ENSURE_TIMEOUT_S
    + LOCK_WAIT_S
    + CREDENTIAL_WORST_S
    + OP_TIMEOUT_S
    + OP_TIMEOUT_S
    + DECISION_HEADROOM_S
)
GATEWAY_SLOW_WORST_S = (
    ENSURE_TIMEOUT_S
    + LOCK_WAIT_S
    + CREDENTIAL_WORST_S
    + OP_TIMEOUT_S
    + SLOW_OP_TIMEOUT_S
    + DECISION_HEADROOM_S
)

#: What the shim waits on the HTTP call, per op class. Strictly longer than the
#: gateway's worst case, so an op the gateway is still running is never reported
#: failed (and retried) by the tool.
SHIM_FAST_TIMEOUT_S = GATEWAY_FAST_WORST_S + SHIM_MARGIN_S
SHIM_SLOW_TIMEOUT_S = GATEWAY_SLOW_WORST_S + SHIM_MARGIN_S

__all__ = [
    "ASK_CALL_TIMEOUT_S",
    "ASK_TIMEOUT_S",
    "ASK_WAIT_S",
    "HUMAN_CHECK_TIMEOUT_S",
    "DECISION_HEADROOM_S",
    "DECISION_POST_OP_S",
    "DECISION_PRE_OP_S",
    "CREDENTIAL_FINGERPRINT_S",
    "CREDENTIAL_LOCK_WAIT_S",
    "CREDENTIAL_REPULL_S",
    "CREDENTIAL_WORST_S",
    "ENSURE_TIMEOUT_S",
    "GATEWAY_FAST_WORST_S",
    "GATEWAY_SLOW_WORST_S",
    "HANDBACK_CALL_TIMEOUT_S",
    "HANDBACK_POLL_S",
    "HANDBACK_WAIT_S",
    "HOLD_TIMEOUT_S",
    "LOCK_WAIT_S",
    "OP_TIMEOUT_S",
    "SHIM_FAST_TIMEOUT_S",
    "SHIM_MARGIN_S",
    "SHIM_SLOW_TIMEOUT_S",
    "SLOW_OP_TIMEOUT_S",
    "TAKEOVER_IDLE_S",
]
