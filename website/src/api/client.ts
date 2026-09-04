import { resizeImageForModel, type ResizeInfo } from '../utils/resizeImage'
import type {
  AppContributor,
  ChatSlot,
  IssueSource,
  McpApplyChange,
  PullRequestCheck,
  PullRequestSource,
  PullRequestStatusBatch,
  PublishProviderDescriptor,
  SessionDoc,
  SessionInventoryDetail,
  SessionInventoryList,
  SessionLaneKey,
  SessionStorageCleanup,
  SessionStorageEmptyJob,
  SessionStorageReport,
  ReviewFixActionRequest,
  ReviewFixTaskResponse,
  SessionTrashResult,
  UpdateCheckResult,
  WorkflowRunSummary,
} from '../types'
import type { RemoteCrewCapabilities } from '../hooks/useRemoteCapabilities'
import { ApiError, friendlyErrText } from './apiError'
import { refreshOnce, __resetRefreshOnceForTests } from './refreshOnce'
import {
  STALE_OWNER_SESSION_CODE,
  installStaleOwnerHandler,
  noteStaleOwnerResponse,
} from './staleOwnerSignal'
import { beginArtifactWrite, endArtifactWrite } from '../lib/artifactWrites'
import { withDeadline } from '../lib/withDeadline'

/** Deadline for `api.skills`. Measured on one host inside twenty minutes: 0.62s
 *  healthy, then 9.76s, 41.41s, 168.71s for a byte-identical payload. 15s clears
 *  the 9.76s case that still completed and cuts off the pathological ones.
 *  Rationale in the CR description. */
export const SKILLS_TIMEOUT_MS = 15_000

/** Deadline for `api.slashCommands`. Same value as the `$`-menu's above, for the
 *  same reason and against the same gateway: both menus are composer affordances
 *  whose fetch blocks Enter while it is unsettled, so a divergent bound here
 *  would only be a second number to explain. Rationale in the CR description. */
export const SLASH_COMMANDS_TIMEOUT_MS = 15_000
import { installApiTransport } from './apiTransport'
import type { SessionSummary } from '../types/sessionSummary'
import { queryClient } from './queryClient'
import { getStoredConsent } from '../utils/themeConsent'
import { recordError, parseErrorCode, requestPath } from '../utils/errorReport'
import { i18nT } from '../i18n/t'
import { normalizeInstalledApp, normalizeInstalledApps } from '../components/appstore/types'
import { TAB_ID } from './tabId'

/**
 * Resolve the theme-consent token to transmit for an installed pack's chat.
 *
 * Two-tier consent, wire side: the client does not compute a trust boolean —
 * it just transmits the RAW stored grant (the sha256 the user granted for the
 * persona content they saw). The backend does the content-binding check: it
 * injects the persona only if this token equals sha256 of the persona.md it
 * reads, so a re-install that swaps persona.md (new sha) does not match the
 * stale grant and the never-consented persona is never injected.
 *
 * Installed/custom packs are keyed `custom-<slug>` in colorTheme (useTheme), so
 * slice(7) drops the `custom-` prefix to recover the slug. Returns null (field
 * omitted from the body) when there's nothing transmittable: no colorTheme, a
 * built-in theme, no stored grant, or a legacy `'1'`/`''` token (which must
 * re-prompt, never activate).
 */
function themeConsentSha(colorTheme?: string): string | null {
  if (!colorTheme || !colorTheme.startsWith('custom-')) return null
  const stored = getStoredConsent(colorTheme.slice('custom-'.length))
  if (stored === null || stored === '' || stored === '1') return null
  return stored
}

/** One machine-readable ground for a sharing verdict.
 *
 *  `code` is stable and is what the UI translates. `detail` is verbatim data
 *  from the server or the config (an env name, a capability path, a protocol
 *  version) and is deliberately NOT translated.
 */
/** Response of POST /api/reveal. When the backend cannot drive a file manager
 *  (a remote or headless session) it degrades to a clipboard copy: `copy` is the
 *  path to write. The caller (`revealOrOpen`) writes the clipboard silently — the
 *  affordance that routed here already promised a copy. */
type RevealResult = { copy?: string }

export type McpShareReason = {
  code: string
  detail: string
}

export interface WorkflowLineage {
  workflow_id: string
  revision: number
}

export interface WorkflowDefinitionRevision {
  revision: number
  source: string
  created_at: string
}

export interface WorkflowDefinition {
  schema_version: number
  id: string
  slug: string
  name: string
  description: string
  created_at: string
  updated_at: string
  revision: number
  format: 'python' | 'task-plan'
  source: string
  content_hash: string
  derived_from: WorkflowLineage | null
  revisions: WorkflowDefinitionRevision[]
}

export interface WorkflowDefinitionWrite {
  source: string
  format?: 'python' | 'task-plan'
  name?: string
  description?: string
  slug?: string
  derived_from?: WorkflowLineage | null
}
/** The gateway's advisory reading of whether a server's backend can be shared.
 *
 *  `strength` is the evidence tier, weakest first: `unknown`, `no_objection`,
 *  `declared`, `disqualified`, `refuted`. Only `declared` sets `recommendShare`,
 *  because finding nothing disqualifying is an absence of evidence rather than
 *  evidence of absence.
 *
 *  The wire object also carries a separate stub recommendation, which is not
 *  declared here: a TS type is structural, so the field costs a reader something
 *  and buys nothing until a component actually renders it.
 */
export type McpShareRecommendation = {
  strength: string
  // Two axes, not one verdict. `recommendStub` is the safe half — Kiro Crew's
  // stub in the path, backend still 1:1 with the session — while
  // `recommendShare` is the one that introduces co-tenancy. A bulk action has to
  // consult whichever one the global sharing switch makes true of a click.
  recommendStub: boolean
  recommendShare: boolean
  reasons: McpShareReason[]
}

/**
 * Where an operator-requested measurement pass got to.
 *
 * ``running`` is the only field a caller may branch on to decide whether to keep
 * polling: ``done`` and ``total`` are a readout, and both are 0 both before a
 * pass starts and when a pass found nothing to measure. ``error`` names the
 * exception class of a pass that stopped early, because a pass that dies
 * silently is indistinguishable from one that finished with nothing to do.
 */
export type McpMeasureProgress = {
  running: boolean
  // Servers attempted, which is what the progress line advances on.
  done: number
  // How many of those produced a verdict. Lower than `done` whenever a pre-flight
  // could not run, so any claim about the outcome is built from this one.
  measured: number
  total: number
  error?: string
}

export type McpManagedServer = {
  name: string
  stub: boolean            // effective: can_stub AND in_allowlist
  can_stub: boolean       // stdio AND not denylisted — a property of the server, not a choice
  in_allowlist: boolean    // present in config mcp_gateway.stub_servers
  entry_poolable: boolean  // some agent entry sets poolable:true — RETIRED, informational only
  agents: string[]         // agent configs that declare this server
  transport: string        // "stdio" (stubbable) or "http" (no stdio pipe to interpose on)
  denylisted: boolean      // in UNPOOLABLE_SERVERS — can never be pooled
  // True when stubbing this server cannot produce a SHARED backend anyway: the
  // rewriter leaves an env-declaring entry unwrapped rather than spawn a pooled
  // backend without a declared key. Optional for the same reason as
  // `recommendation` — an older gateway does not send it, and its absence must
  // read as "no obstacle known", not as an obstacle.
  pooling_blocked_by_env?: boolean
  // Optional because the field is only as old as the shareability detector: a
  // dashboard served from this build can be pointed at an older gateway (Make
  // Live to an earlier worktree), and a row with no verdict must read as "not
  // measured" rather than crash the table.
  recommendation?: McpShareRecommendation
}

export const SEARCH_MIN_CHARS = 2  // backend session search threshold (must match kiro_crew.history.SEARCH_MIN_CHARS)

/**
 * A Connections provider's approval-URL mint, as the card reads it.
 *
 * `idle` means no mint exists — distinct from `failed`, which is a mint that ran
 * and produced nothing. `oauth_url` is present only while `waiting`, and only
 * while the process holding the URL is alive: the backend reports `expired`
 * rather than serving a URL no redirect can be redeemed against.
 */
export interface ConnectionMintState {
  slug: string
  state: 'idle' | 'minting' | 'waiting' | 'granted' | 'failed' | 'expired'
  oauth_url?: string
  reason?: string
  /** Opaque id of the backend row, unique across gateway restarts as well as
   *  within one process. Reported so a row can be told apart from its
   *  successor for the same provider. */
  token?: string
}

/**
 * A provider's authorization verdict from GET /api/connections/status.
 *
 * This is the AUTHORIZATION axis only: `grantPresent` says whether kiro-cli
 * holds an OAuth grant. Endpoint reachability is a separate axis carried by the
 * `/api/mcp` server status — the two together are what let the card tell a
 * provider authorized outside the dashboard (grant present, probe answers 401)
 * from one never authorized (no grant, same 401). `connectedSince` is a
 * persisted first-authorization timestamp, present only while a grant exists.
 */
export interface ConnectionStatus {
  slug: string
  status: 'connected' | 'awaiting_consent' | 'not_connected'
  reason?: string
  grantPresent: boolean
  /** True when the grant lookup itself failed, so `grantPresent: false` means
   *  "could not look" rather than "absent". */
  grantIndeterminate?: boolean
  connectedSince?: string
}

/** Authenticated provider-tool verdict returned by POST /api/connections/test. */
export interface ConnectionTestResult {
  schema_version: number
  slug: string
  verdict: 'usable' | 'no_tools' | 'failed'
  code: string
  toolCount: number
}

/**
 * A single task-runner plan step as sent to the server. Known fields are
 * typed; the payload is forwarded verbatim, so extra fields are permitted via
 * the index signature.
 */
export interface PlanStepInput {
  title?: string
  description?: string
  depends_on?: number[]
  requires_approval?: boolean
  [key: string]: unknown
}

/** Final payload resolved by installFromRegistryStream's SSE `done` event. */
export interface InstallStreamResult {
  ok?: boolean
  error?: string
  /**
   * Machine-readable failure code. The registry install path checks the
   * execution gate BEFORE cloning, so a third-party install can be refused with
   * `app_execution_denied` — and because the stream RESOLVES that refusal (SSE
   * `done`) instead of rejecting, the code has to travel on the result for the
   * consent modal to open at all.
   */
  code?: string
  needsClientInstall?: boolean
  clientInstall?: { shell?: string; postInstall?: string }
}


/**
 * The Playwright CLI browser view, as reported by `GET /api/browser/view` and
 * returned again by `POST /api/browser/view/start`.
 *
 * The CLI serves its own dashboard over loopback HTTP (`show --port`), which
 * already carries the session grid, live screencast, tab bar and full remote
 * mouse/keyboard input — so the dashboard's Browser panel frames that URL rather
 * than assembling a picture from pushed screenshot frames.
 *
 * Three states, and the UI must be able to tell them apart:
 *   • `running`     — `url` and `port` are set; frame it.
 *   • `stopped`     — installed but no view server up; a start is worth offering.
 *   • `unavailable` — it cannot run here at all (CLI not installed, unsupported
 *                     host). `reason` says why, in words meant for a human.
 *
 * `reason` is server-authored prose, so it is rendered VERBATIM and never
 * translated: inventing a catalog key for it would either drop the detail or
 * assert a cause the server did not report. A null `reason` is the caller's cue
 * to fall back to its own generic (translated) copy.
 */
export interface BrowserInstallData {
  installed: boolean
  cli_path: string | null
  cli_version: string | null
  node_ok: boolean
  node_version: string | null
  browser_ok: boolean
  installing: boolean
  last_error: string | null
  token: boolean
  /** Per-engine download state, keyed by engine name (chromium/firefox/webkit).
   *  Optional so an older gateway that predates it degrades to "unknown" rather
   *  than rendering every engine as missing. */
  browsers?: Record<string, boolean>
  /** The OS-appropriate standalone installer command, composed by the gateway
   *  because only it knows which OS it runs on. Offered when Node blocks the
   *  in-app install. Optional so an older gateway simply shows nothing extra
   *  rather than rendering `undefined`. */
  standalone_install?: string
}

export interface BrowserViewData {
  status: 'running' | 'stopped' | 'unavailable'
  url: string | null
  port: number | null
  reason: string | null
}

/** ADVISORY macOS permission rows. Never a gate — macOS attributes a TCC grant
 * to the responsible parent process, so `missing` can coexist with a working
 * capture, and `unknown` means the probe could not be run. */
export interface ComputerUsePermissions {
  accessibility: string
  screen_recording: string
  responsible_hint: string
}

/** Computer-use config as returned by GET /api/computer-use/config.
 *
 * `enabled` comes from the keystone `computer_use.json`, not `config.json`; the
 * numeric fields are the config.json budgets. There is deliberately no
 * `read_only`/governance-lock field — computer use is one operator opt-in with no
 * `computer_use*` governance scope, so nothing can forbid it and there is nothing to
 * grey out. An unsupported platform is the separate `supported: false` branch. */
export interface ComputerUseConfigData {
  enabled: boolean
  supported: boolean
  platform: string
  reason: string
  max_tree_nodes: number
  max_tree_depth: number
  text_limit: number
  attach_screenshot: boolean
  screenshot_max_px: number
  screenshot_jpeg_quality: number
  /** Draw a visible cursor gliding to each real-pointer target. macOS only. */
  cursor_motion: boolean
  /** False off macOS, where there is no overlay to draw — the row is hidden. */
  cursor_motion_supported: boolean
  allowed_apps: string[]
  extra_denied_apps: string[]
  /** Non-empty ONLY when the keystone's policy could not be parsed. The two lists
   *  above are then empty because they were unreadable — not because no restriction
   *  is configured — and the panel must be able to tell those apart. The GET
   *  deliberately still succeeds in that case: a hand-edited keystone used to 500
   *  this endpoint, which made the only UI that can repair the file unreachable. */
  policy_error?: string
  permissions: ComputerUsePermissions
  limits: Record<string, [number, number]>
  /** Sessions restarted by the last PUT so kiro-cli re-reads the tool list.
   *  Only ever non-zero on a save that FLIPPED `enabled` (see the handler);
   *  absent on GET. */
  sessions_reset?: number
}

/** Writable computer-use fields sent to PUT /api/computer-use/config. */
export interface ComputerUseConfigSave {
  enabled: boolean
  max_tree_nodes: number
  max_tree_depth: number
  text_limit: number
  attach_screenshot: boolean
  screenshot_max_px: number
  screenshot_jpeg_quality: number
  cursor_motion: boolean
  allowed_apps: string[]
  extra_denied_apps: string[]
}

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

/** A built-in denied-command rule as returned by GET /api/security/denied-commands. */
export interface DeniedCommandRule {
  id: string
  pattern: string
  category: string
  description: string
  enabled: boolean
  pinned: boolean
  /** Why the rule is locked (forced-on, non-toggleable): 'floor' = enforced by
   *  an always-on floor built into Kiro Crew; 'policy' = governance-pinned;
   *  null/absent = freely toggleable. Additive — `pinned` keeps its
   *  governance-only meaning. */
  lock_reason?: 'floor' | 'policy' | null
  /** Where the rule came from: 'builtin' = shipped in Kiro Crew's catalog;
   *  'edition' = contributed by a composed edition through the `denied_rules`
   *  seam. Edition rules are default-on and freely toggleable (never locked),
   *  and are grouped by their own `category`, so the panel needs no special
   *  case. Optional so an older gateway's response still type-checks. */
  source?: 'builtin' | 'edition'
}

/** A user-authored denied-command pattern. */
export interface DeniedUserRule {
  id: string
  pattern: string
  enabled: boolean
  /** Operator prose shown in the refusal when this rule fires. Optional: rules
   *  added before the field existed, and rules added without one, omit it. */
  note?: string
}

/** What a paid AWS service would bill, and whether the operator confirmed it. */
export interface AwsConsentStatus {
  service: string
  serviceLabel: string
  /** Configured profile name. Empty means the provider's own default chain. */
  profile: string
  /** Human-readable rendering of `profile` for display. */
  credentialSource: string
  region: string
  account: string
  arn: string
  identityResolved: boolean
  identityDetail: string
  granted: boolean
  /** Operator-facing explanation when `granted` is false. */
  reason: string
  /** True when this GET withdrew a stale grant because the account changed. */
  revokedOnAccountChange: boolean
  grant: { account: string; region: string; profile: string; granted_at: string } | null
}

/** Full denied-commands snapshot returned by every denied-commands endpoint. */
export interface DeniedCommandsData {
  builtins: DeniedCommandRule[]
  user_added: DeniedUserRule[]
  disable_all: boolean
  effective_count: number
  governance_locked: boolean
}

/** Serialized effective control for one governed scope (archetype-specific). */
export interface GovernanceScopeDetail {
  /** ruleset / scopedmap-members / capability-inner: the set MODE plus how many
   *  entries it holds. POSTURE ONLY — the endpoint deliberately never sends the
   *  rule CONTENTS (allow/deny globs, command patterns) to the browser, because
   *  the dashboard is reachable by the agent's own Playwright tooling and the
   *  exact deny patterns are the security ceiling the agent is fenced from. The
   *  human operator reads the authoritative rules from the policy files directly. */
  mode?: string
  allow_count?: number
  deny_count?: number
  /** ruleset intersection (allow∩ that can't flatten): the composed halves. */
  components?: GovernanceScopeDetail[]
  /** ordinal: the enforced floor value + its strictness scale. */
  scale?: string
  floor?: string
  /** capability: on/off + inner allowlists (e.g. spawn→agents). */
  enabled?: boolean
  inner?: Record<string, GovernanceScopeDetail>
  /** scopedmap (channels): allowed members + per-member posture. */
  members?: GovernanceScopeDetail
  posture?: Record<string, Record<string, GovernanceScopeDetail>>
}

/** One row of the effective governance ceiling for a single scope. */
export interface GovernanceScope {
  scope: string
  archetype: 'ruleset' | 'ordinal' | 'capability' | 'scopedmap'
  /** false = neither policy nor profile governs it → the scope permits. */
  governed: boolean
  source: 'policy' | 'profile' | 'policy+profile' | 'ungoverned'
  /** WHOSE ceiling this row describes, so a host-only pin is not read as
   *  install-wide. `host_profile` = the host-surface profile contributes, so the
   *  value is that ONE surface's posture (the host profile disables cron and
   *  messaging because the host process performs neither; the cron and messaging
   *  surfaces enable them under their own profiles). `policy_wide` = policy alone
   *  governs, which applies to every surface. Absent/'' = ungoverned. */
  scope_note?: '' | 'host_profile' | 'policy_wide'
  detail: GovernanceScopeDetail
}

/** Central-policy-distribution posture, from the `distribution` key of
 *  GET /api/governance/policy.
 *
 *  POSTURE ONLY, and one field more strictly than the rest of that payload: the
 *  producer reports the source's SCHEME and never its URL, because the endpoint is
 *  the fleet's control plane and the dashboard is reachable by the agent's own
 *  browser tooling. Every value is a number, a boolean, or a machine-readable enum,
 *  so the dashboard renders translated text and no English arrives in the JSON body.
 *
 *  Only `configured` is guaranteed: the producer's fail-safe path answers
 *  `{configured: false}` alone when the posture itself could not be resolved, so
 *  every other field is optional and a reader must treat absence as unknown rather
 *  than as a zero. */
export interface GovernanceDistributionData {
  /** Whether this host fetches its ceiling from a central source at all. False
   *  also covers "could not tell" — pair it with `error_code`. */
  configured: boolean
  /** URL scheme of the source (`https`, `file`, `http`), never the URL. '' when
   *  unconfigured. */
  source_scheme?: string
  /** Seconds between background re-fetches, already clamped to the polling floor.
   *  0 = fetched at boot only. */
  refresh_interval_seconds?: number
  /** The fleet's staleness bound on the cached copy. 0 = no bound. */
  max_cache_age_seconds?: number
  /** What the host does when no ceiling can be established: refuse to start, or
   *  fall through to the next policy tier and report a governance incident. */
  on_unavailable?: 'fail_closed' | 'degrade' | ''
  /** Whether the background poll loop is alive in this process. */
  refresher_running?: boolean
  /** Whether a last-known-good copy recorded against THIS source is on disk. */
  cache_present?: boolean
  /** Age of that copy in seconds; null when there is none. */
  cache_age_seconds?: number | null
  /** Outcome of the most recent background refresh. '' before the first one. */
  last_refresh_status?: 'not_configured' | 'unchanged' | 'applied' | 'rejected' | 'unreachable' | ''
  /** Seconds since that refresh; null when none has run. */
  last_refresh_age_seconds?: number | null
  /** Why the posture could not be resolved, as an enum the dashboard maps to
   *  translated copy. '' when nothing went wrong. */
  error_code?: 'misconfigured' | ''
}

/** GET /api/governance/policy — the read-only effective ceiling across scopes. */
export interface GovernancePolicyData {
  /** Policy schema version, or null when no enterprise ceiling is present. */
  version: number | null
  /** Whether a Level-1 enterprise ceiling is in effect at all. */
  has_policy: boolean
  /** The bound host-surface profile name, or null. */
  profile: string | null
  /** The surface this snapshot resolved (always "host"); narrower per-surface/
   *  app/task profiles can tighten a scope further at runtime. */
  surface?: string
  /** Surfaces OTHER than host that carry their own bound profile — names only.
   *  Rendered so a reader can see that a host row's "disabled" is one surface's
   *  posture, not the whole install's. */
  other_bound_surfaces?: string[]
  /** True when the resolved profile is a deny-all fallback because the file
   *  could not be read or parsed — enforcement is correct (fail-closed) but the
   *  operator should know the ceiling is synthetic, not intentional. */
  fallback_profiles?: string[]
  /** Capability scopes a profile names that this build does not register —
   *  typically scopes a companion edition adds, though a misspelled scope key
   *  lands here too. Keyed by profile stem, sorted scope names as values;
   *  present only for profiles carrying such scopes, and deliberately NOT
   *  narrowed to the host profile — every loaded profile reports. Producer:
   *  the governance security payload (PR #5544). Tolerated at load time and
   *  inert in this build. */
  unknown_profile_scopes?: Record<string, string[]>
  /** Where the ceiling itself comes from, when a central source publishes it.
   *  Present on BOTH the normal and the fail-safe snapshot, so a host whose first
   *  fetch has not landed — `has_policy: false`, `profile: null` — can still be told
   *  apart from a host that has no enterprise ceiling at all. */
  distribution?: GovernanceDistributionData
  /** True when governance resolution failed — the viewer shows a soft notice. */
  unavailable: boolean
  scopes: GovernanceScope[]
}

/** One concrete element behind a security-posture count. */
export interface PostureItem {
  /** What the control covers — a blocked path, a redaction sink, a credential family. */
  label: string
  /** Optional "how/where" secondary text. */
  detail: string
}

/** One expandable security control from GET /api/security/posture.
 *
 *  POSTURE ONLY, by the same contract as the governance viewer: items are public
 *  control definitions (blocked path patterns, redaction sink modules, credential
 *  FAMILY names) and derived counts — never credential material, governance rule
 *  contents, or user data. `count` is `items.length` server-side, so a pill can
 *  never drift from the list it summarizes. */
export interface PostureControl {
  key: string
  label: string
  /** Noun for the count, e.g. "output paths" — rendered as `${count} ${unit}`. */
  unit: string
  summary: string
  /** Repo-relative path of the module that enforces the control. */
  source: string
  /** null when the control could not be resolved (see `unavailable`). */
  count: number | null
  items: PostureItem[]
  /** True when this control's detail could not be resolved; the rest still render. */
  unavailable: boolean
}

