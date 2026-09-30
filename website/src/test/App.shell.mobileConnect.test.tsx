/**
 * The rail's "Connect your phone" owner (`shell/nav/mobileConnect.tsx`): only
 * kinds this build can draw count, the dialog is the lazy modal, and any
 * navigation — or a refresh that leaves nothing drawable — closes it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useMobileConnect, MobileConnectDialog } from '../shell/nav/mobileConnect'
import { api } from '../api/client'

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
})
