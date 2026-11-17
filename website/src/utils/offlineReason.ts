import type { OfflineCause } from '../store/dashboardSlice'
import { i18nT } from '../i18n/t'
import { isLoopbackHost } from '../lib/loopbackHost'

/** True when this page is a remote Crew opened in the desktop app's own relay
 *  window. That window says when it stops waiting for the Crew, so while it
 *  still waits the page says only that it is reconnecting. The marker is the
 *  whole space-separated token `electron/relay-crew-window.js` appends to the
 *  user agent: a wire token, not copy, and not a credential. */
export function isRelayCrewWindow(ua: string = typeof navigator === 'undefined' ? '' : navigator.userAgent): boolean {
  return ua.split(' ').includes('kiroCrewRelayWindow')
}

/** True when this page is a remote Crew shown inside the desktop app's main
 *  window, below the app's own top bar. The page then leaves out its own header,
 *  so the window does not show two bars. Layout only: the marker is the whole
 *  user-agent token `kiroCrewRelayPane`, which any page could claim, so it must
 *  never decide what the page trusts. */
export function isRelayCrewPane(ua: string = typeof navigator === 'undefined' ? '' : navigator.userAgent): boolean {
  return ua.split(' ').includes('kiroCrewRelayPane')
}

/** True when a relay Crew window's dropped connection is still being retried:
 *  the relay neither refused it nor ended the session. The desktop app decides
 *  when retrying stops (its "currently unavailable" notice then replaces this
 *  page), so until then the page shows a calm reconnecting view, not an error.
 *  The caller adds the transport state: offline, and past the first connect. */
export function isRelayRetrying(cause: OfflineCause | null, relayCrewWindow: boolean = isRelayCrewWindow()): boolean {
  return relayCrewWindow && cause !== 'refused' && cause !== 'session_ended'
}

/** Which calm view covers the content area while the connection is down, or
 *  null when none does. One gate for every panel: under the cover the panels'
 *  own fetch errors (all caused by the same lost connection) are hidden and
 *  inert, and while connected they show as they always did.
 *  - `relay`: a relay Crew window still retrying (`isRelayRetrying`).
 *  - `host`: the owner's own dashboard on this machine (a loopback origin) with
 *    the gateway gone. A relay reason, a relay Crew pane and any other origin
 *    (a tailnet, a Kiro Mobile WebView) keep their current surfaces.
 *  Never before the first socket has failed, and never while sign-in is the
 *  cause: the session-expired banner names the fix. */
export type OfflineCover = 'relay' | 'host' | null
export function offlineCover(s: {
  connected: boolean
  connecting: boolean
  authRequired: boolean
  cause: OfflineCause | null
  relayCrewWindow: boolean
  relayCrewPane: boolean
  hostname: string
}): OfflineCover {
  if (s.connected || s.connecting || s.authRequired) return null
  if (s.relayCrewWindow) return isRelayRetrying(s.cause, true) ? 'relay' : null
  if (s.relayCrewPane || s.cause || !isLoopbackHost(s.hostname)) return null
  return 'host'
}

/** The offline wording: the Kiro relay's reason when it gave one (a Crew opened
 *  through Kiro Mobile or the desktop app), otherwise the generic reconnecting.
 *  In a relay Crew window the generic case is the same word the desktop's Crew
 *  switcher shows for that Crew. */
export function offlineReason(cause: OfflineCause | null, relayCrewWindow: boolean = isRelayCrewWindow()): string {
  if (cause === 'session_ended') return i18nT('app.relay_session_ended')
  if (cause === 'refused') return i18nT('app.relay_access_refused')
  if (relayCrewWindow) return i18nT('app.relay_crew_reconnecting')
  return i18nT('app.gateway_offline_reconnecting')
}
