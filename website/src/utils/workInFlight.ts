/**
 * What the gateway says when a restart would cut running work.
 *
 * Two answers carry it, with the same counts: `POST /api/update` answers
 * `200 {status: "deferred"}`, and `POST /api/restart` with `{force: false}`
 * answers `409 {code: "work_in_flight"}`. Nothing started and nothing was cut;
 * the person is shown what is running and decides. `force: true` is sent only
 * after they confirm (see `api/client/updates.ts`, the one place that asks).
 */
import { ApiError } from '../api/apiError'
import { i18nT } from '../i18n/t'

export interface WorkInFlight {
  /** Work items an immediate restart would cut. */
  busy: number
  /** Seconds of chat quiet still owed. */
  quietRemainingSecs: number
}

const wholeNumber = (v: unknown): number =>
  typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : 0

/** The running work an answer reports, or `null` for any other answer. */
export function asWorkInFlight(answer: unknown): WorkInFlight | null {
  if (typeof answer !== 'object' || answer === null) return null
  const a = answer as Record<string, unknown>
  if (a.status !== 'deferred' && a.code !== 'work_in_flight') return null
  return {
    busy: wholeNumber(a.busy),
    quietRemainingSecs: Math.max(wholeNumber(a.quiet_remaining_secs), wholeNumber(a.settle_remaining_secs)),
  }
}

/** The sentence that says what a restart now would interrupt. */
export function workInFlightNotice(w: WorkInFlight): string {
  if (w.busy > 0) return i18nT('components.updateFoundModal.work_running_busy', { active: w.busy })
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
