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
