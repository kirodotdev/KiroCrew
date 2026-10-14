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
 * The server (`/api/autonudge/draft/slot/{slot}`) is the canonical copy shared
 * across devices; this module is the browser's offline fallback plus the
 * per-slot request protocol the popover runs against that server. Locally each
 * slot owns ONE record key (`mc-goal-drafts-v2:<slot>`) whose single JSON value
 * holds the draft-or-tombstone together with its conflict timestamp, so one
 * `setItem` commits both and two slots never share a blob. A write never
 * replaces a strictly newer record for the same slot unless it is the server's
 * canonical answer to exactly that record, so a delayed tab cannot downgrade a
 * newer edit. Records keep the same 30-day TTL and 50-slot cap as ordinary text
 * drafts. The pre-upgrade raw body + timestamp sidecar remain a read-only migration
 * input. The canonical server record decides what is shown after migration.
 */
import { safeGetItem, safeSetItem } from './safeStorage'
import { DRAFT_MAX_ENTRIES, DRAFT_TTL_MS } from './draftConstants'

/** Pre-version clients keep writing this raw body + timestamp sidecar. */
export const LEGACY_GOAL_DRAFTS_KEY = 'mc-goal-drafts'
const LEGACY_GOAL_DRAFT_TIMESTAMPS_KEY = `${LEGACY_GOAL_DRAFTS_KEY}-ts`
/** Version-isolated per-slot records. Old tabs never write this namespace. */
export const GOAL_DRAFTS_KEY = 'mc-goal-drafts-v2'
export const GOAL_DRAFT_RECORD_PREFIX = `${GOAL_DRAFTS_KEY}:`
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

interface GoalDraftTombstone {
  deleted: true
}

type StoredGoalDraft = GoalDraft | GoalDraftTombstone

/** The one JSON value stored under a slot's record key. */
type GoalDraftRecord = {
  updatedAt: number
  value: StoredGoalDraft
  /** Canonical version this unconfirmed live edit was based on. */
  baseUpdatedAt?: number
}

type RetainedGoalDraft = {
  snapshot: GoalDraftSnapshot
  baseUpdatedAt?: number
  /** A lower canonical answer still supersedes this submitted local timestamp. */
  canonicalSupersedesUpdatedAt?: number
}

type LegacyGoalDraftInput = {
  updatedAt: number | null
  value: StoredGoalDraft
}

/**
 * Module state keyed by externally supplied slot names starts with no
 * prototype: a slot named `toString` or `constructor` must read as `undefined`,
 * never as an inherited function masquerading as stored state.
 */
function createSlotKeyedRecord<T>(): Record<string, T> {
  return Object.create(null) as Record<string, T>
}

function clampDraftInteger(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, Math.trunc(value)))
}

export function goalDraftMessageLength(value: string): number {
  return Array.from(value).length
}

export function clampGoalDraftMessage(value: string): string {
  const characters = Array.from(value)
  return characters.length <= GOAL_DRAFT_MAX_MESSAGE_CHARS
    ? value
    : characters.slice(0, GOAL_DRAFT_MAX_MESSAGE_CHARS).join('')
}

/** A value is a valid GoalDraft iff it carries a non-blank message string plus
 *  finite numeric idle/cycle fields. Accepted values are normalized to the
 *  canonical server bounds, so every local fallback can be synced remotely. */
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
    idleSecs: clampDraftInteger(
      d.idleSecs,
      GOAL_DRAFT_MIN_IDLE_SECS,
      GOAL_DRAFT_MAX_IDLE_SECS,
    ),
    maxCycles: clampDraftInteger(d.maxCycles, 0, GOAL_DRAFT_MAX_CYCLES),
  }
}

function sanitizeStoredGoalDraft(v: unknown): StoredGoalDraft | null {
  const draft = sanitizeGoalDraft(v)
  if (draft) return draft
  if (v && typeof v === 'object' && (v as Record<string, unknown>).deleted === true) {
    return { deleted: true }
  }
  return null
}

function storedDraftValue(value: StoredGoalDraft | undefined): GoalDraft | null {
  return value && !('deleted' in value) ? value : null
}

