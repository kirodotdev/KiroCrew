/**
 * The wire text of a sub-agent card's feed, as the gateway writes it.
 *
 * A running card's `streaming` is the gateway's own copy of the feed: the
 * answer text as it streamed, and -- on a native kiro-cli card -- each call's
 * output under its title, as `native_cards._native_result_feed_line` files it.
 * The browser keeps the same feed on the card (`SubagentActivity.streaming`),
 * built from the frames it received, so a reconnect can tell what the snapshot
 * carries past it (`feedPastHeld` in `subagents.ts`). Every string here is
 * matched or written byte-for-byte against the gateway's; none is shown, and
 * translating one would break the match. Copy that a person reads lives in the
 * catalog and renders in `pages/chat/SubagentTranscript.tsx`.
 */

/** Heads a managed run's feed once it passed 50k characters
 *  (`subagent_manager/run_turn.py`). A native card's feed carries no line; it
 *  is a bare newest tail (`NATIVE_SUBAGENT_OUTPUT_TAIL`). */
export const FEED_TRUNCATION_LINE = '\u2026(truncated)\n'

/** Ends a native card's running snapshot and finished result when its run
 *  crossed the call bound (`NATIVE_SUBAGENT_CALLS_OVERFLOW_FEED_LINE` in
 *  `state_parts/slot_directory.py`). Said at snapshot time, never as a chunk. */
export const FEED_CALLS_OVERFLOW_LINE = '\u2026(this run made more tool calls than the card keeps names for; later outputs are filed under \u2192 Tool call past the name limit)\n'

/** Opens the title line over a call's output in the feed. */
export const FEED_RESULT_TITLE_PREFIX = '\u2192 '

/** kiro-cli's status word on a live tool title; the feed's title line drops it
 *  (`native_subagent_cards._NATIVE_RUNNING_PREFIX`). */
export const FEED_RUNNING_PREFIX = /^Running: /

/** The feed's entry for a call's output, as `_native_result_feed_line` writes
 *  it for an admitted call: under the call's title when the gateway holds one,
 *  bare when the call was first seen through its result. */
export function feedResultLine(title: string, output: string): string {
  return title
    ? `${FEED_RESULT_TITLE_PREFIX}${title.replace(FEED_RUNNING_PREFIX, '')}\n${output}\n`
    : `${output}\n`
}
