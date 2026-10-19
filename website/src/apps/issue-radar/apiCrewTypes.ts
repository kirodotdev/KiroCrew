// Issue Radar's crew records: the shapes the crew routes return and accept, the
// phase list with its three classifications, and the event kinds, all mirroring
// the backend crew store (see below).
//
// `api.ts` re-exports every name here and is the only import path: the specs
// mock that module id, so a module importing this file directly would slip past
// their mocks. Nothing here imports `api.ts`.

// ── crews ───────────────────────────────────────────────────────────────────
//
// Every shape below MIRRORS the backend store,
// `src/kiro_crew/apps/builtins/issue_radar/backend/crew_store.py` — that module is
// the SOURCE OF TRUTH for the phase list, the three phase classifications, the
// event kinds and every record field. A crew record has no upstream to refetch
// from (unlike an issue, where a schema mismatch is just a cache miss), so these
// types and that module must be changed together.

/** Every phase a work item can be in, in lifecycle order — mirrors
 * `crew_store.PHASES`. `selected` is local-only and never public: it is the state
 * between "this issue looks workable" and the claim comment. */
export const CREW_PHASES = [
  'selected',
  'claimed',
  'investigating',
  'implementing',
  'awaiting-ci',
  'addressing-review',
  'awaiting-merge',
  'awaiting-reply',
  'resolved',
  'skipped',
  'yielded',
  'handed-back',
  'preempted',
] as const

export type CrewPhase = typeof CREW_PHASES[number]

/** Mirrors `crew_store.EVENT_KINDS`. The store REFUSES an unknown kind, so this
 * union is enforced server-side rather than merely documented.
 *
 * `sweep` is the one kind that belongs to no issue — a crew reporting that it
 * checked the queue and took nothing — so its lines carry no `number`. */
export const CREW_EVENT_KINDS = [
  'claim', 'investigate', 'reply', 'implement', 'ci',
  'review', 'conflict', 'merge', 'handback', 'skip', 'yield', 'sweep',
] as const

export type CrewEventKind = typeof CREW_EVENT_KINDS[number]

// The three phase classifications, mirroring `crew_store.py`'s frozensets of the
// same names. They deliberately do NOT coincide, which is why a view must read
// them from `api.ts` rather than re-deriving any of them from a phase string:
//
//   TERMINAL_PHASES     — the work is over, one way or another.
//   TTL_ACTIVE_PHASES   — only these age toward the claim TTL. A parked PR is
//                         stronger evidence of a live claim than a heartbeat, and
//                         a crew waiting three days on a human review has no
//                         progress to record.
//   EDITING_PHASES      — a worktree with uncommitted changes; at most one per
//                         crew, enforced in the store's `upsert_work_item`.

/** Mirrors `crew_store.TERMINAL_PHASES`. */
export const TERMINAL_PHASES: ReadonlySet<CrewPhase> = new Set<CrewPhase>([
  'resolved', 'skipped', 'yielded', 'handed-back', 'preempted',
])

/** Mirrors `crew_store.TTL_ACTIVE_PHASES`. */
export const TTL_ACTIVE_PHASES: ReadonlySet<CrewPhase> = new Set<CrewPhase>([
  'claimed', 'investigating', 'implementing',
])

/** Mirrors `crew_store.EDITING_PHASES`. */
export const EDITING_PHASES: ReadonlySet<CrewPhase> = new Set<CrewPhase>([
  'implementing', 'addressing-review',
])

/** Whether a work item occupies one of the crew's `max_open` slots — mirrors
 * `crew_store.open_slot_count`.
 *
 * Every NON-TERMINAL phase: an item is either finished or it is still the crew's
 * to carry, and a crew that cannot proceed on its own records the pass on the
 * issue and moves to the next one rather than parking a slot indefinitely.
 */
export function countsTowardOpen(phase: CrewPhase): boolean {
  return !TERMINAL_PHASES.has(phase)
}

/** One approach the crew already ruled out, so a later turn (or a fresh session
 * after compaction) does not retry it. */
export interface CrewTriedEntry {
  approach: string
  rejected_because: string
  at: string
}

/** CI readings for a work item's PR. Open-ended on purpose: the store MERGES
 * whatever the crew records into the existing map, and these four keys are the
 * ones the ledger's flattened `ci_*` fields write. */
export interface CrewCiState {
  passed?: number
  total?: number
  round?: number
  inherited_reds?: number
  [key: string]: unknown
}

/** One crew: a persistent worker with a name, a face and a work log.
 *
 * `avatar_seed` is stored SEPARATELY from `name` because renaming a crew must not
 * change its face. `retired_at` non-null means retired — the record, the name
 * reservation and the work log all survive, so an old claim comment can never be
 * mistaken for a live claim by a crew that reused the name. */
export interface Crew {
  schema: number
  id: string
  name: string
  avatar_seed: string
  avatar_variant: number | null
  agent: string
  model: string
  /** The model for turns that only check the queue; `''` = off, every turn on
   *  `model`. Optional because a gateway that predates it omits the field. */
  triage_model?: string
  /** True once a person picked the model of the crew's session by hand: the crew
   *  stops switching it until switching is resumed or a model is changed.
   *  Optional because a gateway that predates it omits the field. */
  model_held?: boolean
  extra_prompt: string
  labels: string[]
  auto_resolve_conflicts: boolean
  auto_merge: boolean
  unattended: boolean
  max_open: number
  worktree_root: string
  slot_key: string
  enabled: boolean
  paused_reason: string
  created_at: string
  retired_at: string | null
}

