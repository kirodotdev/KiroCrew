/**
 * Pure derivations behind Settings -> Browser.
 *
 * Each fact the page shows (which state the main card is in, what its badge
 * says, what the browser picker and the "no window" switch read and write) is
 * one function of the gateway's `GET /api/browser/status` answer plus the
 * config, so each rule is testable without rendering.
 */
import type { BrowserConnection, BrowserKind, BrowserStatusData } from '../../api/client'

/** Human text for a rejected call: the `ApiError` message is what the error
 *  journal is keyed on, so `ErrorNotice` recovers the endpoint from it. */
export const failText = (e: unknown, fallback: string): string =>
  e instanceof Error && e.message ? e.message : fallback

/** Whether the attached browser is the user's own (Chrome or Edge), with their logins. */
export function isUserBrowser(kind: BrowserKind | null | undefined): boolean {
  return kind === 'user_chrome' || kind === 'edge'
}

/**
 * The one state the main card shows, and with it the one button it offers.
 *
 *   - `installing`     an install job runs (the gateway's own at startup, or one
 *                      this tab asked for). Disabled spinner, indeterminate bar.
 *   - `install_failed` nothing is installed and the last job failed or was
 *                      interrupted. Red, Retry.
 *   - `not_installed`  nothing is installed and no job ran (a gateway that does
 *                      not install on its own). Install.
 *   - `waiting_allow`  Chrome shows its Allow prompt. Yellow, "Allow in Chrome".
 *   - `error`          the last attach failed. Red, Retry with the gateway's detail.
 *   - `disconnected`   installed, no browser attached. Connect.
 *   - `connected`      Disconnect.
 *
 * A failed job while the engine IS installed (a Redownload that did not finish)
 * does not take over the card: the browser still works, so it is reported on the
 * Engine row under More instead.
 */
export type PrimaryState =
  | 'installing'
  | 'install_failed'
  | 'not_installed'
  | 'waiting_allow'
  | 'error'
  | 'disconnected'
  | 'connected'

export function primaryState(status: BrowserStatusData, installPending = false): PrimaryState {
  if (installPending || status.install_job?.status === 'running') return 'installing'
  if (!status.installed) {
    // An interrupted job (the gateway stopped mid-install) did not finish either;
    // its `error_detail` says so and Retry is the move.
    const ended = status.install_job?.status
    return ended === 'failed' || ended === 'interrupted' ? 'install_failed' : 'not_installed'
  }
  return status.connection
}

/** Badge tone per connection state. `waiting_allow` is the one yellow state. */
export const CONNECTION_VARIANT: Record<BrowserConnection, 'ok' | 'warn' | 'err' | 'muted'> = {
  connected: 'ok',
  waiting_allow: 'warn',
  disconnected: 'muted',
  error: 'err',
}

/**
 * Catalog KEY per connection state. A flat record of literal keys so the
 * key-reference gate resolves `i18nT(CONNECTION_LABEL_KEY[state])` statically;
 * module scope is fine because these are keys, not copy.
 */
export const CONNECTION_LABEL_KEY: Record<BrowserConnection, string> = {
  connected: 'pages.settings.browserPanel.connection_connected',
  waiting_allow: 'pages.settings.browserPanel.connection_waiting_allow',
  disconnected: 'pages.settings.browserPanel.connection_disconnected',
  error: 'pages.settings.browserPanel.connection_error',
}

/** Catalog KEY per browser kind, for the main card's title. */
export const BROWSER_KIND_LABEL_KEY: Record<BrowserKind, string> = {
  user_chrome: 'pages.settings.browserPanel.kind_user_chrome',
  edge: 'pages.settings.browserPanel.kind_edge',
  kiro_chromium: 'pages.settings.browserPanel.kind_kiro_chromium',
}

/* ── `browser.choice` as two controls ──
 *
 * The config key keeps its four values (`auto`, `user_chrome`, `kiro_chromium`,
 * `headless`) and the CLI still writes any of them. The page splits it in two:
 * the "Which browser" picker offers three, and "Run with no window" under More
 * is the fourth. `headless` IS Kiro's Chromium without a window, so:
 *
 *   - the picker reads `headless` as `kiro_chromium`;
 *   - picking `kiro_chromium` while `headless` writes nothing (it is already
 *     Kiro's Chromium); picking anything else writes that value, which turns
 *     the switch off;
 *   - the switch on writes `headless`; off writes `kiro_chromium` -- the same
 *     browser, now with a window. It never guesses the user's earlier pick.
 */
export const PICKER_CHOICES = ['auto', 'user_chrome', 'kiro_chromium'] as const
export type PickerChoice = (typeof PICKER_CHOICES)[number]

/** What the picker shows for a stored `browser.choice`. Unknown values read as `auto`, the default. */
export function pickerValueOf(choice: string): PickerChoice {
  if (choice === 'headless') return 'kiro_chromium'
  return (PICKER_CHOICES as readonly string[]).includes(choice) ? (choice as PickerChoice) : 'auto'
}

/** The value to write when the picker changes, or null when nothing changes. */
export function choiceFromPicker(current: string, picked: string): string | null {
  return pickerValueOf(current) === picked ? null : picked
}

/** Whether the "Run with no window" switch is on. */
export function isHeadlessChoice(choice: string): boolean {
  return choice === 'headless'
}

/** The value to write when the "Run with no window" switch flips. */
export function choiceFromHeadless(on: boolean): string {
  return on ? 'headless' : 'kiro_chromium'
}

/* ── sign-in for Kiro's Chromium (`browser.credentials`) ── */

/** The gateway's `CREDENTIAL_DOMAIN_PATTERN`: two or more lowercase DNS labels. */
const CREDENTIAL_DOMAIN_RE = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$/

/**
 * One sign-in domain as the gateway stores it, or `null`. Trims, lowercases and
 * drops one leading dot (`.example.com` reads as `example.com`, which covers its
 * subdomains); refuses a port, a path, a wildcard, a single label and an IP
 * literal, the same as the credential seam.
 */
export function normalizeCredentialDomain(raw: string): string | null {
  const host = raw.trim().toLowerCase().replace(/^\./, '')
  if (host.length > 253 || !CREDENTIAL_DOMAIN_RE.test(host)) return null
  if (/^[0-9.]+$/.test(host)) return null
  return host
}

/** Catalog KEY of the sign-in row's status line for the states with a fixed line;
 *  `applied` (a count) and `needs_refresh` (the provider's own hint) are built in
 *  `credentialLine`. */
export const CREDENTIAL_LINE_KEY: Record<string, string> = {
  pending: 'pages.settings.browserPanel.credentials_pending',
  disabled: 'pages.settings.browserPanel.credentials_disabled',
  skipped: 'pages.settings.browserPanel.credentials_skipped',
}
