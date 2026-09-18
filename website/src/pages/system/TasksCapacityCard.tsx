/**
 * Tasks & capacity — the durable task queue and the concurrency it runs under.
 *
 * Answers the questions the Sessions plane cannot: how much accepted work is
 * WAITING for capacity (and for how long), what the effective concurrency is
 * right now against the ceiling the user configured, why it was lowered, and
 * which live runs have yielded their slot and for what reason (children,
 * permission, dependency cooldown, real user input) or are being recovered.
 *
 * Read from `/api/tasks/summary` (`dashboard/handlers/tasks.py`), which folds
 * the task store, the structured session health and the adaptive controller
 * into one payload. Polled at the same cadence as the spawn panels: the store
 * read is cheap and the numbers move on the scale of seconds when a burst
 * lands.
 *
 * Purely additive to the Services plane: the card composes the plane's own
 * `Card` / section-row shape and the design tokens; it introduces no new
 * theme variables. It self-describes when the gateway runs without a task
 * store rather than rendering zeros as if the queue were empty.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Card, CardTitle, Badge } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import InfoTip from '../../components/InfoTip'
import { api } from '../../api/client'
import type { LaneCap, TaskRow, TasksSummary } from '../../api/tasks'
import { fmtDuration, fmtNumber, type FormatUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { findReport } from '../../utils/errorReport'
import { sessionChatPath } from './sessionRows'

/** Same cadence as the spawn/activity panels (`/api/spawn` every 5s). */
export const TASKS_SUMMARY_REFETCH_MS = 5_000
/**
 * Rows the waits list shows before it folds. The fold is what keeps the card
 * bounded when 2000 tasks are accepted; the rest stay one click away in place,
 * never behind a count with nowhere to go.
 */
const WAIT_ROWS_SHOWN = 8

/**
 * Catalog key per wait/health classification. A flat map of FULL literal keys
 * indexed inline at the `i18nT()` call, so `check-i18n-keys.mjs` validates the
 * union of the values; a key assembled from the wire's state string would be
 * invisible to that gate and render its raw dotted path the day a state is
 * renamed. Unknown states fall back to the raw token (a fact, not copy).
 */
const STATE_LABEL_KEY: Record<string, string> = {
  queued: 'pages.tasksCapacityCard.state_queued',
  admitted: 'pages.tasksCapacityCard.state_queued',
  starting: 'pages.tasksCapacityCard.state_running',
  running: 'pages.tasksCapacityCard.state_running',
  waiting_children: 'pages.tasksCapacityCard.state_waiting_children',
  waiting_permission: 'pages.tasksCapacityCard.state_waiting_permission',
  waiting_dependency: 'pages.tasksCapacityCard.state_waiting_dependency',
  waiting_input: 'pages.tasksCapacityCard.state_waiting_input',
  waiting_infra: 'pages.tasksCapacityCard.state_waiting_infra',
  retry_wait: 'pages.tasksCapacityCard.state_retry_wait',
  recovering: 'pages.tasksCapacityCard.state_recovering',
  stalled: 'pages.tasksCapacityCard.state_stalled',
}

/** Lane names the gateway publishes; anything else renders as its own key. */
const LANE_LABEL_KEY: Record<string, string> = {
  subagents: 'pages.tasksCapacityCard.lane_subagents',
  spawn_gate: 'pages.tasksCapacityCard.lane_spawn_gate',
  system: 'pages.tasksCapacityCard.lane_system',
}

/**
 * The two cap lanes `/api/tasks/summary` publishes when a controller answers
 * (`handlers/tasks.py`: the subagent manager's live cap and the controller's
 * `spawn_gate`). They name the empty capacity column so a reader meets the same
 * two rows whether or not the payload carries numbers — a label that surfaces
 * ONLY in the no-data state teaches a term the populated card never uses again.
 */
const PLACEHOLDER_LANES = ['subagents', 'spawn_gate']

/**
 * Catalog key per degrade reason the gateway publishes
 * (`slack/gateway.py::_wire_overload_health`). The wire token stays snake_case
 * because it is also the `taskq.pressure_reason` metric attribute, so it is
 * mapped here the same way `STATE_LABEL_KEY` maps wait states. Nothing couples
 * that Python closed set to this map: a token added there falls through to the
 * verbatim branch rather than breaking. Anything else — the controller's own
 * free-text reading, which `handlers/tasks.py` falls back to — is machine text.
 */
