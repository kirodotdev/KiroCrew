// Install-failure state must survive a renderer reload.
//
// Contract under test:
// - a FRESH mount (post-reload) whose bridge reports a lastState install
//   failure re-renders the failure card with its Retry — the recovery path
//   reloads the renderer, and without the replay the card silently vanishes
// - the replayed install failure uses the install-phase copy, which does NOT
//   advise re-checking next to the Retry button
// - a replayed 'downloaded' restores the About card but does NOT resurrect the
//   UpdateModal (interruption is reserved for live events; a reload must not
//   undo a dismissal)
// - stale transient states (a check-phase error) are NOT replayed at all
// - a live event that arrives before the replay round-trip resolves wins
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { store } from '../store'
import { AboutPanel } from '../pages/settings/AboutPanel'
import UpdateModal from '../components/UpdateModal'
import { useUpdateSubscription } from '../hooks/useUpdateSubscription'

type AnyRecord = Record<string, unknown>

/**
 * App.tsx mounts the subscription hook and UpdateModal at the root and
 * AboutPanel reads the cache the hook populates. Mounting all three here
 * reproduces the real fresh-mount wiring, so the replay is exercised
 * end-to-end instead of by poking the cache.
 */
function Host() {
  useUpdateSubscription()
  return (
    <>
      <UpdateModal />
      <AboutPanel />
    </>
  )
}

function mountFresh({ lastState = null as AnyRecord | null, onState = (() => () => {}) as (cb: (p: AnyRecord) => void) => () => void } = {}) {
  const getInfo = vi.fn().mockResolvedValue({
    version: '0.1.2',
    channel: 'stable',
    packaged: true,
    stampedChannel: 'stable',
    channelSwitchable: false,
    lastState,
  })
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
    onState,
    check: vi.fn().mockResolvedValue({ ok: true }),
    download: vi.fn().mockResolvedValue({ ok: true }),
    install: vi.fn().mockResolvedValue({ ok: true }),
    getInfo,
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <Host />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
  return { ...utils, qc, getInfo }
}

/** Let the getInfo replay promise chain settle. */
async function settleReplay() {
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
}

describe('AboutPanel install-failure replay', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: async () => ({}),
      text: async () => '',
      headers: new Headers({ 'content-type': 'application/json' }),
    }))
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })

  it('re-renders the failure card (with Retry) on a fresh mount after an install failure', async () => {
    mountFresh({
      lastState: { state: 'error', phase: 'install', code: 'unknown', version: '0.1.3', message: 'ShipIt error' },
    })

    // The card must come back exactly as it was before the reload wiped it.
    expect(await screen.findByTestId('update-card')).toBeTruthy()
    const error = screen.getByTestId('update-download-error')
    expect(error.textContent).toContain('Something went wrong while installing the update.')
    // The install-phase copy must not pair "check again" advice with Retry —
    // that was the two-conflicting-next-steps defect.
    expect(error.textContent).not.toMatch(/checking for updates again/i)
    expect(screen.getByText(/Retry/)).toBeTruthy()
  })

  it('replays a downloaded state into the About card WITHOUT resurrecting the modal', async () => {
    mountFresh({ lastState: { state: 'downloaded', version: '0.1.3', notes: '' } })
    // Restoration surface: the About card comes back.
    expect(await screen.findByTestId('update-card')).toBeTruthy()
    // Interruption surface: the modal stays shut — the user already saw (and
    // possibly dismissed) this prompt before the reload.
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('a LIVE downloaded event still opens the modal (control case)', async () => {
    let push: ((p: AnyRecord) => void) | null = null
    mountFresh({ onState: (cb) => { push = cb; return () => {} } })
    act(() => { push!({ state: 'downloaded', version: '0.1.3' }) })
    expect(await screen.findByRole('dialog')).toBeTruthy()
  })

  it('does NOT replay a stale check-phase error as current state', async () => {
    mountFresh({
      lastState: { state: 'error', phase: 'check', code: 'offline', version: '0.1.2' },
    })
    await settleReplay()
    // An offline error from before the reload replayed as live would read as
    // "the update lane is broken now" — transient states are not restored.
    expect(screen.queryByTestId('update-card')).toBeNull()
    expect(screen.queryByText(/Couldn't reach the update server/)).toBeNull()
  })

  it('a live event beats the replay: the snapshot never overwrites newer state', async () => {
    let push: ((p: AnyRecord) => void) | null = null
    const { getInfo } = mountFresh({
      lastState: { state: 'error', phase: 'install', code: 'unknown', version: '0.1.3' },
      onState: (cb) => { push = cb; return () => {} },
    })
    // The live event lands before the getInfo round-trip resolves.
    act(() => { push!({ state: 'not-available' }) })
    await settleReplay()
    expect(getInfo).toHaveBeenCalled()
    expect(screen.queryByTestId('update-card')).toBeNull()
  })

  it('an empty lastState leaves the panel in its default state', async () => {
    mountFresh({ lastState: null })
    await settleReplay()
    expect(screen.queryByTestId('update-card')).toBeNull()
    expect(screen.queryByTestId('update-download-error')).toBeNull()
  })
})
