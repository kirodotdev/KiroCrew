/**
 * Presentation helpers for actions that require a live gateway connection.
 *
 * These cover the *visual + accessibility* affordance only — the offline
 * tooltip, the aria-disabled state, and the offline aria-label. The behavioral
 * guards that actually prevent a server-dependent action while offline (the
 * `if (!connected) return` bails in ChatPage.send(), the URL/timer/auto-create
 * effects, and the sidebar row handlers) are deliberately NOT centralized here:
 * they are layered defense-in-depth at distinct entry points and sinks, not
 * duplication. Collapsing them would reopen the offline draft-loss path.
 */

import { i18nT } from '../i18n/t'

export interface OfflineProps {
  'aria-disabled': boolean
  title?: string
  'aria-label'?: string
}

/**
 * Returns the offline affordance for an interactive element.
 *
 * @param online current gateway connection flag
 * @param verb   action phrase shown in the tooltip, e.g. "send", "optimize",
 *               "switch sessions", "resume sessions"
 * @param label  optional accessible name of the control; when supplied and
 *               offline, the aria-label becomes "{label} disabled — gateway
 *               offline" so screen-reader users hear why the control is inert.
 *
 * Spread the result AFTER the element's online title/aria-label so the offline
 * variants override only while offline:
 *
 *   <button
 *     aria-label="Send"
 *     {...offlineProps(connected, 'send', 'Send')}
 *     disabled={!value.trim() || !connected}
 *   />
 *
 * When online the result is just `{ 'aria-disabled': false }`, leaving the
 * caller's own title/aria-label intact. The `disabled` attribute is always
 * composed by the caller — it combines with content conditions such as
 * !value.trim() — and is never owned by this helper.
 */
export function offlineProps(online: boolean, verb: string, label?: string): OfflineProps {
  if (online) return { 'aria-disabled': false }
  return {
    'aria-disabled': true,
    title: i18nT('utils.offline.gateway_offline_reconnect', { action: verb }),
    ...(label ? { 'aria-label': i18nT('utils.offline.disabled_gateway_offline', { label }) } : {}),
  }
}
