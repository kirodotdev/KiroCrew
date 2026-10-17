/** "Regenerate title" from a menu item: one LLM title generation per slot at a
 *  time, shared by every menu that offers it (the sidebar row and the phone top
 *  bar), and a per-slot "generating" flag the row reads to show a spinner while
 *  the model call runs. The header's hover button keeps its own path because it
 *  also offers Undo. */
import { useSyncExternalStore } from 'react'
import { api } from '../api/client'
import { sseSlotTitle } from '../store/dashboardSlice'
import type { AppDispatch } from '../store'

const inFlight = new Set<string>()
const listeners = new Set<() => void>()
const notify = () => { for (const l of listeners) l() }

/** Start a title generation for `slot`, or return null when one is already
 *  running for it: a second pick would pay for a second model call whose
 *  answer races the first. The returned promise rejects with the server error,
 *  for the caller to surface; on success the new title is already in the store. */
export function generateSlotTitle(slot: string, dispatch: AppDispatch): Promise<void> | null {
  if (inFlight.has(slot)) return null
  inFlight.add(slot)
  notify()
  return api.generateTitle(slot).then(r => {
    /* title is redacted server-side via redact_exfiltration_urls + redact_credentials */
    if (r.title) dispatch(sseSlotTitle({ key: slot, title: r.title }))
  }).finally(() => { inFlight.delete(slot); notify() })
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

/** True while a menu-started title generation for `slot` is running. */
export function useSlotTitleGenerating(slot: string): boolean {
  return useSyncExternalStore(subscribe, () => inFlight.has(slot))
}
