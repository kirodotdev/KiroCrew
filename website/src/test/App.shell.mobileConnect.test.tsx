/**
 * The rail's "Connect your phone" owner (`components/mobileConnect.tsx`): only
 * kinds this build can draw count, the dialog is the lazy modal, and any
 * navigation — or a refresh that leaves nothing drawable — closes it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useMobileConnect, MobileConnectDialog } from '../components/mobileConnect'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { refreshOnce, __resetRefreshOnceForTests } from '../api/refreshOnce'

vi.mock('../components/MobileConnectModal', () => ({
  default: ({ kinds, onClose }: { kinds: string[]; onClose: () => void }) => (
    <button type="button" data-testid="mobile-connect-modal" onClick={onClose}>{kinds.join(',')}</button>
  ),
}))

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

describe('useMobileConnect', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('counts only drawable kinds, opens the lazy dialog, and closes it on navigation', async () => {
    vi.spyOn(api, 'mobileConnectMethods').mockResolvedValue({ methods: [{ id: 'a', kind: 'login_link' }, { id: 'b', kind: 'no-such-renderer' }] } as never)
    let key = 'k1'
    const { result, rerender } = renderHook(() => useMobileConnect(key), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.hasRenderableMobileConnect).toBe(true))
    expect(result.current.mobileConnectKinds).toEqual(['login_link'])
    act(() => result.current.setMobileConnectOpen(true))
    const view = render(<MobileConnectDialog mobileConnect={result.current} />)
    expect(await screen.findByTestId('mobile-connect-modal')).toHaveTextContent('login_link')
    act(() => screen.getByTestId('mobile-connect-modal').click())
    expect(result.current.mobileConnectOpen).toBe(false)
    act(() => result.current.setMobileConnectOpen(true))
    key = 'k2'
    rerender()
    expect(result.current.mobileConnectOpen).toBe(false)
    view.unmount()
  })

  it('a refresh that leaves nothing drawable closes an open dialog, and the dialog then renders nothing', async () => {
    const methods = vi.spyOn(api, 'mobileConnectMethods').mockResolvedValue({ methods: [{ id: 'a', kind: 'login_link' }] } as never)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useMobileConnect('k'), {
      wrapper: ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
    })
    await waitFor(() => expect(result.current.hasRenderableMobileConnect).toBe(true))
    act(() => result.current.setMobileConnectOpen(true))
    methods.mockResolvedValue({ methods: [{ id: 'b', kind: 'no-such-renderer' }] } as never)
    await act(() => client.invalidateQueries({ queryKey: ['mobile-connect-methods'] }))
    await waitFor(() => expect(result.current.hasRenderableMobileConnect).toBe(false))
    expect(result.current.mobileConnectOpen).toBe(false)
    const { container } = render(<MobileConnectDialog mobileConnect={{ ...result.current, mobileConnectOpen: true }} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('a teammate gets no row: pairing a phone is the machine owner\'s', async () => {
    vi.spyOn(api, 'mobileConnectMethods').mockResolvedValue({ methods: [{ id: 'a', kind: 'login_link' }] } as never)
    vi.spyOn(api, 'viewerOwnerOk').mockResolvedValue(false)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useMobileConnect('k'), {
      wrapper: ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
    })
    // Both reads settled: the methods would draw a row, and the owner check said no.
    await waitFor(() => expect(result.current.mobileConnectQuery.isSuccess).toBe(true))
    await waitFor(() => expect(client.isFetching()).toBe(0))
    expect(result.current.hasRenderableMobileConnect).toBe(false)
    expect(result.current.mobileConnectKinds).toEqual([])
  })

  it('the owner keeps the row', async () => {
    vi.spyOn(api, 'mobileConnectMethods').mockResolvedValue({ methods: [{ id: 'a', kind: 'login_link' }] } as never)
    vi.spyOn(api, 'viewerOwnerOk').mockResolvedValue(true)
    const { result } = renderHook(() => useMobileConnect('k'), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.hasRenderableMobileConnect).toBe(true))
  })
})

describe('useMobileConnect, a first read refused because the sign-in ran out', () => {
  // The spy an earlier test left on `api.mobileConnectMethods` would carry its calls into the counts below.
  beforeEach(() => { vi.restoreAllMocks() })
  afterEach(() => {
    vi.unstubAllGlobals()
    __resetRefreshOnceForTests()
    vi.restoreAllMocks()
  })

  it('reads the methods once more after the renewal that refusal started has answered', async () => {
    let renewed: (status: number) => void = () => {}
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { renewed = status => resolve(new Response('{}', { status })) })))
    const methods = vi.spyOn(api, 'mobileConnectMethods')
      // What `checkSessionExpired` does on that 403: start the renewal, hand the refusal back.
      .mockImplementationOnce(() => { void refreshOnce(); return Promise.reject(new ApiError(403, 'Token required', '', true)) })
      .mockResolvedValue({ methods: [{ id: 'a', kind: 'login_link' }] } as never)
    // The owner check reads through `fetch` too; it must not take the renewal's answer.
    vi.spyOn(api, 'viewerOwnerOk').mockResolvedValue(true)
    const { result } = renderHook(() => useMobileConnect('k'), { wrapper: wrapper() })
    await waitFor(() => expect(methods).toHaveBeenCalledTimes(1))
    await act(async () => {})
    // Held: a second read sent now would carry the same lapsed sign-in.
    expect(methods).toHaveBeenCalledTimes(1)

    await act(async () => { renewed(200) })
    await waitFor(() => expect(result.current.hasRenderableMobileConnect).toBe(true))
    expect(methods).toHaveBeenCalledTimes(2)
  })
})
