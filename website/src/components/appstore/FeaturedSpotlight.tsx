/**
 * FeaturedSpotlight — the editorial hero at the top of Discover.
 *
 * Layout: text panel (kicker / name / tagline /
 * provenance meta / CTA) beside an art panel. Art prefers the app's own
 * theme-appropriate hero image and degrades to a deterministic gradient with
 * the app icon on a glass tile.
 */
import { BadgeCheck, Check, Download, Package, Power } from 'lucide-react'
import { Btn } from '../ui'
import Clickable from '../Clickable'
import AppIcon from '../AppIcon'
import { gradientFor } from './gradient'
import { categoryFor } from './categories'
import { useHeroArt } from './useHeroArt'
import { sourceLabel, isVerified, type RegistryApp } from './types'
import { appDisplayName, appDescription } from './appManifest'

import { i18nT } from '../../i18n/t'
export default function FeaturedSpotlight({ app, onOpen, onGet, onEnable, busy }: {
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
      aria-label={i18nT('components.appstore.featuredSpotlight.view_details_for', { name: appDisplayName(app) })}
      className="grid grid-cols-1 md:grid-cols-[1.05fr_.95fr] border border-border rounded-[20px] overflow-hidden bg-card mb-3.5 cursor-pointer group hover:border-border-strong transition-colors focus-ring"
      onClick={onOpen}
    >
      <div className="px-9 py-8 flex flex-col justify-center gap-2.5 min-w-0">
        <span className="text-[11px] font-bold tracking-[.14em] text-accent">{i18nT('components.appstore.featuredSpotlight.featured')}</span>
        <h2 className="text-[32px] leading-[1.15] font-bold text-text-strong tracking-tight">{appDisplayName(app)}</h2>
        <p className="text-[15px] text-muted line-clamp-2" title={appDescription(app)}>{appDescription(app)}</p>
        <div className="flex items-center gap-2 text-[12.5px] text-muted">
          {isVerified(app) && (
            <BadgeCheck size={14} className="text-accent shrink-0" aria-label={i18nT('components.appstore.featuredSpotlight.verified_publisher')}>
              <title>{i18nT('components.appstore.featuredSpotlight.verified_publisher_first_party')}</title>
            </BadgeCheck>
          )}
          <span className="truncate">{app.author} · {categoryFor(app.tags)}</span>
        </div>
        <div
          className="flex items-center gap-3.5 mt-2"
          onClick={e => e.stopPropagation()}
          onKeyDown={e => e.stopPropagation()}
          role="presentation"
        >
          {hiddenBuiltin ? (
            <Btn primary className="rounded-full px-4 py-1.5 font-semibold" disabled={busy} onClick={onEnable}>
              <Power size={14} /> {i18nT('components.appstore.featuredSpotlight.enable')}
            </Btn>
          ) : app.installed ? (
            <span className="inline-flex items-center gap-1.5 text-[13px] text-muted"><Check size={14} /> {i18nT('components.appstore.featuredSpotlight.installed')}</span>
          ) : (
            <Btn primary className="rounded-full px-4 py-1.5 font-semibold" disabled={busy} onClick={onGet}>
              <Download size={14} /> {i18nT('components.appstore.featuredSpotlight.get')}
            </Btn>
          )}
          <span className="text-[12px] text-muted">{i18nT('components.appstore.featuredSpotlight.v')}{app.installedVersion || app.version} · {sourceLabel(app)}</span>
        </div>
      </div>
      <div
        className="relative min-h-[200px] md:min-h-[250px] grid place-items-center overflow-hidden"
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
          <div className="w-[92px] h-[92px] rounded-3xl bg-white/15 border border-white/25 backdrop-blur-sm grid place-items-center text-white">
            {(app.iconUrl || app.icon) ? <AppIcon icon={app.icon} iconUrl={app.iconUrl} size={56} /> : <Package size={44} />}
          </div>
        )}
      </div>
    </Clickable>
  )
}
