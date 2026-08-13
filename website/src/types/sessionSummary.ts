/**
 * Shapes returned by `GET /api/chat/slots/{slot}/summary`.
 *
 * Mirrors the payload written by the backend's session-summary pass — see
 * `docs/system-specs/modules/session-summary.md`. Kept in its own module so the
 * panel and the api client share one definition rather than two that drift.
 */

/** What the panel renders as a single word in an intent's header.
 *
 *  Derived server-side from the two stored axes (progress status + verification)
 *  so both surfaces agree by construction. `needs-you` is the one that earns the
 *  split: it means the discussion ended while the goal was never actually
 *  reached — diagnosed but not fixed, merged but never run. */
export type IntentState = 'done' | 'needs-you' | 'in-progress' | 'dropped'

export interface IntentNextStep {
  /** The action. */
  what: string
  /** Why it matters — present because deciding is the effortful part of re-entry. */
  why: string
  /** What to expect if they do it. Secondary: rendered visually subordinate. */
  expect: string
}

export interface SessionIntent {
  title: string
  /** Why the work started, in the person's own terms. */
  initial_intent: string
  /** What is true now — a runbook, not a turn-by-turn history. */
  progress: string[]
  /** The summarizer's INFERENCES, not things the person asked for. Rendered
   *  behind an accent rule so the distinction is visible. */
  next_steps: IntentNextStep[]
  /** `[firstUserTurn, lastUserTurn]` pairs. A LIST, and pairs may overlap
   *  another intent's: an intent can go dormant and resume, and one intent can
   *  sit inside another's span. */
  ranges: [number, number][]
  status: 'active' | 'completed' | 'abandoned'
  /** Independent of `status`. `false` means the outcome was never confirmed. */
  verified: boolean | null
  state: IntentState
  /** Highest turn in `ranges`. What the panel sorts on, descending. */
  last_touched_turn: number
  /** The turn that triggered this intent — rendered as provenance, because a
   *  question that caused a goal is not itself a goal. */
  origin_turn: number | null
}

/** Which on-demand affordance the panel offers, decided server-side so the
 *  frontend never re-implements the turn threshold.
 *
 *  `ready` — offer the button. `too_few_turns` — say so and offer nothing, since
 *  a click could only fail. `unavailable` — the feature is off, a pass is already
 *  running, or the session is incognito and must not leave a durable artifact. */
export type SummaryGenerateState = 'ready' | 'too_few_turns' | 'unavailable'

export interface SessionSummary {
  /** False when the feature is switched off. The panel explains itself rather
   *  than erroring, because the Settings toggle ships separately. */
  enabled: boolean
  /** A summary exists but the transcript has moved on since. Shown rather than
   *  withheld: an empty panel reads as "broken", a stale one reads as "not
   *  regenerated yet", which is the truth. */
  stale: boolean
  intents: SessionIntent[]
  /** Recurring operational facts about this project — the things you would
   *  otherwise re-learn the hard way. Capped server-side. */
  constraints: string[]
  generated_at: number | null
  user_turns: number | null
  last_activity: string | null
  /** Server's verdict on the on-demand affordance. Optional so a panel talking
   *  to a gateway that predates the POST route degrades to the read-only
   *  behaviour instead of rendering a button the backend cannot serve. */
  generate_state?: SummaryGenerateState
}

/** An open item hoisted out of an intent into the triage block at the top.
 *
 *  Carries its source intent's title so hoisting does not sever it from context. */
export interface TriageItem extends IntentNextStep {
  fromIntent: string
}

/** Collect what needs the person, across every intent.
 *
 *  Two sources, in this order: an intent whose derived state is `needs-you`
 *  (completed but unverified — the case a reader forgets), then the open next
 *  steps of the most recently touched intents. Ordering follows the intent
 *  ordering, so the block never disagrees with the list below it. */
/** How many open items the block RENDERS. The count beside the heading is
 *  deliberately NOT capped by this — a chip reading "3 open items" for a session
 *  with seven of them understates exactly the busiest sessions, and the reader
 *  takes the block as the whole answer to "does this need me?". Cap the list,
 *  count the truth, and say how many are hidden. */
export const TRIAGE_VISIBLE = 3

export function collectTriage(
  intents: SessionIntent[],
  limit: number = TRIAGE_VISIBLE,
): TriageItem[] {
  const out: TriageItem[] = []
  for (const intent of intents) {
    if (intent.state !== 'needs-you') continue
    for (const step of intent.next_steps) {
      out.push({ ...step, fromIntent: intent.title })
      if (out.length >= limit) return out
    }
  }
  for (const intent of intents) {
    if (intent.state === 'needs-you' || intent.state === 'dropped') continue
    for (const step of intent.next_steps) {
      out.push({ ...step, fromIntent: intent.title })
      if (out.length >= limit) return out
    }
  }
  return out
}

/** Render a range list as the panel shows it: `turns 1–14, 77–100`. */
export function formatRanges(ranges: [number, number][]): string {
  return ranges
    .map(([a, b]) => (a === b ? String(a) : `${a}–${b}`))
    .join(', ')
}

/** True when an intent was returned to after a gap — more than one range.
 *
 *  Surfaced rather than flattened: repetition is signal. A goal returned to ten
 *  times is the strongest thing a summary can tell you about a session. */
export function resumptionCount(intent: SessionIntent): number {
  return Math.max(0, intent.ranges.length - 1)
}