function isValidStamp(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function recordKey(slot: string): string {
  return `${GOAL_DRAFT_RECORD_PREFIX}${encodeURIComponent(slot)}`
}

function parseRecord(raw: string | null): GoalDraftRecord | null {
  if (raw === null) return null
  try {
    const value: unknown = JSON.parse(raw)
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null
    const row = value as Record<string, unknown>
    if (!isValidStamp(row.updatedAt)) return null
    if (row.baseUpdatedAt !== undefined && !isValidStamp(row.baseUpdatedAt)) return null
    const stored = sanitizeStoredGoalDraft(row.value)
    return stored ? {
      updatedAt: row.updatedAt,
      value: stored,
      ...(row.baseUpdatedAt === undefined ? {} : { baseUpdatedAt: row.baseUpdatedAt }),
    } : null
  } catch {
    return null
  }
}

function readRecord(slot: string): GoalDraftRecord | null {
  return parseRecord(safeGetItem(recordKey(slot)))
}

function removeKey(key: string): boolean {
  try {
    localStorage.removeItem(key)
    return localStorage.getItem(key) === null
  } catch {
    return false
  }
}

/** Remove only this slot from the legacy body before its v2 mask disappears. */
function retireLegacySlot(slot: string): boolean {
  const raw = safeGetItem(LEGACY_GOAL_DRAFTS_KEY)
  if (raw === null) return true
  let parsed: unknown
  try { parsed = JSON.parse(raw) } catch { return true }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return true
  const legacy = parsed as Record<string, unknown>
  if (!Object.prototype.hasOwnProperty.call(legacy, slot)) return true
  const remaining = { ...legacy }
  delete remaining[slot]
  // The body is the resurrection source, so it is the commit point. A sidecar
  // row left after a failed best-effort cleanup cannot recreate a draft.
  if (!safeSetItem(LEGACY_GOAL_DRAFTS_KEY, JSON.stringify(remaining))) return false

  const timestampsRaw = safeGetItem(LEGACY_GOAL_DRAFT_TIMESTAMPS_KEY)
  if (timestampsRaw !== null) {
    try {
      const parsedTimestamps: unknown = JSON.parse(timestampsRaw)
      if (parsedTimestamps && typeof parsedTimestamps === 'object' && !Array.isArray(parsedTimestamps)) {
        const timestamps = { ...(parsedTimestamps as Record<string, unknown>) }
        delete timestamps[slot]
        safeSetItem(LEGACY_GOAL_DRAFT_TIMESTAMPS_KEY, JSON.stringify(timestamps))
      }
    } catch { /* a malformed sidecar cannot restore body content */ }
  }
  return true
}

function readRecordKeys(): string[] {
  const keys: string[] = []
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key?.startsWith(GOAL_DRAFT_RECORD_PREFIX)) keys.push(key)
    }
  } catch { /* denied storage is an ordinary local-fallback miss */ }
  return keys
}

function slotOfRecordKey(key: string): string | null {
  try {
    return decodeURIComponent(key.slice(GOAL_DRAFT_RECORD_PREFIX.length))
  } catch {
    return null
  }
}

/**
 * Drop expired records and the oldest slots past the cap. Each removal names
 * an exact per-slot key, and a slot leaving v2 first retires its legacy input
 * so the eviction cannot resurrect the goal it once covered. `protectSlot` is
 * the write that triggered compaction and is never its own casualty.
 */
function compactRecords(protectSlot?: string): void {
  const cutoff = Date.now() - GOAL_DRAFT_TTL_MS
  const live: Array<{ key: string; slot: string; updatedAt: number }> = []
  for (const key of readRecordKeys()) {
    const slot = slotOfRecordKey(key)
    const record = parseRecord(safeGetItem(key))
    if (slot === null) { removeKey(key); continue }
    if (!record) { evictRecord(key, slot); continue }
    if (record.updatedAt < cutoff) { evictRecord(key, slot); continue }
    live.push({ key, slot, updatedAt: record.updatedAt })
  }
  if (live.length <= GOAL_DRAFT_MAX_ENTRIES) return
  live.sort((left, right) => left.updatedAt - right.updatedAt)
  let excess = live.length - GOAL_DRAFT_MAX_ENTRIES
  for (const entry of live) {
    if (excess === 0) break
    if (entry.slot === protectSlot) continue
    if (evictRecord(entry.key, entry.slot)) excess -= 1
  }
}

