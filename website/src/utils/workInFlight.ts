/**
 * What the gateway says when a restart would cut running work.
 *
 * `POST /api/restart` with `{force: false}` answers `409 {code: "work_in_flight"}`
 * with the counts and, by name, the first holders of that work (an older
 * gateway's 409 has no holders). Nothing started and nothing was cut; the
 * person is shown what is running and decides.
 * `force: true` is sent only after they confirm (see `api/client/updates.ts`,
 * the one place that asks).
 */
import { ApiError } from '../api/apiError'
import { fmtElapsed, fmtList, fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'

/** One thing a restart would cut, as the gateway names it. */
export interface WorkHolder {
  kind: string
  /** A slot key or a cron job name; empty for work the gateway counts unnamed. */
  name: string
  /** How long it has run, or `null` where no start was recorded. */
  sinceSecs: number | null
  /** The items behind an unnamed holder. */
  count: number
}

export interface WorkInFlight {
  /** Work items an immediate restart would cut. */
  busy: number
  /** Seconds of chat quiet still owed. */
  quietRemainingSecs: number
  /** The longest-running holders the gateway named, and how many it left out. */
  holders: WorkHolder[]
  holdersMore: number
}

const wholeNumber = (v: unknown): number =>
  typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : 0

function asHolder(entry: unknown): WorkHolder[] {
  if (typeof entry !== 'object' || entry === null) return []
  const e = entry as Record<string, unknown>
  if (typeof e.kind !== 'string') return []
  const since = e.since_secs
  return [{
    kind: e.kind,
    name: typeof e.name === 'string' ? e.name : '',
    sinceSecs: typeof since === 'number' && Number.isFinite(since) && since >= 0 ? Math.floor(since) : null,
    count: Math.max(1, wholeNumber(e.count)),
  }]
}

const HOLDER_KIND_KEY = {
  turn: 'components.updateFoundModal.work_kind_turn',
  script: 'components.updateFoundModal.work_kind_script',
  workflow: 'components.updateFoundModal.work_kind_workflow',
  subagent: 'components.updateFoundModal.work_kind_subagent',
  channel_handler: 'components.updateFoundModal.work_kind_channel_handler',
  queued: 'components.updateFoundModal.work_kind_queued',
  approval: 'components.updateFoundModal.work_kind_approval',
  unreadable: 'components.updateFoundModal.work_kind_unreadable',
  callback: 'components.updateFoundModal.work_kind_callback',
  refusal_write: 'components.updateFoundModal.work_kind_refusal_write',
  update_build: 'components.updateFoundModal.work_kind_update_build',
  task_runner: 'components.updateFoundModal.work_kind_task_runner',
} as const

function kindLabel(kind: string): string {
  return Object.hasOwn(HOLDER_KIND_KEY, kind)
    ? i18nT(HOLDER_KIND_KEY[kind as keyof typeof HOLDER_KIND_KEY])
    : i18nT('components.updateFoundModal.work_kind_other')
}

/** `nightly-sync (scheduled job, 21s)`, `sub-agent ×3`: one holder as the dialog lists it. */
function describeHolder(h: WorkHolder): string {
  const kind = kindLabel(h.kind)
  // An unreadable source's name is the gateway's own label for it: the kind says enough.
  if (h.kind === 'unreadable') return kind
  const age = h.sinceSecs === null ? '' : fmtElapsed(h.sinceSecs * 1000, { wholeSeconds: true })
  if (h.name) {
    return age
      ? i18nT('components.updateFoundModal.work_holder_named_age', { name: h.name, kind, age })
      : i18nT('components.updateFoundModal.work_holder_named', { name: h.name, kind })
  }
  if (h.count > 1) return i18nT('components.updateFoundModal.work_holder_times', { kind, n: fmtNumber(h.count) })
  return age ? i18nT('components.updateFoundModal.work_holder_age', { kind, age }) : kind
}

/** The running work an answer reports, or `null` for any other answer. */
export function asWorkInFlight(answer: unknown): WorkInFlight | null {
  if (typeof answer !== 'object' || answer === null) return null
  const a = answer as Record<string, unknown>
  if (a.status !== 'deferred' && a.code !== 'work_in_flight') return null
  return {
    busy: wholeNumber(a.busy),
    quietRemainingSecs: Math.max(wholeNumber(a.quiet_remaining_secs), wholeNumber(a.settle_remaining_secs)),
    holders: Array.isArray(a.holders) ? a.holders.flatMap(asHolder) : [],
    holdersMore: wholeNumber(a.holders_more),
  }
}

/** The sentences that say what a restart now would interrupt. */
export function workInFlightNotice(w: WorkInFlight): string {
  if (w.busy > 0) {
    const busy = i18nT('components.updateFoundModal.work_running_busy', { active: w.busy })
    if (w.holders.length === 0) return busy
    const listed = w.holders.map(describeHolder)
    if (w.holdersMore > 0) {
      listed.push(i18nT('components.updateFoundModal.work_holders_more', { count: w.holdersMore, n: fmtNumber(w.holdersMore) }))
    }
    // On its own line, under the count.
    return [busy, i18nT('components.updateFoundModal.work_running_now', { holders: fmtList(listed) })].join('\n')
  }
  return i18nT('components.updateFoundModal.work_running_quiet', {
    minutes: Math.max(1, Math.ceil(w.quietRemainingSecs / 60)),
  })
}

/**
 * Thrown when the person declines the confirmation. An `ApiError` so a caller
 * that already shows a server refusal shows this too; a surface that wants a
 * quiet cancel checks `instanceof WorkConfirmDeclined`.
 */
export class WorkConfirmDeclined extends ApiError {
  constructor(message: string) {
    super(409, message)
    this.name = 'WorkConfirmDeclined'
  }
}
