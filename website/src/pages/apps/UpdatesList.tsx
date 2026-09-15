/**
 * UpdatesList — the Discover Updates sub-tab's content (PR2 App Store split).
 *
 * One row per updatable app: the shared icon tile, the display name, the
 * version diff, and an in-place Update button; the header carries Update All
 * with the sequential batch's progress. The rows are `useAppsData.updatables`
 * — the SAME list the sidebar badge and the sub-tab count are sized from — so
 * a row leaves this list by the data refresh that confirms its update
 * (`useAppUpdates` announces each success, the registry refetch clears the
 * app's `updateAvailable`), never by local bookkeeping that could disagree
 * with the badge.
 *
 * Presentational on purpose: update BEHAVIOR (recorded-source routing, the
 * per-app pending state, the Update All loop) is `useAppUpdates`' contract,
 * instantiated by the page that owns the notice surfaces. This component only
 * renders the state it is handed, so it cannot fork that behavior from the
 * Library's.
 *
 * No last-updated timestamps and no release notes here — the backend has no
 * `updatedAt` and the catalog carries no release-notes field, so the version
 * diff is everything an honest row can say (deferred, see the PR2 spec).
 */
import { ArrowUp } from 'lucide-react'
import { Btn } from '../../components/ui'
import AppIconTile from '../../components/appstore/AppIconTile'
import { appDisplayName } from '../../components/appstore/appManifest'
import { installedIcon } from '../../components/appstore/useHeroArt'
import { i18nT } from '../../i18n/t'
import type { LibraryApp } from './useAppsData'

export default function UpdatesList({
  rows, updatingAll, updatePending, onUpdate, onUpdateAll,
}: {
  /** The updatable Library rows (`useAppsData.updatables`) — non-empty; the page owns the empty state. */
  rows: LibraryApp[]
  /** Sequential Update All progress from `useAppUpdates`, or null when idle. */
  updatingAll: { done: number; total: number } | null
  /** Name of the app whose single in-place update is in flight, or null. */
  updatePending: string | null
  /** Per-row update dispatch (`useAppUpdates.runUpdate`). */
  onUpdate: (name: string) => void
  /** The shared sequential batch (`useAppUpdates.updateAll`). */
  onUpdateAll: () => void
}) {
  return (
    <div>
      {/* Header: the same count line and Update All control the Library banner
          carries (same i18n keys), so the two surfaces describe one worklist
          in one voice. The progress label replaces the button text while the
          batch runs — the disabled button IS the progress surface. */}
      <div className="flex items-center justify-between gap-3 mb-3">
        <h3 className="text-[17px] font-semibold text-text-strong">
          {i18nT('pages.appsPage.update', { count: rows.length })} {i18nT('pages.appsPage.available')}
        </h3>
        <Btn
          className="!bg-[var(--info)] !text-white hover:!opacity-80"
          onClick={onUpdateAll}
          disabled={!!updatingAll}
        >
          {updatingAll
            ? i18nT('pages.appsPage.updating_progress', { done: updatingAll.done, total: updatingAll.total })
            : i18nT('pages.appsPage.update_all')}
        </Btn>
      </div>
      <div className="space-y-2">
        {rows.map(app => {
          // Icon resolution — the shared installed-app icon chain verbatim, so an app
          // wears the same mark on this worklist as on its Library card: a
          // page icon glyph, else the app's own installed art through the
          // resolver that refuses external hosts, else the name-hashed
          // gradient inside AppIconTile.
          const m = app.manifest
          // `iconPath` FIRST, matching every other surface: the order is only
          // observable for a manifest declaring both, and there it decides which
          // file the app wears. See LaunchpadTile for the full rationale.
          const iconUrl = installedIcon(m?.iconPath, m?.iconUrl, app.name)
          const iconUrlDark = installedIcon(m?.iconPathDark, m?.iconUrlDark, app.name)
          // Disabled while ANY update runs — the batch, this row's own call,
          // or another row's: `updatePending` is a single slot, so letting a
          // sibling stay clickable would invite a dispatch the hook refuses
          // silently. Mirrors how the Library freezes its cards' actions.
          const busy = !!updatingAll || !!updatePending
          return (
            <div
              key={app.name}
              className="border border-border rounded-lg p-3 flex items-center gap-3"
            >
              <AppIconTile
                name={app.name}
                icon={m?.ui?.pages?.[0]?.icon || ''}
                iconUrl={iconUrl}
                iconUrlDark={iconUrlDark}
              />
              <div className="flex-1 min-w-0">
                <div className="font-medium text-text truncate">{appDisplayName(app)}</div>
                <div className="text-[12px] text-muted">
                  {i18nT('pages.discoverPage.version_diff', {
                    from: app.version,
                    to: app._newVersion || '',
                  })}
                </div>
              </div>
              <Btn
                className="!bg-[var(--info)] !text-white hover:!opacity-80 shrink-0"
                onClick={() => onUpdate(app.name)}
                disabled={busy}
                title={i18nT('components.appstore.installedAppCard.update_to', { version: app._newVersion || app.version })}
              >
                {/* In-flight form at the point of action: a slow registry
                    re-clone otherwise leaves a silently disabled button. */}
                {updatePending === app.name
                  ? <>{i18nT('pages.discoverPage.updating')}</>
                  : <><ArrowUp size={14} /> {i18nT('components.appstore.installedAppCard.update')}</>}
              </Btn>
            </div>
          )
        })}
      </div>
    </div>
  )
}
