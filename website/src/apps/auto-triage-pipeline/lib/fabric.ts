// Pure fold: turn a folded work item (api.ts's `CrewFabricItem`) into the
// primitives the pipeline view draws — the reached columns, the head, the phase
// class (edit/wait/reply/exit/done), the return traces, the reopen count, and
// the per-phase dwell. No React, no DOM, no timers — every function here takes
// its inputs explicitly (including "now" for dwell), so the whole layer is unit
// testable without a render. Wave 2 changes only the PRESENTATION on top of this.
//
// The SERVER already did the phase-recording fold and hands us `phase` (live,
// authoritative), `timeline` (time order, may repeat a phase) and `exit`. So this
// layer re-derives only the DRAWING facts, and — per PLAN.md's design decisions —
// must not second-guess the live phase:
//
//   1. Columns are the ON-SPINE phase enum. `resolved` is the only terminal on
//      the spine; the four other terminals and `awaiting-reply` are EXITS drawn
//      off the lane, never columns (a column would imply a skipped item got
//      further than a claimed one).
//   2. The head is the item's LIVE `phase`, not `timeline`'s max index. A review
//      round-trip ends LEFT of where it has been.
//   3. An open dwell is measured from the item's MOST RECENT entry into its
//      current phase, because re-entering a phase restarts its clock.

import type {
  CrewFabricItem, CrewFabricTimelineEntry, RepoRef, ConnectedRepo,
} from '../api'
import { CREW_PHASES, type CrewPhase } from '../api'

/** The phases that get a COLUMN, in lifecycle order — the on-spine subset of the
 * enum. `resolved` is the only terminal here; every off-spine phase is an exit
 * (see `EXIT_PHASES`). Derived from `CREW_PHASES` by removing the off-spine ones,
 * so it cannot drift from the shared enum. */
export const SPINE_PHASES: readonly CrewPhase[] = [
  'selected',
  'claimed',
  'investigating',
  'implementing',
  'awaiting-ci',
  'addressing-review',
  'awaiting-merge',
  'resolved',
]

/** Off-spine phases → their short exit label token. A lane whose live phase (or a
 * mid-timeline step) is one of these is drawn as a stub OFF the lane rather than a
 * column. `awaiting-reply` is here too: it is a wait on a human, an alternate
 * holding state, not a later stage than `awaiting-merge`. */
export const EXIT_PHASES: Readonly<Record<string, true>> = {
  'skipped': true,
  'yielded': true,
  'handed-back': true,
  'preempted': true,
  'awaiting-reply': true,
}

/** The editing phases — the L1 "a crew is actively editing a worktree" class.
 * Mirrors `crew_store.EDITING_PHASES`. */
const EDITING_PHASES: Readonly<Record<string, true>> = {
  'implementing': true,
  'addressing-review': true,
}

/** Column index of an on-spine phase, or -1 for an off-spine one. The single
 * place the drawing turns a phase into an x-position. */
export function spineIndex(phase: string): number {
  return SPINE_PHASES.indexOf(phase as CrewPhase)
}

/** True when `phase` is a real member of the shared phase enum. Guards a payload
 * that carries a phase string this client does not know (forward-compat): an
 * unknown phase is ignored in the fold rather than crashing it. */
export function isKnownPhase(phase: string): boolean {
  return (CREW_PHASES as readonly string[]).includes(phase)
}

/** The L1 phase class — what COLOUR/word the live segment and head carry.
 *
 *   edit  — a crew is editing (implementing / addressing-review)
 *   reply — waiting on a human (awaiting-reply exit)
 *   exit  — the claim was released (skipped / yielded / handed-back / preempted)
 *   done  — terminal on the spine (resolved)
 *   wait  — anything else on the spine that is not editing (claimed, awaiting-ci…)
 *
 * Keyed off the LIVE phase, never the timeline max — same rule as the head. */
export type PhaseClass = 'edit' | 'wait' | 'reply' | 'exit' | 'done'

export function phaseClass(livePhase: string): PhaseClass {
  if (livePhase === 'awaiting-reply') return 'reply'
  if (EXIT_PHASES[livePhase]) return 'exit'
  if (livePhase === 'resolved') return 'done'
  if (EDITING_PHASES[livePhase]) return 'edit'
  return 'wait'
}

