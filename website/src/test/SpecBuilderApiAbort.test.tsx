// Reads must be cancellable and writes must not be. Switching specs while a poll
// is in flight otherwise lets the older response resolve last and overwrite the
// newer one, so every read takes react-query's AbortSignal. Writes deliberately
// take none: the request has already reached the server by the time a component
// unmounts, and cancelling the client side would hide the outcome of a mutation
// that still lands.
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SettingsModal from '../apps/spec-builder/components/SettingsModal'
import { specApi } from '../apps/spec-builder/api'

/** Captured RequestInit of the last fetch, so the signal can be inspected. */
function stubFetch() {
  const spy = vi.fn(async () => ({
    ok: true,
    status: 200,
    text: async () => '{}',
    json: async () => ({}),
  }))
  vi.stubGlobal('fetch', spy as unknown as typeof fetch)
  return spy
}

const lastInit = (spy: ReturnType<typeof stubFetch>): RequestInit =>
  (spy.mock.calls[spy.mock.calls.length - 1] as unknown as [string, RequestInit])[1]

describe('spec-builder api cancellation', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('threads an AbortSignal into every read', async () => {
    const spy = stubFetch()
    const ac = new AbortController()

    await specApi.list(ac.signal)
    expect(lastInit(spy).signal).toBe(ac.signal)

    await specApi.get('login', ac.signal)
    expect(lastInit(spy).signal).toBe(ac.signal)

    await specApi.getSettings(ac.signal)
    expect(lastInit(spy).signal).toBe(ac.signal)

    await specApi.browse('/srv', ac.signal)
    expect(lastInit(spy).signal).toBe(ac.signal)
  })

  it('leaves reads uncancelled when no signal is supplied', async () => {
    const spy = stubFetch()
    await specApi.list()
    expect(lastInit(spy).signal).toBeUndefined()
  })

  it('does not make writes cancellable', async () => {
    const spy = stubFetch()

    await specApi.message('login', 'looks good')
    expect(lastInit(spy).signal).toBeUndefined()

    await specApi.execute('login')
    expect(lastInit(spy).signal).toBeUndefined()

    await specApi.stop('login')
    expect(lastInit(spy).signal).toBeUndefined()

    await specApi.remove('login')
    expect(lastInit(spy).signal).toBeUndefined()

    await specApi.saveSettings('/srv/specs', '')
    expect(lastInit(spy).signal).toBeUndefined()
  })

  it('passes the query signal down from a mounted queryFn', async () => {
    // Proves the wiring, not just the api surface: a queryFn that ignores its
    // context argument type-checks fine and silently leaves the fetch
    // uncancellable, so the assertion has to come from a real mount.
    const getSettings = vi
      .spyOn(specApi, 'getSettings')
      .mockResolvedValue({ base_path: '/srv/specs' })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <SettingsModal onClose={() => {}} setErr={() => {}} />
      </QueryClientProvider>,
    )

    await waitFor(() => expect(getSettings).toHaveBeenCalled())
    expect(getSettings.mock.calls[0][0]).toBeInstanceOf(AbortSignal)
  })
})
