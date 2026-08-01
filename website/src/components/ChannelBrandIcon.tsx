import type { ComponentType } from 'react'
import { Link2 } from 'lucide-react'
import { DiscordIcon } from './DiscordIcon'
import { SlackIcon } from './SlackIcon'
import { TeamsIcon } from './TeamsIcon'
import { TelegramLogo } from './TelegramLogo'
import { WeComLogo } from './WeComLogo'
import { WebexIcon } from './WebexIcon'
import { WeixinLogo } from './WeixinLogo'

/**
 * Brand mark per channel namespace, keyed lowercase.
 *
 * A table rather than a `switch` so callers can ASK whether a brand mark exists
 * (`hasChannelBrandIcon`) instead of rendering one and discovering the `Link2`
 * fallback. Two namespaces reach this component with no mark of their own —
 * `whatsapp` (no asset yet) and `unified` (the aggregated DM inbox, which is not
 * a product and will never have one) — and a caller that needs a *truthful*
 * glyph has to branch before rendering.
 */
const BRAND_ICONS: Record<string, ComponentType<{ size?: number }>> = {
  slack: SlackIcon,
  discord: DiscordIcon,
  telegram: TelegramLogo,
  teams: TeamsIcon,
  webex: WebexIcon,
  wecom: WeComLogo,
  weixin: WeixinLogo,
}

/** True when `channel` has a real brand mark (i.e. not the `Link2` fallback). */
export function hasChannelBrandIcon(channel: string): boolean {
  return channel.toLowerCase() in BRAND_ICONS
}

export function ChannelBrandIcon({ channel, size = 16 }: {
  channel: string
  size?: number
}) {
  const Icon = BRAND_ICONS[channel.toLowerCase()]
  return Icon ? <Icon size={size} /> : <Link2 size={size} aria-hidden="true" />
}