/** Epoch-ms for an ISO timestamp, or null when absent/unparseable (a legacy
 * timeline line may lack `at`). Null propagates as "no duration" in the dwell
 * math rather than as NaN or 0. */
export function tsMs(at?: string | null): number | null {
  if (!at) return null
  const t = new Date(at).getTime()
  return Number.isNaN(t) ? null : t
}

/** One return trace: the item came BACK to a spine column it had already
 * visited (a review round-trip, or a reopen). Drawn as an under-lane arc from
 * `from` back to `to`. */
export interface FabricLoop {
  from: number
  to: number
}

/** The drawing-ready fold of ONE work item. Everything the pipeline view needs to
 * lay out a lane, with no further phase reasoning on the render side. */
export interface FabricLane {
  number: number
  crewId: string
  title: string
  /** The crew's resumable INTENT for this item — what it is about to do next, not
   * a title. Shown in the hover card; never used as the lane's title. */
  next: string
  prNumber: number | null
  /** Live phase, verbatim from the payload — authoritative. */
  phase: CrewPhase
  /** The L1 class of the live phase. */
  cls: PhaseClass
  /** Column index of the live phase, or -1 when the item is off-spine (an exit).
   * This is the HEAD marker's column, taken from the live phase — never from the
   * furthest column reached. */
  head: number
  /** On-spine column indices the lane reached, ascending, de-duplicated. Each
   * gets a pad. */
  reach: number[]
  /** First-entry epoch-ms per reached column (the dwell into a column is measured
   * from the FIRST time it was entered to the first entry of the next column).
   * Null when that entry had no timestamp. */
  enteredAt: Map<number, number | null>
  /** The column the lane was in immediately before the head, in TIME (not the
   * second-furthest column). Drives where the coloured live segment starts.
   * -1 when there is no distinct prior column. */
  prevIdx: number
  /** Return traces (round-trips / reopens). */
  loops: FabricLoop[]
  /** How many times the lane re-entered the spine after leaving it. */
  reopens: number
  /** The exit stub, when the live phase is off-spine — its label token and the
   * column it left FROM. Null for an on-spine lane. */
  exit: { phase: CrewPhase; token: string; atColumn: number; atMs: number | null } | null
  /** Epoch-ms of the MOST RECENT entry into the current phase, for an open dwell.
   * Null when unknown (no timestamped current entry) or when the lane is terminal
   * / exited (a closed lane has no running clock). */
  currentSince: number | null
  /** The SPINE timeline in TIME order — every on-spine step, INCLUDING repeats
   * (an off-spine step is NOT here; it is carried in `exit`) — with `at` parsed to epoch-ms (null when the line had none). The
   * lane drawing folds this down to columns; the hover card renders it verbatim
   * as the full phase table, so it lives on the lane rather than being re-parsed
   * on the render side. Length matches the payload's `timeline` (unknown phases
   * are kept here — the card shows them; only the spine fold drops them). */
  timeline: Array<{ phase: string; atMs: number | null }>
}

/** One row of a lane's full phase table (the hover card). The dwell is the gap
 * from the PREVIOUS timeline step to this one — `null` for the first row or when
 * either end lacked a timestamp — and `slow` flags a step that sat longer than an
 * hour, the same >3600s threshold the lane dwell labels use. Pure: the card only
 * renders these. */
export interface TimelineRow {
  phase: string
  atMs: number | null
  dwellSeconds: number | null
  slow: boolean
}

/** The one-hour threshold above which a dwell is flagged (mirrors the mock's
 * `d > 3600` and the lane label rule). Exported so the view and the tests agree. */
export const SLOW_DWELL_SECONDS = 3600

/** Fold a lane's raw timeline into the hover card's rows: each step with the
 * dwell since the previous step and a `slow` flag for anything over an hour.
 * Pure and total — a legacy line with no `at` yields a null dwell rather than
 * NaN. */
