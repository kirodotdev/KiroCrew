/**
 * useAppUpdates — the shared update contract (PR2 App Store split).
 *
 * LibraryPage and the Discover Updates sub-page both drive updates through
 * this one hook, so its behavior is pinned HERE, once, rather than through
 * each page's render surface:
 *
 * - recorded-source routing: a registry-sourced app navigates to the detail
 *   page (`updateApp` callback); a path-installed app updates in place via
 *   `api.updateApp`; an unknown name navigates (the detail page re-dispatches
 *   on the record it loads).
 * - per-app pending state (`updatePending`) around the in-place call.
 * - Update All: SEQUENTIAL loop with `{done,total}` progress, failure
 *   aggregation into one `failed_to_update` message, and a per-success
 *   `announceAppsChanged` once per settled batch (and per single update).
 * - re-entrance guards: `runUpdate` and a second `updateAll` are both no-ops
 *   while a batch is running.
 *
 * `i18nT` is mocked to `key {params}` so message assertions pin the KEY and
 * interpolated names, not any locale's copy.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

const { apiUpdateApp } = vi.hoisted(() => ({ apiUpdateApp: vi.fn() }))

vi.mock('../../api/client', () => ({
  api: { updateApp: (...a: unknown[]) => apiUpdateApp(...a) },
}))

vi.mock('../../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../../i18n/t')>()
  return {
    ...orig,
    i18nT: (key: string, params?: Record<string, unknown>) =>
      params ? `${key} ${JSON.stringify(params)}` : key,
  }
})

import { useAppUpdates } from './useAppUpdates'
import { announceAppsChanged as realAnnounceAppsChanged } from './useAppsData'
import type { AppsData } from './useAppsData'

/** A deferred api.updateApp call the test resolves/rejects by hand. */
type Deferred = { resolve: () => void; reject: (e: Error) => void }

/** Queue every api.updateApp call as a deferred the test settles explicitly. */
function deferUpdates(): Deferred[] {
  const deferred: Deferred[] = []
  apiUpdateApp.mockImplementation(
    () => new Promise<void>((resolve, reject) => { deferred.push({ resolve, reject }) }),
  )
  return deferred
}

// Minimal rows: the hook reads only name/displayName plus what
// `isRegistrySourced` reads (source/origin), and `updatables` is consumed as
// a name list. Casts keep the fixtures at exactly those fields.
const pathApp = {
  name: 'notes', displayName: 'Notes', source: '/home/u/apps/notes',
  origin: 'local', enabled: true, lifecycle: 'gateway',
} as unknown as AppsData['apps'][number]
const docsApp = {
  name: 'docs', displayName: 'Docs', source: '/home/u/apps/docs',
  origin: 'local', enabled: true, lifecycle: 'gateway',
} as unknown as AppsData['apps'][number]
const registryApp = {
  name: 'radar', displayName: 'Radar',
  origin: 'registry', enabled: true, lifecycle: 'gateway',
} as unknown as AppsData['apps'][number]

function setup(over: Partial<Parameters<typeof useAppUpdates>[0]> = {}) {
  const updateAppNav = vi.fn()
  const setError = vi.fn()
  const setSuccess = vi.fn()
  const announce = vi.fn()
  const utils = renderHook(() => useAppUpdates({
    apps: [pathApp, docsApp, registryApp],
    updatables: [pathApp, docsApp] as unknown as AppsData['updatables'],
    announceAppsChanged: announce,
    updateApp: updateAppNav,
    setError,
    setSuccess,
    ...over,
  }))
  return { ...utils, updateAppNav, setError, setSuccess, announce }
}

