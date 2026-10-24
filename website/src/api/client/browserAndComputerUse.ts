/**
 * Agent-driven UI automation on the gateway host: the Playwright CLI browser
 * install, token, engine, view and open-URL, and the computer-use opt-in
 * config.
 */

import { ApiError } from '../apiError'
import type { ClientTransport } from './transport'

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
  /** The gateway's current or most recent install operation. `null` means no
   *  install has run since the gateway started; absent means the gateway
   *  predates job reporting, so the panel falls back to `installing` alone and
   *  cannot say which operation is running. */
  install_job?: BrowserInstallJob | null
  /** Per-engine detection with an explicit `unknown`, which the `browsers`
   *  booleans cannot express. Optional for the same older-gateway reason. */
  browser_status?: Partial<Record<BrowserEngine, BrowserEngineStatus>>
}

/** Engines the managed install can download. */
export type BrowserEngine = 'chromium' | 'firefox' | 'webkit'

/** Passive filesystem detection only: `downloaded` is not a launch check. */
export type BrowserEngineStatus = 'downloaded' | 'missing' | 'unknown'

/**
 * One gateway-owned install operation. Identity, target, stage and outcome all
 * come from the server, so a refreshed page or a second tab attributes progress
 * the same way the tab that pressed the button does.
 */
export interface BrowserInstallJob {
  id: string
  kind: 'cli_setup' | 'engine_download'
  /** Set for `engine_download`; `null` for `cli_setup`. */
  engine: BrowserEngine | null
  status: 'running' | 'succeeded' | 'failed' | 'interrupted'
  stage: 'preparing' | 'installing_cli' | 'downloading_browser' | 'installing_skills' | 'finishing'
  started_at: string
  updated_at: string
  finished_at: string | null
  /** Server-computed at response time; the panel ticks it forward locally. */
  elapsed_s: number
  error_code: 'step_failed' | 'timeout' | 'exception' | 'interrupted' | null
  /** Redacted, then truncated, by the gateway. */
  error_detail: string | null
}

/**
 * The active job carried by a `409 install_already_running` refusal, or `null`
 * for any other rejection.
 *
 * The refusal is still thrown, so a caller keeps its error path; this reads the
 * body `j()` already kept on the `ApiError` instead of discarding it. Validated
 * field by field because the body is whatever answered the request: an older
 * gateway's 409 carries no job at all, and a proxy's carries HTML.
 */
export function browserInstallConflictJob(err: unknown): BrowserInstallJob | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(err.body)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object') return null
  const job = (parsed as { install_job?: unknown }).install_job
  if (!job || typeof job !== 'object') return null
  const { id, kind, status, stage } = job as Record<string, unknown>
  if (typeof id !== 'string' || typeof status !== 'string' || typeof stage !== 'string') return null
  if (kind !== 'cli_setup' && kind !== 'engine_download') return null
  return job as BrowserInstallJob
}

export interface BrowserViewData {
  status: 'running' | 'stopped' | 'unavailable'
  url: string | null
  port: number | null
  reason: string | null
  /** Dashboard-origin relay path (`/browser-view/<token>/`) to FRAME the view
   * through — same origin as the dashboard, so it works over an SSH forward or
   * tunnel with no second port. The embedded per-instance capability token is
   * the relay's auth (the panel frames it in an opaque-origin sandbox that
   * sends no cookies). Null unless running; absent entirely from an older
   * gateway, in which case the panel falls back to framing the absolute
   * loopback `url`. */
  path?: string | null
}

/** Answer of POST /api/browser/open: the Browser panel's address bar on the
 * non-native transport, where the gateway host's Playwright CLI browser is the
 * only thing that can render an external site.
 *
 * A launch that FAILED is a 200 with `ok: false`, exactly as a failed view start
 * comes back as a `stopped` status: `error` is the CLI's own text (a Chromium
 * sandbox refusal, a missing browser build), rendered verbatim so the panel
 * explains the cause instead of showing a blank frame. `view` is the post-attempt
 * status of the `show` dashboard, so the panel can frame it without a second
 * read. `session` is the CLI session name (`panel-<8hex>`), one per chat slot. */
export interface BrowserOpenData {
  ok: boolean
  /** The CLI session this chat slot's browser lives in (`panel-<8hex>`), shown
   * in the view's header so the human can tell it from the other sessions in
   * the framed dashboard's sidebar. */
  session: string
  error: string | null
  /** Whether the framed `show` dashboard attached its viewport to the session.
   * `false` means the page is open but the reader is looking at the frame's
   * session grid and has to pick `session` in its sidebar; the panel says so
   * only in that case. */
  attached: boolean
  view: BrowserViewData
}

/** Summary of the imported Playwright storageState the gateway holds. NEVER
 * carries cookie values — only the counts, domains and expiries needed to
 * describe the set to the user. `imported_at` and `earliest_expiry` are unix
 * seconds; `earliest_expiry` is null when every cookie is session-scoped. */
