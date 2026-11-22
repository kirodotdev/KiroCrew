/** Reads the gateway has not confirmed yet (`mc-read-outbox`: slot -> newest time), kept across a reload.
 * Each counts as read until a `stored` echo or a row's `last_read_ts` covers it, and each new socket resends it. */
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { newerTs } from './slotReadRelay'
import { readCovers } from '../store/dashboard/unread'

export const READ_OUTBOX_KEY = 'mc-read-outbox'
export const READ_OUTBOX_MAX = 500
const pending = new Map<string, string>()

const load = (): Record<string, string> => {
  try {
    const raw = JSON.parse(safeGetItem(READ_OUTBOX_KEY) ?? '{}') as unknown
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) if (typeof v === 'string' && Number.isFinite(Date.parse(v))) out[k] = v
    return out
  } catch { return {} }
}

/** Run under one Web Lock all windows share, granted in request order; without Web Locks (jsdom) run at once. */
const locked = (run: () => void): void => {
  const locks = typeof navigator === 'undefined' ? undefined : navigator.locks
  if (typeof locks?.request === 'function') void locks.request(READ_OUTBOX_KEY, run)
  else run()
}

const update = (change: (entries: Record<string, string>) => boolean, saved?: () => void): void => locked(() => {
  const entries = load()
  if (change(entries)) {
    let kept = Object.entries(entries)
    if (kept.length > READ_OUTBOX_MAX) kept = kept.sort(([, a], [, b]) => Date.parse(a) - Date.parse(b)).slice(-READ_OUTBOX_MAX)
    if (!safeSetItem(READ_OUTBOX_KEY, JSON.stringify(Object.fromEntries(kept)))) return
  }
  saved?.()
})

export function queueRead(slot: string, readTs: string | undefined): void {
  if (!slot || !readTs || !Number.isFinite(Date.parse(readTs))) return
  pending.set(slot, newerTs(pending.get(slot), readTs) as string)
  update((entries) => {
    const next = newerTs(entries[slot], readTs)
    if (next === entries[slot]) return false
    entries[slot] = next as string
    return true
  }, () => { if (pending.get(slot) === readTs) pending.delete(slot) })
}

export const ackRead = (slot: string, storedTs: string | undefined): void => {
  if (readCovers(storedTs, pending.get(slot) ?? '')) pending.delete(slot)
  update((entries) => entries[slot] !== undefined && readCovers(storedTs, entries[slot]) && delete entries[slot])
}

export const queuedReads = (): Record<string, string> => {
  const out = load()
  for (const [slot, ts] of pending) out[slot] = newerTs(out[slot], ts) as string
  return out
}

/** Send every kept read once through *send*, after any write queued before it; a new socket calls this as it opens. */
export const resendKeptReads = (send: (slot: string, readTs: string) => void): void =>
  locked(() => { for (const [slot, readTs] of Object.entries(queuedReads())) send(slot, readTs) })
