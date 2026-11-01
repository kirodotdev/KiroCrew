import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import React from 'react'
import AppPage from './AppPage'

// Every /apps/:name renders the same <AppPage> instance (the route has no key),
// so switching apps changes the param in place. The response for the app the
// user left must not land on the app they switched to.
const { getApp, navigate, params } = vi.hoisted(() => ({
  getApp: vi.fn(), navigate: vi.fn(), params: { name: 'alpha' },
}))
vi.mock('react-router-dom', () => ({
  useParams: () => params,
  useNavigate: () => navigate,
}))
vi.mock('../api/client', () => ({ api: { getApp } }))
vi.mock('../components/AppHost', () => ({
  default: ({ app }: { app: { name?: string } | null }) => (
    <div data-testid="app-host" data-app={app?.name ?? ''} />
  ),
}))
vi.mock('../components/ErrorNotice', () => ({
  default: ({ message }: { message: string }) => <div data-testid="error">{message}</div>,
}))
vi.mock('../components/ui', () => ({ Btn: () => null }))
vi.mock('../i18n/t', () => ({ i18nT: (key: string) => key }))

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const BETA = { name: 'beta', origin: 'local', manifest: { ui: { entry: 'b.mjs' } } }
const ALPHA = { name: 'alpha', origin: 'local', manifest: { ui: { entry: 'a.mjs' } } }

/** Open alpha (held on `alpha`), then switch the same instance to beta (held on `beta`). */
function openAlphaThenSwitchToBeta(alpha: Promise<unknown>, beta: Promise<unknown>) {
  getApp.mockImplementation((n: string) => (n === 'alpha' ? alpha : beta))
  const { rerender } = render(<AppPage />)
  params.name = 'beta'
  rerender(<AppPage />)
}

afterEach(() => { cleanup(); vi.clearAllMocks(); params.name = 'alpha' })

describe('AppPage switching apps', () => {
  it('a late response for the previous app does not replace the current app', async () => {
    const alpha = deferred<unknown>()
    openAlphaThenSwitchToBeta(alpha.promise, Promise.resolve(BETA))
    expect((await screen.findByTestId('app-host')).getAttribute('data-app')).toBe('beta')
    await act(async () => { alpha.resolve(ALPHA) })
    expect(screen.getByTestId('app-host').getAttribute('data-app')).toBe('beta')
  })

  it('a late builtin-redirect response for the previous app does not navigate away', async () => {
    const alpha = deferred<unknown>()
    openAlphaThenSwitchToBeta(alpha.promise, Promise.resolve(BETA))
    await screen.findByTestId('app-host')
    await act(async () => {
      alpha.resolve({ name: 'alpha', origin: 'builtin', manifest: { ui: { pages: [{ route: '/alpha-native' }] } } })
    })
    expect(navigate).not.toHaveBeenCalled()
  })

  it('a late failure for the previous app does not replace the current app with an error', async () => {
    const alpha = deferred<unknown>()
    openAlphaThenSwitchToBeta(alpha.promise, Promise.resolve(BETA))
    await screen.findByTestId('app-host')
    await act(async () => { alpha.reject(new Error('alpha gateway down')) })
    expect(screen.queryByTestId('error')).toBeNull()
    expect(screen.getByTestId('app-host').getAttribute('data-app')).toBe('beta')
  })

  it('the previous app settling while the current one loads keeps the loading state', async () => {
    const alpha = deferred<unknown>()
    const beta = deferred<unknown>()
    openAlphaThenSwitchToBeta(alpha.promise, beta.promise)
    await act(async () => { alpha.resolve(ALPHA) })
    expect(screen.queryByTestId('app-host')).toBeNull()
    expect(screen.getByText('pages.appPage.loading_app')).toBeTruthy()
    await act(async () => { beta.resolve(BETA) })
    expect(screen.getByTestId('app-host').getAttribute('data-app')).toBe('beta')
  })
})
