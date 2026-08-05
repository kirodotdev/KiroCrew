/**
 * FeatureCard — secondary feature slots under the spotlight. Compact
 * art-left / text-right card using the
 * app's own theme-appropriate hero art, with a gradient + icon fallback.
 */
import { Check, Package, Power, Monitor } from 'lucide-react'
import { Btn } from '../ui'
import Clickable from '../Clickable'
import AppIcon from '../AppIcon'
import { gradientFor } from './gradient'
import { categoryFor } from './categories'
import { useHeroArt } from './useHeroArt'
import type { RegistryApp } from './types'
import { appDisplayName, appDescription } from './appManifest'
import { needsDesktopApp } from '../../lib/electron'

import { i18nT } from '../../i18n/t'
export default function FeatureCard({ app, onOpen, onGet, onEnable, busy }: {
  app: RegistryApp
  onOpen: (e?: React.MouseEvent | React.KeyboardEvent) => void
  onGet: () => void
  onEnable: () => void
  busy?: boolean
}) {
  const hero = useHeroArt(app)
  const hiddenBuiltin = app.origin === 'builtin' && app.installed && !app.enabled

  return (
    <Clickable
      aria-label={i18nT('components.appstore.featureCard.view_details_for', { name: appDisplayName(app) })}
      className="grid grid-cols-[140px_1fr] sm:grid-cols-[235px_1fr] border border-border rounded-2xl overflow-hidden bg-card cursor-pointer hover:border-border-strong transition-colors group focus-ring"
      onClick={onOpen}
    >
      {/* Art column is sized ~16:9 against the 132px row height so a
          developer's hero lands with minimal cropping (aspect-ratio respect). */}
      <div
        className="relative min-h-[132px] grid place-items-center overflow-hidden"
        style={hero.src ? { background: 'var(--card)' } : { background: gradientFor(app.name) }}
      >
        {hero.src ? (
          <img
            src={hero.src}
            alt=""
            className="absolute inset-0 w-full h-full object-cover group-hover:scale-[1.02] transition-transform duration-300"
            onError={hero.onError}
          />
        ) : (
          <div className="w-[52px] h-[52px] rounded-[14px] bg-white/15 border border-white/25 backdrop-blur-sm grid place-items-center text-white">
            {(app.iconUrl || app.icon) ? <AppIcon icon={app.icon} iconUrl={app.iconUrl} size={32} /> : <Package size={26} />}
          </div>
        )}
      </div>
      <div className="px-[18px] py-4 flex flex-col gap-1 justify-center min-w-0">
        <span className="text-[15px] font-bold text-text-strong truncate">{appDisplayName(app)}</span>
        <p className="text-[12.5px] text-muted line-clamp-2" title={appDescription(app)}>{appDescription(app)}</p>
        <div
          className="flex items-center justify-between mt-1.5"
          onClick={e => e.stopPropagation()}
          onKeyDown={e => e.stopPropagation()}
          role="presentation"
        >
          <span className="text-[11px] text-muted">{categoryFor(app.tags)}</span>
          {/* Same as AppListRow: enabling is server-side, so a browser user can
              do it; only the app's UI needs the desktop shell. */}
          {hiddenBuiltin ? (
            <span className="inline-flex items-center gap-2">
              <Btn
                primary
                className="rounded-full px-3.5 text-[12px] font-semibold"
                disabled={busy}
                onClick={onEnable}
                /* The desktop badge beside this is visual only; carry its hint as
                   the button's accessible name so keyboard/screen-reader users
                   learn the app's window needs the desktop build (same as
                   AppListRow and AppDetailPage). */
                aria-label={needsDesktopApp(app)
                  ? `${i18nT('components.appstore.featureCard.enable')}. ${i18nT('components.appstore.featureCard.desktop_app_hint')}`
                  : undefined}
              >
                <Power size={13} /> {i18nT('components.appstore.featureCard.enable')}
              </Btn>
              {needsDesktopApp(app) && (
                <span className="inline-flex items-center gap-1 text-[12px] text-muted" title={i18nT('components.appstore.featureCard.desktop_app_hint')}>
                  <Monitor size={12} /> {i18nT('components.appstore.featureCard.desktop_app')}
                </span>
              )}
            </span>
          ) : app.installed ? (
            <span className="inline-flex items-center gap-1 text-[12px] text-muted"><Check size={12} /> {i18nT('components.appstore.featureCard.installed')}</span>
          ) : (
            <Btn primary className="rounded-full px-3.5 text-[12px] font-semibold" disabled={busy} onClick={onGet}>{i18nT('components.appstore.featureCard.get')}</Btn>
          )}
        </div>
      </div>
    </Clickable>
  )
}
