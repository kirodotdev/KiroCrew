/**
 * Tests for useGatewayPlatform.
 *
 * The hook reads the query the prerequisite gate owns. React Query keeps ONE
 * options object per query key, so an observer that registers a fetch-less
 * `queryFn` can decide what a client-driven refetch runs — which is why these
 * tests mount the reader AFTER the owner and then refetch through the client:
 * that is the ordering in which a reader can strand the gate's query without a
 * usable `queryFn`.
 */

import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'

vi.mock('../api/client', () => ({
  api: { kiroPrerequisite: vi.fn().mockResolvedValue({ platform: 'linux' }) },
}))

import { useConfigSetHintsV1, useGatewayPlatform } from '../hooks/useGatewayPlatform'
import { api } from '../api/client'

const PREREQUISITE_KEY = ['kiro-prerequisite']

const makeClient = (): QueryClient =>
  new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false },
    },
  })

const wrapperFor = (qc: QueryClient): React.FC<{ children: React.ReactNode }> =>
  ({ children }): JSX.Element => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

/** Stands in for the prerequisite gate: the owner of the query. */
const GateOwner = ({ queryFn }: { queryFn: () => Promise<unknown> }): JSX.Element => {
  const { data } = useQuery({ queryKey: PREREQUISITE_KEY, queryFn })
  const platform = (data as { platform?: string } | undefined)?.platform
  return <div>owner:{platform ?? 'pending'}</div>
}

const PlatformReader = (): JSX.Element => <div>reader:{useGatewayPlatform()}</div>

/** Keeps the owner mounted across rerenders so only the reader is newly added. */
const Harness = ({
  queryFn,
  withReader,
}: {
  queryFn: () => Promise<unknown>
  withReader: boolean
}): JSX.Element => (
  <>
    <GateOwner queryFn={queryFn} />
    {withReader ? <PlatformReader /> : null}
  </>
)

afterEach(() => {
  cleanup()
  vi.mocked(api.kiroPrerequisite).mockClear()
})

describe('useGatewayPlatform', () => {
  it('leaves the shared query fetchable when the reader mounts after the gate', async () => {
    const qc = makeClient()
    const queryFn = vi.fn().mockResolvedValue({ platform: 'darwin' })
    const view = render(<Harness queryFn={queryFn} withReader={false} />, {
      wrapper: wrapperFor(qc),
    })
    await waitFor(() => expect(screen.getByText('owner:darwin')).toBeTruthy())

    view.rerender(<Harness queryFn={queryFn} withReader />)
    await waitFor(() => expect(screen.getByText('reader:darwin')).toBeTruthy())

    await act(async () => {
      await qc.refetchQueries({ queryKey: PREREQUISITE_KEY })
    })

    expect(qc.getQueryState(PREREQUISITE_KEY)?.error).toBeNull()
    expect(qc.getQueryState(PREREQUISITE_KEY)?.status).toBe('success')
  })

  it('holds the cached status against collection while only the reader is mounted', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    render(<PlatformReader />, { wrapper: wrapperFor(qc) })

    act(() => {
      qc.setQueryData(PREREQUISITE_KEY, { platform: 'darwin' })
    })

    await waitFor(() => expect(screen.getByText('reader:darwin')).toBeTruthy())
  })

  it('never fetches on its own', async () => {
    const qc = makeClient()
    render(<PlatformReader />, { wrapper: wrapperFor(qc) })

    await act(async () => {
      await qc.refetchQueries({ queryKey: PREREQUISITE_KEY })
    })

    expect(api.kiroPrerequisite).not.toHaveBeenCalled()
  })

  it('re-renders when the cached status changes', async () => {
    const qc = makeClient()
    render(<PlatformReader />, { wrapper: wrapperFor(qc) })
    expect(screen.getByText('reader:other')).toBeTruthy()

    act(() => {
      qc.setQueryData(PREREQUISITE_KEY, { platform: 'win32' })
    })

    await waitFor(() => expect(screen.getByText('reader:windows')).toBeTruthy())
  })

  it('resolves to generic wording in a tree with no QueryClientProvider', () => {
    render(<PlatformReader />)

    expect(screen.getByText('reader:other')).toBeTruthy()
  })
})

