import { useEffect, useRef, useState } from 'react'

/** How long a `false` must hold before the display layer believes it. Long
 *  enough to swallow the gap between two tool calls of one turn, which is the
 *  flap this latch exists for. */
export const RUNNING_LATCH_MS = 2500

/**
 * The slot's running flag, latched for the DISPLAY layer and scoped to the slot.
 *
 * The raw flag is derived from slots broadcasts that catch the agent momentarily
 * idle BETWEEN tool calls, so mid-turn it flaps false for a beat and back. Each
 * flap marks the trailing turn complete: TurnBlock auto-collapses it, the next
 * broadcast re-expands it, and on a long-running turn (hundreds of steps) that
 * is a multi-thousand-px accordion right above a reader parked at the bottom.
 * So TRUE applies immediately (a new turn must render live) and FALSE only
 * after holding steady past the flap window.
 *
 * The hold is only meaningful WITHIN one slot, because the flap it absorbs is
 * one slot's own broadcast. Carried across a session switch it inverts into a
 * defect: `<ChatPage />` is a single route element that does not remount when
 * the slug changes, so leaving a running session left the latch raised while
 * the INCOMING transcript rendered. `applyRunningState` then stamped that
 * session's trailing turn `complete: false` for the whole window, TurnBlock's
 * incomplete branch rendered its steps with no fold at all, and the reasoning
 * collapsed a beat later — an expanded frame and a several-hundred-px reflow on
 * every switch, exactly the shape #8526 fixed for the deferred transcript.
 *
 * So the latch is keyed by slot: while it belongs to another session the raw
 * flag is returned instead, which lands the incoming session's own state in the
 * FIRST commit that paints its transcript rather than after it. And a slot
 * change REPLACES the stored frame at once rather than merely scheduling the
 * hold: left in place, a frame the previous session raised would be revived by
 * returning to that session inside the window, reporting it running after it
 * had stopped.
 */
export function useLatchedRunning(slot: string | null | undefined, running: boolean): boolean {
  const key = slot ?? null
  const [latch, setLatch] = useState<{ slot: string | null; running: boolean }>({ slot: key, running })
  const lastKey = useRef(key)
  useEffect(() => {
    const switched = lastKey.current !== key
    lastKey.current = key
    if (running || switched) { setLatch({ slot: key, running }); return }
    const timer = setTimeout(() => setLatch({ slot: key, running: false }), RUNNING_LATCH_MS)
    return () => clearTimeout(timer)
  }, [running, key])
  return latch.slot === key ? latch.running : running
}
