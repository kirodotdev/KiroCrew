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

/** Byte budget for a SINGLE store's serialized blob. When exceeded, the
 *  byte-aware LRU evicts OLDEST slots until it fits (the newest slot is never
 *  evicted). Replaces the old arbitrary 512 KB per-slot hard cap so the most
 *  recent large draft survives whether collapsed or expanded.
 *
 *  Sizing: localStorage gives ~5 MB per origin, SHARED across all keys. The two
 *  byte-capped stores are `mc-chat-drafts` and `mc-chat-paste-drafts`; at 2 MB
 *  each that's 4 MB worst case, leaving ~1 MB for the uncapped localStorage
 *  siblings (`mc-comment-drafts`, `mc-paste-store-v1`). A per-store budget can't
 *  by itself guarantee the origin total stays under quota, but if a write does
 *  blow the shared quota `setItem` throws and the store no-ops that cycle
 *  (caught, DEV-warned) rather than corrupting. `mc-chat-file-drafts` is
 *  sessionStorage (separate quota), so it doesn't count against this. */
export const DRAFT_MAX_STORE_BYTES = 2 * 1024 * 1024
