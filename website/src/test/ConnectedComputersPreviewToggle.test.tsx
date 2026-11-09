/**
 * The Connected computers switch in Feature previews (flag scenarios): off on a
 * fresh install, a click writes the gateway's own copy that its backend reads
 * (`dashboard.feature_previews.connected_computers`), turning it off writes
 * false, and a refused write names the switch and puts it back.
 *
 * Authored for CI; this host runs no tests locally.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../api/client'
import {
  PREVIEW_FLAGS_SYNCED_KEY, __resetPreviewFlagServerSyncForTests, usePreviewFlagServerSync,
} from '../hooks/usePreviewFlagServerSync'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'
import { PREVIEW_CONNECTED_COMPUTERS, previewFlagConfigPath } from '../utils/previewFlags'

const PATH = 'dashboard.feature_previews.connected_computers'

function WithSync() {
  usePreviewFlagServerSync()
  return <FeaturePreviewsSection />
}

async function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings']}>
        <WithSync />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await act(async () => {
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('success'))
  })
  return qc
}

const toggle = () => screen.getAllByRole('switch', { name: /connected computers/i })[0]

let server: Record<string, boolean>

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem(PREVIEW_FLAGS_SYNCED_KEY, '1')
  __resetPreviewFlagServerSyncForTests()
  server = {}
  vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
  vi.spyOn(api, 'kirocrewConfig').mockImplementation(
    async () => ({ dashboard: { feature_previews: { ...server } } }) as never,
  )
  vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue({
    enabled: false, endpoint: '', configured_endpoint: 'https://api.typesafe.ai/v1/systemone', permits: false,
  })
})

afterEach(() => {
  cleanup()
  act(() => { __resetPreviewFlagServerSyncForTests() })
  vi.restoreAllMocks()
})

describe('Connected computers preview switch', () => {
  it('maps to the config path the backend reads', () => {
    expect(previewFlagConfigPath(PREVIEW_CONNECTED_COMPUTERS)).toBe(PATH)
  })

  it('is off on a fresh install', async () => {
    vi.spyOn(api, 'patchConfig').mockResolvedValue({ ok: true } as never)
    await renderSection()
    expect(toggle().getAttribute('aria-checked')).toBe('false')
    expect(api.patchConfig).not.toHaveBeenCalled()
  })

  it('writes true to the gateway when turned on, and false when turned off', async () => {
    const patch = vi.spyOn(api, 'patchConfig').mockImplementation(async (path: string, value: unknown) => {
      server[path.replace('dashboard.feature_previews.', '')] = value as boolean
      return { ok: true } as never
    })
    await renderSection()
    await act(async () => { toggle().click() })
    // A click is a plain two-argument write: the path and the value, nothing else.
    await waitFor(() => expect(patch).toHaveBeenCalledWith(PATH, true))
    await waitFor(() => expect(toggle().getAttribute('aria-checked')).toBe('true'))
    await act(async () => { toggle().click() })
    await waitFor(() => expect(patch).toHaveBeenLastCalledWith(PATH, false))
    await waitFor(() => expect(toggle().getAttribute('aria-checked')).toBe('false'))
  })

  it('names the switch and puts it back when the owner-only write is refused', async () => {
    vi.spyOn(api, 'patchConfig').mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    const qc = await renderSection()
    await act(async () => { toggle().click() })
    await waitFor(() => expect(screen.getByTestId('feature-previews-save-error').textContent ?? '')
      .toContain('“Connected computers” was switched back'))
    await waitFor(() => expect(toggle().getAttribute('aria-checked')).toBe('false'))
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalledTimes(2))
    await act(async () => {
      await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.fetchStatus).toBe('idle'))
    })
    expect(toggle().getAttribute('aria-checked')).toBe('false')
  })
})
