/**
 * AppDetailPage — a failed Retry puts focus back on Retry.
 *
 * Retry swaps the failed-load view for the loading view, so the button the
 * reader pressed unmounts. When that retry fails too, focus goes back to the
 * new Retry instead of staying on <body>; any other load leaves focus alone.
 */
import { afterEach, describe, it, expect, vi, beforeEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useNavigate, type NavigateFunction } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
    updateApp: vi.fn(),
    installFromRegistryStream: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    enableApp: vi.fn(),
    disableApp: vi.fn(),
    uninstallApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const record = {
  name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
  source: 'registry:secretary', origin: 'registry',
  resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
  manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
}

let navigateTo: NavigateFunction = () => {}
function CaptureNavigate() {
  navigateTo = useNavigate()
  return null
}

async function failFirstLoad({ elsewhere = false } = {}) {
  getApp.mockRejectedValueOnce(new Error('gateway down'))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      {elsewhere && <button type="button">Elsewhere</button>}
      <MemoryRouter initialEntries={['/apps/detail/secretary']}>
        <CaptureNavigate />
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  expect(await screen.findByText('gateway down')).toBeTruthy()
  return screen.getByRole('button', { name: 'Retry' })
}

describe('AppDetailPage — Retry focus', () => {
  beforeEach(() => {
    system.mockResolvedValue({ hostname: '' })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' } })
  })
  afterEach(() => { cleanup(); vi.clearAllMocks() })

  it('puts focus back on Retry when the retry fails again', async () => {
    const retry = await failFirstLoad()
    const second = deferred<unknown>()
    getApp.mockReturnValueOnce(second.promise)
    retry.focus()
    fireEvent.click(retry)

    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    await act(async () => { second.reject(new Error('still down')) })

    expect(screen.getByText('still down')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toHaveFocus()
  })

  it('leaves focus alone when the user moved it during the retry', async () => {
    const retry = await failFirstLoad({ elsewhere: true })
    const second = deferred<unknown>()
    getApp.mockReturnValueOnce(second.promise)
    retry.focus()
    fireEvent.click(retry)
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' })
    elsewhere.focus()

    await act(async () => { second.reject(new Error('still down')) })

    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(elsewhere).toHaveFocus()
  })

  it('does not move focus when the retry succeeds', async () => {
    const retry = await failFirstLoad()
    getApp.mockResolvedValueOnce(record)
    retry.focus()
    await act(async () => { fireEvent.click(retry) })

    expect(await screen.findByText('Slack inbox manager.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(document.activeElement).toBe(document.body)
  })

  it('does not refocus a load a route change started during the retry', async () => {
    const retry = await failFirstLoad()
    const pending = deferred<unknown>()
    getApp.mockReturnValueOnce(pending.promise)
    retry.focus()
    fireEvent.click(retry)

    // Another app's page opens before the retry settles, and its own load fails.
    getApp.mockRejectedValueOnce(new Error('beta down'))
    await act(async () => { navigateTo('/apps/detail/beta') })

    expect(await screen.findByText('beta down')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(document.activeElement).toBe(document.body)
  })
})
