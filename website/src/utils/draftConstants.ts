/**
 * Shared slot-draft constants. Neutral home so `chatDrafts`, `chatPasteDrafts`,
 * and friends stay in lockstep without one importing from another.
 */

/** Cap stored drafts to prevent unbounded growth from deleted slots. */
export const DRAFT_MAX_ENTRIES = 50

/** Discard drafts not edited within this window. Guards against stale sensitive
 *  content (API keys, credentials, PII) persisting indefinitely in storage. */
export const DRAFT_TTL_MS = 30 * 24 * 60 * 60 * 1000 // 30 days

/** Debounce for draft persistence on input change. */
export const DRAFT_SAVE_DEBOUNCE_MS = 300

/** Byte budget for a SINGLE existing draft store's serialized blob. When exceeded, the
 *  byte-aware LRU evicts OLDEST slots until it fits (the newest slot is never
 *  evicted), so the most recent large draft survives whether collapsed or
 *  expanded.
 *
 *  Held at 2 MiB deliberately. Deriving it by dividing the shared budget by the store count
 *  LOWERED it, and a store already sitting between the old and new caps has its oldest
 *  UNSENT drafts LRU-evicted on the very next save — a silent loss of work the user typed,
 *  caused purely by the constant moving. The recovery store is budgeted separately below
 *  instead, so adding it cannot shrink what the existing two stores already hold. */
export const DRAFT_MAX_STORE_BYTES = 2 * 1024 * 1024

/** Byte budget for `mc-chat-pane-recovery`, sized on top of the two 2 MiB stores rather than
 *  carved out of them: 2 + 2 + 0.5 = 4.5 MiB against the ~5 MB an origin gives, so the three
 *  together do not overcommit. It bounds MARKER records only — a record still carrying a
 *  prompt is never evicted for budget, because this store is that prompt's only copy — so
 *  this is a ceiling on bookkeeping, not on recoverable work.
 *
 *  `mc-chat-file-drafts` is sessionStorage (separate quota) and does not count here. */
export const RECOVERY_MAX_STORE_BYTES = 512 * 1024
