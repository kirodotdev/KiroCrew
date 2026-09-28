// Thin fetch wrapper for the Spec Builder builtin backend (registered on the
// main gateway's aiohttp Application, base path /api/apps/spec-builder — same
// convention as issue-radar / code-review-sage). Ported from the external
// kiro-specs app's /api/apps/kiro-specs module.
const API = '/api/apps/spec-builder'

/** Shown in the rail footer. Mirrors the builtin's app.json version and the
 *  Issue Radar convention of surfacing the app version in its own rail. */
export const APP_VERSION = '0.1.0'

// ── domain types ──────────────────────────────────────────────────────────

/** One row in the specs rail (GET /specs). */
export interface SpecSummary {
  name: string
  phase: string
  /** e.g. "executing" while an agent is building the task list. */
  status?: string
  /** true while the spec's agent turn is in flight (drives the pulsing dot). */
  running?: boolean
  /** Optional display label. The NAME stays the spec's identity (its directory,
   *  git branch and chat slot key); this is the only part a rename may change. */
  title?: string
  archived?: boolean
}

/** Per-document metadata used for hash-bound approval. */
export interface SpecDocMeta {
  /** Hash of the file AS STORED, even when its rendered copy is redacted. */
  hash: string
}

/** One addressable task parsed out of tasks.md. */
export interface SpecTask {
  /** Position among task lines; what the run endpoint addresses. */
  index: number
  text: string
  done: boolean
  /** Hash of the task text, sent with a run so a list that moved is refused
   *  rather than dispatching whatever ended up at that index. */
  hash: string
}

/** A recorded phase approval. */
export interface SpecApproval {
  /** The document hash that was approved. */
  hash: string
  at?: number
  user?: string
  /** True when the document changed after it was approved. */
  stale?: boolean
}

export interface SpecListResponse {
  specs: SpecSummary[]
}

/** A single structured decision the agent surfaced for the user to answer. */
export interface SpecDecision {
  id: string
  title: string
  options?: string[]
  recommended?: string
  answer?: string
  /** Set by the backend for a decision whose answer it has already dispatched.
   *  Such a decision is settled for good: the card must never render options for
   *  it again, even if the agent's own state file re-emits it as pending. */
  locked?: boolean
}

/** Phase-2 structured state the agent maintains in .spec-state.json. */
export interface SpecState {
  decisions?: SpecDecision[]
  blocking?: string
  context?: { template?: string }
}

/** Live counters exposed by the backend for the CONTEXT card. */
export interface SpecContextStats {
  turns?: number
  tool_calls?: number
  worktree_branch?: string
}

/** Full single-spec payload (GET /specs/{name}). */
export interface SpecDetail {
  name: string
  phase?: string
  status?: string
  working_dir?: string
  /** The chat slot this spec's conversation lives in. Server-assigned and
   *  per-creation, so it must never be derived client-side from the name. */
  slot_key?: string
  /** The spec's directory as the backend rendered it. Sent back with every
   *  mutation as a client-captured identity, so a stale tab cannot drive a
   *  same-name spec that was deleted and recreated elsewhere. */
  spec_dir?: string
  /** Document contents keyed by filename, e.g. { 'requirements.md': '…' }. */
  files?: Record<string, string>
  /** Stored-content hash per document, keyed like `files`. */
  docs?: Record<string, SpecDocMeta>
  /** tasks.md's checklist, enumerated. Derived by re-parsing the markdown, which
   *  stays the source of truth — there is no separate task store. */
  tasks?: SpecTask[]
  task_progress?: { done: number; total: number }
  /** Recorded human approvals, keyed by phase ('requirements' | 'design'). */
  approvals?: Record<string, SpecApproval>
  title?: string
  archived?: boolean
  /** False where crash-safe duplicate publication cannot pin directories. */
  duplicate_supported?: boolean
  state?: SpecState
  context?: SpecContextStats
  running?: boolean
  /** A crash left an accepted answer waiting for relay. The detail GET reports
   *  this only; the client requests recovery through a CSRF-protected POST. */
  decision_recovery_pending?: boolean
}

