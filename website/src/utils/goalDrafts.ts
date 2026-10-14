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
 * The server (`/api/autonudge/draft/slot/{slot}`) holds the one canonical copy
 * shared across devices. The browser keeps only the latest edit the server has
 * not confirmed yet, one record per slot: the edit (or a Clear's `null`) and the
 * canonical version it was made on, which its save sends as the compare-and-set
 * base. A confirmed save drops the record. Records use the shared slot-draft
 * store with the same 30-day TTL and 50-slot cap as chat drafts. The draft a
 * pre-sync browser kept under `mc-goal-drafts` is uploaded once into an empty
 * server slot and then deleted.
 */
import { TAB_ID } from '../api/tabId'
import { createSlotDraftStore } from './slotDraftStore'
import { DRAFT_MAX_ENTRIES, DRAFT_TTL_MS } from './draftConstants'

/** Where browsers before the shared store kept the draft itself. */
export const LEGACY_GOAL_DRAFTS_KEY = 'mc-goal-drafts'
/** The latest edit per slot the server has not confirmed yet. */
export const GOAL_DRAFTS_KEY = 'mc-goal-drafts-pending'
/** Cap stored slots to prevent unbounded growth (shared with text drafts). */
export const GOAL_DRAFT_MAX_ENTRIES = DRAFT_MAX_ENTRIES
/** Discard drafts not touched within this window (shared with text drafts). */
export const GOAL_DRAFT_TTL_MS = DRAFT_TTL_MS
export const GOAL_DRAFT_MIN_IDLE_SECS = 15
export const GOAL_DRAFT_MAX_IDLE_SECS = 86_400
export const GOAL_DRAFT_MAX_CYCLES = 2_147_483_647
/** Server and editor contract, counted as Unicode code points (Python `len`). */
export const GOAL_DRAFT_MAX_MESSAGE_CHARS = 8_000
export const GOAL_DRAFT_SYNC_TIMEOUT_MS = 5_000

/** The three fields of the goal popover, remembered together per slot. */
export interface GoalDraft {
  message: string
  idleSecs: number
  maxCycles: number
}

/** The canonical server record: a draft, or `null` for a cleared or empty slot. */
export interface GoalDraftSnapshot {
  draft: GoalDraft | null
  /** Server version; zero means the slot holds no record at all. */
  updatedAt: number
}

/** An edit the server has not confirmed, and the canonical version it was made on. */
export interface PendingGoalDraft {
  draft: GoalDraft | null
  base: number
  tab?: string
}

function clampDraftInteger(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, Math.trunc(value)))
}

export function clampGoalDraftMessage(value: string): string {
  const characters = Array.from(value)
  return characters.length <= GOAL_DRAFT_MAX_MESSAGE_CHARS
    ? value
    : characters.slice(0, GOAL_DRAFT_MAX_MESSAGE_CHARS).join('')
}

/** A value is a valid GoalDraft iff it carries a non-blank message string plus
 *  finite numeric idle/cycle fields. Accepted values are normalized to the
 *  canonical server bounds, so every stored edit can be synced remotely. */
function sanitizeGoalDraft(v: unknown): GoalDraft | null {
  if (!v || typeof v !== 'object') return null
  const d = v as Record<string, unknown>
  if (typeof d.message !== 'string') return null
  const message = clampGoalDraftMessage(d.message)
  if (!message.trim()) return null
  if (
    typeof d.idleSecs !== 'number'
    || !Number.isFinite(d.idleSecs)
    || typeof d.maxCycles !== 'number'
    || !Number.isFinite(d.maxCycles)
  ) return null
  return {
    message,
    idleSecs: clampDraftInteger(d.idleSecs, GOAL_DRAFT_MIN_IDLE_SECS, GOAL_DRAFT_MAX_IDLE_SECS),
    maxCycles: clampDraftInteger(d.maxCycles, 0, GOAL_DRAFT_MAX_CYCLES),
  }
}

