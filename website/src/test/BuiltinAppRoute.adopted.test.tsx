import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import BuiltinAppRoute from '../apps/BuiltinAppRoute'
import { useAppIdentity, useTrustedAppId } from '../app-sdk/identity'
import { withInstalledApps } from './installedAppsTestUtils'
import { i18nT } from '../i18n/t'

/**
 * A former built-in whose record the gateway moved to its registry copy renders
 * that copy's own bundle (through AppHost, under the record's own origin) at the
 * route its compiled page is registered at. Everything else keeps the compiled page.
 */

function CompiledPage() {
  return <div data-testid="compiled-page" data-identity={useAppIdentity()?.origin ?? ''} />
}

vi.mock('../apps/builtinRegistry', async () => {
  const { lazy } = await import('react')
  const component = lazy(() => Promise.resolve({ default: CompiledPage }))
  return {
    getBuiltinApp: (path: string) => (path === '/former-app' ? { component, appId: 'former-app' } : undefined),
    hasBuiltinComponent: (path: string) => path === '/former-app',
    builtinRefusalReason: () => undefined,
    BUILTIN_COMPONENT_REGISTRY: {},
  }
})

// AppHost dynamic-imports the bundle from the gateway, which a unit test has no
// server for. The stand-in records what it was handed, and what identity is in
// context where the bundle would mount.
function AppHostProbe({ app }: { app: { name: string; origin?: string } }) {
  return (
    <div
      data-testid="app-host"
      data-name={app.name}
      data-origin={app.origin ?? ''}
      data-identity={useAppIdentity()?.origin ?? 'none'}
      data-trusted={useTrustedAppId() ?? 'refused'}
    />
  )
}
vi.mock('../components/AppHost', () => ({ default: AppHostProbe }))

const listApps = vi.fn()
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, listApps: () => listApps() } }
})

const ADOPTED = {
  name: 'former-app',
  enabled: true,
  origin: 'registry',
  adoption: { state: 'adopted', legacyNav: { route: '/former-app', icon: 'ShoppingBag' } },
  manifest: { ui: { entry: 'dist/index.mjs', pages: [{ route: '/apps/former-app', label: 'Former' }] } },
}

/**
 * Ceiling for a wait that crosses the route's async chain: the `['apps']` query
 * answers, then the mocked `React.lazy` page (or AppHost's stand-in) commits
 * under the route's Suspense boundary. It only turns a hang into a named failure.
 */
const LAZY_PAGE_TIMEOUT = { timeout: 5000 }

function renderAt(path: string, wrapper: (props: { children: React.ReactNode }) => React.ReactElement) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/:builtinApp/*" element={<BuiltinAppRoute />} />
      </Routes>
    </MemoryRouter>,
    { wrapper },
  )
}

describe('BuiltinAppRoute for an adopted former built-in', () => {
  it('renders the record through AppHost with its own origin and no builtin identity', async () => {
    renderAt('/former-app', withInstalledApps([ADOPTED]))
    // The list query answers, then the adopted copy mounts.
    const host = await screen.findByTestId('app-host', undefined, LAZY_PAGE_TIMEOUT)
    expect(host.dataset.name).toBe('former-app')
    expect(host.dataset.origin).toBe('registry')
    // The route publishes no `builtin` identity around the copy, so it cannot
    // reach the host's first-party namespace.
    expect(host.dataset.identity).toBe('none')
    expect(host.dataset.trusted).toBe('refused')
    expect(screen.queryByTestId('compiled-page')).toBeNull()
  })

  it.each([
    ['a builtin-owned record', { ...ADOPTED, origin: 'builtin' }],
    ['a record still staged', { ...ADOPTED, adoption: { state: 'staged' } }],
    ['an adopted record with no bundle', { ...ADOPTED, manifest: { ui: { pages: [] } } }],
    ['an adopted record for another route', { ...ADOPTED, adoption: { state: 'adopted', legacyNav: { route: '/elsewhere', icon: '' } } }],
  ])('keeps the compiled page for %s', async (_label, record) => {
    renderAt('/former-app', withInstalledApps([record]))
    // The list query answers, then the lazy compiled page commits.
    const page = await screen.findByTestId('compiled-page', undefined, LAZY_PAGE_TIMEOUT)
    expect(page.dataset.identity).toBe('builtin')
    expect(screen.queryByTestId('app-host')).toBeNull()
  })

  it('keeps the compiled page when no record exists', async () => {
    renderAt('/former-app', withInstalledApps([]))
    // The list query answers, then the lazy compiled page commits.
    expect(await screen.findByTestId('compiled-page', undefined, LAZY_PAGE_TIMEOUT)).toBeInTheDocument()
    // A list that loaded is not a lookup failure.
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows a skeleton, not the compiled page, while the list is still loading', () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // A list request that never answers inside this test.
    listApps.mockReturnValueOnce(new Promise(() => {}))
    renderAt('/former-app', ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>)
    expect(screen.queryByTestId('compiled-page')).toBeNull()
    expect(screen.queryByTestId('app-host')).toBeNull()
    expect(listApps).toHaveBeenCalled()
  })

  it('falls back to the compiled page when the first list load fails, and says the lookup failed', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    listApps.mockRejectedValueOnce(new Error('offline'))
    renderAt('/former-app', ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>)
    // The rejected list query settles, then the lazy compiled page commits.
    expect(await screen.findByTestId('compiled-page', undefined, LAZY_PAGE_TIMEOUT)).toBeInTheDocument()
    // Rendered in the same commit as the page: the failed query decided both.
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain(i18nT('apps.builtinAppRoute.apps_list_failed'))
    expect(alert.textContent).toContain('offline')
  })
})
