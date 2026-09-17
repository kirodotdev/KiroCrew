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
 * Goal drafts use a dedicated versioned localStorage record journal because
 * pre-upgrade and delayed tabs may remain open while newer edits are offline.
 * Each edit or tombstone gets a unique primary key whose single JSON value holds
 * body + conflict timestamp atomically. Writers append; they never replace a
 * shared blob. Cleanup deletes only exact older keys observed by its own scan,
 * so a concurrent record cannot be overwritten or pruned. The old raw body and
 * timestamp sidecar remain read-only migration inputs until an explicit save.
 * Immutable retirement watermarks outlive executable-record TTL/cap eviction:
 * older or exact migrated legacy inputs stay retired, while equal-stamp distinct
 * content remains eligible for the normal reissue rule. Timestamp-less legacy
 * content has ordering stamp zero and can migrate only after the server proves
 * the canonical slot empty. Records keep the same 30-day TTL and 50-slot cap as
 * ordinary text drafts. Corrupt, missing, denied, or quota-exhausted storage
 * degrades to the in-memory fallback and the visible Retry path rather than throwing.
 */
import { createSlotKeyedRecord } from './slotDraftStore'
import { safeGetItem, safeSetItem } from './safeStorage'
import { DRAFT_MAX_ENTRIES, DRAFT_TTL_MS } from './draftConstants'

/** Pre-version clients keep writing this raw body + timestamp sidecar. */
export const LEGACY_GOAL_DRAFTS_KEY = 'mc-goal-drafts'
const LEGACY_GOAL_DRAFT_TIMESTAMPS_KEY = `${LEGACY_GOAL_DRAFTS_KEY}-ts`
/** Version-isolated immutable records. Old tabs never write this namespace. */
export const GOAL_DRAFTS_KEY = 'mc-goal-drafts-v2'
export const GOAL_DRAFT_RECORD_PREFIX = `${GOAL_DRAFTS_KEY}:`
/** Durable immutable watermarks prevent retired legacy inputs from resurfacing. */
export const GOAL_DRAFT_LEGACY_RETIREMENT_PREFIX = 'mc-goal-drafts-v2-retired:'
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
const GOAL_DRAFT_EQUAL_STAMP_RETRIES = 3

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

type LegacyGoalDraftInput = {
  updatedAt: number | null
  value: StoredGoalDraft
}

type GoalDraftRecord = {
  __goalDraftRecord: 2
  slot: string
  updatedAt: number
  value: StoredGoalDraft
  kind: 'local' | 'canonical'
  supersedes?: string[]
  retiresLegacy?: LegacyGoalDraftInput
}

type ParsedGoalDraftRecord = GoalDraftRecord & { key: string }

type GoalDraftLegacyRetirementRecord = {
  __goalDraftLegacyRetirement: 1
  slot: string
  through: number
  timestampLess: boolean
  exactInputs: LegacyGoalDraftInput[]
}

type ParsedGoalDraftLegacyRetirementRecord = GoalDraftLegacyRetirementRecord & { key: string }

type LegacyRetirementState = {
  through: number
  timestampLess: boolean
  exactInputs: LegacyGoalDraftInput[]
}

type SnapshotProvenance = {
  version?: string
  baseVersion?: string
  legacyInput?: LegacyGoalDraftInput
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

function sameStoredGoalDraft(left: StoredGoalDraft, right: StoredGoalDraft): boolean {
  if ('deleted' in left || 'deleted' in right) return 'deleted' in left && 'deleted' in right
  return left.message === right.message
    && left.idleSecs === right.idleSecs
    && left.maxCycles === right.maxCycles
}

function sanitizeLegacyInput(value: unknown): LegacyGoalDraftInput | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const row = value as Record<string, unknown>
  const updatedAt = row.updatedAt
  if (
    updatedAt !== null
    && (typeof updatedAt !== 'number' || !Number.isFinite(updatedAt) || updatedAt < 0)
  ) return null
  const stored = sanitizeStoredGoalDraft(row.value)
  return stored ? { updatedAt, value: stored } : null
}

function sameLegacyInput(left: LegacyGoalDraftInput, right: LegacyGoalDraftInput): boolean {
  return left.updatedAt === right.updatedAt && sameStoredGoalDraft(left.value, right.value)
}

