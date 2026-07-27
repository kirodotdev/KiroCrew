import { WeComLogo } from '../../components/WeComLogo'
import { api } from '../../api/client'
import { BotChannelPanel, type BotChannelSpec } from './BotChannelPanel'

const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/wecom-integration.md'

// WeCom userids: 1-64 chars of letters, digits, and .-_@ (mirrors the
// backend's fail-closed check in the save handler).
const USERID_RE = /^[A-Za-z0-9._@-]{1,64}$/

const WECOM_SPEC: BotChannelSpec = {
  name: 'WeCom',
  queryKey: 'wecom-config',
  logo: <WeComLogo size={20} />,
  description:
    'Talk to your agent from WeCom through a WeCom (企业微信) AI bot. The connection ' +
    'is an outbound WebSocket — no callback URL or open port to manage.',
  host: 'openws.work.weixin.qq.com',
  setupGuide: SETUP_GUIDE,
  guideTitle: 'Get your bot credentials',
  guideBody: (
    <>
      In the WeCom admin console, open{' '}
      <span className="font-mono">应用管理 → AI 智能体</span> and create a bot. Its
      settings page shows a <span className="font-mono">Bot ID</span> and a{' '}
      <span className="font-mono">Secret</span> — paste both below. Every WeCom
      member has a <span className="font-mono">userid</span> (账号) for the
      allow-list.
    </>
  ),
  guideLink: { label: 'Open WeCom admin console', href: 'https://work.weixin.qq.com/' },
  secondCredential: {
    label: 'WeCom bot ID',
    description: 'The Bot ID from your AI bot\u2019s settings page in the WeCom admin console.',
    placeholder: 'Paste WeCom bot ID',
  },
  tokenLabel: 'WeCom bot secret',
  tokenDescription: 'The Secret shown next to the Bot ID on the same settings page.',
  tokenPlaceholder: 'Paste WeCom bot secret',
  allowlistDescription:
    'WeCom userids (账号) permitted to DM the bot. Empty = deny all (fail closed).',
  allowlistPlaceholder: 'zhangsan',
  allowlistValidate: v => USERID_RE.test(v),
  allowAll: {
    label: 'Allow all organization members',
    description:
      'Let everyone in your WeCom organization DM the bot without listing each userid. ' +
      'A WeCom AI bot is only reachable inside your own org tenant, but this grants ' +
      'agent access to the whole company — leave off for a per-person allow-list.',
    bypassNote:
      'Allow-all is on: the userid list above is bypassed. It is kept for when you switch back.',
  },
  thresholdDescription: 'Prompt to /compact or /new when the session context passes this percentage.',
  emptyAllowlistHint:
    'No allowed userids: the bot rejects every message (fail closed). Add your WeCom userid below.',
  getConfig: api.getWeComConfig,
  saveConfig: api.saveWeComConfig,
  // The backend updates connected/connect_error at channel start; refresh
  // periodically so the status badge tracks a gateway restart.
  refetchInterval: 15_000,
}

/** WeCom (企业微信) channel-integration settings. */
export function WeComPanel() {
  return <BotChannelPanel spec={WECOM_SPEC} />
}