export function laneTimelineRows(lane: FabricLane): TimelineRow[] {
  // A row's dwell is the time spent IN that phase -- this entry to the NEXT one --
  // not the gap that preceded it. Measuring backwards put the time in the wrong row:
  // claimed at 02:00 then implementing at 02:30 showed 30m beside `implementing`,
  // which is where the item had just arrived, while `claimed`, where those 30 minutes
  // were actually spent, showed nothing. The lane drawing was already correct because
  // it labels the SPAN between two columns; only this per-PHASE table read it as a
  // property of the row it sat on, which is what the DWELL header promises.
  //
  // The exit is the last stop when present, so the final spine phase is measured to
  // it rather than left open.
  const stops: Array<{ phase: string; atMs: number | null }> = [
    ...lane.timeline.map((e) => ({ phase: e.phase, atMs: e.atMs })),
    ...(lane.exit ? [{ phase: lane.exit.phase, atMs: lane.exit.atMs }] : []),
  ]
  return stops.map((stop, k) => {
    const next = k + 1 < stops.length ? stops[k + 1].atMs : null
    // The last stop has no successor: its clock is still running (or the lane is
    // closed), and an open dwell is reported separately rather than guessed here.
    const secs = next == null ? null : dwellSeconds(stop.atMs, next)
    return {
      phase: stop.phase,
      atMs: stop.atMs,
      dwellSeconds: secs,
      slow: secs != null && secs > SLOW_DWELL_SECONDS,
    }
  })
}

/** The catalog KEY (relative to `apps.autoTriagePipeline.pipeline`) an off-spine
 * phase renders its exit label from. Pure — this module decides WHICH label a
 * phase gets and stays unit-tested on that decision; the English words live in
 * the catalogs and the view translates the key. An unknown phase gets no key
 * (empty string) and the view falls back to the raw phase. */
export function exitToken(phase: string): string {
  switch (phase) {
    case 'skipped': return 'exit_skipped'
    case 'yielded': return 'exit_yielded'
    case 'handed-back': return 'exit_handed_back'
    case 'preempted': return 'exit_preempted'
    case 'awaiting-reply': return 'exit_await_reply'
    default: return ''
  }
}

/**
 * Fold one work item into a `FabricLane`.
 *
 * Walks `timeline` in time order to recover which SPINE columns were reached,
 * when each was first entered, and every return trace. The head and the phase
 * class come from the LIVE `phase`, not from the walk — a round-trip's last step
 * is left of its furthest column, and keying the head off the walk would put the
 * item in a phase it already left (PLAN.md decision 3).
 */
