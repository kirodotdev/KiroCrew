/**
 * GitHub star count on the app detail page.
 *
 * The registry-only (not-installed) branch spreads the RAW `listRegistry()`
 * payload — it never passes through `normalizeRegistryApp` — so the page must
 * sanitize the display-only star count itself (`sanitizeStargazersCount`).
 * These tests pin both halves: a valid count renders in the hero subtitle
 * (compact) AND the Details card (exact), and a malformed value from a
 * hostile/older gateway is suppressed rather than rendered as NaN/-1/1e308.
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

function renderDetail(name = 'todo-ledger') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/apps/detail/${name}`]}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
          <Route path="/apps" element={<div>apps list</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** A not-installed git-type third-party registry row, in the API envelope. */
const registryPayload = (stargazersCount: unknown) => ({
  apps: [{
    name: 'todo-ledger',
    displayName: 'Todo Ledger',
    description: 'A third-party app.',
    version: '1.2.0',
    author: 'octocat',
    tags: ['productivity'],
    installed: false,
    origin: 'registry',
    repo: 'https://example.invalid/octocat/todo-ledger',
    stargazersCount,
  }],
  serverPlatform: { os: 'linux', arch: 'x86_64' },
})

describe('AppDetailPage — star count', () => {
  beforeEach(() => {
    getApp.mockReset()
    listRegistry.mockReset()
    system.mockReset()
    system.mockResolvedValue({ hostname: '' })
    // Not installed: /api/apps/{name} rejects, the page falls through to the
    // registry row — the branch that spreads the raw payload.
    getApp.mockRejectedValue(new Error('not found'))
  })

  it('renders the compact count in the hero and the exact count in Details', async () => {
    listRegistry.mockResolvedValue(registryPayload(15300))
    renderDetail();
    // Hero subtitle: en compact of 15300.
    expect(await screen.findByText(/15\.3K/)).toBeInTheDocument()
    // Details card: exact locale-formatted count.
    expect(screen.getByText(/15,300/)).toBeInTheDocument()
    // The star icon carries an accessible name on both surfaces.
    expect(screen.getAllByLabelText('GitHub stars').length).toBeGreaterThan(0)
  })

  it('suppresses a malformed count from a non-sanitizing gateway', async () => {
    listRegistry.mockResolvedValue(registryPayload(-5))
    renderDetail();
    await screen.findAllByText('Todo Ledger')
    expect(screen.queryByLabelText('GitHub stars')).toBeNull()
    expect(screen.queryByText('-5')).toBeNull()
  })

  it('suppresses an unsafe-magnitude count (finite but not a safe integer)', async () => {
    listRegistry.mockResolvedValue(registryPayload(1e308))
    renderDetail();
    await screen.findAllByText('Todo Ledger')
    expect(screen.queryByLabelText('GitHub stars')).toBeNull()
  })

  it('renders no star surfaces when the field is absent', async () => {
    listRegistry.mockResolvedValue(registryPayload(undefined))
    renderDetail();
    await screen.findAllByText('Todo Ledger')
    expect(screen.queryByLabelText('GitHub stars')).toBeNull()
  })
})