function evictRecord(key: string, slot: string): boolean {
  if (!retireLegacySlot(slot)) return false
  return removeKey(key)
}

function readLegacyInput(slot: string): LegacyGoalDraftInput | null {
  // Every released writer of the legacy key stores a bare `Record<slot, draft>`
  // body with its edit times in the `-ts` sidecar (or no sidecar at all on
  // pre-TTL installs). No other shape has ever been written, so no other shape
  // is read.
  const raw = safeGetItem(LEGACY_GOAL_DRAFTS_KEY)
  if (raw === null) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    const stored = sanitizeStoredGoalDraft((parsed as Record<string, unknown>)[slot])
    if (!stored) return null
    let timestampValue: unknown
    const sidecarRaw = safeGetItem(LEGACY_GOAL_DRAFT_TIMESTAMPS_KEY)
    if (sidecarRaw !== null) {
      try {
        const sidecar: unknown = JSON.parse(sidecarRaw)
        if (sidecar && typeof sidecar === 'object' && !Array.isArray(sidecar)) {
          timestampValue = (sidecar as Record<string, unknown>)[slot]
        }
      } catch { /* timestamp-less legacy remains unordered */ }
    }
    return { updatedAt: isValidStamp(timestampValue) ? timestampValue : null, value: stored }
  } catch {
    return null
  }
}

function legacySnapshot(slot: string): GoalDraftSnapshot {
  const input = readLegacyInput(slot)
  if (!input) return { draft: null, updatedAt: 0 }
  if (input.updatedAt !== null && input.updatedAt < Date.now() - GOAL_DRAFT_TTL_MS) {
    return { draft: null, updatedAt: 0 }
  }
  // A timestamp-less pre-sidecar value may fill an empty server slot, but it
  // has no evidence of recency and therefore never outranks canonical state.
  return { draft: storedDraftValue(input.value), updatedAt: input.updatedAt ?? 0 }
}

/** Read the remembered goal draft for `slot`, or `null` if none is stored
 *  (never set, cleared, expired, or corrupt). */
export function loadGoalDraft(slot: string): GoalDraft | null {
  return loadGoalDraftSnapshot(slot).draft
}

export interface GoalDraftSnapshot {
  draft: GoalDraft | null
  /** Browser edit time or canonical server time; zero means no local record. */
  updatedAt: number
  /** Present only when this browser could not persist the intended local value. */
  persisted?: false
}

/** Read the local fallback, including a clear tombstone's ordering timestamp. */
export function loadGoalDraftSnapshot(slot: string): GoalDraftSnapshot {
  const record = readRecord(slot)
  if (!record) return legacySnapshot(slot)
  if (record.updatedAt < Date.now() - GOAL_DRAFT_TTL_MS) {
    if (!evictRecord(recordKey(slot), slot)) {
      return { draft: storedDraftValue(record.value), updatedAt: record.updatedAt }
    }
    return legacySnapshot(slot)
  }
  return { draft: storedDraftValue(record.value), updatedAt: record.updatedAt }
}

function writeGoalDraftRecord(
  slot: string,
  draft: GoalDraft | null,
  updatedAt: number,
  supersedes?: GoalDraftSnapshot,
  baseUpdatedAt?: number,
): GoalDraftSnapshot {
  const clean = draft === null ? null : sanitizeGoalDraft(draft)
  const value: StoredGoalDraft = clean ?? { deleted: true }
  const snapshot: GoalDraftSnapshot = { draft: storedDraftValue(value), updatedAt }
  const stored = loadGoalDraftSnapshot(slot)
  // Newer wins. A canonical answer is judged against the submission it
  // settles, not its own stamp: the server may normalize a migrated stamp
  // downward, and a stale record the submission already outranked must not
  // outrank the answer either. Only a record newer than that submission stays.
  const newerRecordExists = supersedes === undefined
    ? stored.updatedAt > updatedAt
    : stored.updatedAt > supersedes.updatedAt
  if (newerRecordExists) return stored
  const record: GoalDraftRecord = {
    updatedAt,
    value,
    ...(baseUpdatedAt === undefined ? {} : { baseUpdatedAt }),
  }
  if (!safeSetItem(recordKey(slot), JSON.stringify(record))) {
    return { ...snapshot, persisted: false }
  }
  compactRecords(slot)
  return snapshot
}

