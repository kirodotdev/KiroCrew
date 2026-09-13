/**
 * Register THIS host's composer against its slot, for the close action's gate.
 *
 * ONE definition, called by every host that holds a slot-bound composer — the action
 * dispatcher for the hosts that offer the chip, and the embedded surfaces that do not.
 * A host that offers no chip still has to register: the gate asks "does any composer on
 * this slot hold unsent work", and the host that would LOSE a draft is not necessarily
 * the host that was clicked. SideChat is exactly that case — it drops the action chip,
 * so it never touches the dispatcher, yet a close fired from the main chat deletes the
 * slot its draft belongs to.
 *
 * `hasWork` is read at gate time through a ref, never captured, because the gate runs
 * after an awaited network write and the draft moves inside that window.
 */
import { useEffect, useLayoutEffect, useRef } from 'react'

import { publishComposerPresence, vetoLiveClosingIntent } from '../utils/slotClosingIntent'
import { nextComposerId, registerSlotComposer, slotIsQuiescing } from '../utils/slotComposerRegistry'
import { publishSlotDirty, retractSlotDirty, SLOT_DIRTY_REFRESH_MS } from '../utils/slotDirtyBeacon'

export function useSlotComposerRegistration(
  resolveSlot: () => string | null,
  hasUnsentWork: boolean,
  workIsRecoverable = true,
): void {
  const slotRef = useRef(resolveSlot)
  slotRef.current = resolveSlot
  const workRef = useRef(hasUnsentWork)
  workRef.current = hasUnsentWork

  const idRef = useRef<string | null>(null)
  if (idRef.current === null) idRef.current = nextComposerId()

  // Resolved during RENDER, not inside the effect, so it can be a dependency: a mounted
  // composer changes slots while staying dirty, and a ref read would not re-fire.
  const slot = resolveSlot()

  // Registered at commit for the same reason the claim is published there: a composer
  // that has painted is answerable for its draft.
  useLayoutEffect(() => {
    const id = idRef.current
    if (id === null) return
    return registerSlotComposer(id, {
      getSlot: () => slotRef.current(),
      hasWork: () => workRef.current,
    })
  }, [])

  // The registry answers only for THIS window, so the claim is mirrored to storage where
  // every window can read it. Re-fires on a slot change, or the old slot stays claimed.

  // A LAYOUT effect: a passive one runs after the browser PAINTS, so the composer was on
  // screen and dirty while storage still said clean, and a close elsewhere destroyed it.
  useLayoutEffect(() => {
    const id = idRef.current
    if (id === null) return
    publishSlotDirty(id, slot, hasUnsentWork, workIsRecoverable)
    // Answer a close ALREADY waiting: it read this slot while the composer was still clean,
    // so without this the work is destroyed with nothing having refused on its behalf.
    if (hasUnsentWork && slot) vetoLiveClosingIntent(slot, `composer-${id}`)
    // A close is COMMITTING for this slot, so this work was born inside the round-trip and
    // no store can answer for it yet: claim it unrecoverable whatever the caller passed.
    if (hasUnsentWork && slot && slotIsQuiescing(slot)) publishSlotDirty(id, slot, true, false)
  }, [hasUnsentWork, slot, workIsRecoverable])

  // A claim expires so a CRASHED window stops blocking closes forever. That cannot tell a
  // dead window from a quiet one, so a live dirty composer re-stamps its own claim.

  // Re-stamped with the SAME recoverability the publish used: re-stamping unrecoverable
  // work as recoverable would hand it the short TTL the publish deliberately withheld.
  useEffect(() => {
    const id = idRef.current
    if (id === null || !hasUnsentWork || !slot) return
    const timer = setInterval(
      () => publishSlotDirty(id, slot, true, workIsRecoverable),
      SLOT_DIRTY_REFRESH_MS,
    )
    return () => clearInterval(timer)
  }, [hasUnsentWork, slot, workIsRecoverable])

  useEffect(() => {
    const id = idRef.current
    if (id === null) return
    // Presence is stamped and bounded, so a LIVE composer must re-stamp or its own window
    // stops being seen. Unconditional: presence tracks the composer, not its dirtiness.
    const timer = setInterval(() => publishComposerPresence(id), SLOT_DIRTY_REFRESH_MS)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    const id = idRef.current
    if (id === null) return
    // `beforeunload` covers the window closing with the composer still mounted, where the
    // unmount cleanup below never runs.
    const drop = () => retractSlotDirty(id)
    window.addEventListener('beforeunload', drop)
    return () => {
      window.removeEventListener('beforeunload', drop)
      retractSlotDirty(id)
    }
  }, [])
}
