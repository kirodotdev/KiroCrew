import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { MessageQuote } from '../../chat-core/composer/messageQuote'
import type { JumpReveal } from '../../hooks/JumpRevealContext'
import { i18nT } from '../../i18n/t'
import type { ChatMessage } from '../../types'
import { buildMessageToDisplayIdx, supersededSuccessorIdx } from './groupDisplayItems'
import type { DisplayItem } from './types'

/** How long the landed row wears its ring; the main chat's pin and quote
 *  landing uses the same 3 s, written inline there. */
const QUOTE_JUMP_RING_MS = 3000
/** How long the not-loaded notice stays: the main chat's own quote/pin notice
 *  clears after the same 8 s (`transcriptPins`). */
const QUOTE_NOTICE_MS = 8000

/** The index in `messages` a quote names: by `mid` when it carries one, by
 *  `ts` only when it does not, as the main chat's jump resolves it (a ts match
 *  with a mid in hand can take a same-tick twin). Lands on the row that ran in
 *  a superseded steer's place. -1 when the quoted row is not loaded. */
function quotedMessageIndex(messages: readonly ChatMessage[], quote: MessageQuote): number {
  const at = quote.mid
    ? messages.findIndex((m) => (m.meta as Record<string, unknown> | undefined)?.mid === quote.mid)
    : messages.findIndex((m) => !!quote.ts && m.ts === quote.ts)
  if (at < 0) return -1
  const next = supersededSuccessorIdx(messages as ChatMessage[], at)
  return next >= 0 ? next : at
}

export interface QuoteJumpOptions {
  /** The messages the surface's list draws, in the order its rows index them. */
  messages: ChatMessage[]
  /** The display items that list grouped `messages` into (its `onDisplayItems`). */
  getDisplayItems: () => readonly DisplayItem[]
  /** Bring display row `index` into view in the surface's own scroller. */
  land: (index: number) => void
  /** A new value (another session bound into a surface that is not
   *  remounted for it) drops the ring, the notice and a pending jump. */
  resetKey?: string
  /** Every row the surface holds, when its list folds some away from
   *  `messages`, and how to unfold them. */
  fold?: { messages: ChatMessage[]; open: () => void }
  /** The surface's transcript scroller. */
  scrollerEl: () => HTMLElement | null
  /** Ends a landing scroll that outlives `land`, so a turn's own scroll to the
   *  row inside its opened step fold is not undone. */
  claimScroll?: () => void
}

/**
 * A sent quote card's jump on a surface that is not the main chat (a split-view
 * pane, the crew window): find the quoted row among the rows THIS surface has
 * loaded, land it through the surface's own scroll path, ring it for
 * QUOTE_JUMP_RING_MS, and when it is not loaded say so: the row may still
 * exist past the window this surface holds, so the notice says "not loaded
 * here", not the main chat's "no longer in this conversation". It clears
 * itself after QUOTE_NOTICE_MS, as the main chat's does. A row held but folded
 * away (`fold`) opens the fold and lands once `messages` draws it. No paging:
 * the surface reports what it holds. `reveal` is the landed row for the
 * surface's JumpRevealContext, so a turn whose step fold holds it opens.
 */
export function useQuoteJump({ messages, getDisplayItems, land, resetKey, fold, scrollerEl, claimScroll }: QuoteJumpOptions) {
  const [target, setTarget] = useState<{ ts: string; mid: string | null } | null>(null)
  const [seq, setSeq] = useState(0)
  const [notice, setNotice] = useState<string | null>(null)
  // Refs, so the handler keeps one identity across streamed frames: it rides
  // into every user row, and a new identity per frame would redraw them all.
  const live = useRef({ messages, getDisplayItems, land, fold, scrollerEl, claimScroll })
  live.current = { messages, getDisplayItems, land, fold, scrollerEl, claimScroll }
  const pending = useRef<MessageQuote | null>(null)
  const ringTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const clearRing = useCallback(() => {
    if (ringTimer.current) clearTimeout(ringTimer.current)
    ringTimer.current = null
  }, [])
  useEffect(() => clearRing, [clearRing])
  useEffect(() => { clearRing(); setTarget(null); setNotice(null); pending.current = null }, [resetKey, clearRing])
  useEffect(() => {
    if (!notice) return
    const timeout = setTimeout(() => setNotice(null), QUOTE_NOTICE_MS)
    return () => clearTimeout(timeout)
  }, [notice])

  const resolve = useCallback((quote: MessageQuote, mayUnfold: boolean) => {
    const { messages: msgs, getDisplayItems: items, land: landRow, fold: held } = live.current
    pending.current = null
    const idx = quotedMessageIndex(msgs, quote)
    const di = idx < 0 ? undefined : buildMessageToDisplayIdx([...items()]).get(idx)
    if (di === undefined) {
      if (mayUnfold && idx < 0 && held && quotedMessageIndex(held.messages, quote) >= 0) {
        pending.current = quote
        held.open()
        return
      }
      setNotice(i18nT('pages.chat.quoteCard.message_not_loaded'))
      return
    }
    setNotice(null)
    landRow(di)
    clearRing()
    const ts = msgs[idx].ts ?? quote.ts
    const mid = (msgs[idx].meta as Record<string, unknown> | undefined)?.mid
    setTarget(ts ? { ts, mid: typeof mid === 'string' ? mid : null } : null)
    setSeq((n) => n + 1)
    ringTimer.current = setTimeout(() => { ringTimer.current = null; setTarget(null) }, QUOTE_JUMP_RING_MS)
  }, [clearRing])
  const onJumpToQuote = useCallback((quote: MessageQuote) => resolve(quote, true), [resolve])
  // Passive, so the list's layout-effect `onDisplayItems` has already reported
  // the unfolded rows `getDisplayItems` reads.
  useEffect(() => {
    if (pending.current) resolve(pending.current, false)
  }, [messages, resolve])
  const dismissNotice = useCallback(() => setNotice(null), [])
  const revealClaimScroll = useCallback(() => live.current.claimScroll?.(), [])
  const revealScrollerEl = useCallback(() => live.current.scrollerEl(), [])
  const reveal = useMemo<JumpReveal>(
    () => ({ ts: target?.ts ?? null, mid: target?.mid ?? null, seq, claimScroll: revealClaimScroll, scrollerEl: revealScrollerEl }),
    [target, seq, revealClaimScroll, revealScrollerEl],
  )

  return { onJumpToQuote, reveal, notice, dismissNotice }
}