/** Remember a draft or a timestamped clear tombstone for `slot`. A supplied
 *  `updatedAt` is used verbatim (migration reissue, canonical write-back);
 *  otherwise the stamp advances past the slot's cached stamp. */
export function saveGoalDraft(
  slot: string,
  draft: GoalDraft | null,
  updatedAt?: number,
): GoalDraftSnapshot {
  const previous = loadGoalDraftSnapshot(slot)
  const effectiveUpdatedAt = updatedAt ?? Math.max(Date.now(), previous.updatedAt + 1)
  return writeGoalDraftRecord(slot, draft, effectiveUpdatedAt)
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

export class GoalDraftConflict extends Error {
  readonly current: GoalDraftSnapshot | null

  constructor(current: GoalDraftSnapshot | null) {
    super()
    this.name = 'GoalDraftConflict'
    this.current = current
  }
}

async function fetchRemoteGoalDraft(url: string, init?: RequestInit): Promise<Response> {
  const controller = new AbortController()
  const timeout = globalThis.setTimeout(
    () => controller.abort(),
    GOAL_DRAFT_SYNC_TIMEOUT_MS,
  )
  try {
    return await fetch(url, { ...init, signal: controller.signal })
  } finally {
    globalThis.clearTimeout(timeout)
  }
}

/** Read the canonical cross-device draft. The local store remains the offline fallback. */
export async function loadRemoteGoalDraft(slot: string): Promise<GoalDraftSnapshot> {
  const response = await fetchRemoteGoalDraft(
    `/api/autonudge/draft/slot/${encodeURIComponent(slot)}`,
  )
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`)
  return parseRemoteSnapshot(body)
}

/** Write a browser edit or clear and return the server's canonical snapshot. */
export async function saveRemoteGoalDraft(
  slot: string,
  snapshot: GoalDraftSnapshot,
  options: { keepalive?: boolean; migration?: boolean; baseUpdatedAt?: number } = {},
): Promise<GoalDraftSnapshot> {
  const draft = snapshot.draft === null ? null : sanitizeGoalDraft(snapshot.draft)
  if (snapshot.draft !== null && draft === null) throw new Error('Invalid goal draft')
  const response = await fetchRemoteGoalDraft(`/api/autonudge/draft/slot/${encodeURIComponent(slot)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    keepalive: options.keepalive,
    body: JSON.stringify({
      updated_at: snapshot.updatedAt,
      migration: options.migration === true,
      ...(options.migration === true ? {} : {
        base_updated_at: options.baseUpdatedAt ?? baseForSnapshot(slot, snapshot) ?? 0,
      }),
      draft: draft ? {
        message: draft.message,
        idle_secs: draft.idleSecs,
        max_cycles: draft.maxCycles,
      } : null,
    }),
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 409 && body.code === 'goal_draft_conflict') {
      let current: GoalDraftSnapshot | null = null
      try { current = parseRemoteSnapshot(body.current) } catch { /* malformed conflict stays a conflict */ }
      throw new GoalDraftConflict(current)
    }
    throw new Error(body.error || `HTTP ${response.status}`)
  }
  const canonical = parseRemoteSnapshot(body)
  remoteCanonicalUpdatedAt[slot] = canonical.updatedAt
  return canonical
}

export type GoalDraftPersistResult = 'persisted' | 'retained' | 'superseded' | 'conflict' | 'remote-failed'

type GoalDraftRemoteWriter = (
  slot: string,
  snapshot: GoalDraftSnapshot,
  options: { keepalive?: boolean; migration?: boolean; baseUpdatedAt?: number },
) => Promise<GoalDraftSnapshot>

export interface GoalDraftMigrationResult {
  canonical?: GoalDraftSnapshot
  result: GoalDraftPersistResult
}

