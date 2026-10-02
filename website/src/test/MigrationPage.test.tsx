import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useNavigate, useLocation } from 'react-router-dom'
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

import MigrationPage from '../pages/MigrationPage'
import { i18nT } from '../i18n/t'

function InstallPage() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  return <div>install page<span>{pathname}</span><button onClick={() => navigate(-1)}>Back</button></div>
}

function renderAt(name: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps', `/apps/migrate/${name}`]} initialIndex={1}>
        <Routes>
          <Route path="/apps" element={<div>apps page</div>} />
          <Route path="/apps/migrate/:name" element={<MigrationPage />} />
          <Route path="/apps/detail/:name" element={<InstallPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function mockRetiredApp() {
  getApp.mockResolvedValue({
    name: 'retired-app', displayName: 'Retired App', version: '1.0.0',
    enabled: true, origin: 'builtin',
  })
  listApps.mockResolvedValue([{ name: 'retired-app', orphaned: true }])
  listRegistry.mockResolvedValue({
    apps: [{ name: 'retired-app', displayName: 'Retired App', installed: true }],
  })
}

describe('MigrationPage', () => {
  beforeEach(() => {
    getApp.mockReset()
    listApps.mockReset()
    listRegistry.mockReset()
    migrateCleanup.mockReset()
  })

  it('does not claim a same-name successor is installed when only the stale record holds the slot', async () => {
    // The registry marks the row installed because the stale builtin record
    // shares its name; the successor itself was never installed.
    mockRetiredApp()
    let finishCleanup!: () => void
    migrateCleanup.mockImplementation(() => new Promise<void>(resolve => { finishCleanup = resolve }))

    renderAt('retired-app')

    const install = await screen.findByRole('button', { name: /Install from Apps/ })
    expect(screen.queryByText('Migration Complete')).toBeNull()
    expect(install.querySelector('svg')).not.toHaveClass('animate-spin')

    fireEvent.click(install)

    await waitFor(() => expect(migrateCleanup).toHaveBeenCalledWith('retired-app'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Install from Apps' })).toBeDisabled())
    expect(install.querySelector('svg')).toHaveClass('animate-spin')
    expect(screen.queryByText('install page')).toBeNull()
    await act(async () => finishCleanup())
    expect(await screen.findByText('install page')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(await screen.findByText('apps page')).toBeTruthy()
  })

  it('keeps the cleanup notice visible and offers installation of the same-name successor', async () => {
    mockRetiredApp()
    const notice = 'Conversation pointers were dropped in memory, but the write did not persist -- a reinstall may still resume one'
    migrateCleanup.mockResolvedValue({ ok: true, notice })

    renderAt('retired-app')
    fireEvent.click(await screen.findByRole('button', { name: 'Install from Apps' }))

    expect(await screen.findByTestId('migration-page-error')).toHaveTextContent(notice)
    expect(screen.getByText(i18nT('pages.migrationPage.cleanup_complete'))).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Back to Apps' })).toBeEnabled()
    expect(screen.queryByText('install page')).toBeNull()
    expect(migrateCleanup).toHaveBeenCalledWith('retired-app')

    fireEvent.click(screen.getByRole('button', { name: 'Install from Apps' }))
    expect(await screen.findByText('install page')).toBeTruthy()
    expect(screen.getByText('/apps/detail/retired-app')).toBeTruthy()
    expect(migrateCleanup).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(await screen.findByText('apps page')).toBeTruthy()
  })

  it('shows a cleanup notice and completion for a differently named successor', async () => {
    getApp.mockResolvedValue({
      name: 'old-app', displayName: 'Old App', version: '1.0.0', enabled: true,
      origin: 'builtin', migratedTo: 'registry:new-app',
    })
    listApps.mockResolvedValue([{ name: 'old-app', orphaned: true }])
    listRegistry.mockResolvedValue({
      apps: [{ name: 'new-app', displayName: 'New App', installed: true }],
    })
    const notice = 'Conversation pointers were dropped in memory, but the write did not persist -- a reinstall may still resume one'
    migrateCleanup.mockResolvedValue({ ok: true, notice })

    renderAt('old-app')
    fireEvent.click(await screen.findByRole('button', { name: /Clean up old entry/ }))

    expect(await screen.findByTestId('migration-page-error')).toHaveTextContent(notice)
    expect(screen.getByText(i18nT('pages.migrationPage.cleanup_complete'))).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Install from Apps' })).toBeNull()
    expect(screen.queryByText('install page')).toBeNull()
    expect(migrateCleanup).toHaveBeenCalledWith('old-app')

    fireEvent.click(screen.getByRole('button', { name: 'Back to Apps' }))
    expect(await screen.findByText('apps page')).toBeTruthy()
  })

  it.each([false, undefined])('does not replace a builtin whose list orphaned flag is %s', async (orphaned) => {
    getApp.mockResolvedValue({
      name: 'active-app', displayName: 'Active App', version: '1.0.0',
      enabled: true, origin: 'builtin',
    })
    listApps.mockResolvedValue([
      { name: 'unrelated-app', orphaned: true },
      { name: 'active-app', orphaned },
    ])
    listRegistry.mockResolvedValue({
      apps: [{ name: 'active-app', displayName: 'Active App', installed: true }],
    })

    renderAt('active-app')

    expect(await screen.findByText('Migration Complete')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Install from Apps' })).toBeNull()
    expect(migrateCleanup).not.toHaveBeenCalled()
  })

  it('shows localized cleanup failure and does not navigate when the install handoff fails', async () => {
    mockRetiredApp()
    migrateCleanup.mockRejectedValue(new Error('backend failure with internal details'))

    renderAt('retired-app')
    fireEvent.click(await screen.findByRole('button', { name: 'Install from Apps' }))

    expect(await screen.findByText(i18nT('pages.migrationPage.cleanup_failed'))).toBeTruthy()
    expect(screen.queryByText('backend failure with internal details')).toBeNull()
    expect(screen.queryByText('install page')).toBeNull()
    expect(screen.getByRole('button', { name: 'Install from Apps' })).toBeEnabled()
  })

  it('still reports a differently named successor as installed', async () => {
    getApp.mockResolvedValue({
      name: 'old-app', displayName: 'Old App', version: '1.0.0', enabled: true,
      origin: 'builtin', migratedTo: 'registry:new-app',
    })
    listApps.mockResolvedValue([{ name: 'old-app', orphaned: true }])
    listRegistry.mockResolvedValue({
      apps: [{ name: 'new-app', displayName: 'New App', installed: true }],
    })
    migrateCleanup.mockRejectedValue(new Error('cleanup-only failure'))

    renderAt('old-app')

    expect(await screen.findByText('Migration Complete')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Clean up old entry/ }))
    expect(await screen.findByText('cleanup-only failure')).toBeTruthy()
  })
})