/** Directory listing for the project folder picker (GET /browse?path=). */
export interface BrowseEntry {
  name: string
  path: string
}

export interface BrowseResponse {
  path: string
  parent: string
  dirs: BrowseEntry[]
  /** true when `path` is (inside) a git repository — enables the worktree opt-in. */
  is_git?: boolean
  /** Recently-used project folders, returned on the initial (empty-path) browse. */
  recents?: string[]
}

export interface SettingsResponse {
  base_path?: string
  /** App-wide default model for spec generation. '' = inherit the chat default. */
  model?: string
}

/** Body for POST /specs. */
export interface CreateSpecBody {
  name: string
  working_dir: string
  spec_type: string
  description: string
  use_worktree: boolean
}

/** What the client rendered, sent back with every mutation so the server can
 *  refuse a stale control. Lifecycle mutations require the complete pair; the
 *  optional shape reflects that detail may still be loading, in which case the
 *  UI keeps those controls unavailable and the server rejects a direct call. */
export interface SpecIdentity {
  spec_dir?: string
  slot_key?: string
}

/** Drop empty fields so an absent value never reads as a claim of "". */
function identity(id?: SpecIdentity): Record<string, string> {
  const out: Record<string, string> = {}
  if (id?.spec_dir) out.spec_dir = id.spec_dir
  if (id?.slot_key) out.slot_key = id.slot_key
  return out
}

import { i18nT } from '../../i18n/t'

// ── fetch helper ────────────────────────────────────────────────────────────

/** An error carrying the backend's required machine-readable `code`. */
export class SpecApiError extends Error {
  code: string
  constructor(message: string, code: string) {
    super(message)
    this.name = 'SpecApiError'
    this.code = code
  }
}

/** An error carrying the backend's machine-readable `code`, when it sent one.
 *  Callers use it to tell a definite refusal ("nothing was recorded") apart from an
 *  ambiguous failure (the write may have committed and the response was lost). */
export interface ApiError extends Error {
  code?: string
}

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(API + path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!r.ok) {
    let msg = i18nT('apps.specBuilder.api.something_went_wrong', { status: r.status })
    let code = ''
    try {
      const parsed = (await r.json()) as { error?: string; code?: string }
      msg = parsed.error || msg
      code = parsed.code || ''
    } catch {
      /* non-JSON error body — keep the generic message */
    }
    throw new SpecApiError(msg, code)
  }
  if (r.status === 204) return undefined as T
  const text = await r.text()
  return (text.trim() === '' ? undefined : JSON.parse(text)) as T
}

const enc = (name: string) => encodeURIComponent(name)

