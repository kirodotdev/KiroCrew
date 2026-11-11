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
      // Installed but never checked: the backend reports not_configured with
      // the verify step current, and the answer is Run check, not another
      // download.
      return doc.steps.some(s => s.id === 'verify' && s.status === 'current') ? 'verify' : 'install'
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

/** Catalog key for each desktop session kind. A flat record of literal keys so
 *  the i18n key checker can resolve every one. */
const SESSION_KIND_KEY: Record<ComputerUseSetupDoc['session']['kind'], string> = {
  x11: 'pages.settings.computerUseSetup.session_x11',
  wayland: 'pages.settings.computerUseSetup.session_wayland',
  windows_interactive: 'pages.settings.computerUseSetup.session_windows_interactive',
  session0: 'pages.settings.computerUseSetup.session_session0',
  aqua: 'pages.settings.computerUseSetup.session_aqua',
  none: 'pages.settings.computerUseSetup.session_none',
}

export function sessionKindKey(kind: ComputerUseSetupDoc['session']['kind']): string {
  return Object.prototype.hasOwnProperty.call(SESSION_KIND_KEY, kind) ? SESSION_KIND_KEY[kind] : SESSION_KIND_KEY.none
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

/** The grant screens: the document only moves here when the user flips a
 *  switch in System Settings. */
const GRANT_STATES = new Set<ComputerUseSetupState>([
  'requesting_accessibility',
  'accessibility_denied',
  'requesting_screen_recording',
  'screen_recording_denied',
  'permission_revoked',
])

/** True while the user is in System Settings for a grant: the backend says it
 *  is waiting for System Settings, or the user clicked an Open … Settings
 *  button on a grant screen and the state has not moved since. */
export function waitingOnGrant(state: ComputerUseSetupState | undefined, awaitingSettings: boolean): boolean {
  if (state === 'waiting_for_system_settings') return true
  return awaitingSettings && state !== undefined && GRANT_STATES.has(state)
}

function waitBound(state: ComputerUseSetupState): number {
  return state === 'installing' ? SETUP_INSTALL_POLL_MAX_MS : SETUP_POLL_MAX_MS
}

/** Next read of the document, or `false` to stop. Only the states the backend
 *  moves on its own are re-read; a grant wait is answered by
 *  {@link grantRecheckInterval}, because only a recheck asks the driver for the
 *  current grants (a read returns the last recorded ones). `since` is when the
 *  current wait began (the last user action or state change), so a Check again
 *  starts a fresh window. */
export function setupPollInterval(
  state: ComputerUseSetupState | undefined,
  awaitingSettings: boolean,
  since: number,
  now: number,
): number | false {
  if (state === undefined || !MOVING_STATES.has(state) || waitingOnGrant(state, awaitingSettings)) return false
  if (now - since >= waitBound(state)) return false
  return SETUP_POLL_MS
}

/** Next non-prompting recheck while the user is in System Settings, or `false`
 *  to stop. Bounded like every other wait. */
export function grantRecheckInterval(
  state: ComputerUseSetupState | undefined,
  awaitingSettings: boolean,
  since: number,
  now: number,
): number | false {
  if (state === undefined || !waitingOnGrant(state, awaitingSettings)) return false
  if (now - since >= SETUP_POLL_MAX_MS) return false
  return SETUP_POLL_MS
}

/** True once a wait has outlasted its bound, whether a moving state or a grant
 *  wait: the view swaps its spinner for "taking longer than expected" and a
 *  Check again button, the same moment the polling stops. */
export function waitExpired(
  state: ComputerUseSetupState | undefined,
  since: number,
  now: number,
  awaitingSettings = false,
): boolean {
  if (state === undefined) return false
  if (!MOVING_STATES.has(state) && !waitingOnGrant(state, awaitingSettings)) return false
  return now - since >= waitBound(state)
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

/** The i18n key for the reason the system (endpoint security software) stopped
 *  the upstream driver at launch, or null. On the native driver the text says
 *  Computer Use keeps working on it; elsewhere it replaces the English
 *  `user_action`. Pass `{ how: doc.blocked }` when translating. */
export function blockedKey(doc: ComputerUseSetupDoc): string | null {
  if (!doc.blocked) return null
  if (doc.driver.kind === 'native') return 'pages.settings.computerUseSetup.upstream_blocked_native'
  return doc.platform === 'linux'
    ? 'pages.settings.computerUseSetup.driver_blocked_no_native'
    : 'pages.settings.computerUseSetup.driver_blocked'
}

/** The text of the document's current problem, for `ErrorNotice`, or null when
 *  nothing is wrong. A launch the system blocked uses the localized reason
 *  (`blockedMessage`); otherwise the problem's own message, followed by the next
 *  step (`user_action`) when that adds something. */
export function problemText(doc: ComputerUseSetupDoc, blockedMessage: string | null): string | null {
  const problem = doc.problem
  if (!problem) return null
  if (blockedMessage && (problem.code === 'install_blocked' || problem.code === 'launch_blocked')) return blockedMessage
  const next = doc.user_action && doc.user_action !== problem.message ? doc.user_action : ''
  // A sentence break between the reason and the next step, even when the reason
  // (a driver or OS message) ends without one.
  const reason = next && problem.message && !/[.!?。！？]$/.test(problem.message) ? `${problem.message}.` : problem.message
  return [reason, next].filter(Boolean).join(' ') || null
}
