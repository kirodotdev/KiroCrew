import wecomLogoUrl from '../assets/wecom-logo.png'

/**
 * Official WeCom (企业微信 / WeChat Work) mark — blue speech bubble with the
 * four colored teardrops. Importing the asset lets Vite emit a hashed URL
 * under /assets, which the production gateway serves (same treatment as
 * telegram-logo.svg — see vite.config.ts).
 */
export function WeComLogo({ size = 16 }: { size?: number }) {
  return <img src={wecomLogoUrl} width={size} height={size} alt="" aria-hidden="true" />
}
