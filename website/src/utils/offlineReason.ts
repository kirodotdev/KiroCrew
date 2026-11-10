import type { OfflineCause } from '../store/dashboardSlice'
import { i18nT } from '../i18n/t'

/** The offline wording: the Kiro relay's reason when it gave one (a Crew opened
 *  through Kiro Mobile or the desktop app), otherwise the generic reconnecting. */
export function offlineReason(cause: OfflineCause | null): string {
  if (cause === 'session_ended') return i18nT('app.relay_session_ended')
  if (cause === 'refused') return i18nT('app.relay_access_refused')
  return i18nT('app.gateway_offline_reconnecting')
}
