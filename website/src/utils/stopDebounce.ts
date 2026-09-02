// Arming window for the force-kill escalation on the Stop button.
//
// First Stop press = cooperative soft cancel (preserves session state).
// Second press (while the slot is `soft_pending`) escalates to a hard kill,
// which discards the in-flight turn and queued messages. To stop a frantic
// double-tap from immediately hard-killing, a second press that lands within
// this window of the soft press is ignored. The backend auto-escalates the
// soft cancel to a hard kill on its own after `agent.soft_stop_budget_secs`
// (default 10s), so a genuine "force now" press loses nothing by waiting.
export const FORCE_KILL_ARMING_MS = 400

export type StopAction = 'soft' | 'force' | 'ignore'

/**
 * Whether a slot's `stop_state` means a Stop press should escalate to a
 * hard kill rather than send another (redundant) soft cancel.
 *
 * - `soft_pending`: the classic second-press force kill.
 * - `killing`: the >15s escape hatch — the hard kill itself has stalled, so
 *   the press must re-dispatch `force: true`; a plain soft cancel would be
 *   ignored as redundant by the backend and the button would be a no-op.
 */
export function isEscalationState(stopState: string | undefined | null): boolean {
  return stopState === 'soft_pending' || stopState === 'killing'
}

/**
 * Decide what a Stop-button press should do.
 *
 * @param isSoftPending whether the slot is already in the `soft_pending` state
 * @param now           current timestamp (ms), e.g. `Date.now()`
 * @param softStopAt    timestamp (ms) of the last soft-stop press (0 if none)
 * @param armingMs      arming window in ms (defaults to FORCE_KILL_ARMING_MS)
 * @returns
 *   - `'soft'`   first press: send the cooperative cancel
 *   - `'ignore'` second press inside the arming window: accidental double-tap
 *   - `'force'`  second press after the arming window: escalate to hard kill
 */
export function decideStopAction(
  isSoftPending: boolean,
  now: number,
  softStopAt: number,
  armingMs: number = FORCE_KILL_ARMING_MS,
): StopAction {
  if (!isSoftPending) return 'soft'
  if (now - softStopAt < armingMs) return 'ignore'
  return 'force'
}

/** Mutable timestamp holder (matches React's MutableRefObject<number>). */
export interface SoftStopRef {
  current: number
}

/**
 * Apply a Stop-button press: decide the action, manage the soft-stop
 * timestamp ref, and invoke the matching side-effect callback. This is the
 * wiring that `ChatPage.onStop` delegates to, factored out so the
 * ref-management and force-flag branching can be unit-tested without
 * rendering the full page.
 *
 * - `'soft'`   -> records `now` into `ref.current`, calls `onSoft()`
 * - `'ignore'` -> does nothing (accidental double-tap inside the window)
 * - `'force'`  -> calls `onForce()`, leaves `ref` untouched
 *
 * @returns the action that was taken (useful for tests / callers)
 */
export function handleStopPress(
  isSoftPending: boolean,
  now: number,
  ref: SoftStopRef,
  onSoft: () => void,
  onForce: () => void,
  armingMs: number = FORCE_KILL_ARMING_MS,
): StopAction {
  const action = decideStopAction(isSoftPending, now, ref.current, armingMs)
  if (action === 'soft') {
    ref.current = now
    onSoft()
  } else if (action === 'force') {
    onForce()
  }
  return action
}
