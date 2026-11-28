/** The persisted copy of every delivery warning, so a reload or crash cannot lose an unconfirmed
 *  send's text. sessionStorage: a record belongs to its tab, and only that tab writes the blob. */
import { safeGetSessionItem, safeSetSessionItem } from './safeStorage'
import { DRAFT_TTL_MS } from './draftConstants'
import { ownsStoredSession } from './windowStorageOwner'

export const SEND_RECOVERY_KEY = 'mc-chat-send-recovery'

export type SendRecoverySurface = 'page' | 'pane'

export interface SendRecovery {
  sendId: string
  surface: SendRecoverySurface
  /** The raising surface's own restore payload. Opaque here: only that surface can parse it. */
  sent: unknown
}

export type LoadedSendRecovery = SendRecovery & { parkedAt: number }

interface Stored extends SendRecovery { ts: number }

const keyOf = (slot: string, sendId: string): string => `${slot}|${sendId}`

const sanitize = (v: unknown): Stored | null => {
  if (typeof v !== 'object' || v === null) return null
  const r = v as Record<string, unknown>
  if (typeof r.sendId !== 'string' || !r.sendId) return null
  if (r.surface !== 'page' && r.surface !== 'pane') return null
  if (typeof r.sent !== 'object' || r.sent === null) return null
  if (typeof r.ts !== 'number' || !Number.isFinite(r.ts) || Date.now() - r.ts > DRAFT_TTL_MS) return null
  return { sendId: r.sendId, surface: r.surface, sent: r.sent, ts: r.ts }
}

export const SEND_RECOVERY_WINDOW_KEY = 'mc-chat-send-recovery-window'

/** A pop-out starts with a COPY of its opener's live records, so the copy is dropped on first access. */
let copyChecked = false
const dropCopiedFromOpener = (): void => {
  if (copyChecked) return
  copyChecked = true
  if (ownsStoredSession(SEND_RECOVERY_WINDOW_KEY)) return
  try { globalThis.sessionStorage?.removeItem(SEND_RECOVERY_KEY) } catch { /* nothing readable to drop */ }
}

export const __resetSendRecoveryLoadForTests = (): void => { copyChecked = false }

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

/** Persist one warning; whether it landed. A refused write leaves the stored records as they were. */
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

export const loadSendRecoveries = (slot: string): LoadedSendRecovery[] => {
  if (!slot) return []
  return Object.entries(read())
    .filter(([k]) => k.startsWith(`${slot}|`))
    .map(([, r]) => ({ sendId: r.sendId, surface: r.surface, sent: r.sent, parkedAt: r.ts }))
    .sort((a, b) => a.parkedAt - b.parkedAt)
}
