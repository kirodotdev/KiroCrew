import wechatLogoUrl from '../assets/wechat-logo.png'

/**
 * Official WeChat (微信) mark — the green double speech bubble. Importing the
 * asset lets Vite emit a hashed URL under /assets, which the production gateway
 * serves (same treatment as wecom-logo.png — see WeComLogo).
 *
 * Distinct from {@link WeComLogo}: that is enterprise WeCom (企业微信, blue),
 * this is personal WeChat via the iLink bot API.
 */
export function WeixinLogo({ size = 16 }: { size?: number }) {
  return <img src={wechatLogoUrl} width={size} height={size} alt="" aria-hidden="true" />
}
