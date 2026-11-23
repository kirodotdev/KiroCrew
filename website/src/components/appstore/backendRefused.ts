/**
 * The `backend_refused` field of an enable, install or update response.
 *
 * `start_app_backend` returns the same "nothing started" for an app with no
 * backend and for a backend the host refused (a shell launcher on native
 * Windows), so the gateway names the refusal in this field. The action still
 * succeeded, and the app is enabled, but its backend is not running, so every
 * surface that reports the result shows this as a warning instead of a success.
 */

import { joinSentences } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** The refusal reason the response carries, or '' when there is none. */
export function backendRefusedReason(result: unknown): string {
  if (!result || typeof result !== 'object') return ''
  const reason = (result as { backend_refused?: unknown }).backend_refused
  return typeof reason === 'string' ? reason.trim() : ''
}

/** The warning to show for *result*, or '' when its backend was not refused. */
export function backendRefusedNotice(name: string, result: unknown): string {
  const reason = backendRefusedReason(result)
  return reason ? i18nT('pages.appsPage.backend_refused_notice', { name, reason }) : ''
}

/**
 * The notice for an enable, disable or uninstall *result*: a refused backend
 * when the result names one, then the `warnings` the gateway attached (a step
 * that failed without undoing the action: a hook, the config sync, a service
 * notify or a backend stop). The action still landed, so the line says so
 * first and the gateway's own detail follows.
 */
export function appActionNotice(name: string, result: unknown, action: 'enable' | 'disable' | 'uninstall'): string {
  const refused = backendRefusedNotice(name, result)
  const raw = result && typeof result === 'object' ? (result as { warnings?: unknown }).warnings : undefined
  const warnings = Array.isArray(raw) ? raw.filter((w): w is string => typeof w === 'string' && w.trim() !== '') : []
  const key = ({
    enable: 'pages.appsPage.action_warnings_enabled',
    disable: 'pages.appsPage.action_warnings_disabled',
    uninstall: 'pages.appsPage.action_warnings_uninstalled',
  } as const)[action]
  const warned = warnings.length ? i18nT(key, { name, warnings: warnings.join('; ') }) : ''
  return joinSentences([refused, warned])
}
