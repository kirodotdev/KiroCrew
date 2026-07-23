import { copyToClipboard } from '../utils/clipboard'
import { resizeImageForModel, type ResizeInfo } from '../utils/resizeImage'
import type {
  McpApplyChange,
  PullRequestCheck,
  PullRequestSource,
  PublishProviderDescriptor,
  SessionDoc,
} from '../types'
import { refreshOnce, __resetRefreshOnceForTests } from './refreshOnce'
import { installApiTransport } from './apiTransport'
import { queryClient } from './queryClient'
import { getStoredConsent } from '../utils/themeConsent'

/**
 * Resolve the theme-consent token to transmit for an installed pack's chat.
 *
 * Two-tier consent, wire side: the client no longer computes a trust boolean —
 * it just transmits the RAW stored grant (the sha256 the user granted for the
 * persona content they saw). The backend does the content-binding check: it
 * injects the persona only if this token equals sha256 of the persona.md it
 * reads, so a re-install that swaps persona.md (new sha) no longer matches the
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

export type McpPoolableServer = {
  name: string
  poolable: boolean        // effective: stdio AND not denylisted AND (in_allowlist OR entry_poolable)
  in_allowlist: boolean    // present in config mcp_gateway.poolable_servers
  entry_poolable: boolean  // some agent entry sets poolable:true (third-party escape hatch)
  agents: string[]         // agent configs that declare this server
  transport: string        // "stdio" (poolable-eligible) or "http"
  denylisted: boolean      // in UNPOOLABLE_SERVERS — can never be pooled
}

export const SEARCH_MIN_CHARS = 2  // backend session search threshold (must match kiro_crew.history.SEARCH_MIN_CHARS)

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
  needsClientInstall?: boolean
  clientInstall?: { shell?: string; postInstall?: string }
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
  soft_threshold_pct: number
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
  // Forum per-topic config. chat_ids are negative supergroup ids as strings.
  allow_forum?: boolean
  allowed_forum_chat_ids?: string[]
}

/** Writable Discord config fields sent to PUT /api/discord/config. */
export interface DiscordConfigSave {
  bot_token: string
  bot_token_clear: boolean
  enabled: boolean
  allowed_user_ids: string[]
  allowed_thread_ids: string[]
  soft_threshold_pct: number
}

/** Writable Telegram config fields sent to PUT /api/telegram/config. */
export interface TelegramConfigSave {
  bot_token: string
  bot_token_clear: boolean
  enabled: boolean
  allowed_user_ids: string[]
  soft_threshold_pct: number
  allow_forum?: boolean
  allowed_forum_chat_ids?: string[]
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
}

/** Writable Webex config fields sent to PUT /api/webex/config. */
export interface WebexConfigSave {
  bot_token: string
  bot_token_clear: boolean
  enabled: boolean
  allowed_emails: string[]
}

/** A built-in denied-command rule as returned by GET /api/security/denied-commands. */
export interface DeniedCommandRule {
  id: string
  pattern: string
  category: string
  description: string
  enabled: boolean
  pinned: boolean
}

/** A user-authored denied-command pattern. */
export interface DeniedUserRule {
  id: string
  pattern: string
  enabled: boolean
}

/** Full denied-commands snapshot returned by every denied-commands endpoint. */
export interface DeniedCommandsData {
  builtins: DeniedCommandRule[]
  user_added: DeniedUserRule[]
  disable_all: boolean
  effective_count: number
  governance_locked: boolean
}

let _sessionExpiredShown = false

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
  if (!_sessionExpiredShown) return
  _sessionExpiredShown = false
  const el = document.getElementById('mc-session-expired')
  if (el) el.remove()
  _emitAuthEvent('mc-auth-cleared')
}

// Reactive warm-path recovery: background-poll 403s funnel here, through the
// shared single-flight refreshOnce(). True if the 30-day cookie rotated.
let _silentRefreshExhausted = false

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
  _sessionExpiredShown = false
  __resetRefreshOnceForTests()
  if (typeof document !== 'undefined') {
    document.getElementById('mc-session-expired')?.remove()
  }
}

