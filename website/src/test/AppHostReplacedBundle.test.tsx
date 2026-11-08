/**
 * AppHost after the gateway replaced an app's files while no host showed it.
 *
 * The browser keeps every module it imported for the life of the document, so
 * importing the same bundle URL again returns the old module. A host that
 * mounts an app whose bundle this document already imported, after an
 * `mc:app-reload` for that app, reloads the window instead of importing again.
 * `AppHostCoverage.test.tsx` pins the reload while a host is mounted.
 *
 * The bundle import is stood in (`appBundleImport`) so the test can count the
 * imports and settle each one at once. Each case uses its own app name: what the
 * document imported lasts as long as the module does, as it does in the browser.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { renderWithProviders } from './helpers'
import type { AppHostProps } from '../components/AppHost'

vi.mock('../app-sdk', () => ({
  AppApiProvider: (props: { children: ReactNode }) => <div>{props.children}</div>,
}))

const bundle = vi.hoisted(() => ({ calls: [] as string[] }))

vi.mock('../components/appBundleImport', () => ({
  importAppBundle: (path: string) => {
    bundle.calls.push(path)
    return Promise.resolve({ default: () => <div data-testid="the-app">app body</div> })
  },
}))

import AppHost from '../components/AppHost'

// The app body renders through AppHost's React.lazy (LazyApp) over the
// appBundleImport stub, so a find waits for that lazy to settle and commit.
const LAZY_APP_TIMEOUT_MS = 5000

function app(name: string): AppHostProps['app'] {
  return { name, displayName: name, enabled: true, manifest: { ui: { entry: 'index.mjs' } } }
}

/** What the dashboard socket dispatches for an `app_reload` frame. */
function announce(name: string) {
  act(() => { window.dispatchEvent(new CustomEvent('mc:app-reload', { detail: { app: name } })) })
}

const origReload = window.location.reload
let reloadSpy: ReturnType<typeof vi.fn>

beforeEach(() => {
  bundle.calls = []
  reloadSpy = vi.fn()
  Object.defineProperty(window.location, 'reload', { configurable: true, value: reloadSpy })
})

afterEach(() => {
  Object.defineProperty(window.location, 'reload', { configurable: true, value: origReload })
})

describe('AppHost: a bundle replaced while no host showed it', () => {
  it('reloads the window on the next mount instead of importing the old module again', async () => {
    const first = renderWithProviders(<AppHost app={app('ledger-lens')} />)
    expect(await screen.findByTestId('the-app', undefined, { timeout: LAZY_APP_TIMEOUT_MS })).toBeTruthy()
    first.unmount()

    announce('ledger-lens')
    expect(reloadSpy).not.toHaveBeenCalled()

    renderWithProviders(<AppHost app={app('ledger-lens')} />)
    expect(reloadSpy).toHaveBeenCalledTimes(1)
    expect(bundle.calls).toEqual(['/apps/ledger-lens/ui/index.mjs'])
    expect(screen.queryByTestId('the-app')).toBeNull()
  })

  it('imports an app this document never loaded, whatever was announced for it', async () => {
    announce('fresh-app')

    renderWithProviders(<AppHost app={app('fresh-app')} />)
    expect(await screen.findByTestId('the-app', undefined, { timeout: LAZY_APP_TIMEOUT_MS })).toBeTruthy()
    expect(reloadSpy).not.toHaveBeenCalled()
    expect(bundle.calls).toEqual(['/apps/fresh-app/ui/index.mjs'])
  })

  it('remounts normally when the announcement named another app', async () => {
    const first = renderWithProviders(<AppHost app={app('notes')} />)
    expect(await screen.findByTestId('the-app', undefined, { timeout: LAZY_APP_TIMEOUT_MS })).toBeTruthy()
    first.unmount()

    announce('other-app')

    renderWithProviders(<AppHost app={app('notes')} />)
    expect(await screen.findByTestId('the-app', undefined, { timeout: LAZY_APP_TIMEOUT_MS })).toBeTruthy()
    expect(reloadSpy).not.toHaveBeenCalled()
  })
})
