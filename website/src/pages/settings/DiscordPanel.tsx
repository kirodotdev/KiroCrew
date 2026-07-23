import { DiscordIcon } from '../../components/DiscordIcon'
import { api } from '../../api/client'
import { BotChannelPanel, type BotChannelSpec } from './BotChannelPanel'

const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/discord-integration.md'

const DISCORD_SPEC: BotChannelSpec = {
  name: 'Discord',
  queryKey: 'discord-config',
  logo: <DiscordIcon size={20} />,
  description:
    'Talk to your agent from Discord DMs over the Gateway WebSocket (no webhook or ' +
    'public address needed). Add allowed user IDs so the bot can respond.',
  host: 'discord.com',
  setupGuide: SETUP_GUIDE,
  guideBody: (
    <>
      Create an app in the Discord Developer Portal, open the{' '}
      <span className="font-mono">Bot</span> page, and click{' '}
      <span className="font-mono">Reset Token</span> — paste the token below.
      No privileged intents are needed (the bot is DM-only). Invite the bot to a
      server you share, or use its install link. To find your user ID, enable
      Developer Mode in Discord settings, then right-click your name and choose{' '}
      <span className="font-mono">Copy User ID</span>.
    </>
  ),
  guideLink: { label: 'Open Developer Portal', href: 'https://discord.com/developers/applications' },
  tokenDescription: "From the Developer Portal's Bot page (Reset Token to view it once).",
  tokenPlaceholder: 'Paste Discord bot token',
  allowlistDescription:
    'Discord user IDs permitted to DM the bot. Empty = deny all (fail closed): ' +
    'anyone sharing a server with the bot can DM it.',
  allowlistPlaceholder: '123456789012345678',
  thresholdDescription: 'Prompt to !compact or !new when the session context passes this percentage.',
  emptyAllowlistHint:
    'No allowed user IDs: the bot rejects every message (fail closed). Add your Discord user ID below.',
  getConfig: api.getDiscordConfig,
  saveConfig: api.saveDiscordConfig,
}

/** Discord channel-integration settings. */
export function DiscordPanel() {
  return <BotChannelPanel spec={DISCORD_SPEC} />
}
