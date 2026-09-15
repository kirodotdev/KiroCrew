import { i18nT } from '../i18n/t'
import { findReport, parseErrorCode, recentErrors, type ErrorReport } from './errorReport'
import { errMessage } from './thunkError'

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
 * True for the 503 a switch answers when the configured workspace root cannot be
 * resolved. Detected here rather than left to the generic path because that path
 * prefers the API layer's message, which for this one is the backend's own English
 * prose -- unlocalized, on a localized page.
 */
export function isWorkspaceUnavailableError(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false
  const { status, body } = error as { status?: unknown; body?: unknown }
  return (
    status === 503
    && typeof body === 'string'
    && parseErrorCode(body) === 'workspace_unavailable'
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
  if (isWorkspaceUnavailableError(error)) {
    return i18nT('utils.agentSwitchFeedback.workspace_unavailable')
  }
  // `errMessage` owns reading a message off a rejection object, including the
  // plain serialized form a Redux Toolkit thunk boundary produces, so this stops
  // hand-rolling that cast and cannot drift from the other readers of the shape.
  //
  // The `typeof error === 'object'` guard is kept deliberately and is NOT
  // redundant: `errMessage` also reads a thrown PRIMITIVE as its own message,
  // which is right for a surface rendering a raw failure string but wrong here.
  // On this one a bare `'offline'` is an internal artifact rather than copy a
  // user should be shown, so it must still fall through to the generic line --
  // see the "no usable text" case in `agentSwitchFeedback.test.ts`.
  const message = typeof error === 'object' && error !== null ? errMessage(error) : ''
  if (message.trim()) return message
  return i18nT('components.errorBoundary.something_went_wrong')
}

/**
 * Whether a hand-off to the agent helps with this refusal.
 *
 * Only the unavailable workspace: that one needs someone to look at the machine, so a fresh
 * chat is the remedy. A turn already in flight clears itself, and the hand-off would create
 * and activate a NEW session — moving the user off the very turn the notice told them to
 * wait for, to ask about a condition that is already resolving. Keyed on the copy this
 * module produced, so it cannot disagree with the branch that chose it.
 */
export function agentSwitchOffersHandoff(message?: string | null): boolean {
  return !!message && message === i18nT('utils.agentSwitchFeedback.workspace_unavailable')
}

/**
 * Whether the switch was WITHHELD rather than broken -- `ErrorNotice`'s `warn` axis.
 *
 * True for the two recognized refusals only. Every other outcome this module reports is a
 * genuine failure -- a rejected agent name, a missing slot, a network error, the generic
 * fallback -- and must keep the danger default, because `warn` renders `role="status"` and a
 * CircleAlert, which announces a real breakage as a polite notice.
 *
 * Keyed on the copy this module produced, so it cannot disagree with the branch that chose it.
 */
export function agentSwitchWasWithheld(message?: string | null): boolean {
  if (!message) return false
  return (
    message === i18nT('utils.agentSwitchFeedback.workspace_unavailable')
    || message === i18nT('utils.agentSwitchFeedback.turn_in_flight')
  )
}

/** Newest journal entry carrying a status+code pair, or nothing if the failure never reached it. */
function codedReport(status: number, code: string): ErrorReport | undefined {
  // `recentErrors` is newest-first, so the first match is the live failure rather than a
  // same-coded retry from earlier in the session.
  return recentErrors().find(r => r.status === status && r.code === code)
}

/**
 * The structured report behind a switch-failure notice.
 *
 * `ErrorNotice` recovers an error's context (route, endpoint, status, backend `code`) by looking
 * the journal up ON THE MESSAGE, and for the two refusals named above that lookup CANNOT hit:
 * both deliberately replace the backend's own English prose with localized copy, so the string
 * the notice carries is not the string the API layer journaled. Nothing errors -- the hand-off
 * simply arrives carrying only a sentence, stripped of the `code` that made it actionable, which
 * for the unavailable workspace is the entire reason it offers a hand-off at all.
 *
 * So a coded refusal is resolved on the WIRE CONTRACT instead: the status+`code` pair, the same
 * one `isWorkspaceUnavailableError` matches and for the same stated reason -- prose can be
 * reworded, a code cannot. Keyed on the copy this module produced, so it cannot disagree with the
 * branch that chose it. Every other failure keeps the message lookup, which is exact there
 * because that path passes the API layer's own message through untouched.
 *
 * Both coded refusals are resolved, not just the one that currently offers a hand-off: the pairing
 * of a localized message with a journal keyed on prose is what breaks the lookup, so any refusal
 * that later opts into a hand-off would inherit the same silent gap.
 */
export function agentSwitchFailureReport(message?: string | null): ErrorReport | undefined {
  if (!message) return undefined
  if (message === i18nT('utils.agentSwitchFeedback.workspace_unavailable')) {
    return codedReport(503, 'workspace_unavailable')
  }
  if (message === i18nT('utils.agentSwitchFeedback.turn_in_flight')) {
    return codedReport(409, 'turn_in_flight')
  }
  return findReport(message)
}
