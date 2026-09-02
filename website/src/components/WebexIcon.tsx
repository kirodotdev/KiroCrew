import webexLogoUrl from '../assets/webex-logo.svg'

/**
 * Official Webex logo mark, full color. Importing the asset lets Vite emit a
 * hashed URL under /assets, which the production gateway serves (39 KB — well
 * over the inline limit, so it is always emitted as a physical file).
 */
export function WebexIcon({ size = 16 }: { size?: number }) {
  return <img src={webexLogoUrl} width={size} height={size} alt="" aria-hidden="true" />
}
