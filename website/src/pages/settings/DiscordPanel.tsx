import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { ExternalLink } from 'lucide-react'

import { DiscordIcon } from '../../components/DiscordIcon'
import ErrorNotice from '../../components/ErrorNotice'
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
    namespace: 'discord',
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
    tokenLabel: i18nT('pages.settings.discordPanel.token_label'),
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
    sharedChannels: {
      label: i18nT('pages.settings.discordPanel.allowed_server_channel_ids'),
      description: i18nT('pages.settings.discordPanel.channel_allowlist_description'),
      placeholder: '123456789012345678',
      // One key, for the same reason as `thread_help` above.
      help: (
        <Trans
          i18nKey="pages.settings.discordPanel.channel_help"
          components={{ mono: <span className="font-mono" /> }}
        />
      ),
      warning: <>{i18nT('pages.settings.discordPanel.channel_warning')}</>,
      autoThreadLabel: i18nT('pages.settings.discordPanel.auto_thread_label'),
      autoThreadDescription: i18nT('pages.settings.discordPanel.auto_thread_description'),
      autoThreadConfigKey: 'discord.auto_thread',
      autoThreadOffHint: i18nT('pages.settings.discordPanel.auto_thread_off_hint'),
    },
    progressDisplay: {
      reactionsLabel: i18nT('pages.settings.discordPanel.reactions_label'),
      reactionsDescription: i18nT('pages.settings.discordPanel.reactions_description'),
      reactionsConfigKey: 'discord.reactions_enabled',
      thinkingLabel: i18nT('pages.settings.discordPanel.thinking_label'),
      thinkingDescription: i18nT('pages.settings.discordPanel.thinking_description'),
      thinkingConfigKey: 'discord.show_thinking',
      footerLabel: i18nT('pages.settings.discordPanel.footer_label'),
      footerDescription: i18nT('pages.settings.discordPanel.footer_description'),
      footerConfigKey: 'discord.show_timing_footer',
    },
    guideExtra: <InviteLinks />,
    getConfig: api.getDiscordConfig,
    saveConfig: api.saveDiscordConfig,
  }
}

/** What the backend's install-URL builder emits; anything else is not rendered as a link. */
const AUTHORIZE_PREFIX = 'https://discord.com/oauth2/authorize?'

/**
 * The connected bot's invite links, built by the backend from the application id
 * the live connection reported (`discord/install_url.py`). Reads the panel's own
 * query, so it adds no request. Until the bot connects there is no id to build
 * them from, and the line says so.
 */
function InviteLinks() {
  const { data } = useQuery({ queryKey: ['discord-config'], queryFn: api.getDiscordConfig, retry: false, refetchOnWindowFocus: false })
  const threads = data?.install_url ?? ''
  const dmOnly = data?.install_url_dm_only ?? ''
  const ready = threads.startsWith(AUTHORIZE_PREFIX) && dmOnly.startsWith(AUTHORIZE_PREFIX)
  return (
    <div className="border-t border-border mt-3 pt-3" data-testid="discord-invite-links">
      <p className="text-[13px] font-medium text-text-strong m-0">{i18nT('pages.settings.discordPanel.invite_title')}</p>
      {ready ? (
        <>
          <p className="text-[12px] text-muted mt-1 mb-0">{i18nT('pages.settings.discordPanel.invite_body')}</p>
          <div className="flex items-center gap-4 mt-2 flex-wrap">
            <a href={dmOnly} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              {i18nT('pages.settings.discordPanel.invite_dm_only')} <ExternalLink size={13} />
            </a>
            <a href={threads} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              {i18nT('pages.settings.discordPanel.invite_threads')} <ExternalLink size={13} />
            </a>
          </div>
        </>
      ) : (
        <p className="text-[12px] text-muted mt-1 mb-0">{i18nT('pages.settings.discordPanel.invite_pending')}</p>
      )}
    </div>
  )
}

/**
 * Discord channel-integration settings.
 *
 * A save also reconnects the running bot with what it stored. When the bot
 * could not take part of it (a token Discord rejected, server-thread intents it
 * refused), the save still succeeded, so the reason stays here as a `partial`
 * notice until the next save or a dismiss, rather than in the short-lived
 * "Saved" line.
 */
export function DiscordPanel() {
  const [reconnectError, setReconnectError] = useState('')
  const spec = discordSpec()
  const saveConfig: BotChannelSpec['saveConfig'] = async body => {
    const res = await api.saveDiscordConfig(body)
    setReconnectError(res.reconnect_error || '')
    return res
  }
  return (
    <>
      <BotChannelPanel spec={{ ...spec, saveConfig }} />
      {/* No hand-off: the panel's fields above can hold edits made after this save. */}
      <ErrorNotice
        message={reconnectError}
        impact="partial"
        onDismiss={() => setReconnectError('')}
        className="max-w-[720px] mb-4"
      />
    </>
  )
}