const DEGRADE_REASON_KEY: Record<string, string> = {
  adaptive_decrease: 'pages.tasksCapacityCard.degrade_adaptive_decrease',
  adaptive_pause: 'pages.tasksCapacityCard.degrade_adaptive_pause',
  adaptive_probe: 'pages.tasksCapacityCard.degrade_adaptive_probe',
}

type BadgeVariant = 'ok' | 'err' | 'warn' | 'aim' | 'muted'

/**
 * When the gateway publishes no stall threshold, the queue is "backed up"
 * once its oldest row has waited this long — the same 10 minutes
 * `session_health` uses before it calls a silent run stalled.
 */
const BACKLOG_WAIT_SECS_FALLBACK = 600
/**
 * Queued rows per effective slot of the widest lane before the backlog
 * counts as a warning: at 2× every slot already has a full turn of work
 * behind it, so a new request waits at least two dispatch cycles.
 */
const BACKLOG_DEPTH_FACTOR = 2

export type CardHealthState = 'healthy' | 'degraded' | 'backlog' | 'stalled'

export interface CardHealth {
  variant: BadgeVariant
  state: CardHealthState
}

/**
 * Every live slot the health monitor calls stalled: the `stalled` map's keys,
 * plus any slot whose own classification says so. ONE reading feeds the badge,
 * the Recovery counter and the list rows, so the card cannot go red over a stall
 * that has no row to open, nor list a row no counter admits to.
 */
function stalledKeysOf(d: TasksSummary): string[] {
  const keys = new Set(Object.keys(d.stalled))
  for (const slot of d.slots) {
    if (slot.classification === 'stalled') keys.add(slot.key)
  }
  return [...keys]
}

/**
 * Accepted work waiting for a SLOT — what the Queue column's first row shows.
 * `depth.queued` sums `queued + admitted + retry_wait + waiting_infra`
 * (`session_health.TASK_QUEUED_STATES`, the one set both backend surfaces read),
 * and `depth.recovering` counts those same `retry_wait`
 * rows as work being retried, so a row parked until its next attempt would sit
 * under two counters. It belongs to the more specific one, "Tasks retrying", and
 * is taken out here — including for the badge, which stands on the numbers the
 * card SHOWS: an amber "Backlog" over a queue count below its own threshold is a
 * verdict the card contradicts on the same line.
 */
function slotWaitingOf(d: TasksSummary): number {
  return Math.max(0, d.depth.queued - (d.depth.by_state.retry_wait ?? 0))
}

/** Literal keys per health state, indexed inline (see `STATE_LABEL_KEY`). */
const HEALTH_LABEL_KEY: Record<CardHealthState, string> = {
  healthy: 'pages.tasksCapacityCard.badge_healthy',
  degraded: 'pages.tasksCapacityCard.badge_degraded',
  backlog: 'pages.tasksCapacityCard.badge_backlog',
  stalled: 'pages.tasksCapacityCard.badge_stalled',
}

/**
 * The badge state, derived from the numbers the card already shows so the
 * two can never disagree. Severity order, first match wins:
 *
 *  1. `err`  — any stalled run (`stalled` non-empty): the same red the
 *              "Stalled" row badge uses, because a run with no progress and
 *              no wait reason is the one thing nothing here recovers on its own.
 *  2. `warn` — the controller lowered the effective cap (`degrade_reason`
 *              set): the strongest signal about capacity, kept ahead of the
 *              backlog rules since it explains why the backlog exists.
 *  3. `warn` — backed up: the oldest queued row has waited at least
 *              `stall_after_secs` (fallback `BACKLOG_WAIT_SECS_FALLBACK`), or the
 *              slot waiters (`slotWaitingOf`, the count the Queue column shows)
 *              exceed `BACKLOG_DEPTH_FACTOR` × the largest effective lane cap
 *              (skipped when no lane reports one). Amber to match the
 *              "Retrying now" row badge: work is late, not lost.
 *  4. `ok`   — otherwise.
 *
 * Null means NO verdict this card can support, which is what a gateway with no
 * task store leaves it: rules 1 and 2 read live sessions and the controller, both
 * published without a store, while every input rule 3 and 4 stand on
 * (`depth`, `oldest_wait_secs`) is then a zero the wire has nothing to fill with.
 * "Healthy" off those zeros is a health claim about a queue that is not being
 * recorded — the reader cannot tell it from "nothing to show" — so the badge is
 * withheld instead and the store notice states what is missing.
 */