export function foldItem(item: CrewFabricItem): FabricLane {
  const enteredAt = new Map<number, number | null>()
  const reachSet: number[] = []
  const loops: FabricLoop[] = []
  let reopens = 0
  let sawExit = false
  // The column of the last DISTINCT on-spine phase seen, for the round-trip arc
  // and for prevIdx.
  let lastSpineCol = -1
  const timeline: CrewFabricTimelineEntry[] = Array.isArray(item.timeline) ? item.timeline : []

  for (const entry of timeline) {
    const ph = entry.phase
    if (!isKnownPhase(ph)) continue
    if (EXIT_PHASES[ph]) {
      // An exit does not advance the spine; it only records that the lane left it.
      sawExit = true
      continue
    }
    const col = spineIndex(ph)
    if (col < 0) continue
    if (enteredAt.has(col)) {
      // Came back to a column already visited — a round-trip, or a reopen if we
      // had exited since. The arc runs from the furthest-in-time prior column.
      loops.push({ from: lastSpineCol >= 0 ? lastSpineCol : col, to: col })
      if (sawExit) reopens += 1
    } else {
      enteredAt.set(col, tsMs(entry.at))
      reachSet.push(col)
    }
    // Re-entering the spine cancels a pending exit — the lane is live again.
    sawExit = false
    lastSpineCol = col
  }

  const reach = [...reachSet].sort((a, b) => a - b)

  const livePhase = item.phase
  const offSpine = EXIT_PHASES[livePhase] || !isKnownPhase(livePhase) || spineIndex(livePhase) < 0
  const head = offSpine ? -1 : spineIndex(livePhase)

  // prevIdx: the last DISTINCT on-spine column before the current one, in time.
  // Scan the timeline backwards past the current phase's own trailing entries.
  let prevIdx = -1
  for (let i = timeline.length - 1; i >= 0; i--) {
    const ph = timeline[i].phase
    if (EXIT_PHASES[ph] || !isKnownPhase(ph)) continue
    const col = spineIndex(ph)
    if (col < 0) continue
    if (col !== head) { prevIdx = col; break }
  }

  // The exit stub. Prefer the payload's own `exit` (the fold's authority); fall
  // back to the live phase when it is off-spine but the payload omitted `exit`.
  let exit: FabricLane['exit'] = null
  const exitPhase = item.exit?.phase ?? (offSpine && isKnownPhase(livePhase) ? livePhase : null)
  if (exitPhase && EXIT_PHASES[exitPhase]) {
    // The stub hangs off the furthest column the lane actually reached (or the
    // start when it reached none) — the point on the spine it departed from.
    const atColumn = reach.length ? reach[reach.length - 1] : -1
    exit = { phase: exitPhase, token: exitToken(exitPhase), atColumn, atMs: tsMs(item.exit?.at) }
  }

  // The most-recent entry into the CURRENT phase, for an open dwell. Only defined
  // for a live (non-terminal, non-exited) on-spine lane: a resolved/exited lane
  // has no running clock. Found by the LAST timeline entry whose phase is the
  // live phase — that is the re-entry a round-trip restarts the clock at.
  let currentSince: number | null = null
  const running = !offSpine && livePhase !== 'resolved'
  if (running) {
    for (let i = timeline.length - 1; i >= 0; i--) {
      if (timeline[i].phase === livePhase) {
        currentSince = tsMs(timeline[i].at)
        break
      }
    }
    // Fall back to the first-entry stamp of the head column when the trailing
    // entry lacked one but an earlier entry into the same column had it.
    if (currentSince == null && head >= 0) currentSince = enteredAt.get(head) ?? null
  }

  return {
    number: item.number,
    crewId: item.crew_id,
    title: item.title ?? '',
    next: item.next ?? '',
    prNumber: item.pr_number ?? null,
    phase: livePhase,
    cls: phaseClass(livePhase),
    head,
    reach,
    enteredAt,
    prevIdx,
    loops,
    // Prefer the payload's own count, but a 0/absent payload must fall back to
    // the walk-derived count rather than override it — `??` treats 0 as present,
    // so `max` is what lets the fold recover reopens on a payload that omitted it.
    reopens: Math.max(item.reopens ?? 0, reopens),
    exit,
    currentSince,
    timeline: timeline.map((entry) => ({ phase: entry.phase, atMs: tsMs(entry.at) })),
  }
}

/** Per-column occupancy for the station headers: how many LIVE lanes (not done,
 * not exited) sit in each spine column, and how many of those are editing. */
export interface ColumnOccupancy {
  total: number
  editing: number
}

export function columnOccupancy(lanes: FabricLane[]): Map<number, ColumnOccupancy> {
  const out = new Map<number, ColumnOccupancy>()
  for (const lane of lanes) {
    // A done or exited lane is not "occupying" its column — it has left the flow.
    if (lane.head < 0 || lane.cls === 'done') continue
    const cur = out.get(lane.head) ?? { total: 0, editing: 0 }
    cur.total += 1
    if (lane.cls === 'edit') cur.editing += 1
    out.set(lane.head, cur)
  }
  return out
}

/** Whole seconds a dwell spans, for the label. `null` in → `null` out. */
export function dwellSeconds(fromMs: number | null, toMs: number | null): number | null {
  if (fromMs == null || toMs == null) return null
  return Math.max(0, Math.round((toMs - fromMs) / 1000))
}

/** A dwell reduced to a single amount and the CLDR duration unit it is measured
 * in, WITHOUT rendering. The pure module keeps the threshold decision (seconds
 * under 90 read in seconds, under 90 minutes in minutes, else hours to one
 * decimal — the same ladder the mock's `dur()` used); the view turns this into a
 * localized string with `fmtUnit`, so the unit word and the number's separator
 * follow the active language instead of being welded on in English. `unit` is a
 * subset of `format.ts`'s `FormatUnit` — kept as a string-literal union here so
 * this module still imports nothing from the i18n layer. */
export interface DwellParts {
  value: number
  unit: 'second' | 'minute' | 'hour'
}

/** Reduce a whole-second dwell to `{value, unit}` for localized rendering.
 * Mirrors the mock's `dur()` thresholds exactly: `45s`, `12m`, `3.2h`. */
