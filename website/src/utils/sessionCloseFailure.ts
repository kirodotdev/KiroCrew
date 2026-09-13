import { closeHitAReplacement, isCloseOutcomeUnknown } from './closeOutcome'

/** Which of the two close failures this rejection is — CLASSIFICATION ONLY.
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
 *  a second close could reach whatever now holds the key.
 *
 *  This module renders nothing and resolves no copy. It used to call `alert()`,
 *  which the repo's `errors-use-error-notice` rule bans; the rule's glob covers
 *  `.tsx` only, so a `.ts` helper escaped it by scope rather than by being right.
 *  The App shell now renders the outcome through `ErrorNotice`. */
export const closeFailureKind = (e?: unknown): 'refused' | 'unknown' | 'replaced' => {
  if (closeHitAReplacement(e)) return 'replaced'
  return isCloseOutcomeUnknown(e) ? 'unknown' : 'refused'
}

/** The kind-to-catalog-key map, exported so the shell and the copy contract test read
 *  ONE mapping. A second copy of it in either place could drift from the other. */
export const CLOSE_FAILURE_COPY_KEY = {
  refused: 'hooks.useSessionActions.close_failed_refused',
  unknown: 'hooks.useSessionActions.close_failed_unknown',
  replaced: 'hooks.useSessionActions.close_failed_replaced',
  confirmed: 'hooks.useSessionActions.close_failed_confirmed',
} as const

/** Bodies for the ARTIFACT page's "New chat", which archives the bound session. The shared
 *  bodies name the session list, a control that user did not touch. */
export const ARTIFACT_CLOSE_FAILURE_COPY_KEY = {
  refused: 'pages.artifactDetailPage.close_failed_refused',
  unknown: 'pages.artifactDetailPage.close_failed_unknown',
  replaced: 'pages.artifactDetailPage.close_failed_replaced',
  confirmed: 'pages.artifactDetailPage.close_failed_confirmed',
} as const

/** Kinds whose outcome is settled, so the notice reports rather than warns. */
export const RESOLVED_CLOSE_KINDS: ReadonlySet<string> = new Set(['confirmed', 'replaced'])

/** The bold LEAD, rendered as `ErrorNotice`'s own `title` prop.
 *
 *  Split from the guidance rather than concatenated into it, because one 33-word danger
 *  banner packing three directives is read by a skimmer as its first clause alone -- and
 *  here that clause is "couldn't confirm", which invites the retry the rest forbids. The lead states WHAT happened and the guidance opens with the
 *  prohibition, so the forbidden act is the first thing read in either half. */
export const CLOSE_FAILURE_TITLE_KEY = {
  refused: 'hooks.useSessionActions.close_failed_refused_title',
  unknown: 'hooks.useSessionActions.close_failed_unknown_title',
  replaced: 'hooks.useSessionActions.close_failed_replaced_title',
  confirmed: 'hooks.useSessionActions.close_failed_confirmed_title',
} as const