function showSessionExpiredBanner(): void {
  if (_sessionExpiredShown) return
  _sessionExpiredShown = true
  _emitAuthEvent('mc-auth-required')
  const el = document.createElement('div')
  el.id = 'mc-session-expired'
  el.style.cssText =
    'position:fixed;top:0;left:0;right:0;z-index:99999;background:#b91c1c;color:#fff;' +
    'padding:12px 20px;text-align:center;font:14px/1.5 system-ui;'
  const b = document.createElement('b')
  b.textContent = 'Session expired.'
  const code = document.createElement('code')
  code.textContent = 'kirocrew token'
  code.style.cssText = 'background:#7f1d1d;padding:2px 6px;border-radius:4px'
  const input = document.createElement('input')
  input.type = 'text'
  input.placeholder = 'Paste token URL or raw token…'
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
    // the hub owns recovery. Signal the parent, which force-mints a fresh
    // token and reloads this iframe (mirrors the hub's auto-recovery).
    // The message carries no secret; the parent validates event.origin before
    // acting (see InstancesViewport / resolveTunnelOrigin).
    if (window.parent && window.parent !== window) {
      try {
        window.parent.postMessage({ type: 'mc-auth-expired' }, '*')
      } catch {
        /* cross-origin parent unreachable — fall through to the banner below */
      }
      return r
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
 * HTTP error from an API call. Carries the response status so call sites can
 * branch on specific codes (e.g. 404 = not found, 409 = conflict) without
 * regex-matching the error message text.
 *
 * Extends Error so existing `e instanceof Error ? e.message : String(e)`
 * fallbacks keep working.
 */
export class ApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * Map raw edge/proxy error bodies to a human-readable message. A dashboard
 * served through Builder Tunnels sits behind API Gateway, whose throttle
 * response is the opaque `{"message":"Rate exceeded","throttlingReasons":null}`
 * — rendering that verbatim in an error card is a terrible UX. The mapped
 * message only ever shows after the QueryClient's 429 retry ladder
 * (api/queryClient.ts) is exhausted.
 */
export const friendlyErrText = (status: number, body: string): string => {
  if (status === 429) {
    return 'Rate limited by the tunnel edge (HTTP 429) — too many requests in a burst. '
      + 'The dashboard retries automatically; if this persists, wait a few seconds and reload.'
  }
  // Backends return errors as {"error": "…"} (or detail/message). Unwrap the
  // field so the UI shows the human message with its real newlines, not the
  // raw JSON envelope with escaped \n and \".
  const trimmed = body.trim()
  if (trimmed.startsWith('{')) {
    try {
      const parsed = JSON.parse(trimmed)
      const msg = parsed?.error ?? parsed?.detail ?? parsed?.message
      if (typeof msg === 'string' && msg.trim()) return msg
    } catch { /* not JSON — fall through to raw body */ }
  }
  return body
}

const j = async (r: Response) => {
  checkSessionExpired(r)
  if (r.ok) removeAuthBanner()
  if (!r.ok) {
    const errText = await r.text()
    throw new ApiError(r.status, friendlyErrText(r.status, errText) || `HTTP ${r.status}`)
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
    throw new ApiError(r.status, friendlyErrText(r.status, errText) || `HTTP ${r.status}`)
  }
  return r.json()
}
// X-Session-Key ensures the server-side ephemeral gate always runs.
// Without it, browser requests would skip the `if sk:` check — a fail-open
// path that an MCP subprocess could exploit by omitting its own header.
const _sk = { 'X-Session-Key': 'dashboard:ui' }
const get = (url: string) => fetch(url, { headers: { ..._sk } })
const post = (url: string, body?: object) =>
  fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', ..._sk }, body: body ? JSON.stringify(body) : undefined })
const put = (url: string, body: object) =>
  fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify(body) })
const del = (url: string, body?: object) =>
  fetch(url, { method: 'DELETE', headers: body ? { 'Content-Type': 'application/json', ..._sk } : _sk, body: body ? JSON.stringify(body) : undefined })
const patch = (url: string, body: object) =>
  fetch(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify(body) })

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
    code: 'ok' | 'not_connected' | 'ssh_unreachable' | 'remote_down' | 'tunnel_down' | 'unknown'
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
  was_connected: boolean
  status: InstanceTunnelStatus
}

export interface AddInstanceBody {
  name: string
  ssh_host: string
  remote_port?: number
  ttl?: string
  remote_bin?: string
  id?: string
}

/** Tunnel status surfaced by GET /api/tunnel/status (backend TunnelManager).
 *  Enables mobile dashboard access via a remote tunnel. */
export interface TunnelStatus {
  state: 'disabled' | 'starting' | 'connected' | 'reconnecting' | 'error' | 'stopped'
  url: string
  error: string
  uptime: number
  reconnect_attempt: number
}

