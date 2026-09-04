import { Trans } from 'react-i18next'
import { FeishuLogo } from '../../components/FeishuLogo'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { BotChannelPanel, type BotChannelSpec } from './BotChannelPanel'

const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/feishu-integration.md'

/**
 * Feishu id shape check, mirroring the backend's fail-closed
 * `_is_valid_feishu_id`: a fixed prefix (`ou_` a user open_id, `oc_` a group
 * chat_id) plus an opaque ASCII-alphanumeric body. No length equality — the body
 * length is not contractual, and a stricter rule here would reject ids the
 * backend accepts, which reads to the user as the field being broken.
 */
const OPEN_ID_RE = /^ou_[A-Za-z0-9]+$/
const CHAT_ID_RE = /^oc_[A-Za-z0-9]+$/

/**
 * The Feishu channel spec, built PER RENDER.
 *
 * Module scope would freeze the boot language for the life of the tab: an
 * `i18nT()` call in a module-level object resolves once at import, before the
 * user has picked a language, and never re-resolves on a switch
 * (`src/i18n/moduleLevel.test.ts` fails the build on exactly that). Building it
 * in a function moves every lookup into render, which is safe because
 * `BotChannelPanel` reads the spec during render only.
 */
function feishuSpec(): BotChannelSpec {
  return {
    // Product name — not translated.
    name: 'Feishu',
    queryKey: 'feishu-config',
    logo: <FeishuLogo size={20} />,
    description: i18nT('pages.settings.feishuPanel.description'),
    // The lark-oapi SDK opens the long connection to this host; named so the
    // failed-to-start hint can point at egress rather than at the credentials.
    host: 'open.feishu.cn',
    setupGuide: SETUP_GUIDE,
    guideTitle: i18nT('pages.settings.channels.get_your_bot_credentials'),
    // One key for the whole passage rather than a key per clause: the console
    // menu path, `App ID`, `App Secret` and `open_id` are Feishu's own
    // identifiers (not translated), and splitting the sentence around them would
    // pin every locale to English word order. `<mono>` maps to the code span.
    guideBody: (
      <Trans
        i18nKey="pages.settings.feishuPanel.guide_body"
        components={{ mono: <span className="font-mono" /> }}
      />
    ),
    guideLink: {
      label: i18nT('pages.settings.feishuPanel.open_developer_console'),
      href: 'https://open.feishu.cn/app',
    },
    secondCredential: {
      label: i18nT('pages.settings.feishuPanel.app_id_label'),
      description: i18nT('pages.settings.feishuPanel.app_id_description'),
      placeholder: 'cli_a1b2c3d4e5f6g7h8',
    },
    tokenLabel: i18nT('pages.settings.feishuPanel.app_secret_label'),
    tokenDescription: i18nT('pages.settings.feishuPanel.app_secret_description'),
    tokenPlaceholder: i18nT('pages.settings.feishuPanel.app_secret_placeholder'),
    allowlistDescription: i18nT('pages.settings.feishuPanel.allowlist_description'),
    allowlistPlaceholder: 'ou_c99cbd8a1b2c3d4e5f6a7b8c9d0e1f2a',
    allowlistValidate: v => OPEN_ID_RE.test(v),
    // No allow-everyone switch, unlike WeCom: a Feishu custom app can be
    // published to the whole tenant, so an allow-all here would hand the agent
    // to every employee with no per-user record of who was granted it.
    groupChats: {
      toggleLabel: i18nT('pages.settings.feishuPanel.allow_group_label'),
      toggleDescription: (
        <Trans
          i18nKey="pages.settings.feishuPanel.allow_group_description"
          components={{ mono: <span className="font-mono" /> }}
        />
      ),
      allowlistLabel: i18nT('pages.settings.feishuPanel.group_allowlist_label'),
      allowlistDescription: i18nT('pages.settings.feishuPanel.group_allowlist_description'),
      allowlistPlaceholder: 'oc_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6',
      allowlistValidate: v => CHAT_ID_RE.test(v),
      emptyHint: i18nT('pages.settings.feishuPanel.group_empty_hint'),
    },
    thresholdDescription: i18nT('pages.settings.channels.threshold_description', {
      compact: '/compact',
      new: '/new',
    }),
    emptyAllowlistHint: i18nT('pages.settings.feishuPanel.empty_allowlist_hint'),
    // lark-oapi ships as the optional [feishu] extra, so a fully credentialed
    // channel still cannot start without it; the panel surfaces the install
    // command for THIS gateway's interpreter when it is missing.
    sdkExtra: { packageLabel: 'lark-oapi' },
    getConfig: api.getFeishuConfig,
    saveConfig: api.saveFeishuConfig,
    // The badge tracks the receiver thread, which a rejected app ends within
    // seconds of a restart; refresh so that correction actually reaches the UI.
    refetchInterval: 15_000,
  }
}

/** Feishu (飞书/Lark) channel-integration settings. */
export function FeishuPanel() {
  return <BotChannelPanel spec={feishuSpec()} />
}
