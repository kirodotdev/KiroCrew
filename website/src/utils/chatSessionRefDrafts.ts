/**
 * Per-slot persistence for session references staged in the compose box before
 * send. Thin instance of `createSlotDraftStore`, alongside `chatDrafts` (text),
 * `chatFileDrafts` (attachments), and `chatPasteDrafts` (collapsed pastes).
 *
 * sessionStorage, matching `chatFileDrafts` rather than `chatDrafts`: a staged
 * ref points at another session that can be deleted or renamed while it sits in
 * the composer, so a ref surviving tab close would resurrect as a stale or
 * dangling chip. Scoping it to the tab bounds that window. No TTL / LRU cap —
 * the arrays are at most `MAX_SESSION_REFS` short records.
 *
 * Persisting at all (rather than clearing on slot switch) is what keeps refs
 * from leaking BETWEEN sessions: the composer reads the draft for the slot it is
 * showing, so switching away and back restores that slot's own refs and never
 * another slot's.
 */
import { createSlotDraftStore } from './slotDraftStore'
import { sanitizeSessionRefs, type SessionRef } from './sessionRefs'

export const SESSION_REF_DRAFTS_KEY = 'mc-chat-session-ref-drafts'

export type SessionRefDrafts = Record<string, SessionRef[]>

const store = createSlotDraftStore<SessionRef[]>({
  key: SESSION_REF_DRAFTS_KEY,
  storage: 'session',
  sanitize: sanitizeSessionRefs,
})

export const loadSessionRefDrafts = store.load
export const saveSessionRefDrafts = store.save
export const setSessionRefDraft = store.set
/** @internal test-only */
export const __resetSessionRefDraftsForTests = store.__resetForTests