// Reads take an optional AbortSignal so react-query can cancel a fetch that is
// no longer wanted -- switching specs while a poll is in flight otherwise lets
// the older response resolve last and overwrite the newer one. Writes
// deliberately take no signal: the request has already reached the server by the
// time a component unmounts, so cancelling the client side would hide the
// outcome of a mutation that still lands.
export const specApi = {
  // One response carries both sets; the rail already groups on `archived`.
  list: (signal?: AbortSignal) => req<SpecListResponse>('/specs', { signal }),
  create: (body: CreateSpecBody) => req<{ name?: string }>('/specs', { method: 'POST', body: JSON.stringify(body) }),
  get: async (name: string, signal?: AbortSignal) => {
    const detail = await req<SpecDetail>('/specs/' + enc(name), { signal })
    if (detail.decision_recovery_pending && detail.spec_dir && detail.slot_key) {
      const recovered = await req<{ ok: boolean }>(
        '/specs/' + enc(name) + '/recover-decision',
        {
          method: 'POST',
          body: JSON.stringify(identity(detail)),
        },
      )
      if (recovered.ok) detail.running = true
    }
    return detail
  },
  // specDir is the identity the CLIENT rendered: the backend compares it against
  // the live index so a stale tab cannot drive a same-name spec that was deleted
  // and recreated pointing somewhere else.
  // identity is the pair the CLIENT rendered: the per-creation slot key plus the
  // spec_dir. The backend compares both and refuses a mismatch, because a
  // directory alone does not identify a creation -- delete leaves the documents on
  // disk, so a re-import under the same name and path is a DIFFERENT spec.
  message: (name: string, text: string, id?: SpecIdentity) =>
    req<void>('/specs/' + enc(name) + '/message', {
      method: 'POST',
      body: JSON.stringify({ text, ...identity(id) }),
    }),
  // A decision card's answer, sent with the decision's id so the backend can
  // record it and refuse a second answer for the same decision. Same endpoint as
  // `message` — the id is what makes the write one-way.
  //
  // `option` is the bare choice and `text` the composed prompt the agent reads.
  // They are separate fields because the backend records the OPTION: recording the
  // prompt would render the whole localized sentence back as the answer.
  answerDecision: (name: string, decisionId: string, option: string, text: string, id?: SpecIdentity) =>
    req<void>('/specs/' + enc(name) + '/message', {
      method: 'POST',
      body: JSON.stringify({ text, decision_id: decisionId, decision_option: option, ...identity(id) }),
    }),
  execute: (name: string, id?: SpecIdentity) =>
    req<void>('/specs/' + enc(name) + '/execute', {
      method: 'POST',
      body: JSON.stringify(identity(id)),
    }),
  stop: (name: string, id?: SpecIdentity) =>
    req<void>('/specs/' + enc(name) + '/stop', {
      method: 'POST',
      body: JSON.stringify(identity(id)),
    }),
  // DELETE has no body, so the identity rides the query string.
  remove: (name: string, id?: SpecIdentity) => {
    const q = new URLSearchParams(identity(id) as Record<string, string>).toString()
    return req<void>('/specs/' + enc(name) + (q ? '?' + q : ''), { method: 'DELETE' })
  },
  // ── direct authority over recorded approvals and lifecycle ──
  /** Record an approval of `phase` against the exact document hash reviewed. The
   *  server rejects a hash that is not the current one, so an approval always
   *  names a version somebody actually saw. */
  approve: (name: string, phase: string, hash: string, id?: SpecIdentity) =>
    req<{ ok: boolean }>('/specs/' + enc(name) + '/approve', {
      method: 'POST',
      body: JSON.stringify({ phase, hash, ...identity(id) }),
    }),
  /** Run ONE task as a single turn. Both index and hash are sent: the agent
   *  rewrites tasks.md between polls, so an index alone could dispatch whatever
   *  ended up in that position. */
  runTask: (name: string, index: number, hash: string, id?: SpecIdentity) =>
    req<{ ok: boolean }>('/specs/' + enc(name) + '/task', {
      method: 'POST',
      body: JSON.stringify({ index, hash, ...identity(id) }),
    }),
  /** Set the display label. '' clears it and the UI falls back to the name. */
  setTitle: (name: string, title: string, id?: SpecIdentity) =>
    req<{ ok: boolean }>('/specs/' + enc(name) + '/title', {
      method: 'POST',
      body: JSON.stringify({ title, ...identity(id) }),
    }),
  /** Move a spec out of the working set, or bring it back. Non-destructive. */
  setArchived: (name: string, archived: boolean, id?: SpecIdentity) =>
    req<{ ok: boolean }>('/specs/' + enc(name) + '/archive', {
      method: 'POST',
      body: JSON.stringify({ archived, ...identity(id) }),
    }),
  /** Copy the documents into a new spec. The copy gets a fresh conversation. */
  duplicate: (name: string, new_name: string, id?: SpecIdentity) =>
    req<{ name: string }>('/specs/' + enc(name) + '/duplicate', {
      method: 'POST',
      body: JSON.stringify({ new_name, ...identity(id) }),
    }),
  getSettings: (signal?: AbortSignal) => req<{ base_path: string; model?: string }>('/settings', { signal }),
  saveSettings: (base_path: string, model: string) =>
    req<{ ok: boolean }>('/settings', { method: 'POST', body: JSON.stringify({ base_path, model }) }),
  browse: (path: string, signal?: AbortSignal) => {
    // Not copy: a URL. Built through URLSearchParams so the remaining literal has
    // the same shape as every other endpoint path in this file.
    const q = new URLSearchParams({ path: path || '' }).toString()
    return req<BrowseResponse>('/browse' + (q ? '?' + q : ''), { signal })
  },
}

