import { useConnected } from '../hooks/useConnected'

import { i18nT } from '../i18n/t'

/**
 * A natively disabled row carries `pointer-events-none`, so it fires no event and
 * opens no tooltip — an explanation gated on activation cannot reach it.
 *
 * `reason` defaults to the session-menu wording. A host outside that menu must
 * pass its own: this component also mounts in the artifacts library, where
 * "session changes" names the wrong thing.
 *
 * `spacing` defaults to a menu row's own padding rhythm. A popover host sets its
 * own, because there the row follows an input rather than a run of menu items.
 */
export default function OfflineMenuReason({ testId = 'menu-offline-reason', reason, spacing = 'px-2 py-1' }: {
  readonly testId?: string
  readonly reason?: string
  readonly spacing?: string
}) {
  const connected = useConnected()
  if (connected) return null
  return (
    <div role="status" data-testid={testId} className={`${spacing} text-[11px] text-muted`}>
      {reason ?? i18nT('components.sessionActionsMenu.offline_reason')}
    </div>
  )
}
