/**
 * Per-slot "Set a goal" (auto-nudge) draft persistence. Remembers the goal
 * description + idle/cycle settings the user last entered in the goal popover,
 * keyed by slot, so they survive the popover closing and re-opening.
 *
 * WHY THIS EXISTS: the goal popover (`AutoNudgePopover`) seeds its fields from
 * the active auto-nudge loop, falling back to a hard-coded DEFAULT message when
 * there is no loop. The popover unmounts on close, so its `useState` seeds
 * re-run on every open. The moment the loop is stopped (or hits its cycle
 * limit) the loop becomes null — so re-opening the popover threw away whatever
 * the user had typed and re-showed the default template, forcing them to retype
 * their goal. Persisting the last-entered draft per slot fixes that: after a
 * stop, re-opening restores exactly what the user last had.
 *
 * Thin instance of the shared `createSlotDraftStore` factory, same
 * as `chatDrafts` / `chatPasteDrafts` / `chatFileDrafts` — the TTL / LRU /
 * timestamp-sidecar / quota-safe persist machinery lives in one place there, so
 * this module is just the GoalDraft shape + validator. localStorage with the
 * SAME 30-day TTL and 50-entry cap as `chatDrafts` (unsent user input that may
 * carry paths / instructions, worth the same staleness-eviction policy). A
 * blank / whitespace-only message drops the slot, so an unedited popup never
 * pins a stale copy of the default template. Safe against corrupt / missing /
 * quota-exhausted storage: worst case the slot's draft is dropped and the
 * popover shows the default — i.e. the default behavior, never worse.
 */
import { createSlotDraftStore } from './slotDraftStore'
import { DRAFT_MAX_ENTRIES, DRAFT_TTL_MS } from './draftConstants'

export const GOAL_DRAFTS_KEY = 'mc-goal-drafts'
/** Cap stored slots to prevent unbounded growth (shared with text drafts). */
export const GOAL_DRAFT_MAX_ENTRIES = DRAFT_MAX_ENTRIES
/** Discard drafts not touched within this window (shared with text drafts). */
export const GOAL_DRAFT_TTL_MS = DRAFT_TTL_MS

/** The three fields of the goal popover, remembered together per slot. */
export interface GoalDraft {
  message: string
  idleSecs: number
  maxCycles: number
}

/** A value is a valid GoalDraft iff it carries a non-blank message string plus
 *  numeric idle/cycle fields. Anything else — including a blank / whitespace-only
 *  message — is dropped (the factory deletes the slot when sanitize returns
 *  null), so clearing the goal or storing the pristine-empty case removes it. */
function sanitizeGoalDraft(v: unknown): GoalDraft | null {
  if (!v || typeof v !== 'object') return null
  const d = v as Record<string, unknown>
  if (typeof d.message !== 'string' || !d.message.trim()) return null
  if (typeof d.idleSecs !== 'number' || typeof d.maxCycles !== 'number') return null
  return { message: d.message, idleSecs: d.idleSecs, maxCycles: d.maxCycles }
}

const store = createSlotDraftStore<GoalDraft>({
  key: GOAL_DRAFTS_KEY,
  storage: 'local',
  ttlMs: GOAL_DRAFT_TTL_MS,
  maxEntries: GOAL_DRAFT_MAX_ENTRIES,
  sanitize: sanitizeGoalDraft,
})

/** Read the remembered goal draft for `slot`, or `null` if none is stored
 *  (never set, blank, expired, or corrupt). */
export function loadGoalDraft(slot: string): GoalDraft | null {
  return store.load()[slot] ?? null
}

export interface GoalDraftSnapshot {
  draft: GoalDraft | null
  /** Browser edit time in epoch milliseconds; zero means no local record. */
  updatedAt: number
}

/** Read the local fallback and the TTL sidecar timestamp that orders first sync. */
export function loadGoalDraftSnapshot(slot: string): GoalDraftSnapshot {
  const draft = loadGoalDraft(slot)
  return { draft, updatedAt: draft ? (store.updatedAt(slot) ?? 0) : 0 }
}

/** Remember (or clear) the goal draft for `slot`. Pass `null` — or a draft with
 *  a blank message — to drop the slot; the caller uses this to avoid pinning the
 *  pristine default template. A supplied timestamp preserves the server's
 *  conflict ordering when a newer remote value refreshes this local cache. */
export function saveGoalDraft(
  slot: string,
  draft: GoalDraft | null,
  updatedAt: number = Date.now(),
): GoalDraftSnapshot {
  const drafts = store.load()
  // A blank message sanitizes to null, which makes `set` delete the slot — so a
  // null/empty draft is the uniform "forget this slot" path.
  store.set(drafts, slot, draft ?? { message: '', idleSecs: 0, maxCycles: 0 }, updatedAt)
  store.save(drafts)
  const saved = drafts[slot] ?? null
  return { draft: saved, updatedAt: saved ? (store.updatedAt(slot) ?? updatedAt) : updatedAt }
}

function parseRemoteSnapshot(value: unknown): GoalDraftSnapshot {
  if (!value || typeof value !== 'object') throw new Error('Invalid goal draft response')
  const row = value as Record<string, unknown>
  if (typeof row.updated_at !== 'number' || !Number.isFinite(row.updated_at)) {
    throw new Error('Invalid goal draft timestamp')
  }
  if (row.draft === null) return { draft: null, updatedAt: row.updated_at }
  if (!row.draft || typeof row.draft !== 'object') throw new Error('Invalid goal draft response')
  const wire = row.draft as Record<string, unknown>
  const draft = sanitizeGoalDraft({
    message: wire.message,
    idleSecs: wire.idle_secs,
    maxCycles: wire.max_cycles,
  })
  if (!draft) throw new Error('Invalid goal draft response')
  return { draft, updatedAt: row.updated_at }
}

/** Read the canonical cross-device draft. The local store remains the offline fallback. */
export async function loadRemoteGoalDraft(slot: string): Promise<GoalDraftSnapshot> {
  const response = await fetch(`/api/autonudge/draft/slot/${encodeURIComponent(slot)}`)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`)
  return parseRemoteSnapshot(body)
}

/** Last-write-wins write of a browser edit or clear tombstone. */
export async function saveRemoteGoalDraft(
  slot: string,
  snapshot: GoalDraftSnapshot,
  options: { keepalive?: boolean } = {},
): Promise<GoalDraftSnapshot> {
  const response = await fetch(`/api/autonudge/draft/slot/${encodeURIComponent(slot)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    keepalive: options.keepalive,
    body: JSON.stringify({
      updated_at: snapshot.updatedAt,
      draft: snapshot.draft ? {
        message: snapshot.draft.message,
        idle_secs: snapshot.draft.idleSecs,
        max_cycles: snapshot.draft.maxCycles,
      } : null,
    }),
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`)
  return parseRemoteSnapshot(body)
}

/** @internal test-only: reset module state between tests. `undefined` in the
 *  production bundle (the factory gates it on `!import.meta.env.PROD`). */
export const __resetForTests: () => void = store.__resetForTests
