/**
 * Messaging-channel integrations: config read/save for Slack, Discord,
 * Telegram, WeCom, Feishu, Webex, iMessage, Teams, Weixin and WhatsApp, the
 * Weixin and WhatsApp QR pairing, unlink and groups, and the effective
 * per-channel governance decision.
 */

import type { ClientTransport } from './transport'

/** Slack config as returned by GET /api/slack/config (secrets masked). */
export interface SlackConfigData {
  connected: boolean
  connect_error: string
  configured: boolean
  read_only: boolean
  bot_token_set: boolean
  app_token_set: boolean
  bot_token_preview: string
  app_token_preview: string
  owner_id: string
  command: string
  allowed_enterprise_ids: string[]
  reactions_enabled: boolean
  show_thinking: boolean
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
  /** Open a Slack thread for every new dashboard session on its first message. */
  auto_link_sessions?: boolean
  /** Where that thread opens: "" = the owner's bot DM, else a channel ID. */
  auto_link_channel?: string
}

/** Writable Slack config fields sent to PUT /api/slack/config. */
export interface SlackConfigSave {
  bot_token: string
  bot_token_clear: boolean
  app_token: string
  app_token_clear: boolean
  owner_id: string
  command: string
  allowed_enterprise_ids: string[]
  reactions_enabled: boolean
  show_thinking: boolean
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
  /** Open a Slack thread for every new dashboard session on its first message. */
  auto_link_sessions?: boolean
  /** Where that thread opens: "" = the owner's bot DM, else a channel ID. */
  auto_link_channel?: string
}

