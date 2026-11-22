/**
 * MigrationRoute — opening another app's migration page starts it fresh.
 *
 * The nav rail links every migrating app to /apps/migrate/:name, so it
 * navigates straight from one app's migration page to another's. The route
 * keys the page by name; without that, the first app's cleanup notice, its
 * Cleanup Complete card and its install step stayed on the second app's page.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const getApp = vi.fn()
const listApps = vi.fn()
const listRegistry = vi.fn()
const migrateCleanup = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    migrateCleanup: (...a: unknown[]) => migrateCleanup(...a),
  },
}))

import MigrationRoute from '../pages/MigrationRoute'
import { i18nT } from '../i18n/t'

/** Stands in for the nav rail's row of a second migrating app. */
function OpenBeta() {
  const navigate = useNavigate()
  return <button type="button" onClick={() => navigate('/apps/migrate/beta')}>open beta</button>
}

function renderRoute() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps/migrate/alpha']}>
        <OpenBeta />
        <Routes>
          <Route path="/apps/migrate/:name" element={<MigrationRoute />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('MigrationRoute', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getApp.mockImplementation((name: string) => Promise.resolve({
      name, displayName: name === 'beta' ? 'Beta' : 'Alpha', version: '1.0.0', enabled: true, origin: 'builtin',
    }))
    listApps.mockResolvedValue([{ name: 'alpha', orphaned: true }, { name: 'beta', orphaned: true }])
    listRegistry.mockResolvedValue({ apps: [{ name: 'alpha', displayName: 'Alpha', installed: true }] })
    migrateCleanup.mockResolvedValue({ ok: true, notice: 'Alpha cleanup notice' })
  })

  it("does not carry one app's cleanup result onto the next app's migration page", async () => {
    renderRoute()
    await userEvent.click(await screen.findByRole('button', { name: 'Install from Apps' }))
    expect(await screen.findByTestId('migration-page-error')).toHaveTextContent('Alpha cleanup notice')
    expect(screen.getByText(i18nT('pages.migrationPage.cleanup_complete'))).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'open beta' }))
    await waitFor(() => expect(getApp).toHaveBeenCalledWith('beta'))
    expect(await screen.findByText('Beta has moved to a standalone app')).toBeInTheDocument()
    expect(screen.queryByTestId('migration-page-error')).not.toBeInTheDocument()
    expect(screen.queryByText(i18nT('pages.migrationPage.cleanup_complete'))).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Install from Apps' })).not.toBeInTheDocument()
  })
})