export function formatDwell(seconds: number): DwellParts {
  if (seconds < 90) return { value: seconds, unit: 'second' }
  if (seconds < 5400) return { value: Math.round(seconds / 60), unit: 'minute' }
  return { value: Number((seconds / 3600).toFixed(1)), unit: 'hour' }
}

/** The per-column dwell durations for a lane's reached columns (L2): the time
 * from entering column `reach[k-1]` to entering `reach[k]`. Only pairs with both
 * timestamps produce a value; a legacy line with no `at` yields null (skipped by
 * the view). Keyed by the DESTINATION column. */
export function laneDwells(lane: FabricLane): Array<{ toColumn: number; seconds: number }> {
  const out: Array<{ toColumn: number; seconds: number }> = []
  for (let k = 1; k < lane.reach.length; k++) {
    const a = lane.reach[k - 1]
    const b = lane.reach[k]
    const secs = dwellSeconds(lane.enteredAt.get(a) ?? null, lane.enteredAt.get(b) ?? null)
    if (secs != null) out.push({ toColumn: b, seconds: secs })
  }
  return out
}

/** The open dwell for a still-running lane, measured from its MOST RECENT entry
 * into the current phase to `nowMs` (the fold's `generated_at`, or the browser
 * clock as a fallback the view supplies). Null when the lane is closed or the
 * entry had no timestamp. A re-entered phase restarts this clock — that is why it
 * reads `currentSince`, not the first entry (PLAN.md decision 3). */
export function openDwellSeconds(lane: FabricLane, nowMs: number): number | null {
  if (lane.currentSince == null) return null
  return Math.max(0, Math.round((nowMs - lane.currentSince) / 1000))
}

/** The queue SUMMARY — "how is the queue doing" without reading a single lane.
 * The lane drawing answers "where is this item"; this answers the aggregate an
 * operator scans first. Wave 2 renders the full dashboard from these numbers; the
 * pure counts live here so they stay unit-tested. */
export interface QueueSummary {
  /** Live lanes per phase (excludes done/exited), for the "piling up where" read. */
  perPhase: Map<CrewPhase, number>
  /** Total live (non-terminal, non-exited on-spine) lanes. */
  live: number
  /** How many live lanes are currently editing (occupy an editing slot). */
  editing: number
  /** The MOST editing lanes any single crew holds at once.
   *
   * The store's cap is per crew (`_editing_item` is scoped to `crew_id`, and its
   * refusal reads "crew X is already editing #Y"), so N crews editing one item each
   * is legal and routine. Comparing the TOTAL against a cap of 1 therefore reports a
   * violation whenever a second crew starts work -- and reports it in fault red, for
   * something that is not a fault. This is the number the invariant actually bounds. */
  editingMaxPerCrew: number
  /** Total reopens summed across all lanes — the escalation count. */
  reopens: number
  /** Lanes that ended OFF the spine (skipped / yielded / handed-back / preempted /
   * awaiting-reply) — the exit count. An `awaiting-reply` lane is an exit here too,
   * matching the drawing: it is an alternate ending, not a queue position. */
  exits: number
  /** Lanes that reached `resolved` — the only terminal ON the spine. Reported so
   * the dashboard can show throughput (done) beside the live queue. */
  resolved: number
  /** The single longest open dwell in seconds, and the lane it belongs to, so the
   * summary can name what has waited longest. Null when nothing is running. */
  longestWait: { number: number; phase: CrewPhase; seconds: number } | null
}

export function queueSummary(lanes: FabricLane[], nowMs: number): QueueSummary {
  const perPhase = new Map<CrewPhase, number>()
  let live = 0
  let editing = 0
  const editingByCrew = new Map<string, number>()
  let reopens = 0
  let exits = 0
  let resolved = 0
  let longestWait: QueueSummary['longestWait'] = null

  for (const lane of lanes) {
    reopens += lane.reopens
    // Classify the ENDING first: an exited lane (off-spine head) or a resolved one
    // is not "in the queue" — it is throughput / an alternate ending.
    if (lane.exit) exits += 1
    if (lane.cls === 'done') resolved += 1
    const running = lane.head >= 0 && lane.cls !== 'done'
    if (!running) continue
    live += 1
    if (lane.cls === 'edit') {
      editing += 1
      editingByCrew.set(lane.crewId, (editingByCrew.get(lane.crewId) ?? 0) + 1)
    }
    perPhase.set(lane.phase, (perPhase.get(lane.phase) ?? 0) + 1)
    const wait = openDwellSeconds(lane, nowMs)
    if (wait != null && (longestWait == null || wait > longestWait.seconds)) {
      longestWait = { number: lane.number, phase: lane.phase, seconds: wait }
    }
  }

  const editingMaxPerCrew = editingByCrew.size
    ? Math.max(...editingByCrew.values())
    : 0
  return { perPhase, live, editing, editingMaxPerCrew, reopens, exits, resolved, longestWait }
}

