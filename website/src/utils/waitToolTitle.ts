/**
 * Does a tool title name the kirocrew-core `wait` tool?
 *
 * An allowlist of the three shapes the transport actually produces, not a
 * substring or token test:
 *
 *   wait                      direct MCP
 *   kirocrew-core___wait      pooled gateway namespacing
 *   wait (mcp)                suffixed
 *
 * A strict `=== 'wait'` comparison matches only the first, which reads as "the
 * countdown is broken on my machine" on the other two transports. But the
 * obvious relaxation — `.some(token => token === 'wait')` over the title's
 * tokens — is unsafe, and NOT merely untidy:
 *
 * `slot.wait_state` is minted by whichever session `_resolve_session_key()`
 * resolved to, and that answers per RUNTIME rather than per ACP session (see
 * issue #2347). So a subagent's `wait` can publish a deadline onto its parent's
 * slot while the PARENT's newest tool row is some unrelated `wait_for_ci` from a
 * different server. A token test accepts that title, the countdown renders on
 * it, and the End-wait button ends the subagent's sleep. The contested-slot
 * latch cannot save this case: only one wait_id ever pings, so there is no
 * collision to detect.
 *
 * Deliberately STRICTER than its backend counterpart
 * `acp/liveness.py::is_wait_tool`, which is a token-membership test. That
 * asymmetry is correct rather than drift: the backend only asks "is this session
 * legitimately blocked in a long tool", where over-matching is harmless — it
 * merely declines to reap something. Here a false positive misattributes a live
 * deadline and arms a button against the wrong sleep, so the same looseness
 * buys a real defect.
 */
const WAIT_TITLE_RE = /^(?:[a-z0-9][a-z0-9._-]*___)?wait(?:\s*\([a-z0-9 ._-]+\))?$/

export function isWaitToolTitle(title: string): boolean {
  return WAIT_TITLE_RE.test((title || '').trim().toLowerCase())
}