export function cardHealth(d: TasksSummary): CardHealth | null {
  if (stalledKeysOf(d).length > 0) return { variant: 'err', state: 'stalled' }
  if (d.degrade_reason) return { variant: 'warn', state: 'degraded' }
  if (!d.available) return null
  const waitLimit = d.stall_after_secs ?? BACKLOG_WAIT_SECS_FALLBACK
  const widestCap = Math.max(0, ...Object.values(d.lanes).map(cap => cap.effective ?? 0))
  const longWait = d.oldest_wait_secs >= waitLimit
  const deepQueue = widestCap > 0 && slotWaitingOf(d) > BACKLOG_DEPTH_FACTOR * widestCap
  if (longWait || deepQueue) return { variant: 'warn', state: 'backlog' }
  return { variant: 'ok', state: 'healthy' }
}

/**
 * Two recovery states, two colours: `recovering` (amber) is a retry IN FLIGHT
 * — the backend is being restarted right now — while `retry_wait` (grey) is a
 * row PARKED until its next attempt, with nothing happening to it. Painted the
 * same amber they read as one state with two names, which is what the badge
 * copy exists to prevent.
 */
function stateVariant(state: string): BadgeVariant {
  if (state === 'stalled') return 'err'
  if (state === 'recovering' || state === 'waiting_infra') return 'warn'
  if (state.startsWith('waiting_')) return 'aim'
  if (state === 'running' || state === 'starting') return 'ok'
  return 'muted'
}

/** A row the USER has to act on: the run is parked on their answer, not on the system. */
export function stateLabel(state: string): string {
  const key = STATE_LABEL_KEY[state]
  return key ? i18nT(key) : state
}

function laneLabel(lane: string): string {
  const key = LANE_LABEL_KEY[lane]
  return key ? i18nT(key) : lane
}

/** Catalog copy for a known degrade token; null for anything unmapped. */
function degradeLabel(reason: string): string | null {
  const key = DEGRADE_REASON_KEY[reason]
  return key ? i18nT(key) : null
}

/**
 * The part of a failed fetch worth showing a reader: its message when that
 * message is a SENTENCE, null when it is the machine's spelling of "no reason
 * given". A refusal whose body is an empty JSON envelope reaches the client as
 * `{}` and a body-less one as `HTTP 500`; printed after "Could not load the task
 * queue" either reads as a blank where the cause belongs, so the notice states
 * the sentence alone and the raw text goes to the agent hand-off instead.
 *
 * PROSE means two or more letter-bearing words and no leading structural
 * delimiter — the narrowest rule that keeps "503 task store unavailable" and
 * "Failed to fetch" while dropping `{}`, `[]`, an HTML page and `HTTP 500`.
 */