export interface BrowserCookiesSummary {
  cookie_count: number
  domains: string[]
  earliest_expiry: number | null
  imported_at: number
}

/** Answer of GET /api/browser/cookies: whether an imported cookie set exists and,
 * if so, its value-free summary. */
export interface BrowserCookiesStatus {
  present: boolean
  summary: BrowserCookiesSummary | null
}

/** Answer of POST /api/browser/cookies. `summary` is the value-free description
 * of the newly-stored set. `hot_load` reports the best-effort attempt to apply
 * the cookies to the agent's LIVE browser sessions: `loaded` names the sessions
 * that took them, `failed` maps a session name to why it did not, and `note`
 * explains a case where no session could be enumerated at all. `deliverable`
 * reports whether new agent sessions can receive the saved set. Only a
 * deliverable import without live failures and with an empty `loaded` can tell
 * the user the cookies apply to NEW sessions when the view is running. */
export interface BrowserCookiesImportResult {
  ok: true
  summary: BrowserCookiesSummary
  hot_load: {
    deliverable: boolean
    loaded: string[]
    failed: Record<string, string>
    note?: string
  }
}

/** Answer of DELETE /api/browser/cookies. The stored set is gone (`present:
 * false`); `live` reports the best-effort `cookie-clear` on the agent's LIVE
 * browser sessions: `cleared` names the sessions that took it, `failed` maps a
 * session name to why it did not. A non-empty `failed` means a live session may
 * still be signed in until it closes — the panel says so. */
export interface BrowserCookiesClearResult {
  ok: true
  present: false
  live: {
    cleared: string[]
    failed: Record<string, string>
  }
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
  /** Present when the enable changed; false when the agent spec was not rewritten. */
  agent_spec_rewritten?: boolean
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

export function createBrowserAndComputerUseEndpoints({ get, post, put, del, j }: ClientTransport) {
  const hostAutomation = {
    // Playwright CLI browser view. GET reports; POST /start is idempotent and
    // returns the SAME shape, so a start needs no follow-up read.
    getBrowserInstall: () => get('/api/browser/install').then(j) as Promise<BrowserInstallData>,
    setBrowserToken: (token: string) => put('/api/browser/token', { token }).then(j) as Promise<{ok: boolean; token: boolean}>,
    // Both reject a busy slot with 409 `install_already_running`; the active job
    // rides on that rejection and `browserInstallConflictJob` reads it back.
    installBrowserCli: () => post('/api/browser/install', {}).then(j) as Promise<BrowserInstallData>,
    installBrowserEngine: (engine: BrowserEngine) => post('/api/browser/engine', { engine }).then(j) as Promise<BrowserInstallData>,
    getBrowserView: () => get('/api/browser/view').then(j) as Promise<BrowserViewData>,
    startBrowserView: () => post('/api/browser/view/start', {}).then(j) as Promise<BrowserViewData>,
    // The address bar's launcher: opens an owner-typed URL in the gateway host's
    // Playwright CLI browser (starting the view first) and returns the view status
    // alongside the verdict, so a success frames the view with no follow-up read.
    openInBrowser: (url: string, sessionKey: string) =>
      post('/api/browser/open', { url, session_key: sessionKey }).then(j) as Promise<BrowserOpenData>,
    // Imported browser cookies (a Playwright storageState the gateway stores and
    // applies to the agent's browser sessions). GET reports a value-free summary;
    // POST stores a pasted/loaded export and hot-loads it into live sessions;
    // DELETE clears it. All three are owner-only (403 to a non-owner), which the
    // hook treats as "hide the control" rather than an error.
    // `sessionKey` MUST carry the active slot's key when one is active: the
    // server's restricted-session guard reads X-Session-Key, and the shared
    // `dashboard:ui` default answers "not restricted" — which would let an
    // incognito/temporary slot read the sites a stored credential unlocks, or
    // persist a logged-in browser state the slot promised to keep nothing of.
    // Same contract as `openInBrowser` and the mobile-link surface; the guard
    // answers 403, which the hook maps to "hide the control".
    getBrowserCookies: (sessionKey?: string) =>
      get('/api/browser/cookies', sessionKey).then(j) as Promise<BrowserCookiesStatus>,
    importBrowserCookies: (content: string, sessionKey?: string) =>
      post('/api/browser/cookies', { content }, sessionKey)
        .then(j) as Promise<BrowserCookiesImportResult>,
    clearBrowserCookies: (sessionKey?: string) =>
      del('/api/browser/cookies', undefined, sessionKey).then(j) as Promise<BrowserCookiesClearResult>,
    // Computer use (desktop automation). The PUT returns the refreshed snapshot so
    // the panel re-renders from server truth rather than its optimistic guess.
    getComputerUseConfig: () => get('/api/computer-use/config').then(j) as Promise<ComputerUseConfigData>,
    saveComputerUseConfig: (body: Partial<ComputerUseConfigSave>) =>
      put('/api/computer-use/config', body).then(j) as Promise<ComputerUseConfigData>,
  }

  return { hostAutomation }
}
