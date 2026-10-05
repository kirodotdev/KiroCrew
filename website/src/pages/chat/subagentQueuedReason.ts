/**
 * Why a slot's queued subagents are waiting -- the `reason` the gateway puts on
 * `subagent_queued` beside the count.
 *
 * The count alone made every chip say "queued behind the concurrency limit",
 * including for a wave the MEMORY guard parked: on a 16 GB laptop that read as
 * "cap 4, queue 1, blocked by the cap" and never cleared, while the real cause
 * sat only in the gateway log. The gate now labels the wait it decided on and
 * the chips render the label. Nothing here decides anything: it reads a field
 * the backend already emitted.
 *
 * Backward compatible by construction: an event with no `reason` (an older
 * gateway) parses to `undefined`, and `queuedWaitText` then returns `null`, so
 * every chip falls back to the text it showed before. The `concurrency_limit`
 * kind ALSO resolves to `null` on purpose -- it is the ordinary wave shape and
 * the existing text already describes it.
 */
import { fmtUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import backendPhrases from '../../lib/backendPhrases.json'

/** The gate's kinds. `concurrency_limit` clears on its own within seconds; the
 *  others can wait possibly for hours. `memory_pressure` is the macOS kernel's
 *  pressure verdict and carries no GB figures: the free-memory figure cleared
 *  the floor, so numbers would contradict it. The memory posture tier is not a
 *  spawn wait: spawns admit on the floor alone. Nor is a paused execution cap:
 *  the adaptive controller no longer pauses it, so an older gateway's
 *  `adaptive_cap_zero` label falls back to the default text like any unknown
 *  kind. */
const KINDS = [
  'concurrency_limit',
  'low_memory',
  'memory_pressure',
] as const

export type SubagentQueuedReasonKind = (typeof KINDS)[number]

export type SubagentQueuedReason = {
  reason: SubagentQueuedReasonKind
  /** Reclaimable host memory the gate measured, in GB (`low_memory` only;
   *  `memory_pressure` carries no figures). */
  available_gb?: number
  /** The bar that measurement fell short of, in GB (`low_memory` only). */
  required_gb?: number
}

/** The `subagent_queued` WS payload: the count every gateway sends, plus the
 *  optional label a current gateway adds. */
export type SubagentQueuedEvent = {
  slot: string
  queued: number
  reason?: string
  available_gb?: number
  required_gb?: number
}

const KIND_SET: ReadonlySet<string> = new Set<string>(KINDS)

const finiteOrUndefined = (v: unknown): number | undefined =>
  typeof v === 'number' && Number.isFinite(v) ? v : undefined

/** The typed label carried by an event, or `undefined` when the event carries
 *  none the UI knows how to render (older gateway, unknown kind). */
export function parseSubagentQueuedReason(ev: SubagentQueuedEvent): SubagentQueuedReason | undefined {
  if (typeof ev.reason !== 'string' || !KIND_SET.has(ev.reason)) return undefined
  const out: SubagentQueuedReason = { reason: ev.reason as SubagentQueuedReasonKind }
  const available = finiteOrUndefined(ev.available_gb)
  const required = finiteOrUndefined(ev.required_gb)
  if (available !== undefined) out.available_gb = available
  if (required !== undefined) out.required_gb = required
  return out
}

const gb = (v: number): string => fmtUnit(v, 'gigabyte', { maximumFractionDigits: 1 })

/**
 * The sentence the chips show for a labelled wait, or `null` when the chip
 * should keep its own default text (no label, or the concurrency kind).
 *
 * Written for the person looking at the chip, not for the code: each sentence
 * names what is short and what to do about it, never the gate or the
 * controller. A memory event that arrived without its figures (a gateway that
 * labels the kind but not the numbers) gets the figure-less sentence rather
 * than a placeholder in a number's place.
 *
 * Lower-case after the count on purpose: the Activity banner renders it as
 * `{count} {text}`, which is the shape its existing default already has.
 */
export function queuedWaitText(reason: SubagentQueuedReason | undefined): string | null {
  switch (reason?.reason) {
    case 'low_memory':
      return reason.required_gb !== undefined && reason.available_gb !== undefined
        ? i18nT('pages.chat.subagentQueued.low_memory', {
          required: gb(reason.required_gb),
          available: gb(reason.available_gb),
        })
        : i18nT('pages.chat.subagentQueued.low_memory_no_figures')
    case 'memory_pressure':
      return i18nT('pages.chat.subagentQueued.memory_pressure')
    default:
      return null
  }
}

/** The opening words of the gate's terminal error for a start its macOS
 *  memory-pressure hold ended (`MEMORY_PRESSURE_NEVER_STARTED`, backend). A run
 *  carrying it launched nothing, so a grouped header must not call it
 *  "finished".
 *
 *  It lives in `lib/backendPhrases.json`, not as a literal here, because it is
 *  backend prose this UI MATCHES rather than copy it renders: the i18n gate reads
 *  a string literal in a `.ts` file as a user-visible string to translate, which
 *  this must never be. `test_the_card_and_the_gate_agree_on_the_never_started_prefix`
 *  pins it against the Python constant. */
export const NEVER_STARTED_PREFIX = backendPhrases.neverStartedPrefix

/** Whether a terminal run's error says it never started. */
export function isNeverStarted(error: string | undefined): boolean {
  return typeof error === 'string' && error.startsWith(NEVER_STARTED_PREFIX)
}

/** Whether the wait is on host memory: the gate deferred the start until enough
 *  memory is free (by its own measurement or the macOS pressure verdict), rather
 *  than holding it behind the concurrency cap. The adaptive pause is left out
 *  because its own sentence names overload as well. */
export function isMemoryWait(reason: SubagentQueuedReason | undefined): boolean {
  return reason?.reason === 'low_memory' || reason?.reason === 'memory_pressure'
}