describe('useAppUpdates — recorded-source routing (runUpdate)', () => {
  beforeEach(() => { apiUpdateApp.mockReset() })

  it('updates a path-installed app in place, with per-app pending state', async () => {
    const deferred = deferUpdates()
    const { result, announce, setSuccess, setError } = setup()

    expect(result.current.updatePending).toBeNull()
    act(() => { void result.current.runUpdate('notes') })

    // Pending is set for THIS app while the call is in flight.
    expect(result.current.updatePending).toBe('notes')
    expect(apiUpdateApp).toHaveBeenCalledExactlyOnceWith('notes')

    await act(async () => { deferred[0].resolve() })
    await waitFor(() => expect(result.current.updatePending).toBeNull())

    expect(announce).toHaveBeenCalledTimes(1)
    expect(setSuccess).toHaveBeenCalledWith(
      expect.stringContaining('pages.appsPage.synced_from_its_source_directory'),
    )
    expect(setSuccess).toHaveBeenCalledWith(expect.stringContaining('Notes'))
    // setError('') clears the notice on start; no failure message follows.
    expect(setError).toHaveBeenCalledExactlyOnceWith('')
  })

  it('routes a registry-sourced app to the detail page instead of updating in place', () => {
    const { result, updateAppNav } = setup()
    act(() => { void result.current.runUpdate('radar') })
    expect(updateAppNav).toHaveBeenCalledExactlyOnceWith('radar')
    expect(apiUpdateApp).not.toHaveBeenCalled()
    expect(result.current.updatePending).toBeNull()
  })

  it('rowUpdatesInPlace updates a registry-sourced app where it stands (the worklist contract)', async () => {
    const deferred = deferUpdates()
    const { result, updateAppNav, setSuccess, announce } = setup({ rowUpdatesInPlace: true })

    act(() => { void result.current.runUpdate('radar') })
    // No navigation: the Updates worklist promises in-place updating next to
    // Update All, so the row's button must not hand off to the detail page.
    expect(updateAppNav).not.toHaveBeenCalled()
    expect(result.current.updatePending).toBe('radar')
    expect(apiUpdateApp).toHaveBeenCalledExactlyOnceWith('radar')

    await act(async () => { deferred[0].resolve() })
    await waitFor(() => expect(result.current.updatePending).toBeNull())
    expect(announce).toHaveBeenCalledTimes(1)
    // The batch path's own success wording, not the path-sync message.
    expect(setSuccess).toHaveBeenCalledWith(expect.stringContaining('pages.appsPage.updated_app'))
  })

  it('rowUpdatesInPlace still routes an unknown name to the detail page (no record to update)', () => {
    const { result, updateAppNav } = setup({ rowUpdatesInPlace: true })
    act(() => { void result.current.runUpdate('ghost') })
    expect(updateAppNav).toHaveBeenCalledExactlyOnceWith('ghost')
    expect(apiUpdateApp).not.toHaveBeenCalled()
  })

  it('a trust-denied in-place update hands the surface an UPDATE retry, not an error', async () => {
    const onTrustDenied = vi.fn()
    // Shape per TrustAppModal's errorCode reader: top-level string `code`.
    const trustError = Object.assign(new Error('app execution denied'), {
      code: 'app_execution_denied',
    })
    apiUpdateApp.mockRejectedValueOnce(trustError)
    const { result, setError, setSuccess, announce } = setup({
      rowUpdatesInPlace: true,
      onTrustDenied,
    })

    await act(async () => { await result.current.runUpdate('radar') })
    // Consent prompt, not an error banner.
    expect(setError).not.toHaveBeenCalledWith(expect.stringContaining('denied'))
    expect(onTrustDenied).toHaveBeenCalledTimes(1)
    const [name, retryUpdate] = onTrustDenied.mock.calls[0]
    expect(name).toBe('radar')

    // The handed retry re-runs THE UPDATE (the gate's default retry is the
    // enable action — resuming that after consent would start the app
    // instead of updating it) and rejects on failure, the shape
    // `useTrustGate` requires to report a failed post-grant retry.
    apiUpdateApp.mockResolvedValueOnce(undefined)
    await act(async () => { await retryUpdate('radar') })
    expect(apiUpdateApp).toHaveBeenLastCalledWith('radar')
    expect(announce).toHaveBeenCalled()
    expect(setSuccess).toHaveBeenCalledWith(expect.stringContaining('pages.appsPage.updated_app'))

    apiUpdateApp.mockRejectedValueOnce(new Error('boom'))
    await expect(retryUpdate('radar')).rejects.toThrow('boom')
  })

  it('routes an unknown name to the detail page (no recorded source to read)', () => {
    const { result, updateAppNav } = setup()
    act(() => { void result.current.runUpdate('ghost') })
    expect(updateAppNav).toHaveBeenCalledExactlyOnceWith('ghost')
    expect(apiUpdateApp).not.toHaveBeenCalled()
  })

  it('reports a failed in-place update through setError and clears pending', async () => {
    const deferred = deferUpdates()
    const { result, setError, setSuccess, announce } = setup()

    act(() => { void result.current.runUpdate('notes') })
    await act(async () => { deferred[0].reject(new Error('boom')) })
    await waitFor(() => expect(result.current.updatePending).toBeNull())

    expect(setError).toHaveBeenLastCalledWith('boom')
    expect(setSuccess).not.toHaveBeenCalled()
    expect(announce).not.toHaveBeenCalled()
  })
})

