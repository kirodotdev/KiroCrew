import imessageLogoUrl from '../assets/imessage-logo.svg'

/**
 * iMessage brand mark: a speech bubble in the green Messages uses for an
 * iMessage thread. Deliberately a generic bubble rather than Apple's own
 * artwork, which is trademarked — the channel list only needs a mark that reads
 * at a glance.
 *
 * Shipped as an asset and rendered with `<img>`, the same treatment as
 * slack-logo.svg and discord-logo.svg: Vite emits a hashed URL under /assets
 * which the production gateway serves. Inline SVG would also risk a duplicate
 * `id` for the gradient wherever the icon renders more than once on a page.
 */
export function IMessageIcon({ size = 16 }: { size?: number }) {
  return <img src={imessageLogoUrl} width={size} height={size} alt="" aria-hidden="true" />
}