export function proseReason(message: string): string | null {
  const text = message.trim()
  if (!text || /^[[{<]/.test(text)) return null
  const words = text.split(/\s+/).filter(word => /\p{L}/u.test(word))
  return words.length > 1 ? text : null
}

/**
 * An age in seconds as a compound duration. Seconds are kept: a 40-second
 * dependency cooldown is the common case here, and "0 min" would read as a
 * frozen counter.
 */
export function fmtAge(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '—'
  const s = Math.floor(seconds)
  const parts: Array<[number, FormatUnit]> = [
    [Math.floor(s / 86400), 'day'],
    [Math.floor((s % 86400) / 3600), 'hour'],
    [Math.floor((s % 3600) / 60), 'minute'],
    [s % 60, 'second'],
  ]
  // Past an hour the seconds are noise; past a day so are the minutes.
  const trimmed = s >= 86400 ? parts.slice(0, 2) : s >= 3600 ? parts.slice(0, 3) : parts
  return fmtDuration(trimmed, { dropZero: true, maximumFractionDigits: 0 })
}

/**
 * "Up to N at once" for a lane whose cap is NOT under the user's ceiling — the
 * runs it may hold right now. Not "N of M slots": beside a "0 running now" line
 * that reads as "N of M in use" and contradicts it. Short enough not to wrap in a
 * third of the card, so it is the one unbreakable line; the ceiling and the live
 * count go on the lines beneath (`capLines`).
 */
function fmtCap(cap: LaneCap): string {
  const eff = cap.effective
  if (eff == null) return '—'
  return i18nT('pages.tasksCapacityCard.cap_at_once', { effective: fmtNumber(eff) })
}

/**
 * The cap in force sits BELOW the ceiling the user configured for this lane. A
 * predicate rather than a bare boolean, so the one line that prints BOTH numbers
 * reads them off the same test that decided they exist.
 */
function isLowered(cap: LaneCap): cap is LaneCap & { effective: number; user_max: number } {
  return cap.user_max != null && cap.effective != null && cap.effective < cap.user_max
}

/** One line of a lane's capacity column, top to bottom. */
interface CapLine {
  text: string
  /** The line carrying the cap in force: never muted, it is what the column answers. */
  lead?: boolean
  /**
   * Set on the bare "Up to N at once" phrase alone, which is short enough to hold
   * one line at a third of the card. Every other line is prose about a number and
   * must be free to wrap, or it lands on top of the column beside it.
   */
  nowrap?: boolean
}

/**
 * A lane's lines. They state the RELATIONSHIP between the cap in force and the
 * ceiling the user configured, never two bare numbers: two maxima printed as two
 * labelled facts are read as two unrelated answers, and this column exists to
 * answer one question — how much work may run, and whether that is all the
 * reader asked for.
 *
 * So an equal pair is ONE fact ("Your full limit", no second number to
 * reconcile); a LOWERED cap puts both numbers in ONE line joined by the verb that
 * relates them ("Up to 4 at once, lowered from your 14"), because a bare cap
 * stacked over a line calling the ceiling "your limit" states two limits and the
 * reader asks which one is in force. `cap_ceiling` is left for the wire
 * contradiction alone — a cap ABOVE the user's ceiling, where neither "full"
 * nor "lowered" is true and the bare ceiling is the only honest line.
 *
 * Every line but the bare cap phrase is a SENTENCE about a number, not the
 * number, so the caller sets it in the UI font and lets it wrap: a third of the
 * card is ~230px and an unbreakable phrase that long overflows into the column
 * beside it.
 */
function capLines(cap: LaneCap, degradeText: string | null): CapLine[] {
  const lines: CapLine[] = []
  const { effective: eff, user_max: max, running } = cap
  if (isLowered(cap)) {
    lines.push({
      text: i18nT('pages.tasksCapacityCard.cap_below_limit', {
        effective: fmtNumber(cap.effective),
        max: fmtNumber(cap.user_max),
      }),
      lead: true,
    })
    // `degradeText` is non-null only for the lane the caller made the cause's one
    // home, in the SAME words the degrade row would use, and it continues that
    // line's verb ("lowered from your 14" / "Concurrency lowered under load").
    // Never a cause this function decides for itself: `degrade_reason` is one
    // GLOBAL string, so a lane that printed it on its own authority would print
    // it once per lowered lane and one cause would read as several.
    if (degradeText) lines.push({ text: degradeText })
  } else {
    lines.push({ text: fmtCap(cap), lead: true, nowrap: true })
    if (max == null) {
      // No ceiling on the wire: there is nothing for the cap to stand against.
    } else if (eff === max) {
      lines.push({ text: i18nT('pages.tasksCapacityCard.cap_at_limit') })
    } else {
      lines.push({ text: i18nT('pages.tasksCapacityCard.cap_ceiling', { max: fmtNumber(max) }) })
    }
  }
  if (running != null) {
    lines.push({ text: i18nT('pages.tasksCapacityCard.lane_running', { count: fmtNumber(running) }) })
  }
  return lines
}

/** One waiting/recovering entry, whether it came from the store or a live slot. */
interface WaitEntry {
  key: string
  id: string
  state: string
  reason: string
  ageSecs: number
  nextRunAt: number | null
  attempts: number
  /**
   * Where the row's subject IS: the session's chat, by the same route the
   * Sessions plane rows use. Every row this card shows names one session, and
   * naming a session the reader cannot open is the dead end — most sharply on a
   * `stalled` row, the one condition here that nothing recovers on its own and
   * so the one they most need to reach.
   *
   * Null ONLY when there is genuinely nowhere to go: `sessionChatPath` answers
   * null for a session with no chat window (cron, channel, `_bg`), so the guard
   * is the route's own and no dead link is rendered. Not gated on WHY the row is
   * waiting — an approval and a stall need the same window, and the lever lives
   * in that chat either way.
   */
  href: string | null
}

function entriesOf(data: TasksSummary): WaitEntry[] {
  // A parked row (retry_wait / recovering) carries no wait record; its
  // "reason" is when the dispatcher will pick it up again.
  const retryReason = (row: TaskRow): string =>
    row.next_run_at != null
      ? i18nT('pages.tasksCapacityCard.next_retry_in', {
        age: fmtAge(Math.max(0, row.next_run_at - data.generated_at)),
      })
      : ''
  const fromTask = (row: TaskRow): WaitEntry => ({
    key: `task:${row.id}`,
    id: row.id,
    state: row.state,
    reason: row.wait_reason ?? row.wait?.dependency_scope ?? retryReason(row),
    ageSecs: row.age_secs,
    nextRunAt: row.next_run_at,
    attempts: row.attempts,
    href: sessionChatPath(row.session_key),
  })
  const taskIds = new Set<string>()
  const out: WaitEntry[] = []
  for (const row of data.waiting) { out.push(fromTask(row)); taskIds.add(row.id) }
  for (const row of data.recovering.tasks) { out.push(fromTask(row)); taskIds.add(row.id) }
  // A stall is the only condition on this card that nothing recovers on its own,
  // so it is the one the reader most needs to open — and `stalled` is its own
  // wire field, carried whether or not a slot entry mirrors it. Built from
  // `stalledKeysOf` and skipped in the slot loop below, so one stall is ONE row.
  // The row's reason is the EVIDENCE, because the wire's `reason` reads
  // `no_progress` for every structured stall (the badge already says that) and
  // carries a sentence only when a log scan found one — hence the fallback.
  const slotOf = new Map(data.slots.map(slot => [slot.key, slot]))
  const stalledKeys = new Set(stalledKeysOf(data))
  for (const key of stalledKeys) {
    const stall = data.stalled[key]
    const evidence = stall?.evidence ?? slotOf.get(key)?.evidence ?? []
    out.push({
      key: `stalled:${key}`,
      id: key,
      state: 'stalled',
      reason: evidence.join(' · ') || stall?.reason || '',
      ageSecs: stall?.age_secs ?? slotOf.get(key)?.age_secs ?? 0,
      nextRunAt: null,
      attempts: 0,
      // A stall is keyed by SLOT on both wire sources, so its chat window is the
      // one place the evidence can be read against the transcript that produced
      // it -- and this is the row with no self-recovery, so a reader who cannot
      // open it has been told a name and given no exit.
      href: sessionChatPath(`dashboard:${key}`),
    })
  }
  // Live slots the store does not know (main chat sessions): their own
  // classification is the reason. A slot key is the bare dashboard slot; the
  // backend spells its session `dashboard:<slot>`.
  for (const slot of data.slots) {
    if (slot.classification === 'running' || taskIds.has(slot.key) || stalledKeys.has(slot.key)) continue
    out.push({
      key: `slot:${slot.key}`,
      id: slot.key,
      state: slot.classification,
      reason: slot.evidence.join(' · '),
      ageSecs: slot.age_secs,
      nextRunAt: null,
      attempts: 0,
      href: sessionChatPath(`dashboard:${slot.key}`),
    })
  }
  // Stalls first, then longest wait: the fold shows the first `WAIT_ROWS_SHOWN`
  // rows, so ordering by age alone can hide the one row the red badge names
  // behind the fold — a young stall is still the alarm, and a two-hour approval
  // wait is not. Within each group the longest wait leads: that is the one the
  // operator is asking about.
  const rank = (e: WaitEntry): number => (e.state === 'stalled' ? 0 : 1)
  out.sort((a, b) => rank(a) - rank(b) || b.ageSecs - a.ageSecs)
  return out
}

/* ── Rows (the Services plane's own label/value shape) ── */

interface Row {
  label: string
  value: React.ReactNode
  tip?: string
}

function SectionBlock({ title, rows }: { title: string; rows: Row[] }) {
  return (
    <div className="mb-4" style={{ breakInside: 'avoid' }}>
      <h4 className="text-[11.5px] font-semibold text-muted uppercase tracking-wide mb-2">{title}</h4>
      {rows.map(row => (
        <div
          key={row.label}
          className="flex justify-between gap-3 py-1.5 border-b border-border text-[12.5px] last:border-b-0"
        >
          <span className="text-muted shrink-0 inline-flex items-center gap-1">
            {row.label}
            {row.tip && <InfoTip text={row.tip} />}
          </span>
          <span className="text-text-strong font-mono tabular-nums text-right break-words">{row.value}</span>
        </div>
      ))}
    </div>
  )
}

/* ── Main component ── */

export default function TasksCapacityCard() {
  const { data, error, isError } = useQuery<TasksSummary>({
    queryKey: ['tasksSummary'],
    queryFn: () => api.tasksSummary(),
    refetchInterval: TASKS_SUMMARY_REFETCH_MS,
  })

  // The fold is the DEFAULT, not the ceiling: the card opens bounded and the
  // reader can reach the rest without leaving it.
  const [allWaits, setAllWaits] = useState(false)

  const d = data ?? null
  const depth = d?.depth
  const lanes = d ? Object.entries(d.lanes) : []
  const entries = d ? entriesOf(d) : []
  const foldable = entries.length > WAIT_ROWS_SHOWN
  const shown = allWaits ? entries : entries.slice(0, WAIT_ROWS_SHOWN)
  const hidden = entries.length - shown.length
  const health = d ? cardHealth(d) : null
  const degrade = d?.degrade_reason ?? null
  const degradeText = degrade ? degradeLabel(degrade) : null
  // Kept whole for the agent hand-off even where it is not prose a reader can
  // use: the journal recovers the endpoint and status from this exact string.
  let loadRaw = ''
  if (isError) loadRaw = error instanceof Error ? error.message : String(error)
  const loadReason = proseReason(loadRaw)

  const queueRows: Row[] = [
    // Every counter here names a set no other counter names, so no row is counted
    // twice on one card: `slotWaitingOf` is what takes the retry-wait rows out of
    // the wire's `depth.queued`, and the tip states that rule rather than leaving
    // the reader to reconcile two numbers with no tooltip open.
    { label: i18nT('pages.tasksCapacityCard.queued'),
      value: d ? fmtNumber(slotWaitingOf(d)) : '—',
      tip: i18nT('pages.tasksCapacityCard.queued_tip') },
    { label: i18nT('pages.tasksCapacityCard.running'), value: depth ? fmtNumber(depth.running) : '—' },
    // A task row and a live chat slot can both be parked mid-run, but
    // `depth.waiting` counts store rows only, so a session held on the
    // operator's approval was a list row under no counter at all. Counted off
    // `entries` — the array the list itself is built from — so the number and
    // the rows beneath it cannot disagree.
    { label: i18nT('pages.tasksCapacityCard.waiting'),
      value: d ? fmtNumber(entries.filter(e => e.state.startsWith('waiting_')).length) : '—',
      tip: i18nT('pages.tasksCapacityCard.waiting_tip') },
    { label: i18nT('pages.tasksCapacityCard.recovering'), value: depth ? fmtNumber(depth.recovering) : '—',
      tip: i18nT('pages.tasksCapacityCard.recovering_tip') },
    { label: i18nT('pages.tasksCapacityCard.oldest_wait'), value: d ? fmtAge(d.oldest_wait_secs) : '—',
      tip: i18nT('pages.tasksCapacityCard.oldest_wait_tip') },
  ]

  // The cause has ONE home, the closest to the number it explains: the lane line
  // when EXACTLY ONE lane is under its ceiling and this catalog can say why, else
  // the "Degrade reason" row. `degrade_reason` is one GLOBAL string covering the
  // whole card, so above one lowered lane no single number owns it and the row is
  // the only place it can be stated once.
  const loweredCount = lanes.filter(([, cap]) => isLowered(cap)).length
  const causeAtLane = degradeText != null && loweredCount === 1

  const laneRows: Row[] = lanes.length > 0
    ? lanes.map(([lane, cap]) => ({
      label: laneLabel(lane),
      tip: i18nT('pages.tasksCapacityCard.lane_tip'),
      // The cap in force leads every lane, and it is never muted: a lane at its
      // ceiling states it as the bare "Up to N at once", a lowered one states it
      // in the same line that says what it was lowered from. The muted lines
      // beneath carry the cause and the live count. The lane label can take half
      // a row a third of the card wide, so only the bare phrase is unbreakable —
      // a wrapped sentence still reads, whereas one that cannot wrap lands on top
      // of the column beside it.
      value: (
        <span className="inline-flex flex-col items-end leading-snug" data-testid="tasks-capacity-lane">
          {capLines(cap, causeAtLane ? degradeText : null).map(line => (
            <span
              key={line.text}
              className={line.nowrap ? 'whitespace-nowrap' : line.lead ? 'font-body' : 'text-muted font-body'}
            >
              {line.text}
            </span>
          ))}
        </span>
      ),
    }))
    : PLACEHOLDER_LANES.map(lane => ({ label: laneLabel(lane), value: '—' }))
  // So this row is absent when nothing is degraded — a labelled "none" beside a
  // red badge reads as a missing answer rather than as "nothing lowered this" —
  // and absent again when the one lowered lane already carries the sentence,
  // because the same cause in two places reads as two causes.
  if (degrade && !causeAtLane) {
    laneRows.push({
      label: i18nT('pages.tasksCapacityCard.degrade_reason'),
      // The badge beside the title already reads "Degraded" in the reader's own
      // language, so the explanation next to it cannot be a wire token. An
      // unmapped reason is machine text: mono, and `translate="no"`.
      value: (
        <span
          className={degradeText ? 'font-body' : undefined}
          style={{ color: 'var(--warn)' }}
          data-testid="tasks-capacity-degrade"
          translate={degradeText ? undefined : 'no'}
        >
          {degradeText ?? degrade}
        </span>
      ),
      tip: i18nT('pages.tasksCapacityCard.degrade_reason_tip'),
    })
  }

  const recovery = d?.recovering
  const recoveryRows: Row[] = [
    { label: i18nT('pages.tasksCapacityCard.task_retries'), value: recovery ? fmtNumber(recovery.task_attempts) : '—',
      tip: i18nT('pages.tasksCapacityCard.task_retries_tip') },
    { label: i18nT('pages.tasksCapacityCard.recovering_runs'), value: recovery ? fmtNumber(recovery.slots.length) : '—',
      tip: i18nT('pages.tasksCapacityCard.recovering_runs_tip') },
    // Counted off `entries`, like "Paused mid-run": the list is where a stall is
    // opened, so the alarm and the row it points at are read off one array.
    { label: i18nT('pages.tasksCapacityCard.stalled'),
      value: d ? fmtNumber(entries.filter(e => e.state === 'stalled').length) : '—',
      tip: d?.stall_after_secs != null
        ? i18nT('pages.tasksCapacityCard.stalled_tip', { age: fmtAge(d.stall_after_secs) })
        : undefined },
  ]

  return (
    <Card data-testid="tasks-capacity-card">
      <CardTitle>
        {i18nT('pages.tasksCapacityCard.title')}
        {/* Absent whenever `cardHealth` supports no verdict — a store-less gateway
            with nothing stalled or degraded. A green pill directly above the store
            notice reads as "the queue is fine" where the honest answer is "the
            queue is not recorded here". */}
        {health && (
          <Badge variant={health.variant} data-testid="tasks-capacity-health">
            {i18nT(HEALTH_LABEL_KEY[health.state])}
          </Badge>
        )}
      </CardTitle>
      {isError && (
        <ErrorNotice
          // The sentence carries itself when the failure has no readable reason;
          // the raw text still reaches the agent through the journal, so nothing
          // is lost by refusing to print it.
          message={loadReason ?? i18nT('pages.tasksCapacityCard.load_failed')}
          title={loadReason ? i18nT('pages.tasksCapacityCard.load_failed') : undefined}
          report={loadReason ? undefined : findReport(loadRaw)}
          askAgent
          className="mb-3"
          testId="tasks-capacity-error"
        />
      )}
      {d && !d.available && (
        <p className="text-[12.5px] text-muted mb-3">{i18nT('pages.tasksCapacityCard.no_store')}</p>
      )}
      <div className="columns-3 gap-6 max-[900px]:columns-2 max-[600px]:columns-1">
        <SectionBlock title={i18nT('pages.tasksCapacityCard.section_queue')} rows={queueRows} />
        <SectionBlock title={i18nT('pages.tasksCapacityCard.section_capacity')} rows={laneRows} />
        <SectionBlock title={i18nT('pages.tasksCapacityCard.section_recovery')} rows={recoveryRows} />
      </div>

      <h4 className="text-[11.5px] font-semibold text-muted uppercase tracking-wide mb-2 mt-1 flex items-center gap-1">
        {i18nT('pages.tasksCapacityCard.section_waits')}
        <InfoTip text={i18nT('pages.tasksCapacityCard.section_waits_tip')} />
      </h4>
      {d && shown.length === 0 && (
        <p className="text-[12.5px] text-muted py-1.5" data-testid="tasks-capacity-empty">
          {i18nT('pages.tasksCapacityCard.waits_empty')}
        </p>
      )}
      {shown.length > 0 && (
        <ul
          id="tasks-capacity-waits"
          className="m-0 p-0 list-none"
          aria-label={i18nT('pages.tasksCapacityCard.section_waits')}
        >
          {shown.map(entry => (
            <li
              key={entry.key}
              data-testid="tasks-capacity-wait-row"
              className="flex flex-col gap-1 py-1.5 border-b border-border text-[12.5px] last:border-b-0 sm:flex-row sm:items-center sm:gap-3"
            >
              {/* Narrow-first: three stacked lines (badge + id / reason / action +
                  attempts + age) so the reason keeps its full width on a phone,
                  where a truncated span has no hover to reveal it. From `sm` up the
                  two wrappers dissolve (`contents`) and everything sits on one row. */}
              <div className="flex items-center gap-2 min-w-0 sm:contents">
                <Badge variant={stateVariant(entry.state)}>{stateLabel(entry.state)}</Badge>
                {/* The id is a fact, never the control. Up to WAIT_ROWS_SHOWN
                    identically shaped mono ids sit in this list, so an accented
                    one carries no at-rest cue that it is the actionable row. */}
                <span className="font-mono text-text-strong truncate min-w-0 max-w-[220px]" title={entry.id}>
                  {entry.id}
                </span>
              </div>
              <span
                className="text-muted break-words sm:min-w-0 sm:flex-1 sm:truncate"
                data-testid="tasks-capacity-wait-reason"
                title={entry.reason || undefined}
              >
                {entry.reason}
              </span>
              <div className="flex items-center gap-3 sm:contents">
                {/* Every row names a session, so every row whose session HAS a
                    chat window offers a named way to reach it -- not a coloured
                    id. The card stays read-only: this only navigates, and
                    whatever lever the row needs (answer, approve, stop) lives in
                    that chat. */}
                {entry.href && (
                  <Link
                    to={entry.href}
                    className="text-accent hover:underline shrink-0 whitespace-nowrap"
                    data-testid="tasks-capacity-wait-link"
                  >
                    {i18nT('pages.tasksCapacityCard.open_chat')}
                  </Link>
                )}
                {entry.attempts > 1 && (
                  <span className="text-muted shrink-0">
                    {i18nT('pages.tasksCapacityCard.attempts', { count: fmtNumber(entry.attempts) })}
                  </span>
                )}
                {/* "5s so far", not a bare "5s": on a retry row the reason column
                    already carries a clock ("next retry in 1m 29s"), so the age
                    says which one it is. Not "for 5s": a value opening with a
                    connector word reads as half a sentence to the source-string
                    gate, and to a translator. */}
                <span
                  className="font-mono tabular-nums text-text-strong shrink-0"
                  data-testid="tasks-capacity-wait-age"
                  title={i18nT('pages.tasksCapacityCard.age_tip')}
                >
                  {i18nT('pages.tasksCapacityCard.age_for', { age: fmtAge(entry.ageSecs) })}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {/* A count of rows the reader cannot reach is a dead end, so the fold is a
          CONTROL: it names how many are held back and opens them in place. The
          default stays bounded, which is what the fold is for. */}
      {foldable && (
        <button
          type="button"
          className="mt-1.5 bg-transparent border-none p-0 text-[12.5px] text-accent hover:underline cursor-pointer"
          aria-expanded={allWaits}
          aria-controls="tasks-capacity-waits"
          onClick={() => setAllWaits(open => !open)}
          data-testid="tasks-capacity-fold"
        >
          {allWaits
            ? i18nT('pages.tasksCapacityCard.waits_show_fewer')
            : i18nT('pages.tasksCapacityCard.waits_show_all', { count: fmtNumber(hidden) })}
        </button>
      )}
    </Card>
  )
}
