import { i18nT } from '../i18n/t'
import { parseErrorCode } from './errorReport'

/**
 * Whether *error* is the gateway's "a turn is in flight" refusal (HTTP 409,
 * machine-readable code `turn_in_flight`).
 *
 * Matched on the structured code, never on the human message: the message is
 * server-authored English prose that can be reworded, while the code is the
 * wire contract. Status is checked too so a hypothetical non-409 reuse of the
 * code word elsewhere cannot trip the busy copy.
 *
 * The shape check is STRUCTURAL (`status` + `body`, the fields `ApiError`
 * carries) rather than `instanceof ApiError`, deliberately: the switch
 * surfaces' tests replace the `../api/client` module wholesale, and an
 * `instanceof` against a class the mock does not export throws inside the
 * failure handler — turning every switch failure into an unhandled error.
 */
export function isTurnInFlightError(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false
  const { status, body } = error as { status?: unknown; body?: unknown }
  return (
    status === 409
    && typeof body === 'string'
    && parseErrorCode(body) === 'turn_in_flight'
  )
}

/**
 * Convert an agent-switch failure into copy the chat surface can show.
 *
 * A `turn_in_flight` 409 gets its own copy first: the dropdown picker is
 * disabled mid-turn, but the Alt+Shift keyboard cycles have no disabled state
 * to gray out, so this message is the only place the user learns WHY the
 * switch was refused and that retrying after the turn ends will work. Mapping
 * it here (the one chokepoint every switch surface routes failures through)
 * keeps the dropdown, all four cycle handlers, and future callers in step.
 *
 * Otherwise prefers the message the API layer already produced, because it is
 * the only part of the failure that carries anything specific — the endpoint
 * answers a bad agent name and a missing slot differently, and both are more
 * useful than a generic string. Falls back to the shared unexpected-error copy
 * when the rejection carries no usable message, so a non-Error throw still
 * surfaces.
 */
export function agentSwitchFailureMessage(error: unknown): string {
  if (isTurnInFlightError(error)) {
    return i18nT('utils.agentSwitchFeedback.turn_in_flight')
  }
  const message = typeof error === 'object' && error !== null
    ? (error as { message?: unknown }).message
    : null
  if (typeof message === 'string' && message.trim()) return message
  return i18nT('components.errorBoundary.something_went_wrong')
}
