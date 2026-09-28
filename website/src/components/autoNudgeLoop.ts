/**
 * The auto-nudge (goal loop / monitor) record as `GET /api/autonudge` and the
 * `autonudge_state` websocket frame deliver it, plus the readouts built on it:
 * the compact cycle counter the composer's goal chip wears, the interval, and
 * the deadline-preserving next-fire reading that both the chip and the Crew
 * Members drawer word from one rule. Kept together so the record's shape and
 * the meaning of its timestamps live beside each other.
 */
import { i18nT } from '../i18n/t'
import { fmtDuration, fmtTimeNumeric } from '../i18n/format'

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
  /** The kill-switch file the server substitutes for `{{STOP_FILE}}` at fire
   *  time; '' when the loop was armed with none. Carried by the REST list and
   *  the arm/update responses (`asdict(loop)`), NOT by the websocket frame,
   *  which is broadcast without an owner gate and withholds filesystem paths.
   *  So `undefined` means "not known here", while '' is a real "no sentinel". */
  stop_sentinel_path?: string
  /** The wake judge's brief, as the owner armed it; absent or `{}` = no judge.
   *  The two sentences are the owner's own words about their own loop, so the
   *  popover shows them back rather than making the owner reopen the tool call to
   *  remember what a loop is screening on. */
  judge?: { wake_when?: string; quiet_when?: string; targets?: string[] }
  /** The last verdict, deliberately text-free: an outcome, how many evidence
   *  items it was based on, and when. It carries NO transcript text and no
   *  per-answer probability; those live in the decisions log, which is where the
   *  thresholds are meant to be tuned from. */
  judge_last_verdict?: { outcome?: string; evidence_items?: number; at?: number }
}

/** `GET /api/autonudge`: every loop record the service holds, active or stopped.
 *  A STRUCTURED MONITOR is included as a REDUCED row -- that route has no owner
 *  gate, so it publishes only presence, cadence and state, and withholds
 *  `message`, `banner`, the sentinel path and the cycle accounting, which its
 *  tick path never maintains. The fields below are therefore absent on such a
 *  row even though they are typed as required; marking them optional belongs
 *  with the popover rendering that reads them. Such a row also carries no
 *  positive marker: it is told apart by that absence. The full monitor record
 *  lives on the owner-gated `/api/monitors`. (The module spec reserves an optional `denied` array beside
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

/** The judge line as DATA, so each surface words it for its own layout -- the
 *  same split `nextCycle` uses, and for the same reason: the popover has room for
 *  the brief, a compact row may want only the last reading.
 *
 *  `kind: 'none'` is a loop with no judge, which is every loop by default. A
 *  `verdict` of undefined is a judge that has not answered yet, which is not the
 *  same thing and must not read as one: the first is "this loop fires on a timer",
 *  the second is "it will be screened, starting next cycle". */
export type JudgeReading =
  | { kind: 'none' }
  | { kind: 'armed'; sense: 'wake' | 'quiet'; criterion: string; verdict?: JudgeVerdict }

export interface JudgeVerdict {
  /** The outcome word as the point spells it, e.g. 'quiet', 'progress_only'. */
  outcome: string
  /** How many evidence items the answer was based on. */
  items: number
  /** When it was answered, epoch seconds; 0 when the record carried no time. */
  at: number
}

/**
 * Read one loop's judge state.
 *
 * A brief with neither sentence reads as `none`, and that is what a loop stores when
 * its owner named no criteria of their own. Such a loop may still be SCREENED, under
 * the default brief the gateway supplies per tick, which is never written back to the
 * record — so this reading is "does the owner have a criterion here", not "is a judge
 * running". The row is the owner's own sentence or nothing; the per-tick transcript
 * notice is where a verdict reached under the default is reported, and it names which
 * brief it used.
 */
export function judgeReading(loop: AutoNudgeLoop | null | undefined): JudgeReading {
  const wakeWhen = (loop?.judge?.wake_when ?? '').trim()
  const quietWhen = (loop?.judge?.quiet_when ?? '').trim()
  if (!wakeWhen && !quietWhen) return { kind: 'none' }
  const raw = loop?.judge_last_verdict
  const outcome = (raw?.outcome ?? '').trim()
  // No outcome means no answer yet. The item count alone is not enough to call it
  // one: a verdict is identified by what it decided, and a 0-item tick is a real
  // answer the judge gave on nothing.
  const verdict: JudgeVerdict | undefined = outcome
    ? { outcome, items: Math.max(0, Math.trunc(raw?.evidence_items ?? 0)), at: raw?.at ?? 0 }
    : undefined
  // Which SENTENCE the criterion is, not just its text. A brief may carry either one,
  // and they say opposite things: printing a `quiet_when` under a "wake when" label
  // tells the owner the inverse of what they armed, on every visit. `wake_when` wins
  // when both are present, because a wake condition is the one that costs a turn.
  return {
    kind: 'armed',
    sense: wakeWhen ? 'wake' : 'quiet',
    criterion: wakeWhen || quietWhen,
    verdict,
  }
}

/** The verdict's clock reading, e.g. "12:30", in the reader's own zone. */
export function judgeVerdictTime(at: number): string {
  // The SAME formatter the last-fire line one row above uses. A hand-rolled UTC
  // clock here put two times in one block that disagree by the reader's offset,
  // every visit, and spelled the zone as a bare `Z` that only names itself to
  // someone who already knows it.
  if (!at || at <= 0) return ''
  return fmtTimeNumeric(at)
}
