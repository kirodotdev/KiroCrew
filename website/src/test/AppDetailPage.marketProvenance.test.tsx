/**
 * AppDetailPage — marketplace provenance.
 *
 * An app installed from the marketplace records `source: "market:<ref>@<n>"`.
 * The Details card names that origin and the folder Sync action is hidden for
 * it, because its updates come from the marketplace. A folder-installed app
 * shows neither the line nor the note.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'

function renderDetail() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps/detail/todo-ledger']}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const installed = (source: string) => ({
  name: 'todo-ledger', displayName: 'Todo Ledger', version: '1.0.0', enabled: true,
  source, origin: 'local',
  resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
  manifest: { displayName: 'Todo Ledger', description: 'Tracks todos.', author: 'octocat' },
})

describe('AppDetailPage — marketplace provenance', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    system.mockResolvedValue({ hostname: '' })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } })
  })

  it('names the listing and version, hides Sync, and says where updates come from', async () => {
    getApp.mockResolvedValue(installed('market:acme/todo-ledger@3'))
    renderDetail()
    expect(await screen.findByText('Installed from Kiro Space: acme/todo-ledger, version 3')).toBeInTheDocument()
    expect(screen.getByText('Updates for this app come from Kiro Space.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Sync/i })).toBeNull()
  })

  it('shows no marketplace line for a folder-installed app and keeps Sync', async () => {
    getApp.mockResolvedValue(installed('/home/me/apps/todo-ledger'))
    renderDetail()
    expect(await screen.findByRole('button', { name: /Sync/i })).toBeInTheDocument()
    expect(screen.queryByText(/Installed from Kiro Space/)).toBeNull()
    expect(screen.queryByText(/Updates for this app come from Kiro Space/)).toBeNull()
  })

  it('treats a malformed market source as a folder source', async () => {
    getApp.mockResolvedValue(installed('market:acme/todo-ledger'))
    renderDetail()
    expect(await screen.findByRole('button', { name: /Sync/i })).toBeInTheDocument()
    expect(screen.queryByText(/Installed from Kiro Space/)).toBeNull()
  })
})
