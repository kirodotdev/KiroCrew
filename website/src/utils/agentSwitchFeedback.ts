import { i18nT } from '../i18n/t'
import { parseErrorCode } from './errorReport'
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
 * Whether *error* is the gateway's refusal to move a conversation that already
 * has messages onto a crewmate with its own memory (HTTP 409,
 * `member_memory_requires_new_conversation`). Same structural match as
 * `isTurnInFlightError`.
 */
export function isMemberNeedsNewChatError(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false
  const { status, body } = error as { status?: unknown; body?: unknown }
  return (
    status === 409
    && typeof body === 'string'
    && parseErrorCode(body) === 'member_memory_requires_new_conversation'
  )
}

/** What the shared switch notice shows. `newChatAgent` names a crewmate the
 *  notice offers to open a new chat with. `error` marks a failure or refused
 *  request, which the notice renders through ErrorNotice with its agent
 *  hand-off; without it the message is plain status text. */
export interface AgentSwitchNotice {
  message: string
  newChatAgent?: string
  error?: true
}

/** The notice for a failed agent, model, effort or setting write: the
 *  `agentSwitchFailureMessage` copy, marked as an error. */
export function agentSwitchErrorNotice(error: unknown): AgentSwitchNotice {
  return { message: agentSwitchFailureMessage(error), error: true }
}

/**
 * The notice for a failed pick of *name*. A crewmate refused because this chat
 * already has messages gets copy that names it, says the chat kept its agent,
 * and carries the crewmate so the notice can offer a new chat with it. The pick
 * was still refused, so the notice is an error. Any other failure is
 * `agentSwitchErrorNotice`.
 */
export function agentSwitchFailureNotice(error: unknown, name: string): AgentSwitchNotice {
  if (isMemberNeedsNewChatError(error) && name) {
    return {
      message: i18nT('utils.agentSwitchFeedback.member_needs_new_chat', { name }),
      newChatAgent: name,
      error: true,
    }
  }
  return agentSwitchErrorNotice(error)
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
 * The one-line notice for a model pick the gateway answered `deferred: true`:
 * a response was running, so the pick is committed and takes over from the
 * next message while the running reply finishes on the model it started with.
 * `model` is the stored id the response carries (`''` is Auto). Shown through
 * the same transient notice as a refused switch, so every picker surface
 * (menu, split pane, keyboard cycle) reports it the same way.
 */
export function modelSwitchDeferredMessage(model: string): string {
  return i18nT('utils.agentSwitchFeedback.model_deferred', {
    model: model || i18nT('components.jobForm.auto'),
  })
}