// ── misc helpers ─────────────────────────────────────────────────────────────

/** localStorage keys (renamed from the external app's kiro-specs:* namespace). */
export const LS = {
  lastOpen: 'spec-builder:last-open',
  /** Legacy: '0' meant COLLAPSED. Read once for migration, never written. */
  railOpen: 'spec-builder:rail-open',
  /** Current: '1' means collapsed (the shared hook's encoding). */
  railCollapsed: 'spec-builder:rail-collapsed',
  railWidth: 'spec-builder:rail-width',
  docPct: 'spec-builder:doc-pct',
} as const

// ── rail geometry ────────────────────────────────────────────────────────────
// The specs rail is a resizable column (same hook Issue Radar's rail uses), so
// its width is a persisted number rather than a fixed class. Dragging well past
// the minimum collapses it to an icon strip.

export const DEFAULT_RAIL_WIDTH = 250
export const MIN_RAIL_WIDTH = 190
export const MAX_RAIL_WIDTH = 420
export const COLLAPSED_RAIL_WIDTH = 44

/** Persisted rail width, clamped — a corrupt value must not wedge the layout. */
export function loadRailWidth(): number {
  try {
    const v = Number(localStorage.getItem(LS.railWidth))
    if (Number.isFinite(v) && v >= MIN_RAIL_WIDTH && v <= MAX_RAIL_WIDTH) return v
  } catch { /* private mode — fall through */ }
  return DEFAULT_RAIL_WIDTH
}

/** Persisted collapsed flag.
 *
 *  The shared hook writes '1' for collapsed; the app's previous key wrote '0'
 *  for collapsed under the opposite name (rail-OPEN). Reusing that key would
 *  invert the state for anyone who had already collapsed the rail, so the flag
 *  moved to its own key and the old one is read once as a fallback.
 */
export function loadRailCollapsed(): boolean {
  try {
    const current = localStorage.getItem(LS.railCollapsed)
    if (current !== null) return current === '1'
    return localStorage.getItem(LS.railOpen) === '0'
  } catch {
    return false
  }
}

/** Slugify a free-text description into a stable spec name.
 *
 *  The ASCII filter below means a description written entirely in a non-Latin
 *  script (Korean, Japanese, Cyrillic, emoji, …) filters down to the empty
 *  string. The name becomes the spec's on-disk directory and its
 *  ``spec/<name>`` worktree branch, so rather than emitting raw Unicode into a
 *  filesystem path and a git ref, such input falls back to a generic ASCII
 *  stem plus a short hash of the original text. The hash is a pure function of
 *  the input: the branch-name preview and the created spec agree across
 *  re-renders, and the same description always derives the same name (a rare
 *  collision is absorbed by the caller's numeric-suffix retry). Latin input is
 *  unchanged byte-for-byte.
 */
