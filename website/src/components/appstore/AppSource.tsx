import { i18nT } from '../../i18n/t'
import { sourceLabel, type RegistryApp } from './types'
import { Badge } from '../ui'

export type SourceName = { name: string; label: string; review?: string; builtin?: boolean }

/** Source names are display metadata, never an endorsement or a trust grant. */
export default function AppSource({ app, sources = [], unlisted = false }: {
  app: Pick<RegistryApp, '_registry' | 'origin' | 'provenance'>
  sources?: SourceName[]
  unlisted?: boolean
}) {
  // Pinned rows precede operator rows. Keep that collision precedence; a stale
  // case-variant catalog tag still shows its own id, like the source rail.
  const registry = app._registry
    ? sources.find(r => !r.builtin && r.name.toLowerCase() === app._registry?.toLowerCase())
    : undefined
  const builtin = !app._registry && (app.provenance === 'builtin' || (!app.provenance && app.origin === 'builtin'))
  const label = unlisted && app.origin !== 'builtin'
    ? app.origin === 'local' ? i18nT('appStoreSources.local') : i18nT('appStoreSources.unknown')
    : registry && registry.name === app._registry
      ? registry.label || app._registry
      : builtin ? i18nT('pages.appsPage.built_in_kirocrew') : sourceLabel(app)

  const review = !unlisted && registry?.name === app._registry ? registry?.review : undefined

  return (
    <span className="block text-[12px] text-muted break-words [overflow-wrap:anywhere]">
      {i18nT('appStoreSources.source')} <span data-i18n-opaque title={label}>{label}</span>
      {review === 'community' && <> <Badge variant="warn">{i18nT('components.appstore.registryTier.community')}</Badge></>}
      {review === 'curated' && <> <Badge variant="aim">{i18nT('components.appstore.registryTier.curated')}</Badge></>}
    </span>
  )
}
