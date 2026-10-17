/**
 * The user-facing copy a surface renders for a receipt that did not confirm
 * delivery -- ONE spelling for every surface that hands text back.
 *
 * Two surfaces (ChatEmbed, SideChat) grew the same construction independently:
 * a refusal is FRAMED as a failed send rather than shown as the server's bare
 * sentence (a raw "slot agent mismatch" reads as the agent erroring mid-work),
 * the row says the text is back in the composer only when the send actually
 * consumed the draft (a composer submit; a chip send never did, and a
 * pre-filled box with no word about it reads as a guess), and the `_restored`
 * template supplies its own sentence end, so a reason that is itself a
 * sentence (the permission denial's human copy) loses its terminal stop rather
 * than closing twice. Spelled here once so the surfaces cannot drift.
 *
 * The keys are the shared `pages.chatPage.*` ones ChatPane and ChatPage also
 * render, in every locale.
 */
import { i18nT } from '../../i18n/t'

/**
 * The failure row for a `refused` / `transport-error` receipt.
 *
 * @param reason  the server's own sentence when it gave one; without a reason
 *                the only remaining cause is the transport itself (the wire
 *                names every server-side refusal), so the row states that.
 * @param restored whether the send consumed the composer's draft, i.e. whether
 *                the surface handed text back into it.
 */
export function sendFailureCopy(reason: string | undefined, restored: boolean): string {
  if (!reason) return i18nT('pages.chatPage.send_failed_connection') as string
  return (restored
    ? i18nT('pages.chatPage.send_failed_with_error_restored', { error: reason.replace(/[.。।]+$/u, '') })
    : i18nT('pages.chatPage.send_failed_with_error', { error: reason })) as string
}

/**
 * The standing notice for a `response-late` / `unknown` receipt: delivery is
 * unconfirmed, look before resending. The chip variant tells the user to tap
 * the suggestion again instead of claiming a restore that did not happen.
 */
export function deliveryUnconfirmedCopy(restored: boolean): string {
  return i18nT(restored ? 'pages.chatPage.delivery_unconfirmed' : 'pages.chatPage.delivery_unconfirmed_option') as string
}

/**
 * The banner text for the page's action-error when a comment batch send returns
 * `response-late` (delivery indeterminate) or `transport-error` (connection
 * dropped before a response arrived). The comments stay pending for a retry,
 * and the copy gives the rule for it: send them again if they are not in the
 * conversation within a minute, because sending again sooner repeats any
 * comments that did arrive. The minute is the same bound the chat page gives
 * for an unconfirmed message (`delivery_unconfirmed_pending`).
 *
 * `panelClosed` is true when the failure closed the overlay (a narrow window),
 * so the copy also says to reopen the side panel; a wide window still shows the
 * document beside the thread, so it says to send them "from the document". The
 * copy names no button: the retry control is Submit All on a file tab and
 * Submit on an artifact tab. Shown outside the DM transcript -- the crewmate's
 * DM stays messages-only per the 2026-10-09 recorded decision.
 */
export function deliveryUnconfirmedCommentsCopy(panelClosed = false): string {
  return i18nT(panelClosed
    ? 'pages.chatPage.delivery_unconfirmed_comments_overlay'
    : 'pages.chatPage.delivery_unconfirmed_comments') as string
}

/**
 * The banner text when a `refused` receipt arrives for a comment batch send.
 * Uses a user-friendly message with the crewmate's display name rather than
 * the server's internal reason, so implementation details like "slot unavailable"
 * never reach the UI. The raw reason is logged by the caller for debugging.
 * Falls back to `yourCrewmateCopy()` when `name` is empty. The comments did not
 * arrive, so the copy says to send them again at once and that they are still in
 * the document. `panelClosed` picks the variant that also says to reopen the
 * side panel, for the reason `deliveryUnconfirmedCommentsCopy` gives; neither
 * variant names a button.
 */
export function commentsRefusedCopy(name: string | undefined, panelClosed = false): string {
  return i18nT(panelClosed ? 'pages.chatPage.comments_refused_overlay' : 'pages.chatPage.comments_refused',
    { name: name || yourCrewmateCopy() }) as string
}

/**
 * The fallback crewmate display name used when the member's name is unknown.
 * Rendered by `commentsRefusedCopy` when no display name is available.
 */
export function yourCrewmateCopy(): string {
  return i18nT('pages.chatPage.your_crewmate') as string
}

/**
 * The status line for a delivered comment batch: a `dispatched` or `queued`
 * receipt, or an `unknown` one (a 2xx whose body could not be read, so the
 * server took the request). `count` is the number of comment rows in the
 * batch; i18next picks the singular or plural form automatically.
 */
export function commentsSentCopy(count: number): string {
  return i18nT('pages.chatPage.comments_sent', { count }) as string
}
