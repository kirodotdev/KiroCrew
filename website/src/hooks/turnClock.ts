/**
 * How long each session's CURRENT turn has been running, as this window saw it.
 *
 * A `chat_done` frame carries no duration, so a notification rule that depends
 * on "the turn ran longer than N seconds" needs a start time from somewhere.
 * The row that begins a turn (`opensTurn`: a person's prompt, an auto-nudge cycle,
 * a drained sub-agent result, or an inject row whose kind dispatched a turn of its
 * own) reaches this window live, so it stamps the turn. A queued send the drain
 * turns into the person's row arrives as a `queue_pop` with no row echo, so that
 * pop stamps instead. The turn's `chat_done` takes the stamp, except a
 * `continuing` one: its sub-agents, workflow, loop or queue still owe work, so the
 * stamp stays and the run's final completion measures the whole run.
 *
 * A window that opened mid-turn, or reconnected after the turn began, never saw
 * that row: a reply, tool, steer or notice row only continues a turn and stamps nothing.
 * `takeTurnSeconds` then answers `null`, and the caller treats
 * "unknown" as "long enough": a missed notification costs more than an extra
 * one, and the rule defaults to off anyway.
 *
 * Window-local, like `turnError.ts`. Stamped once per run and taken, which clears
 * it, by the run's final `chat_done`. A run that ends without one leaves its stamp,
 * so the next completion reads long: the safe direction.
 *
 * Each stamp holds two readings, and the length is the larger of the two. The
 * wall clock can step back (a clock correction) and the monotonic clock may stand
 * still while the computer is suspended, so either alone can make a long turn read
 * short. A forward wall-clock step can make a short turn read long: the safe direction.
 */

interface TurnStart {
  wallMs: number
  monoMs: number
}

const turnStarts = new Map<string, TurnStart>()

/** Stamp *slotKey*'s running turn unless it already has a start. */
export function noteTurnActivity(slotKey: string, wallMs: number = Date.now(), monoMs: number = performance.now()): void {
  if (!turnStarts.has(slotKey)) turnStarts.set(slotKey, { wallMs, monoMs })
}

/** Whole seconds *slotKey*'s finishing turn ran, or `null` when this window did
 *  not see it begin. Clears the stamp so the next turn starts clean. */
export function takeTurnSeconds(slotKey: string, wallMs: number = Date.now(), monoMs: number = performance.now()): number | null {
  const started = turnStarts.get(slotKey)
  turnStarts.delete(slotKey)
  if (started === undefined) return null
  const elapsedMs = Math.max(wallMs - started.wallMs, monoMs - started.monoMs)
  return Math.max(0, Math.floor(elapsedMs / 1000))
}

export function _resetTurnClockForTest(): void {
  turnStarts.clear()
}
