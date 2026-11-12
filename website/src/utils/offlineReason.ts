import type { OfflineCause } from '../store/dashboardSlice'
import { i18nT } from '../i18n/t'

/** True when this page is a remote Crew opened in the desktop app's own relay
 *  window. That window says when it stops waiting for the Crew, so while it
 *  still waits the page says only that it is reconnecting. The marker is the
 *  whole space-separated token `electron/relay-crew-window.js` appends to the
 *  user agent: a wire token, not copy, and not a credential. */
export function isRelayCrewWindow(ua: string = typeof navigator === 'undefined' ? '' : navigator.userAgent): boolean {
  return ua.split(' ').includes('kiroCrewRelayWindow')
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