function sanitizePending(v: unknown): PendingGoalDraft | null {
  if (!v || typeof v !== 'object') return null
  const row = v as Record<string, unknown>
  if (typeof row.base !== 'number' || !Number.isFinite(row.base) || row.base < 0) return null
  const tab = typeof row.tab === 'string' ? row.tab : undefined
  if (row.draft === null) return { draft: null, base: row.base, tab }
  const draft = sanitizeGoalDraft(row.draft)
  return draft ? { draft, base: row.base, tab } : null
}

const pendingStore = createSlotDraftStore<PendingGoalDraft>({
  key: GOAL_DRAFTS_KEY,
  storage: 'local',
  ttlMs: GOAL_DRAFT_TTL_MS,
  maxEntries: GOAL_DRAFT_MAX_ENTRIES,
  sanitize: sanitizePending,
})

// The pre-sync store, as main wrote it. Read once per slot and then deleted.
const legacyStore = createSlotDraftStore<GoalDraft>({
  key: LEGACY_GOAL_DRAFTS_KEY,
  storage: 'local',
  ttlMs: GOAL_DRAFT_TTL_MS,
  maxEntries: GOAL_DRAFT_MAX_ENTRIES,
  sanitize: sanitizeGoalDraft,
})

export function sameGoalDraft(left: GoalDraft | null, right: GoalDraft | null): boolean {
  if (left === null || right === null) return left === right
  return left.message === right.message
    && left.idleSecs === right.idleSecs
    && left.maxCycles === right.maxCycles
}

// The slot's pending record when browser storage refused the last write to it
// (`null`: a refused removal), so a refused write never loses or revives an edit.
const unstoredPendingGoalDrafts: Record<string, PendingGoalDraft | null> = Object.create(null)

function storePendingGoalDrafts(slot: string, drafts: Record<string, PendingGoalDraft>): void {
  if (pendingStore.save(drafts)) delete unstoredPendingGoalDrafts[slot]
  else unstoredPendingGoalDrafts[slot] = drafts[slot] ?? null
}

/** The slot's unconfirmed edit, or `null` when the server holds the latest one. */
export function loadPendingGoalDraft(slot: string): PendingGoalDraft | null {
  if (slot in unstoredPendingGoalDrafts) return unstoredPendingGoalDrafts[slot]
  return pendingStore.load()[slot] ?? null
}

/** An edit's version: this tab's own pending edit's, else the one this tab confirmed, else the stored edit's. */
function editBase(slot: string): number {
  const pending = loadPendingGoalDraft(slot)
  return (pending?.tab === TAB_ID ? pending.base : undefined) ?? confirmedGoalDrafts[slot]?.updatedAt ?? pending?.base ?? 0
}

/** Remember an edit (`null` for a Clear) as the slot's unconfirmed edit. The
 *  base defaults to `editBase`: an edit is made on the version the editor showed. */
export function savePendingGoalDraft(slot: string, draft: GoalDraft | null, base?: number): PendingGoalDraft {
  const drafts = pendingStore.load()
  const pending = {
    draft: draft === null ? null : sanitizeGoalDraft(draft),
    base: base ?? editBase(slot),
    tab: TAB_ID,
  }
  pendingStore.set(drafts, slot, pending)
  storePendingGoalDrafts(slot, drafts)
  return pending
}

/** Forget the slot's unconfirmed edit: the server now holds it, or the user dropped it. */
export function clearPendingGoalDraft(slot: string): void {
  const drafts = pendingStore.load()
  delete unstoredPendingGoalDrafts[slot]
  if (!(slot in drafts)) return
  delete drafts[slot]
  storePendingGoalDrafts(slot, drafts)
}

/** The draft a pre-sync browser kept for `slot`, or `null`. */
export function loadLegacyGoalDraft(slot: string): GoalDraft | null {
  return legacyStore.load()[slot] ?? null
}

/** Delete the slot's pre-sync draft once the server has answered for the slot. */
export function deleteLegacyGoalDraft(slot: string): void {
  const drafts = legacyStore.load()
  if (!(slot in drafts)) return
  delete drafts[slot]
  legacyStore.save(drafts)
}

