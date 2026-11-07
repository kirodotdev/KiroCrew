import { useEffect, useReducer, useRef } from 'react'
import { anyWindowSlotDirty, publishSlotDirty, retractSlotDirty, SLOT_DIRTY_KEY_PREFIX, SLOT_DIRTY_REFRESH_MS } from '../utils/slotDirtyBeacon'
import { readSideDraftForSlot, SIDE_DRAFT_KEY_PREFIX } from '../utils/sideComposerDrafts'
import { notifySlotWorkChange, onSlotWorkChange } from '../utils/slotWorkChanges'
import type { UnsentWorkSource } from '../components/FollowUpBar'
import { CLOSING_KEY_PREFIX, publishComposerPresence, vetoLiveClosingIntent } from '../utils/slotClosingIntent'

/** Composers in THIS window that hold unsent work, by composer id. Another window learns
 *  about them through the dirty beacon; this window's own closer reads this map, since a
 *  `storage` event never reaches the window that wrote it. */
const localWork = new Map<string, string>()

/** Does a composer in this window, other than *exceptId*, hold unsent work for *slot*? */
export function localComposerHoldsWork(slot: string, exceptId?: string): boolean {
  for (const [id, s] of localWork) if (s === slot && id !== exceptId) return true
  return false
}

/** Where OTHER than composer *exceptComposer* this slot holds unsent work, so the close chip
 *  can block with the right reason. A pane of this window is checked first because the
 *  user can reach it without leaving. */
export function slotUnsentWorkSource(slot: string, exceptComposer?: string): UnsentWorkSource | null {
  if (localComposerHoldsWork(slot, exceptComposer) || readSideDraftForSlot(slot) !== null) return 'elsewhere'
  return anyWindowSlotDirty(slot, exceptComposer) ? 'other-window' : null
}

/**
 * `slotUnsentWorkSource`, kept current: it re-reads when a composer in this window claims
 * or drops work or a side draft changes, when another window's claim or draft changes, and,
 * while it blocks, on the claim refresh cadence so a claim that expired unretracted clears.
 * Null when not *enabled*.
 */
export function useSlotUnsentWorkSource(
  slot: string | null | undefined,
  exceptComposer: string,
  enabled: boolean,
): UnsentWorkSource | null {
  const [, rerender] = useReducer((n: number) => n + 1, 0)
  const source = enabled && slot ? slotUnsentWorkSource(slot, exceptComposer) : null
  useEffect(() => {
    if (!enabled || !slot) return
    const offLocal = onSlotWorkChange(rerender)
    const onStorage = (e: StorageEvent) => {
      if (e.key === null || e.key.startsWith(SLOT_DIRTY_KEY_PREFIX) || e.key.startsWith(SIDE_DRAFT_KEY_PREFIX)) rerender()
    }
    window.addEventListener('storage', onStorage)
    return () => {
      offLocal()
      window.removeEventListener('storage', onStorage)
    }
  }, [enabled, slot])
  useEffect(() => {
    if (source === null) return
    const recheck = setInterval(rerender, SLOT_DIRTY_REFRESH_MS)
    return () => clearInterval(recheck)
  }, [source])
  return source
}

let nextId = 0

/**
 * Answer for this composer's unsent work across panes and windows.
 *
 * While *dirty*, it publishes and refreshes a dirty claim on *slot*, vetoes any close of
 * *slot* already in flight, and vetoes each new closing intent another window announces.
 * Clean, unmounted or unloading, it retracts the claim.
 */
export function useSlotWorkBeacon(slot: string | null | undefined, dirty: boolean): string {
  const idRef = useRef<string>('')
  if (!idRef.current) idRef.current = `composer-${Date.now().toString(36)}-${(nextId += 1)}`
  const id = idRef.current

  useEffect(() => {
    let unpublish = publishComposerPresence(id)
    // A presence stamp goes stale, so it is re-stamped on the same cadence as a claim.
    const restamp = setInterval(() => { unpublish = publishComposerPresence(id) }, SLOT_DIRTY_REFRESH_MS)
    // React runs no cleanup on unload, so the presence stamp is dropped here too: a
    // composer id is fresh per mount, and a stamp left behind is never removed.
    const onUnload = () => { retractSlotDirty(id); unpublish() }
    window.addEventListener('beforeunload', onUnload)
    return () => {
      clearInterval(restamp)
      window.removeEventListener('beforeunload', onUnload)
      unpublish()
    }
  }, [id])

  useEffect(() => {
    if (!slot || !dirty) return
    localWork.set(id, slot)
    notifySlotWorkChange()
    publishSlotDirty(id, slot, true)
    vetoLiveClosingIntent(slot, id)
    const refresh = setInterval(() => { publishSlotDirty(id, slot, true) }, SLOT_DIRTY_REFRESH_MS)
    const onStorage = (e: StorageEvent) => {
      if (e.key?.startsWith(`${CLOSING_KEY_PREFIX}intent:`)) vetoLiveClosingIntent(slot, id)
    }
    window.addEventListener('storage', onStorage)
    return () => {
      clearInterval(refresh)
      window.removeEventListener('storage', onStorage)
      localWork.delete(id)
      retractSlotDirty(id)
      notifySlotWorkChange()
    }
  }, [id, slot, dirty])

  return id
}

/** @internal test-only. */
export function __resetSlotWorkBeaconForTests(): void {
  localWork.clear()
}
