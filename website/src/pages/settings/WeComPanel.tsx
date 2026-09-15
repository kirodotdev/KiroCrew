import { Trans } from 'react-i18next'
import { WeComLogo } from '../../components/WeComLogo'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { BotChannelPanel, type BotChannelSpec } from './BotChannelPanel'

const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/wecom-integration.md'

// WeCom userids: 1-64 chars of letters, digits, and .-_@ (mirrors the
// backend's fail-closed check in the save handler).
const USERID_RE = /^[A-Za-z0-9._@-]{1,64}$/

/**
 * The WeCom channel spec, built PER RENDER.
 *
 * It used to be a module-level `const WECOM_SPEC`, which cannot hold localised
 * copy: a module-scope object initialises once at import — before the user has
 * picked a language — so an `i18nT()` call inside it would freeze the boot
 * language for the life of the tab and never re-resolve on a language switch
 * (`src/i18n/moduleLevel.test.ts` fails the build on exactly that). Building the
 * object in a function moves every lookup into render.
 *
 * Recreating it each render is safe: `BotChannelPanel` reads the spec during
 * render only. It is in no dependency array, `queryKey` and `refetchInterval`
 * are compared by value by react-query, and the optional-section fields
 * (`allowAll`, `secondCredential`) keep the same shape every time.
 */
function wecomSpec(): BotChannelSpec {
  return {
    // Product name — not translated.
    name: 'WeCom',
    queryKey: 'wecom-config',
    logo: <WeComLogo size={20} />,
    description: i18nT('pages.settings.weComPanel.description'),
    host: 'openws.work.weixin.qq.com',
    setupGuide: SETUP_GUIDE,
    guideTitle: i18nT('pages.settings.channels.get_your_bot_credentials'),
    // One key for the whole passage rather than a key per clause: the console
    // menu path, `Bot ID`, `Secret` and `userid` are WeCom's own identifiers
    // (not translated), and splitting the sentence around them would pin every
    // locale to English word order. `<mono>` maps to the code-style span.
    guideBody: (
      <Trans
        i18nKey="pages.settings.weComPanel.guide_body"
        components={{ mono: <span className="font-mono" /> }}
      />
    ),
    guideLink: {
      label: i18nT('pages.settings.weComPanel.open_admin_console'),
      href: 'https://work.weixin.qq.com/',
    },
    secondCredential: {
      label: i18nT('pages.settings.weComPanel.bot_id_label'),
      description: i18nT('pages.settings.weComPanel.bot_id_description'),
      placeholder: i18nT('pages.settings.weComPanel.bot_id_placeholder'),
    },
    tokenLabel: i18nT('pages.settings.weComPanel.token_label'),
    tokenDescription: i18nT('pages.settings.weComPanel.token_description'),
    tokenPlaceholder: i18nT('pages.settings.weComPanel.token_placeholder'),
    allowlistDescription: i18nT('pages.settings.weComPanel.allowlist_description'),
    allowlistPlaceholder: 'zhangsan',
    allowlistValidate: v => USERID_RE.test(v),
    allowAll: {
      label: i18nT('pages.settings.weComPanel.allow_all_label'),
      description: i18nT('pages.settings.weComPanel.allow_all_description'),
      bypassNote: i18nT('pages.settings.weComPanel.allow_all_bypass_note'),
    },
    thresholdDescription: i18nT('pages.settings.channels.threshold_description', {
      compact: '/compact',
      new: '/new',
    }),
    emptyAllowlistHint: i18nT('pages.settings.weComPanel.empty_allowlist_hint'),
    getConfig: api.getWeComConfig,
    saveConfig: api.saveWeComConfig,
    // The backend updates connected/connect_error at channel start; refresh
    // periodically so the status badge tracks a gateway restart.
    refetchInterval: 15_000,
  }
}

/** WeCom (企业微信) channel-integration settings. */
export function WeComPanel() {
  return <BotChannelPanel spec={wecomSpec()} />
}