/** GET /api/security/posture — expandable detail behind every posture count. */
export interface SecurityPostureData {
  controls: PostureControl[]
  /** Flat `key → count` map for callers that only need the pill values. */
  counts: Record<string, number | null>
}

/**
 * GET /api/tailnet/status — whether this machine's Tailscale MagicDNS name is in
 * the dashboard's Origin allow-list.
 *
 * `state` is derived SERVER-SIDE and the UI must render off it directly rather
 * than recomputing it from the other fields: one owner for the state machine
 * means the two layers cannot disagree about what "active" means. Precedence is
 * `pinned` > `off` > `unresolved` > `active`.
 *
 * `host` / `origin` / `resolved_at` describe the STARTUP resolution — the value
 * that actually went into `build_allowed_origins` — not a fresh probe. A live
 * probe could report a name the running origin set does not contain (daemon came
 * up after the gateway), and rendering that as trusted is the same
 * checked-but-never-ran defect the posture registry guards against.
 */
export interface TailnetStatusData {
  /** `dashboard.tailscale.enabled` as actually loaded, post-hydration. */
  enabled: boolean
  /** `capabilities.tailnet_origin` pinned off at the POLICY layer. */
  governance_pinned: boolean
  /** MagicDNS name resolved at startup; `''` when none was. */
  host: string
  /** `https://<host>`; `''` when `host` is `''`. */
  origin: string
  /** Epoch seconds of that startup resolution; `0` when it never resolved. */
  resolved_at: number
  state: 'pinned' | 'off' | 'unresolved' | 'active'
}

/** The single next action for tailnet mobile access.
 *
 * Ordered by what blocks what, and derived SERVER-side (see
 * `handlers/tailnet_mobile._derive_step`) so this list is rendered, never
 * re-computed here — one owner for the state machine.
 *
 * - `pinned` — an administrator's policy forbids tailnet access. Dead end.
 * - `install` / `start_daemon` / `sign_in` / `enable_magicdns` — the four ways
 *   there is no usable tailnet name, kept apart because each is a different
 *   errand for the operator.
 * - `enable_https` — the tailnet has not granted certificate provisioning for
 *   that name; this requires one-time tailnet administrator consent.
 * - `trust_off` — a name exists but the gateway will not accept it as an origin
 *   yet, so publishing would yield a reachable dashboard answering 403.
 * - `restart_gateway` — configured and resolvable NOW, but this server did not
 *   trust that exact name at startup. The one-click flow restarts and resumes.
 * - `occupied` — serve holds the mount for something that is not this dashboard,
 *   or its state is undeterminable; publishing would REPLACE it.
 * - `publish` — everything in place, one action left.
 * - `ready` — published and trusted.
 */
export type TailnetMobileStep =
  | 'pinned'
  | 'install'
  | 'start_daemon'
  | 'sign_in'
  | 'enable_magicdns'
  | 'enable_https'
  | 'trust_off'
  | 'restart_gateway'
  | 'occupied'
  | 'publish'
  | 'ready'

/** Live readiness for tailnet mobile access (`GET /api/tailnet/mobile`).
 *
 * Unlike `TailnetStatusData` this IS a live daemon probe: it answers "what can
 * this machine do next", where the other answers "what does the running server
 * already trust". Both are needed and they are not interchangeable. */
export interface TailnetMobileData {
  /** Per-process marker used only to prove a requested restart completed. */
  boot_id: string
  step: TailnetMobileStep
  /** MagicDNS name as resolved right now; `''` when unresolvable. */
  host: string
  origin: string
  installed: boolean
  reachable: boolean
  logged_in: boolean
  /** Other devices on this tailnet. `0` means there is nothing to reach this
   *  dashboard FROM — publishing and the QR both still succeed, so this is the
   *  only signal that the scan is going to fail. */
  peer_count: number
  /** How many of those are online right now. */
  peers_online: number
  /** `dashboard.tailscale.enabled` — the origin-trust config switch. */
  trusted: boolean
  /** Whether the RUNNING server trusted this exact name at startup. */
  startup_trusted: boolean
  /** `null` when serve state could not be determined — never render as false. */
  published: boolean | null
  keep_awake: boolean
  governance_pinned: boolean
  /** Verbatim daemon/serve text. Shown as-is; never rephrased client-side. */
  detail: string
  download_url: string
  qr_ttl_secs: number
  serve_port: number
  dashboard_port: number
}

/** Result of a publish/unpublish attempt. `detail` carries the daemon's own
 *  words, which is the only part guaranteed to stay correct if Tailscale
 *  rewords its errors. */
export interface TailnetMobileMutation {
  ok: boolean
  code: string
  detail: string
}

/** Phone-connection methods surviving the governance filter. `kind` names the
 *  renderer; the dashboard skips a kind it does not recognise, so an edition's
 *  new method degrades to absent on an older frontend, never to a broken panel. */
export interface MobileConnectMethodsData {
  methods: { id: string; kind: string }[]
}

/** Durable config established by the explicit mobile setup action. */
export interface TailnetMobileConfigure {
  /** Trust/origin settings are snapshotted by middleware at gateway boot. */
  restart_required: boolean
}

/** A minted mobile-access QR. Carries a LIVE session token in both fields, so
 *  it is fetched only on explicit user action and never cached. */
export interface TailnetMobileQr {
  /** `https://<host>/?token=<token>` — treat as a credential. */
  url: string
  /** PNG data URI, rendered server-side (no client QR library). */
  image: string
  /** Lifetime of the session the link opens. */
  ttl_secs: number
  /** Window in which the LINK must be opened — much shorter than `ttl_secs`,
   *  and the part that surprises people. */
  link_window_secs: number
  host: string
}

/**
 * GET /api/security/trusted-apps — per-app grants that let a third-party app
 * run its own code (Python in-process, its own backend, manifest shell
 * commands).  Third-party app code is refused by default; a grant is made for
 * ONE app at a time from the trust-consent modal, so `apps` is the explicit
 * allow list and `allowAll` is the separate blanket escape hatch.
 */
export interface TrustedAppsData {
  /** Grants the execution gate ACTUALLY enforces (valid app names, sorted). */
  apps: string[]
  /**
   * Entries stored in `config.json` that the gate IGNORES because they fail the
   * app-name charset — a hand-edited config can hold `LD-App`, `ld-app ` with a
   * trailing space, a fullwidth homoglyph, `..` or `*`. They must render
   * separately from `apps`: folded in, the panel claims trust that does not
   * exist and the user cannot tell why their app is still blocked.
   */
  ineffective: string[]
  /** Blanket grant — trusts every third-party app, present or future. */
  allowAll: boolean
}

/**
 * DELETE /api/security/trusted-apps/{name} — the refreshed snapshot PLUS whether
 * the revoke also had to DISABLE the app. Revoking trust has to stop the app's
 * code from running, so the backend disables a currently-enabled app in the same
 * transaction; the UI must say so, otherwise an app silently stops working.
 */
export interface TrustedAppsRevokeResult extends TrustedAppsData {
  disabled: boolean
}

/**
 * Whether THIS MACHINE has an ACP backend's components installed.
 *
 * `'unknown'` means the check itself failed, and it is NOT a synonym for
 * `'missing'`: telling someone to install what they may already have costs them a
 * global package install for nothing. Consumers must keep the three apart.
 */
export type AcpBackendInstalled = 'installed' | 'missing' | 'unknown'

/**
 * One row of `GET /api/acp-backends` — a backend the code knows about, paired
 * with what this build can serve and what this machine actually has.
 *
 * `selectable` is a BUILD/edition-and-policy fact (the same set
 * `PATCH /api/config/kirocrew` validates against); `installed` is a machine fact.
 * They are independent: a build can serve a backend whose binary is absent, and a
 * machine can hold a backend this build refuses to run.
 *
 * `missing_components` is non-empty only when `installed === 'missing'`;
 * `install_command` is `''` when there is nothing to suggest.
 */
export interface AcpBackendProbe {
  id: string
  policy_id: string
  selectable: boolean
  installed: AcpBackendInstalled
  missing_components: string[]
  install_command: string
  /**
   * Installed on disk, but the RUNNING gateway already resolved its absence and
   * cached that for the process's life — so a session started now would still
   * fail. Disables the option and says restart, rather than offering a control
   * that is guaranteed to error.
   */
  restart_required: boolean
}

let _sessionExpiredShown = false

/**
 * True while the banner on screen is the stale-owner variant. A separate latch
 * because that session is still AUTHENTICATED: ordinary polls keep succeeding,
 * so the `j` wrapper's clear-banner-on-2xx self-dismissal would remove the one
 * instruction that recovers the owner-gated surfaces. It clears via the
 * banner's own ✕ or the sign-in reload, never via a 2xx.
 */
let _staleOwnerBanner = false

/**
 * Synchronous getter so React components can read the auth-banner state on
 * mount (e.g. when the banner was already injected before the component
 * subscribed to the `mc-auth-required` / `mc-auth-cleared` events).
 */
export function isAuthBannerShown(): boolean {
  return _sessionExpiredShown
}

/**
 * Internal: fire a window-level CustomEvent so React components can react
 * to auth-banner state transitions. The banner itself is a vanilla DOM
 * element managed by this module; the events let consumers (e.g.
 * `ChatPage`) suppress redundant offline UI when auth is the real blocker.
 */
function _emitAuthEvent(kind: 'mc-auth-required' | 'mc-auth-cleared'): void {
  if (typeof window === 'undefined') return
  try { window.dispatchEvent(new CustomEvent(kind)) } catch { /* ignore */ }
}

/**
 * Clear the session-expired banner if it is currently shown.
 * Called automatically from the `j` response wrapper on any 2xx response so
 * the banner self-dismisses once auth is restored (e.g. via the in-banner
 * token-paste flow that reloads with `?token=X`, OR via a successful poll
 * after gateway restart wiped the session table).
 *
 * Idempotent: safe to call on every response.
 */
export function removeAuthBanner(): void {
  // A 2xx means auth works again — clear the terminal-refresh latch so a later
  // lapse retries silently instead of going straight to the banner.
  _silentRefreshExhausted = false
  // The stale-owner banner is exempt from the 2xx self-dismissal: that session
  // still authenticates for everything the owner gate does not front, so a
  // success proves nothing about the stale-subject denial.
  if (_staleOwnerBanner) return
  // Auth works again, so a LATER lapse in this same document deserves its own
  // hand-off. Deliberately after the stale-owner return above: that denial is not
  // disproved by an unrelated success, and re-asking the hub for it loops forever.
  _embeddedHandoffPosted = false
  if (!_sessionExpiredShown) return
  _sessionExpiredShown = false
  const el = document.getElementById('mc-session-expired')
  if (el) el.remove()
  _emitAuthEvent('mc-auth-cleared')
}

// Reactive warm-path recovery: background-poll 403s funnel here, through the
// shared single-flight refreshOnce(). True if the 30-day cookie rotated.
let _silentRefreshExhausted = false
// One hub hand-off per pane DOCUMENT. Without this every 403 from every
// background poll posts another `mc-auth-expired`, and the hub answers each one
// with an SSH token mint (rate-limited, never stopped) — a mint storm behind a
// loading spinner. A hub re-mint reloads this iframe, which resets this latch,
// so a pane that can recover still gets a fresh ask on every load.
let _embeddedHandoffPosted = false

/** Hand auth recovery to the hub, at most once per pane document.
 *
 * The wildcard target is deliberate and matches the two call sites' comments:
 * the hub's origin is not knowable from inside the pane (tunnel hosts vary), and
 * the message carries only a fixed type string — no secret — while the parent
 * validates `event.origin` before acting on it (see resolveTunnelOrigin).
 */
function postAuthExpiredToHub(): boolean {
  if (_embeddedHandoffPosted) return true
  try {
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    window.parent.postMessage({ type: 'mc-auth-expired' }, '*')
  } catch {
    return false // cross-origin parent unreachable — caller falls back to the banner
  }
  _embeddedHandoffPosted = true
  return true
}

export function attemptSilentRefresh(): Promise<boolean> {
  return refreshOnce().then((res) => {
    if (res.ok) {
      // Keep the scheduler's ['auth-me'] cache from holding a stale
      // pre-rotation session_exp after a warm-path recovery.
      void queryClient.invalidateQueries({ queryKey: ['auth-me'] })
      return true
    }
    // 401 = terminal (chain revoked / no cookie) → latch to banner; 5xx is transient.
    if (res.status === 401) _silentRefreshExhausted = true
    return false
  })
}

/** Test-only: reset module auth-recovery state between cases. */
export function __resetAuthRecoveryStateForTests(): void {
  _silentRefreshExhausted = false
  _embeddedHandoffPosted = false
  _sessionExpiredShown = false
  _staleOwnerBanner = false
  __resetRefreshOnceForTests()
  if (typeof document !== 'undefined') {
    document.getElementById('mc-session-expired')?.remove()
  }
}

function showSessionExpiredBanner(lead?: string): void {
  if (_sessionExpiredShown) return
  _sessionExpiredShown = true
  _emitAuthEvent('mc-auth-required')
  const el = document.createElement('div')
  el.id = 'mc-session-expired'
  el.style.cssText =
    'position:fixed;top:0;left:0;right:0;z-index:99999;background:#b91c1c;color:#fff;' +
    'padding:12px 20px;text-align:center;font:14px/1.5 system-ui;'
  const b = document.createElement('b')
  b.textContent = lead ?? i18nT('api.client.session_expired')
  const code = document.createElement('code')
  code.textContent = 'kirocrew token'
  code.style.cssText = 'background:#7f1d1d;padding:2px 6px;border-radius:4px'
  const input = document.createElement('input')
  input.type = 'text'
  input.placeholder = i18nT('api.client.paste_token_url_or_raw_token')
  input.style.cssText =
    'margin-left:12px;padding:4px 8px;border-radius:4px;border:1px solid #fca5a5;' +
    'background:#7f1d1d;color:#fff;font-size:13px;width:280px;cursor:text;caret-color:#fff;' +
    'outline:2px solid transparent;outline-offset:2px;transition:border-color 0.2s,box-shadow 0.2s;'
  input.addEventListener('focus', () => { input.style.borderColor = '#fff'; input.style.boxShadow = '0 0 0 3px rgba(255,255,255,0.25),0 0 20px rgba(255,255,255,0.1)' })
  input.addEventListener('blur', () => { input.style.borderColor = '#fca5a5'; input.style.boxShadow = 'none' })
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const v = input.value.trim()
      if (!v) return
      let t: string | null = null
      try { t = new URL(v).searchParams.get('token') } catch { t = v }
      if (t) window.location.href = `${window.location.protocol}//${window.location.host}?token=${encodeURIComponent(t)}`
    }
  })
  el.append(b, ' Run ', code, ' then paste URL: ', input)
  const dismiss = document.createElement('button')
  dismiss.textContent = '✕'
  dismiss.style.cssText =
    'margin-left:12px;background:none;border:none;color:#fca5a5;cursor:pointer;font-size:18px;vertical-align:middle;'
  dismiss.addEventListener('click', () => {
    el.remove()
    _sessionExpiredShown = false
    _staleOwnerBanner = false
    _emitAuthEvent('mc-auth-cleared')
  })
  el.append(dismiss)
  document.body.prepend(el)
  requestAnimationFrame(() => input.focus())
}

export function checkSessionExpired(r: Response): Response {
  if (r.status === 403 && r.headers.get('X-Auth-Required') === 'true' && !_sessionExpiredShown) {
    // When this dashboard is running embedded in the Instances pane stack
    // (an <iframe> inside the hub), don't show the paste-token banner here —
    // the user can't easily fetch the remote token from inside the pane, and
    // the hub owns recovery.
    //
    // But try OUR OWN recovery first. This pane holds the same 30-day
    // `mc_refresh_<port>` cookie the top-level path below uses, and the common
    // cause of a burst of 403s here is a lapsed access cookie (a laptop that
    // slept through the proactive refresh) — which one silent refresh fixes, with
    // no SSH mint, no iframe reload, and no lost pane state. Handing that to the
    // hub instead costs a remote mint and a full reload of this document.
    //
    // Only when the refresh cannot recover do we signal the parent, which
    // force-mints a fresh token and reloads this iframe. That hand-off is
    // latched to once per document (see postAuthExpiredToHub): the hub answers
    // every ask with a mint, so an unrepairable session used to produce one mint
    // per rate-limit window for as long as the window stayed open.
    if (window.parent && window.parent !== window) {
      if (!_silentRefreshExhausted) {
        void attemptSilentRefresh().then((ok) => {
          if (ok) removeAuthBanner()
          else postAuthExpiredToHub()
        })
        return r
      }
      if (postAuthExpiredToHub()) return r
      // Cross-origin parent unreachable — fall through to the banner below.
    }
    // Mid-session the access cookie can lapse (20h TTL, or laptop sleep
    // pausing the proactive refresh timer) while the tab stays open. The
    // background polls then 403 in a burst. Before showing the re-auth banner,
    // try a single-flight silent refresh with the still-valid 30-day cookie —
    // this recovers without ever showing the banner. Only banner if the
    // refresh can't recover (chain revoked / no refresh cookie).
    if (!_silentRefreshExhausted) {
      void attemptSilentRefresh().then((ok) => {
        if (ok) removeAuthBanner()
        else if (_silentRefreshExhausted) showSessionExpiredBanner()
      })
      return r
    }
    showSessionExpiredBanner()
  }
  return r
}

/**
 * Recovery prompt for the ONE denial the silent-refresh path can never clear:
 * a session whose token was minted before `KIROCREW_OWNER_ID` was configured.
 * `/api/auth/refresh` re-mints from the incoming subject, so a "successful"
 * refresh would rotate the cookie and keep the stale bootstrap subject — the
 * next owner-gated call is denied again, forever. Only a fresh sign-in (a new
 * token link, whose subject is derived from the now-configured owner) recovers,
 * so this goes straight to the banner instead of attempting a refresh.
 */
function handleStaleOwnerSession(): void {
  // Latch FIRST, even when a banner is already showing: a plain-expiry banner
  // raised moments earlier would otherwise keep its clear-on-2xx self-dismissal
  // and vanish on the next successful poll — this session still succeeds on
  // everything the owner gate does not front, so once the stale denial is seen
  // only the ✕ or a sign-in reload may clear the prompt.
  _staleOwnerBanner = true
  if (_sessionExpiredShown) return
  // Embedded in the Instances pane stack: hand recovery to the hub, mirroring
  // checkSessionExpired — the hub force-mints a fresh token (whose subject is
  // derived from the current owner) and reloads this iframe.
  // No silent refresh attempt here, unlike checkSessionExpired: re-minting from
  // the incoming subject keeps the stale bootstrap subject, so a "successful"
  // refresh would rotate the cookie and be denied again. The hand-off is latched
  // to once per document, and `_staleOwnerBanner` above keeps a later 2xx from
  // re-opening it — a hub re-mint cannot fix this denial either, so asking twice
  // only buys another SSH mint.
  if (typeof window !== 'undefined' && window.parent && window.parent !== window) {
    if (postAuthExpiredToHub()) return
    // Cross-origin parent unreachable — fall through to the banner below.
  }
  showSessionExpiredBanner(i18nT('api.client.stale_owner_session'))
}

// The signal module is a leaf shared with the direct-fetch surfaces (app-sdk,
// the MCP-app relay, Mochi's approval bridge); this module owns the banner, so
// it supplies the prompt those detections raise. Re-exported so consumers of
// the blessed transport can reference the wire contract from one place.
installStaleOwnerHandler(handleStaleOwnerSession)
export { STALE_OWNER_SESSION_CODE }

/**
 * `ApiError` and `friendlyErrText` now live in the side-effect-free
 * `api/apiError` module so app API clients can import them without pulling this
 * file's graph (queryClient, transport install, the error journal) into their
 * bundles. Re-exported here because this has always been their import path —
 * every existing consumer, and every test that mocks `../api/client`, is
 * unchanged by the move.
 */
export { ApiError, friendlyErrText }

/**
 * Whether *e* is a failure the user can only clear by signing back in.
 *
 * The gateway's auth denial names the cryptographic reason it rejected the
 * token (`invalid signature`, `session revoked`), which is accurate and
 * useless to a user: it neither says the session is what broke nor points at
 * the re-auth banner. Call sites use this to swap a futile retry for the one
 * action that recovers.
 */
export const isAuthExpiredError = (e: unknown): boolean =>
  e instanceof ApiError && e.authRequired

/**
 * Build the ApiError AND journal it.
 *
 * `j`/`jNullable` are the single chokepoint every dashboard API failure passes
 * through, which makes this the one place that can capture the full context
 * (status, path, backend `code`, raw body) before call sites collapse it to
 * `e.message`. `utils/errorReport` then lets a shared error banner recover that
 * context from the message alone — see AskAgentButton / ErrorNotice.
 */
const apiFailure = (r: Response, errText: string): ApiError => {
  // An auth denial's own reason text ("invalid signature") describes HMAC
  // verification, not anything the user can act on, and every card that renders
  // it hides the fact that one re-auth clears all of them at once. Substitute
  // the recovery instruction for display; the raw reason still travels in
  // `body` and in the error report's `detail` for diagnostics.
  const authRequired = r.status === 403 && r.headers.get('X-Auth-Required') === 'true'
  // The stale-owner signal is matched on status AND the backend's code — a
  // generic 401 (or any 403) keeps its current handling untouched. Detection
  // lives HERE rather than in checkSessionExpired because the code travels in
  // the BODY, which checkSessionExpired (a pre-body Response hook) cannot read;
  // the prompt itself is idempotent, so the factory raising it cannot spam.
  const staleOwnerSession = noteStaleOwnerResponse(r.status, errText)
  const message = staleOwnerSession
    ? i18nT('api.client.stale_owner_session_sign_in_again')
    : authRequired
      ? i18nT('api.client.session_expired_sign_in_again')
      : friendlyErrText(r.status, errText) || `HTTP ${r.status}`
  recordError({
    source: 'api',
    message,
    status: r.status,
    code: parseErrorCode(errText),
    endpoint: requestPath(r.url),
    detail: errText,
  })
  // A stale-owner denial is authRequired in the sense call sites care about:
  // no retry can succeed until the user signs in again.
  return new ApiError(r.status, message, errText, authRequired || staleOwnerSession)
}

const j = async (r: Response) => {
  checkSessionExpired(r)
  if (r.ok) removeAuthBanner()
  if (!r.ok) {
    const errText = await r.text()
    throw apiFailure(r, errText)
  }
  return r.json()
}

/**
 * Nullable variant of j(): preserves auth recovery + ApiError semantics but
 * returns null on 204 (No Content). Used by tips endpoints.
 */
const jNullable = async (r: Response) => {
  checkSessionExpired(r)
  if (r.ok) removeAuthBanner()
  if (r.status === 204) return null
  if (!r.ok) {
    const errText = await r.text()
    throw apiFailure(r, errText)
  }
  return r.json()
}
// X-Session-Key ensures the server-side ephemeral gate always runs.
// Without it, browser requests would skip the `if sk:` check — a fail-open
// path that an MCP subprocess could exploit by omitting its own header.
const _sk = { 'X-Session-Key': 'dashboard:ui' }

/**
 * Count a mutating request against the artifact it targets, so the leave-time
 * cleanup can tell an unacknowledged write from a document nobody touched.
 *
 * Hooked HERE, at the transport, rather than in each caller: the previous design
 * asked every write path to announce itself and repeatedly shipped one that did
 * not, letting a document be deleted with its own PATCH still in the air. A
 * request cannot be issued without passing through these five helpers, so this
 * cannot be forgotten by a new call site. `settle` itself is excluded — it is the
 * cleanup, not a user write, and counting it would have it guard against itself.
 */
