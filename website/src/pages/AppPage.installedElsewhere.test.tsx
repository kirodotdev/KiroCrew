import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import React from 'react'
import AppPage from './AppPage'

// An open "not installed" page must notice the app being installed from
// somewhere else (the CLI, another tab). The gateway announces the new files
// with the app_reload frame, re-dispatched as `mc:app-reload`.
const { getApp, navigate } = vi.hoisted(() => ({ getApp: vi.fn(), navigate: vi.fn() }))
vi.mock('react-router-dom', () => ({
  useParams: () => ({ name: 'alpha' }),
  useNavigate: () => navigate,
}))
vi.mock('../api/client', () => ({ api: { getApp } }))
vi.mock('../components/InstalledAppSurface', () => ({
  default: ({ app }: { app: { name?: string } | null }) => (
    <div data-testid="app-surface" data-app={app?.name ?? ''} />
  ),
}))
vi.mock('../components/ErrorNotice', () => ({
  default: ({ message }: { message: string }) => <div data-testid="error">{message}</div>,
}))

const ALPHA = { name: 'alpha', origin: 'local', enabled: false, manifest: { ui: { entry: 'a.mjs' } } }
const notFound = () => Object.assign(new Error('not found'), { status: 404 })
const announce = (app: string) => window.dispatchEvent(new CustomEvent('mc:app-reload', { detail: { app } }))

afterEach(() => { cleanup(); vi.resetAllMocks() })

async function openNotInstalled() {
  getApp.mockRejectedValueOnce(notFound())
  render(<AppPage />)
  const surface = await screen.findByTestId('app-surface')
  expect(surface.getAttribute('data-app')).toBe('')
  return surface
}

describe('AppPage when the app is installed elsewhere', () => {
  it('re-reads the record and the shell list when app_reload names this app', async () => {
    await openNotInstalled()
    const appsChanged = vi.fn()
    window.addEventListener('mc:apps-changed', appsChanged)
    try {
      getApp.mockResolvedValueOnce(ALPHA)
      await act(async () => { announce('alpha') })
      expect((await screen.findByTestId('app-surface')).getAttribute('data-app')).toBe('alpha')
      expect(getApp).toHaveBeenCalledTimes(2)
      expect(appsChanged).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('mc:apps-changed', appsChanged)
    }
  })

  it('ignores an announcement for another app', async () => {
    await openNotInstalled()
    const appsChanged = vi.fn()
    window.addEventListener('mc:apps-changed', appsChanged)
    try {
      await act(async () => { announce('beta') })
      expect(getApp).toHaveBeenCalledTimes(1)
      expect(appsChanged).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('mc:apps-changed', appsChanged)
    }
  })

  it('leaves a page that shows the app to the host, which reloads the window itself', async () => {
    getApp.mockResolvedValueOnce(ALPHA)
    render(<AppPage />)
    expect((await screen.findByTestId('app-surface')).getAttribute('data-app')).toBe('alpha')
    await act(async () => { announce('alpha') })
    expect(getApp).toHaveBeenCalledTimes(1)
  })
})
