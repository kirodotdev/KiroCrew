/** One LLM title generation per slot at a time, shared by every control that
 *  offers it (the sidebar row, the phone top bar and each pane header's
 *  Auto-title button), and a per-slot "generating" flag each of them reads to
 *  show a spinner while the model call runs. The header claims the slot through
 *  `claimSlotTitleGeneration` and handles the answer itself, because it also
 *  offers Undo. */
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
  const call = claimSlotTitleGeneration(slot)
  if (!call) return null
  return call.then(r => {
    /* title is redacted server-side via redact_exfiltration_urls + redact_credentials */
    if (r.title) dispatch(sseSlotTitle({ key: slot, title: r.title }))
  })
}

/** Start the model call for `slot` and mark it generating until it settles, or
 *  return null when one is already running. The caller applies the answer. */
export function claimSlotTitleGeneration(slot: string): ReturnType<typeof api.generateTitle> | null {
  if (inFlight.has(slot)) return null
  inFlight.add(slot)
  notify()
  return api.generateTitle(slot).finally(() => { inFlight.delete(slot); notify() })
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

/** True while a title generation for `slot` is running, whoever started it. */
export function useSlotTitleGenerating(slot: string): boolean {
  return useSyncExternalStore(subscribe, () => inFlight.has(slot))
}
