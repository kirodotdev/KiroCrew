/**
 * The persisted copy of every delivery warning, so a reload or a crash cannot lose the words of a
 * send whose delivery was never confirmed.
 *
 * `deliveryWarning.ts` holds the same payload in module memory, which the page load destroys. By then
 * the composer was cleared and the optimistic bubble was store-only, so nothing else carries the
 * user's text. This store mirrors those records: written where a warning is raised, retired where
 * one is pruned or dismissed, or once the composer that got a recovered send back is emptied.
 *
 * sessionStorage, so a record belongs to the tab that saved it: a reload and a renderer crash keep
 * it, and no other tab can read it. One blob per tab; only this tab writes it, so a
 * read-modify-write cannot race another writer.
 */
import { safeGetSessionItem, safeSetSessionItem } from './safeStorage'
import { DRAFT_TTL_MS } from './draftConstants'

export const SEND_RECOVERY_KEY = 'mc-chat-send-recovery'

export type SendRecoverySurface = 'page' | 'pane'

export interface SendRecovery {
  sendId: string
  surface: SendRecoverySurface
  /** The raising surface's own restore payload. Opaque here: only that surface can parse it. */
  sent: unknown
}

/** A record as read back: when it was parked, and whether a page load already handed it back. */
export type LoadedSendRecovery = SendRecovery & { parkedAt: number; restored: boolean }

interface Stored extends SendRecovery { ts: number; restored?: true }

const keyOf = (slot: string, sendId: string): string => `${slot}|${sendId}`

const sanitize = (v: unknown): Stored | null => {
  if (typeof v !== 'object' || v === null) return null
  const r = v as Record<string, unknown>
  if (typeof r.sendId !== 'string' || !r.sendId) return null
  if (r.surface !== 'page' && r.surface !== 'pane') return null
  if (typeof r.sent !== 'object' || r.sent === null) return null
  if (typeof r.ts !== 'number' || !Number.isFinite(r.ts) || Date.now() - r.ts > DRAFT_TTL_MS) return null
  return { sendId: r.sendId, surface: r.surface, sent: r.sent, ts: r.ts, ...(r.restored === true ? { restored: true as const } : {}) }
}

/** A window another window opened (the chat pop-out) starts with a COPY of the opener's
 *  sessionStorage, records included, while the opener still holds the live warning for each. So
 *  the copy is dropped once, on this load's first access, unless this load is a reload of the
 *  opened window itself, whose records are its own. */
let copyChecked = false
const dropCopiedFromOpener = (): void => {
  if (copyChecked) return
  copyChecked = true
  try {
    if (!globalThis.opener) return
    const nav = performance?.getEntriesByType?.('navigation')?.[0] as { type?: string } | undefined
    if (nav?.type === 'reload') return
    globalThis.sessionStorage?.removeItem(SEND_RECOVERY_KEY)
  } catch { /* nothing readable to drop */ }
}

/** Test-only: run the opener check again, as a fresh page load would. */
export const __resetSendRecoveryLoadForTests = (): void => { copyChecked = false }

/** Every live record, dropping anything expired or malformed. */
const read = (): Record<string, Stored> => {
  dropCopiedFromOpener()
  const out: Record<string, Stored> = {}
  try {
    const parsed = JSON.parse(safeGetSessionItem(SEND_RECOVERY_KEY) || '{}') as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return out
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      const rec = sanitize(v)
      if (rec && k.endsWith(`|${rec.sendId}`)) out[k] = rec
    }
  } catch { /* unparseable: treated as empty, and the next write replaces it */ }
  return out
}

const write = (all: Record<string, Stored>): boolean => {
  if (Object.keys(all).length) return safeSetSessionItem(SEND_RECOVERY_KEY, JSON.stringify(all))
  try { globalThis.sessionStorage?.removeItem(SEND_RECOVERY_KEY) } catch { /* nothing left to keep */ }
  return true
}

/** Persist one warning. Returns whether it landed; a refused write leaves the stored records as
 *  they were, and the in-memory warning still offers Remove for this page load. */
export const parkSendRecovery = (slot: string, rec: SendRecovery): boolean => {
  if (!slot || !rec.sendId) return false
  const all = read()
  all[keyOf(slot, rec.sendId)] = { sendId: rec.sendId, surface: rec.surface, sent: rec.sent, ts: Date.now() }
  return write(all)
}

export const forgetSendRecovery = (slot: string, sendId: string): void => {
  const all = read()
  const k = keyOf(slot, sendId)
  if (!(k in all)) return
  delete all[k]
  write(all)
}

/** Note that a page load handed this send back, so a later load restores it only if the composer
 *  lost it. */
export const markSendRecoveryRestored = (slot: string, sendId: string): void => {
  const all = read()
  const rec = all[keyOf(slot, sendId)]
  if (!rec || rec.restored) return
  rec.restored = true
  write(all)
}

/** Every record for `slot`, oldest park first. */
export const loadSendRecoveries = (slot: string): LoadedSendRecovery[] => {
  if (!slot) return []
  return Object.entries(read())
    .filter(([k]) => k.startsWith(`${slot}|`))
    .map(([, r]) => ({ sendId: r.sendId, surface: r.surface, sent: r.sent, parkedAt: r.ts, restored: !!r.restored }))
    .sort((a, b) => a.parkedAt - b.parkedAt)
}

/** Retire the handed-back records `surface` holds for `slot`: its composer was emptied, by a send
 *  or a clear, so the text they guarded is gone on purpose. Records still waiting are kept. */
export const forgetRestoredSendRecoveries = (slot: string, surface: SendRecoverySurface): void => {
  const all = read()
  let changed = false
  for (const [k, r] of Object.entries(all)) {
    if (k.startsWith(`${slot}|`) && r.surface === surface && r.restored) { delete all[k]; changed = true }
  }
  if (changed) write(all)
}