export const api = {
  status: () => fetch('/api/status').then(j),
  tunnelStatus: () => fetch('/api/tunnel/status').then(j) as Promise<TunnelStatus>,
  system: () => fetch('/api/system').then(j),
  telemetryStartup: () => fetch('/api/telemetry/startup').then(j),
  securityStats: () => fetch('/api/security/stats').then(j) as Promise<{ denied_commands: number; suspicious_patterns: number; tool_schemas: number; redaction_paths: number }>,
  // Denied commands (Settings → Security). Every endpoint returns the full
  // refreshed snapshot so callers can seed their query cache from the response.
  deniedCommands: () => get('/api/security/denied-commands').then(j) as Promise<DeniedCommandsData>,
  toggleBuiltinDeniedCommand: (id: string, enabled: boolean) =>
    patch('/api/security/denied-commands/builtins/' + encodeURIComponent(id), { enabled }).then(j) as Promise<DeniedCommandsData>,
  setDeniedCommandsDisableAll: (value: boolean) =>
    patch('/api/security/denied-commands/disable-all', { value }).then(j) as Promise<DeniedCommandsData>,
  addUserDeniedCommand: (pattern: string) =>
    post('/api/security/denied-commands/user', { pattern }).then(j) as Promise<DeniedCommandsData>,
  toggleUserDeniedCommand: (id: string, enabled: boolean) =>
    patch('/api/security/denied-commands/user/' + encodeURIComponent(id), { enabled }).then(j) as Promise<DeniedCommandsData>,
  deleteUserDeniedCommand: (id: string) =>
    del('/api/security/denied-commands/user/' + encodeURIComponent(id)).then(j) as Promise<DeniedCommandsData>,
  suggestions: (force?: boolean) => fetch(`/api/suggestions${force ? '?force=1' : ''}`).then(j) as Promise<{ suggestions: string[]; generated_at: number; stale: boolean }>,
  branding: () => fetch('/api/dashboard/branding').then(j) as Promise<{ bot_name: string; avatar: string }>,
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
  vectorDisableEmbeddings: () => post('/api/memory/disable-embeddings').then(j),
  vectorImport: (data: object) => post('/api/memory/import', data).then(j),
  vectorContextPreview: (query?: string) => fetch('/api/memory/context-preview' + (query ? '?q=' + encodeURIComponent(query) : '')).then(j),
  memoryGraph: () => fetch('/api/memory/graph').then(j),
  consolidateMemory: (key: string, includeHistory: boolean) => post('/api/memory/consolidate', { key, include_history: includeHistory }).then(j),
  restartSessions: () => post('/api/sessions/restart').then(j),
  sessionsContext: () => fetch('/api/sessions/context').then(j),
  sessionsUsage: () => fetch('/api/sessions/usage').then(j),
  providerUsage: () => fetch('/api/usage').then(j),
  mcpProbeCache: () => fetch('/api/mcp/probe').then(j),
  // Agents
  agentsInstalled: () => fetch('/api/agents/installed').then(j),
  agentDetail: (name: string) => fetch('/api/agents/detail/' + encodeURIComponent(name)).then(j),
  agentPatch: (name: string, body: object) => fetch('/api/agents/detail/' + encodeURIComponent(name), { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(j),
  agentDelete: (name: string) => fetch('/api/agents/detail/' + encodeURIComponent(name), { method: 'DELETE' }).then(j),
  agentMetadata: (name: string) => fetch('/api/agent-metadata/' + encodeURIComponent(name)).then(j),
  agentMetadataSave: (name: string, content: string) => fetch('/api/agent-metadata/' + encodeURIComponent(name), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }) }).then(j),
  // KiroCrew agents
  kirocrewAgents: () => fetch('/api/agents').then(j),
  syncKirocrewAgents: () => post('/api/agents/sync', {}).then(j),
  createKirocrewAgent: (body: object) => post('/api/agents', body).then(j),
  updateKirocrewAgent: (name: string, body: object) =>
    put('/api/agents/' + encodeURIComponent(name), body).then(j),
  deleteKirocrewAgent: (name: string) =>
    del('/api/agents/' + encodeURIComponent(name)).then(j),
  models: () => fetch('/api/models').then(j),
  effortLevels: (slot?: string) =>
    fetch('/api/effort-levels' + (slot ? '?slot=' + encodeURIComponent(slot) : '')).then(j) as Promise<string[]>,
  slashCommands: () => fetch('/api/slash-commands').then(j),
  chatSlotAgent: (slot: string, agent: string) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/agent', { agent }).then(j),
  chatSlotModel: (slot: string, model: string) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/model', { model }).then(j),
  chatSlotsModel: (model: string, skip_running: boolean) =>
    post('/api/chat/slots/model', { model, skip_running }).then(j) as Promise<{ ok: boolean; model: string; switched: string[]; skipped_running: string[]; unchanged: string[]; failed: string[] }>,
  chatSlotReasoningEffort: (slot: string, reasoning_effort: string) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/reasoning-effort', { reasoning_effort }).then(j),
  chatSlotWorkspace: (slot: string, workspace: string) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/workspace', { workspace }).then(j),
  chatSlotProject: (slot: string, project: string) =>
    post('/api/chat/slots/' + encodeURIComponent(slot) + '/project', { project }).then(j),
  recentProjects: () => fetch('/api/recent-projects').then(j) as Promise<{ dirs: string[] }>,
  browseDirs: (path?: string) => fetch('/api/browse-dirs' + (path ? '?path=' + encodeURIComponent(path) : '')).then(j) as Promise<{ path: string; parent: string; dirs: { name: string; path: string }[] }>,
  browseFiles: (path?: string) => fetch('/api/browse-files' + (path ? '?path=' + encodeURIComponent(path) : '')).then(j) as Promise<{ path: string; parent: string; dirs: { name: string; path: string; mtime: number }[]; files: { name: string; path: string; mtime: number }[] }>,
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
  ackCron: (id: string, summary: string, ts?: string) => post('/api/crons/' + id + '/ack', { summary, ts }).then(j),
  cronHistoryAll: (opts?: { offset?: number; limit?: number; jobId?: string }) => {
    const p = new URLSearchParams()
    if (opts?.offset != null) p.set('offset', String(opts.offset))
    if (opts?.limit != null) p.set('limit', String(opts.limit))
    if (opts?.jobId) p.set('job_id', opts.jobId)
    return fetch('/api/crons/history' + (p.toString() ? '?' + p : ''), { headers: { ..._sk } }).then(j)
  },

  // Lessons
  lessons: () => fetch('/api/lessons').then(j),
  createLesson: (rule: string, category: string) => post('/api/lessons', { rule, category }).then(j),
  deleteLesson: (rule: string) => del('/api/lessons', { rule }).then(j),
  // Hooks
  hooks: () => fetch('/api/hooks').then(j),
  kiroHooks: () => fetch('/api/kiro-hooks').then(j),
  createHook: (body: object) => post('/api/hooks', body).then(j),
  updateHook: (id: string, body: object) => put('/api/hooks/' + id, body).then(j),
  deleteHook: (id: string) => del('/api/hooks/' + id).then(j),
  toggleHook: (id: string) => post('/api/hooks/' + id + '/toggle', {}).then(j),
  testHook: (id: string, context?: string) => post('/api/hooks/' + id + '/test', { context: context || 'test' }).then(j),
  // Prompts (Agent SOPs)
  prompts: () => fetch('/api/prompts').then(j),
  promptDetail: (name: string) => fetch('/api/prompts/' + name.split('/').map(encodeURIComponent).join('/')).then(j),
  // Skills
  skills: () => fetch('/api/skills').then(j),
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
  mcpActive: (agent?: string) => fetch('/api/mcp/active' + (agent ? `?agent=${encodeURIComponent(agent)}` : '')).then(j),
  mcpProbe: () => post('/api/mcp/probe').then(j),
  mcpSync: () => post('/api/mcp/sync').then(j),
  mcpApply: (changes: McpApplyChange[]) =>
    post('/api/mcp/apply', { changes }).then(j),
  mcpToggle: (name: string, enabled: boolean) => post('/api/mcp/toggle', { name, enabled }).then(j),
  mcpToggleTool: (server: string, tool: string, enabled: boolean) => post('/api/mcp/toggle-tool', { server, tool, enabled }).then(j),
  mcpToggleAll: (enabled: boolean) => post('/api/mcp/toggle-all', { enabled }).then(j),
  mcpRemove: (name: string) => post('/api/mcp/remove', { name }).then(j),
  // MCP Gateway (shared pool)
  mcpGatewayStatus: () => fetch('/api/mcp-gateway/status').then(j) as Promise<{ enabled: boolean; running: boolean; ping_ok: boolean }>,
  mcpGatewayEnable: (enabled: boolean) => post('/api/mcp-gateway/enable', { enabled }).then(j) as Promise<{ ok: boolean; enabled: boolean; running: boolean; ping_ok: boolean }>,
  mcpGatewayMetrics: () => fetch('/api/mcp-gateway/metrics').then(j) as Promise<{ running: boolean; size?: number; max_backends?: number; backends: { server: string; agent: string; pid: number | null; sessions: number; idle_s: number; rss_kb: number }[]; warm_pool_hits?: number; warm_pool_misses?: number; warm_pool_hit_rate_pct?: number }>,
  mcpGatewayServers: () => fetch('/api/mcp-gateway/servers').then(j) as Promise<{ servers: McpPoolableServer[] }>,
  mcpGatewaySetPoolable: (name: string, poolable: boolean) => post('/api/mcp-gateway/servers/poolable', { name, poolable }).then(j) as Promise<{ ok: boolean; name: string; poolable: boolean; enabled?: boolean; applied?: boolean; poolable_servers?: string[] }>,
  // Agent config
  agentConfig: () => fetch('/api/agent/config').then(j),
  saveAgentConfig: (config: object) => put('/api/agent/config', { config }).then(j),
  defaultAgent: () => fetch('/api/config/default-agent').then(j),
  setDefaultAgent: (agent: string) => put('/api/config/default-agent', { agent }).then(j),
  kirocrewConfig: () => fetch('/api/config/kirocrew').then(j),
  saveKirocrewConfig: (agent: object) => put('/api/config/kirocrew', { agent }).then(j),
  patchConfig: (path: string, value: unknown) => fetch('/api/config/kirocrew', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path, value }) }).then(j),
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
  capabilityMcpRegistry: () => fetch('/api/capability/mcp/registry').then(j),
  // STT
  sttConfig: () => fetch('/api/config/stt').then(j),
  saveSttConfig: (body: {
    enabled?: boolean
    provider?: string
    model?: string
    mlx_model?: string
    streaming?: boolean
    transcribe_region?: string
    transcribe_profile?: string
    language_code?: string
  }) => put('/api/config/stt', body).then(j),
  sttInstall: () => post('/api/stt/install').then(j),
  sttTranscribe: (blob: Blob, ext = 'webm') => {
    const fd = new FormData()
    fd.append('audio', blob, `recording.${ext}`)
    return fetch('/api/stt/transcribe', { method: 'POST', body: fd }).then(j)
  },
  // Chat
  pullRequestSource: (url: string, refresh = false) => post('/api/source/pull-request', { url, refresh }).then(j) as Promise<PullRequestSource>,
  pullRequestChecks: (url: string) => post('/api/source/pull-request/checks', { url }).then(j) as Promise<{ checks: PullRequestCheck[] }>,
  resolvePullRequestThread: (url: string, threadId: string) => post('/api/source/pull-request/resolve', { url, threadId }).then(j) as Promise<{ resolved: boolean }>,
  chatSlots: () => fetch('/api/chat/slots').then(j),
  chatSlotDetail: (slot: string, limit?: number, before?: number) => {
    const p = new URLSearchParams()
    if (limit) p.set('limit', String(limit))
    if (before !== undefined) p.set('before', String(before))
    return fetch('/api/chat/slots/' + encodeURIComponent(slot) + '?' + p).then(j)
  },
  createChatSlot: (name?: string, agent?: string, model?: string, mode?: string, memory_mode?: string, title?: string, clean_mode?: boolean) => post('/api/chat/slots', { ...(name ? { name } : {}), ...(agent ? { agent } : {}), ...(model ? { model } : {}), ...(mode ? { mode } : {}), ...(memory_mode ? { memory_mode } : {}), ...(title ? { title } : {}), ...(clean_mode !== undefined ? { clean_mode } : {}) }).then(j),
  deleteChatSlot: (slot: string) => del('/api/chat/slots/' + encodeURIComponent(slot)).then(j),
  cleanupSessions: (maxInactiveDays: number, activeSlot?: string, dryRun?: boolean) => post('/api/chat/slots/cleanup', { max_inactive_days: maxInactiveDays, active_slot: activeSlot || '', dry_run: !!dryRun }).then(j) as Promise<{ ok: boolean; archived: number; keys: string[]; failed: string[]; dry_run?: boolean; count?: number; active_is_stale?: boolean }>,
  stopChatSlot: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/stop').then(j),
  stopChatSlotForce: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/stop?force=true').then(j),
  cancelQueuedMessage: (slot: string, queueId: string) => del('/api/chat/slots/' + encodeURIComponent(slot) + '/queue/' + encodeURIComponent(queueId)).then(j),
  editQueuedMessage: (slot: string, queueId: string, content: string) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/queue/' + encodeURIComponent(queueId), { content }).then(j),
  interruptSlot: (slot: string, queueId?: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/interrupt', queueId ? { queue_id: queueId } : {}).then(j),
  approveChatSlot: (slot: string, action: string, extra?: Record<string, string>) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/approve', { action, ...extra }).then(j),
  planAction: (slot: string, action: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/plan-action', { action }).then(j),
  resumeChatSlot: (key: string, title?: string) => post('/api/chat/slots/' + encodeURIComponent(key) + '/resume', { name: key, key, title: title || key }).then(j),
  forkChatSlot: (slot: string, atIndex?: number, prompt?: string, mode?: string, direction?: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/fork', { ...(atIndex !== undefined ? { at_message_index: atIndex } : {}), ...(prompt ? { prompt } : {}), ...(mode ? { mode } : {}), ...(direction ? { direction } : {}) }).then(j),
  sideOpen: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/side/open', {}).then(j) as Promise<{ ok: boolean; open: boolean; messages: number; last_run_id: string; created_at: string }>,
  sideTurn: (slot: string, question: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/side/turn', { question }).then(j) as Promise<{ ok: boolean; run_id: string; messages: number }>,
  sideClose: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/side/close', {}).then(j) as Promise<{ ok: boolean; was_open: boolean }>,
  chatMode: (mode: string, slot?: string) => post('/api/chat/mode', { mode, slot: slot || '' }).then(j),
  generateTitle: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/generate-title').then(j),
  resolveNavLinks: (links: { url: string; context: string }[]) => post('/api/chat/nav/resolve-links', { links }).then(j) as Promise<{ summaries: string[] }>,
  renameSlot: (slot: string, title: string) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/title', { title }).then(j),
  regenerateSlot: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/regenerate').then(j),
  switchVariant: (slot: string, index: number) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/switch-variant', { index }).then(j),
  editResend: (slot: string, ts: string, content: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/edit-resend', { ts, content }).then(j),
  rewind: (slot: string, ts: string, content: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/rewind', { ts, content }).then(j),
  slackLink: (slot: string, channel?: string, threadTs?: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/slack-link', (channel || threadTs) ? { ...(channel ? { channel } : {}), ...(threadTs ? { thread_ts: threadTs } : {}) } : undefined).then(j),
  unlinkSlack: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/slack-unlink').then(j),
  slackChannels: () => fetch('/api/slack/channels').then(j),
  // Folders
  chatFolders: () => fetch('/api/chat/folders', { headers: { ..._sk } }).then(j),
  createChatFolder: (name: string, parentId?: string) => post('/api/chat/folders', { name, parent_id: parentId || '' }).then(j),
  updateChatFolder: (id: string, body: object) => patch('/api/chat/folders/' + encodeURIComponent(id), body).then(j),
  deleteChatFolder: (id: string) => del('/api/chat/folders/' + encodeURIComponent(id)).then(j),
  setSlotFolder: (slot: string, folderId: string | null) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/folder', { folder_id: folderId || '' }).then(j),
  setSlotColor: (slot: string, colorIndex: number | null) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/color', { color_index: colorIndex }).then(j),
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
  createTagColumn: (body: { name?: string; tag_ids?: string[]; mode?: 'any' | 'all' | 'none'; include_untagged?: boolean }) => post('/api/chat/tag-columns', body).then(j),
  updateTagColumn: (id: string, body: { name?: string; tag_ids?: string[]; mode?: 'any' | 'all' | 'none'; order?: number; include_untagged?: boolean }) => patch('/api/chat/tag-columns/' + encodeURIComponent(id), body).then(j),
  deleteTagColumn: (id: string) => del('/api/chat/tag-columns/' + encodeURIComponent(id)).then(j),
  reorderTagColumns: (ids: string[]) => fetch('/api/chat/tag-columns/order', { method: 'PUT', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify({ ids }) }).then(j),
  sendChat: (message: string, slot?: string, colorTheme?: string, signal?: AbortSignal, meta?: Record<string, unknown>, browse?: boolean) => {
    // theme_consent_sha is the WIRE TOKEN (two-tier consent). The client just
    // TRANSMITS the raw stored grant (see themeConsentSha) — the server verifies
    // content-binding, injecting the persona only when this token equals sha256
    // of the persona.md it reads. Omitted for a built-in theme, no grant, or a
    // legacy '1'/'' token (must re-prompt). The legacy `theme_consent` boolean
    // is intentionally NOT sent anymore: gating is content-bound server-side.
    const themeConsent = themeConsentSha(colorTheme)
    return fetch('/api/chat?ws=1', { method: 'POST', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify({ message, slot, ...(colorTheme ? { color_theme: colorTheme } : {}), ...(themeConsent ? { theme_consent_sha: themeConsent } : {}), ...(meta ? { meta } : {}), ...(browse ? { browse: true } : {}) }), signal })
  },
  // Mid-turn steer: inject into the RUNNING turn instead of queueing. Fire-and-forget
  // JSON response ({ok, steered}); the backend falls back to queue if steer is
  // unavailable so the text is never dropped.
  steerChat: (message: string, slot?: string) =>
    fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify({ message, slot, steer: true }) }).then(j),
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
  sessions: (limit = 30, offset = 0, preview = false) => fetch('/api/sessions?limit=' + limit + '&offset=' + offset + (preview ? '&preview=1' : '')).then(j),
  sessionsSearch: (q: string, limit = 50) => fetch('/api/sessions/search?q=' + encodeURIComponent(q) + '&limit=' + limit).then(j),
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
  spawnClear: () => del('/api/spawn').then(j),
  approvals: (): Promise<{ id: string; source?: string; tool?: string; tool_input?: string; tool_call_id?: string; slot?: string; ts?: number }[]> => fetch('/api/approvals').then(j),
  resolveApproval: (id: string, action: 'approve' | 'reject') => post('/api/approvals/' + encodeURIComponent(id) + '/' + action, {}).then(j),
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
  revealPath: (path: string) => post('/api/reveal', { path }).then(j).then((r: { copy?: string }) => {
    if (r.copy) copyToClipboard(r.copy)
    return r
  }),
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
  checkUpdate: () => fetch('/api/update/check').then(j),
  changelog: () => fetch('/api/changelog').then(j),
  applyUpdate: () => post('/api/update').then(j),
  setAutoUpdate: (enabled: boolean) => post('/api/update/auto', { enabled }).then(j),
  cancelUpdate: () => post('/api/update/cancel').then(j),
  simulateUpdate: (opts?: { delay?: number; fail_at?: string }) => post('/api/update/simulate', opts || {}).then(j),
  pickFiles: () => post('/api/upload').then(j) as Promise<{ paths: string[] }>,
  fileDiff: (path: string) => fetch('/api/file-diff?path=' + encodeURIComponent(path)).then(j) as Promise<{ diff: string; original: string; status?: 'clean' | 'modified' | 'untracked' | 'not_git' }>,
  /** Fuzzy file search for @-mention picker */
  fileSearch: (q: string, project?: string, signal?: AbortSignal) => {
    const p = new URLSearchParams({ q })
    if (project) p.set('project', project)
    return fetch(`/api/file-search?${p}`, signal ? { signal } : undefined).then(j) as Promise<{ results: Array<{ path: string; name: string; size: number; mtime: number }>; root: string }>
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
    if (!Array.isArray(body.paths)) return { paths: [] as string[], error: 'Unexpected server response', resized, resizedByPath: {} as Record<string, ResizeInfo> }
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
  updateThemeConfig: (body: { mode?: string; color?: string; onboarded?: boolean }) =>
    put('/api/config/theme', body).then(j),
  // Voice
  voiceConfig: () => fetch('/api/voice/config').then(j),
  updateVoiceConfig: (body: object) => put('/api/voice/config', body).then(j),
  voiceVoices: () => fetch('/api/voice/voices').then(j),
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
  channelApproveAgent: (id: string, aid: string, action: string) => post('/api/channels/' + encodeURIComponent(id) + '/agents/' + encodeURIComponent(aid) + '/approve', { action }).then(j),
  channelClearContext: (id: string, scope: 'all' | 'agent', agentId?: string) => post('/api/channels/' + encodeURIComponent(id) + '/clear-context', scope === 'agent' ? { scope, agent_id: agentId } : { scope }).then(j),

  // --- Apps ---
  listApps: () => fetch('/api/apps').then(j),
  getApp: (name: string) => fetch('/api/apps/' + encodeURIComponent(name)).then(j),
  getAppManifest: (name: string) => fetch('/api/apps/' + encodeURIComponent(name) + '/manifest').then(j),
  installApp: (source: string) => post('/api/apps/install', { source }).then(j),
  enableApp: (name: string) => post('/api/apps/' + encodeURIComponent(name) + '/enable').then(j),
  disableApp: (name: string) => post('/api/apps/' + encodeURIComponent(name) + '/disable').then(j),
  openApp: (name: string) => post('/api/apps/' + encodeURIComponent(name) + '/open').then(j),
  uninstallApp: (name: string, keepData?: boolean, keepDependencies?: boolean, keepSpecific?: string[]) =>
    post('/api/apps/' + encodeURIComponent(name) + '/uninstall', {
      ...(keepData ? { keep_data: true } : {}),
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
  listRegistry: () => fetch('/api/apps/registry').then(j) as Promise<{ apps: any[]; serverPlatform: { os: string; arch: string } }>,
  listRegistries: () => fetch('/api/apps/registries').then(j) as Promise<{ registries: { name: string; repo: string; branch: string }[] }>,
  updateRegistries: (registries: { name: string; repo: string; branch: string }[]) => put('/api/apps/registries', { registries }).then(j),
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
      return { ok: false, error: 'Stream ended without completion' }
    } finally {
      reader.releaseLock()
    }
  },
  registerApp: (body: object) => post('/api/apps/register', body).then(j),

  // Artifacts
  artifacts: (filters?: { tag?: string; kind?: string; q?: string; source_path?: string; snippet?: boolean; contentMatch?: boolean }) => {
    const params = new URLSearchParams()
    if (filters?.tag) params.set('tag', filters.tag)
    if (filters?.kind) params.set('kind', filters.kind)
    if (filters?.q) params.set('q', filters.q)
    if (filters?.source_path) params.set('source_path', filters.source_path)
    if (filters?.snippet) params.set('snippet', '1')
    if (filters?.contentMatch) params.set('content', '1')
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
  createArtifact: (body: { name: string; content: string; kind?: string; source?: string; description?: string; tags?: string[]; slug?: string; source_path?: string; origin_session_key?: string }) =>
    post('/api/artifacts', body).then(j),
  updateArtifact: (slug: string, body: { content?: string; name?: string; description?: string; tags?: string[]; actor?: 'user' | 'agent'; event_type?: 'edited' | 'iterated' | 'reverted'; from_version?: number; snapshot?: boolean }) =>
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
  setArtifactPinned: (slug: string, pinned: boolean) =>
    patch(`/api/artifacts/${encodeURIComponent(slug)}/pin`, { pinned }).then(j),
  /** Virtual list of non-code documents from chat sessions. Pass `session`
   * (a slot key) to scope to a single session. */
  artifactSessionDocs: (session?: string) =>
    get(`/api/artifacts/session-docs${session ? `?session=${encodeURIComponent(session)}` : ''}`).then(j) as Promise<{ docs: SessionDoc[] }>,
  /** Turn a session document path into a real, saved (pinned) file-backed artifact.
   * `originSessionKey` records which chat session saved it (for the Source column). */
  materializeArtifact: (path: string, originSessionKey?: string) =>
    post('/api/artifacts/materialize', { path, ...(originSessionKey ? { origin_session_key: originSessionKey } : {}) }).then(j),
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
  browserAuthRetry: () => post('/api/browser-auth-retry', {}).then(j),
  getBrowserConfig: () => get('/api/browser/config').then(j) as Promise<{extension_mode: boolean; token: boolean}>,
  saveBrowserConfig: (body: {extension_mode: boolean; token: string}) => put('/api/browser/config', body).then(j),
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
  saveWeComConfig: (body: Partial<WeComConfigSave>) => put('/api/wecom/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>,
  // Webex integration config
  getWebexConfig: () => get('/api/webex/config').then(j) as Promise<WebexConfigData>,
  saveWebexConfig: (body: Partial<WebexConfigSave>) => put('/api/webex/config', body).then(j) as Promise<{ ok: boolean; restart_required: boolean; verify_warning: string }>,

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
    // (R12 F3 — omitting it here made preview use the backend 72h default).
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
  tipsNext: () => get('/api/tips/next').then(jNullable) as Promise<{ tip: { id: string; feature: string; title: string; body: string; why: string; doc: string; cta_prompt: string; action?: { kind: 'route'; label: string; route: string } | null } | null; glow: boolean } | null>,
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
