/**
 * A query client whose `['apps']` list is already answered, for a test that
 * renders a component reading the installed apps (`useInstalledApps`) without
 * caring what they are. `staleTime: Infinity` keeps the seeded answer from being
 * refetched, so nothing reaches `api.listApps`.
 */
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

export function installedAppsClient(apps: unknown[] = []): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['apps'], apps)
  return client
}

/** A `render` wrapper serving *apps* as the installed-apps list. */
export function withInstalledApps(apps: unknown[] = []) {
  const client = installedAppsClient(apps)
  return function InstalledAppsWrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}
