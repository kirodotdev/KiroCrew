/**
 * Per-slot persistence for a pane send the transport handed back — the text, the
 * attachments and the send id it was minted with.
 *
 * `ChatPane` used to hold this in a component ref (`strandedSends`), so the payload
 * of a send that timed out before reaching the gateway existed in exactly one place
 * that a reload destroyed. The composer had already been cleared, and the optimistic
 * bubble is store-only, so nothing else carried the user's words.
 *
 * localStorage, deliberately, and on the same TTL as `chatDrafts`. This store owns BOTH
 * surfaces' records — the pane's payload and ChatPage's marker-only form — because they are
 * one concept and were previously two stores that had to expire together by hand. A payload in
 * sessionStorage would die on tab close while a marker survived, leaving a warning about a send
 * whose text is gone — strictly worse than losing both.
 */
import { createSlotDraftStore } from './slotDraftStore'
import { DRAFT_MAX_ENTRIES, DRAFT_MAX_STORE_BYTES, DRAFT_TTL_MS } from './draftConstants'

export const PANE_RECOVERY_KEY = 'mc-chat-pane-recovery'

export interface PaneRecovery {
  /** Empty on ChatPage's marker-only record: it names the send without carrying a payload. */
  text: string
  files: string[]
  sendId?: string
  /** Bumped on every write, so a receipt can tell the payload it consumed from a newer draft. */
  gen?: number
  /** The SEND's own fragment, distinct from `text` when the composer had mid-flight work merged
   *  into it. Gates the Discard exit, which must never offer to delete more than it restored. */
  sent?: string
  sentFiles?: string[]
}

export type PaneRecoveries = Record<string, PaneRecovery>

/** Reject anything not shaped like a recovery, so a hand-edited or older value is
 *  dropped rather than restored as a half-record. */
const sanitize = (v: unknown): PaneRecovery | null => {
  if (typeof v !== 'object' || v === null) return null
  const r = v as Record<string, unknown>
  const text = typeof r.text === 'string' ? r.text : ''
  const files = Array.isArray(r.files) ? r.files.filter((f): f is string => typeof f === 'string') : []
  const sendId = typeof r.sendId === 'string' && r.sendId ? r.sendId : undefined
  // A record must carry SOMETHING: a payload, or the send id whose caption it drives.
  if (!text && !files.length && !sendId) return null
  const gen = typeof r.gen === 'number' && Number.isFinite(r.gen) ? r.gen : undefined
  const sent = typeof r.sent === 'string' ? r.sent : undefined
  const sentFiles = Array.isArray(r.sentFiles) ? r.sentFiles.filter((f): f is string => typeof f === 'string') : undefined
  return {
    text,
    files,
    ...(sendId ? { sendId } : {}),
    ...(gen !== undefined ? { gen } : {}),
    ...(sent !== undefined ? { sent } : {}),
    ...(sentFiles !== undefined ? { sentFiles } : {}),
  }
}

const store = createSlotDraftStore<PaneRecovery>({
  key: PANE_RECOVERY_KEY,
  storage: 'local',
  ttlMs: DRAFT_TTL_MS,
  // Same limits as `chatDrafts`, and for the same reason: unbounded growth ends in a quota
  // failure whose victim is the NEWEST write, so the record a reload needs is the one lost.
  maxEntries: DRAFT_MAX_ENTRIES,
  maxStoreBytes: DRAFT_MAX_STORE_BYTES,
  sanitize,
})

export const loadPaneRecoveries = store.load
export const savePaneRecoveries = store.save
export const setPaneRecovery = store.set
/** @internal test-only */
export const __resetPaneRecoveryForTests = store.__resetForTests

/** ChatPage's marker for a restored send, in this same store under a SURFACE-QUALIFIED key.
 *
 *  Qualified because the two surfaces can address ONE slot at the same time: `MembersPage`
 *  renders `<ChatPane slotKey={activeSlot}>`, so an unqualified key would let the page's
 *  marker overwrite the pane's payload for that slot — losing exactly the words this store
 *  exists to keep. One store, one record shape, one TTL; two records that cannot collide. */
export const stagedSendKey = (slot: string): string => `page:${slot}`
const pageKey = stagedSendKey

export const loadStagedSend = (slot: string): string | undefined =>
  loadPaneRecoveries()[pageKey(slot)]?.sendId

export const setStagedSend = (slot: string, sendId: string): void => {
  const all = loadPaneRecoveries()
  setPaneRecovery(all, pageKey(slot), { text: '', files: [], sendId })
  savePaneRecoveries(all)
}

export const clearStagedSend = (slot: string): void => {
  const all = loadPaneRecoveries()
  if (!all[pageKey(slot)]) return
  delete all[pageKey(slot)]
  savePaneRecoveries(all)
}