// Every browser realm owns a distinct writer suffix. A timestamp collision is
// therefore still a total order, while no writer ever reuses another record's
// primary key. The record body holds value + timestamp together in one setItem.
const writerId = (() => {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID()
    }
  } catch { /* deterministic fallback below */ }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
})()
let recordSequence = 0
let lastIssuedWriteAt = 0
let snapshotProvenance = new WeakMap<GoalDraftSnapshot, SnapshotProvenance>()
const lastIssuedEditAt = createSlotKeyedRecord<number>()

function snapshotWithProvenance(
  snapshot: GoalDraftSnapshot,
  provenance: SnapshotProvenance,
): GoalDraftSnapshot {
  snapshotProvenance.set(snapshot, provenance)
  return snapshot
}

function nextRecordKey(slot: string, updatedAt: number): string {
  recordSequence += 1
  return `${GOAL_DRAFT_RECORD_PREFIX}${encodeURIComponent(slot)}:${Math.trunc(updatedAt).toString(36)}:${writerId}:${recordSequence.toString(36)}`
}

function nextLegacyRetirementKey(slot: string): string {
  recordSequence += 1
  return `${GOAL_DRAFT_LEGACY_RETIREMENT_PREFIX}${encodeURIComponent(slot)}:${writerId}:${recordSequence.toString(36)}`
}

function parseRecord(key: string): ParsedGoalDraftRecord | null {
  const raw = safeGetItem(key)
  if (raw === null) return null
  try {
    const value: unknown = JSON.parse(raw)
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null
    const row = value as Record<string, unknown>
    if (
      row.__goalDraftRecord !== 2
      || typeof row.slot !== 'string'
      || !row.slot
      || typeof row.updatedAt !== 'number'
      || !Number.isFinite(row.updatedAt)
      || row.updatedAt < 0
      || (row.kind !== 'local' && row.kind !== 'canonical')
    ) return null
    const stored = sanitizeStoredGoalDraft(row.value)
    if (!stored) return null
    const supersedes = Array.isArray(row.supersedes)
      ? row.supersedes.filter((item): item is string => (
          typeof item === 'string' && item.startsWith(GOAL_DRAFT_RECORD_PREFIX)
        ))
      : undefined
    const retiresLegacy = sanitizeLegacyInput(row.retiresLegacy)
    return {
      __goalDraftRecord: 2,
      slot: row.slot,
      updatedAt: row.updatedAt,
      value: stored,
      kind: row.kind,
      ...(supersedes?.length ? { supersedes } : {}),
      ...(retiresLegacy ? { retiresLegacy } : {}),
      key,
    }
  } catch {
    return null
  }
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

function readRecords(): ParsedGoalDraftRecord[] {
  // Snapshot the names first, then read each immutable primary key exactly once.
  // A concurrent append missed by this scan survives for the next scan/event;
  // unlike a shared envelope, it can never be overwritten by this reader.
  return readRecordKeys()
    .map(parseRecord)
    .filter((record): record is ParsedGoalDraftRecord => record !== null)
}

function parseLegacyRetirementRecord(
  key: string,
): ParsedGoalDraftLegacyRetirementRecord | null {
  const raw = safeGetItem(key)
  if (raw === null) return null
  try {
    const value: unknown = JSON.parse(raw)
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null
    const row = value as Record<string, unknown>
    if (
      row.__goalDraftLegacyRetirement !== 1
      || typeof row.slot !== 'string'
      || !row.slot
      || typeof row.through !== 'number'
      || !Number.isFinite(row.through)
      || row.through < 0
      || typeof row.timestampLess !== 'boolean'
      || !Array.isArray(row.exactInputs)
    ) return null
    const exactInputs: LegacyGoalDraftInput[] = []
    for (const candidate of row.exactInputs) {
      const input = sanitizeLegacyInput(candidate)
      if (!input || input.updatedAt === null) return null
      if (!exactInputs.some(existing => sameLegacyInput(existing, input))) {
        exactInputs.push(input)
      }
    }
    return {
      __goalDraftLegacyRetirement: 1,
      slot: row.slot,
      through: row.through,
      timestampLess: row.timestampLess,
      exactInputs,
      key,
    }
  } catch {
    return null
  }
}

function readLegacyRetirementRecords(): ParsedGoalDraftLegacyRetirementRecord[] {
  const keys: string[] = []
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key?.startsWith(GOAL_DRAFT_LEGACY_RETIREMENT_PREFIX)) keys.push(key)
    }
  } catch { /* denied storage is an ordinary local-fallback miss */ }
  return keys
    .map(parseLegacyRetirementRecord)
    .filter((record): record is ParsedGoalDraftLegacyRetirementRecord => record !== null)
}

