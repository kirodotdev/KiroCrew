/**
 * Presentation helpers for actions that require a live gateway connection.
 *
 * These cover the *visual + accessibility* affordance only — the offline
 * tooltip, the aria-disabled state, and the offline aria-label. The behavioral
 * guards that actually prevent a server-dependent action while offline (the
 * `if (!connected) return` bails in send() (pages/chat/page/composerSend.ts), the URL/timer/auto-create
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
 * @param verb   LOCALIZED action phrase for the tooltip, always a catalogue
 *               lookup — `i18nT('utils.offline.rename_folders')`. A raw English
 *               verb here lands inside a translated sentence for 11 locales.
 * @param label  optional accessible name of the control; when supplied and
 *               offline, the aria-label becomes "{label} disabled — gateway
 *               offline" so screen-reader users hear why the control is inert.
 *
 * Spread the result AFTER the element's online title/aria-label so the offline
 * variants override only while offline:
 *
 *   <button
 *     aria-label={i18nT('pages.chatSidebar.rename')}
 *     {...offlineProps(connected, i18nT('utils.offline.rename_folders'), i18nT('pages.chatSidebar.rename'))}
 *     disabled={!name.trim() || !connected}
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

/**
 * The offline gate for one menu row whose action writes to the gateway. Put it
 * on a `DropdownMenuItem` / `ContextMenuItem`:
 *
 *   <Item {...offlineMenuRow(connected, i18nT('utils.offline.delete_folders'), label, () => del())}>
 *
 * Online it adds only `onSelect`, which runs the action. Offline it dims the row
 * (`offline`), names why in the tooltip and accessible name, and refuses on
 * `onSelect` with `preventDefault`. Radix closes a menu on `onSelect`, so the
 * refusal keeps the menu open, and with it the menu's `OfflineMenuReason` row;
 * an `onClick` that returned early would let the menu close and read as though
 * the action ran.
 *
 * The row is not Radix-`disabled`: that adds `pointer-events-none` and drops it
 * from the focus order, so its tooltip could never open. `offlineProps` is
 * applied only while offline, so an online row keeps its own `disabled` (for
 * example while a create is in flight) without a contradicting
 * `aria-disabled="false"`.
 */
export function offlineMenuRow(online: boolean, verb: string, label: string, run: () => void) {
  return {
    offline: !online,
    ...(online ? {} : offlineProps(false, verb, label)),
    onSelect: (e: Event) => {
      if (!online) { e.preventDefault(); return }
      run()
    },
  }
}
