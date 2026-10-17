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

/**
 * The same standing reason for a surface that is not a menu: a table whose row
 * controls are dimmed, or a field whose typed value is being held. The caller
 * decides when it shows (it owns the connection read), and may narrow the text.
 *
 * A block-level `span`, so it nests validly in an inline host (a table cell's
 * `span`) as well as a block one.
 */
export function OfflineReasonRow({ testId, className = '', message, id }: {
  readonly testId: string
  /** Placement for the host surface; the table pins it, the field insets it. */
  readonly className?: string
  /** Replaces the surface-wide reason, e.g. for a field whose draft is held. */
  readonly message?: string
  /** Lets a host point `aria-describedby` here, for a control that is refused
   *  but still interactive and so keeps its own accessible name. */
  readonly id?: string
}) {
  return (
    <span role="status" id={id} data-testid={testId} className={`block text-[11px] text-muted ${className}`}>
      {message ?? i18nT('utils.offline.menu_reason')}
    </span>
  )
}
