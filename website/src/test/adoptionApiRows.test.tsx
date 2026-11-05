import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import '../i18n/all'
import { i18nT } from '../i18n/t'
import AppDetailPage from '../pages/AppDetailPage'
import BuiltinAppRoute from '../apps/BuiltinAppRoute'
import { adoptedLegacyNav, appNavTarget, type AppNavRecord } from '../appNav'
import { useAppIdentity, useTrustedAppId } from '../app-sdk/identity'
import { withInstalledApps } from './installedAppsTestUtils'

/**
 * From the rows the gateway produces to what the dashboard renders.
 *
 * `fixtures/adoptionApiRows.json` is written from the real `/api/apps` handlers by
 * `test/test_builtin_adoption_dashboard_rows.py`, which fails when the handlers stop
 * producing those bytes. This file renders the app detail page and the legacy route
 * from the same bytes, so the two halves cannot drift apart on a field name.
 */

type Row = AppNavRecord & { name: string; displayName: string; origin: string }
const ROWS = JSON.parse(
  readFileSync(join(__dirname, 'fixtures/adoptionApiRows.json'), 'utf8'),
) as { detail: Record<string, Row>; list: { adopted: Row[] } }
const DISPLAY = ROWS.detail.staged.displayName
// The fixture app's compiled route, which the wheel pin keeps for its adopted copy.
const LEGACY_ROUTE = '/fixture-former'

function CompiledPage() {
  return <div data-testid="compiled-page" />
}

vi.mock('../apps/builtinRegistry', async () => {
  const { lazy } = await import('react')
  const component = lazy(() => Promise.resolve({ default: CompiledPage }))
  return {
    getBuiltinApp: (path: string) => (path === '/fixture-former' ? { component, appId: 'fixture-former' } : undefined),
    hasBuiltinComponent: (path: string) => path === '/fixture-former',
    builtinRefusalReason: () => undefined,
    BUILTIN_COMPONENT_REGISTRY: {},
  }
})

// AppHost dynamic-imports the bundle from the gateway, which a unit test has no
// server for; the stand-in records what it was handed.
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
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))

const getApp = vi.fn()
const listApps = vi.fn()
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getApp: (...a: unknown[]) => getApp(...a),
      listApps: () => listApps(),
      listRegistry: () => Promise.resolve({ apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } }),
      system: () => Promise.resolve({ hostname: '' }),
      appContributors: () => Promise.resolve([]),
    },
  }
})

/** Ceiling for a wait across the page's async load; it only names a hang. */
const LOAD_TIMEOUT = { timeout: 5000 }

function renderDetail(row: Row) {
  getApp.mockResolvedValue(row)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/apps/detail/${row.name}`]}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function line(key: string, withName = true): string {
  return i18nT(`pages.appDetailPage.adoption.${key}`, withName ? { name: DISPLAY } : undefined)
}

describe('the app detail page renders the adoption row the gateway produced', () => {
  beforeEach(() => { getApp.mockReset() })

  it.each([
    ['blocked_admission', line('policy'), line('keeps_builtin_until', false)],
    ['blocked_verification', line('verification'), line('keeps_builtin', false)],
    ['pending_network', line('network'), line('keeps_builtin_until', false)],
  ])('%s is a failed attempt, shown on the shared error surface', async (scenario, remedy, keeps) => {
    renderDetail(ROWS.detail[scenario])
    const notice = await screen.findByTestId('app-adoption-failure', undefined, LOAD_TIMEOUT)
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain(remedy)
    expect(notice.textContent).toContain(keeps)
  })

  it.each([
    ['staged', line('staged')],
    ['adopted_host_too_old', line('installed_needs_newer_host')],
  ])('%s is status, not a failure', async (scenario, text) => {
    renderDetail(ROWS.detail[scenario])
    const notice = await screen.findByText(text, undefined, LOAD_TIMEOUT)
    expect(notice.closest('[role="status"]')).not.toBeNull()
    expect(screen.queryByTestId('app-adoption-failure')).toBeNull()
  })

  it('says nothing for an adopted record on a host new enough for it', async () => {
    renderDetail(ROWS.detail.adopted)
    expect(await screen.findAllByText(DISPLAY, undefined, LOAD_TIMEOUT)).not.toHaveLength(0)
    for (const key of ['staged', 'installed_needs_newer_host', 'policy', 'network']) {
      expect(screen.queryByText(line(key), { exact: false })).toBeNull()
    }
  })
})

describe('the legacy route and rail entry follow the adopted row the gateway produced', () => {
  it('keeps the former route and glyph in the rail', () => {
    const [row] = ROWS.list.adopted
    expect(adoptedLegacyNav(row)).toEqual({ route: LEGACY_ROUTE, icon: 'ShoppingBag' })
    const target = appNavTarget(row)
    expect(target?.builtin).toBe(false)
    expect(target?.glyphAllowed).toBe(true)
  })

  it('mounts the adopted copy at the legacy route under its own origin', async () => {
    render(
      <MemoryRouter initialEntries={[LEGACY_ROUTE]}>
        <Routes>
          <Route path="/:builtinApp/*" element={<BuiltinAppRoute />} />
        </Routes>
      </MemoryRouter>,
      { wrapper: withInstalledApps(ROWS.list.adopted) },
    )
    const host = await screen.findByTestId('app-host', undefined, LOAD_TIMEOUT)
    expect(host.dataset.name).toBe(ROWS.list.adopted[0].name)
    expect(host.dataset.origin).toBe('registry')
    expect(host.dataset.identity).toBe('none')
    expect(host.dataset.trusted).toBe('refused')
    expect(screen.queryByTestId('compiled-page')).toBeNull()
    expect(listApps).not.toHaveBeenCalled()
  })

  it('keeps the compiled page for a row the gateway still owns as a built-in', async () => {
    render(
      <MemoryRouter initialEntries={[LEGACY_ROUTE]}>
        <Routes>
          <Route path="/:builtinApp/*" element={<BuiltinAppRoute />} />
        </Routes>
      </MemoryRouter>,
      { wrapper: withInstalledApps([ROWS.detail.staged]) },
    )
    expect(await screen.findByTestId('compiled-page', undefined, LOAD_TIMEOUT)).toBeInTheDocument()
    expect(screen.queryByTestId('app-host')).toBeNull()
  })
})
