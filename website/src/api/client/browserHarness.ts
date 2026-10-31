/**
 * The agent's browser on the gateway host, as Settings -> Browser reads and
 * drives it: `GET /api/browser/status` and the three owner-only POSTs
 * (`install`, `test`, `disconnect`). The `browser_harness` package owns the
 * server side (RFC browser-live-harness, sections 1-2 and 8).
 */

import type { ClientTransport } from './transport'

/** Which browser the harness drives, in the discovery order the RFC fixes. */
export type BrowserKind = 'user_chrome' | 'edge' | 'kiro_chromium'

/**
 * The relay's link to that browser.
 *
 *   - `connected`     the daemon talks to the browser through the relay.
 *   - `waiting_allow` the user's Chrome shows its "Allow remote debugging?"
 *                     prompt and nothing has answered it yet. Never times out.
 *   - `disconnected`  no browser attached (not launched, or the user pressed
 *                     Disconnect).
 *   - `error`         the last attempt failed; `detail` says how, in the
 *                     gateway's own words.
 */
export type BrowserConnection = 'connected' | 'waiting_allow' | 'disconnected' | 'error'

/** Kiro's own Chromium, the fallback for a machine with no display or no Chrome. */
export interface KiroChromiumState {
  downloaded: boolean
  /** Major version as downloaded (`"156"`), or null until it is. */
  version: string | null
}

/**
 * One gateway-owned install operation (the Browser Harness venv plus Kiro's
 * Chromium). Same shape family as the other install jobs the gateway reports, so a
 * refreshed page attributes progress the same way the tab that pressed the button
 * does.
 */
export interface BrowserInstallJob {
  status: 'running' | 'succeeded' | 'failed' | 'interrupted'
  /** Server-authored stage name; rendered verbatim as a progress hint. */
  stage?: string | null
  /** Redacted, then truncated, by the gateway. Never translated. */
  error_detail?: string | null
}

/**
 * The `credentials` block of `GET /api/browser/status`: who signs Kiro's Chromium
 * in, and how the last apply went. Counts, codes and domain names only, never a
 * cookie value. `provider` is the edition provider's display name, `null` when
 * the edition supplies none (then Settings shows no sign-in control).
 */
export interface BrowserCredentialsStatus {
  provider: string | null
  /** Effective: the config's value, or the edition's default when unset. */
  enabled: boolean
  domains: string[]
  state: 'none' | 'applied' | 'skipped' | 'error' | 'pending' | 'disabled' | 'needs_refresh' | string
  reason?: string | null
  /** Cookies the last apply set. */
  applied?: number
  /** The provider's own state, edition-authored; `message` is shown as-is. */
  provider_status?: { needs_refresh?: boolean; message?: string | null } | null
}

/**
 * `GET /api/browser/status`: `supervisor.status()` plus the install state.
 *
 * `detail` is server-authored prose (the gateway names the real cause), so it is
 * rendered verbatim and never translated. Every optional field is one an older or
 * leaner gateway may omit; the panel degrades to the generic wording for it.
 */
export interface BrowserStatusData {
  /** The Browser Harness venv exists and its pinned version imports. */
  installed: boolean
  /** The Browser Harness version installed (`"0.1.13"`), or null. */
  version: string | null
  browser: { kind: BrowserKind; headless: boolean } | null
  connection: BrowserConnection
  detail: string | null
  chromium?: KiroChromiumState | null
  install_job?: BrowserInstallJob | null
  credentials?: BrowserCredentialsStatus | null
  /** The gateway host's OS, as `sys.platform` spells it (`darwin`, `linux`, `win32`). */
  platform?: string
}

/** Answer of `POST /api/browser/test`: one round trip to the attached browser. */
export interface BrowserTestData {
  ok: boolean
  connection: BrowserConnection
  detail: string | null
}

/** Which parts an install request asks for, and whether a whole Chromium is replaced. */
export interface BrowserInstallRequest {
  components?: Array<'harness' | 'chromium'>
  /** Redownload: replace a Chromium that is already on disk (the repair for a damaged copy). */
  refresh?: boolean
}

export function createBrowserHarnessEndpoints({ get, post, j }: ClientTransport) {
  const browserHarness = {
    getBrowserStatus: () => get('/api/browser/status').then(j) as Promise<BrowserStatusData>,
    // Starts the install job and answers the status at once, so no follow-up read
    // is needed to show the job running. A second POST while one runs is a 409.
    // With no body the gateway installs both parts and keeps a healthy Chromium.
    installBrowserHarness: (req: BrowserInstallRequest = {}) =>
      post('/api/browser/install', req).then(j) as Promise<BrowserStatusData>,
    // Opens one page in the attached browser and reports the connection it found.
    testBrowser: () => post('/api/browser/test', {}).then(j) as Promise<BrowserTestData>,
    // Detaches the relay from the browser; the next agent call re-attaches.
    disconnectBrowser: () => post('/api/browser/disconnect', {}).then(j) as Promise<BrowserStatusData>,
  }
  return { browserHarness }
}
