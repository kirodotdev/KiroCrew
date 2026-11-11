/**
 * The pure half of Computer Use setup: which view a setup document asks for,
 * which steps a platform walks, how long the dialog may wait for a state to
 * move, and how System Settings is opened. No React here, so every rule is
 * asserted directly.
 *
 * The document comes from `GET /api/computer-use/setup` and is the only source
 * of truth. The dialog never decides on its own that something is granted or
 * ready; it renders what the document says and offers the one action that
 * moves it forward.
 */

import type { ComputerUseSetupDoc, ComputerUseSetupState, ComputerUseSetupStep } from '../../api/client'

/** macOS System Settings deep links for the two privacy panes. Byte-equal to the
 *  two URLs the desktop app's external-scheme allowlist accepts
 *  (`electron/external-scheme.js`); any other URL is refused there. */
const PANES = {
  accessibility: { url: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility' },
  screen_recording: { url: 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture' },
} as const
export const PANE_URL = {
  accessibility: PANES.accessibility.url,
  screen_recording: PANES.screen_recording.url,
} as const

export type SetupPane = keyof typeof PANE_URL

/** Hand a System Settings deep link to the OS.
 *
 *  MUST be `window.open`, not `window.location.href`. The dashboard renders
 *  inside an instance <iframe>, and a FRAME navigation is governed by the CSP
 *  `frame-src` directive, which names no custom scheme, so assigning
 *  `location.href` to an `x-apple.systempreferences:` URL is refused and the
 *  button is a dead click in the desktop app. `window.open` is a new top-level
 *  request: the browser hands it to the OS, and in Electron it reaches the main
 *  process's `setWindowOpenHandler`, which forwards the allowlisted URL to
 *  `shell.openExternal`.
 *
 *  Called ONLY from a click handler. Nothing in setup opens System Settings on
 *  its own: not a state change, not a poll, not reopening the dialog.
 */
export function openSystemSettings(pane: SetupPane): void {
  // `noopener` keeps the opened context from holding a handle on the dashboard.
  window.open(PANE_URL[pane], '_blank', 'noopener,noreferrer')
}

/** Every state, in contract order. Tests iterate it so a new state cannot ship
 *  without a view. */
export const SETUP_STATES: readonly ComputerUseSetupState[] = [
  'disabled',
  'not_configured',
  'installing',
  'install_failed',
  'requesting_accessibility',
  'accessibility_denied',
  'requesting_screen_recording',
  'screen_recording_denied',
  'waiting_for_system_settings',
  'restart_required',
  'validating',
  'ready',
  'temporarily_unavailable',
  'permission_revoked',
  'unsupported',
  'error',
]

/** What the dialog shows for a document. */
export type SetupView =
  | 'intro'
  | 'install'
  | 'installing'
  | 'install_failed'
  | 'accessibility'
  | 'screen_recording'
  | 'waiting'
  | 'restart'
  | 'verify'
  | 'ready'
  | 'unavailable'
  | 'revoked'
  | 'session0'
  | 'no_desktop'
  | 'unsupported'
  | 'error'

/** The pane a revoked grant points back at: whichever permission is missing now,
 *  Accessibility first because nothing works without it. */
export function revokedPane(doc: ComputerUseSetupDoc): SetupPane {
  return doc.permissions.screen_recording === 'missing' && doc.permissions.accessibility !== 'missing'
    ? 'screen_recording'
    : 'accessibility'
}

/** The pane the "waiting for System Settings" state is about: the first step
 *  still open, else the first missing grant. */
export function waitingPane(doc: ComputerUseSetupDoc): SetupPane {
  const open = doc.steps.find(s => (s.id === 'accessibility' || s.id === 'screen_recording') && s.status !== 'done' && s.status !== 'not_applicable')
  if (open) return open.id as SetupPane
  return revokedPane(doc)
}

/** Map a document to the one view that answers it. Total over the contract's
 *  states; Windows and Linux states that depend on the desktop session read
 *  `session.kind`, so Session 0 and a missing desktop get their own view
 *  instead of a generic "unsupported". */
export function viewFor(doc: ComputerUseSetupDoc): SetupView {
  switch (doc.state) {
    case 'disabled':
      return 'intro'
    case 'not_configured':
      return 'install'
    case 'installing':
      return 'installing'
    case 'install_failed':
      return 'install_failed'
    case 'requesting_accessibility':
    case 'accessibility_denied':
      return 'accessibility'
    case 'requesting_screen_recording':
    case 'screen_recording_denied':
      return 'screen_recording'
    case 'waiting_for_system_settings':
      return 'waiting'
    case 'restart_required':
      return 'restart'
    case 'validating':
      return 'verify'
    case 'ready':
      return 'ready'
    case 'temporarily_unavailable':
      return doc.session.kind === 'session0' ? 'session0' : 'unavailable'
    case 'permission_revoked':
      return 'revoked'
    case 'unsupported':
      if (doc.session.kind === 'session0') return 'session0'
      if (doc.platform !== 'macos' && doc.platform !== 'unknown' && doc.session.kind === 'none') return 'no_desktop'
      return 'unsupported'
    case 'error':
    default:
      return 'error'
  }
}

/** The step list the progress rail shows. The document's own steps win; a
 *  document that carries none (an early error, a backend that has not run yet)
 *  gets the platform's standard walk, all pending. Windows and Linux never list
 *  a permission step: there is no such screen on those systems. */
export function stepsFor(doc: ComputerUseSetupDoc): ComputerUseSetupStep[] {
  if (doc.steps.length > 0) return doc.steps
  const ids: ComputerUseSetupStep['id'][] =
    doc.platform === 'macos'
      ? ['install', 'accessibility', 'screen_recording', 'verify', 'ready']
      : ['install', 'session', 'verify', 'ready']
  return ids.map(id => ({ id, status: 'pending' }))
}

/** Catalog key for a step's label. A flat record of literal keys so the i18n
 *  key checker can resolve every one. The `session` step reads differently on
 *  Linux, where it is the desktop and compositor check. */
const STEP_LABEL_KEY: Record<string, string> = {
  install: 'pages.settings.computerUseSetup.step_install',
  accessibility: 'pages.settings.computerUseSetup.step_accessibility',
  screen_recording: 'pages.settings.computerUseSetup.step_screen_recording',
  session: 'pages.settings.computerUseSetup.step_session_windows',
  session_linux: 'pages.settings.computerUseSetup.step_session_linux',
  verify: 'pages.settings.computerUseSetup.step_verify',
  ready: 'pages.settings.computerUseSetup.step_ready',
}

export function stepLabelKey(id: string, platform: string): string {
  if (id === 'session' && platform === 'linux') return STEP_LABEL_KEY.session_linux
  return Object.prototype.hasOwnProperty.call(STEP_LABEL_KEY, id) ? STEP_LABEL_KEY[id] : STEP_LABEL_KEY.verify
}

/** States in which the backend is busy on its own and the dialog should look
 *  again shortly. Every other state waits for the user. */
const MOVING_STATES = new Set<ComputerUseSetupState>(['installing', 'validating', 'waiting_for_system_settings'])

/** How often to read the document while waiting, and for how long at most.
 *  Each read asks the driver for a permission and health report, so the wait is
 *  bounded: past the bound the dialog stops and offers Check again, and the
 *  view says it is taking longer than expected. Install gets longer because it
 *  downloads and verifies an archive. */
export const SETUP_POLL_MS = 2000
export const SETUP_POLL_MAX_MS = 120_000
export const SETUP_INSTALL_POLL_MAX_MS = 300_000

/** Next poll delay, or `false` to stop.
 *
 *  `awaitingSettings` is true after the user clicked an Open … Settings button
 *  for the current grant: then a `requesting_*` / `*_denied` state is also worth
 *  re-reading, so the dialog notices the switch being turned on. `since` is when
 *  the current wait began (the last user action or state change), so a Check
 *  again starts a fresh window. */
export function setupPollInterval(
  state: ComputerUseSetupState | undefined,
  awaitingSettings: boolean,
  since: number,
  now: number,
): number | false {
  if (state === undefined) return false
  const waitingOnGrant =
    awaitingSettings &&
    (state === 'requesting_accessibility' ||
      state === 'accessibility_denied' ||
      state === 'requesting_screen_recording' ||
      state === 'screen_recording_denied' ||
      state === 'permission_revoked')
  if (!MOVING_STATES.has(state) && !waitingOnGrant) return false
  const bound = state === 'installing' ? SETUP_INSTALL_POLL_MAX_MS : SETUP_POLL_MAX_MS
  if (now - since >= bound) return false
  return SETUP_POLL_MS
}

/** True once a moving state has outlasted its bound: the view swaps its spinner
 *  for "taking longer than expected" and a Check again button. */
export function waitExpired(state: ComputerUseSetupState | undefined, since: number, now: number): boolean {
  if (state === undefined || !MOVING_STATES.has(state)) return false
  const bound = state === 'installing' ? SETUP_INSTALL_POLL_MAX_MS : SETUP_POLL_MAX_MS
  return now - since >= bound
}

/** Settings panel readiness summary. */
export type Readiness = 'off' | 'ready' | 'needs_setup' | 'attention' | 'unavailable'

export function readinessFor(doc: ComputerUseSetupDoc): Readiness {
  switch (doc.state) {
    case 'disabled':
      return 'off'
    case 'ready':
      return 'ready'
    case 'unsupported':
      return 'unavailable'
    case 'install_failed':
    case 'permission_revoked':
    case 'temporarily_unavailable':
    case 'restart_required':
    case 'error':
      return 'attention'
    default:
      return 'needs_setup'
  }
}

/** The name the user must find in Privacy & Security. The backend names it;
 *  this fallback only covers a document from a backend that left it empty. */
/** The bundle name the upstream driver registers with macOS. */
const DEFAULT_OWNER = { name: 'CuaDriver' } as const

export function ownerName(doc: ComputerUseSetupDoc): string {
  return doc.permissions.owner_name || DEFAULT_OWNER.name
}

/** The query string the chat affordance uses to open the dialog on arrival. */
export const SETUP_QUERY_PARAM = 'setup'