function compareRecords(left: ParsedGoalDraftRecord, right: ParsedGoalDraftRecord): number {
  if (left.updatedAt !== right.updatedAt) return left.updatedAt - right.updatedAt
  if (left.key === right.key) return 0
  return left.key < right.key ? -1 : 1
}

function emptyLegacyRetirementState(): LegacyRetirementState {
  return { through: 0, timestampLess: false, exactInputs: [] }
}

function mergeLegacyRetirement(
  state: LegacyRetirementState,
  through: number,
  timestampLess: boolean,
  exactInputs: LegacyGoalDraftInput[],
): void {
  state.timestampLess ||= timestampLess
  state.through = Math.max(state.through, through)
  state.exactInputs = state.exactInputs.filter(input => (
    input.updatedAt !== null && input.updatedAt >= state.through
  ))
  for (const input of exactInputs) {
    if (
      input.updatedAt !== null
      && input.updatedAt >= state.through
      && !state.exactInputs.some(existing => sameLegacyInput(existing, input))
    ) {
      state.exactInputs.push(input)
    }
  }
}

function mergeLegacyInput(
  state: LegacyRetirementState,
  input: LegacyGoalDraftInput | undefined,
): void {
  if (!input) return
  if (input.updatedAt === null) {
    state.timestampLess = true
    return
  }
  mergeLegacyRetirement(state, state.through, false, [input])
}

function retirementStateForSlot(
  slot: string,
  retirementRecords: ParsedGoalDraftLegacyRetirementRecord[],
  goalRecords: ParsedGoalDraftRecord[] = [],
): LegacyRetirementState {
  const state = emptyLegacyRetirementState()
  for (const record of retirementRecords) {
    if (record.slot === slot) {
      mergeLegacyRetirement(
        state,
        record.through,
        record.timestampLess,
        record.exactInputs,
      )
    }
  }
  // Every durable v2 version is also a retirement watermark. Even an expired
  // version remains a non-executable carrier until its compact marker sticks.
  for (const record of goalRecords) {
    if (record.slot !== slot) continue
    mergeLegacyRetirement(state, record.updatedAt, false, [])
    mergeLegacyInput(state, record.retiresLegacy)
  }
  return state
}

function retirementStateCovers(
  actual: LegacyRetirementState,
  desired: LegacyRetirementState,
): boolean {
  if (desired.timestampLess && !actual.timestampLess) return false
  if (actual.through < desired.through) return false
  return desired.exactInputs.every(input => (
    input.updatedAt !== null && (
      input.updatedAt < actual.through
      || actual.exactInputs.some(existing => sameLegacyInput(existing, input))
    )
  ))
}

function persistLegacyRetirementState(
  slot: string,
  desired: LegacyRetirementState,
  observed: ParsedGoalDraftLegacyRetirementRecord[],
): boolean {
  const actual = retirementStateForSlot(slot, observed)
  const observedForSlot = observed.filter(record => record.slot === slot)
  if (retirementStateCovers(actual, desired) && observedForSlot.length <= 1) return true
  const merged = retirementStateForSlot(slot, observed)
  mergeLegacyRetirement(
    merged,
    desired.through,
    desired.timestampLess,
    desired.exactInputs,
  )
  const key = nextLegacyRetirementKey(slot)
  const record: GoalDraftLegacyRetirementRecord = {
    __goalDraftLegacyRetirement: 1,
    slot,
    through: merged.through,
    timestampLess: merged.timestampLess,
    exactInputs: merged.exactInputs,
  }
  if (!safeSetItem(key, JSON.stringify(record))) return false
  // Exact-key compaction cannot erase a concurrent append missed by `observed`.
  for (const old of observed) if (old.slot === slot) removeRecord(old.key)
  return true
}

