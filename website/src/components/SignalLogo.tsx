import signalLogoUrl from '../assets/signal-logo.svg'

/**
 * Signal brand mark: a round speech bubble in Signal's blue with a dashed
 * ring. Deliberately a self-drawn bubble rather than Signal's own artwork,
 * which is trademarked — the channel list only needs a mark that reads at a
 * glance, the same choice IMessageIcon makes.
 *
 * Shipped as an asset and rendered with `<img>`, the same treatment as
 * imessage-logo.svg and whatsapp-logo.svg: Vite emits a hashed URL under
 * /assets which the production gateway serves.
 */
export function SignalLogo({ size = 16 }: { size?: number }) {
  return <img src={signalLogoUrl} width={size} height={size} alt="" aria-hidden="true" />
}
