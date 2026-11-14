import type { InstalledApp } from '../../components/appstore/types'
import type { RestartExhausted } from '../../utils/backendStatus'

/**
 * Why an installed app's rail row asks for attention.
 *
 * Only states an app WITH a row can be in: `appNavTarget` gives a disabled or
 * unavailable app no row, so neither can be marked here.
 */
export type AppRailAttention = 'migrate' | 'backend'

/** What the rail shows and offers for one installed app beyond its route. */
export interface AppRailStatus {
  /** The newer version the registry offers. Absent when it offers none. */
  newVersion?: string
  attention?: AppRailAttention
  /** The backend's restart-exhausted block, kept for the menu's notice. Set with `attention: 'backend'`. */
  restartExhausted?: RestartExhausted
  /** The installed row's lifecycle. A `locked` app cannot be uninstalled. */
  lifecycle?: string
}

/**
 * The `['apps']` row fields the derivation reads. All are server-emitted: that
 * cache can hold raw, unnormalized rows (see `useAppsData`).
 */
export type AppRailStatusRow = Pick<InstalledApp, 'name' | 'lifecycle' | 'orphaned' | 'backend_status'>

/**
 * Per-app rail status, keyed by app name.
 *
 * The update rule is the Library tile's own (`updateMap.has(name)`, whatever
 * the lifecycle), so a row carries the arrow exactly when its tile shows an
 * update. The map is prototype-less because an app name is attacker-chosen and
 * `constructor` is a valid one; see `appNotificationBadges`.
 */
export function appRailStatuses(
  updateMap: ReadonlyMap<string, string>,
  installed: readonly AppRailStatusRow[] | undefined,
): Record<string, AppRailStatus> {
  const out: Record<string, AppRailStatus> = Object.create(null)
  for (const app of installed ?? []) {
    const status: AppRailStatus = { lifecycle: app.lifecycle }
    const newVersion = updateMap.get(app.name)
    if (newVersion !== undefined) status.newVersion = newVersion
    // One mark per row, migration first: an orphaned app's row already opens
    // the migration page, and nothing else about the app is reliable until
    // that runs.
    if (app.orphaned) status.attention = 'migrate'
    else if (app.backend_status?.restart_exhausted) {
      status.attention = 'backend'
      status.restartExhausted = app.backend_status.restart_exhausted
    }
    out[app.name] = status
  }
  return out
}

/** Whether *status* puts a mark on its row (the unread count aside). */
export function hasRailMark(status: AppRailStatus | undefined): boolean {
  return status?.newVersion !== undefined || status?.attention !== undefined
}

/**
 * *status* for a rail whose rows have no actions menu: the phone chat page's
 * icon rail. A backend that keeps failing to start is an error, and an error
 * needs a surface that can be read and handed to the agent. The menu's
 * `ErrorNotice` is that surface, and a mark alone would leave the failure in
 * a tooltip that touch cannot open. So such a rail drops that mark, and the
 * nav drawer's rows, which have the menu, keep it. An orphaned app keeps its
 * mark: a tap on its row opens the migration page, which says what to do.
 */
export function menulessRailStatus(status: AppRailStatus | undefined): AppRailStatus | undefined {
  if (status?.attention !== 'backend') return status
  const { attention: _dropped, restartExhausted: _block, ...rest } = status
  return rest
}

export type AppRailMenuItem =
  | 'update' | 'details' | 'library' | 'mark-read'
  | 'disable' | 'unpin' | 'uninstall'

/**
 * The menu items an installed app's row offers, in menu order.
 *
 * `unread` is the number of the app's own bell notes that Mark as read would
 * acknowledge, not the row's shown count: an app that pushes its own badge
 * through `useNavBadge` shows that number, which no acknowledgement clears.
 * Open is not offered: a click on the row already opens the app. Enable is
 * never offered because a disabled app has no row. Uninstall
 * follows the Library tile's rule, which refuses only a `locked` lifecycle.
 */
export function appRailMenuItems(status: AppRailStatus | undefined, unread: number): AppRailMenuItem[] {
  const items: AppRailMenuItem[] = []
  if (status?.newVersion !== undefined) items.push('update')
  items.push('details', 'library')
  if (unread > 0) items.push('mark-read')
  items.push('disable', 'unpin')
  if (status?.lifecycle !== 'locked') items.push('uninstall')
  return items
}