const ARTIFACT_WRITE_RE = /\/api\/artifacts\/([^/?#]+)/
function trackArtifactWrite(url: string, res: Promise<Response>): Promise<Response> {
  const m = ARTIFACT_WRITE_RE.exec(url)
  if (!m || url.includes('/settle')) return res
  let slug: string
  try {
    slug = decodeURIComponent(m[1])
  } catch {
    slug = m[1]
  }
  beginArtifactWrite(slug)
  // `finally` on both paths: a FAILED write clears too, which is correct — the
  // server never applied it, so the record it re-reads is authoritative anyway.
  return res.finally(() => endArtifactWrite(slug))
}

/** Precondition header for a steering workspace write. Omitted when the caller
 *  has no project key, which the server treats as fail-closed for `workspace/`
 *  keys — an absent view is not an agreeing one. */
const projectHeader = (projectKey?: string): HeadersInit | undefined =>
  projectKey ? { 'X-Steering-Project': projectKey } : undefined

const get = (url: string, sessionKey?: string, signal?: AbortSignal) =>
  fetch(url, { headers: { ...(sessionKey ? { 'X-Session-Key': sessionKey } : _sk) }, ...(signal ? { signal } : {}) })
const post = (url: string, body?: object, sessionKey?: string, extra?: HeadersInit) =>
  trackArtifactWrite(url, fetch(url, {
    method: 'POST',
    // sessionKey overrides the shared `dashboard:ui` placeholder with the REAL
    // slot. The placeholder satisfies the server's `if sk:` gate but names no
    // actual session, so a restricted (incognito) slot was never recognised as
    // restricted and its writes were allowed through. Callers acting on behalf
    // of a specific chat slot must pass it.
    // `extra` carries a per-call precondition header (a view the server must
    // still agree with) without every caller re-implementing the header merge.
    headers: { 'Content-Type': 'application/json', ...(sessionKey ? { 'X-Session-Key': sessionKey } : _sk), ...extra },
    body: body ? JSON.stringify(body) : undefined,
  }))
const put = (url: string, body: object, sessionKey?: string, extra?: HeadersInit) =>
  trackArtifactWrite(url, fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json', ...(sessionKey ? { 'X-Session-Key': sessionKey } : _sk), ...extra }, body: JSON.stringify(body) }))
const del = (url: string, body?: object, sessionKey?: string, extra?: HeadersInit) =>
  trackArtifactWrite(url, fetch(url, { method: 'DELETE', headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...(sessionKey ? { 'X-Session-Key': sessionKey } : _sk), ...extra }, body: body ? JSON.stringify(body) : undefined }))
const patch = (url: string, body: object, sessionKey?: string) =>
  trackArtifactWrite(url, fetch(url, {
    method: 'PATCH',
    // Same override as post(): replace the shared `dashboard:ui` placeholder with
    // the REAL slot when the write belongs to a chat session, so the server's
    // restricted-session gate applies to it.
    headers: { 'Content-Type': 'application/json', ...(sessionKey ? { 'X-Session-Key': sessionKey } : _sk) },
    body: JSON.stringify(body),
  }))

// Publish the blessed transport so a downstream edition can build its OWN typed
// API module on the SAME session-key-authenticated helpers as core methods
// (inheriting X-Session-Key + auth-recovery + ApiError), instead of forking this
// file or writing methods on raw fetch. See api/apiTransport.ts.
installApiTransport({ get, post, put, del, patch, j, jNullable })

export interface InstanceTunnelStatus {
  instance_id: string
  state: 'disconnected' | 'connecting' | 'connected' | 'error' | 'stopped'
  local_port?: number
  remote_port?: number
  error?: string
  connected_at?: number
  token_ttl_remaining?: number
  diagnosis?: {
    code:
      | 'ok'
      | 'not_connected'
      | 'ssh_unreachable'
      | 'ssm_unreachable'
      | 'remote_down'
      | 'tunnel_down'
      | 'unknown'
    ok: boolean
    reason: string
    probes: { name: string; ok: boolean }[]
  }
}

export interface SsoStatus {
  state: 'ok' | 'expiring' | 'expired' | 'unknown'
  seconds_remaining: number | null
  expires_at: number | null
  reason: string
}

export interface InstanceView {
  id: string
  name: string
  ssh_host: string
  remote_port: number
  local_port: number
  ttl: string
  remote_bin: string
  /** Transport used to reach the instance. Older records default to 'ssh'. */
  connection_method: 'ssh' | 'ssm'
  /** SSM-only: EC2 instance id (i-...) or SSM managed-instance id (mi-...). */
  ssm_target: string
  /** SSM-only: named AWS profile ('' = default credential chain). */
  aws_profile: string
  /** SSM-only: AWS region ('' = profile/environment default). */
  aws_region: string
  ssm_run_as: string
  was_connected: boolean
  status: InstanceTunnelStatus
}

export interface AddInstanceBody {
  name: string
  /** Required when connection_method is 'ssh' (the default). */
  ssh_host?: string
  remote_port?: number
  ttl?: string
  remote_bin?: string
  /** Transport to reach the instance. Defaults to 'ssh' when omitted. */
  connection_method?: 'ssh' | 'ssm'
  /** Required when connection_method is 'ssm': i-... / mi-... instance id. */
  ssm_target?: string
  aws_profile?: string
  aws_region?: string
  ssm_run_as?: string
  id?: string
}

/* ── Cloud provisioning (GET/POST /api/cloud/*) ──
 * Shapes mirror the backend launch-job model (cloud/launch_job.py). `size_key`
 * is the stable CLI id (light|balanced|power); `balanced` is the recommended
 * "Development" default. */

/** AWS preflight for the cloud launcher. Booleans are per-capability checks;
 *  `note`/`detail` are server-authored human text rendered verbatim. */
/** AWS coordinates a cloud lifecycle call needs beyond the stack tag.
 *
 *  `instanceId` is only meaningful for destroy, where the gateway uses it to
 *  drop the local Instances registration alongside the stack.
 */
export interface CloudCoords {
  profile?: string
  region?: string
  instanceId?: string
}

const cloudQuery = (c?: CloudCoords): string => {
  const q = new URLSearchParams()
  if (c?.profile) q.set('profile', c.profile)
  if (c?.region) q.set('region', c.region)
  if (c?.instanceId) q.set('instance_id', c.instanceId)
  const s = q.toString()
  return s ? `?${s}` : ''
}

export interface CloudPreflight {
  reachable: boolean
  account: string
  arn: string
  ec2_reachable: boolean
  cloudformation_reachable: boolean
  ssm_reachable: boolean
  note: string
  detail: string
  session_manager_plugin: boolean
  /** Copy-pasteable install command for the GATEWAY's platform, resolved
   *  server-side (the browser cannot know that host's OS). "" when the platform
   *  has no one-liner. */
  session_manager_plugin_command?: string
}

export type LaunchJobStatus =
  | 'pending' | 'running' | 'awaiting_signin' | 'done' | 'failed' | 'cancelled'
export type LaunchStepState = 'pending' | 'active' | 'done' | 'failed' | 'skipped'

/** One step of a launch job (preflight/provision/signin/connect). `label` and
 *  `detail` are server-authored and rendered verbatim, not translated. */
export interface LaunchStep {
  key: string
  label: string
  state: LaunchStepState
  detail?: string
}

/** The device-code sign-in prompt surfaced while a job is `awaiting_signin`. */
export interface CloudLaunchSignin {
  url: string
  code: string
  ports?: number[]
}

export interface LaunchJob {
  id: string
  profile: string
  region: string
  size_key: string
  tag: string
  status: LaunchJobStatus
  steps: LaunchStep[]
  instance_id?: string
  signin?: CloudLaunchSignin | null
  signin_detected?: boolean
  error?: string
  created_at: number
  updated_at: number
}

/** Tunnel status surfaced by GET /api/tunnel/status (backend TunnelManager).
 *  Enables mobile dashboard access via a remote tunnel. */
export interface TunnelStatus {
  state: 'disabled' | 'starting' | 'connected' | 'reconnecting' | 'error' | 'stopped'
  url: string
  error: string
  uptime: number
  reconnect_attempt: number
  /**
   * Why a `disabled` tunnel is off. `boot_flag` means the gateway was started
   * with `--no-tunnel` and will never publish, whatever `tunnel.enabled` says in
   * its config (a Dev Fleet pod always boots that way). Empty for every other
   * state and for an ordinary unconfigured tunnel. Optional because a gateway
   * older than this field does not send it.
   */
  reason?: string
}

export interface KiroPrerequisiteStatus {
  platform: string
  installed: boolean
  authenticated: boolean
  ready: boolean
  initial_setup_complete: boolean
  repair_required: boolean
  docs_url: string
  /**
   * The command the USER runs to sign in (`kiro-cli login`). Supplied by the
   * gateway and rendered verbatim in a `<code>` — never a catalog value, because
   * a translated command cannot be typed.
   */
  login_command: string
  sso_login_command: string
  setup_allowed: boolean
  /**
   * True when the CLI binary is present and executable but could not be
   * VERIFIED because this host cannot build a sandbox (verification runs the
   * binary inside it). A categorically different condition from a missing
   * binary — a failed sandbox build carries no information about whether the
   * CLI is installed.
   */
  sandbox_unavailable: boolean
  /** Machine-readable: 'transient' | 'foreign_sandbox' | 'no_backend' | ''. */
  sandbox_failure_kind: string
  /** Technical probe reason, e.g. 'unshare(CLONE_NEWNS) failed with errno 1 (EPERM)'. */
  sandbox_detail: string
  /**
   * Machine-readable host mechanism behind a Linux user-namespace denial:
   * 'apparmor_userns' | 'max_user_namespaces' | 'no_user_ns' | 'userns_denied' | ''.
   * Selects which concrete remedy the gate renders — the errno alone leaves the
   * user with nothing to act on.
   */
  sandbox_remedy: string
  /**
   * Kiro Crew's own agent spec files missing from the kiro-cli agents directory.
   * Non-empty means kiro-cli will answer every session/set_mode with
   * "Mode '<name>' not found", so `ready` is forced false and `repair_required`
   * true — a viable binary and a good `whoami` are NOT sufficient on their own.
   */
  missing_agent_specs: string[]
  /**
   * Failure text from the repair the Check again button attempts when specs are
   * missing. Empty when none was attempted or it succeeded. Shown verbatim and
   * untranslated: it names the failing install step.
   */
  agent_spec_repair_error: string
  /**
   * Kiro Crew's own specs that are PRESENT on disk but which the installed
   * kiro-cli refuses to load. Presence and acceptance are different questions: a
   * rejected spec is dropped from kiro-cli's agent table, so `--agent kirocrew`
   * resolves to the default agent with none of Kiro Crew's MCP servers — the
   * same total failure as an absent spec, which statting the file cannot detect.
   * Non-empty forces `ready` false and `repair_required` true.
   *
   * Optional because a gateway older than this field does not send it.
   */
  rejected_agent_specs?: string[]
  /**
   * kiro-cli's own reason for the first rejection above, sanitized. Shown
   * verbatim and untranslated: it names the file and the construct refused.
   */
  agent_spec_rejection_detail?: string
  /**
   * Whether the installed kiro-cli exposes the `acp` subcommand every Kiro Crew
   * session is launched through. False means the CLI runs and is signed in but
   * is too OLD to start a session, so `ready` is forced false. The remedy is an
   * update, not a reinstall. Optional because a gateway older than this field
   * does not send it — treat a missing value as `true` (supported).
   */
  acp_supported?: boolean
  /**
   * The command that updates the CLI in place (`kiro-cli update`). Unlike
   * `login_command`, Kiro Crew also runs this FOR the owner via the update-cli
   * POST — it is the CLI's own self-update. Rendered verbatim in a `<code>`.
   */
  update_command?: string
  /**
   * Failure text from an update-cli attempt. Empty when none was attempted or it
   * succeeded. Shown verbatim and untranslated: it names why the self-update did
   * not complete.
   */
  cli_update_error?: string
}

export interface KiroBonusCreditGrantPayload {
  name: string
  used: number
  total: number
  days_left?: number
}

export interface KiroUsagePayload {
  available?: boolean
  /** Why usage is unavailable when `available` is false (e.g. `api_key_auth`). */
  reason?: string
  credits_used?: number
  credits_covered?: number
  credits_overage?: number
  credits_plan?: number
  resets?: string
  plan?: string
  cost_usd?: number
  overage_rate?: number | string
  bonus_credits?: KiroBonusCreditGrantPayload[]
  stale?: boolean
  account?: string
  email?: string
  account_type?: string
  start_url?: string
}

export interface KiroBonusCreditGrant {
  name: string
  used: number
  total: number
  daysLeft?: number
}

export interface KiroCreditUsage {
  used: number
  limit: number
  overage: number
  resets?: string
  plan?: string
  costUsd?: number
  overageRate?: number
  bonusCredits: KiroBonusCreditGrant[]
  stale: boolean
  account?: string
  email?: string
  accountType?: string
  startUrl?: string
}

export interface KasLoginStatus {
  authenticated: boolean
  /** Provider of the active sign-in (e.g. 'google', 'github', 'builder_id'), null when signed out. */
  provider: string | null
  /** Human-readable account identity (email / profile ARN), null when signed out. */
  identity: string | null
  /**
   * How a sign-in can return to this gateway. 'loopback' means the browser and
   * the gateway share a machine, so the OAuth callback lands directly on a
   * local port; 'device' means the gateway is remote and the user instead
   * approves a short code in their own browser (no callback required).
   */
  transport: 'loopback' | 'device'
}

export interface KasLoginDeviceSession {
  /** Handle for polling this sign-in attempt. */
  login_id: string
  /** The short code the user types into the verification page. */
  user_code: string
  /** The page (opened on ANY device) where the code is entered. */
  verification_uri_complete: string
  /** ISO-8601 UTC instant the code stops working. */
  expires_at: string
}

export interface KasLoginPollResult {
  status: 'pending' | 'authorized' | 'expired' | 'error'
  /** Machine-readable failure code — error responses carry one too. */
  code?: string
  error?: string
}

/**
 * A loopback (PKCE) sign-in the gateway is listening for on a local port. The
 * dashboard opens `auth_url` in the user's browser; the portal redirects back
 * to `http://localhost:<port>` on this machine, and the gateway finishes the
 * exchange itself — the user confirms nothing. Polled with the same login_id.
 */
export interface KasLoginLoopbackSession extends KasLoginDeviceSession {
  auth_url: string
  port: number
}

export interface AgentImportCategory {
  id: string
  label: string
  count: number
  description?: string
}

export interface AgentImportSource {
  id: string
  name: string
  detected: boolean
  detail?: string
  categories: AgentImportCategory[]
}

export interface AgentImportSkipped {
  source: string
  category: string
  reason: string
  count?: number
}

export interface AgentImportScanResponse {
  sources: AgentImportSource[]
  skipped?: AgentImportSkipped[]
  merge_only: true
}

export interface AgentImportSelection {
  id: string
  categories: string[]
}

/** Skip keeps KiroCrew's item; rename installs alongside; overwrite replaces it
 *  after the backend writes a restore copy. Omitting the field means 'skip'. */
export type AgentImportConflictStrategy = 'skip' | 'rename' | 'overwrite'

export interface AgentImportApplyRequest {
  sources: AgentImportSelection[]
  conflict_strategy?: AgentImportConflictStrategy
}

export interface AgentImportSummary {
  imported: number
  deduplicated: number
  skipped: number
  conflicts: number
  /** How many of `conflicts` a retry with rename/overwrite could clear. */
  resolvable_conflicts: number
}

export interface AgentImportApplyResponse {
  ok: true
  conflict_strategy: AgentImportConflictStrategy
  summary: AgentImportSummary
}

/* ── Inbound webhooks (GET /api/webhooks) ──
 * Shapes mirror the pinned backend contract. Both one-time secrets — the bearer
 * token and the HMAC signing secret — only ever appear in
 * `WebhookTokenCreated`, from the create call; `GET /api/webhooks` never echoes
 * either one. */

export type WebhookFreshness = 'fresh' | 'stale' | 'expired'

export type WebhookOutcome =
  | 'completed' | 'timeout' | 'error' | 'rejected_capacity' | 'unauthorized' | 'disabled'

export interface WebhookTokenEntry {
  id: string
  label: string
  /** Leading, non-secret slice of the raw token, e.g. `kc_whk_4f2b`. */
  display_prefix: string
  last4: string
  created_at: number
  /** null / 0 until the token authorizes its first call. */
  last_used_at: number | null
  /** True when a caller using this token must also send a timestamp + HMAC
   *  signature of the raw body. The signing secret itself is never in this
   *  payload — it is returned once, from the create call. Legacy config tokens
   *  have no signing secret, so they report false. */
  require_signature: boolean
  /** True for the legacy `hooks.webhook_token` config scalar, which cannot be
   *  deleted from the dashboard. */
  legacy: boolean
  /** Operator-owned destination. Empty only for legacy or pre-routing rows. */
  agent: string
  /** Per-source admission switch; absent historical rows normalize to true. */
  enabled: boolean
}

export interface WebhookContextEntry {
  hook_id: string
  session_key: string
  registered_at: number
  age_seconds: number
  freshness: WebhookFreshness
  context_summary: string
  context_chars: number
}

export interface WebhookRunRecord {
  id: string
  /** null for a 401 — the caller is unknown at that point. */
  hook_id: string | null
  session_key: string
  name?: string
  outcome: WebhookOutcome
  started_at: number
  duration_ms: number
  result_chars: number
  token_id: string | null
  delivered: boolean
  detail?: string
}

export interface WebhooksView {
  /** Effective state: `has_tokens && switch_on`. */
  enabled: boolean
  /** The kill switch on its own. False ⇒ every inbound call is answered
   *  with 503 before any auth work, while tokens and history are kept. */
  switch_on: boolean
  /** True when at least one token exists (stored or legacy). */
  has_tokens: boolean
  url: string
  slots: { in_use: number; max: number }
  limits: {
    session_key_prefix: string
    message_max: number
    timeout_default: number
    timeout_max: number
    max_concurrent: number
    /** Raw request-body cap in bytes. Optional: a server predating the cap
     *  omits it, and the page falls back rather than rendering `undefined`. */
    body_max_bytes?: number
    /** Accepted clock skew, in seconds, for a signed request's timestamp. */
    signature_window_seconds: number
  }
  tokens: WebhookTokenEntry[]
  contexts: WebhookContextEntry[]
  runs: WebhookRunRecord[]
}

export interface WebhookTokenCreated {
  ok: boolean
  /** The raw secret — returned exactly once and unrecoverable afterwards. */
  token: string
  /** The HMAC signing secret — also returned exactly once. Absent when the
   *  token was minted bearer-only (`require_signature: false`). */
  signing_secret?: string
  entry: WebhookTokenEntry
}

export interface WebhookTestResult {
  ok: boolean
  status: number
  session_key?: string
  error?: string
}

/** One row of GET /api/members — a global crew as a Crew Members roster entry.
 *  Crew-record fields (kiro_agent, workspace, memory_store, model, …) are
 *  spread verbatim from the backend dataclass; only the fields the page reads
 *  are typed here, and extras pass through untyped by design so a new backend
 *  field is not a frontend break. */
export interface MemberRosterRow {
  /** Crew name — the display identity and the agent the DM thread pins to. */
  name: string
  /** Stable path-safe slug deriving the member dir and the slot key. */
  slug: string
  /** The pinned DM thread's slot key ('' until first open / unbound). */
  slot_key: string
  /** O(1) liveness: the bound slot is mid-turn right now. */
  running: boolean
  /** Epoch seconds of the DM transcript's last write; 0 = never talked. */
  last_active_ts?: number
  last_message?: string
  kiro_agent?: string
  workspace?: string
  memory_store?: string
  model?: string
  [extra: string]: unknown
}

/** One entry of GET /api/members/{slug}/activity — a recorded engagement.
 *  `via` distinguishes a session the user opened with the member ('chat')
 *  from an orchestrator routing decision ('select_crew'); the latter records
 *  intent, not a run. */
export interface MemberActivityEntry {
  /** Epoch seconds (UTC) the engagement was recorded. */
  ts: number
  via: 'chat' | 'select_crew' | string
  project?: string
}

export const api = {
  status: () => fetch('/api/status').then(j),
  tunnelStatus: () => fetch('/api/tunnel/status').then(j) as Promise<TunnelStatus>,
  system: () => fetch('/api/system').then(j),
  sessionStorage: () => get('/api/system/session-storage').then(j) as Promise<SessionStorageReport>,
  sessionStorageCleanup: (olderThanDays: number, dryRun = false) =>
    post('/api/system/session-storage/cleanup', { older_than_days: olderThanDays, dry_run: dryRun })
      .then(j) as Promise<SessionStorageCleanup>,
  sessionStorageRestore: (batchId: string, uids?: string[]) =>
    post('/api/system/session-storage/restore', uids ? { batch_id: batchId, uids } : { batch_id: batchId })
      .then(j) as Promise<{ restored: number }>,
  /** Starts an empty and returns the job; the delete outlives this request. */
  sessionStorageEmpty: (batchIds: string[]) =>
    post('/api/system/session-storage/empty', { batch_ids: batchIds }).then(j) as Promise<SessionStorageEmptyJob>,
  /** The running or last-finished empty. Cheap — no store is walked, so it polls. */
  sessionStorageEmptyStatus: () =>
    get('/api/system/session-storage/empty').then(j) as Promise<{ job: SessionStorageEmptyJob | null }>,
  /** Session inventory — the flat list contract (§1). */
  sessionInventory: () =>
    get('/api/system/session-storage/sessions').then(j) as Promise<SessionInventoryList>,
  /** Session detail — lazy per-row fetch (§2). */
  sessionInventoryDetail: (uid: string) =>
    get(`/api/system/session-storage/sessions/${encodeURIComponent(uid)}`).then(j) as Promise<SessionInventoryDetail>,
  /** Move explicit selection to trash (§3). */
  sessionInventoryTrash: (uids: string[]) =>
    post('/api/system/session-storage/trash', { uids }).then(j) as Promise<SessionTrashResult>,
  telemetryStartup: () => fetch('/api/telemetry/startup').then(j),
  // Per-turn context injection breakdown for one session. Independent of the
  // telemetry main switch: the usage rows it reads are always written.
  telemetryContextTrace: (slot: string) =>
    fetch('/api/telemetry/context-trace?slot=' + encodeURIComponent(slot)).then(j),
  /** Per-turn usage rows for one session — the Spend table's drill-down.
   *  Same always-written row store as the context trace; the dashboard reads
   *  every row (the endpoint's app-ownership filter applies to app callers). */
  usageTurns: (slot: string) =>
    fetch('/api/usage/turns?slot=' + encodeURIComponent(slot)).then(j),
  /** Intent summary for the chat summary panel.
   *
   *  Read-only: it never triggers generation. Summaries are produced at turn end
   *  by a background pass, so opening the panel cannot spend tokens and repeated
   *  opening cannot become a refresh loop. Returns `enabled: false` (not an
   *  error) when the feature is off, so the panel can explain itself. */
  sessionSummary: (slot: string) =>
    fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/summary').then(j) as Promise<SessionSummary>,
  /** Summarize this session NOW, on the person's explicit request.
   *
   *  Same path as the GET, different verb: reading a summary must stay free of
   *  side effects, so spending tokens is a separate verb rather than a flag on
   *  the read. Rejects with the body's `code` (`summary_in_flight`,
   *  `too_few_turns`, `summary_unavailable`, `summary_disabled`) so the panel can
   *  say which rather than showing one generic failure. */
  generateSessionSummary: (slot: string) =>
    fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/summary', {
      method: 'POST',
    }).then(j) as Promise<SessionSummary>,
  beaconStatus: () => fetch('/api/telemetry/beacon').then(j),
  /** Local metric-collection posture for the Privacy panel's recording switch.
   *  Separate from telemetryStartup(), which parses every shard in the window. */
  collectionStatus: () => fetch('/api/telemetry/collection').then(j),
  // Background polls read the gateway's latched state (no kiro-cli subprocess).
  // `refresh` is the explicit user action (Refresh / Check again) that forces a
  // real host probe.
  /**
   * `refresh` picks the probe mode, and the two are deliberately different:
   * `'explicit'` is the human Check again and always probes the host, `'auto'` is
   * the blocking gate's poll and is coalesced server-side behind a short floor so
   * several open tabs cannot multiply the `kiro-cli` spawns. `false` reads the
   * gateway's latched state and spawns nothing.
   */
  kiroPrerequisite: (refresh: false | 'auto' | 'explicit' = false) => {
    // Built with URLSearchParams like every other query here (see artifacts
    // below): the mode is its own wire value, so there is no query-string
    // literal for the i18n gate to mistake for user-visible copy.
    const params = new URLSearchParams()
    if (refresh) params.set('refresh', refresh)
    const s = params.toString()
    return get(`/api/kiro-prerequisite${s ? `?${s}` : ''}`).then(
      j,
    ) as Promise<KiroPrerequisiteStatus>
  },
  // A POST, not a flag on the status GET: the gateway's CSRF check and its SEL
  // audit are both method-scoped, so a spec rewrite reached from a GET would be
  // cross-site triggerable and would leave no audit record.
  repairKiroPrerequisiteSpecs: () =>
    post('/api/kiro-prerequisite/repair-specs').then(j) as Promise<KiroPrerequisiteStatus>,
  // A POST for the same CSRF/audit reasons as the spec repair above. Runs the
  // CLI's own in-place self-update on the gateway host and returns the
  // post-update snapshot; `cli_update_error` is empty on success.
  updateKiroPrerequisiteCli: () =>
    post('/api/kiro-prerequisite/update-cli').then(j) as Promise<KiroPrerequisiteStatus>,
  // KAS-mode in-product sign-in (no kiro-cli, no terminal). Status is a cheap
  // read; every step that changes sign-in state is a POST for the same
  // CSRF/audit reasons as the spec repair above. Error responses carry a
  // machine-readable `code` field alongside the human message.
  kasLoginStatus: () => get('/api/kas-login').then(j) as Promise<KasLoginStatus>,
  kasLoginBeginDevice: (provider: string, extra?: { start_url?: string; region?: string }) =>
    post('/api/kas-login/device', { provider, ...(extra ?? {}) }).then(
      j,
    ) as Promise<KasLoginDeviceSession>,
  kasLoginPoll: (login_id: string) =>
    post('/api/kas-login/poll', { login_id }).then(j) as Promise<KasLoginPollResult>,
  // Loopback begin answers 409 `loopback_unavailable` when this install shape
  // cannot receive the callback (or every allowlisted port is busy); the gate
  // treats that as "start the device flow instead", not as a failure.
  kasLoginBeginLoopback: (provider: string) =>
    post('/api/kas-login/loopback', { provider }).then(j) as Promise<KasLoginLoopbackSession>,
  // Idempotent: releases a loopback listener's port early on every start-over path.
  kasLoginCancel: (login_id: string) =>
    post('/api/kas-login/cancel', { login_id }).then(j) as Promise<{ ok: boolean }>,
  kasLoginLogout: (identity: string) =>
    post('/api/kas-login/logout', { identity }).then(j) as Promise<KasLoginStatus>,
  onboardingImportScan: () =>
    get('/api/onboarding/import/scan').then(j) as Promise<AgentImportScanResponse>,
  onboardingImportApply: (body: AgentImportApplyRequest) =>
    post('/api/onboarding/import/apply', body).then(j) as Promise<AgentImportApplyResponse>,
  onboardingImportState: (body: { completed: true }) =>
    put('/api/onboarding/import/state', body).then(jNullable) as Promise<{ ok?: boolean } | null>,
  // Counts are derived server-side from the controls they describe, so a null
  // means "temporarily unresolvable", never "zero".
  securityStats: () => get('/api/security/stats').then(j) as Promise<{ denied_commands: number | null; suspicious_patterns: number | null; tool_schemas: number | null; redaction_paths: number | null }>,
  securityPosture: () => get('/api/security/posture').then(j) as Promise<SecurityPostureData>,
  // `sessionKey` MUST carry the active slot's key (`dashboard:<slot>`) when one
  // is active: the server's restricted-session guard reads X-Session-Key, and
  // the shared `dashboard:ui` default answers "not restricted" — which would
  // let an incognito/temporary slot mint a durable any-device credential. Same
  // cooperative-honesty contract as the tailnet mobile surface.
  mobileLoginLink: (sessionKey?: string) =>
    post('/api/auth/mobile-link', undefined, sessionKey).then(j) as Promise<{
    url: string
    expires_in: number
  }>,
  // Phone-connection methods available on this deployment under the current
  // governance ceiling (CPP mobile_connect seam). Descriptor-only: minting the
  // credential stays on each method's own endpoint above/below. An empty list
  // hides the sidebar "Connect your phone" entry entirely.
  mobileConnectMethods: () =>
    get('/api/mobile-connect/methods').then(j) as Promise<MobileConnectMethodsData>,
  // Tailnet origin (Settings → Security). READ ONLY here: the toggle writes
  // `dashboard.tailscale.enabled` through the generic config PATCH, because the
  // setting IS a config value and the status endpoint reports what the running
  // server resolved from it at startup.
  tailnetStatus: () => get('/api/tailnet/status').then(j) as Promise<TailnetStatusData>,
  // Mobile access. `tailnetMobile` is a LIVE probe (two daemon round trips
  // server-side), so poll it gently; the mutations below are user-driven.
  tailnetMobile: () => get('/api/tailnet/mobile').then(j) as Promise<TailnetMobileData>,
  tailnetMobileConfigure: () =>
    post('/api/tailnet/mobile/configure', {}).then(j) as Promise<TailnetMobileConfigure>,
  tailnetMobilePublish: () =>
    post('/api/tailnet/mobile/publish', {}).then(j) as Promise<TailnetMobileMutation>,
  tailnetMobileUnpublish: () =>
    post('/api/tailnet/mobile/unpublish', {}).then(j) as Promise<TailnetMobileMutation>,
  // Mints a session token. Called ONLY from an explicit user action — never on
  // render — because the response is a live credential. `sessionKey` carries
  // the caller's REAL slot key so the server's restricted-session guard sees
  // it instead of the shared `dashboard:ui` placeholder.
  tailnetMobileQr: (ttl?: string, sessionKey?: string) =>
    post('/api/tailnet/mobile/qr', ttl ? { ttl } : {}, sessionKey).then(j) as Promise<TailnetMobileQr>,
  // Denied commands (Settings → Security). Every endpoint returns the full
  // refreshed snapshot so callers can seed their query cache from the response.
  deniedCommands: () => get('/api/security/denied-commands').then(j) as Promise<DeniedCommandsData>,
  toggleBuiltinDeniedCommand: (id: string, enabled: boolean) =>
    patch('/api/security/denied-commands/builtins/' + encodeURIComponent(id), { enabled }).then(j) as Promise<DeniedCommandsData>,
  setDeniedCommandsDisableAll: (value: boolean) =>
    patch('/api/security/denied-commands/disable-all', { value }).then(j) as Promise<DeniedCommandsData>,
  addUserDeniedCommand: (pattern: string, note = '') =>
    post('/api/security/denied-commands/user', { pattern, note }).then(j) as Promise<DeniedCommandsData>,
  toggleUserDeniedCommand: (id: string, enabled: boolean) =>
    patch('/api/security/denied-commands/user/' + encodeURIComponent(id), { enabled }).then(j) as Promise<DeniedCommandsData>,
  deleteUserDeniedCommand: (id: string) =>
    del('/api/security/denied-commands/user/' + encodeURIComponent(id)).then(j) as Promise<DeniedCommandsData>,
  // Third-party app trust (Settings → Security). Like denied-commands, every
  // endpoint returns the full refreshed snapshot so callers can seed the query
  // cache from the mutation response instead of re-fetching.
  listTrustedApps: () => get('/api/security/trusted-apps').then(j) as Promise<TrustedAppsData>,
  trustApp: (name: string, repository?: string) =>
    post(
      '/api/security/trusted-apps/' + encodeURIComponent(name),
      repository ? { repository } : undefined,
    ).then(j) as Promise<TrustedAppsData>,
  // Returns the snapshot PLUS `disabled` — revoking trust also disables an app
  // that is currently enabled, so its code stops running immediately.
  untrustApp: (name: string) =>
    del('/api/security/trusted-apps/' + encodeURIComponent(name)).then(j) as Promise<TrustedAppsRevokeResult>,
  setTrustAllApps: (value: boolean) =>
    put('/api/security/trusted-apps/allow-all', { value }).then(j) as Promise<TrustedAppsData>,
  // Read-only governance policy viewer (Settings → Security). No write path —
  // the enterprise ceiling is file-authored and un-editable via the UI.
  governancePolicy: () => get('/api/governance/policy').then(j) as Promise<GovernancePolicyData>,
  suggestions: (force?: boolean) => fetch(`/api/suggestions${force ? '?force=1' : ''}`).then(j) as Promise<{ suggestions: string[]; generated_at: number; stale: boolean }>,
  branding: () => fetch('/api/dashboard/branding').then(j) as Promise<{ bot_name: string; avatar: string; direct_local?: boolean }>,
  // Instances (multi-instance management) — owner-only, gated by instances.enabled.
  // listInstances throws ApiError(403) when the feature is disabled; callers
  // should catch and render the enable toggle rather than an error. `active`
  // is true only when the SSH manager is actually running (the flag was on at
  // gateway startup) — enabled-but-not-active means a restart is required.
  listInstances: () => get('/api/instances').then(j) as Promise<{ active: boolean; instances: InstanceView[]; warm_set_cap: number; sso: SsoStatus }>,
  addInstance: (body: AddInstanceBody) => post('/api/instances', body).then(j) as Promise<InstanceView>,
  updateInstance: (id: string, body: Partial<AddInstanceBody>) =>
    patch('/api/instances/' + encodeURIComponent(id), body).then(j) as Promise<InstanceView>,
  removeInstance: (id: string) => del('/api/instances/' + encodeURIComponent(id)).then(j),
  instanceStatus: (id: string, diagnose = false) =>
    get('/api/instances/' + encodeURIComponent(id) + '/status' + (diagnose ? '?diagnose=1' : '')).then(j) as Promise<InstanceTunnelStatus>,
  connectInstance: (id: string) =>
    post('/api/instances/' + encodeURIComponent(id) + '/connect').then(j) as Promise<
      InstanceTunnelStatus & { token?: string }
    >,
  refreshInstanceToken: (id: string) =>
    post('/api/instances/' + encodeURIComponent(id) + '/refresh-token').then(j) as Promise<
      InstanceTunnelStatus & { token?: string }
    >,
  disconnectInstance: (id: string) =>
    post('/api/instances/' + encodeURIComponent(id) + '/disconnect').then(j) as Promise<{
      disconnected: string
      was_connected: boolean
    }>,
  restartInstance: (id: string) =>
    post('/api/instances/' + encodeURIComponent(id) + '/restart').then(j) as Promise<{
      ok: boolean
      message: string
    }>,
  // Copies a session to another instance. The local session is left untouched:
  // the peer allocates its own key, so this is a copy and never a move.
  sendSessionToInstance: (id: string, slot: string) =>
    post('/api/instances/' + encodeURIComponent(id) + '/send-session', { slot }).then(j) as Promise<{
      ok: boolean
      instance: string
      remote_key: string
      messages: number
      // '' when the peer is too old to report it — treated as unknown.
      resume_mode?: 'session_load' | 'prefix' | ''
    }>,
  // Cloud provisioning (owner-only) — launch a cloud-hosted remote crew on the
  // user's OWN AWS account, then register it as an SSM instance on connect. The
  // launch is a DURABLE gateway job (see cloud/launch_job.py): it survives
  // dashboard navigation and restart, so the UI polls its state rather than
  // holding it in memory. `tag` (kc-xxxx) is the cloud lifecycle handle used by
  // stop/start/destroy; `instance_id` (i-...) is the EC2 id it registers under.
  cloudPreflight: (profile?: string, region?: string) => {
    const p = new URLSearchParams()
    if (profile) p.set('profile', profile)
    if (region) p.set('region', region)
    const s = p.toString()
    return get('/api/cloud/preflight' + (s ? '?' + s : '')).then(j) as Promise<CloudPreflight>
  },
  cloudIamPolicy: () => get('/api/cloud/iam-policy').then(j) as Promise<{ policy: string }>,
  cloudLaunches: () => get('/api/cloud/launch').then(j) as Promise<{ jobs: LaunchJob[] }>,
  cloudLaunch: (body: { profile: string; region: string; size_key: string }) =>
    post('/api/cloud/launch', body).then(j) as Promise<LaunchJob>,
  cloudLaunchStatus: (id: string) =>
    get('/api/cloud/launch/' + encodeURIComponent(id)).then(j) as Promise<LaunchJob>,
  cloudLaunchCancel: (id: string) =>
    post('/api/cloud/launch/' + encodeURIComponent(id) + '/cancel').then(j) as Promise<LaunchJob>,
  // Fetches the device-code prompt while the job is awaiting sign-in; 409 when
  // there is no pending prompt (surfaced as ApiError(409) to the caller).
  cloudLaunchSignin: (id: string) =>
    post('/api/cloud/launch/' + encodeURIComponent(id) + '/signin').then(j) as Promise<{ signin: CloudLaunchSignin }>,
  // The gateway resolves the stack from the tag but needs the launch's AWS
  // coordinates: a crew created under a non-default profile/region is invisible
  // to the default ones, so omitting them makes stop/start/destroy fail. destroy
  // also needs instance_id to drop the local Instances registration, otherwise
  // the crew keeps appearing in the list after its box is gone.
  cloudStop: (tag: string, coords?: CloudCoords) =>
    post('/api/cloud/' + encodeURIComponent(tag) + '/stop' + cloudQuery(coords)).then(j) as Promise<{ ok?: boolean }>,
  cloudStart: (tag: string, coords?: CloudCoords) =>
    post('/api/cloud/' + encodeURIComponent(tag) + '/start' + cloudQuery(coords)).then(j) as Promise<{ ok?: boolean }>,
  cloudDestroy: (tag: string, coords?: CloudCoords) =>
    del('/api/cloud/' + encodeURIComponent(tag) + cloudQuery(coords)).then(j) as Promise<{ ok?: boolean; unregistered?: boolean; source_removed?: boolean }>,
  // Memory
  memoryPreferences: () => fetch('/api/memory/preferences').then(j),
  saveMemoryPreferences: (content: string) => put('/api/memory/preferences', { content }),
  memoryProjects: () => fetch('/api/memory/projects').then(j),
  saveMemoryProjects: (content: string) => put('/api/memory/projects', { content }),
  memoryHistory: () => fetch('/api/memory/history').then(j),
  saveMemoryHistory: (content: string) => put('/api/memory/history', { content }),
  memorySettings: () => fetch('/api/memory/settings').then(j),
  saveMemorySettings: (s: {history_idle_hours?: number; history_max_days?: number}) => put('/api/memory/settings', s),
  // Vector memory
  vectorSemantic: () => fetch('/api/memory/semantic').then(j),
  vectorSemanticWrite: (key: string, value: string) => put('/api/memory/semantic', { key, value, source: 'user_explicit' }).then(j),
  vectorSemanticDelete: (key: string) => del('/api/memory/semantic/' + encodeURIComponent(key)),
  vectorEpisodic: (limit = 50, offset = 0, tags?: string) => fetch('/api/memory/episodic?limit=' + limit + '&offset=' + offset + (tags ? '&tags=' + encodeURIComponent(tags) : '')).then(j),
  vectorEpisodicSearch: (q: string, tags?: string) => fetch('/api/memory/episodic/search?q=' + encodeURIComponent(q) + (tags ? '&tags=' + encodeURIComponent(tags) : '')).then(j),
  vectorEpisodicDelete: (id: string) => del('/api/memory/episodic/' + encodeURIComponent(id)),
  vectorStats: () => fetch('/api/memory/stats').then(j),
  vectorEvents: (limit = 50, offset = 0) => fetch('/api/memory/events?limit=' + limit + '&offset=' + offset).then(j),
  vectorEmbeddingStatus: () => fetch('/api/memory/embedding-status').then(j),
  vectorEnableEmbeddings: () => post('/api/memory/enable-embeddings').then(j),
  vectorValidateEmbedModel: (path: string) =>
    post('/api/memory/embedding-model', { path, validate_only: true }).then(j),
  vectorApplyEmbedModel: (path: string) =>
    post('/api/memory/embedding-model', { path }).then(j),
  vectorDisableEmbeddings: () => post('/api/memory/disable-embeddings').then(j),
  vectorImport: (data: object) => post('/api/memory/import', data).then(j),
  vectorContextPreview: (query?: string) => fetch('/api/memory/context-preview' + (query ? '?q=' + encodeURIComponent(query) : '')).then(j),
  memoryGraph: () => fetch('/api/memory/graph').then(j),
  consolidateMemory: (key: string, includeHistory: boolean) => post('/api/memory/consolidate', { key, include_history: includeHistory }).then(j),
  restartSessions: () =>
    post('/api/sessions/restart').then(j) as Promise<{
      ok: boolean
      sessions_reset: number
      mcp_synced: number
      /** false when the MCP reconcile FAILED before the restart: sessions did
       *  restart, but against a config that may not match the sources. */
      mcp_sync_ok: boolean
    }>,
  sessionsContext: () => fetch('/api/sessions/context').then(j),
  sessionsMemory: () => fetch('/api/sessions/memory').then(j) as Promise<{
    sessions: {
      key: string; title: string; slot_key: string; untitled: boolean
      agent: string; pid: number | null; owns_runtime: boolean; prompts: number
      channel: string
      rss_mb: number | null; procs: number | null; mcp: number | null
      cpu_cores: number | null; uptime_s: number | null
      credits: number | null; turns: number | null
    }[]
    tasks: {
      id: string; task: string; agent: string; parent: string
      rss_mb: number; peak_rss_mb: number; cpu_cores: number
      procs: number | null; mcp: number | null
      started_at: number; shared: boolean; pid: number | null; sampled: boolean
    }[]
    totals: {
      rss_mb: number; runtimes: number; host_mb: number | null
      host_pct: number | null; rss_is_upper_bound: boolean
    }
    history: { t: number; mb: number }[]
  }>,
  sessionsUsage: () => fetch('/api/sessions/usage').then(j) as Promise<{ usage?: KiroUsagePayload }>,
  providerUsage: () => fetch('/api/usage').then(j),
  mcpProbeCache: () => fetch('/api/mcp/probe').then(j),
  // Agents
  agentsInstalled: () => fetch('/api/agents/installed').then(j),
  agentDetail: (name: string) => fetch('/api/agents/detail/' + encodeURIComponent(name)).then(j),
  agentPatch: (name: string, body: object) => fetch('/api/agents/detail/' + encodeURIComponent(name), { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j),
  agentDelete: (name: string) => fetch('/api/agents/detail/' + encodeURIComponent(name), { method: 'DELETE' }).then(j),
  // KiroCrew agents
  // sessionKey identifies the CHAT SLOT whose project scope applies. The
  // server resolves project-local agents through
  // active_project_dir(state, session_key); with no key it falls back to
  // "the single project shared by every slot" and fails closed when two
  // slots sit on different projects, so project-scoped agents silently
  // vanish from the picker. Surfaces with no slot context (Channels,
  // Schedule) pass nothing and keep the global-only view.
  kirocrewAgents: (sessionKey?: string) =>
    fetch('/api/agents', {
      headers: sessionKey ? { 'X-Session-Key': sessionKey } : { ..._sk },
    }).then(j),
  /** The model a new session on this KiroCrew agent would run on. Empty
   *  `agent` resolves the configured default agent. */
  agentResolvedModel: (agent: string) =>
    fetch('/api/agents/resolved-model?agent=' + encodeURIComponent(agent)).then(j),
  syncKirocrewAgents: () => post('/api/agents/sync', {}).then(j),
  createKirocrewAgent: (body: object) => post('/api/agents', body).then(j),
  // Crew Members page — roster of GLOBAL crews with DM-thread binding and the
  // cheap live-status fields the backend can answer without IO (richer live
  // detail rides the already-subscribed WS `slots` frames).
  members: () => fetch('/api/members').then(j) as Promise<{ members: MemberRosterRow[] }>,
  // Idempotent get-or-create of a member's pinned DM thread. Member slots are
  // born ONLY through this route (the generic slot-create endpoint refuses
  // mode="member"), so this is also the only place a member slot key comes from.
  memberThread: (slug: string) =>
    post('/api/members/' + encodeURIComponent(slug) + '/thread').then(j) as Promise<{ slot_key: string; slug: string; member: string }>,
  // A member's recent activity pointers (real recorded signal only: session
  // participations and routing decisions). `member` is the exact crew name —
  // slugs are lossy, so the backend filters the shared log by exact name.
  // Fetched on drawer open, never polled.
  memberActivity: (slug: string, member: string) =>
    fetch(
      '/api/members/' + encodeURIComponent(slug) + '/activity?member=' + encodeURIComponent(member),
    ).then(j) as Promise<{
      slug: string
      member: string
      /** True when the display window is saturated — derived counters are floors. */
      capped: boolean
      entries: MemberActivityEntry[]
    }>,
  updateKirocrewAgent: (name: string, body: object) =>
    put('/api/agents/' + encodeURIComponent(name), body).then(j),
  deleteKirocrewAgent: (name: string) =>
    del('/api/agents/' + encodeURIComponent(name)).then(j),
  /** Stage a crew's picture on the server (a `.pending` file only — the
   *  config PUT with `avatar: {kind:'image'}` is what promotes it live,
   *  keeping the editor's Apply→Save two-step a real commit point). */
  uploadCrewAvatar: (name: string, file: Blob) => {
    const form = new FormData()
    form.append('file', file, 'avatar.png')
    return fetch('/api/agents/' + encodeURIComponent(name) + '/avatar', {
      method: 'POST',
      body: form,
    }).then(j) as Promise<{ ok?: boolean; staged?: boolean; token?: string; error?: string }>
  },
  models: () => fetch('/api/models').then(j),
  effortLevels: (slot?: string) =>
    fetch('/api/effort-levels' + (slot ? '?slot=' + encodeURIComponent(slot) : '')).then(j) as Promise<string[]>,
  // Bounded HERE, not per initiator: react-query dedupes on the key, so the
  // weakest initiator would otherwise decide whether the promise is bounded.
  slashCommands: (signal?: AbortSignal) =>
    withDeadline(SLASH_COMMANDS_TIMEOUT_MS, signal, s =>
      fetch('/api/slash-commands', { signal: s }).then(j)),
  chatSlotAgent: (slot: string, agent: string) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/agent', { agent }).then(j) as Promise<{ ok?: boolean; agent?: string; workspace?: string }>,
  chatSlotModel: (slot: string, model: string) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/model', { model }).then(j) as Promise<{ ok?: boolean; model?: string }>,
  /** This slot's auto-compact threshold override (null = follows the global). */
  chatSlotAutocompact: (slot: string) =>
    fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/autocompact').then(j) as Promise<{ pct: number | null; global_pct: number; min: number; max: number }>,
  /** Set (number) or clear (null) this slot's auto-compact threshold override. */
  setChatSlotAutocompact: (slot: string, pct: number | null) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/autocompact', { pct }).then(j) as Promise<{ ok?: boolean; pct: number | null; global_pct: number }>,
  chatSlotsModel: (model: string, skip_running: boolean) =>
    post('/api/chat/slots/model', { model, skip_running }).then(j) as Promise<{ ok: boolean; model: string; switched: string[]; skipped_running: string[]; unchanged: string[]; failed: string[] }>,
  chatSlotReasoningEffort: (slot: string, reasoning_effort: string) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/reasoning-effort', { reasoning_effort }).then(j) as Promise<{ ok?: boolean; reasoning_effort?: string; deferred?: boolean }>,
  chatSlotWorkspace: (slot: string, workspace: string) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/workspace', { workspace }).then(j),
  // Relaunch the slot's agent process in place (fresh agent spec, env, and MCP
  // servers; conversation preserved). 409 while a turn is in flight.
  chatSlotReload: (slot: string) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/reload', {}).then(j) as Promise<{ ok?: boolean; error?: string }>,
  chatSlotProject: (slot: string, project: string) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/project', { project }).then(j) as Promise<{ ok?: boolean; project?: string }>,
  // Follow-up card: create a sibling git worktree of `repo` on a new `branch`.
  // Resolves with the created path, or rejects with the server's message
  // (branch/dir already exists, not a git repo, git unavailable).
  createWorktree: (repo: string, branch: string) =>
    post('/api/worktree/create', { repo, branch }).then(j) as Promise<{
      ok?: boolean
      path?: string
      branch?: string
      base?: string
      error?: string
    }>,
  recentProjects: () => fetch('/api/recent-projects').then(j) as Promise<{ dirs: string[] }>,
  browseDirs: (path?: string) => fetch('/api/browse-dirs' + (path ? '?path=' + encodeURIComponent(path) : '')).then(j) as Promise<{ path: string; parent: string; dirs: { name: string; path: string }[] }>,
  browseFiles: (path?: string) => fetch('/api/browse-files' + (path ? '?path=' + encodeURIComponent(path) : '')).then(j) as Promise<{ path: string; parent: string; dirs: { name: string; path: string; mtime: number }[]; files: { name: string; path: string; mtime: number }[] }>,
  projectGit: (path: string) => fetch('/api/project/git?path=' + encodeURIComponent(path)).then(j) as Promise<{ path: string; repo: boolean; repoRoot?: string; branch?: string; detached?: boolean; head?: string }>,
  projectGitStatus: (path: string) => fetch('/api/project/git/status?path=' + encodeURIComponent(path)).then(j) as Promise<{ repo: boolean; repoRoot?: string; branch?: string; ahead?: number; behind?: number; files: { path: string; status: string; staged: boolean; additions?: number; deletions?: number }[] }>,
  projectGitLog: (path: string, limit = 20) => fetch('/api/project/git/log?path=' + encodeURIComponent(path) + '&limit=' + limit).then(j) as Promise<{ repo: boolean; commits: { sha: string; message: string; author: string; date: string; isHead: boolean }[] }>,
  projectTree: (path: string) => fetch('/api/project/tree?path=' + encodeURIComponent(path)).then(j) as Promise<{ root: string; paths: string[]; repo: boolean; truncated?: boolean }>,
  workspaces: () => fetch('/api/workspaces').then(j),
  createWorkspace: (body: object) => post('/api/workspaces', body).then(j),
  updateWorkspace: (name: string, body: object) =>
    put('/api/workspaces/' + encodeURIComponent(name), body).then(j),
  deleteWorkspace: (name: string) =>
    del('/api/workspaces/' + encodeURIComponent(name)).then(j),
  // Crons
  crons: () => fetch('/api/crons').then(j),
  createCron: (body: object) => post('/api/crons', body).then(j),
  deleteCron: (id: string) => del('/api/crons/' + id).then(j),
  batchDeleteCron: (ids: string[]) => del('/api/crons', { ids }).then(j),
  updateCron: (id: string, body: object) =>
    fetch('/api/crons/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j),
  runCron: (id: string) => post('/api/crons/' + id + '/run').then(j),
  /** Grant/revoke vault secrets, or act on an agent-requested pending grant.
   * Body: {secret_env: {...}} grants (empty object revokes), or
   * {approve_pending: true, expected_secret_env, expected_ts} /
   * {deny_pending: true}. Approval restates the displayed request; the server
   * refuses with 409 stale_request if it was replaced. Operator-only server-side. */
  cronSecretsGrant: (id: string, body: { secret_env?: Record<string, string>; approve_pending?: boolean; deny_pending?: boolean; expected_secret_env?: Record<string, string> | null; expected_ts?: number; expected_source_sha256?: string }) =>
    put('/api/crons/' + id + '/secrets', body).then(j),
  cancelCron: (id: string) => post('/api/crons/' + id + '/cancel').then(j),
  cronToChat: (id: string) => post('/api/crons/' + id + '/to-chat').then(j),
  toggleCron: (id: string, enabled: boolean) => post('/api/crons/' + id + '/enable', { enabled }).then(j),
  cronHistory: (jobId: string, offset?: number, limit?: number) => {
    const p = new URLSearchParams()
    if (offset != null) p.set('offset', String(offset))
    if (limit != null) p.set('limit', String(limit))
    const qs = p.toString()
    return fetch('/api/crons/' + jobId + '/history' + (qs ? '?' + qs : ''), { headers: { ..._sk } }).then(j)
  },
  cronRunDetail: (jobId: string, runId: string) => fetch('/api/crons/' + jobId + '/history/' + encodeURIComponent(runId), { headers: { ..._sk } }).then(j),
  cronScript: (jobId: string) => fetch('/api/crons/' + jobId + '/script', { headers: { ..._sk } }).then(j),
  /** Vault secret NAMES (values are never exposed). Same endpoint the Settings
   * Secrets panel reads. */
  secretsList: () => fetch('/api/secrets').then(j),
  ackCron: (id: string, summary: string, ts?: string) => post('/api/crons/' + id + '/ack', { summary, ts }).then(j),
  cronHistoryAll: (opts?: { offset?: number; limit?: number; jobId?: string }) => {
    const p = new URLSearchParams()
    if (opts?.offset != null) p.set('offset', String(opts.offset))
    if (opts?.limit != null) p.set('limit', String(opts.limit))
    if (opts?.jobId) p.set('job_id', opts.jobId)
    return fetch('/api/crons/history' + (p.toString() ? '?' + p : ''), { headers: { ..._sk } }).then(j)
  },

  // Cron Folders
  cronFolders: () => fetch('/api/cron-folders').then(j),
  createCronFolder: (name: string) => post('/api/cron-folders', { name }).then(j),
  updateCronFolder: (id: string, body: { name?: string }) =>
    fetch('/api/cron-folders/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j),
  deleteCronFolder: (id: string) => del('/api/cron-folders/' + id).then(j),

  // Lessons
  lessons: () => fetch('/api/lessons').then(j),
  createLesson: (rule: string, category: string) =>
    post('/api/lessons', { rule, category }).then(j) as Promise<{
      ok: boolean
      outcome: 'inserted' | 'enriched' | 'unchanged' | 'deduped' | 'refused'
      reason: string
    }>,
  deleteLesson: (rule: string) => del('/api/lessons', { rule }).then(j),
  // Hooks
  hooks: () => fetch('/api/hooks').then(j),
  kiroHooks: () => fetch('/api/kiro-hooks').then(j),
  createHook: (body: object) => post('/api/hooks', body).then(j),
  updateHook: (id: string, body: object) => put('/api/hooks/' + id, body).then(j),
  deleteHook: (id: string) => del('/api/hooks/' + id).then(j),
  toggleHook: (id: string) => post('/api/hooks/' + id + '/toggle', {}).then(j),
  testHook: (id: string, context?: string) => post('/api/hooks/' + id + '/test', { context: context || 'test' }).then(j),
  // Inbound webhooks (POST /api/hooks/agent) — token store, registered
  // contexts, run history. All dashboard-authed; the webhook bearer token is
  // never used from the browser.
  webhooks: () => fetch('/api/webhooks').then(j),
  // `require_signature` defaults to true server-side; a destination is required
  // for every newly created first-class source.
  createWebhookToken: (label: string, requireSignature = true, agent = '') =>
    post('/api/webhooks/tokens', {
      label,
      require_signature: requireSignature,
      agent,
    }).then(j),
  updateWebhookToken: (
    id: string,
    patch: { agent?: string; enabled?: boolean; label?: string },
  ) => fetch('/api/webhooks/tokens/' + encodeURIComponent(id), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  }).then(j),
  deleteWebhookToken: (id: string) => del('/api/webhooks/tokens/' + encodeURIComponent(id)).then(j),
  deleteWebhookContext: (hookId: string) => del('/api/webhooks/contexts/' + encodeURIComponent(hookId)).then(j),
  testWebhook: (message?: string, agent?: string) => post('/api/webhooks/test', { message, agent }).then(j),
  setWebhooksEnabled: (enabled: boolean) => post('/api/webhooks/switch', { enabled }).then(j),
  // Prompts (Agent SOPs)
  prompts: () => fetch('/api/prompts').then(j),
  promptDetail: (name: string, scope?: 'global' | 'local') =>
    fetch('/api/prompts/' + name.split('/').map(encodeURIComponent).join('/')
      + (scope ? '?scope=' + scope : '')).then(j),
  createPrompt: (name: string, content: string, scope: 'global' | 'local') =>
    post('/api/prompts', { name, content, scope }).then(j),
  updatePrompt: (name: string, scope: 'global' | 'local', content: string, baseHash: string) =>
    put('/api/prompts/' + encodeURIComponent(name) + '?scope=' + scope, { content, base_hash: baseHash }).then(j),
  deletePrompt: (name: string, scope: 'global' | 'local') =>
    del('/api/prompts/' + encodeURIComponent(name) + '?scope=' + scope).then(j),
  // Skills
  // sessionKey names the REAL chat slot so the server can resolve THIS chat's
  // project and include its `<project>/.kiro/skills`. Without it the shared
  // `dashboard:ui` placeholder makes the server fall back to "the one project
  // every slot shares", so workspace skills leak between chats on different
  // projects and vanish entirely when two chats disagree (#2457, #3551).
  // agent, when given, scopes the listing to that agent's own skill:// mapping;
  // an agent with no explicit mapping keeps the unfiltered listing. When the
  // mapping IS applied the server answers with the envelope
  // {skills, agent_scoped: true, agent} instead of the bare array, so the
  // picker can cue the scope (and tell "nothing mapped" from "nothing exists").
  // Consume through lib/skillsPayload.ts unwrapSkills() rather than assuming an array.
  // Bounded HERE, not per initiator: react-query dedupes on the key, so the
  // weakest initiator would otherwise decide whether the promise is bounded.
  skills: (sessionKey?: string, agent?: string, signal?: AbortSignal) =>
    withDeadline(SKILLS_TIMEOUT_MS, signal, s =>
      get('/api/skills' + (agent ? '?agent=' + encodeURIComponent(agent) : ''),
          sessionKey, s).then(j)),
  /** Project-skills trust: this chat's grant state plus every stored grant. */
  skillTrust: (sessionKey?: string) => get('/api/skills/-/trust', sessionKey).then(j),
  /** Grant trust to THIS chat's project. The server takes the directory from
   *  the slot, not from us — a caller-supplied path would let any caller
   *  consent for a directory the operator never opened. */
  // expectedKey is the canonical identity returned by the consent snapshot. It
  // is a confirmation, not a selector: the server still derives the directory
  // from the requesting slot and refuses when the current key differs.
  grantSkillTrust: (sessionKey: string | undefined, expectedKey: string) =>
    post('/api/skills/-/trust', { expected_key: expectedKey }, sessionKey).then(j),
  /** Revoke a grant. `path` is optional — omitted revokes this chat's project. */
  revokeSkillTrust: (path?: string, sessionKey?: string) =>
    del('/api/skills/-/trust' + (path ? '?path=' + encodeURIComponent(path) : ''),
        undefined, sessionKey).then(j),
  skill: (name: string) => fetch('/api/skills/' + name.split('/').map(encodeURIComponent).join('/')).then(j),
  /** List the file tree under a skill's directory.  The ``/-/`` separator
   *  disambiguates from a nested skill whose last segment is ``tree``. */
  skillTree: (name: string) => fetch('/api/skills/' + name.split('/').map(encodeURIComponent).join('/') + '/-/tree').then(j),
  /** Read a single file inside a skill's directory by relative path. */
  skillFile: (name: string, relPath: string) =>
    fetch('/api/skills/' + name.split('/').map(encodeURIComponent).join('/') +
          '/-/file?path=' + encodeURIComponent(relPath)).then(j),
  createSkill: (name: string, content: string) => post('/api/skills', { name, content }).then(j),
  updateSkill: (name: string, content: string) => put('/api/skills/' + name.split('/').map(encodeURIComponent).join('/'), { content }).then(j),
  deleteSkill: (name: string) => del('/api/skills/' + name.split('/').map(encodeURIComponent).join('/')).then(j),

  // Steering (Kiro steering files — ~/.kiro/steering + <project>/.kiro/steering)
  // sessionKey names the CHAT SLOT whose project `workspace/` keys resolve
  // against, exactly as it does for kirocrewAgents. Without it the server can
  // only fall back to "the single project every slot shares" and fails closed
  // with two chats on different projects, so project steering silently
  // disappears from a tab that has no way to say why. All five verbs take it:
  // a key created under one project must stay readable, editable and deletable
  // from the same page load.
  steeringFiles: (sessionKey?: string) =>
    fetch('/api/steering', { headers: sessionKey ? { 'X-Session-Key': sessionKey } : { ..._sk } }).then(j),
  steeringFile: (key: string, sessionKey?: string) =>
    fetch('/api/steering/' + key.split('/').map(encodeURIComponent).join('/'), {
      headers: sessionKey ? { 'X-Session-Key': sessionKey } : { ..._sk },
    }).then(j),
  // projectKey is the `project_key` the listing returned: a workspace write
  // echoes it so the server can refuse (409) when the chat slot has since been
  // re-pointed at a different project. The session key names the slot, and the
  // slot is precisely what can move, so it cannot close this on its own.
  createSteering: (name: string, content: string, source?: string, sessionKey?: string, projectKey?: string) =>
    post('/api/steering', { name, content, source }, sessionKey, projectHeader(projectKey)).then(j),
  /** Save a steering file. `declaration` optionally rewrites its front matter
   *  (mode and pattern) SERVER-side — an empty string on a field removes that
   *  key. The editor never splices YAML into its own
   *  textarea: the body is the user's document, and the server's writer
   *  preserves it byte for byte. */
  updateSteering: (
    key: string,
    content: string,
    sessionKey?: string,
    projectKey?: string,
    declaration?: { inclusion?: string; file_match_pattern?: string },
  ) =>
    put('/api/steering/' + key.split('/').map(encodeURIComponent).join('/'), { content, ...(declaration ?? {}) }, sessionKey, projectHeader(projectKey)).then(j),
  deleteSteering: (key: string, sessionKey?: string, projectKey?: string) =>
    del('/api/steering/' + key.split('/').map(encodeURIComponent).join('/'), undefined, sessionKey, projectHeader(projectKey)).then(j),

  // Auto-skill pending queue + lifecycle pin
  skillsPending: () => fetch('/api/skills/-/pending').then(j),
  skillPendingDetail: (slug: string) => fetch('/api/skills/-/pending/' + encodeURIComponent(slug)).then(j),
  approvePendingSkill: (slug: string) => post('/api/skills/-/pending/' + encodeURIComponent(slug) + '/approve', {}).then(j),
  dismissPendingSkill: (slug: string) => post('/api/skills/-/pending/' + encodeURIComponent(slug) + '/dismiss', {}).then(j),
  dismissAllPendingSkills: (slugs: string[]) => post('/api/skills/-/pending/-/dismiss-all', { slugs }).then(j),
  pinSkill: (name: string, pinned: boolean) => post('/api/skills/-/pin', { name, pinned }).then(j),
  /** Opt a skill in/out of full-body injection when its triggers match.
   *  `inject: false` reduces the skill to a one-line pointer on a match. */
  setSkillInjectOnTrigger: (name: string, inject: boolean) =>
    post('/api/skills/-/inject-on-trigger', { name, inject }).then(j),
  /** Context budget: cost data for the skill control plane. */
  skillsBudget: () => get('/api/skills/-/budget').then(j) as Promise<import('../types').SkillBudgetResponse>,
  /** Multi-provider skill discovery (skills.sh, etc.) */
  discoverSkills: (query: string, opts?: { provider?: string; limit?: number }) =>
    get(`/api/skills/-/discover?q=${encodeURIComponent(query)}${opts?.provider ? `&provider=${opts.provider}` : ''}${opts?.limit ? `&limit=${opts.limit}` : ''}`).then(j) as Promise<import('../types').DiscoverSkillsResponse>,
  /** Preview a skill's description, full SKILL.md, and bundle manifest before installing */
  previewDiscoveredSkill: (provider: string, id: string) =>
    get(`/api/skills/-/discover/preview?provider=${encodeURIComponent(provider)}&id=${encodeURIComponent(id)}`).then(j) as Promise<import('../types').DiscoverSkillPreview>,
  /** Install a skill from a provider by ID. Throws ApiError(409) when already installed and overwrite is not set. */
  installDiscoveredSkill: (provider: string, skillId: string, opts?: { name?: string; overwrite?: boolean }) =>
    post('/api/skills/-/discover/install', { provider, skill_id: skillId, name: opts?.name, overwrite: opts?.overwrite }).then(j) as Promise<import('../types').DiscoverInstallResult>,
  // MCP
  mcpServers: () => fetch('/api/mcp').then(j),
  mcpGlobalScopes: () => fetch('/api/mcp/scopes').then(j),
  /** Multi-provider MCP server discovery (official registry, plus the
   *  edition capability provider when one is installed). A query
   *  shorter than 2 chars returns {results: [], providers: [...]} without
   *  hitting any provider — a cheap availability probe. */
  mcpDiscover: (query: string, opts?: { provider?: string; limit?: number }) =>
    get(`/api/mcp/discover?q=${encodeURIComponent(query)}${opts?.provider ? `&provider=${opts.provider}` : ''}${opts?.limit ? `&limit=${opts.limit}` : ''}`).then(j) as Promise<import('../types').McpDiscoverResponse>,
  /** Full description + install-plan preview for one discovered server. */
  mcpDiscoverDetail: (provider: string, id: string) =>
    get(`/api/mcp/discover/detail?provider=${encodeURIComponent(provider)}&id=${encodeURIComponent(id)}`).then(j) as Promise<import('../types').McpDiscoverDetail>,
  /** Install a discovered MCP server. Throws ApiError(409) on name collision. */
  mcpDiscoverInstall: (provider: string, id: string) =>
    post('/api/mcp/discover/install', { provider, id }).then(j) as Promise<import('../types').McpDiscoverInstallResult>,

  mcpCustomAdd: (servers: Record<string, import('../types').McpCustomSpec>, enable: boolean) =>
    post('/api/mcp/custom', { servers, enable }).then(j) as Promise<{ ok: boolean; added: string[]; enabled: boolean }>,

  mcpCustomGet: (name: string) =>
    get(`/api/mcp/custom/${encodeURIComponent(name)}`).then(j) as Promise<import('../types').McpCustomSpecResponse>,

  mcpCustomUpdate: (name: string, spec: import('../types').McpCustomSpec) =>
    put(`/api/mcp/custom/${encodeURIComponent(name)}`, { spec }).then(j) as Promise<{ ok: boolean; name: string }>,
  mcpActive: (agent?: string) => fetch('/api/mcp/active' + (agent ? `?agent=${encodeURIComponent(agent)}` : '')).then(j),
  mcpProbe: () => post('/api/mcp/probe').then(j),
  mcpResetProbeFailures: (name: string) =>
    post('/api/mcp/quarantine/clear', { name }).then(j) as Promise<{ ok: boolean; name: string; released: boolean }>,
  mcpSync: () => post('/api/mcp/sync').then(j),
  mcpApply: (changes: McpApplyChange[]) =>
    post('/api/mcp/apply', { changes }).then(j),
  mcpToggle: (name: string, enabled: boolean) => post('/api/mcp/toggle', { name, enabled }).then(j),
  mcpToggleTool: (server: string, tool: string, enabled: boolean) => post('/api/mcp/toggle-tool', { server, tool, enabled }).then(j),
  mcpToggleAll: (enabled: boolean) => post('/api/mcp/toggle-all', { enabled }).then(j),
  mcpRemove: (name: string) => post('/api/mcp/remove', { name }).then(j),
  mcpOAuthRelay: (server: string, redirectUrl: string) =>
    post('/api/mcp/oauth/relay', { server, redirect_url: redirectUrl }).then(j) as Promise<{ ok: boolean }>,
  // Connections approval-URL mint. POST starts one; GET is the card's feed for it.
  connectionsMint: (slug: string) =>
    post('/api/connections/mint', { slug }).then(j) as Promise<{ ok: boolean; slug: string; state: string; token: string }>,
  connectionsMintState: (slug: string) =>
    fetch(`/api/connections/mint?slug=${encodeURIComponent(slug)}`).then(j) as Promise<ConnectionMintState>,
  // Warm every mintable provider's URL in one activation, so a later Connect
  // serves a URL the warm table already holds instead of paying a cold spawn.
  // Deliberately BODYLESS: what is mintable is a fact about the user's registry
  // and grant state, never a caller's choice, and the bound on what may be
  // spawned stays on the server's side of the wire. `preminting` names the
  // providers warming was STARTED for, which is why it can answer before any of
  // them holds a URL — a card's verdict remains its mint state, never this.
  // Owner-gated, so a non-owner session rejects (403); the caller is expected to
  // treat that as "no warm table", not as an error worth showing.
  connectionsPremint: () =>
    post('/api/connections/premint').then(j) as Promise<{ ok: boolean; preminting: string[] }>,
  // Authorization verdict + first-connect time per visible provider. Additive to
  // the mint feed above; never mints.
  connectionsStatus: () =>
    fetch('/api/connections/status').then(j) as Promise<{ schema_version: number; connections: ConnectionStatus[] }>,
  // Promptless authenticated enumeration through kiro-cli. The runtime owns
  // bearer injection and provider tools/list; this receives only a verdict and count.
  connectionsTest: (slug: string) =>
    post('/api/connections/test', { slug }).then(j) as Promise<ConnectionTestResult>,
  // Dispose an in-flight mint (process, listener, spec). Does NOT touch the MCP
  // config entry — the card owns that. `token` fences a sibling tab's row.
  connectionsCancel: (slug: string, token?: string) =>
    post('/api/connections/cancel', token ? { slug, token } : { slug }).then(j) as Promise<{ ok: boolean; slug: string; dropped: boolean }>,
  // Undo a connection on THIS machine: disposes any in-flight mint, deletes the
  // runtime's stored grant artifacts when they are ours alone, and removes the MCP
  // entry. `grantRemoved` and `grantSurviving` are separate answers because the
  // artifacts are a pair and either half can fail alone; `entryRemoved` is false
  // when the entry configured under this slug points at a different endpoint (so it
  // is not ours to delete); `grantSharedWith` names the other entries using the same
  // endpoint, which is why the grant was deliberately kept. `grantCensusUnreadable`
  // names the sources the census could not read, so the card can say WHICH file to
  // repair -- optional because the other half of `grantCensusIncomplete` (an entry
  // whose URL could not be compared) names no file, and a gateway predating the
  // field sends none.
  connectionsDisconnect: (slug: string) =>
    post('/api/connections/disconnect', { slug }).then(j) as Promise<{ ok: boolean; grantRemoved: boolean; grantSurviving: string[]; entryRemoved: boolean; grantSharedWith: string[]; grantCensusIncomplete: boolean; grantCensusUnreadable?: string[] }>,
  // MCP Gateway (shared pool)
  mcpGatewayStatus: () => fetch('/api/mcp-gateway/status').then(j) as Promise<{ enabled: boolean; stub: string[]; stub_count: number; running: boolean; ping_ok: boolean; supported: boolean }>,
  mcpGatewayEnable: (enabled: boolean) => post('/api/mcp-gateway/enable', { enabled }).then(j) as Promise<{ ok: boolean; enabled: boolean; running: boolean; ping_ok: boolean }>,
  mcpGatewayMetrics: () => fetch('/api/mcp-gateway/metrics').then(j) as Promise<{ running: boolean; size?: number; max_backends?: number; backends: { server: string; agent: string; pid: number | null; sessions: number; idle_s: number; rss_kb: number }[]; warm_pool_hits?: number; warm_pool_misses?: number; warm_pool_hit_rate_pct?: number }>,
  mcpGatewayServers: () => fetch('/api/mcp-gateway/servers').then(j) as Promise<{ servers: McpManagedServer[] }>,
  mcpGatewaySetStub: (name: string, stub: boolean) => post('/api/mcp-gateway/servers/stub', { name, stub }).then(j) as Promise<{ ok: boolean; name: string; stub: boolean; enabled?: boolean; applied?: boolean; restart_required?: boolean; stub_servers?: string[] }>,
  mcpResolveRefresh: () => post('/api/mcp-gateway/resolve-refresh', {}).then(j) as Promise<{ ok: boolean; reason?: string; resolved: Record<string, 'ready' | 'unresolved' | 'error'>; ready?: string[] }>,
  // Starting a measurement pass returns immediately: it spawns two processes per
  // unmeasured server, so the answer arrives through the progress read, not here.
  mcpMeasureStart: () => post('/api/mcp/measure', {}).then(j) as Promise<McpMeasureProgress>,
  mcpMeasureProgress: () => fetch('/api/mcp/measure').then(j) as Promise<McpMeasureProgress>,
  // Batch form of the above -- one config write for the whole set, so "toggle
  // all" can't land the allowlist half-flipped. Like the single form it records
  // rather than applies, and answers `restart_required`.
  //
  // `resolveEligibility` hands the decision to the server: it re-reads the sharing
  // switch and each server's verdict inside the same lock hold that writes them, so
  // the policy and the write cannot disagree. The response then reports `stubbed`
  // and `skipped` rather than echoing the request, because the two differ by design.
  mcpGatewaySetStubMany: (names: string[], stub: boolean, resolveEligibility?: boolean) => post('/api/mcp-gateway/servers/stub', resolveEligibility ? { names, stub, resolve_eligibility: true } : { names, stub }).then(j) as Promise<{ ok: boolean; names: string[]; stub: boolean; stubbed?: string[]; skipped?: Array<{ name: string; reason: string }>; sharing_on?: boolean; applied?: boolean; restart_required?: boolean; stub_servers?: string[] }>,
  // Agent config
  agentConfig: () => fetch('/api/agent/config').then(j),
  saveAgentConfig: (config: object) => put('/api/agent/config', { config }).then(j),
  defaultAgent: () => fetch('/api/config/default-agent').then(j),
  setDefaultAgent: (agent: string) => put('/api/config/default-agent', { agent }).then(j),
  kirocrewConfig: () => fetch('/api/config/kirocrew').then(j),
  saveKirocrewConfig: (agent: object) => put('/api/config/kirocrew', { agent }).then(j) as Promise<{ ok?: boolean; restart_required?: boolean; error?: string }>,
  patchConfig: (path: string, value: unknown) => fetch('/api/config/kirocrew', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path, value }) }).then(j),
  // Owner-only, and absent (404) on an older gateway. Both of those reach the
  // caller as a rejection, which is the intended signal: "no probe information",
  // to be treated as fail-open rather than as a verdict.
  acpBackends: () => fetch('/api/acp-backends').then(j) as Promise<{ backends: AcpBackendProbe[] }>,
  // Optional integrations — backend endpoints are graceful no-ops on a public
  // install (AIM / kiro usage are stubbed). Kept so the UI compiles and
  // degrades gracefully (panels render empty when the feature is absent).
  kiroUsage: () => fetch('/api/usage/kiro').then(j),
  capabilityMcpList: () => fetch('/api/capability/mcp').then(j),
  capabilityMcpInstall: (serverId: string) => post('/api/capability/mcp/install', { server_id: serverId }).then(j),
  capabilityMcpUninstall: (serverId: string) => post('/api/capability/mcp/uninstall', { server_id: serverId }).then(j),
  capabilitySkillsList: () => fetch('/api/capability/skills').then(j),
  capabilitySkillsInstall: (pkg: string) => post('/api/capability/skills/install', { package: pkg }).then(j),
  capabilitySkillsUninstall: (pkg: string) => post('/api/capability/skills/uninstall', { package: pkg }).then(j),
  capabilityAgentsList: () => fetch('/api/capability/agents').then(j),
  capabilityAgentsInstall: (pkg: string) => post('/api/capability/agents/install', { package: pkg }).then(j),
  capabilityAgentsUninstall: (pkg: string) => post('/api/capability/agents/uninstall', { package: pkg }).then(j),
  // Plugin packages (agent-client integrations). The response pairs the installed
  // rows with `out_of_sync` — packages installed as agents but missing their
  // plugin counterpart — so the UI can offer a one-click reconcile.
  capabilityPluginsList: () => fetch('/api/capability/plugins').then(j),
  capabilityPluginsSync: () => post('/api/capability/plugins/sync', {}).then(j),
  capabilityMcpRegistry: () => fetch('/api/capability/mcp/registry').then(j),
  // STT
  sttConfig: () => fetch('/api/config/stt').then(j),
  saveSttConfig: (body: {
    enabled?: boolean
    provider?: string
    model?: string
    streaming?: boolean
    silence_ms?: number
    partial_interval_ms?: number
    endpointing?: boolean
    dictation_panel?: boolean
    transcribe_region?: string
    transcribe_profile?: string
    language_code?: string
  }) => put('/api/config/stt', body).then(j),
  // Recogniser availability plus the model catalog and the progress of any
  // download in flight. Separate from `sttConfig` because it is POLLED while a
  // model is being fetched, and polling the config endpoint would re-read and
  // re-probe configuration several times a second.
  sttStatus: () => fetch('/api/stt/status').then(j),
  // Fetch a model now, so the cost is paid at a moment the user chose rather
  // than in the middle of their first dictation. Returns as soon as the transfer
  // is under way; progress is read from `sttStatus`.
  sttPrepare: (model: string) => post('/api/stt/prepare', { model }).then(j),
  // Fetch the audio decoder (ffmpeg) into the gateway's digest-verified store,
  // for a source install whose OS ships no ffmpeg package. Same 202-then-poll
  // shape as `sttPrepare`; progress arrives on `sttStatus().ffmpeg.download`.
  sttFfmpegDownload: () => post('/api/stt/ffmpeg/download', {}).then(j),
  // Load the model and run one throwaway decode so the first real utterance does
  // not pay for the graph allocation. Fire-and-forget at every call site: a
  // failure only costs the latency it was meant to hide.
  sttPrewarm: () => post('/api/stt/prewarm', {}).then(j),
  sttTranscribe: (blob: Blob, ext = 'webm') => {
    const fd = new FormData()
    fd.append('audio', blob, `recording.${ext}`)
    return fetch('/api/stt/transcribe', { method: 'POST', body: fd }).then(j)
  },
  // Chat
  pullRequestSource: (url: string, refresh = false) => post('/api/source/pull-request', { url, refresh }).then(j) as Promise<PullRequestSource>,
  pullRequestChecks: (url: string) => post('/api/source/pull-request/checks', { url }).then(j) as Promise<{ checks: PullRequestCheck[] }>,
  pullRequestStatuses: (urls: string[]) => post('/api/source/pull-request/status', { urls }).then(j) as Promise<PullRequestStatusBatch>,
  resolvePullRequestThread: (url: string, threadId: string) => post('/api/source/pull-request/resolve', { url, threadId }).then(j) as Promise<{ resolved: boolean }>,
  unresolvePullRequestThread: (url: string, threadId: string) => post('/api/source/pull-request/unresolve', { url, threadId }).then(j) as Promise<{ resolved: boolean }>,
  /** Reply into an existing review thread. Owner-only on the gateway. */
  replyToPullRequestThread: (url: string, threadId: string, body: string) => post('/api/source/pull-request/reply', { url, threadId, body }).then(j) as Promise<{ posted: boolean }>,
  /** Top-level comment on the pull request conversation. */
  commentOnPullRequest: (url: string, body: string) => post('/api/source/pull-request/comment', { url, body }).then(j) as Promise<{ posted: boolean }>,
  enablePullRequestAutoMerge: (url: string, confirmImmediateMerge = false) => post('/api/source/pull-request/auto-merge', { url, confirmImmediateMerge }).then(j) as Promise<{ autoMerge: boolean; mergeMethod: string }>,
  markPullRequestReady: (url: string) => post('/api/source/pull-request/ready', { url }).then(j) as Promise<{ ready: boolean }>,
  pullRequestPendingReview: (url: string) => post('/api/source/pull-request/pending-review', { url }).then(j) as Promise<{ reviewId: string; body: string; comments?: { path: string; line: number | null; body: string }[]; commitId: string; headSha: string; stale: boolean; contentRedacted: boolean; autoMergeArmed: boolean; contentDigest: string; staleDismissalEnabled: boolean }>,
  submitPullRequestReview: (url: string, reviewId: string, event: 'APPROVE' | 'REQUEST_CHANGES' | 'COMMENT', contentDigest: string) =>
    post('/api/source/pull-request/submit-review', { url, reviewId, event, contentDigest }).then(j) as Promise<{ submitted: boolean; event: string }>,
  // Issue sources. `refresh` bypasses the server's cached payload; the panel
  // never polls, so a refresh is always an explicit user action.
  fetchIssueSource: (url: string, refresh = false) => post('/api/source/issue', { url, refresh }).then(j) as Promise<IssueSource>,
  /** Top contributors to an app's source repo (GitHub only). Owner-gated. */
  appContributors: (url: string, refresh = false) => post('/api/source/contributors', { url, refresh }).then(j) as Promise<{ contributors: AppContributor[] }>,
  chatSlots: () => fetch('/api/chat/slots').then(j),
  /** All goal loops across sessions. Returns `{enabled:false, loops:[]}` when
   *  the auto-nudge feature flag is off, so callers need no flag check. */
  autonudgeList: (): Promise<{ enabled: boolean; loops: { slot_key: string; active?: boolean; cycle_count?: number; max_cycles?: number }[] }> =>
    fetch('/api/autonudge').then(j),
  /** Every pull request / issue link a session carries — the unbudgeted read
   *  behind the sidebar's expandable "+N" overflow chip. The slots payload caps
   *  chips per kind, so the links behind that chip are not on the client until
   *  this is called. */
  chatSlotSourceLinks: (slot: string): Promise<{ links: NonNullable<ChatSlot['source_links']>; total: number }> =>
    fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/source-links').then(j),
  chatSlotDetail: (slot: string, limit?: number, before?: number, signal?: AbortSignal) => {
    const p = new URLSearchParams()
    if (limit) p.set('limit', String(limit))
    if (before !== undefined) p.set('before', String(before))
    return fetch('/api/chat/slots/' + encodeURIComponent(slot) + '?' + p, { signal }).then(j)
  },
  /** Create a chat slot. `instance_id` binds the new session to a connected crew
   *  for EXECUTION: it lives in this machine's list and history, and its turns run
   *  over there. The backend opens the peer's slot first, so a peer that is
   *  disconnected or on a different version fails the create rather than yielding
   *  a session that cannot send. */
  createChatSlot: (name?: string, agent?: string, model?: string, mode?: string, memory_mode?: string, title?: string, clean_mode?: boolean, artifact?: string, folder_id?: string, instance_id?: string) => post('/api/chat/slots', { ...(name ? { name } : {}), ...(agent ? { agent } : {}), ...(model ? { model } : {}), ...(mode ? { mode } : {}), ...(memory_mode ? { memory_mode } : {}), ...(title ? { title } : {}), ...(clean_mode !== undefined ? { clean_mode } : {}), ...(artifact ? { artifact } : {}), ...(folder_id ? { folder_id } : {}), ...(instance_id ? { instance_id } : {}) }).then(j) as Promise<ChatSlot>,
  /** Inject silent background context into a slot — consumed on the next user
   * message. Used by the artifact companion chat to name the bound artifact so
   * the user's first message needs no slug boilerplate. */
  chatSlotContext: (slot: string, content: string, opts?: { source?: string; ephemeral?: boolean; maxAge?: number }) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/context', { content, ...(opts?.source ? { source: opts.source } : {}), ...(opts?.ephemeral !== undefined ? { ephemeral: opts.ephemeral } : {}), ...(opts?.maxAge !== undefined ? { maxAge: opts.maxAge } : {}) }).then(j),
  deleteChatSlot: (slot: string) => del('/api/chat/slots/' + encodeURIComponent(slot)).then(j),
  cleanupSessions: (maxInactiveDays: number, activeSlot?: string, dryRun?: boolean) => post('/api/chat/slots/cleanup', { max_inactive_days: maxInactiveDays, active_slot: activeSlot || '', dry_run: !!dryRun }).then(j) as Promise<{ ok: boolean; archived: number; keys: string[]; failed: string[]; dry_run?: boolean; count?: number; active_is_stale?: boolean }>,
  stopChatSlot: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/stop').then(j),
  stopChatSlotForce: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/stop?force=true').then(j),
  cancelQueuedMessage: (slot: string, queueId: string) => del('/api/chat/slots/' + encodeURIComponent(slot) + '/queue/' + encodeURIComponent(queueId)).then(j),
  editQueuedMessage: (slot: string, queueId: string, content: string) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/queue/' + encodeURIComponent(queueId), { content }).then(j),
  reorderQueuedMessages: (slot: string, order: string[]) => put('/api/chat/slots/' + encodeURIComponent(slot) + '/queue/order', { order }).then(j),
  interruptSlot: (slot: string, queueId?: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/interrupt', queueId ? { queue_id: queueId } : {}).then(j),
  /** Ask the sleeping `wait` tool to return early. Cooperative, not a stop:
   *  the turn continues with a normal tool result. `waitId` must name the sleep
   *  currently in flight — the backend answers 409 for a stale one, which is how
   *  a click on a leftover countdown is rejected rather than ending a later wait. */
  endWait: (slot: string, waitId: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/end-wait', { wait_id: waitId }).then(j),
  approveChatSlot: (slot: string, action: string, extra?: Record<string, string>) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/approve', { action, ...extra }).then(j),
  planAction: (slot: string, action: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/plan-action', { action }).then(j),
  resumeChatSlot: (key: string, title?: string) => post('/api/chat/slots/' + encodeURIComponent(key) + '/resume', { name: key, key, title: title || key }).then(j),
  forkChatSlot: (slot: string, atIndex?: number, prompt?: string, mode?: string, direction?: string, messageId?: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/fork', { ...(atIndex !== undefined ? { at_message_index: atIndex } : {}), ...(messageId ? { at_message_id: messageId } : {}), ...(prompt ? { prompt } : {}), ...(mode ? { mode } : {}), ...(direction ? { direction } : {}) }).then(j),
  sideOpen: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/side/open', {}).then(j) as Promise<{ ok: boolean; open: boolean; messages: number; last_run_id: string; created_at: string }>,
  sideTurn: (slot: string, question: string, opts?: { steer?: boolean }) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/side/turn', { question, ...(opts?.steer ? { steer: true } : {}) }).then(j) as Promise<{ ok: boolean; run_id?: string; messages?: number; steered?: boolean; pending?: boolean; queued?: boolean; demoted?: boolean; queue_id?: string; still_queued?: boolean; depth?: number; steer_id?: string }>,
  sideQueueCancel: (slot: string, queueId: string) => del('/api/chat/slots/' + encodeURIComponent(slot) + '/side/queue/' + encodeURIComponent(queueId), { client: TAB_ID }).then(j) as Promise<{ ok: boolean; content: string; depth: number }>,
  sideQueueEdit: (slot: string, queueId: string, content: string) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/side/queue/' + encodeURIComponent(queueId), { content }).then(j) as Promise<{ ok: boolean; depth: number }>,
  sideClose: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/side/close', {}).then(j) as Promise<{ ok: boolean; was_open: boolean }>,
  chatMode: (mode: string, slot?: string) => post('/api/chat/mode', { mode, slot: slot || '' }).then(j),
  generateTitle: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/generate-title').then(j),
  resolveNavLinks: (links: { url: string; context: string }[]) => post('/api/chat/nav/resolve-links', { links }).then(j) as Promise<{ summaries: string[] }>,
  renameSlot: (slot: string, title: string) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/title', { title }).then(j),
  regenerateSlot: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/regenerate').then(j),
  /** Pick an interrupted turn back up. NOT `/resume` — that path opens a history session into a tab. */
  continueSlot: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/continue').then(j),
  switchVariant: (slot: string, index: number) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/switch-variant', { index }).then(j),
  editResend: (slot: string, ts: string, content: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/edit-resend', { ts, content }).then(j),
  rewind: (slot: string, ts: string, content: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/rewind', { ts, content }).then(j),
  slackLink: (slot: string, channel?: string, threadTs?: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/slack-link', (channel || threadTs) ? { ...(channel ? { channel } : {}), ...(threadTs ? { thread_ts: threadTs } : {}) } : undefined).then(j),
  unlinkSlack: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/slack-unlink').then(j),
  // Sets whether turns reach the linked Slack thread. One call for both
  // directions: a session born in its thread has no binding to re-establish, so
  // reconnecting cannot go through slack-link.
  pauseSlack: (slot: string, paused: boolean) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/slack-pause', { paused }).then(j),
  /** `origin` names WHICH non-Slack delivery to act on: the conversation the
   *  session was born in, or its explicit mirror binding. A session can hold
   *  both, and they mute independently, so the row has to say which it is. */
  pauseMirror: (slot: string, paused: boolean, origin = false) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/mirror-pause', { paused, origin }).then(j),
  channelTargets: () => fetch('/api/chat/channel-targets').then(j),
  linkMirror: (slot: string, channelType: string, targetId: string) => post(
    '/api/chat/slots/' + encodeURIComponent(slot) + '/mirror-link',
    { channel_type: channelType, target_id: targetId },
  ).then(j),
  remindMirror: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/mirror-link').then(j),
  unlinkMirror: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/mirror-unlink').then(j),
  slackChannels: () => fetch('/api/slack/channels').then(j),
  // Folders
  chatFolders: () => fetch('/api/chat/folders', { headers: { ..._sk } }).then(j),
  /** `config` carries the folder settings the create modal collects. Each is
   *  omitted when empty so the backend applies its own default. */
  createChatFolder: (name: string, parentId?: string, config?: { project_dir?: string; default_agent?: string; color?: string; tags?: string[] }) =>
    post('/api/chat/folders', { name, parent_id: parentId || '', ...(config ?? {}) }).then(j),
  updateChatFolder: (id: string, body: object) => patch('/api/chat/folders/' + encodeURIComponent(id), body).then(j),
  deleteChatFolder: (id: string) => del('/api/chat/folders/' + encodeURIComponent(id)).then(j),
  setSlotFolder: (slot: string, folderId: string | null) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/folder', { folder_id: folderId || '' }).then(j),
  setSlotColor: (slot: string, colorIndex: number | null) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/color', { color_index: colorIndex }).then(j),
  /** Set a custom per-session color (#rrggbb). The backend clears color_index
   *  when a hex is set and vice versa (mutual exclusion), so callers send one
   *  or the other, never both. */
  setSlotColorHex: (slot: string, colorHex: string | null) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/color', { color_hex: colorHex }).then(j),
  /** Clear BOTH color fields in one PATCH. The endpoint is in-body-gated, so
   *  an index-only null would leave a custom hex behind. */
  clearSlotColor: (slot: string) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/color', { color_index: null, color_hex: null }).then(j),
  setSlotPin: (slot: string, pinned: boolean) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/pin', { pinned }).then(j),
  setSlotMode: (slot: string, mode: string) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/mode', { mode }).then(j),
  // Tags
  chatTags: () => fetch('/api/chat/tags', { headers: { ..._sk } }).then(j),
  createChatTag: (name: string, color?: string, status?: boolean) => post('/api/chat/tags', { name, color: color || '', status: !!status }).then(j),
  updateChatTag: (id: string, body: { name?: string; color?: string; order?: number; status?: boolean }) => patch('/api/chat/tags/' + encodeURIComponent(id), body).then(j),
  deleteChatTag: (id: string) => del('/api/chat/tags/' + encodeURIComponent(id)).then(j),
  setSlotTags: (slot: string, tags: string[]) => fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/tags', { method: 'PUT', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify({ tags }) }).then(j),
  dropSlotToColumn: (slot: string, columnId: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/drop', { column_id: columnId }).then(j),
  tagColumns: () => fetch('/api/chat/tag-columns', { headers: { ..._sk } }).then(j),
  createTagColumn: (body: { name?: string; tag_ids?: string[]; mode?: 'any' | 'all' | 'none'; include_untagged?: boolean; source?: 'tags' | 'state'; state_key?: SessionLaneKey }) => post('/api/chat/tag-columns', body).then(j),
  updateTagColumn: (id: string, body: { name?: string; tag_ids?: string[]; mode?: 'any' | 'all' | 'none'; order?: number; include_untagged?: boolean }) => patch('/api/chat/tag-columns/' + encodeURIComponent(id), body).then(j),
  deleteTagColumn: (id: string) => del('/api/chat/tag-columns/' + encodeURIComponent(id)).then(j),
  reorderTagColumns: (ids: string[]) => fetch('/api/chat/tag-columns/order', { method: 'PUT', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify({ ids }) }).then(j),
  sendChat: (message: string, slot?: string, colorTheme?: string, signal?: AbortSignal, meta?: Record<string, unknown>, steer?: boolean) => {
    // theme_consent_sha is the WIRE TOKEN (two-tier consent). The client just
    // TRANSMITS the raw stored grant (see themeConsentSha) — the server verifies
    // content-binding, injecting the persona only when this token equals sha256
    // of the persona.md it reads. Omitted for a built-in theme, no grant, or a
    // legacy '1'/'' token (must re-prompt). The legacy `theme_consent` boolean
    // is intentionally NOT sent: gating is content-bound server-side.
    //
    // Browse mode is no longer sent per message: it is default-on server-side
    // whenever Browser Mode is enabled in Settings (a durable capability),
    // gated there rather than per turn.
    //
    // `steer` carries the user's "act on this now" intent into a send that
    // starts its OWN turn. The slot is idle, so there is no running turn to
    // inject into; the flag's only effect server-side is to skip the hold that
    // parks a user message behind still-running sub-agents. Sent through this
    // endpoint rather than steerChat because a new turn needs `ws=1` to stream.
    const themeConsent = themeConsentSha(colorTheme)
    return fetch('/api/chat?ws=1', { method: 'POST', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify({ message, slot, ...(colorTheme ? { color_theme: colorTheme } : {}), ...(themeConsent ? { theme_consent_sha: themeConsent } : {}), ...(meta ? { meta } : {}), ...(steer ? { steer: true } : {}) }), signal })
  },
  // Mid-turn steer: inject into the RUNNING turn instead of queueing. Fire-and-forget
  // JSON response ({ok, steered}); the backend falls back to queue if steer is
  // unavailable so the text is never dropped.
  // `ws=1` because a steer that races `chat_done` falls through to the plain send
  // path, whose JSON receipt is gated on it — without it that arm streams SSE.
  // `sendId` is the client-minted correlation id stamped on the optimistic steer
  // bubble (same convention as the plain send path). It rides in `meta`, which
  // BOTH backend paths persist — the accepted-steer row and the new-turn row a
  // steer that races chat_done falls onto — so the bubble is reconcilable, and
  // its accepted-vs-new-turn ambiguity resolvable, by id identity (#6075).
  steerChat: (message: string, slot?: string, sendId?: string) =>
    fetch('/api/chat?ws=1', { method: 'POST', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify({ message, slot, steer: true, ...(sendId ? { meta: { sendId } } : {}) }) }).then(j),
  sessionsHealth: () => fetch('/api/sessions/health').then(j),
  // Knowledge
  knowledgeSearch: (q: string) => get(`/api/knowledge/search-for-context?q=${encodeURIComponent(q)}`).then(j),
  // Notifications
  notifications: () => fetch('/api/notifications').then(j),
  deleteNotification: (ts: string) => del('/api/notifications', { ts }).then(j),
  clearNotifications: () => post('/api/notifications/clear').then(j),
  ackNotification: (ts: string) => post('/api/notifications/ack', { ts }).then(j),
  unackNotification: (ts: string) => post('/api/notifications/unack', { ts }).then(j),
  ackAllNotifications: () => post('/api/notifications/ack-all').then(j),
  notificationChannels: () => fetch('/api/notifications/channels').then(j),
  updateNotificationChannelSettings: (channel: string, settings: { muted?: boolean; priority?: string | null }) =>
    put('/api/notifications/channels/settings', { channel, ...settings }).then(j),
  // Handoff
  handoffChannels: () => fetch('/api/handoff-channels').then(j) as Promise<Record<string, string> | null>,
  handoffSlot: (slot: string, channel?: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/handoff', channel ? { channel } : undefined).then(j),
  // Sessions (history)
  // `excludeOpen` drops sessions already open as a tab — for the sidebar's
  // Older-sessions pane, which is the complement of the tab list above it.
  // Off by default: every other caller wants the full inventory.
  sessions: (limit = 30, offset = 0, preview = false, excludeOpen = false) => fetch('/api/sessions?limit=' + limit + '&offset=' + offset + (preview ? '&preview=1' : '') + (excludeOpen ? '&exclude_open=1' : '')).then(j),
  sessionsSearch: (q: string, limit = 50) => fetch('/api/sessions/search?q=' + encodeURIComponent(q) + '&limit=' + limit).then(j),
  // Federated session search across the local gateway + every CONNECTED remote
  // instance (backend rank-interleaves; remote rows carry instance_id/_name).
  // 403 = instances feature disabled — callers fall back to sessionsSearch.
  instancesSearchSessions: (q: string, limit = 50) => fetch('/api/instances/search-sessions?q=' + encodeURIComponent(q) + '&limit=' + limit).then(j),
  /** What a connected crew can do: its version, agent roster, model list, effort
   *  levels and workspaces. The per-instance counterpart to `/api/agents`,
   *  `/api/models`, `/api/effort-levels` and `/api/workspaces`, which are all
   *  same-origin reads of THIS machine — a session bound to a peer for execution
   *  must offer the peer's options, since picking a crew or model that only exists
   *  here would fail on the first send.
   *
   *  `version_match` is the gate the backend enforces on every dispatch, surfaced
   *  so the UI can explain a refusal before the user types rather than after.
   *  `unavailable` names the reads that failed, per field, so one unreachable
   *  roster disables exactly its own control instead of blanking the shelf. */
  instancesCapabilities: (instanceId: string) =>
    fetch('/api/instances/' + encodeURIComponent(instanceId) + '/capabilities').then(j) as Promise<RemoteCrewCapabilities>,
  sessionDetail: (key: string) => fetch('/api/sessions/' + encodeURIComponent(key)).then(j),
  deleteSession: (key: string) => del('/api/sessions/' + encodeURIComponent(key)).then(j),
  clearSessions: () => del('/api/sessions').then(j),
  // Autocomplete
  autocomplete: (q: string): Promise<{suggestions: string[]}> => fetch('/api/autocomplete?q=' + encodeURIComponent(q)).then(j),
  // Spawn
  spawnList: () => fetch('/api/spawn').then(j),
  spawn: (task: string) => post('/api/spawn', { task }).then(j),
  spawnStatus: (id: string, opts?: { signal?: AbortSignal }) => fetch('/api/spawn/' + encodeURIComponent(id), opts).then(j),
  spawnDelete: (id: string) => del('/api/spawn/' + encodeURIComponent(id)).then(j),
  spawnStopAll: (slot: string) => post('/api/spawn/stop-all', { slot }).then(j),
  spawnRetry: (id: string) => post('/api/spawn/' + encodeURIComponent(id) + '/retry', {}).then(j),
  spawnClear: () => del('/api/spawn').then(j),
  approvals: (): Promise<{ id: string; source?: string; tool?: string; tool_input?: string; tool_call_id?: string; slot?: string; ts?: number }[]> => fetch('/api/approvals').then(j),
  resolveApproval: (id: string, action: 'approve' | 'reject' | 'reject_once') => post('/api/approvals/' + encodeURIComponent(id) + '/' + action, {}).then(j),
  /** Question cards still awaiting an answer, for rehydration after a reload or
   *  websocket reconnect (`question_card` is a one-shot broadcast). A blocking
   *  ask carries `ask_id`; a stateless card carries `card_id` instead. */
  pendingQuestions: (): Promise<{ ask_id?: string; card_id?: string; slot: string; questions: { question: string; header?: string; multiSelect?: boolean; options: { label: string; description?: string }[] }[]; ts?: number }[]> =>
    fetch('/api/ask-question/pending').then(j),
  /** Resolve a pending agent question that carries an `ask_id` — a server-side
   *  wait opened by `POST /api/ask-question`, not the MCP ask_question tool,
   *  which posts a stateless `card_id` card instead. Pass no answers to
   *  dismiss, which unblocks the waiting caller with a timeout-equivalent
   *  result. */
  answerQuestion: (askId: string, answers?: Record<string, string>) =>
    post('/api/ask-question/' + encodeURIComponent(askId) + '/answer',
      answers ? { answers } : { dismissed: true }).then(j),
  /** Retire the slot's needs-input status for a STATELESS card (no `ask_id`),
   *  which blocks nothing and is otherwise removed client-side only — leaving
   *  the sidebar and sessions board claiming the agent is still waiting.
   *  `cardId` is the server-minted identity from the `question_card` payload:
   *  the dismissal is a round-trip, so a newer card can replace this one before
   *  it lands, and the server refuses rather than retiring the wrong ask. */
  dismissQuestionCard: (slot: string, cardId: string) =>
    post('/api/ask-question/dismiss', { slot, card_id: cardId }).then(j),
  // Logs
  logLevel: () => fetch('/api/logs/level').then(j),
  setLogLevel: (level: string) => post('/api/logs/level', { level }).then(j),
  // Task runner
  taskRunnerStatus: () => fetch('/api/taskrunner').then(j),
  startTaskRunner: (spec: string, agent?: string, workspaceDir?: string) => post('/api/taskrunner', { spec, agent: agent || '', workspace_dir: workspaceDir || '' }).then(j),
  cancelTaskRunner: (taskId?: string) => post('/api/taskrunner/cancel', taskId ? { task_id: taskId } : undefined).then(j),
  pauseTaskRun: (taskId: string) => post('/api/taskrunner/' + encodeURIComponent(taskId) + '/pause').then(j),
  deleteTaskRun: (taskId: string) => del('/api/taskrunner/' + encodeURIComponent(taskId)).then(j),
  retryTaskRun: (taskId: string, fromStep: number) => post('/api/taskrunner/' + encodeURIComponent(taskId) + '/retry', { from_step: fromStep }).then(j),
  renameTaskRun: (taskId: string, name: string) => fetch('/api/taskrunner/' + encodeURIComponent(taskId) + '/name', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }).then(j),
  updateTask: (taskId: string, index: number, updates: { title?: string; description?: string; depends_on?: number[]; requires_approval?: boolean; force_approval?: boolean }) => fetch('/api/taskrunner/' + encodeURIComponent(taskId) + '/tasks/' + index, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(updates) }).then(j),
  taskRunToChat: (taskId: string) => post('/api/taskrunner/' + encodeURIComponent(taskId) + '/to-chat').then(j),
  reviewFixStatus: (taskId: string): Promise<ReviewFixTaskResponse> =>
    fetch('/api/taskrunner/' + encodeURIComponent(taskId) + '/review-fix').then(j),
  reviewFixAction: (
    taskId: string,
    input: ReviewFixActionRequest,
  ): Promise<ReviewFixTaskResponse> =>
    post('/api/taskrunner/' + encodeURIComponent(taskId) + '/review-fix/actions', input).then(j),
  // `action` mirrors the backend's own two modes: 'reveal' selects the path in
  // the OS file manager (the default every existing caller relies on), 'open'
  // hands a regular file to its default application. Headless hosts have
  // neither, so the backend answers with `copy` and the path goes to the
  // clipboard instead of the call silently doing nothing.
  // This is a SIDE-EFFECT-FREE transport call: it neither writes the clipboard
  // nor shows a dialog. The single caller `revealOrOpen` owns the clipboard copy,
  // so the degrade is presented in exactly one place instead of in the transport
  // layer where a dialog is a surprise.
  revealPath: (path: string, action: 'open' | 'reveal' = 'reveal') =>
    post('/api/reveal', { path, action }).then(j) as Promise<RevealResult>,
  collectDiagnostics: (body: { note: string; include_logs: boolean }) =>
    post('/api/diagnostics/collect', body).then(j) as Promise<{
      zip_path: string
      filename: string
      included: string[]
      skipped: string[]
      redaction_summary: Record<string, number>
      total_redactions: number
      github_issue_url: string
      download_url: string
    }>,
  /** Compact list of dynamic-workflow runs, newest first — the AUTHORITY for a
   *  run's status.
   *
   *  Live status reaches the chat only as one-shot `workflow_run_event` frames,
   *  so a client that was closed, asleep, or disconnected when a run ended holds
   *  a row that never leaves `running`. This is the read that corrects it (see
   *  `reconcileWorkflowRuns`). Rejects (503) when the workflows service is
   *  unavailable, which callers must treat as "no evidence" — never as "no runs".
   */
  workflowRuns: () =>
    get('/api/workflows/runs').then(j) as Promise<{ runs?: WorkflowRunSummary[] }>,
  workflowDefinitions: (search = '') =>
    get('/api/workflows/definitions' + (search ? `?q=${encodeURIComponent(search)}` : '')).then(j) as Promise<{ definitions: WorkflowDefinition[] }>,
  authorWorkflow: (intent: string) =>
    post('/api/workflows/author', { intent }).then(j) as Promise<{
      ok: boolean
      source: string
      meta?: { name?: string; description?: string }
      derived_from?: WorkflowLineage | null
      errors?: string[]
    }>,
  saveWorkflowDefinition: (body: WorkflowDefinitionWrite) =>
    post('/api/workflows/definitions', body).then(j) as Promise<{ ok: boolean; definition: WorkflowDefinition }>,
  promoteWorkflowRun: (
    runId: string,
    body: Omit<WorkflowDefinitionWrite, 'source' | 'derived_from'>,
  ) =>
    post(`/api/workflows/runs/${encodeURIComponent(runId)}/promote`, body).then(j) as Promise<{
      ok: boolean
      definition: WorkflowDefinition
    }>,
  updateWorkflowDefinition: (
    workflowRef: string,
    body: Omit<WorkflowDefinitionWrite, 'derived_from'> & { expected_revision: number },
  ) => patch(`/api/workflows/definitions/${encodeURIComponent(workflowRef)}`, body).then(j) as Promise<{ ok: boolean; definition: WorkflowDefinition }>,
  runWorkflowDefinition: (workflowRef: string, input: string, args: Record<string, unknown> = {}) =>
    post(`/api/workflows/definitions/${encodeURIComponent(workflowRef)}/run`, { input, args }).then(j) as Promise<{ run_id: string; workflow_id: string; revision: number; slug: string }>,
  refineTaskInput: (input: string) => post('/api/taskrunner/refine', { input }).then(j),
  refineStatus: () => fetch('/api/taskrunner/refine').then(j),
  refineCancel: () => post('/api/taskrunner/refine/cancel').then(j),
  planTask: (input: string, source: string, spec?: string, agent?: string, workspaceDir?: string) =>
    post('/api/taskrunner/plan', { input, source, spec: spec || '', agent: agent || '', workspace_dir: workspaceDir || '' }).then(j),
  cancelPlan: () => post('/api/taskrunner/plan/cancel').then(j),
  updatePlan: (taskId: string, steps: PlanStepInput[]) =>
    put('/api/taskrunner/' + encodeURIComponent(taskId) + '/plan', { steps }).then(j),
  executePlan: (taskId: string, agent?: string, autoApprove?: boolean) =>
    post('/api/taskrunner/' + encodeURIComponent(taskId) + '/execute', { agent: agent || '', auto_approve: !!autoApprove }).then(j),
  planFromChat: (steps: PlanStepInput[], taskId?: string, originalInput?: string) =>
    post('/api/taskrunner/from-chat', { steps, task_id: taskId || '', original_input: originalInput || '' }).then(j),
  planContext: (taskId: string) =>
    fetch('/api/taskrunner/' + encodeURIComponent(taskId) + '/plan-context').then(j),
  /** Download the run's plan as a YAML workflow (re-importable via the "From YAML" tab).
   *  Fetches with the auth header, then triggers a browser download honoring the
   *  server's sanitized Content-Disposition filename. */
  exportPlanYaml: async (taskId: string) => {
    const r = await get('/api/taskrunner/' + encodeURIComponent(taskId) + '/plan.yaml')
    if (!r.ok) {
      const t = await r.text()
      throw new ApiError(r.status, t || `HTTP ${r.status}`)
    }
    const blob = await r.blob()
    const cd = r.headers.get('Content-Disposition') || ''
    const m = /filename="?([^";]+)"?/.exec(cd)
    const filename = (m && m[1]) || `${taskId}.yaml`
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  },
  // Update
  checkUpdate: () => fetch('/api/update/check').then(j) as Promise<UpdateCheckResult>,
  changelog: () => fetch('/api/changelog').then(j),
  releases: () => fetch('/api/releases').then(j),
  applyUpdate: () => post('/api/update').then(j),
  setAutoUpdate: (enabled: boolean) => post('/api/update/auto', { enabled }).then(j),
  /**
   * Move this install onto another release channel. Changes which feed the next
   * check compares against; it never installs anything, so the response is the
   * re-run check against the NEW channel.
   */
  setUpdateChannel: (channel: string) => post('/api/update/channel', { channel }).then(j) as Promise<UpdateCheckResult>,
  /**
   * Restart the gateway without updating. The connection drops as the process
   * image is replaced, so callers must treat a network failure after a 200 as
   * the expected path rather than an error.
   */
  restartGateway: () => post('/api/restart').then(j),
  // In-app wheel update step-up: arming records the request and returns the
  // host command to run; the approval nonce never reaches this client.
  armUpdate: () => post('/api/update/arm').then(j) as Promise<{ ok?: boolean; armed?: boolean; request_id?: string; version?: string; expires_in?: number; approve_command?: string; error?: string; code?: string }>,
  armStatus: () => fetch('/api/update/arm').then(j) as Promise<{ armed: boolean; request_id?: string; version?: string; expires_in?: number; approve_command?: string }>,
  cancelUpdate: () => post('/api/update/cancel').then(j),
  simulateUpdate: (opts?: { delay?: number; fail_at?: string }) => post('/api/update/simulate', opts || {}).then(j),
  pickFiles: () => post('/api/upload').then(j) as Promise<{ paths: string[] }>,
  fileDiff: (path: string) => fetch('/api/file-diff?path=' + encodeURIComponent(path)).then(j) as Promise<{ diff: string; original: string; status?: 'clean' | 'modified' | 'untracked' | 'not_git' | 'error' }>,
  /** Fuzzy file search for @-mention picker. `kind` distinguishes folder hits from files.
   *  `kinds` narrows the result set server-side — 'files' or 'dirs'; omitted returns both.
   *  Filtering server-side rather than dropping unwanted hits here matters because the
   *  backend caps results BEFORE the response, so a client-side filter would silently
   *  shrink an already-capped list. `limit` raises the server's result cap (default 15);
   *  the server clamps it to a fixed ceiling, so a large value cannot amplify the walk. */
  fileSearch: (q: string, project?: string, signal?: AbortSignal, kinds?: 'files' | 'dirs', limit?: number) => {
    const p = new URLSearchParams({ q })
    if (project) p.set('project', project)
    if (kinds) p.set('kinds', kinds)
    if (limit) p.set('limit', String(limit))
    return fetch(`/api/file-search?${p}`, signal ? { signal } : undefined).then(j) as Promise<{ results: Array<{ path: string; name: string; size: number; mtime: number; kind?: 'file' | 'dir' }>; root: string }>
  },
  /** Upload files via browser File API (cross-platform) */
  uploadFiles: async (files: File[]) => {
    // Downscale oversized images client-side so they fit the model's image
    // limits before they ever reach the server (see resizeImage.ts).
    const prepared = await Promise.all(files.map(f => resizeImageForModel(f)))
    const resized = prepared.map(p => p.info).filter((i): i is ResizeInfo => i !== null)
    const fd = new FormData()
    prepared.forEach(p => fd.append('file', p.file))
    const res = await fetch('/api/upload/file', { method: 'POST', body: fd })
    checkSessionExpired(res)
    let body: { paths?: unknown; error?: string }
    try { body = await res.json() } catch { body = {} }
    if (!res.ok) return { paths: [] as string[], error: body.error || res.statusText, resized, resizedByPath: {} as Record<string, ResizeInfo> }
    if (!Array.isArray(body.paths)) return { paths: [] as string[], error: i18nT('api.client.unexpected_server_response'), resized, resizedByPath: {} as Record<string, ResizeInfo> }
    // The server appends one path per multipart 'file' part in order, so
    // paths[i] is prepared[i]'s stored location — zip them to key resize
    // details by the exact server path the attachment chip renders from.
    const paths = body.paths as string[]
    const resizedByPath: Record<string, ResizeInfo> = {}
    prepared.forEach((p, i) => { if (p.info && paths[i]) resizedByPath[paths[i]] = p.info })
    return { ...(body as { paths: string[]; error?: string }), resized, resizedByPath }
  },
  screenshot: () => post('/api/screenshot').then(j) as Promise<{ path: string }>,
  // Custom Themes
  themes: () => fetch('/api/themes').then(j),
  // Dashboard config
  dashboardConfig: () => fetch('/api/dashboard/config').then(j),
  updateDashboardConfig: (body: object) => put('/api/dashboard/config', body).then(j),
  createTheme: (body: object) => post('/api/themes', body).then(j),
  installTheme: (source: { type: 'local'; path: string } | { type: 'github'; url: string }) =>
    post('/api/themes/install', { source }).then(j),
  updateTheme: (slug: string, body: object) => put('/api/themes/' + encodeURIComponent(slug), body).then(j),
  deleteTheme: (slug: string) => del('/api/themes/' + encodeURIComponent(slug)).then(j),
  themeDetail: (slug: string) => fetch('/api/themes/' + encodeURIComponent(slug)).then(j),
  // Workspace theme config (server-authoritative)
  themeBoot: () => fetch('/api/theme/boot').then(j),
  updateThemeConfig: (body: {
    mode?: string
    color?: string
    /** BCP-47 UI language tag; '' means follow the browser. */
    language?: string
    onboarded?: boolean
    import_onboarded?: boolean
    /** Gates the gateway's first heartbeat; see `beacon.telemetry_permitted`. */
    privacy_acked?: boolean
  }) =>
    put('/api/config/theme', body).then(j),
  // Voice
  voiceConfig: () => fetch('/api/voice/config').then(j),
  updateVoiceConfig: (body: object) => put('/api/voice/config', body).then(j),
  voiceVoices: () => fetch('/api/voice/voices').then(j),
  // Paid-AWS-service consent (Amazon Polly for TTS, Amazon Transcribe for STT).
  // The GET reports what would be billed AND performs the identity probe, so it
  // is the call that surfaces the account before the operator agrees to it.
  awsConsent: (service: string) =>
    fetch('/api/aws/consent?service=' + encodeURIComponent(service)).then(j) as Promise<AwsConsentStatus>,
  grantAwsConsent: (service: string, shown: { profile: string; region: string; account: string }) =>
    post('/api/aws/consent', {
      service,
      // Echo back exactly what was on screen. The backend rejects a mismatch, so
      // a confirmation can only ever apply to the account the operator read.
      expectedProfile: shown.profile,
      expectedRegion: shown.region,
      expectedAccount: shown.account,
    }).then(j) as Promise<{ ok?: boolean; error?: string; code?: string; identityDetail?: string }>,
  revokeAwsConsent: (service: string) =>
    del('/api/aws/consent?service=' + encodeURIComponent(service)).then(j) as Promise<{ ok?: boolean; removed?: boolean }>,
  voiceSynthesize: (slot: string, text: string, opts?: { voice?: string; engine?: string; rate?: string; pitch?: string }) =>
    post('/api/voice/synthesize', { slot, text, ...opts }).then(j),

  // Channels
  channelsList: () => fetch('/api/channels').then(j),
  channelPresets: () => fetch('/api/channels/presets').then(j),
  channelGet: (id: string) => fetch('/api/channels/' + encodeURIComponent(id)).then(j),
  channelCreate: (topic: string, agents: object[]) => post('/api/channels', { topic, agents }).then(j),
  channelClose: (id: string) => del('/api/channels/' + encodeURIComponent(id)).then(j),
  channelPost: (id: string, content: string, mention?: string | string[], thread_id?: string) => post('/api/channels/' + encodeURIComponent(id) + '/messages', { content, mention, thread_id }).then(j),
  channelAddAgent: (id: string, agent: object) => post('/api/channels/' + encodeURIComponent(id) + '/agents', agent).then(j),
  channelUpdateAgent: (id: string, aid: string, updates: object) => patch('/api/channels/' + encodeURIComponent(id) + '/agents/' + encodeURIComponent(aid), updates).then(j),
  channelDismissAgent: (id: string, aid: string) => del('/api/channels/' + encodeURIComponent(id) + '/agents/' + encodeURIComponent(aid)).then(j),
  channelWakeAgent: (id: string, aid: string) => post('/api/channels/' + encodeURIComponent(id) + '/agents/' + encodeURIComponent(aid) + '/wake', {}).then(j),
  channelApproveAgent: (id: string, aid: string, action: string, pattern?: string) => post('/api/channels/' + encodeURIComponent(id) + '/agents/' + encodeURIComponent(aid) + '/approve', pattern ? { action, pattern } : { action }).then(j),
  channelClearContext: (id: string, scope: 'all' | 'agent', agentId?: string) => post('/api/channels/' + encodeURIComponent(id) + '/clear-context', scope === 'agent' ? { scope, agent_id: agentId } : { scope }).then(j),

  // --- Apps ---
  // Installed-app payloads are normalized HERE rather than in a queryFn. The
  // registry feed has one consumer, so `AppsPage` can narrow it at its own
  // `useQuery`; `/api/apps` has four (the Apps page, the left rail, the command
  // palette, the migration check), and normalizing per consumer is how the
  // fourth one gets forgotten. This is the boundary all four share.
  listApps: () => fetch('/api/apps').then(j).then(normalizeInstalledApps),
  getApp: (name: string) => fetch('/api/apps/' + encodeURIComponent(name)).then(j).then(normalizeInstalledApp),
  getAppManifest: (name: string) => fetch('/api/apps/' + encodeURIComponent(name) + '/manifest').then(j),
  installApp: (source: string) => post('/api/apps/install', { source }).then(j),
  enableApp: (name: string) => post('/api/apps/' + encodeURIComponent(name) + '/enable').then(j),
  disableApp: (name: string) => post('/api/apps/' + encodeURIComponent(name) + '/disable').then(j),
  openApp: (name: string) => post('/api/apps/' + encodeURIComponent(name) + '/open').then(j),
  uninstallApp: (name: string, keepData = true, keepDependencies?: boolean, keepSpecific?: string[]) =>
    post('/api/apps/' + encodeURIComponent(name) + '/uninstall', {
      ...(keepData === false ? { purge_data: true } : {}),
      ...(keepDependencies ? { keep_dependencies: true } : {}),
      ...(keepSpecific?.length ? { keep_specific: keepSpecific } : {}),
    }).then(j),
  uninstallPreview: (name: string) =>
    fetch('/api/apps/' + encodeURIComponent(name) + '/uninstall/preview').then(j) as Promise<{
      app: string
      resources: { agents: string[]; skills: string[]; crons: string[] }
      dependencies: {
        removable: { id: string; type: string; reason: string }[]
        shared: { id: string; type: string; usedBy: string[]; reason: string }[]
        userInstalled: { id: string; type: string; reason: string }[]
      }
    }>,
  updateApp: (name: string, source?: string) => post('/api/apps/' + encodeURIComponent(name) + '/update', source ? { source } : {}).then(j),
  migrateCleanup: (name: string) => del('/api/apps/' + encodeURIComponent(name) + '/migrate-cleanup').then(j),
  // apps is intentionally `any[]`: each page (AppsPage/MigrationPage/AppDetailPage)
  // narrows it to its own local RegistryApp shape at the call site. Typing it as
  // unknown[] here would break those structural assignments across files.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  listRegistry: () => fetch('/api/apps/registry').then(j) as Promise<{ apps: any[]; serverPlatform: { os: string; arch: string }; categoryOrder?: string[]; editorialSections?: unknown[] }>,
  listRegistries: () => fetch('/api/apps/registries').then(j) as Promise<{ registries: { name: string; repo: string; branch: string; trust?: string }[]; pinned?: { name: string; repo: string; branch: string; trust?: string }[] }>,
  updateRegistries: (registries: { name: string; repo: string; branch: string; trust?: string }[]) => put('/api/apps/registries', { registries }).then(j) as Promise<{ ok: boolean; registries: { name: string; repo: string; branch: string; trust?: string }[]; newlyTrustedHosts: string[] }>,
  // Drops the server's on-disk caches of the published documents (catalog /
  // category order / editorial) so the NEXT listRegistry() is rebuilt from
  // fresh fetches instead of waiting out TTLs. A POST because it is a state
  // change (cache deletion + outbound fetches) and must sit behind the CSRF
  // boundary a GET query param would bypass. Deliberately NOT under
  // /api/apps/: that namespace grants an app named `registry` implicit
  // path ownership (token_auth._app_owns_path).
  refreshAppStore: () => post('/api/app-store/refresh').then(j) as Promise<{ ok: boolean }>,
  refreshRegistries: (repo?: string) => post('/api/apps/registries/refresh', repo ? { repo } : {}).then(j) as Promise<{ ok: boolean; refreshed: string[]; failed: string[]; results: { name: string; ok: boolean }[]; apps: number; lastSyncedAt: string }>,
  installFromRegistry: (name: string) => post('/api/apps/registry/install', { name }).then(j),
  /**
   * Stream install logs via SSE.  Calls `onLog` for each line and resolves
   * with the final result JSON when the install completes.
   */
  installFromRegistryStream: async (
    name: string,
    onLog: (line: string) => void,
    signal?: AbortSignal,
  ): Promise<InstallStreamResult> => {
    const res = await fetch('/api/apps/registry/install-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._sk },
      body: JSON.stringify({ name }),
      signal,
    })
    if (!res.ok || !res.body) {
      const text = await res.text()
      throw new Error(text || `HTTP ${res.status}`)
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        // Parse SSE frames: "event: <type>\ndata: <payload>\n\n"
        const frames = buf.split('\n\n')
        buf = frames.pop() || ''
        for (const frame of frames) {
          if (!frame.trim()) continue
          let eventType = ''
          const dataLines: string[] = []
          for (const line of frame.split('\n')) {
            if (line.startsWith('event: ')) eventType = line.slice(7)
            else if (line.startsWith('data: ')) dataLines.push(line.slice(6))
            else if (line === 'data:') dataLines.push('')
          }
          const data = dataLines.join('\n')
          if (eventType === 'log') {
            onLog(data)
          } else if (eventType === 'done') {
            try { return JSON.parse(data) } catch { return { ok: false, error: data } }
          }
        }
      }
      return { ok: false, error: i18nT('api.client.stream_ended_without_completion') }
    } finally {
      reader.releaseLock()
    }
  },
  registerApp: (body: object) => post('/api/apps/register', body).then(j),

  // Artifacts
  /** List artifacts. `session` scopes to the artifacts one chat session
   *  ORIGINATED; `touchedBy` widens that to every artifact the session was
   *  involved with — created, read, edited, iterated on or reverted — which is
   *  what the in-session Artifacts tab lists. `pinned` filters on the star. */
  artifacts: (filters?: { tag?: string; kind?: string; q?: string; source_path?: string; snippet?: boolean; contentMatch?: boolean; session?: string; touchedBy?: string; pinned?: boolean }) => {
    const params = new URLSearchParams()
    if (filters?.tag) params.set('tag', filters.tag)
    if (filters?.kind) params.set('kind', filters.kind)
    if (filters?.q) params.set('q', filters.q)
    if (filters?.source_path) params.set('source_path', filters.source_path)
    if (filters?.snippet) params.set('snippet', '1')
    if (filters?.contentMatch) params.set('content', '1')
    if (filters?.session) params.set('session', filters.session)
    if (filters?.touchedBy) params.set('touched_by', filters.touchedBy)
    if (filters?.pinned !== undefined) params.set('pinned', filters.pinned ? '1' : '0')
    const s = params.toString()
    return get(`/api/artifacts${s ? `?${s}` : ''}`).then(j)
  },
  artifact: (slug: string) => get(`/api/artifacts/${encodeURIComponent(slug)}`).then(j),
  artifactVersion: (slug: string, version: number) =>
    get(`/api/artifacts/${encodeURIComponent(slug)}/versions/${version}`).then(j),
  artifactVersions: (slug: string) =>
    get(`/api/artifacts/${encodeURIComponent(slug)}/versions`).then(j),
  artifactEvents: (slug: string) =>
    get(`/api/artifacts/${encodeURIComponent(slug)}/events`).then(j),
  /** Record a `referenced` breadcrumb so a chat session that merely OPENED an
   *  artifact still counts as having touched it (the "This session" section
   *  reads the same event log via `touched_by`).
   *
   *  Unlike every other method here this cannot use the shared `post` helper:
   *  that helper hardcodes `X-Session-Key: dashboard:ui`, which the events
   *  handler deliberately maps to "no session" — the breadcrumb would be
   *  recorded against nothing and never surface in the panel. The real slot
   *  key is therefore sent scope-qualified (`dashboard:<slot>`), the same form
   *  MCP callers send and the one the store's `_strip_session_scope` normalizes
   *  to the bare slot that `touched_by` compares against.
   *
   *  Rejects on a non-2xx like the other methods (notably 403 for an incognito
   *  slot, which is correct deny-by-default) — callers treat a breadcrumb as
   *  best-effort and swallow the failure rather than failing the user's click. */
  recordArtifactReference: (slug: string, slot: string, metadata?: { message_ts?: string; widget_index?: number }) =>
    fetch(`/api/artifacts/${encodeURIComponent(slug)}/events`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Session-Key': `dashboard:${slot}` },
      body: JSON.stringify({ type: 'referenced', ...(metadata ? { metadata } : {}) }),
    }).then(j),
  createArtifact: (
    body: { name: string; content: string; kind?: string; source?: string; description?: string; tags?: string[]; slug?: string; source_path?: string; origin_session_key?: string; folder?: string },
    // Pass the owning slot (as `dashboard:<slot>`) when the save is made on
    // behalf of a chat session, so the server's restricted-session gate sees the
    // real session instead of the shared placeholder.
    sessionKey?: string,
  ) =>
    // DEFAULTED from origin_session_key rather than left to each caller. Every
    // save that belongs to a chat session already names it in the body for
    // attribution, so deriving the header from that makes the gate apply by
    // construction -- an opt-in argument meant a caller that only set
    // origin_session_key (WidgetFrame's save-as-artifact) still sent the shared
    // `dashboard:ui` placeholder and an incognito session's write was allowed
    // through. An explicit sessionKey still wins for callers that need to differ.
    post(
      '/api/artifacts',
      body,
      sessionKey
        ?? (body.origin_session_key ? `dashboard:${body.origin_session_key}` : undefined),
    ).then(j),
  /** Atomically resolve a just-created blank document being left: keep it, save
   *  the draft still in the editor, or delete the abandoned shell. The store
   *  decides under its own lock -- deciding here would race a concurrent save. */
  settleBlankArtifact: (
    slug: string,
    body: { untitled_name: string; draft: string; allow_delete: boolean },
  ) =>
    post(`/api/artifacts/${encodeURIComponent(slug)}/settle`, body).then(j) as
      Promise<{ outcome: 'kept' | 'saved' | 'deleted' }>,
  updateArtifact: (slug: string, body: { content?: string; name?: string; kind?: string; description?: string; tags?: string[]; actor?: 'user' | 'agent'; event_type?: 'edited' | 'iterated' | 'reverted'; from_version?: number; snapshot?: boolean }) =>
    patch(`/api/artifacts/${encodeURIComponent(slug)}`, body).then(j),
  deleteArtifact: (slug: string) => del(`/api/artifacts/${encodeURIComponent(slug)}`).then(j),
  // Artifact library folders
  artifactFolders: () => get('/api/artifact-folders').then(j),
  createArtifactFolder: (body: { name: string; parent_id?: string; color?: string }) =>
    post('/api/artifact-folders', body).then(j),
  updateArtifactFolder: (id: string, body: { name?: string; parent_id?: string; order?: number; icon?: string; color?: string }) =>
    patch(`/api/artifact-folders/${encodeURIComponent(id)}`, body).then(j),
  deleteArtifactFolder: (id: string, deleteContents: boolean) =>
    del(`/api/artifact-folders/${encodeURIComponent(id)}?delete_contents=${deleteContents ? 'true' : 'false'}`).then(j),
  /** Move an artifact into a folder ("" = unfile to root). Metadata-only — no version bump. */
  setArtifactFolder: (slug: string, folderId: string) =>
    patch(`/api/artifacts/${encodeURIComponent(slug)}/folder`, { folder_id: folderId }).then(j),
  /** Pin/unpin (favorite) an artifact. Metadata-only — no version bump. */
  // sessionKey: pass `dashboard:<slot>` when the pin is made on behalf of a chat
  // session, so the server's restricted-session gate sees the real session rather
  // than the transport's shared `dashboard:ui` placeholder (which satisfies the
  // `if sk:` check but names no session, so a restricted slot was never gated).
  setArtifactPinned: (slug: string, pinned: boolean, sessionKey?: string) =>
    patch(`/api/artifacts/${encodeURIComponent(slug)}/pin`, { pinned }, sessionKey).then(j),
  /** Virtual list of non-code documents from chat sessions. Pass `session`
   * (a slot key) to scope to a single session. */
  artifactSessionDocs: (session?: string) =>
    get(`/api/artifacts/session-docs${session ? `?session=${encodeURIComponent(session)}` : ''}`).then(j) as Promise<{ docs: SessionDoc[] }>,
  /** Turn a session document path into a real, saved (pinned) file-backed artifact.
   * `originSessionKey` records the saving session; `slug_collided_with` names the plain slug, present only when the store had to suffix it. */
  materializeArtifact: (path: string, originSessionKey?: string) =>
    post('/api/artifacts/materialize', { path, ...(originSessionKey ? { origin_session_key: originSessionKey } : {}) }).then(j) as Promise<{ slug: string; slug_collided_with?: string }>,
  // Artifact publishing / sharing. Local publish/sharing management
  // only — remote-browse / clone / fork surfaces are not part of this edition.
  publishArtifact: (slug: string, body: { visibility?: 'PRIVATE' | 'SHARED' | 'PUBLIC'; shared_with?: string[]; provider?: string }) =>
    post(`/api/artifacts/${encodeURIComponent(slug)}/publish`, body).then(j),
  /** Publishing providers available for an artifact kind, with per-kind support
   *  + sharing/sync/discovery descriptors. Drives the share-panel
   *  picker (selector shown only when >1 capable provider). */
  getArtifactPublishProviders: (kind: string): Promise<{ providers: PublishProviderDescriptor[]; kind: string }> =>
    get(`/api/artifacts/publish-providers?kind=${encodeURIComponent(kind)}`).then(j),
  /** Provider-routed clone/fork of a remote artifact into the local store.
   *  external_id travels in the body (not the path) — provider-native ids can
   *  contain "/", which a path segment can't carry. */
  cloneRemoteArtifact: (provider: string, externalId: string) =>
    post(`/api/remote-artifacts/${encodeURIComponent(provider)}/clone`, { external_id: externalId }).then(j),
  forkRemoteArtifact: (provider: string, externalId: string) =>
    post(`/api/remote-artifacts/${encodeURIComponent(provider)}/fork`, { external_id: externalId }).then(j),
  browseRemoteArtifacts: (provider: string, opts?: { scope?: string; q?: string; pageToken?: string }) =>
    get(
      `/api/remote-artifacts/${encodeURIComponent(provider)}/browse` +
        `?scope=${encodeURIComponent(opts?.scope ?? 'mine')}` +
        (opts?.q ? `&q=${encodeURIComponent(opts.q)}` : '') +
        (opts?.pageToken ? `&pageToken=${encodeURIComponent(opts.pageToken)}` : ''),
    ).then(j),
  // Read-only detail fetch for a provider-hosted artifact (metadata + content),
  // powering the remote-artifact detail page's viewer. external_id can contain
  // "/", so it is percent-encoded into the path segment.
  remoteArtifactDetail: (provider: string, externalId: string) =>
    get(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}`).then(j),
  // Remote artifact comments (view-without-fork): these write straight through
  // to the provider (scope=shared) and are TTL-cached server-side. external_id
  // + comment_id travel in the path, percent-encoded (provider-native ids may
  // contain "/").
  remoteArtifactComments: (provider: string, externalId: string) =>
    get(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}/comments`).then(j),
  postRemoteArtifactComment: (provider: string, externalId: string, body: { text: string; anchor?: object }) =>
    post(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}/comments`, body).then(j),
  replyRemoteArtifactComment: (provider: string, externalId: string, commentId: string, body: { text: string }) =>
    post(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}/comments/${encodeURIComponent(commentId)}/reply`, body).then(j),
  markReviewRemoteComment: (provider: string, externalId: string, commentId: string) =>
    post(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}/comments/${encodeURIComponent(commentId)}/review`, {}).then(j),
  deleteRemoteComment: (provider: string, externalId: string, commentId: string) =>
    del(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}/comments/${encodeURIComponent(commentId)}`).then(j),
  updateArtifactSharing: (slug: string, body: { visibility: 'PRIVATE' | 'SHARED' | 'PUBLIC'; shared_with?: string[] }) =>
    patch(`/api/artifacts/${encodeURIComponent(slug)}/sharing`, body).then(j),
  unpublishArtifact: (slug: string) => del(`/api/artifacts/${encodeURIComponent(slug)}/publish`).then(j),
  /** Stash model-authored HTML and get back a URL a sandboxed iframe can load.
   *
   *  Artifact and widget frames cannot use a `blob:` URL: some WebKit-based
   *  in-app browsers refuse the load outright and can take the page down with
   *  it, and a sandboxed `srcdoc` frame blank-renders on WebKit. The returned
   *  URL carries a short-lived client-bound token and the response pins
   *  `Content-Security-Policy: sandbox`, so the document keeps an opaque origin
   *  even opened top-level. See dashboard/handlers/sandbox_doc.py.
   */
  sandboxDocUrl: (html: string) =>
    post('/api/sandbox-doc', { html }).then(j) as Promise<{ url: string }>,
  refreshArtifactSharing: (slug: string) => post(`/api/artifacts/${encodeURIComponent(slug)}/publish/refresh`, {}).then(j),
  pullLatest: (slug: string) =>
    post(`/api/artifacts/${encodeURIComponent(slug)}/pull-latest`, {}).then(j),
  upstreamStatus: (slug: string) =>
    get(`/api/artifacts/${encodeURIComponent(slug)}/upstream-status`).then(j),
  overwriteRemote: (slug: string) =>
    post(`/api/artifacts/${encodeURIComponent(slug)}/overwrite-remote`, {}).then(j),
  // Artifact comments (durable, local per-slug store)
  artifactComments: (slug: string) =>
    get(`/api/artifacts/${encodeURIComponent(slug)}/comments`).then(j),
  postArtifactComment: (slug: string, body: { text: string; scope?: string; anchor?: object; is_agent?: boolean; author?: string }) =>
    post(`/api/artifacts/${encodeURIComponent(slug)}/comments`, body).then(j),
  replyArtifactComment: (slug: string, commentId: string, body: { text: string; is_agent?: boolean; author?: string }) =>
    post(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}/reply`, body).then(j),
  markCommentReview: (slug: string, commentId: string) =>
    post(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}/review`, {}).then(j),
  resolveComment: (slug: string, commentId: string) =>
    post(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}/resolve`, {}).then(j),
  reopenComment: (slug: string, commentId: string) =>
    post(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}/reopen`, {}).then(j),
  deleteArtifactComment: (slug: string, commentId: string) =>
    del(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}`).then(j),
  editArtifactComment: (slug: string, commentId: string, body: { text: string }) =>
    patch(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}`, body).then(j),
  // Playwright CLI browser view. GET reports; POST /start is idempotent and
  // returns the SAME shape, so a start needs no follow-up read.
  getBrowserInstall: () => get('/api/browser/install').then(j) as Promise<BrowserInstallData>,
  setBrowserToken: (token: string) => put('/api/browser/token', { token }).then(j) as Promise<{ok: boolean; token: boolean}>,
  installBrowserCli: () => post('/api/browser/install', {}).then(j) as Promise<BrowserInstallData>,
  installBrowserEngine: (engine: string) => post('/api/browser/engine', { engine }).then(j) as Promise<BrowserInstallData>,
  getBrowserView: () => get('/api/browser/view').then(j) as Promise<BrowserViewData>,
  startBrowserView: () => post('/api/browser/view/start', {}).then(j) as Promise<BrowserViewData>,
  // Computer use (desktop automation). The PUT returns the refreshed snapshot so
  // the panel re-renders from server truth rather than its optimistic guess.
  getComputerUseConfig: () => get('/api/computer-use/config').then(j) as Promise<ComputerUseConfigData>,
  saveComputerUseConfig: (body: Partial<ComputerUseConfigSave>) =>
    put('/api/computer-use/config', body).then(j) as Promise<ComputerUseConfigData>,
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

  // Auto-research
  researchValidate: (body: object) => post("/api/apps/auto-research/validate", body).then(j),
  researchGrillExpand: (body: object) => post("/api/apps/auto-research/grill/expand", body).then(j),
  researchCampaigns: () => get("/api/apps/auto-research/campaigns").then(j),
  researchCampaign: (id: string) => get("/api/apps/auto-research/campaigns/" + id).then(j),
  researchCreate: (body: object) => post("/api/apps/auto-research/campaigns", body).then(j),
  researchAction: (id: string, action: string, body?: object) => patch("/api/apps/auto-research/campaigns/" + id, { action, ...body }).then(j),
  researchGrillTree: (id: string) => get("/api/apps/auto-research/campaigns/" + id + "/grill-tree").then(j),
  researchNudge: (id: string, text: string) => post("/api/apps/auto-research/campaigns/" + id + "/nudge", { text }).then(j),
  researchAddQuestion: (id: string, text: string) => post("/api/apps/auto-research/campaigns/" + id + "/questions", { text }).then(j),
  researchToKnowledge: (id: string) => post("/api/apps/auto-research/campaigns/" + id + "/to-knowledge", {}).then(j),
  researchKnowledgeStatus: (id: string) => get("/api/apps/auto-research/campaigns/" + id + "/knowledge-status").then(j),
  researchToArtifact: (id: string) => post("/api/apps/auto-research/campaigns/" + id + "/to-artifact", {}).then(j),
  researchReportStatus: (id: string) => get("/api/apps/auto-research/campaigns/" + id + "/report-status").then(j),
  researchReport: (id: string) => get("/api/apps/auto-research/campaigns/" + id + "/report").then(j),
  researchDelete: (id: string) => del("/api/apps/auto-research/campaigns/" + id).then(j),

  artifactTeardown: (slug: string) => post(`/api/deploy/teardown/${slug}`, { confirm: true }).then(j),
  publishProviders: () => get('/api/publish-providers').then(j) as Promise<{ providers: AppPublishProvider[] }>,
  publishToProvider: async (slug: string, providerId: string, provider?: AppPublishProvider, ttlHours?: number) => {
    // Route to the provider's declared endpoint with the payload shape
    // that _do_deploy expects (site_id + artifact_slug). ttl_hours is sent on
    // BOTH preview and confirm so the previewed TTL matches what is deployed
    // (omitting it here makes preview use the backend 72h default).
    const endpoint = provider?.endpoint || '/api/deploy/deploy'
    const payload: Record<string, unknown> = { site_id: slug, artifact_slug: slug, provider_id: providerId }
    if (ttlHours !== undefined) payload.ttl_hours = ttlHours
    const r = await post(endpoint, payload)
    checkSessionExpired(r)
    if (r.ok) { removeAuthBanner(); return r.json() }
    // 409 = scan blocked — parse body so PublishHub can render findings panel
    if (r.status === 409) { return r.json() }
    const errText = await r.text()
    throw new ApiError(r.status, errText || `HTTP ${r.status}`)
  },

  // Tips
  tipsNext: () => get('/api/tips/next').then(jNullable) as Promise<{ tip: { id: string; feature: string; title: string; body: string; why: string; doc: string; doc_link?: string; cta_prompt: string; action?: { kind: 'route'; label: string; route: string } | null } | null; glow: boolean } | null>,
  tipsStatus: () => get('/api/tips/status').then(j) as Promise<{ enabled_config: boolean; opted_out: boolean; cadence_hours: number }>,
  tipsFeedback: (id: string, action: 'shown' | 'ack' | 'dismiss' | 'snooze' | 'helpful' | 'optout' | 'optin') => post('/api/tips/feedback', { id, action }).then(j),
}

export interface AppPublishProvider {
  id: string
  label: string
  icon: string
  kinds: string[]
  configured: boolean
  setupRoute: string
  endpoint: string
}
