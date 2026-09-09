import { useConnected } from '../hooks/useConnected'

import { i18nT } from '../i18n/t'

/**
 * A natively disabled row carries `pointer-events-none`, so it fires no event and
 * opens no tooltip — an explanation gated on activation cannot reach it.
 */
export default function OfflineMenuReason({ testId = 'menu-offline-reason' }: { readonly testId?: string }) {
  const connected = useConnected()
  if (connected) return null
  return (
    <div role="status" data-testid={testId} className="px-2 py-1 text-[11px] text-muted">
      {i18nT('components.sessionActionsMenu.offline_reason')}
    </div>
  )
}