// Coordination belongs beside persistence, not to one popover mount. A remount
// must keep request ordering, local-write fallbacks, and canonical provenance
// as one per-slot protocol.
const remoteWriteTails = createSlotKeyedRecord<Promise<void>>()
const remoteCanonicalUpdatedAt = createSlotKeyedRecord<number>()
const retainedGoalDrafts = createSlotKeyedRecord<RetainedGoalDraft>()

export function sameGoalDraft(left: GoalDraft | null, right: GoalDraft | null): boolean {
  if (left === null || right === null) return left === right
  return left.message === right.message
    && left.idleSecs === right.idleSecs
    && left.maxCycles === right.maxCycles
}

export function sameGoalDraftSnapshot(
  left: GoalDraftSnapshot,
  right: GoalDraftSnapshot,
): boolean {
  return left.updatedAt === right.updatedAt && sameGoalDraft(left.draft, right.draft)
}

function baseForSnapshot(slot: string, snapshot: GoalDraftSnapshot): number | undefined {
  const retained = retainedGoalDrafts[slot]
  if (retained && sameGoalDraftSnapshot(retained.snapshot, snapshot)) return retained.baseUpdatedAt
  const record = readRecord(slot)
  if (
    record
    && record.updatedAt === snapshot.updatedAt
    && sameGoalDraft(storedDraftValue(record.value), snapshot.draft)
  ) return record.baseUpdatedAt
  return undefined
}

/** The newest recoverable browser copy, including quota-failed and retained canonical state. */
export function latestLocalGoalDraft(slot: string): GoalDraftSnapshot {
  const stored = loadGoalDraftSnapshot(slot)
  const retained = retainedGoalDrafts[slot]
  if (!retained) return stored
  const canonicalStillSupersedesStored = retained.canonicalSupersedesUpdatedAt !== undefined
    && stored.updatedAt <= retained.canonicalSupersedesUpdatedAt
  if (canonicalStillSupersedesStored || retained.snapshot.updatedAt >= stored.updatedAt) {
    return retained.snapshot
  }
  // A fresh durable edit outranks an older module fallback. Retire the fallback
  // now so reopening cannot restore it after this record is read.
  delete retainedGoalDrafts[slot]
  return stored
}

/** Persist a new edit/tombstone locally and retain it in memory if Web Storage rejects it. */
export function savePendingGoalDraft(
  slot: string,
  draft: GoalDraft | null,
  updatedAt?: number,
): GoalDraftSnapshot {
  const stored = loadGoalDraftSnapshot(slot)
  const retained = retainedGoalDrafts[slot]
  const latest = latestLocalGoalDraft(slot)
  const stamp = updatedAt ?? Math.max(
    Date.now(),
    stored.updatedAt + 1,
    (retained?.snapshot.updatedAt ?? 0) + 1,
  )
  const baseUpdatedAt = baseForSnapshot(slot, latest) ?? 0
  const snapshot = writeGoalDraftRecord(slot, draft, stamp, undefined, baseUpdatedAt)
  if (snapshot.persisted === false) {
    retainedGoalDrafts[slot] = { snapshot, baseUpdatedAt }
  } else {
    const previous = retainedGoalDrafts[slot]
    if (!previous || previous.snapshot.updatedAt <= snapshot.updatedAt) {
      delete retainedGoalDrafts[slot]
    }
  }
  return snapshot
}

/** Cache a server answer only while its submission is still the newest recoverable local edit. */
export function cacheCanonicalGoalDraft(
  slot: string,
  submitted: GoalDraftSnapshot,
  canonical: GoalDraftSnapshot,
): Exclude<GoalDraftPersistResult, 'remote-failed' | 'conflict'> {
  const newest = latestLocalGoalDraft(slot)
  if (!sameGoalDraftSnapshot(newest, submitted)) return 'superseded'

  const retained = retainedGoalDrafts[slot]
  const resaved = writeGoalDraftRecord(
    slot,
    canonical.draft,
    canonical.updatedAt,
    submitted,
    canonical.updatedAt,
  )
  remoteCanonicalUpdatedAt[slot] = canonical.updatedAt
  if (resaved.persisted === false) {
    retainedGoalDrafts[slot] = {
      snapshot: resaved,
      baseUpdatedAt: canonical.updatedAt,
      canonicalSupersedesUpdatedAt: submitted.updatedAt,
    }
  } else if (!retained || sameGoalDraftSnapshot(retained.snapshot, submitted)) {
    delete retainedGoalDrafts[slot]
  }
  return resaved.persisted === false ? 'retained' : 'persisted'
}