function ensureLegacyRetirements(
  goalRecords: ParsedGoalDraftRecord[],
): Record<string, LegacyRetirementState> {
  const retirementRecords = readLegacyRetirementRecords()
  const states = createSlotKeyedRecord<LegacyRetirementState>()
  for (const record of retirementRecords) {
    states[record.slot] ??= emptyLegacyRetirementState()
    mergeLegacyRetirement(
      states[record.slot],
      record.through,
      record.timestampLess,
      record.exactInputs,
    )
  }
  const slots = new Set(goalRecords.map(record => record.slot))
  for (const slot of slots) {
    const desired = retirementStateForSlot(slot, retirementRecords, goalRecords)
    states[slot] = desired
    persistLegacyRetirementState(slot, desired, retirementRecords)
  }
  return states
}

function retireLegacyInput(
  slot: string,
  input: LegacyGoalDraftInput,
  through = input.updatedAt ?? 0,
): LegacyRetirementState {
  const retirementRecords = readLegacyRetirementRecords()
  const desired = retirementStateForSlot(slot, retirementRecords)
  mergeLegacyRetirement(desired, through, input.updatedAt === null, [])
  mergeLegacyInput(desired, input)
  persistLegacyRetirementState(slot, desired, retirementRecords)
  return desired
}

function legacyInputIsRetired(
  input: LegacyGoalDraftInput,
  retirement: LegacyRetirementState,
): boolean {
  if (input.updatedAt === null) return retirement.timestampLess
  if (input.updatedAt < retirement.through) return true
  return retirement.exactInputs.some(existing => sameLegacyInput(existing, input))
}

function latestRecords(
  records: ParsedGoalDraftRecord[],
): Record<string, ParsedGoalDraftRecord> {
  const byKey = new Map(records.map(record => [record.key, record]))
  const superseded = new Set<string>()
  for (const record of records) {
    if (record.kind !== 'canonical') continue
    for (const target of record.supersedes ?? []) {
      if (byKey.get(target)?.slot === record.slot) superseded.add(target)
    }
  }
  const cutoff = Date.now() - GOAL_DRAFT_TTL_MS
  const latest = createSlotKeyedRecord<ParsedGoalDraftRecord>()
  for (const record of records) {
    if (superseded.has(record.key) || record.updatedAt < cutoff) continue
    const prior = latest[record.slot]
    if (!prior || compareRecords(prior, record) < 0) latest[record.slot] = record
  }
  return latest
}

function removeRecord(key: string): void {
  try { localStorage.removeItem(key) } catch { /* cleanup is best-effort */ }
}

function compactRecords(): void {
  const keys = readRecordKeys()
  const records = keys
    .map(parseRecord)
    .filter((record): record is ParsedGoalDraftRecord => record !== null)
  ensureLegacyRetirements(records)
  const durableRetirements = readLegacyRetirementRecords()
  const byKey = new Map(records.map(record => [record.key, record]))
  const latest = Object.values(latestRecords(records))
    .sort((left, right) => compareRecords(right, left))
  const keep = new Set(latest.slice(0, GOAL_DRAFT_MAX_ENTRIES).map(record => record.key))
  // Delete only exact immutable keys observed by this scan. A concurrent append
  // has a different name and therefore cannot be pruned by stale cleanup. An
  // expired record remains as a non-executable retirement carrier when the
  // compact marker could not be persisted.
  for (const key of keys) {
    if (keep.has(key)) continue
    const record = byKey.get(key)
    if (!record) {
      removeRecord(key)
      continue
    }
    const desired = retirementStateForSlot(record.slot, [], [record])
    const durable = retirementStateForSlot(record.slot, durableRetirements)
    if (retirementStateCovers(durable, desired)) removeRecord(key)
  }
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
    return {
      updatedAt: typeof timestampValue === 'number'
        && Number.isFinite(timestampValue)
        && timestampValue >= 0
        ? timestampValue
        : null,
      value: stored,
    }
  } catch {
    return null
  }
}

