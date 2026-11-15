/**
 * `permissions.memory` arrives from the manifest parser as `str()` of the
 * app.json value, so a JSON `false` / `null` is the text "False" / "None". The
 * stored text is part of the app's signed payload and stays; the detail page
 * reads those as no request instead of showing "Memory: False".
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { memoryRequested } from '../components/appstore/memoryAccessLabel'

const getApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: vi.fn().mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } }),
    system: vi.fn().mockResolvedValue({ hostname: '' }),
  },
}))
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'

describe('memoryRequested', () => {
  it('reads the str() form of a JSON false or null as no request', () => {
    for (const v of ['False', 'None', 'false', '', '  ', undefined, null, false, 0]) {
      expect(memoryRequested(v)).toBe(false)
    }
  })

  it('keeps every declared request', () => {
    for (const v of ['app-scoped', 'shared', 'True', true, 'custom']) {
      expect(memoryRequested(v)).toBe(true)
    }
  })
})

describe('AppDetailPage — memory row', () => {
  function renderWith(memory: unknown) {
    getApp.mockResolvedValue({
      name: 'notes', displayName: 'Notes', version: '1.0.0', enabled: true, origin: 'local',
      resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Notes', description: 'Takes notes.', version: '1.0.0', permissions: { network: true, memory } },
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps/detail/notes']}>
          <Routes><Route path="/apps/detail/:name" element={<AppDetailPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  it('shows no memory row for "False"', async () => {
    renderWith('False')
    await waitFor(() => expect(screen.getByText('Network: yes')).toBeInTheDocument())
    expect(screen.queryByText(/Memory/)).not.toBeInTheDocument()
  })

  it('still shows a declared memory request', async () => {
    renderWith('shared')
    await waitFor(() => expect(screen.getByText('Network: yes')).toBeInTheDocument())
    expect(screen.getByText(/Memory/)).toBeInTheDocument()
  })
})