/** Append one remote write to the slot's process-wide tail. */
export function enqueueRemoteGoalDraft(
  slot: string,
  snapshot: GoalDraftSnapshot,
  writer: GoalDraftRemoteWriter,
  options: { keepalive?: boolean; migration?: boolean; baseUpdatedAt?: number } = {},
): Promise<GoalDraftSnapshot> {
  const previous = remoteWriteTails[slot] ?? Promise.resolve()
  const request = previous
    .catch(() => undefined)
    .then(() => writer(slot, snapshot, options.migration === true ? options : {
      ...options,
      baseUpdatedAt: options.baseUpdatedAt
        ?? remoteCanonicalUpdatedAt[slot]
        ?? baseForSnapshot(slot, snapshot)
        ?? 0,
    }))
    .then(canonical => {
      remoteCanonicalUpdatedAt[slot] = canonical.updatedAt
      return canonical
    })
  const tail = request.then(() => undefined, () => undefined)
  remoteWriteTails[slot] = tail
  void tail.finally(() => {
    if (remoteWriteTails[slot] === tail) delete remoteWriteTails[slot]
  })
  return request
}

/**
 * Upload a browser copy that outranks the canonical the open read returned.
 * A first sync keeps the browser edit time (`migration`), so the server decides
 * whether the offline copy is newer. Equal timestamps belong to the canonical
 * server copy rather than being restamped by an individual tab.
 */
export async function migrateLocalGoalDraft(
  slot: string,
  local: GoalDraftSnapshot,
  canonical: GoalDraftSnapshot,
  writer: GoalDraftRemoteWriter,
  options: { replaceConflict?: boolean } = {},
): Promise<GoalDraftMigrationResult> {
  const submission = local
  if (sameGoalDraftSnapshot(submission, canonical)) {
    return { canonical, result: cacheCanonicalGoalDraft(slot, submission, canonical) }
  }
  const liveBase = baseForSnapshot(slot, submission)
  if (liveBase !== undefined) {
    if (liveBase !== canonical.updatedAt && options.replaceConflict !== true) {
      return { canonical, result: 'conflict' }
    }
    try {
      const answer = await enqueueRemoteGoalDraft(slot, submission, writer, {
        baseUpdatedAt: options.replaceConflict === true ? canonical.updatedAt : liveBase,
      })
      return { canonical: answer, result: cacheCanonicalGoalDraft(slot, submission, answer) }
    } catch (error) {
      return { result: error instanceof GoalDraftConflict ? 'conflict' : 'remote-failed' }
    }
  }
  const timestampLessBootstrap = submission.updatedAt === 0
    && submission.draft !== null
    && canonical.updatedAt === 0
    && canonical.draft === null
  if (submission.updatedAt <= canonical.updatedAt && !timestampLessBootstrap) {
    return { canonical, result: cacheCanonicalGoalDraft(slot, submission, canonical) }
  }
  try {
    const answer = await enqueueRemoteGoalDraft(
      slot,
      submission,
      writer,
      { migration: !timestampLessBootstrap },
    )
    // Canonical zero after a nonzero submission means the bounded store took
    // the write and evicted it at once; it is not permission to erase the
    // browser's only copy.
    if (submission.updatedAt > 0 && answer.updatedAt === 0) {
      return { canonical: answer, result: 'remote-failed' }
    }
    return { canonical: answer, result: cacheCanonicalGoalDraft(slot, submission, answer) }
  } catch {
    return { result: 'remote-failed' }
  }
}

/** @internal test-only: reset module state between tests. `undefined` in the
 *  production bundle. */
export const __resetForTests: () => void = import.meta.env.PROD
  ? (undefined as unknown as () => void)
  : () => {
      for (const registry of [remoteWriteTails, remoteCanonicalUpdatedAt, retainedGoalDrafts]) {
        for (const slot of Object.keys(registry)) delete registry[slot]
      }
    }
