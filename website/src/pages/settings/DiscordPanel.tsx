import { DiscordIcon } from '../../components/DiscordIcon'
import { api } from '../../api/client'
import { BotChannelPanel, type BotChannelSpec } from './BotChannelPanel'

const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/discord-integration.md'

const DISCORD_SPEC: BotChannelSpec = {
  name: 'Discord',
  queryKey: 'discord-config',
  logo: <DiscordIcon size={20} />,
  description:
    'Talk to your agent from Discord DMs or explicitly approved server threads over the ' +
    'Gateway WebSocket. Normal server channels are always ignored.',
  host: 'discord.com',
  setupGuide: SETUP_GUIDE,
  guideBody: (
    <>
      Create an app in the Discord Developer Portal, then copy the token from the{' '}
      <span className="font-mono">Bot</span> page. For DMs only, leave privileged intents
      off. For server threads, enable <span className="font-mono">Message Content Intent</span>{' '}
      and install with the <span className="font-mono">bot</span> scope plus View Channel,
      Read Message History, and Send Messages in Threads. Enable Discord Developer Mode,
      then right-click your name to copy your numeric user ID.
    </>
  ),
  guideLink: { label: 'Open Developer Portal', href: 'https://discord.com/developers/applications' },
  tokenDescription: "From the Developer Portal's Bot page (Reset Token to view it once).",
  tokenPlaceholder: 'Paste Discord bot token',
  allowlistDescription:
    'Discord user IDs permitted to run the agent in DMs or approved threads. ' +
    'Empty = deny all (fail closed).',
  allowlistPlaceholder: '123456789012345678',
  thresholdDescription: 'Prompt to !compact or !new when the session context passes this percentage.',
  emptyAllowlistHint:
    'No allowed user IDs: the bot rejects every message (fail closed). Add your Discord user ID below.',
  threadAllowlist: {
    label: 'Allowed server thread IDs',
    description:
      'Optional. Approved users may run the agent only in these exact Discord threads. ' +
      'Empty = DMs only.',
    placeholder: '123456789012345678',
    help: (
      <>
        With Developer Mode on, right-click the thread →{' '}
        <span className="font-mono">Copy Channel ID</span>. Use the thread's ID, not its
        parent channel ID.
      </>
    ),
    warning: (
      <>
        Thread mode enables Discord's global Message Content intent. Discord delivers content
        from every server channel the bot can see; Kiro Crew discards traffic outside approved
        threads. Everyone who can view an approved thread can read agent replies and tool output.
      </>
    ),
  },
  getConfig: api.getDiscordConfig,
  saveConfig: api.saveDiscordConfig,
}

/** Discord channel-integration settings. */
export function DiscordPanel() {
  return <BotChannelPanel spec={DISCORD_SPEC} />
}
