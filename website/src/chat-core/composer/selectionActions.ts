import { useCallback, useMemo, useState, type Dispatch, type SetStateAction } from 'react'
import { quoteIntoDraft } from './quoteDraft'
import { seedSideChatDraft } from './sideChatDrafts'

/**
 * Selection actions — the two things a reader can do with text selected in an
 * assistant reply, besides copying it:
 *
 * - QUOTE: the selection lands in THIS surface's composer as a markdown
 *   blockquote (appended below whatever the user already typed), with a
 *   transit animation from the selection to the composer.
 * - ASK: the selection seeds the isolated Side Chat for THIS surface's slot,
 *   WITHOUT touching the main composer or the main context.
 *
 * Both used to be ChatPage-local, so every other host of `AssistantMessage`
 * (the split-view / Members `ChatPane`) offered Copy only. This is the one
 * implementation; hosts differ only in the things they genuinely own — the
 * composer draft (`setInput`), how the composer is brought into view after a
 * quote (`revealComposer`), and how a Side Chat surface for a slot is brought
 * on screen (`openSideChat`).
 *
 * The seed is a WRITE into the per-slot Side Chat draft store
 * (`sideChatDrafts`), not an event fired at the panel: a store entry waits for
 * the panel to mount and read it, so a Side Chat that comes up a frame late —
 * after `switchSlot`, a tab open and a drawer transition — still finds the
 * selection, where an event would have fired unheard.
 */

/** A quote in transit: the text and where on screen it was taken from. Feed
 *  it to `FlyingQuote`; clear it with `endQuoteFlight` when the flight ends. */
export interface QuoteFlight {
  text: string
  from: DOMRect
}

export interface SelectionQuoteAskOptions {
  /** Slot this surface shows. The seed is written under it. */
  slot?: string | null
  /** This surface's composer draft setter. */
  setInput: Dispatch<SetStateAction<string>>
  /** Bring this surface's composer into view once the quote has landed
   *  (focus on desktop, scroll on touch). Host-owned: chat-core does not know
   *  where the composer is. */
  revealComposer?: () => void
  /** Bring a Side Chat surface for `slot` on screen. Capability by omission:
   *  a host with no Side Chat surface passes nothing and gets no Ask action
   *  — never an Ask that fires into the void. Return `false` to REFUSE (an
   *  offline re-bind, a rejected slot): the selection is then not seeded, so
   *  a refused Ask leaves no hidden quote waiting in that slot's draft. A
   *  host that must first re-bind the page (split view's non-active pane)
   *  returns a Promise of that verdict — the seed waits for it, so a switch
   *  the server rejects (pane slot gone) strands nothing. */
  openSideChat?: (slot: string) => boolean | void | Promise<boolean | void>
}

export interface SelectionQuoteAsk {
  /** `AssistantMessage`'s `onQuote`. */
  onQuote: (text: string, rect: DOMRect) => void
  /** `AssistantMessage`'s `onAsk`; undefined when the host offers no Side Chat
   *  or there is no slot to seed. */
  onAsk: ((text: string) => void) | undefined
  quoteFlight: QuoteFlight | null
  endQuoteFlight: () => void
}

export function useSelectionQuoteAsk({ slot, setInput, revealComposer, openSideChat }: SelectionQuoteAskOptions): SelectionQuoteAsk {
  const [quoteFlight, setQuoteFlight] = useState<QuoteFlight | null>(null)
  const endQuoteFlight = useCallback(() => setQuoteFlight(null), [])

  const onQuote = useCallback((text: string, rect: DOMRect) => {
    setInput(prev => quoteIntoDraft(prev, text))
    setQuoteFlight({ text, from: rect })
    revealComposer?.()
  }, [setInput, revealComposer])

  // No transit animation for Ask: the popup routes the selection straight to
  // the Side Chat panel (matches Codex's "Ask in side chat" behaviour). Ask
  // needs both a surface to open and a slot to seed; without a slot there is
  // no transcript to select from, so nothing is lost by withholding it.
  const onAsk = useMemo(() => {
    if (!openSideChat || !slot) return undefined
    return (text: string) => {
      // Seed only when the surface actually opened: an opener that refuses
      // (offline, rejected slot) must not leave the quote sitting in the
      // slot's draft store to surface at some later, unrelated open. A
      // synchronous verdict seeds synchronously (the common case); a Promise
      // (a host that had to re-bind first) defers the seed until it settles.
      const verdict = openSideChat(slot)
      if (verdict instanceof Promise) {
        void verdict.then(ok => { if (ok !== false) seedSideChatDraft(slot, text) }, () => {})
        return
      }
      if (verdict === false) return
      seedSideChatDraft(slot, text)
    }
  }, [openSideChat, slot])

  return { onQuote, onAsk, quoteFlight, endQuoteFlight }
}
