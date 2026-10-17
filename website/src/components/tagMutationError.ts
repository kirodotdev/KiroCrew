/**
 * The user-facing message for a failed tag-vocabulary write. Shared by
 * `TagManagerList` and `SlotTagPopover` so one failure reads the same in both.
 */
import { i18nT } from '../i18n/t'

/** A signed-out session's message is already localized recovery guidance. */
function isAuthRecovery(error: unknown): error is Error {
  return error instanceof Error && (error as { authRequired?: unknown }).authRequired === true
}

/**
 * A failed tag create/rename/recolor/status/delete, in the tag manager or the
 * slot tag popover. A server-side (5xx) or transport failure (`fetch`'s
 * `TypeError`, e.g. "Failed to fetch") carries no next step for the user, so it
 * becomes the localized task-shaped message; a request the server refused (4xx)
 * keeps its specific reason.
 */
export function crudErrorMessage(error: unknown): string {
  if (isAuthRecovery(error)) return error.message
  const status = (error as { status?: unknown } | null)?.status
  const serverSide = typeof status === 'number' && status >= 500
  if (!serverSide && !(error instanceof TypeError) && error instanceof Error && error.message) return error.message
  return i18nT('components.tagManagerList.save_failed')
}

/**
 * A failed policy save or adoption always reads from the catalog: the server's
 * refusals here are English prose, and the dashboard renders in the user's
 * language. Only session recovery, already localized, passes through.
 */
export function policyErrorMessage(error: unknown, fallback: string): string {
  return isAuthRecovery(error) ? error.message : fallback
}
