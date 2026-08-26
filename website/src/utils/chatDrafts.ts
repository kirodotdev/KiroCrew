/**
 * Per-slot chat draft persistence. Drafts survive tab close, refresh, and
 * browser crashes via localStorage. Thin instance of `createSlotDraftStore`
 *; all behavior (TTL, LRU, byte-aware eviction, corruption guards,
 * quota-safe write order) lives in the factory.
 */
import { createSlotDraftStore } from './slotDraftStore'
import { DRAFT_MAX_ENTRIES, DRAFT_MAX_STORE_BYTES, DRAFT_TTL_MS, DRAFT_SAVE_DEBOUNCE_MS } from './draftConstants'

export { DRAFT_MAX_ENTRIES, DRAFT_TTL_MS, DRAFT_SAVE_DEBOUNCE_MS }
export const DRAFTS_KEY = 'mc-chat-drafts'

/**
 * Merge a handed-off prompt into whatever the composer already holds.
 *
 * Every path that seeds the composer via `setPendingInput` goes through the same
 * consumer, and that consumer REPLACES the draft and persists the replacement —
 * so a plain set silently destroys unsent text the user was mid-way through
 * typing, unrecoverably. Both hand-off paths (a follow-up card's "add to this
 * session", and the error → agent hand-off) therefore append instead.
 *
 * One implementation on purpose: this was duplicated at the two call sites, which
 * is how the two behaviours drift.
 */
// A blank line separates the two, because a handed-off prompt is multi-line
// prose, not a word to concatenate. Built by concatenation rather than a
// template literal so the only literal here is this separator — punctuation, not
// user-visible copy.
const PARAGRAPH_BREAK = '\n\n'

/** The trailing newline run at the end of a draft, and nothing before it.
 *
 *  Deliberately NOT `/\s+$/`. Two trailing SPACES are a Markdown hard line break,
 *  so a blanket trailing-whitespace strip rewrites the last line of a draft the
 *  user is still holding — the composer keeps its text verbatim, and the merge
 *  below is not the place to edit it. Only the trailing NEWLINES have to go, and
 *  only because the paragraph break is about to supply them.
 *
 *  The match therefore STARTS at a newline. Letting it open with `[^\S\n]*`
 *  instead reaches back across the spaces that precede the first newline and
 *  eats exactly the hard break this exists to protect — `"line  \n"` came out as
 *  `"line"`. Whitespace that follows a newline is inside the run and does go;
 *  `\r?` keeps a CRLF draft from leaving its `\r` behind. */
const TRAILING_NEWLINES = /(?:\r?\n[^\S\n]*)+$/

export function mergeIntoDraft(draft: string | null | undefined, prompt: string): string {
  const existing = draft ?? ''
  if (!existing.trim()) return prompt
  // Nothing to append. Without this the draft grows a trailing paragraph break the
  // user did not type — harmless at the two hand-off call sites, which always carry
  // prose, but the composer merges whatever the server hands back and an edited
  // queue entry can be emptied to nothing.
  if (!prompt.trim()) return existing
  return existing.replace(TRAILING_NEWLINES, '') + PARAGRAPH_BREAK + prompt
}

/**
 * Put the payload of a send the server never accepted back into the composer.
 *
 * The same append-merge as `mergeIntoDraft` — a send is in flight for seconds and
 * the user can type in that window, so neither payload may overwrite the other —
 * with one rule the hand-off paths do not need: an exact duplicate is not
 * appended twice. A synchronously rejected create can land before React flushes
 * the composer clear, so the payload may already be sitting there.
 *
 * Deduped ONLY on exact equality. A whitespace-delimited occurrence is not proof
 * the payload was already restored — a draft like "please run tests first"
 * contains the distinct payload "run tests" — and treating it as restored drops
 * the message. Equality still covers the case this guard exists for, and errs
 * toward a visible duplicate rather than silent loss.
 *
 * One implementation for every recovery site — ChatPage's failed create and its
 * failed send, and ChatPane's own restore, which serves both a failed send and a
 * failed question-card fallback — for the same reason `mergeIntoDraft` is
 * shared: separate spellings of one rule are how the surfaces drift apart.
 */
export function mergeRecoveredDraft(keep: string | null | undefined, payload: string): string {
  const existing = keep ?? ''
  if (existing.trim() && existing.trim() === payload.trim()) return existing
  return mergeIntoDraft(existing, payload)
}

export type Drafts = Record<string, string>

const isNonEmptyString = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)

const store = createSlotDraftStore<string>({
  key: DRAFTS_KEY,
  storage: 'local',
  ttlMs: DRAFT_TTL_MS,
  maxEntries: DRAFT_MAX_ENTRIES,
  maxStoreBytes: DRAFT_MAX_STORE_BYTES,
  sanitize: isNonEmptyString,
})

export const loadDrafts = store.load
export const saveDrafts = store.save
export const setDraft = store.set
export const __resetForTests = store.__resetForTests
