import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError } from '../api/apiError'
import BuiltinAppRoute from './BuiltinAppRoute'

// A former built-in uninstalled after its release shows the not-installed state
// at its old route. Installed again from somewhere else, the gateway announces
// the new files with app_reload (`mc:app-reload`), and that state has the shell
// re-read the installed-apps list, whose new record then replaces it.
const { getApp, installed } = vi.hoisted(() => ({
  getApp: vi.fn(),
  installed: { apps: [] as Array<{ name: string }> },
}))
vi.mock('react-router-dom', () => ({
  useParams: () => ({ builtinApp: 'shop' }),
  Navigate: () => null,
}))
vi.mock('../api/client', () => ({ api: { getApp } }))
vi.mock('./builtinRegistry', () => ({
  getBuiltinApp: () => ({ appId: 'shop', component: () => <div data-testid="compiled-page" /> }),
  builtinRefusalReason: () => null,
}))
vi.mock('../hooks/panelTabRegistry', () => ({
  useInstalledAppsWhen: () => ({ apps: installed.apps, settled: true, loadError: null, retry: () => {} }),
}))
vi.mock('../components/InstalledAppSurface', () => ({
  default: ({ app }: { app: { name?: string } | null }) => (
    <div data-testid="app-surface" data-app={app?.name ?? ''} />
  ),
}))

const released = () => new ApiError(404, 'released', JSON.stringify({ error: 'released', code: 'app_released' }))
const announce = (app: string) => window.dispatchEvent(new CustomEvent('mc:app-reload', { detail: { app } }))

afterEach(() => { cleanup(); vi.resetAllMocks(); installed.apps = [] })

function renderRoute() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <BuiltinAppRoute />
    </QueryClientProvider>,
  )
}

describe('BuiltinAppRoute not-installed state when the app is installed elsewhere', () => {
  it('has the shell re-read its list when app_reload names the app', async () => {
    getApp.mockRejectedValue(released())
    renderRoute()
    expect((await screen.findByTestId('app-surface')).getAttribute('data-app')).toBe('')
    const appsChanged = vi.fn()
    window.addEventListener('mc:apps-changed', appsChanged)
    try {
      await act(async () => { announce('other') })
      expect(appsChanged).not.toHaveBeenCalled()
      await act(async () => { announce('shop') })
      expect(appsChanged).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('mc:apps-changed', appsChanged)
    }
  })

  it('does not listen while the compiled page shows', async () => {
    installed.apps = [{ name: 'shop' }]
    renderRoute()
    await screen.findByTestId('compiled-page')
    const appsChanged = vi.fn()
    window.addEventListener('mc:apps-changed', appsChanged)
    try {
      await act(async () => { announce('shop') })
      expect(appsChanged).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('mc:apps-changed', appsChanged)
    }
  })
})
