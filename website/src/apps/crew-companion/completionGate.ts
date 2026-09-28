/**
 * Completion-notification gate — pure decision logic.
 *
 * Ported from the standalone desktop app's `src/shared/completionGate.ts`. This
 * copy is deliberately host-free: no network, no Electron, no React, no i18n. It
 * only decides whether a finished turn deserves a notification; the caller owns
 * the slot bookkeeping (which slots are running, and when each started) and the
 * history fetch used to recover an unknown start.
 *
 * Every rule here exists because of a failure that reached a real user:
 *
 *  - TOO-SHORT THRESHOLD. The original cutoff was 30s, which sat squarely in the
 *    middle of real turn durations (~15–55s), so it silently swallowed about half
 *    of all completed work. The user saw nothing when a genuine task finished.
 *    Lowering it to 10s keeps out only the trivial blips while letting ordinary
 *    turns through.
 *
 *  - ASSUMED START RESETTING ELAPSED TO ~ZERO. Turn start was measured from when
 *    the app FIRST OBSERVED a slot already running. Restarting the app mid-turn
 *    therefore reset elapsed time to near-zero and dropped the notification —
 *    repeatedly and invisibly. So a start we only ASSUMED must not be run through
 *    the threshold directly; it has to be sent back for the real start to be
 *    recovered from history first (the `verify` path).
 *
 *  - THE COMPANION CELEBRATING ITS OWN TIMERS. The pet runs its own housekeeping
 *    background agent. If its completions notified, the ghost would cheer for its
 *    own internal work all day, so those slots are excluded outright.
 *
 * Ported adaptations (see also STEP notes below):
 *  - The desktop original also exported `completionCopy` (+ its `OutcomeInput` /
 *    `OutcomeCopy` types) and a stateful `TurnStartTracker`. Both are intentionally
 *    NOT carried over here: `completionCopy` emits user-visible copy strings that
 *    belong behind this build's i18n layer, and `TurnStartTracker` is slot
 *    bookkeeping the caller owns. This module is the pure decision core only.
 */

/**
 * Minimum turn duration before a completion is worth interrupting for.
 *
 * 10s, not the original 30s: 30s dropped ~half of all real turns (see header).
 */
export const TURN_NOTIFY_MIN_MS = 10_000

/**
 * Name of the companion's own background (housekeeping) agent, whose completions
 * never notify — otherwise the pet celebrates its own timers.
 *
 * `'crew-companion-bg'` follows this build's `<app-name>-bg` convention: the app
 * name is `crew-companion` (see `app.json` / backend `routes.py` `APP_NAME`), and
 * the sibling Mochi app names its own background agent `mochi-bg`. Verified against
 * the target repo before copying the constant verbatim from the source.
 */
export const BG_AGENT_PREFIX = 'crew-companion-bg'

export interface GateInput {
  /** Slot key that just went idle. */
  slotKey: string
  /** Our recorded start time (ms epoch), or undefined if we never saw it run. */
  startedAt?: number
  /** Now (ms epoch). */
  now: number
  /**
   * True when `startedAt` was only ASSUMED — i.e. the slot was already running
   * the first time we saw it, so the real start is unknown and must be recovered
   * from slot history before the threshold can be applied fairly. Prevents an app
   * restart mid-turn from resetting elapsed to ~zero and swallowing the ping.
   */
  assumedStart: boolean
  /** User preference: suppress completion pings entirely. */
  silent: boolean
}

export type GateDecision =
  /** Nothing to report (slot wasn't running, or is excluded, or user opted out). */
  | { action: 'skip'; reason: string }
  /** Notify now — elapsed is known and passes the threshold. */
  | { action: 'notify'; elapsedMs: number }
  /**
   * Elapsed is unreliable because the start was assumed. The caller should fetch
   * the real start from slot history and call `confirmAssumedStart`.
   */
  | { action: 'verify'; elapsedMs: number }

/** True for the companion's own housekeeping agent slots (they never notify). */
export function isOwnBackgroundAgent(slotKey: string): boolean {
  return slotKey === BG_AGENT_PREFIX || slotKey.startsWith(BG_AGENT_PREFIX)
}

/**
 * Decide what to do when a slot transitions running → idle.
 *
 * Order matters: the excluded/never-seen/opted-out skips come before any elapsed
 * arithmetic, and the assumed-start `verify` deferral comes before the too-short
 * threshold — otherwise a restart-shortened elapsed would be dropped as "too
 * short" instead of being recovered from history.
 */
export function evaluateCompletion(input: GateInput): GateDecision {
  const { slotKey, startedAt, now, assumedStart, silent } = input

  if (isOwnBackgroundAgent(slotKey)) {
    // The pet must never announce its own housekeeping work.
    return { action: 'skip', reason: 'own-background-agent' }
  }
  if (startedAt === undefined) {
    // We never saw this slot start, so there is nothing to time or announce.
    return { action: 'skip', reason: 'never-running' }
  }
  if (silent) {
    return { action: 'skip', reason: 'user-opted-out' }
  }

  const elapsedMs = now - startedAt

  // An assumed start means a short elapsed is an artefact of OUR uptime (the app
  // restarted mid-turn), not of a short turn — so defer to history rather than
  // dropping it as too short.
  if (assumedStart) return { action: 'verify', elapsedMs }

  if (elapsedMs < TURN_NOTIFY_MIN_MS) {
    // Below the threshold: a trivial blip, not worth interrupting for.
    return { action: 'skip', reason: 'too-short' }
  }
  return { action: 'notify', elapsedMs }
}

/**
 * Second half of the `verify` path: apply the threshold against the real start
 * recovered from slot history, now that the assumed start has been replaced by a
 * trustworthy one.
 *
 * @param realStart ms epoch of the turn's actual first user message, or 0 when
 *                  history was unavailable (then we fall back to `fallbackElapsedMs`,
 *                  the assumed elapsed — better than dropping a real completion).
 * @param now       ms epoch.
 * @param fallbackElapsedMs elapsed to use when `realStart` could not be recovered.
 */
export function confirmAssumedStart(
  realStart: number,
  now: number,
  fallbackElapsedMs: number,
): GateDecision {
  const elapsedMs = realStart > 0 ? now - realStart : fallbackElapsedMs
  if (elapsedMs < TURN_NOTIFY_MIN_MS) {
    // Even with the recovered start, the turn was genuinely too short to announce.
    return { action: 'skip', reason: 'too-short-after-recovery' }
  }
  return { action: 'notify', elapsedMs }
}
