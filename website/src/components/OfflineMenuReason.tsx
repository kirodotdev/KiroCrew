import { useConnected } from '../hooks/useConnected'

import { i18nT } from '../i18n/t'

/**
 * A natively disabled row carries `pointer-events-none`, so it fires no event and
 * opens no tooltip — an explanation gated on activation cannot reach it.
 *
 * The string lives under `utils.offline`, not under any one menu's namespace: this
 * row renders in the session-actions, folder and create menus, so a key named for
 * the first of those is a trap for the next locale edit, which would read the
 * namespace and miss two callers.
 */
export default function OfflineMenuReason({ testId = 'menu-offline-reason' }: { readonly testId?: string }) {
  const connected = useConnected()
  if (connected) return null
  return (
    <div role="status" data-testid={testId} className="px-2 py-1 text-[11px] text-muted">
      {i18nT('utils.offline.menu_reason')}
    </div>
  )
}
