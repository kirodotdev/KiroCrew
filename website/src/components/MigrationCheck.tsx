/**
 * MigrationCheck — Checks if the current route matches a builtin app
 * that has `migratedTo` set. If so, renders MigrationBanner above the page content.
 *
 * Uses React Query with shared ['apps'] query key for cache deduplication
 * with AppsPage and other components.
 */
import { useMemo } from 'react'
import { useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import MigrationBanner from './MigrationBanner'

interface AppEntry {
  name: string
  displayName: string
  enabled: boolean
  origin?: string
  migratedTo?: string
  manifest?: {
    ui?: { pages?: { route: string; label: string }[] }
  }
}

// Routes that can never host a migratable app — skip query entirely.
const NON_APP_PREFIXES = ['/chat', '/settings', '/orchestrated', '/capabilities', '/apps/migrate', '/apps/detail']

// Extension seam: a downstream edition registers its own non-app route
// prefixes (e.g. a bundled panel) so the migration banner never probes them —
// instead of editing (and re-diffing) NON_APP_PREFIXES on every upstream sync.
const EXTRA_NON_APP_PREFIXES: string[] = []

export function registerNonAppPrefix(prefix: string): void {
  if (!EXTRA_NON_APP_PREFIXES.includes(prefix)) EXTRA_NON_APP_PREFIXES.push(prefix)
}

export default function MigrationCheck() {
  const location = useLocation()
  const isAppRoute = ![...NON_APP_PREFIXES, ...EXTRA_NON_APP_PREFIXES].some(p => location.pathname === p || location.pathname.startsWith(p + '/'))

  // Deliberately no error surface here (recorded for the errors-use-error-notice
  // sweep): this is a best-effort probe that decides whether to show a
  // migration banner, mounted on EVERY app route. A failed ['apps'] read is
  // reported where that data is the page's subject (AppsPage / AppDetailPage
  // render it through ErrorNotice); repeating it above every app page would put
  // the same failure on screen twice, and the only cost of the miss is a banner
  // not shown.
  const { data: migratedApps = [] } = useQuery<AppEntry[], Error, AppEntry[]>({
    queryKey: ['apps'],
    queryFn: () => api.listApps(),
    select: (apps) => apps.filter(a => a.origin === 'builtin' && a.migratedTo && a.enabled),
    enabled: isAppRoute,
  })

  const migrationApp = useMemo(() => {
    if (!isAppRoute) return null
    return migratedApps.find(a =>
      a.manifest?.ui?.pages?.some(p =>
        location.pathname === p.route || location.pathname.startsWith(p.route + '/')
      )
    ) || null
  }, [migratedApps, location.pathname, isAppRoute])

  if (!migrationApp || !migrationApp.migratedTo) return null

  return (
    <MigrationBanner
      appName={migrationApp.displayName || migrationApp.name}
      migratedTo={migrationApp.migratedTo}
    />
  )
}
