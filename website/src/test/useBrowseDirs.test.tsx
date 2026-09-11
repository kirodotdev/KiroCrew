import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { api } from '../api/client'
import { useBrowseDirs } from '../components/useBrowseDirs'

type Dirs = Awaited<ReturnType<typeof api.browseDirs>>

const dirs = (...names: string[]): Dirs =>
  ({ dirs: names.map(n => '/root/' + n) }) as unknown as Dirs

/**
 * The shared client leaves `refetchOnWindowFocus` on and retries once, so a bare default client
 * would let those defaults decide these assertions instead of the hook's own options.
 */
let client: QueryClient
function wrap({ children }: { children: React.ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

beforeEach(() => {
  vi.restoreAllMocks()
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

const mount = (open = true, onData = vi.fn()) => ({
  onData,
  ...renderHook(({ o }: { o: boolean }) => useBrowseDirs(o, onData),
    { wrapper: wrap, initialProps: { o: open } }),
})

describe('useBrowseDirs delivery', () => {
  it('serves a reopened drill the current directory, not the rows it showed last time', async () => {
    const spy = vi.spyOn(api, 'browseDirs')
      .mockResolvedValueOnce(dirs('alpha'))
      .mockResolvedValueOnce(dirs('alpha', 'brand-new'))
    const { onData, result, rerender } = mount()

    act(() => { void result.current.browse('/root') })
    await waitFor(() => expect(onData).toHaveBeenCalledTimes(1))
    expect(onData.mock.calls[0][0]).toEqual(dirs('alpha'))

    // Closing and reopening must re-READ: `gcTime: 0` means there is no retained entry to replay,
    // so a directory created between the two opens is visible.
    rerender({ o: false })
    rerender({ o: true })
    await waitFor(() => expect(onData).toHaveBeenCalledTimes(2))
    expect(onData.mock.calls[1][0]).toEqual(dirs('alpha', 'brand-new'))
    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('drops a superseded drill rather than delivering its rows over the later one', async () => {
    let releaseSlow: (d: Dirs) => void = () => {}
    vi.spyOn(api, 'browseDirs').mockImplementation((p?: string) =>
      (p === '/root/slow'
        ? new Promise<Dirs>(r => { releaseSlow = r })
        : Promise.resolve(dirs('second'))) as ReturnType<typeof api.browseDirs>)
    const { onData, result } = mount()

    act(() => { void result.current.browse('/root/slow') })
    act(() => { void result.current.browse('/root/second') })
    await waitFor(() => expect(onData).toHaveBeenCalledTimes(1))
    expect(onData.mock.calls[0][0]).toEqual(dirs('second'))

    // The slow drill lands LAST. Its key is no longer the observed one, so it must not deliver.
    await act(async () => { releaseSlow(dirs('slow')); await Promise.resolve() })
    expect(onData).toHaveBeenCalledTimes(1)
  })

  it('drops a superseded drill that FAILS instead of painting a notice over newer rows', async () => {
    let rejectSlow: (e: Error) => void = () => {}
    vi.spyOn(api, 'browseDirs').mockImplementation((p?: string) =>
      (p === '/root/slow'
        ? new Promise<Dirs>((_, rej) => { rejectSlow = rej })
        : Promise.resolve(dirs('second'))) as ReturnType<typeof api.browseDirs>)
    const { result } = mount()

    act(() => { void result.current.browse('/root/slow') })
    act(() => { void result.current.browse('/root/second') })
    await waitFor(() => expect(result.current.listError).toBe(false))

    await act(async () => { rejectSlow(new Error('too late')); await Promise.resolve() })
    expect(result.current.listError).toBe(false)
  })

  it('retry re-reads the path that failed, with no successful drill to fall back on', async () => {
    const spy = vi.spyOn(api, 'browseDirs')
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce(dirs('recovered'))
    const { onData, result } = mount()

    // A SUBDIRECTORY: browsing the root would leave the remembered path empty, so a retry that
    // forgot it would read the same thing by accident and the test could not tell.
    act(() => { void result.current.browse('/root/deep') })
    await waitFor(() => expect(result.current.listError).toBe('failed'))
    expect(onData).not.toHaveBeenCalled()

    await act(async () => { await result.current.retry() })
    await waitFor(() => expect(result.current.listError).toBe(false))
    expect(spy.mock.calls[1][0]).toBe('/root/deep')
    await waitFor(() => expect(onData).toHaveBeenCalledTimes(1))
  })

  it('abandons a drill in flight when the picker closes, and aborts the request', async () => {
    const spy = vi.spyOn(api, 'browseDirs')
      .mockReturnValue(new Promise<Dirs>(() => {}) as ReturnType<typeof api.browseDirs>)
    const { onData, result, rerender } = mount()

    act(() => { void result.current.browse('/root/closing') })
    await waitFor(() => expect(spy).toHaveBeenCalled())
    const signal = spy.mock.calls[0][1] as AbortSignal | undefined
    expect(signal?.aborted).toBe(false)

    rerender({ o: false })
    // Not merely ignored on arrival: cancelling aborts the signal, so the request is dropped.
    await waitFor(() => expect(signal?.aborted).toBe(true))
    expect(onData).not.toHaveBeenCalled()
  })

  it('names a deadline failure apart from any other failure', async () => {
    vi.spyOn(api, 'browseDirs').mockRejectedValue(
      Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }))
    const { result } = mount()
    act(() => { void result.current.browse('/root') })
    await waitFor(() => expect(result.current.listError).toBe('timed_out'))
  })
})
