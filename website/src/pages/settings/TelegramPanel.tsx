import { TelegramLogo } from '../../components/TelegramLogo'
import { api } from '../../api/client'
import { BotChannelPanel, type BotChannelSpec } from './BotChannelPanel'

const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/telegram-integration.md'

const TELEGRAM_SPEC: BotChannelSpec = {
  name: 'Telegram',
  queryKey: 'telegram-config',
  logo: <TelegramLogo size={20} />,
  description:
    'Talk to your agent from Telegram over the Bot API (long-polling — no webhook or ' +
    'public address needed). Add allowed user IDs so the bot can respond.',
  host: 'api.telegram.org',
  setupGuide: SETUP_GUIDE,
  guideBody: (
    <>
      Message <span className="font-mono">@BotFather</span> on Telegram, send{' '}
      <span className="font-mono">/newbot</span>, and follow the prompts. BotFather
      replies with a bot token — paste it below. To find your numeric user ID,
      message <span className="font-mono">@userinfobot</span>.
    </>
  ),
  guideLink: { label: 'Open @BotFather', href: 'https://t.me/BotFather' },
  tokenDescription: 'From @BotFather after creating your bot (looks like 110201543:AAHdqT…).',
  tokenPlaceholder: 'Paste Telegram bot token (123456:ABC-…)',
  allowlistDescription:
    'Numeric Telegram user IDs permitted to DM the bot. Empty = deny all (fail closed): ' +
    'a Telegram bot is globally reachable by @username.',
  allowlistPlaceholder: '123456789',
  thresholdDescription: 'Prompt to /compact or /new when the session context passes this percentage.',
  emptyAllowlistHint:
    'No allowed user IDs: the bot rejects every message (fail closed). Add your numeric Telegram user ID below.',
  getConfig: api.getTelegramConfig,
  saveConfig: api.saveTelegramConfig,
  // The backend updates connected/connect_error live (polling health), so
  // refresh periodically to keep the status badge truthful.
  refetchInterval: 15_000,
}

/** Telegram channel-integration settings. */
export function TelegramPanel() {
  return <BotChannelPanel spec={TELEGRAM_SPEC} />
}
