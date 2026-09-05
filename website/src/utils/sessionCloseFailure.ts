import { i18nT } from '../i18n/t'
import { isCloseOutcomeUnknown } from './closeOutcome'

/** Tell the user a session close did not take, using the rejection's own status.
 *
 *  Both close gestures route here — the session menu and Alt+Shift+W — because a
 *  terminal failure restores the row, and a row reappearing alone looks like the
 *  flicker `closingSlots` fixes.
 *
 *  Only TWO outcomes are distinguishable, and the split is about what the user may
 *  safely do next. A definitive client-side refusal is the server's considered
 *  answer, so the session is provably still there. Anything else — no status at
 *  all, a timeout, a rate limit, a 5xx from something in the path — leaves the
 *  outcome UNKNOWN: the DELETE may have completed, and slot keys are reusable, so
 *  inviting a retry would aim a second close at whatever now holds the key. */
export const alertSessionCloseFailed = (e?: unknown): void => {
  alert(i18nT(!isCloseOutcomeUnknown(e)
    ? 'hooks.useSessionActions.close_failed_refused'
    : 'hooks.useSessionActions.close_failed_unknown'))
}