describe('useAppUpdates — Update All', () => {
  beforeEach(() => { apiUpdateApp.mockReset() })
  afterEach(() => { vi.restoreAllMocks() })

  it('runs sequentially with {done,total} progress and ONE trailing announce', async () => {
    const deferred = deferUpdates()
    const { result, announce, setSuccess } = setup()

    act(() => { void result.current.updateAll() })
    expect(result.current.updatingAll).toEqual({ done: 0, total: 2 })

    // SEQUENTIAL: only the first call has been made while it is unresolved.
    expect(apiUpdateApp).toHaveBeenCalledTimes(1)
    expect(apiUpdateApp).toHaveBeenNthCalledWith(1, 'notes')

    await act(async () => { deferred[0].resolve() })
    await waitFor(() => expect(result.current.updatingAll).toEqual({ done: 1, total: 2 }))
    // NO mid-batch announce: each announce invalidates ['registry'], and the
    // app shell holds an always-active observer on that key — a per-success
    // announce would turn an N-app batch into ~N catalog refetches.
    expect(announce).not.toHaveBeenCalled()
    await waitFor(() => expect(apiUpdateApp).toHaveBeenCalledTimes(2))
    expect(apiUpdateApp).toHaveBeenNthCalledWith(2, 'docs')

    await act(async () => { deferred[1].resolve() })
    await waitFor(() => expect(result.current.updatingAll).toBeNull())
    expect(announce).toHaveBeenCalledTimes(1)
    expect(setSuccess).toHaveBeenCalledWith(
      expect.stringContaining('pages.appsPage.updated_app'),
    )
  })

  it('aggregates failures into one message; successes still announce', async () => {
    const deferred = deferUpdates()
    const { result, announce, setError, setSuccess } = setup()

    act(() => { void result.current.updateAll() })
    await act(async () => { deferred[0].reject(new Error('nope')) })
    await waitFor(() => expect(apiUpdateApp).toHaveBeenCalledTimes(2))
    await act(async () => { deferred[1].resolve() })
    await waitFor(() => expect(result.current.updatingAll).toBeNull())

    expect(setError).toHaveBeenLastCalledWith(
      expect.stringContaining('pages.appsPage.failed_to_update'),
    )
    expect(setError).toHaveBeenLastCalledWith(expect.stringContaining('notes'))
    expect(setSuccess).not.toHaveBeenCalled()
    // Only the surviving app announced.
    expect(announce).toHaveBeenCalledTimes(1)
  })

  it('blocks runUpdate and a second updateAll while a batch is running', async () => {
    const deferred = deferUpdates()
    const { result, updateAppNav } = setup()

    act(() => { void result.current.updateAll() })
    expect(apiUpdateApp).toHaveBeenCalledTimes(1)

    // Neither path may start work mid-batch: no new api call, no navigation.
    act(() => { void result.current.runUpdate('notes') })
    act(() => { void result.current.runUpdate('radar') })
    act(() => { void result.current.updateAll() })
    expect(apiUpdateApp).toHaveBeenCalledTimes(1)
    expect(updateAppNav).not.toHaveBeenCalled()
    expect(result.current.updatePending).toBeNull()

    // Drain the batch so no dangling promise outlives the test.
    await act(async () => { deferred[0].resolve() })
    await waitFor(() => expect(apiUpdateApp).toHaveBeenCalledTimes(2))
    await act(async () => { deferred[1].resolve() })
    await waitFor(() => expect(result.current.updatingAll).toBeNull())
  })
})

describe('useAppUpdates — mc:apps-changed wiring', () => {
  beforeEach(() => { apiUpdateApp.mockReset() })

  it('a success dispatched through the real announceAppsChanged fires mc:apps-changed', async () => {
    const deferred = deferUpdates()
    const listener = vi.fn()
    window.addEventListener('mc:apps-changed', listener)
    try {
      const { result } = setup({ announceAppsChanged: realAnnounceAppsChanged })
      act(() => { void result.current.runUpdate('notes') })
      await act(async () => { deferred[0].resolve() })
      await waitFor(() => expect(result.current.updatePending).toBeNull())
      expect(listener).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('mc:apps-changed', listener)
    }
  })
})