// Module state, so a remounted editor keeps the slot's protocol: the canonical
// record this tab last confirmed or adopted, the queued writes, and a Clear
// still settling.
const confirmedGoalDrafts: Record<string, GoalDraftSnapshot> = Object.create(null)
const remoteWriteTails: Record<string, Promise<void>> = Object.create(null)
const pendingGoalDraftClears: Record<string, Promise<GoalDraftClearResult>> = Object.create(null)
// A Clear whose loop removal got no answer, past the sync deadline or lost on
// the way back: the removal to re-send, its tombstone base and the edit pending
// when it began. Until a re-sent removal is answered, the slot shows the failed
// Clear, never the goal it cleared. Held in memory only.
interface UnansweredGoalDraftClear { url: string, record?: string, base?: number, before: PendingGoalDraft | null }
const unansweredGoalDraftClears: Record<string, UnansweredGoalDraftClear> = Object.create(null)

/** The canonical record this tab last confirmed or adopted for `slot`. A loop
 *  create names its version, so the server refuses a Start over a goal another
 *  device saved since. */
export function confirmedGoalDraft(slot: string): GoalDraftSnapshot | undefined {
  return confirmedGoalDrafts[slot]
}

/** Adopt the canonical record as what this tab shows for `slot`. */
export function adoptGoalDraft(slot: string, canonical: GoalDraftSnapshot): void {
  confirmedGoalDrafts[slot] = canonical
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
  const draft = sanitizeGoalDraft({ message: wire.message, idleSecs: wire.idle_secs, maxCycles: wire.max_cycles })
  if (!draft) throw new Error('Invalid goal draft response')
  return { draft, updatedAt: row.updated_at }
}

export class GoalDraftConflict extends Error {
  readonly current: GoalDraftSnapshot | null

  constructor(current: GoalDraftSnapshot | null) {
    super()
    this.name = 'GoalDraftConflict'
    this.current = current
  }
}

/** The goal-draft conflict a refused draft write or loop create answered, else `null`. */
export function goalDraftConflictFrom(status: number, body: Record<string, unknown>): GoalDraftConflict | null {
  if (status !== 409 || body.code !== 'goal_draft_conflict') return null
  let current: GoalDraftSnapshot | null = null
  try { current = parseRemoteSnapshot(body.current) } catch { /* malformed conflict stays a conflict */ }
  return new GoalDraftConflict(current)
}

function untilAborted<T>(signal: AbortSignal, work: Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason)
    if (signal.aborted) {
      onAbort()
      return
    }
    signal.addEventListener('abort', onAbort, { once: true })
    work.then(resolve, reject).finally(() => signal.removeEventListener('abort', onAbort))
  })
}

/** One goal-draft request under the sync deadline. The deadline covers the
 *  response body as well: headers can arrive and the body still stall. */
async function fetchRemoteGoalDraft(url: string, init?: RequestInit) {
  const controller = new AbortController()
  const timeout = globalThis.setTimeout(() => controller.abort(), GOAL_DRAFT_SYNC_TIMEOUT_MS)
  try {
    const response = await fetch(url, { ...init, signal: controller.signal })
    const body = await untilAborted(controller.signal, response.json().catch(() => ({})))
    return { response, body }
  } finally {
    globalThis.clearTimeout(timeout)
  }
}

const draftUrl = (slot: string) => `/api/autonudge/draft/slot/${encodeURIComponent(slot)}`

