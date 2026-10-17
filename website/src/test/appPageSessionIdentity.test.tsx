import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import AppPage from '../pages/AppPage'

const { getApp, navigate } = vi.hoisted(() => ({ getApp: vi.fn(), navigate: vi.fn() }))
vi.mock('react-router-dom', () => ({
  useParams: () => ({ name: 'sample' }),
  useNavigate: () => navigate,
}))
vi.mock('../api/client', () => ({ api: { getApp } }))
vi.mock('../components/AppHost', () => ({
  default: ({ sessionKey }: { sessionKey?: string }) => (
    <div data-testid="app-host" data-session-key={sessionKey} />
  ),
}))
vi.mock('../components/ErrorNotice', () => ({ default: () => null }))
vi.mock('../components/ui', () => ({ Btn: () => null }))
vi.mock('../i18n/t', () => ({ i18nT: (key: string) => key }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('routed app session identity', () => {
  it('keeps native builtin redirects outside AppHost', async () => {
    getApp.mockResolvedValueOnce({
      name: 'sample', origin: 'builtin',
      manifest: { ui: { pages: [{ route: '/sample-native' }] } },
    })
    render(<AppPage />)
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/sample-native', { replace: true }))
    expect(screen.queryByTestId('app-host')).toBeNull()
  })

  it.each(['local', 'builtin'])('binds a %s bundled page to the dashboard-page identity', async origin => {
    getApp.mockResolvedValueOnce({
      name: 'sample', origin, manifest: { ui: { entry: 'index.mjs' } },
    })
    render(<AppPage />)
    const host = await screen.findByTestId('app-host')
    expect(host.getAttribute('data-session-key')).toBe('dashboard:ui')
    expect(getApp).toHaveBeenCalledWith('sample')
    expect(navigate).not.toHaveBeenCalled()
  })
})
