import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'

/** How long a row the reader was sent to (a `link` jump) keeps its landing
 *  highlight: the length of the `msg-landed` animation in `tailwind-theme.css`. */
export const LANDED_HIGHLIGHT_MS = 5000
/** How long a jump made inside the chat (a pin, a quote) keeps its flash. */
export const FLASH_HIGHLIGHT_MS = 3000
/** How long the prompt an Alt+↑ / Alt+↓ step lands on keeps its flash: the
 *  length of its `msg-highlight` fade. */
export const STEP_HIGHLIGHT_MS = 2000

/** Marks the row a jump landed on. `landed` picks the landing highlight over the
 *  flash; `mid`, when the message has one, singles it out among same-`ts` rows. */
export type MarkJump = (ts: string, landed: boolean, mid?: string) => void
/** Flashes the prompt a turn step landed on. */
export type MarkStep = (ts: string) => void

/** The message a jump marked, by both keys. */
export interface JumpMarkTarget { ts: string; mid: string | null }

/**
 * The one owner of a jump's highlight and its expiry. Each mark replaces the
 * last: it clears the other kind, and it cancels the previous expiry, so an
 * older jump's timer can never clear a newer jump's target (and with it the
 * fold reveal and row scroll that target drives).
 *
 * A turn step (`markStep`) also replaces the last mark, but is no jump: it
 * clears the jump target, so no fold reveal or row scroll runs for it, and an
 * earlier jump's row cannot pull the reader back from the step.
 */
export function useJumpMark(
  setHighlightTs: Dispatch<SetStateAction<string | null>>,
  setLandedTs: Dispatch<SetStateAction<string | null>>,
): { markJump: MarkJump; markStep: MarkStep; jumpSeq: number; jumpTarget: JumpMarkTarget | null } {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Counts every mark, so a repeat jump to the row already marked (a second click
  // on the same pin) still reruns the effects keyed on the target.
  const [jumpSeq, setJumpSeq] = useState(0)
  const [jumpTarget, setJumpTarget] = useState<JumpMarkTarget | null>(null)
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])
  const markJump = useCallback((ts: string, landed: boolean, mid?: string) => {
    if (timerRef.current) clearTimeout(timerRef.current)
    setJumpSeq(n => n + 1)
    setJumpTarget({ ts, mid: mid ?? null })
    const mark = landed ? setLandedTs : setHighlightTs
    if (landed) setHighlightTs(null)
    else setLandedTs(null)
    mark(ts)
    timerRef.current = setTimeout(() => {
      timerRef.current = null
      mark(null)
      setJumpTarget(null)
    }, landed ? LANDED_HIGHLIGHT_MS : FLASH_HIGHLIGHT_MS)
  }, [setHighlightTs, setLandedTs])
  const markStep = useCallback((ts: string) => {
    if (timerRef.current) clearTimeout(timerRef.current)
    setJumpTarget(null)
    setLandedTs(null)
    setHighlightTs(ts)
    timerRef.current = setTimeout(() => {
      timerRef.current = null
      setHighlightTs(null)
    }, STEP_HIGHLIGHT_MS)
  }, [setHighlightTs, setLandedTs])
  return { markJump, markStep, jumpSeq, jumpTarget }
}