/** Discord config as returned by GET /api/discord/config (secret masked). */
export interface DiscordConfigData {
  connected: boolean
  connect_error: string
  configured: boolean
  read_only: boolean
  bot_token_set: boolean
  bot_token_preview: string
  enabled: boolean
  allowed_user_ids: string[]
  allowed_thread_ids: string[]
  /** Shared server channels an approved user may start a turn in. */
  allowed_channel_ids: string[]
  /** Promote an allowed-channel message into a fresh public thread. Default on. */
  auto_thread: boolean
  soft_threshold_pct: number
  /** Phase-reaction ladder on the user's own message. Default on. */
  reactions_enabled: boolean
  /** Surface the model's reasoning as a Discord subtext note. Default off. */
  show_thinking: boolean
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Telegram config as returned by GET /api/telegram/config (secret masked). */
export interface TelegramConfigData {
  connected: boolean
  connect_error: string
  configured: boolean
  read_only: boolean
  bot_token_set: boolean
  bot_token_preview: string
  enabled: boolean
  allowed_user_ids: string[]
  soft_threshold_pct: number
  /** Post the model's reasoning after each answer as a collapsed quote. */
  show_thinking?: boolean
  /** Speak each answer as a voice/audio message alongside the text. */
  voice_replies?: boolean
  // Forum per-topic config. chat_ids are negative supergroup ids as strings.
  allow_forum?: boolean
  allowed_forum_chat_ids?: string[]
  /** When to answer inside an allow-listed topic: "always" | "mention" | "off". */
  forum_activation?: string
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Writable Discord config fields sent to PUT /api/discord/config. */
export interface DiscordConfigSave {
  bot_token: string
  bot_token_clear: boolean
  enabled: boolean
  allowed_user_ids: string[]
  allowed_thread_ids: string[]
  allowed_channel_ids: string[]
  auto_thread: boolean
  soft_threshold_pct: number
  reactions_enabled: boolean
  show_thinking: boolean
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Writable Telegram config fields sent to PUT /api/telegram/config. */
export interface TelegramConfigSave {
  bot_token: string
  bot_token_clear: boolean
  enabled: boolean
  allowed_user_ids: string[]
  soft_threshold_pct: number
  show_thinking?: boolean
  voice_replies?: boolean
  allow_forum?: boolean
  allowed_forum_chat_ids?: string[]
  forum_activation?: string
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** WeCom config as returned by GET /api/wecom/config (secrets masked). */
export interface WeComConfigData {
  connected: boolean
  connect_error: string
  configured: boolean
  read_only: boolean
  /** Primary secret slot = WECOM_SECRET. */
  bot_token_set: boolean
  bot_token_preview: string
  /** Second credential slot = WECOM_BOT_ID. */
  bot_id_set: boolean
  bot_id_preview: string
  enabled: boolean
  allowed_user_ids: string[]
  /** Explicit opt-in: every org member may DM the bot (allow-list bypassed). */
  allow_all_users: boolean
  soft_threshold_pct: number
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Writable WeCom config fields sent to PUT /api/wecom/config. */
export interface WeComConfigSave {
  bot_token: string
  bot_token_clear: boolean
  bot_id: string
  bot_id_clear: boolean
  enabled: boolean
  allowed_user_ids: string[]
  allow_all_users: boolean
  soft_threshold_pct: number
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Feishu (飞书/Lark) config as returned by GET /api/feishu/config (secrets masked). */
export interface FeishuConfigData {
  /** Receiver-thread liveness, not a credential probe — see DashboardState. */
  connected: boolean
  connect_error: string
  configured: boolean
  read_only: boolean
  /** Primary secret slot = FEISHU_APP_SECRET. */
  bot_token_set: boolean
  bot_token_preview: string
  /** Second credential slot = FEISHU_APP_ID. */
  bot_id_set: boolean
  bot_id_preview: string
  enabled: boolean
  /** Stored as feishu.allowed_open_ids; the shared panel's user allow-list. */
  allowed_user_ids: string[]
  /** Whether group conversations are served at all (fails closed). */
  allow_group: boolean
  allowed_group_ids: string[]
  soft_threshold_pct: number
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
  /**
   * Whether lark-oapi (the optional [feishu] extra) is importable by the gateway
   * process. False means the channel is skipped at boot however complete the
   * rest of this config is.
   */
  sdk_installed?: boolean
  /** False where a pip install cannot work: bundled app, no pip, PEP 668. */
  sdk_install_supported?: boolean
  /** Install command naming the gateway's OWN interpreter; "" when not useful. */
  sdk_install_command?: string
}

/** Writable Feishu config fields sent to PUT /api/feishu/config. */
export interface FeishuConfigSave {
  bot_token: string
  bot_token_clear: boolean
  bot_id: string
  bot_id_clear: boolean
  enabled: boolean
  allowed_user_ids: string[]
  allow_group: boolean
  allowed_group_ids: string[]
  soft_threshold_pct: number
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Webex config as returned by GET /api/webex/config (secret masked). */
export interface WebexConfigData {
  connected: boolean
  connect_error: string
  configured: boolean
  read_only: boolean
  bot_token_set: boolean
  bot_token_preview: string
  enabled: boolean
  allowed_emails: string[]
  /** Answer in group spaces as well as DMs. Off by default: a reply in a space is
   *  visible to every member, including people not on the email allow-list. */
  allow_group_rooms: boolean
  /** Spaces the bot may answer in. Empty = deny all, so the switch alone grants nothing. */
  allowed_room_ids: string[]
  /** Reply under the message's own thread when it has one. */
  reply_in_thread: boolean
  /** Context % at which the bot suggests /compact instead of auto-compacting. */
  soft_threshold_pct: number
  /** Context % at which it force-compacts so the window never overflows. */
  hard_threshold_pct: number
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Writable Webex config fields sent to PUT /api/webex/config. */
export interface WebexConfigSave {
  bot_token: string
  bot_token_clear: boolean
  enabled: boolean
  allowed_emails: string[]
  allow_group_rooms: boolean
  allowed_room_ids: string[]
  reply_in_thread: boolean
  soft_threshold_pct: number
  hard_threshold_pct: number
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/**
 * iMessage channel status + config, from GET /api/imessage/config.
 *
 * The only channel payload with no credential in it: the transport is the
 * operator's own Messages.app, so there is nothing to mask or rotate.
 */
export interface IMessageConfigData {
  connected: boolean
  connect_error: string
  configured: boolean
  read_only: boolean
  /** False off macOS, where there is no iMessage to reach. */
  supported: boolean
  enabled: boolean
  db_path: string
  allowed_handles: string[]
  service: string
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Writable iMessage config fields sent to PUT /api/imessage/config. */
export interface IMessageConfigSave {
  enabled: boolean
  db_path: string
  allowed_handles: string[]
  service: string
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Microsoft Teams channel status + config, from GET /api/teams/config. */export interface TeamsConfigData {
  connected: boolean
  connect_error: string
  configured: boolean
  read_only: boolean
  app_id_set: boolean
  app_password_set: boolean
  enabled: boolean
  /** Azure AD tenant id for a single-tenant bot; "" = multi-tenant. Not a secret. */
  tenant_id: string
  allowed_emails: string[]
  /**
   * Whether PyJWT is importable in the gateway's environment. The inbound Bot
   * Framework webhook validates a signed JWT, so the channel refuses to start
   * without it and the panel has to say so — optional because a gateway that
   * predates the field sends none, and absent must not read as false.
   */
  jwt_available?: boolean
  /** Context percentage at which the channel nudges the user to compact. */
  soft_threshold_pct?: number
  /** Context percentage at which the channel compacts without being asked. */
  hard_threshold_pct?: number
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Weixin (iLink personal WeChat) config from GET /api/weixin/config.
 *  There is no credential field: the bot credential is obtained through the QR
 *  login flow and stored server-side, so the client only sees status. */
export interface WeixinConfigData {
  connected: boolean
  connect_error: string
  configured: boolean
  read_only: boolean
  credential_set: boolean
  enabled: boolean
  account_id: string
  dm_policy: string
  allowed_user_ids: string[]
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Writable Teams config fields sent to PUT /api/teams/config. The secret
 *  (app_password) is write-only and stored in .env, never config.json. */
export interface TeamsConfigSave {
  app_id: string
  app_password: string
  app_password_clear: boolean
  tenant_id: string
  enabled: boolean
  allowed_emails: string[]
  /**
   * Context thresholds, as whole percentages in 1..100 with
   * `hard_threshold_pct >= soft_threshold_pct`. The backend answers 400 with a
   * machine-readable `code` when the pair violates that, so the panel checks it
   * client-side first.
   */
  soft_threshold_pct: number
  hard_threshold_pct: number
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** Writable Weixin config fields sent to PUT /api/weixin/config. */
export interface WeixinConfigSave {
  enabled: boolean
  dm_policy: string
  allowed_user_ids: string[]
  disconnect: boolean
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

/** One opted-in WhatsApp group, as stored in config and edited in the panel. */
export interface WhatsAppGroup {
  /** The group JID (e.g. 1203...@g.us). */
  jid: string
  /** Human label shown in the editor (from get_joined_groups or typed in). */
  name: string
  /** How the agent participates: only when @-mentioned, when its rules say it
   *  can help, or off (opted out while kept in the list). */
  mode: 'mention' | 'rules' | 'off'
  /** Free-text rules injected when mode='rules' — when the agent may speak. */
  rules: string
  /** Minimum seconds between agent replies in this group (anti-flood). */
  cooldown_s: number
}

/** WhatsApp (personal account, QR-paired via neonize) config from
 *  GET /api/whatsapp/config. There is no credential field: pairing is done by
 *  QR scan and the session lives server-side in the neonize SQLite store, so
 *  the client only ever sees connection status + policy. */
export interface WhatsAppConfigData {
  configured: boolean
  connected: boolean
  connect_error: string
  read_only: boolean
  enabled: boolean
  /** Who may DM the agent: only the linked number (self), an allow-list, anyone
   *  (open), or nobody (disabled). */
  dm_policy: 'self' | 'allowlist' | 'open' | 'disabled'
  /** Allowed WhatsApp numbers (digits only, no @-suffix) when dm_policy is
   *  'allowlist'. Empty = deny all (fail closed). */
  allowed_wa_ids: string[]
  groups: WhatsAppGroup[]
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
  /** Pairing/connection lifecycle: unpaired → pairing → connected, or a terminal
   *  logged_out / banned / error. Drives the status badge. */
  state: 'unpaired' | 'pairing' | 'connected' | 'logged_out' | 'banned' | 'error'
}

/** Writable WhatsApp config fields sent to PUT /api/whatsapp/config. */
export interface WhatsAppConfigSave {
  enabled: boolean
  dm_policy: 'self' | 'allowlist' | 'open' | 'disabled'
  allowed_wa_ids: string[]
  groups: WhatsAppGroup[]
  /** Sidebar folder this channel's sessions are filed into ("" = off, the default). */
  session_folder?: string
}

export function createMessagingEndpoints({ get, post, put, j }: ClientTransport) {
  const channelConfigs = {
    // Slack integration config
    getSlackConfig: () => get('/api/slack/config').then(j) as Promise<SlackConfigData>,
    getSlackManifest: () => get('/api/slack/manifest').then(j) as Promise<{ alias: string; manifest: string; create_url: string }>,
    saveSlackConfig: (body: Partial<SlackConfigSave>) => put('/api/slack/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>,
    // Discord integration config
    getDiscordConfig: () => get('/api/discord/config').then(j) as Promise<DiscordConfigData>,
    saveDiscordConfig: (body: Partial<DiscordConfigSave>) => put('/api/discord/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>,
    // Telegram integration config
    getTelegramConfig: () => get('/api/telegram/config').then(j) as Promise<TelegramConfigData>,
    saveTelegramConfig: (body: Partial<TelegramConfigSave>) => put('/api/telegram/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>,
    getWeComConfig: () => get('/api/wecom/config').then(j) as Promise<WeComConfigData>,
    getFeishuConfig: () => get('/api/feishu/config').then(j) as Promise<FeishuConfigData>,
    saveFeishuConfig: (body: Partial<FeishuConfigSave>) => put('/api/feishu/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>,
    saveWeComConfig: (body: Partial<WeComConfigSave>) => put('/api/wecom/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>,
    // Webex integration config
    getWebexConfig: () => get('/api/webex/config').then(j) as Promise<WebexConfigData>,
    saveWebexConfig: (body: Partial<WebexConfigSave>) => put('/api/webex/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>,
    // iMessage — no credential to send or mask; the transport is the operator's
    // own Messages.app on this machine.
    getIMessageConfig: () => get('/api/imessage/config').then(j) as Promise<IMessageConfigData>,
    saveIMessageConfig: (body: Partial<IMessageConfigSave>) => put('/api/imessage/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>,
    // Effective per-channel governance policy decision: { slack: true, discord: false, ... }
    // (true = permitted, false = denied by the `channels` policy, null = governance
    // evaluation transiently failed → shown as "unavailable", NOT "Off by admin").
    // All-true when no policy governs channels (standard build). Drives the Settings
    // channel-tab "Off by admin" greying — the editable panel is replaced by a
    // disabled/unavailable state.
    getGovernanceChannels: () => get('/api/governance/channels').then(j) as Promise<Record<string, boolean | null>>,
    getTeamsConfig: () => get('/api/teams/config').then(j) as Promise<TeamsConfigData>,
    saveTeamsConfig: (body: Partial<TeamsConfigSave>) => put('/api/teams/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>,
    // Weixin (iLink personal WeChat) — QR login flow. The bot credential is
    // written server-side; the client only ever sees connection status.
    getWeixinConfig: () => get('/api/weixin/config').then(j) as Promise<WeixinConfigData>,
    saveWeixinConfig: (body: Partial<WeixinConfigSave>) => put('/api/weixin/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean }>,
    weixinQrStart: () => post('/api/channels/weixin/qr/start', {}).then(j) as Promise<{ session_id: string; qrcode_img_content: string; error?: string }>,
    weixinQrStatus: (sessionId: string) => get(`/api/channels/weixin/qr/status?session_id=${encodeURIComponent(sessionId)}`).then(j) as Promise<{ status: string; connected?: boolean; account_id?: string; error?: string }>,

    // WhatsApp (personal account, QR-paired via neonize) — QR pairing flow. The
    // session lives server-side in the neonize SQLite store; the client only ever
    // sees connection status + policy, never a credential.
    getWhatsAppConfig: () => get('/api/whatsapp/config').then(j) as Promise<WhatsAppConfigData>,
    saveWhatsAppConfig: (body: Partial<WhatsAppConfigSave>) => put('/api/whatsapp/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean }>,
    // `state` is the live client's pairing state, and it is the only authority on
    // whether a rotating code exists: the endpoint REPORTS pairing rather than
    // starting it (pairing begins inside the channel's own connect()), so a caller
    // that ignores this field renders a wait for a code that will never arrive.
    whatsAppQrStart: () => post('/api/channels/whatsapp/qr/start', {}).then(j) as Promise<{ ok: boolean; state?: string; error?: string }>,
    whatsAppQrStatus: () => get('/api/channels/whatsapp/qr/status').then(j) as Promise<{ state: string; qr_data_url: string | null; detail: string }>,
    // Two distinguishable successes: a bare `ok` means the device is unlinked and
    // the local session is gone, while `code: 'session_file_kept'` means the device
    // IS unlinked but the store holding its keys survived. A refused logout is an
    // ApiError(502) carrying `code: 'logout_failed'`, the device is still linked
    // there, and the session is kept deliberately so a retry is possible.
    whatsAppUnlink: () => post('/api/channels/whatsapp/unlink', {}).then(j) as Promise<{ ok: boolean; warning?: string; code?: string }>,
    getWhatsAppGroups: () => get('/api/whatsapp/groups').then(j) as Promise<{ groups: { jid: string; name: string }[] }>,
  }

  return { channelConfigs }
}