const V1_HINTS = {
  version: 1,
  encoding: 'utf-8-base64',
  shells: [
    { id: 'powershell', prefix: "& 'C:\\Kiro Crew\\bin\\kirocrew.cmd'" },
    { id: 'cmd', prefix: '"C:\\Kiro Crew\\bin\\kirocrew.cmd"' },
  ],
}

const HintsReader = (): JSX.Element => {
  const hints = useConfigSetHintsV1()
  return <div data-testid="hints">{hints ? JSON.stringify(hints) : 'none'}</div>
}

/** Seeds the gate's cache with a status carrying `hints` and mounts only the reader. */
const renderHints = async (hints: unknown, qc = makeClient()): Promise<QueryClient> => {
  render(<HintsReader />, { wrapper: wrapperFor(qc) })
  act(() => {
    qc.setQueryData(PREREQUISITE_KEY, { platform: 'Windows', config_set_hints: hints })
  })
  return qc
}

describe('useConfigSetHintsV1', () => {
  it('returns a valid V1 payload from the cached status without fetching', async () => {
    await renderHints(V1_HINTS)

    await waitFor(() =>
      expect(JSON.parse(screen.getByTestId('hints').textContent ?? '')).toEqual(V1_HINTS),
    )
    expect(api.kiroPrerequisite).not.toHaveBeenCalled()
  })

  it('is undefined before the gate has loaded and on a gateway that sends no hints', async () => {
    const qc = makeClient()
    render(<HintsReader />, { wrapper: wrapperFor(qc) })
    expect(screen.getByTestId('hints').textContent).toBe('none')

    act(() => {
      qc.setQueryData(PREREQUISITE_KEY, { platform: 'Windows' })
    })
    await waitFor(() => expect(qc.getQueryData(PREREQUISITE_KEY)).toBeTruthy())
    expect(screen.getByTestId('hints').textContent).toBe('none')
  })

  it.each([
    ['a future version', { ...V1_HINTS, version: 2 }],
    ['another encoding', { ...V1_HINTS, encoding: 'base64url' }],
    ['no shells', { ...V1_HINTS, shells: [] }],
    ['shells that are not a list', { ...V1_HINTS, shells: 'powershell' }],
    ['an unknown shell id', { ...V1_HINTS, shells: [{ id: 'fish', prefix: 'kirocrew' }] }],
    ['a duplicate shell id', { ...V1_HINTS, shells: [V1_HINTS.shells[0], V1_HINTS.shells[0]] }],
    ['an empty prefix', { ...V1_HINTS, shells: [{ id: 'posix', prefix: '   ' }] }],
    ['a non-string prefix', { ...V1_HINTS, shells: [{ id: 'posix', prefix: 7 }] }],
    ['a multi-line prefix', { ...V1_HINTS, shells: [{ id: 'posix', prefix: 'kirocrew\nrm -rf x' }] }],
    ['a row that is not an object', { ...V1_HINTS, shells: ['posix'] }],
    ['a payload that is not an object', 'powershell'],
  ])('reads %s as absent', async (_name, hints) => {
    await renderHints(hints)

    await waitFor(() => expect(screen.getByTestId('hints').textContent).toBe('none'))
  })

  it('does not change what useGatewayPlatform returns', async () => {
    const qc = makeClient()
    render(
      <>
        <PlatformReader />
        <HintsReader />
      </>,
      { wrapper: wrapperFor(qc) },
    )
    act(() => {
      qc.setQueryData(PREREQUISITE_KEY, { platform: 'Windows', config_set_hints: V1_HINTS })
    })

    await waitFor(() => expect(screen.getByText('reader:windows')).toBeTruthy())
    expect(typeof useGatewayPlatform).toBe('function')
  })
})
