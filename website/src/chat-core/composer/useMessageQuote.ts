import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { type MessageQuote, type MessageQuoteRole, prependQuote, quoteFromMessage } from './messageQuote'

/**
 * Host-side state for quoting a whole message into the next send.
 *
 * One quote at a time, per surface: quoting another message REPLACES the staged
 * one (the card in the composer shows which). The staged quote is scoped to the
 * slot it was taken from -- a slot switch drops it, since a quote of a message
 * in conversation A is meaningless as the opening of a send into B, and the
 * transcript it points at is no longer on screen to check.
 *
 * `consume()` is what `send()` calls: it hands back the record and the text to
 * send, and clears the stage in the same step so a failed send that restores
 * the composer does not silently re-quote (the host decides whether to
 * `restage` it, exactly as it decides for files and session refs).
 */
export interface UseMessageQuote {
  pendingQuote: MessageQuote | null
  /** Stage `content` as the quote. A row with nothing quotable is a no-op. */
  quoteMessage: (role: MessageQuoteRole, content: string, ts?: string, mid?: string) => void
  clearQuote: () => void
  /** Put a previously consumed quote back (a failed send) -- ONLY when nothing
   *  newer is staged: a quote the user staged while the send was in flight is
   *  theirs and must not be replaced. Returns whether it took; a caller whose
   *  restage did not take carries the quote in the recovered text instead
   *  (`recoverInto`). */
  restage: (quote: MessageQuote | null) => boolean
  /** Recover a consumed quote into a restored draft: restages it when the
   *  stage is free and returns `text` unchanged, else returns `text` with the
   *  quote's block prepended so the quoted context survives in the draft.
   *  For a composer that is NOT live (the slot is off screen, another pane)
   *  pass `live: false` -- there is no stage to put it on, so the block goes
   *  into the parked text. */
  recoverInto: (text: string, quote: MessageQuote | null, live?: boolean) => string
  /** Take the staged quote for a send: `{ quote, text }` with the block
   *  prepended to `typed`, or `{ quote: null, text: typed }` when none. */
  consume: (typed: string) => { quote: MessageQuote | null; text: string }
}

export function useMessageQuote({ slot, revealComposer, assistantName }: {
  /** The slot the surface shows; the stage is dropped when it changes. */
  slot?: string | null
  /** Bring the composer into view once the quote is staged. */
  revealComposer?: () => void
  /** How this surface labels the assistant's replies (a crewmate's name in
   *  its DM). Stamped on assistant quotes as `author`, so the card names the
   *  speaker the transcript names. Absent: the generic role label. */
  assistantName?: string
}): UseMessageQuote {
  const [pendingQuote, setPendingQuote] = useState<MessageQuote | null>(null)
  const pendingRef = useRef<MessageQuote | null>(null)
  // Slot-scoped: drop the stage on a switch, not on mount. Both the ref and
  // the state go in THIS render pass, before any effect of this commit runs:
  // a send effect that fires on the switch itself (an armed auto-send) reads
  // the ref through `consume`, and a stage cleared only by a later effect
  // would hand the old slot's quote to the new slot's send.
  const lastSlot = useRef(slot)
  const switched = lastSlot.current !== slot
  pendingRef.current = switched ? null : pendingQuote
  useLayoutEffect(() => {
    if (!switched) return
    lastSlot.current = slot
    setPendingQuote(null)
  }, [switched, slot])

  const quoteMessage = useCallback((role: MessageQuoteRole, content: string, ts?: string, mid?: string) => {
    const q = quoteFromMessage(role, content, ts, mid, role === 'assistant' ? assistantName : undefined)
    if (!q) return
    pendingRef.current = q
    setPendingQuote(q)
    revealComposer?.()
  }, [revealComposer, assistantName])
  const clearQuote = useCallback(() => { pendingRef.current = null; setPendingQuote(null) }, [])
  const restage = useCallback((q: MessageQuote | null) => {
    if (!q || pendingRef.current) return false
    pendingRef.current = q
    setPendingQuote(q)
    return true
  }, [])
  const recoverInto = useCallback((text: string, q: MessageQuote | null, live = true) => {
    if (!q) return text
    if (live && restage(q)) return text
    return prependQuote(text, q)
  }, [restage])
  const consume = useCallback((typed: string) => {
    const q = pendingRef.current
    if (!q) return { quote: null, text: typed }
    pendingRef.current = null
    setPendingQuote(null)
    return { quote: q, text: prependQuote(typed, q) }
  }, [])

  return { pendingQuote, quoteMessage, clearQuote, restage, recoverInto, consume }
}
