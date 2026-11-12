import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'

// The detail page's three reads, and whether the first render shares the app
// shell's `['registry']` listing instead of building it a second time.

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()
const appContributors = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
    appContributors: (...a: unknown[]) => appContributors(...a),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))

import AppDetailPage from '../pages/AppDetailPage'
import { REGISTRY_STALE_MS, registryQueryFn } from '../pages/apps/useAppsData'

const ROW = {
  name: 'notes-app',
  displayName: 'Notes App',
  description: 'Keeps notes',
  version: '1.2.0',
  author: 'someone',
  repo: 'ssh://git@github.com/someone/notes-app',
  _registry: 'kieser',
  installed: false,
}

const notFound = () => Object.assign(new Error('not found'), { status: 404 })

/** Resolvable from the test, so a read can be held open while others are asserted. */
function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

/** The app shell's observer of the same key, as the nav rail badge mounts it. */
function ShellObserver() {
  useQuery({ queryKey: ['registry'], queryFn: registryQueryFn, staleTime: REGISTRY_STALE_MS })
  return null
}

function renderDetail(qc: QueryClient, { withShell = false } = {}) {
  return render(
    <QueryClientProvider client={qc}>
      {withShell && <ShellObserver />}
      <MemoryRouter initialEntries={['/apps/detail/notes-app']}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Wait until the page header names the resolved app. */
const shown = (name: string) =>
  waitFor(() => expect(screen.getByTestId('page-subtitle').textContent).toBe(name))

const newClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } })

describe('AppDetailPage listing reads', () => {
  beforeEach(() => {
    getApp.mockReset()
    listRegistry.mockReset()
    system.mockReset()
    appContributors.mockReset()
    appContributors.mockResolvedValue({ contributors: [] })
    system.mockResolvedValue({ hostname: 'host-1' })
  })

  it('shares the listing the shell already has in flight on first render', async () => {
    const listing = deferred<{ apps: unknown[] }>()
    listRegistry.mockReturnValue(listing.promise)
    getApp.mockRejectedValue(notFound())
    renderDetail(newClient(), { withShell: true })

    await waitFor(() => expect(getApp).toHaveBeenCalledTimes(1))
    listing.resolve({ apps: [ROW] })

    await shown('Notes App')
    expect(listRegistry).toHaveBeenCalledTimes(1)
  })

  it('uses a fresh cached listing without asking the server again', async () => {
    const qc = newClient()
    qc.setQueryData(['registry'], await (async () => {
      listRegistry.mockResolvedValueOnce({ apps: [ROW] })
      return registryQueryFn()
    })())
    listRegistry.mockClear()
    getApp.mockRejectedValue(notFound())
    renderDetail(qc)

    await shown('Notes App')
    expect(listRegistry).not.toHaveBeenCalled()
  })

  it('asks again when the cached row says installed but the installed record is gone', async () => {
    const qc = newClient()
    listRegistry.mockResolvedValueOnce({ apps: [{ ...ROW, installed: true }] })
    qc.setQueryData(['registry'], await registryQueryFn())
    listRegistry.mockReset()
    listRegistry.mockResolvedValue({ apps: [{ ...ROW, installed: false }] })
    getApp.mockRejectedValue(notFound())
    renderDetail(qc)

    expect(await screen.findByRole('button', { name: /Install/ })).toBeTruthy()
    expect(listRegistry).toHaveBeenCalledTimes(1)
  })

  it('keeps a cached not-installed row without asking the server again', async () => {
    const qc = newClient()
    listRegistry.mockResolvedValueOnce({ apps: [ROW] })
    qc.setQueryData(['registry'], await registryQueryFn())
    listRegistry.mockReset()
    getApp.mockRejectedValue(notFound())
    renderDetail(qc)

    expect(await screen.findByRole('button', { name: /Install/ })).toBeTruthy()
    expect(listRegistry).not.toHaveBeenCalled()
  })

  it('starts the listing and the hostname read before the installed read answers', async () => {
    const installed = deferred<never>()
    getApp.mockReturnValue(installed.promise)
    listRegistry.mockResolvedValue({ apps: [ROW] })
    renderDetail(newClient())

    await waitFor(() => expect(listRegistry).toHaveBeenCalledTimes(1))
    expect(system).toHaveBeenCalledTimes(1)
    expect(screen.queryAllByText('Notes App')).toEqual([])
  })

  it('asks the server for a new listing on Retry, even while the cached one is fresh', async () => {
    listRegistry.mockResolvedValue({ apps: [ROW] })
    getApp.mockRejectedValueOnce(Object.assign(new Error('gateway down'), { status: 500 }))
    getApp.mockRejectedValue(notFound())
    renderDetail(newClient())

    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    await shown('Notes App')
    expect(listRegistry).toHaveBeenCalledTimes(2)
  })

  it('still shows an installed app when the listing fails, and says the catalog failed', async () => {
    getApp.mockResolvedValue({
      name: 'notes-app', version: '1.0.0', enabled: true, origin: 'registry', installed: true,
      manifest: { displayName: 'Notes App', description: 'Keeps notes' },
    })
    listRegistry.mockRejectedValue(new Error('catalog unreachable'))
    renderDetail(newClient())

    await shown('Notes App')
    expect(await screen.findByText(/catalog unreachable/)).toBeTruthy()
  })
})
