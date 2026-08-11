import { i18nT } from '../i18n/t'

/**
 * Convert an agent-switch failure into copy the chat surface can show.
 *
 * Prefers the message the API layer already produced, because it is the only
 * part of the failure that carries anything specific — the endpoint answers a
 * bad agent name and a missing slot differently, and both are more useful than
 * a generic string. Falls back to the shared unexpected-error copy when the
 * rejection carries no usable message, so a non-Error throw still surfaces.
 */
export function agentSwitchFailureMessage(error: unknown): string {
  const message = typeof error === 'object' && error !== null
    ? (error as { message?: unknown }).message
    : null
  if (typeof message === 'string' && message.trim()) return message
  return i18nT('components.errorBoundary.something_went_wrong')
}
