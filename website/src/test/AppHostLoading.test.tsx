/**
 * AppHost's Suspense fallback, end to end through the host: a bundle that lands
 * inside the delay window paints no placeholder at all, a slow one shows the
 * placeholder after the delay, and the shape comes from `manifest.ui.loading`.
 *
 * The bundle import is the one seam stood in (`appBundleImport`), so the test
 * decides when it settles. `AppHostCoverage.test.tsx` keeps the import real.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { renderWithProviders } from './helpers'
import type { AppHostProps } from '../components/AppHost'
import { APP_LOADING_DELAY_MS } from '../components/AppLoadingFallback'

vi.mock('../app-sdk', () => ({
  AppApiProvider: (props: { children: ReactNode }) => <div data-testid="sdk-provider">{props.children}</div>,
}))

const bundle = vi.hoisted(() => ({
  resolve: null as null | ((m: { default: () => JSX.Element }) => void),
  calls: [] as string[],
}))

vi.mock('../components/appBundleImport', () => ({
  importAppBundle: (path: string) => {
    bundle.calls.push(path)
    return new Promise(res => { bundle.resolve = res })
  },
}))

import AppHost from '../components/AppHost'

function app(loading?: unknown, hasLoading = true): AppHostProps['app'] {
  const ui: { entry: string; loading?: unknown } = { entry: 'index.mjs' }
  if (hasLoading) ui.loading = loading
  return { name: 'ledger-lens', displayName: 'Ledger Lens', enabled: true, manifest: { ui } }
}

async function land() {
  await act(async () => {
    bundle.resolve?.({ default: () => <div data-testid="the-app">app body</div> })
    // React.lazy re-renders a few microtasks after the import settles; no
    // timers are involved, so draining the microtask queue is enough.
    for (let i = 0; i < 5; i++) await Promise.resolve()
  })
}

/** Ids of the fallback's delay timers, so a test can check exactly those were cleared. */
function trackDelayTimers() {
  const ids: unknown[] = []
  const realSet = window.setTimeout
  const setSpy = vi.spyOn(window, 'setTimeout').mockImplementation(((fn: () => void, ms?: number, ...rest: unknown[]) => {
    const id = realSet(fn, ms, ...rest)
    if (ms === APP_LOADING_DELAY_MS) ids.push(id)
    return id
  }) as typeof window.setTimeout)
  const clearSpy = vi.spyOn(window, 'clearTimeout')
  return {
    ids,
    cleared: () => ids.filter(id => clearSpy.mock.calls.some(c => c[0] === id)),
    restore: () => { setSpy.mockRestore(); clearSpy.mockRestore() },
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  bundle.resolve = null
  bundle.calls = []
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('AppHost — delayed loading fallback', () => {
  it('a load that lands inside the window never shows a placeholder', async () => {
    renderWithProviders(<AppHost app={app(undefined, false)} />)
    expect(bundle.calls).toEqual(['/apps/ledger-lens/ui/index.mjs'])
    expect(screen.queryByTestId('app-loading-fallback')).toBeNull()

    act(() => { vi.advanceTimersByTime(APP_LOADING_DELAY_MS - 50) })
    expect(screen.queryByTestId('app-loading-fallback')).toBeNull()

    await land()
    expect(screen.getByTestId('the-app')).toBeTruthy()
    // The window closing afterwards paints nothing either: the fallback is gone.
    act(() => { vi.advanceTimersByTime(APP_LOADING_DELAY_MS) })
    expect(screen.queryByTestId('app-loading-fallback')).toBeNull()
  })

  it('a fast load clears the delay timer it never needed', async () => {
    const t = trackDelayTimers()
    renderWithProviders(<AppHost app={app(undefined, false)} />)
    expect(t.ids.length).toBeGreaterThan(0)
    await land()
    expect(screen.getByTestId('the-app')).toBeTruthy()
    expect(t.cleared()).toEqual(t.ids)
    t.restore()
  })

  it('a slow load shows the default page skeleton after the delay, then the app', async () => {
    renderWithProviders(<AppHost app={app(undefined, false)} />)
    expect(screen.queryByTestId('app-loading-fallback')).toBeNull()

    act(() => { vi.advanceTimersByTime(APP_LOADING_DELAY_MS) })
    const fb = screen.getByTestId('app-loading-fallback')
    expect(fb.dataset.layout).toBe('page')
    expect(fb.textContent).toContain('Loading Ledger Lens')

    await land()
    expect(screen.getByTestId('the-app')).toBeTruthy()
    expect(screen.queryByTestId('app-loading-fallback')).toBeNull()
  })

  it.each(['rail', 'centered', 'split', 'page'] as const)('uses the manifest layout %s', (layout) => {
    renderWithProviders(<AppHost app={app({ layout })} />)
    act(() => { vi.advanceTimersByTime(APP_LOADING_DELAY_MS) })
    expect(screen.getByTestId('app-loading-fallback').dataset.layout).toBe(layout)
  })

  it.each([
    ['loading: null', null],
    ['a non-object', 'rail'],
    ['a wrong-case layout', { layout: 'Split' }],
    ['an unknown layout', { layout: 'mosaic' }],
  ])('falls back to page for %s', (_label, loading) => {
    renderWithProviders(<AppHost app={app(loading)} />)
    act(() => { vi.advanceTimersByTime(APP_LOADING_DELAY_MS) })
    expect(screen.getByTestId('app-loading-fallback').dataset.layout).toBe('page')
  })

  it('a side-panel tab (entry override) keeps the default shape, not the page layout', () => {
    renderWithProviders(<AppHost app={app({ layout: 'rail' })} entry="panel.mjs" />)
    expect(bundle.calls).toEqual(['/apps/ledger-lens/ui/panel.mjs'])
    act(() => { vi.advanceTimersByTime(APP_LOADING_DELAY_MS) })
    expect(screen.getByTestId('app-loading-fallback').dataset.layout).toBe('page')
  })

  it('leaving the page mid-load clears the pending timer', () => {
    const t = trackDelayTimers()
    const { unmount } = renderWithProviders(<AppHost app={app({ layout: 'rail' })} />)
    expect(t.ids.length).toBeGreaterThan(0)
    expect(t.cleared()).toEqual([])
    unmount()
    expect(t.cleared()).toEqual(t.ids)
    t.restore()
  })
})
