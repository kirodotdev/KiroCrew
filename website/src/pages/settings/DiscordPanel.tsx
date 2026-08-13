import { Trans } from 'react-i18next'

import { DiscordIcon } from '../../components/DiscordIcon'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { BotChannelPanel, type BotChannelSpec } from './BotChannelPanel'

const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/discord-integration.md'

/**
 * The Discord spec, built PER RENDER rather than held in a module-level constant.
 *
 * Every copy field here is a translated string, and `i18nT()` reads the language
 * that is current when it is CALLED. A module-level `const DISCORD_SPEC = {…}`
 * evaluates once at import, so the whole panel would freeze whatever language was
 * active at boot and never re-resolve on a switch. A function moves all of it onto
 * the render path, which is also why the JSX bodies below can hold `i18nT()` calls
 * at all.
 *
 * Deliberately NOT memoised: `useMemo(…, [])` would reintroduce exactly the freeze
 * this shape exists to avoid. The object is cheap, and `BotChannelPanel` keys its
 * query off `spec.queryKey` (a stable string), so a fresh object per render costs
 * nothing.
 *
 * The literals that remain are not copy: `name`/`host`/`queryKey` are machine
 * values, the numeric placeholders are example IDs, and the monospace tokens are
 * Discord's own UI labels and API nouns (do-not-translate). Those sit in `<code>`
 * rather than `<span>` so the markup states that they are literal tokens —
 * identical rendering, since every `code` rule in `index.css` is scoped to
 * `.msg-content` and `font-mono` already sets the same family.
 */
function discordSpec(): BotChannelSpec {
  return {
    name: 'Discord',
    queryKey: 'discord-config',
    logo: <DiscordIcon size={20} />,
    description: i18nT('pages.settings.discordPanel.description'),
    host: 'discord.com',
    setupGuide: SETUP_GUIDE,
    // One key for the whole passage rather than a key per clause: `Bot`,
    // `Message Content Intent` and `bot` are Discord's own Developer Portal
    // names (never translated), and splitting the sentence around them pinned
    // every locale to English word order and left the connectives as
    // untranslatable fragments. `<mono>` maps to the code-style span.
    guideBody: (
      <Trans
        i18nKey="pages.settings.discordPanel.guide_body"
        components={{ mono: <span className="font-mono" /> }}
      />
    ),
    guideLink: {
      label: i18nT('pages.settings.discordPanel.open_developer_portal'),
      href: 'https://discord.com/developers/applications',
    },
    tokenDescription: i18nT('pages.settings.discordPanel.token_description'),
    tokenPlaceholder: i18nT('pages.settings.discordPanel.token_placeholder'),
    allowlistDescription: i18nT('pages.settings.discordPanel.allowlist_description'),
    allowlistPlaceholder: '123456789012345678',
    thresholdDescription: i18nT('pages.settings.discordPanel.threshold_description'),
    emptyAllowlistHint: i18nT('pages.settings.discordPanel.empty_allowlist_hint'),
    threadAllowlist: {
      label: i18nT('pages.settings.discordPanel.allowed_server_thread_ids'),
      description: i18nT('pages.settings.discordPanel.thread_allowlist_description'),
      placeholder: '123456789012345678',
      // One key: `Copy Channel ID` is the Discord context-menu item verbatim,
      // and the tail clause cannot be reordered by a translator on its own.
      help: (
        <Trans
          i18nKey="pages.settings.discordPanel.thread_help"
          components={{ mono: <span className="font-mono" /> }}
        />
      ),
      warning: <>{i18nT('pages.settings.discordPanel.thread_warning')}</>,
    },
    getConfig: api.getDiscordConfig,
    saveConfig: api.saveDiscordConfig,
  }
}

/** Discord channel-integration settings. */
export function DiscordPanel() {
  return <BotChannelPanel spec={discordSpec()} />
}