function legacySnapshot(
  slot: string,
  knownRetirement: LegacyRetirementState,
): GoalDraftSnapshot {
  const input = readLegacyInput(slot)
  if (!input) return { draft: null, updatedAt: 0 }
  const retirement = retirementStateForSlot(slot, readLegacyRetirementRecords())
  mergeLegacyRetirement(
    retirement,
    knownRetirement.through,
    knownRetirement.timestampLess,
    knownRetirement.exactInputs,
  )
  if (legacyInputIsRetired(input, retirement)) return { draft: null, updatedAt: 0 }
  if (input.updatedAt !== null && input.updatedAt < Date.now() - GOAL_DRAFT_TTL_MS) {
    retireLegacyInput(slot, input)
    return { draft: null, updatedAt: 0 }
  }
  // A timestamp-less pre-sidecar value may fill an empty server slot, but it
  // has no evidence of recency and therefore never outranks canonical state.
  return snapshotWithProvenance(
    { draft: storedDraftValue(input.value), updatedAt: input.updatedAt ?? 0 },
    { legacyInput: input },
  )
}

/** True for every storage event that can change the local goal-draft view. */
export function isGoalDraftStorageKey(key: string | null): boolean {
  return key === LEGACY_GOAL_DRAFTS_KEY
    || key?.startsWith(GOAL_DRAFT_RECORD_PREFIX) === true
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
  const records = readRecords()
  const retirements = ensureLegacyRetirements(records)
  const record = latestRecords(records)[slot]
  if (!record) {
    const slotHistory = records.filter(candidate => candidate.slot === slot)
    const legacyInput = readLegacyInput(slot)
    if (legacyInput && slotHistory.length > 0) {
      const through = Math.max(...slotHistory.map(candidate => candidate.updatedAt))
      retireLegacyInput(slot, legacyInput, through)
      return { draft: null, updatedAt: 0 }
    }
    return legacySnapshot(slot, retirements[slot] ?? emptyLegacyRetirementState())
  }
  const legacyInput = readLegacyInput(slot)
  if (legacyInput) retireLegacyInput(slot, legacyInput, record.updatedAt)
  return snapshotWithProvenance(
    { draft: storedDraftValue(record.value), updatedAt: record.updatedAt },
    { version: record.key, legacyInput: record.retiresLegacy },
  )
}

function writeGoalDraftRecord(
  slot: string,
  draft: GoalDraft | null,
  updatedAt: number,
  kind: GoalDraftRecord['kind'],
  supersedes: string[] = [],
  suppliedLegacyInput?: LegacyGoalDraftInput,
): GoalDraftSnapshot {
  const clean = draft === null ? null : sanitizeGoalDraft(draft)
  const value: StoredGoalDraft = clean ?? { deleted: true }
  const key = nextRecordKey(slot, updatedAt)
  const base = loadGoalDraftSnapshot(slot)
  const baseProvenance = snapshotProvenance.get(base)
  const legacyInput = suppliedLegacyInput ?? baseProvenance?.legacyInput
  const snapshot = snapshotWithProvenance(
    { draft: storedDraftValue(value), updatedAt },
    { version: key, baseVersion: baseProvenance?.version, legacyInput },
  )
  const record: GoalDraftRecord = {
    __goalDraftRecord: 2,
    slot,
    updatedAt,
    value,
    kind,
    ...(supersedes.length ? { supersedes: [...new Set(supersedes)] } : {}),
    ...(legacyInput ? { retiresLegacy: legacyInput } : {}),
  }
  if (!safeSetItem(key, JSON.stringify(record))) {
    return snapshotWithProvenance(
      { ...snapshot, persisted: false },
      snapshotProvenance.get(snapshot) ?? {},
    )
  }
  compactRecords()
  const durable = loadGoalDraftSnapshot(slot)
  return snapshotProvenance.get(durable)?.version === key ? snapshot : durable
}

/** Remember a draft or a timestamped clear tombstone for `slot`. Each write is
 *  an immutable versioned record; a stale save can lose ordering but can never
 *  overwrite or prune the newer record that beat it. */
