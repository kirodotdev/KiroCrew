import { Link2 } from 'lucide-react'
import { DiscordIcon } from './DiscordIcon'
import { SlackIcon } from './SlackIcon'
import { TeamsIcon } from './TeamsIcon'
import { TelegramLogo } from './TelegramLogo'
import { WeComLogo } from './WeComLogo'
import { WebexIcon } from './WebexIcon'
import { WeixinLogo } from './WeixinLogo'

export function ChannelBrandIcon({ channel, size = 16 }: {
  channel: string
  size?: number
}) {
  switch (channel.toLowerCase()) {
    case 'slack': return <SlackIcon size={size} />
    case 'discord': return <DiscordIcon size={size} />
    case 'telegram': return <TelegramLogo size={size} />
    case 'teams': return <TeamsIcon size={size} />
    case 'webex': return <WebexIcon size={size} />
    case 'wecom': return <WeComLogo size={size} />
    case 'weixin': return <WeixinLogo size={size} />
    default: return <Link2 size={size} aria-hidden="true" />
  }
}