/** The editing-slot invariant the store enforces (`crew_store.upsert_work_item`):
 * at most ONE work item may hold a crew's worktree at a time, so the editing
 * phases carry a structural cap of 1. The fabric payload has no per-crew
 * `max_open` field, so the dashboard reads in-flight editing against THIS
 * invariant rather than inventing a concurrency number. */
export const EDITING_SLOT_CAP = 1

/** The catalog KEY (relative to `apps.autoTriagePipeline.pipeline`) for the short
 * caption shown under a station header (and reused by the dashboard's per-phase
 * cells), keyed by phase. Pure lookup so the caption decision has one home and
 * the tests can assert it; the English words live in the catalogs and the view
 * translates the key. An unknown phase returns ''. */
export function phaseCaption(phase: string): string {
  switch (phase) {
    case 'selected': return 'caption_selected'
    case 'claimed': return 'caption_claimed'
    case 'investigating': return 'caption_investigating'
    case 'implementing': return 'caption_implementing'
    case 'awaiting-ci': return 'caption_awaiting_ci'
    case 'addressing-review': return 'caption_addressing_review'
    case 'awaiting-merge': return 'caption_awaiting_merge'
    case 'resolved': return 'caption_resolved'
    default: return ''
  }
}

/** The catalog KEY (relative to `apps.autoTriagePipeline.pipeline`) for the
 * station HEADER label of a phase — the abbreviated column name the mock uses
 * (`AWAIT CI`, `ADDR REV`, …). Pure so the header decision is not re-derived with
 * string replaces scattered across the view; the abbreviations live in the
 * catalogs and the view translates the key. An unknown phase returns ''. */
export function phaseHeader(phase: string): string {
  switch (phase) {
    case 'selected': return 'header_selected'
    case 'claimed': return 'header_claimed'
    case 'investigating': return 'header_investigating'
    case 'implementing': return 'header_implementing'
    case 'awaiting-ci': return 'header_awaiting_ci'
    case 'addressing-review': return 'header_addressing_review'
    case 'awaiting-merge': return 'header_awaiting_merge'
    case 'resolved': return 'header_resolved'
    default: return ''
  }
}

/** One cell of the queue dashboard's per-phase instrument row: a spine column, in
 * order, with its live count and whether it is an editing phase (so the cell can
 * carry the editing hue). Built over `SPINE_PHASES` so the dashboard's cells line
 * up exactly with the lane drawing's columns — the same "columns are the phase
 * enum" rule the whole view lives on. `busiest` marks the single phase with the
 * highest live count (ties resolve to the earliest column), for a "piling up here"
 * highlight; false for every cell when the queue is empty. */
export interface DashboardCell {
  phase: CrewPhase
  /** Catalog KEY for the abbreviated header label (see `phaseHeader`). */
  header: string
  /** Catalog KEY for the short caption (see `phaseCaption`). */
  caption: string
  count: number
  editing: boolean
  busiest: boolean
}

export function dashboardCells(summary: QueueSummary): DashboardCell[] {
  let maxCount = 0
  for (const phase of SPINE_PHASES) {
    const c = summary.perPhase.get(phase) ?? 0
    if (c > maxCount) maxCount = c
  }
  let busiestMarked = false
  return SPINE_PHASES.map((phase) => {
    const count = summary.perPhase.get(phase) ?? 0
    const busiest = !busiestMarked && maxCount > 0 && count === maxCount
    if (busiest) busiestMarked = true
    return {
      phase,
      header: phaseHeader(phase),
      caption: phaseCaption(phase),
      count,
      editing: EDITING_PHASES[phase] === true,
      busiest,
    }
  })
}

