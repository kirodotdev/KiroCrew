/**
 * The auto-nudge (goal loop / monitor) record as `GET /api/autonudge` and the
 * `autonudge_state` websocket frame deliver it, plus the readouts built on it:
 * the compact cycle counter the composer's goal chip wears, the interval, and
 * the deadline-preserving next-fire reading that both the chip and the Crew
 * Members drawer word from one rule. Kept together so the record's shape and
 * the meaning of its timestamps live beside each other.
 */
import { i18nT } from '../i18n/t'
import { fmtDuration } from '../i18n/format'

export interface AutoNudgeLoop {
  id: string
  slot_key: string
  message: string
  idle_secs: number
  max_cycles: number
  cycle_count: number
  active: boolean
  last_fire_ts: number
  /** Absolute wall-clock deadline for the next fire; 0 = not yet scheduled.
   *  Already serialized by the backend's `asdict(loop)` — the field simply
   *  was not surfaced here before (#6482). */
  next_due_ts: number
  /** Why the loop last went inactive: '' while active or never stopped,
   *  otherwise one of the service's terminal codes (`cycle_cap`,
   *  `runtime_budget`, `approval_stalled`, `autonudge_stop`, `manual`). Only
   *  the REST list carries it; the websocket frame for a plain loop does not,
   *  so a consumer merging frames over a fetched record must keep it. */
  stopped_reason?: string
  /** Short stand-in for `message` in the visible transcript row; '' = none. */
  banner?: string
}

/** `GET /api/autonudge`: every prompt-loop record the service holds, active or
 *  stopped. (The module spec reserves an optional `denied` array beside
 *  `loops` for refused arms; no backend emits it yet, so it is deliberately
 *  not typed here — a consumer must not render a verdict nothing produces.) */
export interface AutoNudgeListResponse {
  enabled: boolean
  loops: AutoNudgeLoop[]
}

/** React Query key for the whole loop registry (`GET /api/autonudge`). The
 *  websocket hook invalidates it on every `autonudge_state` frame and on every
 *  (re)connect, so any reader of this key is live without its own listener. */
export const AUTONUDGE_LOOPS_QUERY_KEY = ['autonudge-loops'] as const

/** Cycle readout: "3/24" when a finite cap is armed, and a bare "3" when
 *  max_cycles is 0, which means infinite -- a loop with no backstop has no
 *  denominator to count toward. Safe in aria-label: it changes once per
 *  cycle, not once per second. */
export function cycleText(loop: Pick<AutoNudgeLoop, 'cycle_count' | 'max_cycles'> | null | undefined): string {
  if (loop?.max_cycles && loop.max_cycles > 0) return `${loop.cycle_count}/${loop.max_cycles}`
  return String(loop?.cycle_count ?? 0)
}

/** Split whole seconds into the coarse parts a duration readout wants: hours +
 *  minutes above an hour, minutes + seconds below it. Above an hour the seconds
 *  digit is noise; below it, keeping the tick visible reads as live. */
function durationParts(totalSecs: number): Array<[number, 'hour' | 'minute' | 'second']> {
  const secs = Math.max(0, Math.round(totalSecs))
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = secs % 60
  return h > 0 ? [[h, 'hour'], [m, 'minute']] : [[m, 'minute'], [s, 'second']]
}

/** Human-readable interval between nudges, e.g. "20 min" or "1 hr 30 min". */
export function intervalText(idleSecs: number): string {
  return fmtDuration(durationParts(idleSecs), { dropZero: true })
}

/** The next-fire reading as data, so each surface can word it for its own
 *  layout: the popover says "Next cycle in 13m 57s" on one line, the members
 *  drawer already has a "Next wake" label and wants only "in 13m 57s". */
export type NextCycle =
  | { kind: 'none' }
  | { kind: 'unscheduled' }
  | { kind: 'due' }
  | { kind: 'in'; time: string }

/**
 * Semantics: the loop is deadline-preserving -- a user turn defers a due fire
 * until the turn ends but never pushes the deadline back -- so an elapsed
 * deadline reads "due, fires after the current turn" rather than a negative
 * countdown. next_due_ts of 0 means the next arm has not scheduled yet.
 * next_due_ts is a SERVER wall-clock deadline rendered against the CLIENT
 * clock; skew shifts the countdown by that skew, and the due-fallback bounds
 * the visible damage.
 */
export function nextCycle(loop: AutoNudgeLoop | null | undefined, nowTs: number): NextCycle {
  if (!loop?.active) return { kind: 'none' }
  if (!(loop.next_due_ts > 0)) return { kind: 'unscheduled' }
  const remaining = Math.round(loop.next_due_ts - nowTs)
  if (remaining <= 0) return { kind: 'due' }
  return { kind: 'in', time: fmtDuration(durationParts(remaining), { dropZero: true }) }
}

/** Line for the next trigger, or '' when no active loop. */
export function nextCycleText(loop: AutoNudgeLoop | null | undefined, nowTs: number): string {
  const next = nextCycle(loop, nowTs)
  switch (next.kind) {
    case 'none':
      return ''
    case 'unscheduled':
      return i18nT('components.autoNudgePopover.next_cycle_unscheduled')
    case 'due':
      return i18nT('components.autoNudgePopover.next_cycle_due')
    case 'in':
      return i18nT('components.autoNudgePopover.next_cycle_in', { time: next.time })
  }
}
