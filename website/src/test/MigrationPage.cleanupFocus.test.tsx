/**
 * The migration cleanup buttons stay focusable while cleanup runs. The button
 * stays on screen during the request (and after a failure), so native
 * `disabled` would drop a keyboard user's focus to the page. `aria-disabled`
 * plus a guard keeps focus and ignores a second press.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
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

function renderAt(name: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/apps/migrate/${name}`]}>
        <Routes>
          <Route path="/apps/migrate/:name" element={<MigrationPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function expectFocusKeptWhileBusy(button: HTMLElement) {
  // The cleanup never settles, so the button stays busy for the rest of the test.
  migrateCleanup.mockReturnValue(new Promise(() => {}))
  button.focus()
  fireEvent.click(button)
  await waitFor(() => expect(button).toHaveAttribute('aria-disabled', 'true'))
  expect(button).toBeInTheDocument()
  expect(button).not.toBeDisabled()
  expect(document.activeElement).toBe(button)
  fireEvent.click(button)
  expect(migrateCleanup).toHaveBeenCalledTimes(1)
}

describe('MigrationPage cleanup buttons while cleanup runs', () => {
  beforeEach(() => {
    getApp.mockReset()
    listApps.mockReset()
    listRegistry.mockReset()
    migrateCleanup.mockReset()
  })

  it('keeps focus on Install from Apps (same-name successor)', async () => {
    getApp.mockResolvedValue({ name: 'retired-app', displayName: 'Retired App', version: '1.0.0', enabled: true, origin: 'builtin' })
    listApps.mockResolvedValue([{ name: 'retired-app', orphaned: true }])
    listRegistry.mockResolvedValue({ apps: [{ name: 'retired-app', displayName: 'Retired App', installed: true }] })
    renderAt('retired-app')
    await expectFocusKeptWhileBusy(await screen.findByRole('button', { name: 'Install from Apps' }))
  })

  it('keeps focus on Clean up old entry (differently named successor)', async () => {
    getApp.mockResolvedValue({ name: 'old-app', displayName: 'Old App', version: '1.0.0', enabled: true, origin: 'builtin', migratedTo: 'registry:new-app' })
    listApps.mockResolvedValue([{ name: 'old-app', orphaned: true }])
    listRegistry.mockResolvedValue({ apps: [{ name: 'new-app', displayName: 'New App', installed: true }] })
    renderAt('old-app')
    await expectFocusKeptWhileBusy(await screen.findByRole('button', { name: /Clean up old entry/ }))
  })
})
