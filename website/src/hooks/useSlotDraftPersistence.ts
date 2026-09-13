/**
 * Persist a side/embedded composer's draft against its slot.
 *
 * The close guard's cross-window claim expires, so an in-memory draft in a window that
 * froze in the background became invisible and a close elsewhere destroyed it. Writing
 * the text to storage is what makes expiry safe: the guard's persisted fallback never
 * ages out, so the draft stays discoverable after the claim is gone.
 *
 * Flushes on the debounce alone. An UNMOUNT deliberately CLEARS ITS OWN entry instead: the
 * panel being dismissed takes its draft with it, so a persisted copy would block that slot's
 * close for the store's whole TTL while nothing on screen held the draft it named.
 *
 * That still closes the hole this exists for, because the case it guards is a window that
 * FREEZES — which never unmounts. Its debounced write has already landed, so the draft stays
 * discoverable after the cross-window claim ages out. A window that crashes outright runs no
 * cleanup either, so its copy survives too.
 *
 * Returns whether the CURRENT text is already durable. Until the debounce fires there is no
 * copy for another window to find, so a claim published as recoverable in that window would
 * lean on a fallback that does not exist yet — which is what the caller tiers its claim on.
 */
import { useEffect, useRef, useState } from 'react'

import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'
import { nextComposerId, slotIsQuiescing } from '../utils/slotComposerRegistry'
import { clearSideDraft, readSideDraftForSlot, writeSideDraft } from '../utils/sideComposerDrafts'

export function useSlotDraftPersistence(
  slot: string | null,
  text: string,
  onRestore?: (recovered: string) => void,
): boolean {
  const latest = useRef({ slot, text })
  latest.current = { slot, text }
  // Minted here rather than taken as a prop, so no host has to thread it and no host can
  // pass one that collides. The id is unique across windows, not just within this one.
  const composerId = useRef<string>()
  if (!composerId.current) composerId.current = nextComposerId()
  const id = composerId.current
  const [persisted, setPersisted] = useState<{ slot: string; text: string } | null>(null)
  const heldForClose = useRef(false)

  useEffect(() => {
    if (!slot) return
    // Reset FIRST: this text is not the text the last write stored.
    setPersisted(null)
    heldForClose.current = false
    // A close is COMMITTING, and the archive OUTRUNS the debounce: write NOW, so the durable
    // copy precedes the irreversible deletion instead of a check trailing it.
    if (slotIsQuiescing(slot)) {
      const stored = writeSideDraft(id, slot, text)
      // That close's own broadcast unmounts this composer, and the teardown below would
      // delete the very copy this write exists to leave behind. Hold it instead.
      heldForClose.current = stored
      setPersisted(stored ? { slot, text } : null)
      return
    }
    const timer = setTimeout(() => {
      // The WRITE'S OWN answer, not the fact that it was attempted: a nearly-full or
      // disabled store fails it, and there is then no copy for the short TTL to lean on.
      setPersisted(writeSideDraft(id, slot, text) ? { slot, text } : null)
    }, DRAFT_SAVE_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [id, slot, text])

  const restoredFor = useRef<string | null>(null)
  useEffect(() => {
    if (!slot || !onRestore || restoredFor.current === slot) return
    restoredFor.current = slot
    // Only into an EMPTY composer: text already on screen outranks a stored copy, and the
    // close that persisted it is the path that leaves this mount with nothing.
    if (latest.current.text.trim().length > 0) return
    const recovered = readSideDraftForSlot(slot)
    if (recovered !== null) onRestore(recovered)
  }, [slot, onRestore])

  useEffect(() => () => {
    // Removes THIS composer's key only. A sibling bound to the same slot keeps its own, so
    // dismissing one panel cannot discard a draft still on screen in another.
    if (latest.current.slot && !heldForClose.current) clearSideDraft(id)
  }, [id])

  // Compared against the CURRENT pair, not returned as a bare flag: the reset above runs in a
  // passive effect, which React flushes AFTER the layout effect that publishes this claim.
  return persisted !== null && persisted.slot === slot && persisted.text === text
}