/** Read the canonical cross-device draft. */
export async function loadRemoteGoalDraft(slot: string): Promise<GoalDraftSnapshot> {
  const { response, body } = await fetchRemoteGoalDraft(draftUrl(slot))
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`)
  return parseRemoteSnapshot(body)
}

/** Compare-and-set one draft or Clear on `base`; resolves with the canonical answer. */
export async function saveRemoteGoalDraft(
  slot: string,
  draft: GoalDraft | null,
  base: number,
  keepalive = false,
): Promise<GoalDraftSnapshot> {
  const clean = draft === null ? null : sanitizeGoalDraft(draft)
  if (draft !== null && clean === null) throw new Error('Invalid goal draft')
  const { response, body } = await fetchRemoteGoalDraft(draftUrl(slot), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    keepalive,
    body: JSON.stringify({
      base_updated_at: base,
      draft: clean ? { message: clean.message, idle_secs: clean.idleSecs, max_cycles: clean.maxCycles } : null,
    }),
  })
  if (!response.ok) throw goalDraftConflictFrom(response.status, body) ?? new Error(body.error || `HTTP ${response.status}`)
  return parseRemoteSnapshot(body)
}

/** How one save ended. `canonical` is the record it confirmed, or for a
 *  conflict the record it was refused over, when the server named one. */
export type GoalDraftSyncOutcome =
  | { result: 'persisted', canonical: GoalDraftSnapshot }
  | { result: 'conflict', canonical: GoalDraftSnapshot | null }
  | { result: 'remote-failed' }

/**
 * Save `draft` for `slot` behind the slot's earlier writes. The base is read
 * when the write goes out, after the writes queued before it answered: an
 * explicit `base`, else `editBase`. A confirmed answer becomes this tab's
 * canonical record and settles the pending record: it is dropped when it holds
 * the saved text, and this tab's edit typed after it is now made on the answer.
 */
export function syncGoalDraft(
  slot: string,
  draft: GoalDraft | null,
  options: { base?: number, keepalive?: boolean } = {},
): Promise<GoalDraftSyncOutcome> {
  const previous = remoteWriteTails[slot] ?? Promise.resolve()
  const request = previous.then(async (): Promise<GoalDraftSyncOutcome> => {
    const base = options.base ?? editBase(slot)
    try {
      const canonical = await saveRemoteGoalDraft(slot, draft, base, options.keepalive)
      confirmedGoalDrafts[slot] = canonical
      const pending = loadPendingGoalDraft(slot)
      if (pending && sameGoalDraft(pending.draft, draft)) clearPendingGoalDraft(slot)
      else if (pending?.tab === TAB_ID) savePendingGoalDraft(slot, pending.draft, canonical.updatedAt)
      return { result: 'persisted', canonical }
    } catch (error) {
      return error instanceof GoalDraftConflict
        ? { result: 'conflict', canonical: error.current }
        : { result: 'remote-failed' }
    }
  })
  const tail = request.then(() => undefined)
  remoteWriteTails[slot] = tail
  void tail.finally(() => {
    if (remoteWriteTails[slot] === tail) delete remoteWriteTails[slot]
  })
  return request
}

/** The slot's queued remote writes, for an editor that must read the shared
 *  copy after them; `undefined` when none is in flight. Never rejects. */
export function pendingRemoteGoalDraftWrite(slot: string): Promise<void> | undefined {
  return remoteWriteTails[slot]
}

/** How a Clear ended: its tombstone's save, or `removal-failed` when the loop
 *  removal failed and no tombstone was written. */
export type GoalDraftClearResult = GoalDraftSyncOutcome['result'] | 'removal-failed'

/** One Clear, registered for its slot before the loop removal goes out. */
export interface GoalDraftClear {
  /** The loop is gone: save the tombstone on `base` and settle the Clear with its outcome. */
  settle: (base?: number) => Promise<GoalDraftClearResult>
  /** End the Clear as `removal-failed`; ignored once `settle` has run. */
  release: () => void
  /** Send the loop removal under the sync deadline. One that gets no answer,
   *  past the deadline or lost on the way back, ends the Clear as
   *  `removal-failed` and is kept for `resendUnansweredGoalDraftClear`. */
  removeLoop: (url: string, base?: number, record?: string) => Promise<Response>
}

/** A loop removal under the sync deadline. The deadline ends the wait, not the
 *  request: a removal that gets no answer may still land. */
async function sendLoopRemoval(url: string): Promise<Response> {
  const controller = new AbortController()
  const timeout = globalThis.setTimeout(() => controller.abort(), GOAL_DRAFT_SYNC_TIMEOUT_MS)
  try {
    return await untilAborted(controller.signal, fetch(url, { method: 'DELETE' }))
  } finally {
    globalThis.clearTimeout(timeout)
  }
}

/**
 * Register a Clear for `slot` before its loop removal goes out. Owned here, not
 * by the editor: the dashboard keys the editor on the loop, and the server
 * sends `removed` before it answers the DELETE, so an editor can remount at any
 * point of the Clear. Each one mounted meanwhile waits on this one completion,
 * and the registration ends with it.
 */
export function beginGoalDraftClear(slot: string): GoalDraftClear {
  let finish!: (outcome: GoalDraftClearResult | Promise<GoalDraftClearResult>) => void
  const completion = new Promise<GoalDraftClearResult>(resolve => { finish = resolve })
  const before = loadPendingGoalDraft(slot)
  pendingGoalDraftClears[slot] = completion
  delete unansweredGoalDraftClears[slot]
  void completion.finally(() => {
    if (pendingGoalDraftClears[slot] === completion) delete pendingGoalDraftClears[slot]
  })
  return {
    settle: base => {
      finish(syncGoalDraft(slot, null, { base }).then(outcome => outcome.result))
      return completion
    },
    release: () => finish('removal-failed'),
    removeLoop: async (url, base, record) => {
      try {
        return await sendLoopRemoval(url)
      } catch (error) {
        // No answer, past the deadline or lost on the way back: the removal may
        // have landed, so only a re-sent one that is answered ends this Clear.
        unansweredGoalDraftClears[slot] = { url, record, base, before }
        throw error
      }
    },
  }
}

/** Whether `slot` holds a Clear whose loop removal got no answer yet. */
export function hasUnansweredGoalDraftClear(slot: string): boolean {
  return unansweredGoalDraftClears[slot] !== undefined
}

/** The base the unanswered Clear of `slot` captured, when it removes `url` on the
 *  same loop `record`: a re-send keeps it. A record changed since has no Clear. */
export function unansweredGoalDraftClearOn(slot: string, url: string, record: string): { base?: number } | undefined {
  const unanswered = unansweredGoalDraftClears[slot]
  return unanswered?.url === url && unanswered.record === record ? { base: unanswered.base } : undefined
}

/**
 * Retry after a Clear whose loop removal got no answer: re-send the removal
 * under the deadline. Only an answer that confirms it ends the Clear's hold on
 * the slot: the loop is gone, so the Clear's tombstone becomes the pending edit
 * on the base it captured, unless an edit was saved here since. Resolves whether
 * that answer arrived; a removal still unanswered, or refused, stays for the
 * next Retry.
 */
export async function resendUnansweredGoalDraftClear(slot: string): Promise<boolean> {
  const unanswered = unansweredGoalDraftClears[slot]
  if (!unanswered) return false
  const confirmed = await sendLoopRemoval(unanswered.url).then(response => response.ok, () => false)
  if (!confirmed || unansweredGoalDraftClears[slot] !== unanswered) return false
  delete unansweredGoalDraftClears[slot]
  const pending = loadPendingGoalDraft(slot)
  const unchanged = pending === null || unanswered.before === null
    ? pending === unanswered.before
    : pending.base === unanswered.before.base && sameGoalDraft(pending.draft, unanswered.before.draft)
  if (unchanged) savePendingGoalDraft(slot, null, unanswered.base ?? 0)
  return true
}

/** The slot's Clear still settling, for an editor mounted while it runs. */
export function pendingGoalDraftClear(slot: string): Promise<GoalDraftClearResult> | undefined {
  return pendingGoalDraftClears[slot]
}

/** @internal test-only: reset module state between tests. `undefined` in the
 *  production bundle. */
export const __resetForTests: () => void = import.meta.env.PROD
  ? (undefined as unknown as () => void)
  : () => {
      pendingStore.__resetForTests()
      legacyStore.__resetForTests()
      for (const registry of [confirmedGoalDrafts, remoteWriteTails, pendingGoalDraftClears, unansweredGoalDraftClears, unstoredPendingGoalDrafts]) {
        for (const slot of Object.keys(registry)) delete registry[slot]
      }
    }
