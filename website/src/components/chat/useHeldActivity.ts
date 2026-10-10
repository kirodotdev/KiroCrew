/**
 * useHeldActivity — calms the crewmate's one-line status (builds on #18256).
 *
 * The live reading (`useSlotActivity`) flips to "Thinking" the moment a tool
 * call returns, and a fast call returns in tens of milliseconds, so a burst of
 * tool steps read as titles flashing past a line that mostly said Thinking.
 * This hook only delays what the line SHOWS, never what the slot is doing:
 *
 *   - every step stays on screen for at least `HOLD_STEP_MS`;
 *   - a finished tool's title holds for `HOLD_BEFORE_THINKING_MS` before the
 *     line falls back to Thinking, so the next tool usually replaces it
 *     directly;
 *   - the latest reading wins when the hold ends, except that a tool which
 *     started and finished inside the hold still gets its turn before
 *     Thinking (the line names the newest step, not an older one);
 *   - `stopping` /
 *     `compacting` (the user's own actions) swap at once.
 *
 * The header pill reads `useSlotActivity` directly and is unchanged.
 */
import { useEffect, useRef, useState } from 'react'
import type { PillActivity } from '../../pages/members/pillActivity'

export const HOLD_STEP_MS = 600
export const HOLD_BEFORE_THINKING_MS = 1500

const sameActivity = (a: PillActivity, b: PillActivity) =>
  a.kind === b.kind && a.text === b.text && a.fullText === b.fullText

export function useHeldActivity(activity: PillActivity): PillActivity {
  const [shown, setShown] = useState(activity)
  const shownAt = useRef(Date.now())
  const lastTool = useRef<PillActivity | null>(null)
  useEffect(() => {
    if (activity.kind === 'tool') lastTool.current = activity
    const skipped = lastTool.current
    const next = activity.kind === 'thinking' && shown.kind === 'tool' && skipped && !sameActivity(skipped, shown)
      ? skipped
      : activity
    if (sameActivity(next, shown)) return
    const urgent = next.kind === 'stopping' || next.kind === 'compacting'
    const hold = shown.kind === 'tool' && next.kind === 'thinking' ? HOLD_BEFORE_THINKING_MS : HOLD_STEP_MS
    const wait = urgent ? 0 : Math.max(0, shownAt.current + hold - Date.now())
    const id = setTimeout(() => {
      shownAt.current = Date.now()
      setShown(next)
    }, wait)
    return () => clearTimeout(id)
  }, [activity, shown])
  return shown
}