export function slugify(text: string): string {
  const raw = text || ''
  const ascii = raw
    .toLowerCase()
    .replace(/[^a-z0-9\s_-]/g, '')
    .trim()
    .split(/\s+/)
    .slice(0, 5)
    .join('-')
    .replace(/-+/g, '-')
    // The backend name rule requires a leading alphanumeric; a description
    // opening with a bullet or underscore ('- add login') would otherwise
    // derive a name the create call rejects. All-punctuation input falls
    // through to the hash fallback below.
    .replace(/^[-_]+/, '')
    .slice(0, 48)
  if (ascii) return ascii
  // FNV-1a (32-bit) over the code units — deterministic, dependency-free, and
  // the ``spec-xxxxxxxx`` shape satisfies the backend's name rule
  // (``^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$``) as well as git ref syntax.
  let h = 0x811c9dc5
  for (let i = 0; i < raw.length; i++) {
    h ^= raw.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return 'spec-' + h.toString(16).padStart(8, '0')
}

/**
 * Catalog key per phase. A literal Record of keys is the only shape
 * ``scripts/check-i18n-keys.mjs`` can resolve statically, so the table holds
 * keys and ``phaseLabel`` translates at the point of use.
 */
const PHASE_LABEL_KEY: Record<string, string> = {
  new: 'apps.specBuilder.api.phase_new',
  requirements: 'apps.specBuilder.api.phase_requirements',
  design: 'apps.specBuilder.api.phase_design',
  tasks: 'apps.specBuilder.api.phase_tasks',
}

/** Status overrides that are not phases: shown while the agent runs, and for a
 *  finished plan in the rail. */
export const PHASE_BUILDING_KEY = 'apps.specBuilder.api.phase_building'
export const PHASE_READY_KEY = 'apps.specBuilder.api.phase_ready'

/**
 * Localised label for a spec phase, or the phase id VERBATIM when the backend
 * reports one this table does not know — better than fabricating copy.
 *
 * ``hasOwnProperty``, not ``in``: the phase comes off a backend payload, so a
 * value like ``toString`` would otherwise resolve to an inherited
 * Object.prototype member and hand a function to i18next.
 */
export function phaseLabel(phase: string): string {
  return Object.prototype.hasOwnProperty.call(PHASE_LABEL_KEY, phase)
    ? i18nT(PHASE_LABEL_KEY[phase])
    : phase
}

// ── detail poll cadence ──────────────────────────────────────────────────────
// The Tasks panel is a progress view over tasks.md. That file is also written by
// the agent and by hand in an editor, neither of which goes through a Spec
// Builder mutation — so a poll armed only by last-dispatch / slot.running sits
// idle while checkboxes flip on disk. These constants are the single owner of
// the cadences SpecDetail reads.

/** Fast poll while a turn is in flight, the Tasks tab is open, or tasks.md just changed. */
export const SPEC_DETAIL_FAST_POLL_MS = 2500
/** Catch the worker slot coming up after a dispatch (running is still false). */
export const SPEC_DETAIL_DISPATCH_POLL_MS = 1200
/** Idle poll when nothing is writing and the user is not watching Tasks. */
export const SPEC_DETAIL_IDLE_POLL_MS = 6000
/** How long a dispatch or an observed tasks.md hash change keeps the fast window. */
export const SPEC_DETAIL_FOLLOWUP_MS = 20000

export interface SpecDetailPollInput {
  running?: boolean
  status?: string
  /** True while the document tab showing the checklist is selected. */
  watchingTasks?: boolean
  msSinceDispatch?: number
  msSinceTasksChange?: number
}

/** Interval for the spec-detail React Query poll.
 *
 *  Fast while the worker is active, for a follow-up window after THIS view
 *  dispatched an instruction, while the user is looking at the Tasks panel,
 *  and for the same follow-up window after tasks.md's content hash changes
 *  (an agent or a hand edit that never hit lastSendAt). Otherwise idle.
 */
export function specDetailPollMs(input: SpecDetailPollInput): number {
  if (input.running || input.status === 'executing') return SPEC_DETAIL_FAST_POLL_MS
  if ((input.msSinceDispatch ?? Number.POSITIVE_INFINITY) < SPEC_DETAIL_FOLLOWUP_MS) {
    return SPEC_DETAIL_DISPATCH_POLL_MS
  }
  if (input.watchingTasks) return SPEC_DETAIL_FAST_POLL_MS
  if ((input.msSinceTasksChange ?? Number.POSITIVE_INFINITY) < SPEC_DETAIL_FOLLOWUP_MS) {
    return SPEC_DETAIL_FAST_POLL_MS
  }
  return SPEC_DETAIL_IDLE_POLL_MS
}
