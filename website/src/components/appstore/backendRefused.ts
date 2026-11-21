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
 * The notice for an enable or disable *result*: a refused backend, then the
 * `warnings` the gateway attached (a failed hook, config sync or service
 * notify). The action still landed, so the line says so first and the
 * gateway's own detail follows.
 */
export function appActionNotice(name: string, result: unknown, action: 'enable' | 'disable'): string {
  const refused = backendRefusedNotice(name, result)
  const raw = result && typeof result === 'object' ? (result as { warnings?: unknown }).warnings : undefined
  const warnings = Array.isArray(raw) ? raw.filter((w): w is string => typeof w === 'string' && w.trim() !== '') : []
  const key = action === 'enable' ? 'pages.appsPage.action_warnings_enabled' : 'pages.appsPage.action_warnings_disabled'
  const warned = warnings.length ? i18nT(key, { name, warnings: warnings.join('; ') }) : ''
  return joinSentences([refused, warned])
}

/** The Bot slugs whose chat a stop could not end, from a response's `bot_chat_stop_failed`. */
export function botChatStopFailedSlugs(result: unknown): string[] {
  const raw = result && typeof result === 'object' ? (result as { bot_chat_stop_failed?: unknown }).bot_chat_stop_failed : undefined
  return Array.isArray(raw) ? raw.filter((s): s is string => typeof s === 'string') : []
}

/**
 * The Bots a 409 body names when its `code` is the Bot-chat stop result
 * (`bot_chat_stop_incomplete`), or null for any other 409, which is a refusal
 * of the action and not news about the Bots. The uninstall and disable routes
 * name them in `bot_chat_stop_failed`, trust removal in `bots`.
 */
export function botChatStopIncompleteSlugs(body: unknown): string[] | null {
  if (!body || typeof body !== 'object') return null
  const parsed = body as { code?: unknown; bots?: unknown }
  if (parsed.code !== 'bot_chat_stop_incomplete') return null
  const named = botChatStopFailedSlugs(body)
  if (named.length) return named
  return Array.isArray(parsed.bots) ? parsed.bots.filter((s): s is string => typeof s === 'string') : []
}

/**
 * The notice for an uninstall, disable or trust removal that landed while some
 * Bot chats running the app's tools could not be stopped, or '' when none.
 * The three share one wording and one pair of actions (stop again, restart).
 * Pass an optional `resolveSlug` to show Bot names instead of slugs.
 */
export function botChatStopFailedNotice(
  action: 'uninstall' | 'disable' | 'revoke',
  name: string,
  slugs: string[],
  resolveSlug?: (slug: string) => string,
): string {
  if (!slugs.length) return ''
  const display = resolveSlug ? slugs.map(resolveSlug) : slugs
  const vars = { count: slugs.length, name, slugs: display.join(', ') }
  // i18next picks the plural form for each language from `count`
  // (`_one`/`_other`, `_few`/`_many` in ru and bn, `_other` alone in ja/ko/zh-CN).
  if (action === 'uninstall') return i18nT('pages.appsPage.uninstall_bot_chat_stop_failed', vars)
  if (action === 'disable') return i18nT('pages.appsPage.disable_bot_chat_stop_failed', vars)
  return i18nT('pages.appsPage.bot_chat_stop_incomplete_revoked', vars)
}

/**
 * The warning to show when uninstall succeeds but some Bot chats using the app
 * could not be stopped.  Empty when no slugs are listed.
 * Pass an optional `resolveSlug` to convert raw slugs to display names.
 */
export function uninstallBotChatStopFailedNotice(
  name: string,
  result: unknown,
  resolveSlug?: (slug: string) => string,
): string {
  return botChatStopFailedNotice('uninstall', name, botChatStopFailedSlugs(result), resolveSlug)
}
