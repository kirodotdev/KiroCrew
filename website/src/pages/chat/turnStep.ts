/**
 * Keyboard stepping through the turns of the open chat: Alt+↑ jumps to the
 * previous prompt, Alt+↓ to the next (the `turn-prev` / `turn-next` registry
 * entries).
 *
 * The chord is matched by the global shortcut handler (useKeyboardShortcuts),
 * which owns the gates every Alt chord shares: the global enable toggle, the
 * open shortcuts modal, and text fields, which keep Alt+arrow for caret
 * movement. The transcript lives in the chat page, so the page registers a
 * stepper here while its single-chat transcript is on screen (useTurnStepper)
 * and the handler calls it. With no stepper registered — another page, split
 * view, the welcome screen — the chord is left to the browser.
 *
 * One stepper at a time: the single-chat page is the only host, and a host that
 * unregisters only clears its own registration.
 */

export type TurnStepDirection = -1 | 1

/** Steps the transcript; returns whether it claimed the keystroke. */
export type TurnStepper = (dir: TurnStepDirection) => boolean

let activeStepper: TurnStepper | null = null

export function registerTurnStepper(stepper: TurnStepper): () => void {
  activeStepper = stepper
  return () => {
    if (activeStepper === stepper) activeStepper = null
  }
}

/** Step the registered transcript. `false` when none is registered or it has no turns. */
export function stepTurn(dir: TurnStepDirection): boolean {
  return activeStepper ? activeStepper(dir) : false
}

/**
 * How far (px) a row's top may sit from the reading line and still count as ON
 * it. A jump lands its row on the line to the pixel, but the line itself moves a
 * little afterwards: it is derived from the pinned banner's height, and the
 * banner that pins at the landing is a different prompt with a different wrap.
 */
export const READING_LINE_TOLERANCE_PX = 8

/** A mounted transcript row: its display index and its top edge (viewport px). */
export interface MountedRow {
  idx: number
  top: number
}

/** Where the reader is, for {@link turnStepTarget}. */
export interface ReadingPosition {
  /** Display index of the first row at or below the reading line. */
  at: number
  /** Whether that row's top sits on the line, i.e. a jump just landed it there. */
  onLine: boolean
}

/**
 * The reader's place in a mounted transcript, read against `lineY` — the line a
 * pinned-prompt jump lands a row's top on.
 *
 * `rows` are the mounted rows in transcript order. Every row before the first
 * one at or below the line has scrolled past it; that row and everything after
 * it are still ahead. When every mounted row is above the line, the reader is
 * past them all, so `at` is the index after the last one. `null` when nothing is
 * mounted.
 */
export function readingPosition(rows: readonly MountedRow[], lineY: number): ReadingPosition | null {
  for (const row of rows) {
    if (row.top >= lineY - READING_LINE_TOLERANCE_PX) {
      return { at: row.idx, onLine: row.top <= lineY + READING_LINE_TOLERANCE_PX }
    }
  }
  const last = rows[rows.length - 1]
  return last ? { at: last.idx + 1, onLine: false } : null
}

/**
 * The display index a step in `dir` lands on, or -1 past either end.
 *
 * `stops` are the display indexes of the turns' prompts, ascending. `at` is the
 * reader's position: a turn the reader is ON (`onStop`: the last step landed
 * there, or its prompt sits on the reading line) is stepped away from in both
 * directions; otherwise `at` is the first row still ahead, so a prompt at `at`
 * is the next stop down and every prompt before it is behind. Reading the middle
 * of a long reply therefore steps up to that reply's own prompt first — the same
 * move as clicking the pinned banner.
 */
export function turnStepTarget(
  stops: readonly number[],
  at: number,
  onStop: boolean,
  dir: TurnStepDirection,
): number {
  if (dir < 0) {
    for (let i = stops.length - 1; i >= 0; i--) {
      if (stops[i] < at) return stops[i]
    }
    return -1
  }
  for (const stop of stops) {
    if (stop > at || (stop === at && !onStop)) return stop
  }
  return -1
}
