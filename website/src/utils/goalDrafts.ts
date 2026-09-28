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

/** Remember (or clear) the goal draft for `slot`. Pass `null` — or a draft with
 *  a blank message — to drop the slot; the caller uses this to avoid pinning the
 *  pristine default template. */
export function saveGoalDraft(slot: string, draft: GoalDraft | null): void {
  const drafts = store.load()
  // A blank message sanitizes to null, which makes `set` delete the slot — so a
  // null/empty draft is the uniform "forget this slot" path.
  store.set(drafts, slot, draft ?? { message: '', idleSecs: 0, maxCycles: 0 })
  store.save(drafts)
}

/** @internal test-only: reset module state between tests. `undefined` in the
 *  production bundle (the factory gates it on `!import.meta.env.PROD`). */
export const __resetForTests: () => void = store.__resetForTests