// ─────────────────────────────────────────────────────────────────────────────
// REPO RESOLUTION — pure, so the "which repo does a first-ever visit show"
// decision is unit-testable without a render. localStorage is a REMEMBERED
// PREFERENCE, never the source of truth: the backend's connected-repo list is
// authoritative, so a preference that names a repo no longer connected is stale
// and falls back to a deterministic default (first by a stable order, not by
// chance). This is what lets the app stand alone — it no longer needs a prior
// visit to Issue Radar to know which repo to show.
// ─────────────────────────────────────────────────────────────────────────────

/** Fill in the identity defaults a legacy record omits, so two refs that mean the
 * same repo compare equal. A record written before GitLab support carries no
 * `provider`/`host`; both default to public GitHub — matching the backend, which
 * fills the same defaults in `store.list_connected_repos`. */
function withDefaults(r: { provider?: string; host?: string }): { provider: string; host: string } {
  return { provider: r.provider || 'github', host: r.host || 'github.com' }
}

/** A stable, total ordering key for a connected repo: `provider:host/owner/repo`,
 * lower-cased. Sorting by it makes "the default repo" deterministic across
 * reloads and machines — config insertion order is NOT stable, so picking
 * `repos[0]` directly would be "by chance". */
export function repoOrderKey(r: ConnectedRepo): string {
  const { provider, host } = withDefaults(r)
  return `${provider}:${host}/${r.owner}/${r.repo}`.toLowerCase()
}

/** Byte order, NOT `localeCompare`, for picking a default repo.
 *
 * `repoOrderKey` builds a machine identity (`provider:host/owner/repo`, lowercased)
 * whose only job is to make "first repo" deterministic. `localeCompare` reads the
 * BROWSER's locale, so the same connected set could pick a different default repo
 * for two users -- or for one user after a language switch -- and the app would open
 * on a different repo with nothing in the UI explaining why. Bytes are stable
 * everywhere, which is what a tie-break needs. Nothing here is shown to a reader, so
 * there is no collation to get right. */
export function byteOrder(a: string, b: string): number {
  return a === b ? 0 : a < b ? -1 : 1
}

/** Whether a stored preference names the SAME repo as a connected entry — the
 * full identity (provider+host+owner+repo), because a `group/project` path names
 * a different project on gitlab.com than on a self-managed host. */
function sameRepo(pref: RepoRef, entry: ConnectedRepo): boolean {
  const a = withDefaults(pref)
  const b = withDefaults(entry)
  return (
    pref.owner === entry.owner &&
    pref.repo === entry.repo &&
    a.provider === b.provider &&
    a.host === b.host
  )
}

/** Turn a connected entry into the `RepoRef` the view scopes a fabric request to,
 * carrying the identity explicitly (never left to a default the request would
 * then guess differently). */
function toRef(entry: ConnectedRepo): RepoRef {
  const { provider, host } = withDefaults(entry)
  return { owner: entry.owner, repo: entry.repo, provider: provider as RepoRef['provider'], host }
}

/**
 * Resolve which repo the pipeline should show, given a remembered PREFERENCE
 * (may be null) and the list of CONNECTED repos (the backend source of truth).
 *
 * Contract — deterministic, and this is exactly what the tests pin:
 *   1. Preference present AND still connected  → that repo (identity-matched
 *      against the connected entry, so its canonical provider/host win).
 *   2. Preference present but NO LONGER connected → the first connected repo by
 *      `repoOrderKey` (the preference is stale, not authoritative).
 *   3. Preference absent, repos available → the first connected repo by
 *      `repoOrderKey`.
 *   4. No repos connected at all → null (the genuine empty state).
 *
 * Never returns a repo that is not in `connected`; never depends on the incoming
 * order of `connected`.
 */
export function selectRepo(preference: RepoRef | null, connected: ConnectedRepo[]): RepoRef | null {
  const list = Array.isArray(connected) ? connected.filter((r) => r && r.owner && r.repo) : []
  if (list.length === 0) return null
  if (preference) {
    const match = list.find((entry) => sameRepo(preference, entry))
    if (match) return toRef(match)
    // fall through: the preference is stale, use the deterministic default
  }
  const first = [...list].sort((a, b) => byteOrder(repoOrderKey(a), repoOrderKey(b)))[0]
  return toRef(first)
}
