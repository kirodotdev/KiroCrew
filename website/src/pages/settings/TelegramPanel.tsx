import type { ReactNode } from 'react'
import { TelegramLogo } from '../../components/TelegramLogo'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { BotChannelPanel, type BotChannelSpec } from './BotChannelPanel'

const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/telegram-integration.md'

/**
 * Chat commands this channel accepts, interpolated into the SHARED soft-threshold
 * sentence. DNT: a translated command name invokes nothing. Telegram and WeCom
 * use the `/` prefix, Discord `!`, which is exactly why the sentence takes them
 * as parameters instead of being copied per channel.
 */
const TELEGRAM_COMMANDS = { compact: '/compact', new: '/new' } as const

/**
 * Render a catalog sentence in which backtick-delimited runs are monospace
 * do-not-translate literals — Telegram bot handles, slash commands and API field
 * names (`@BotFather`, `/newbot`, `chat_id`).
 *
 * ONE catalog string per sentence, rather than translated fragments stitched
 * around hard-coded `<span>`s: fragments lock every language into English clause
 * order, and each of these sentences carries its token mid-clause. Same intent as
 * `AboutPanel`'s `{{link}}` split.
 *
 * The tokens live INSIDE the catalog value rather than being interpolated from
 * code, which is what keeps the whole sentence legible to a translator — the
 * `context` note tells them the backticked runs are literal and must survive
 * untranslated. The backtick is markdown's own code convention, so it needs no
 * explaining, and it carries no `{{name}}` that a locale could typo into a
 * silently dropped word.
 */
function withMonoTokens(text: string): ReactNode {
  // Split on the capture group: even positions are prose, odd ones the literals.
  return text.split(/`([^`]+)`/g).map((part, i) => (
    i % 2 === 0 ? part : <span key={i} className="font-mono">{part}</span>
  ))
}

/**
 * Build the Telegram {@link BotChannelSpec}.
 *
 * A FUNCTION, not a module-level const: the spec is almost entirely display copy,
 * and a module const is evaluated once at import, so every `i18nT()` in it would
 * freeze whatever language was active at boot and never re-resolve on a language
 * switch. `TelegramPanel` calls this during render, which is the position the
 * catalog lookups have to happen from.
 */
function telegramSpec(): BotChannelSpec {
  return {
    name: 'Telegram',
    queryKey: 'telegram-config',
    logo: <TelegramLogo size={20} />,
    description: i18nT('pages.settings.telegramPanel.description'),
    host: 'api.telegram.org',
    setupGuide: SETUP_GUIDE,
    guideBody: withMonoTokens(i18nT('pages.settings.telegramPanel.guide_body')),
    guideLink: {
      label: i18nT('pages.settings.telegramPanel.guide_link_label'),
      href: 'https://t.me/BotFather',
    },
    tokenDescription: i18nT('pages.settings.telegramPanel.token_description'),
    tokenPlaceholder: i18nT('pages.settings.telegramPanel.token_placeholder'),
    allowlistDescription: i18nT('pages.settings.telegramPanel.allowlist_description'),
    allowlistPlaceholder: '123456789',
    // SHARED across the bot channels — the command prefix differs per channel
    // (Telegram/WeCom `/compact`, Discord `!compact`), so it is a parameter
    // rather than four copies of one sentence.
    thresholdDescription: i18nT('pages.settings.channels.threshold_description', {
      compact: TELEGRAM_COMMANDS.compact,
      new: TELEGRAM_COMMANDS.new,
    }),
    emptyAllowlistHint: i18nT('pages.settings.telegramPanel.empty_allowlist_hint'),
    showThinking: {
      label: i18nT('pages.settings.telegramPanel.show_thinking_label'),
      description: i18nT('pages.settings.telegramPanel.show_thinking_description'),
    },
    voiceReplies: {
      label: i18nT('pages.settings.telegramPanel.voice_replies_label'),
      description: i18nT('pages.settings.telegramPanel.voice_replies_description'),
    },
    forum: {
      toggleLabel: i18nT('pages.settings.telegramPanel.forum_toggle_label'),
      toggleDescription: withMonoTokens(i18nT('pages.settings.telegramPanel.forum_toggle_description')),
      allowlistLabel: i18nT('pages.settings.telegramPanel.forum_allowlist_label'),
      allowlistDescription: i18nT('pages.settings.telegramPanel.forum_allowlist_description'),
      allowlistPlaceholder: '-1001234567890',
      emptyHint: i18nT('pages.settings.telegramPanel.forum_empty_hint'),
      // POSITIONALLY paired with optionLabels, and the values are the backend's
      // own modes — a label list that drifted out of order would silently save a
      // different mode than the one the operator picked.
      activation: {
        label: i18nT('pages.settings.telegramPanel.forum_activation_label'),
        description: i18nT('pages.settings.telegramPanel.forum_activation_description'),
        hint: i18nT('pages.settings.telegramPanel.forum_activation_hint'),
        options: ['always', 'mention', 'off'],
        optionLabels: [
          i18nT('pages.settings.telegramPanel.forum_activation_always'),
          i18nT('pages.settings.telegramPanel.forum_activation_mention'),
          i18nT('pages.settings.telegramPanel.forum_activation_off'),
        ],
      },
    },
    getConfig: api.getTelegramConfig,
    saveConfig: api.saveTelegramConfig,
    // The backend updates connected/connect_error live (polling health), so
    // refresh periodically to keep the status badge truthful.
    refetchInterval: 15_000,
  }
}

/** Telegram channel-integration settings. */
export function TelegramPanel() {
  return <BotChannelPanel spec={telegramSpec()} />
}