export function saveGoalDraft(
  slot: string,
  draft: GoalDraft | null,
  updatedAt?: number,
): GoalDraftSnapshot {
  const previous = loadGoalDraftSnapshot(slot)
  const effectiveUpdatedAt = updatedAt ?? Math.max(
    Date.now(),
    previous.updatedAt + 1,
    (lastIssuedEditAt[slot] ?? 0) + 1,
    lastIssuedWriteAt + 1,
  )
  lastIssuedEditAt[slot] = Math.max(lastIssuedEditAt[slot] ?? 0, effectiveUpdatedAt)
  lastIssuedWriteAt = Math.max(lastIssuedWriteAt, effectiveUpdatedAt)
  return writeGoalDraftRecord(slot, draft, effectiveUpdatedAt, 'local')
}

function saveCanonicalGoalDraft(
  slot: string,
  submitted: GoalDraftSnapshot,
  canonical: GoalDraftSnapshot,
): GoalDraftSnapshot {
  const provenance = snapshotProvenance.get(submitted)
  const supersedes = [provenance?.version, provenance?.baseVersion]
    .filter((value): value is string => value !== undefined)
  return writeGoalDraftRecord(
    slot,
    canonical.draft,
    canonical.updatedAt,
    'canonical',
    supersedes,
    provenance?.legacyInput,
  )
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
  options: { keepalive?: boolean; migration?: boolean } = {},
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
      draft: draft ? {
        message: draft.message,
        idle_secs: draft.idleSecs,
        max_cycles: draft.maxCycles,
      } : null,
    }),
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`)
  return parseRemoteSnapshot(body)
}

export type GoalDraftPersistResult = 'persisted' | 'retained' | 'superseded' | 'remote-failed'

type GoalDraftRemoteWriter = (
  slot: string,
  snapshot: GoalDraftSnapshot,
  options: { keepalive?: boolean; migration?: boolean },
) => Promise<GoalDraftSnapshot>

interface AcceptedCrossTabSync {
  snapshot: GoalDraftSnapshot
  status: 'pending' | 'failed' | 'canonical'
  canonical?: GoalDraftSnapshot
  promise?: Promise<AcceptedCrossTabSyncResult>
}

export interface AcceptedCrossTabSyncResult {
  canonical?: GoalDraftSnapshot
  result: GoalDraftPersistResult
  /** True only for the call that appended this snapshot to the per-slot write tail. */
  queued: boolean
}

// Coordination belongs beside persistence, not to one popover mount. A remount
// must keep request ordering, local-write fallbacks, canonical provenance, and
// cross-tab event deduplication as one per-slot protocol.
const remoteWriteTails = createSlotKeyedRecord<Promise<void>>()
const unsyncedGoalDrafts = createSlotKeyedRecord<GoalDraftSnapshot>()
const retainedCanonicalGoalDrafts = createSlotKeyedRecord<{
  canonical: GoalDraftSnapshot
  displaced: GoalDraftSnapshot
}>()
const acceptedCrossTabSyncs = createSlotKeyedRecord<AcceptedCrossTabSync>()

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

/** The newest recoverable browser copy, including quota-failed and retained canonical state. */
export function latestLocalGoalDraft(slot: string): GoalDraftSnapshot {
  const stored = loadGoalDraftSnapshot(slot)
  const pending = unsyncedGoalDrafts[slot]
  const retained = retainedCanonicalGoalDrafts[slot]
  if (
    pending
    && retained
    && sameGoalDraftSnapshot(pending, retained.canonical)
    && sameGoalDraftSnapshot(stored, retained.displaced)
  ) return pending
  if (!pending) return stored
  if (pending.updatedAt > stored.updatedAt) return pending
  if (pending.updatedAt < stored.updatedAt) return stored
  // Equal clocks do not identify an edit. The module-owned pending snapshot is
  // this tab's unsynced intent; retain it when the durable equal-stamp value is
  // distinct, then reissue it above the server canonical during reconciliation.
  return sameGoalDraft(pending.draft, stored.draft) ? stored : pending
}

/** Persist a new edit/tombstone locally and retain it in memory if Web Storage rejects it. */
export function savePendingGoalDraft(
  slot: string,
  draft: GoalDraft | null,
): GoalDraftSnapshot {
  const snapshot = saveGoalDraft(slot, draft)
  // A genuine edit supersedes provenance retained for an older canonical.
  delete retainedCanonicalGoalDrafts[slot]
  if (snapshot.persisted === false) {
    unsyncedGoalDrafts[slot] = snapshot
  } else if (
    !unsyncedGoalDrafts[slot]
    || unsyncedGoalDrafts[slot].updatedAt <= snapshot.updatedAt
  ) {
    delete unsyncedGoalDrafts[slot]
  }
  return snapshot
}

function retireOrRetainUnsynced(
  slot: string,
  resaved: GoalDraftSnapshot,
  pendingUpdatedAt?: number,
): void {
  const pending = unsyncedGoalDrafts[slot]
  if (
    pendingUpdatedAt === undefined
      ? pending !== undefined
      : pending?.updatedAt !== pendingUpdatedAt
  ) return
  if (resaved.persisted === false) {
    unsyncedGoalDrafts[slot] = resaved
    retainedCanonicalGoalDrafts[slot] = {
      canonical: resaved,
      displaced: loadGoalDraftSnapshot(slot),
    }
  } else if (pendingUpdatedAt !== undefined) {
    delete unsyncedGoalDrafts[slot]
    delete retainedCanonicalGoalDrafts[slot]
  }
}

/** Cache a server answer only while its submission is still the newest recoverable local edit. */
export function cacheCanonicalGoalDraft(
  slot: string,
  submitted: GoalDraftSnapshot,
  canonical: GoalDraftSnapshot,
): Exclude<GoalDraftPersistResult, 'remote-failed'> {
  const current = loadGoalDraftSnapshot(slot)
  const pending = unsyncedGoalDrafts[slot]
  const localUpdatedAt = Math.max(current.updatedAt, pending?.updatedAt ?? 0)
  const currentIsNewest = current.updatedAt === localUpdatedAt
  const pendingIsNewest = pending?.updatedAt === localUpdatedAt
  const currentMatchesSubmission = !currentIsNewest || (
    current.updatedAt === submitted.updatedAt
    && sameGoalDraft(current.draft, submitted.draft)
  )
  const pendingMatchesSubmission = !pendingIsNewest || (
    pending?.updatedAt === submitted.updatedAt
    && sameGoalDraft(pending.draft, submitted.draft)
  )
  if (
    submitted.updatedAt !== localUpdatedAt
    || (!currentIsNewest && !pendingIsNewest)
    || !currentMatchesSubmission
    || !pendingMatchesSubmission
  ) return 'superseded'

  const resaved = saveCanonicalGoalDraft(slot, submitted, canonical)
  retireOrRetainUnsynced(slot, resaved, pending?.updatedAt)
  const accepted = acceptedCrossTabSyncs[slot]
  if (accepted && sameGoalDraftSnapshot(accepted.snapshot, submitted)) {
    accepted.status = 'canonical'
    accepted.canonical = canonical
    delete accepted.promise
  }
  return resaved.persisted === false ? 'retained' : 'persisted'
}

/** Append one remote write to the slot's process-wide tail. */
export function enqueueRemoteGoalDraft(
  slot: string,
  snapshot: GoalDraftSnapshot,
  writer: GoalDraftRemoteWriter,
  options: { keepalive?: boolean; migration?: boolean } = {},
): Promise<GoalDraftSnapshot> {
  const previous = remoteWriteTails[slot] ?? Promise.resolve()
  const request = previous
    .catch(() => undefined)
    .then(() => writer(slot, snapshot, options))
  const tail = request.then(() => undefined, () => undefined)
  remoteWriteTails[slot] = tail
  void tail.finally(() => {
    if (remoteWriteTails[slot] === tail) delete remoteWriteTails[slot]
  })
  return request
}

/**
 * Queue a cross-tab snapshot exactly once per browser module. Duplicate storage
 * events share the first request; a failed request stays failed until the open
 * reconciliation or explicit Retry runs the normal migration path again.
 */
export function syncAcceptedCrossTabGoalDraft(
  slot: string,
  snapshot: GoalDraftSnapshot,
  knownCanonical: GoalDraftSnapshot | undefined,
  writer: GoalDraftRemoteWriter,
  options: { retryFailed?: boolean } = {},
): Promise<AcceptedCrossTabSyncResult> {
  // Equal wall-clock stamps do not prove equal edits. Preserve distinct local
  // content by issuing a strictly newer record before the migration comparison;
  // the server can then accept it without weakening its `stamp > current` rule.
  const equalStampConflict = knownCanonical !== undefined
    && snapshot.updatedAt === knownCanonical.updatedAt
    && !sameGoalDraft(snapshot.draft, knownCanonical.draft)
  const reissueAt = snapshot.updatedAt === 0 ? Date.now() : snapshot.updatedAt + 1
  const submission = equalStampConflict
    ? saveGoalDraft(slot, snapshot.draft, reissueAt)
    : snapshot

  if (knownCanonical && sameGoalDraftSnapshot(submission, knownCanonical)) {
    const legacyInput = snapshotProvenance.get(submission)?.legacyInput
    if (legacyInput) retireLegacyInput(slot, legacyInput, knownCanonical.updatedAt)
    acceptedCrossTabSyncs[slot] = {
      snapshot: submission,
      status: 'canonical',
      canonical: knownCanonical,
    }
    return Promise.resolve({ canonical: knownCanonical, result: 'persisted', queued: false })
  }

  const prior = acceptedCrossTabSyncs[slot]
  const priorCanonicalIsStale = (
    prior?.status === 'canonical'
    && knownCanonical !== undefined
    && !sameGoalDraftSnapshot(prior.canonical ?? prior.snapshot, knownCanonical)
  )
  if (
    prior
    && sameGoalDraftSnapshot(prior.snapshot, submission)
    && !priorCanonicalIsStale
    && !(prior.status === 'failed' && options.retryFailed)
  ) {
    if (prior.promise) return prior.promise
    return Promise.resolve({
      canonical: prior.canonical,
      result: prior.status === 'failed' ? 'remote-failed' : 'persisted',
      queued: false,
    })
  }

  const record: AcceptedCrossTabSync = { snapshot: submission, status: 'pending' }
  let submitted = submission
  const promise = enqueueRemoteGoalDraft(
    slot,
    submission,
    async (targetSlot, initial, writeOptions) => {
      let candidate = initial
      for (let attempt = 0; ; attempt += 1) {
        const canonical = await writer(targetSlot, candidate, writeOptions)
        const collided = canonical.updatedAt === candidate.updatedAt
          && !sameGoalDraft(canonical.draft, candidate.draft)
        if (!collided) {
          submitted = candidate
          return canonical
        }
        if (attempt >= GOAL_DRAFT_EQUAL_STAMP_RETRIES) {
          throw new Error('goal draft equal-stamp conflict did not settle')
        }
        candidate = saveGoalDraft(
          targetSlot,
          candidate.draft,
          Math.max(candidate.updatedAt, canonical.updatedAt) + 1,
        )
        submitted = candidate
        record.snapshot = candidate
      }
    },
    { migration: true },
  ).then(canonical => {
    // A bounded full store can accept then immediately evict an old migration.
    // Canonical zero is therefore not permission to erase a nonzero browser copy.
    if (submitted.updatedAt > 0 && canonical.updatedAt === 0) {
      record.status = 'failed'
      record.canonical = canonical
      delete record.promise
      return { canonical, result: 'remote-failed' as const, queued: true }
    }
    const result = cacheCanonicalGoalDraft(slot, submitted, canonical)
    record.status = 'canonical'
    record.canonical = canonical
    delete record.promise
    return { canonical, result, queued: true }
  }).catch(() => {
    record.status = 'failed'
    delete record.promise
    return { result: 'remote-failed' as const, queued: true }
  })
  record.promise = promise
  acceptedCrossTabSyncs[slot] = record
  return promise
}

/** @internal test-only: reset module state between tests. `undefined` in the
 *  production bundle. */
export const __resetForTests: () => void = import.meta.env.PROD
  ? (undefined as unknown as () => void)
  : () => {
      recordSequence = 0
      lastIssuedWriteAt = 0
      snapshotProvenance = new WeakMap<GoalDraftSnapshot, SnapshotProvenance>()
      for (const registry of [
        lastIssuedEditAt,
        remoteWriteTails,
        unsyncedGoalDrafts,
        retainedCanonicalGoalDrafts,
        acceptedCrossTabSyncs,
      ]) {
        for (const slot of Object.keys(registry)) delete registry[slot]
      }
    }
