/**
 * FeaturedSpotlight — the editorial hero at the top of Discover.
 *
 * Layout: text panel (kicker / name / tagline /
 * provenance meta / CTA) beside an art panel. Art prefers CURATOR-authored
 * editorial artwork, then the app's own theme-appropriate hero image, and
 * degrades to a deterministic gradient with the app icon on a glass tile.
 *
 * ONE APP OR A GROUP. `apps` is a list because the editorial document makes a
 * spotlight a list: promoting one app to a collection is a data edit, not a new
 * component. The first entry drives the hero panel, and any others render as
 * compact chips beneath it -- so a group is one placement with several apps
 * rather than several placements, which is what `title` names.
 */
import { BadgeCheck, Check, Download, Package, Power } from 'lucide-react'
import { Btn } from '../ui'
import Clickable from '../Clickable'
import AppIcon from '../AppIcon'
import { gradientFor } from './gradient'
import { categoryFor } from './categories'
import { useHeroArt } from './useHeroArt'
import { useEditorialArt, type EditorialArtwork } from './useEditorialArt'
import { sourceLabel, isVerified, type RegistryApp } from './types'
import { appDisplayName, appDescription } from './appManifest'

import { i18nT } from '../../i18n/t'
export default function FeaturedSpotlight({
  app,
  apps,
  title,
  blurb,
  artwork,
  onOpen,
  onGet,
  onEnable,
  onOpenApp,
  busy,
}: {
  app: RegistryApp
  /** Additional apps in the same placement. The hero app is `app`. */
  apps?: RegistryApp[]
  /** Names the group. Absent for a single app, where its own name is the heading. */
  title?: string
  /** Curator copy, preferred over the app's own description when present. */
  blurb?: string
  artwork?: EditorialArtwork | null
  onOpen: (e?: React.MouseEvent | React.KeyboardEvent) => void
  onGet: () => void
  onEnable: () => void
  /** Opens one of the companion apps. Only needed when `apps` is non-empty. */
  onOpenApp?: (name: string, e?: React.MouseEvent | React.KeyboardEvent) => void
  busy?: boolean
}) {
  const hero = useHeroArt(app)
  const editorial = useEditorialArt(artwork)
  // Editorial artwork wins: this is an editorial placement, so the curator's
  // picture is the point of it. The app's own hero is the fallback it always was.
  const artSrc = editorial.src || hero.src
  const onArtError = editorial.src ? editorial.onError : hero.onError
  const companions = apps || []
  const hiddenBuiltin = app.origin === 'builtin' && app.installed && !app.enabled

  return (
    <Clickable
      aria-label={i18nT('components.appstore.featuredSpotlight.view_details_for', { name: appDisplayName(app) })}
      className="grid grid-cols-1 md:grid-cols-[1.05fr_.95fr] border border-border rounded-[20px] overflow-hidden bg-card mb-3.5 cursor-pointer group hover:border-border-strong transition-colors focus-ring"
      onClick={onOpen}
    >
      <div className="px-9 py-8 flex flex-col justify-center gap-2.5 min-w-0">
        <span className="text-[11px] font-bold tracking-[.14em] text-accent">{i18nT('components.appstore.featuredSpotlight.featured')}</span>
        <h2 className="text-[32px] leading-[1.15] font-bold text-text-strong tracking-tight">{title || appDisplayName(app)}</h2>
        <p className="text-[15px] text-muted line-clamp-2" title={blurb || appDescription(app)}>{blurb || appDescription(app)}</p>
        <div className="flex items-center gap-2 text-[12.5px] text-muted">
          {isVerified(app) && (
            <BadgeCheck size={14} className="text-accent shrink-0" aria-label={i18nT('components.appstore.featuredSpotlight.verified_publisher')}>
              <title>{i18nT('components.appstore.featuredSpotlight.verified_publisher_first_party')}</title>
            </BadgeCheck>
          )}
          <span className="truncate">
            {/* A GROUP shows no category: the meta row is derived from the hero
                app, and a cross-cutting collection ("Ship it before lunch" over
                research + productivity + code-review) would be labelled with one
                member's category, which is wrong rather than merely imprecise.
                The author still applies -- these are all ours. */}
            {app.author}{title ? '' : ` · ${categoryFor(app.tags)}`}
          </span>
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
        {companions.length > 0 && (
          <div
            className="flex flex-wrap gap-2 mt-3"
            onClick={e => e.stopPropagation()}
            onKeyDown={e => e.stopPropagation()}
            role="presentation"
          >
            {companions.map(other => (
              <Clickable
                key={other.name}
                aria-label={i18nT('components.appstore.featuredSpotlight.view_details_for', { name: appDisplayName(other) })}
                className="inline-flex items-center gap-2 rounded-full border border-border bg-elevated pl-1.5 pr-3 py-1.5 hover:border-border-strong transition-colors focus-ring min-w-0"
                onClick={e => onOpenApp?.(other.name, e)}
              >
                <AppIcon icon={other.icon} iconUrl={other.iconUrl} iconUrlDark={other.iconUrlDark} size={20} />
                <span className="text-[12.5px] text-text-strong truncate max-w-[160px]">{appDisplayName(other)}</span>
              </Clickable>
            ))}
          </div>
        )}
      </div>
      <div
        className="relative min-h-[200px] md:min-h-[250px] grid place-items-center overflow-hidden"
        style={artSrc ? { background: 'var(--card)' } : { background: gradientFor(app.name) }}
      >
        {artSrc ? (
          <img
            src={artSrc}
            alt={editorial.src ? editorial.alt : ''}
            className="absolute inset-0 w-full h-full object-cover group-hover:scale-[1.02] transition-transform duration-300"
            onError={onArtError}
          />
        ) : (
          <div className="w-[92px] h-[92px] rounded-3xl bg-white/15 border border-white/25 backdrop-blur-sm grid place-items-center text-white">
            {(app.iconUrl || app.iconUrlDark || app.icon) ? <AppIcon icon={app.icon} iconUrl={app.iconUrl} iconUrlDark={app.iconUrlDark} size={56} /> : <Package size={44} />}
          </div>
        )}
      </div>
    </Clickable>
  )
}
