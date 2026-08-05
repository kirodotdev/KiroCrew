/**
 * AppListRow — dense marketplace row in Discover's "All apps" list.
 *
 * The row shows a 16:9 hero capsule
 * (developer-supplied ``heroImage``, theme-aware, gradient + icon fallback),
 * then name with verified mark, publisher / category / source provenance, and
 * a one-line description. The right side carries state — Install (routes to
 * the detail page and starts the install there), Update, Enable (for hidden
 * built-ins), or an Installed check. The row opens the detail page, honoring
 * Cmd/Ctrl-click for a new tab.
 */
import { ArrowUp, BadgeCheck, Check, Monitor, Power } from 'lucide-react'
import { Btn } from '../ui'
import Clickable from '../Clickable'
import HeroCapsule from './HeroCapsule'
import { categoryFor } from './categories'
import { sourceLabel, isVerified, type RegistryApp } from './types'
import { appDisplayName, appDescription } from './appManifest'
import { needsDesktopApp } from '../../lib/electron'

import { i18nT } from '../../i18n/t'
export default function AppListRow({ app, busy, onOpen, onGet, onUpdate, onEnable }: {
  app: RegistryApp
  busy?: boolean
  onOpen: (e?: React.MouseEvent | React.KeyboardEvent) => void
  onGet: () => void
  onUpdate: () => void
  onEnable: () => void
}) {
  const isBuiltin = app.origin === 'builtin' && !!app.installed

  return (
    <Clickable
      aria-label={i18nT('components.appstore.appListRow.view_details_for', { name: appDisplayName(app) })}
      className="flex items-center gap-3.5 px-3.5 py-3 border border-border rounded-xl bg-card mb-2 cursor-pointer hover:border-border-strong transition-colors focus-ring"
      onClick={onOpen}
    >
      {/* Hero capsule — 16:9 crop of the app's own art, gradient when absent */}
      <HeroCapsule name={app.name} art={app} icon={app.icon} iconUrl={app.iconUrl} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 text-[14px] font-semibold text-text-strong">
          <span className="truncate">{appDisplayName(app)}</span>
          {isVerified(app) && (
            <BadgeCheck size={14} className="text-accent shrink-0" aria-label={i18nT('components.appstore.appListRow.verified_publisher')}>
              <title>{i18nT('components.appstore.appListRow.verified_publisher_first_party')}</title>
            </BadgeCheck>
          )}
        </div>
        <div className="text-[12px] text-muted truncate">{app.author} · {categoryFor(app.tags)} · {sourceLabel(app)}</div>
        <div className="text-[12.5px] text-muted truncate" title={appDescription(app)}>{appDescription(app)}</div>
      </div>
      {/* Actions: stop propagation so nested controls keep their own
          click/keyboard activation instead of triggering the row. */}
      <div
        className="flex flex-col items-end gap-1.5 shrink-0"
        onClick={e => e.stopPropagation()}
        onKeyDown={e => e.stopPropagation()}
        role="presentation"
      >
        {isBuiltin ? (
          app.enabled ? (
            <span className="inline-flex items-center gap-1 text-[12px]" style={{ color: 'var(--ok)' }}>
              <Power size={12} /> {i18nT('components.appstore.installedAppCard.enabled')}
            </span>
          ) : (
            /* A desktop-only builtin is still ENABLE-able from a browser: enabling
               is a server-side state change (backend, agents and crons run in the
               gateway); only its UI needs the desktop shell. Offer the action and
               say what it needs — a static tag would strand a remote user with no
               way to turn it on. The Monitor badge's hint lives in a hover title,
               so carry the same hint as the button's accessible name for keyboard
               / screen-reader users (same reason AppDetailPage shows it in text). */
            <span className="inline-flex items-center gap-2">
              <Btn
                disabled={busy}
                onClick={onEnable}
                aria-label={needsDesktopApp(app)
                  ? `${i18nT('components.appstore.appListRow.enable')}. ${i18nT('components.appstore.appListRow.desktop_app_hint')}`
                  : undefined}
              ><Power size={14} /> {i18nT('components.appstore.appListRow.enable')}</Btn>
              {needsDesktopApp(app) && (
                <span className="inline-flex items-center gap-1 text-[12px] text-muted" title={i18nT('components.appstore.appListRow.desktop_app_hint')}>
                  <Monitor size={12} /> {i18nT('components.appstore.appListRow.desktop_app')}
                </span>
              )}
            </span>
          )
        ) : app.installed && app.updateAvailable ? (
          <Btn disabled={busy} className="border-[var(--info)] text-[var(--info)] hover:text-[var(--info)] hover:border-[var(--info)]" onClick={onUpdate}>
            <ArrowUp size={14} /> {i18nT('components.appstore.appListRow.update')}
          </Btn>
        ) : app.installed ? (
          <span className="inline-flex items-center gap-1 text-[12px] text-muted"><Check size={12} /> {i18nT('components.appstore.appListRow.installed')}</span>
        ) : (
          <Btn primary className="rounded-full px-3.5 text-[12px] font-semibold" disabled={busy} onClick={onGet}>{i18nT('components.appstore.appListRow.install')}</Btn>
        )}
      </div>
    </Clickable>
  )
}