/** One crew × one issue. `last_progress_at` moves only on REAL progress (the
 * store enforces that), because the claim TTL is measured from it — a read-back
 * must not renew a claim. */
export interface WorkItem {
  schema: number
  crew_id: string
  owner: string
  repo: string
  number: number
  phase: CrewPhase
  outcome: string | null
  decision: string
  why: string
  next: string
  tried: CrewTriedEntry[]
  worktree: string
  branch: string
  base_sha: string
  pr_number: number | null
  ci_state: CrewCiState
  claim_comment_id: number | null
  labels_applied: string[]
  /** Null while the item is still `selected` — nothing has been claimed yet. */
  claimed_at: string | null
  last_progress_at: string
  finished_at: string | null
}

/** One line of the append-only progress ledger. `id` is content-addressed, so a
 * duplicated line merges on read instead of conflicting.
 *
 * `text` IS PUBLIC — it is rendered on the crew page AND inside the claim
 * comment on the forge. */
export interface CrewEvent {
  id: string
  ts: string
  crew_id: string
  /** ABSENT on a crew-level line (`kind: 'sweep'`), which belongs to no issue.
   *  Optional rather than nullable because the backend omits the key entirely —
   *  a `0` would be indistinguishable from a real issue number. */
  number?: number
  kind: CrewEventKind
  text: string
}

/** Repo-wide protocol constants. Deliberately NOT per-crew: two crews
 * negotiating with different TTLs is how a short-TTL crew steals a long-TTL
 * crew's live work. */
export interface CrewSettings {
  schema: number
  claim_ttl_hours: number
  /** The label a crew puts on an issue whose next step belongs to a human —
   * mirrors `crew_store.DEFAULT_SETTINGS['needs_human_label']`. Repo-wide, because
   * it is how the person answering finds those issues in the tracker's own
   * filters, and two crews using different labels would split that one queue. */
  needs_human_label: string
  commit_trailer: string
}

/** The crew-list header tallies, computed server-side so every view agrees. */
export interface CrewCounts {
  on_duty: number
  working: number
  paused: number
}

/** Fields a crew edit may carry. Partial — the store drops unknown keys and
 * validates every known one, so `{}` is a valid (no-op) patch.
 *
 * No `paused_reason`: pausing goes through `setCrewPaused`, which also stops the
 * crew's session. Writing the field alone would leave a paused-looking crew still
 * working. */
export interface CrewPatch {
  name?: string
  avatar_seed?: string
  avatar_variant?: number | null
  agent?: string
  model?: string
  triage_model?: string
  extra_prompt?: string
  worktree_root?: string
  labels?: string[]
  auto_resolve_conflicts?: boolean
  auto_merge?: boolean
  unattended?: boolean
  max_open?: number
  enabled?: boolean
}

/** The create payload. Only `name` is required — the store fills every other
 * field from its own defaults — and a duplicate name is refused server-side
 * (409), because the name field is free text and the suggestion chips are only a
 * convenience. */
export interface CrewSpec extends CrewPatch {
  name: string
}

/** One work-item write. Flat, mirroring the store's own patch vocabulary, and
 * every field optional: an omitted field keeps what an earlier write stored.
 *
 * `tried_approach` (+ `tried_rejected_because`) APPENDS one `tried` entry rather
 * than replacing the list. `event` + `event_kind` append one ledger line in the
 * same request, so a phase can never change without a logged reason. */
export interface WorkItemPatch {
  phase?: CrewPhase
  outcome?: string
  decision?: string
  why?: string
  next?: string
  worktree?: string
  branch?: string
  base_sha?: string
  pr_number?: number | null
  ci_state?: CrewCiState
  claim_comment_id?: number | null
  labels_applied?: string[]
  tried_approach?: string
  tried_rejected_because?: string
  /** The PUBLIC progress line (see `CrewEvent.text`). */
  event?: string
  event_kind?: CrewEventKind
}

/** Fields a settings write may carry; merged server-side. */
export interface CrewSettingsPatch {
  claim_ttl_hours?: number
  needs_human_label?: string
  commit_trailer?: string
}

export interface CrewsResponse {
  owner: string
  repo: string
  crews: Crew[]
  settings: CrewSettings
  counts: CrewCounts
}

/** Response to every single-crew write (create / update / pause / retire). */
export interface CrewResponse {
  crew: Crew
}

export interface CrewNamesResponse {
  suggestions: string[]
}

export interface CrewDetailResponse {
  crew: Crew
  items: WorkItem[]
  events: CrewEvent[]
  /** Slot usage for THIS crew, against `max_open`. Served rather than counted
   * client-side: the page renders a filtered slice of `items`, so a client tally
   * would follow the filter. */
  counts: { open: number }
  /** The crew's triage model when the account cannot run it, so every turn is on
   *  the work model instead; `''` (or absent on an older gateway) otherwise. */
  triage_model_unavailable?: string
  /** A person's hand pick holds the crew's session model, so the crew no longer
   *  switches to its triage model. Absent on an older gateway. */
  model_held?: boolean
  /** The held model, when the gateway holds the crew's session; `''` otherwise. */
  held_model?: string
}

/** `event` is null when the write carried no progress line. */
export interface CrewWorkResponse {
  item: WorkItem
  event: CrewEvent | null
}

export interface CrewSettingsResponse {
  settings: CrewSettings
}
