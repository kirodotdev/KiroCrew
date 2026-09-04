/**
 * useLocalGateway — the Settings > Developer "Run a local gateway" switch.
 *
 * The hook talks to window.localGatewayAPI, injected by electron/preload.js, so
 * every test installs a fake bridge. The absent-bridge case is the one a plain
 * browser and the PWA actually hit.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useLocalGateway } from '../hooks/useLocalGateway'

type LocalGatewayAPI = {
  get(): Promise<boolean>
  set(enabled: boolean): Promise<boolean>
}

function installBridge(api: LocalGatewayAPI) {
  ;(window as unknown as { localGatewayAPI?: LocalGatewayAPI }).localGatewayAPI = api
}

afterEach(() => {
  delete (window as unknown as { localGatewayAPI?: LocalGatewayAPI }).localGatewayAPI
})

describe('useLocalGateway', () => {
  it('reports unsupported and never calls the bridge when it is absent', () => {
    const { result } = renderHook(() => useLocalGateway())
    expect(result.current.localGatewaySupported).toBe(false)
    // Calling the setter without a bridge must not throw — the UI hides the
    // control, but a stray call has to be inert rather than fatal.
    act(() => { result.current.setLocalGatewayEnabled(false) })
    expect(result.current.localGatewayEnabled).toBe(true)
  })

  it('reads the stored value on mount', async () => {
    installBridge({ get: () => Promise.resolve(false), set: vi.fn() })
    const { result } = renderHook(() => useLocalGateway())
    expect(result.current.localGatewaySupported).toBe(true)
    await waitFor(() => expect(result.current.localGatewayEnabled).toBe(false))
  })

  it('writes through the bridge and keeps the value the bridge returns', async () => {
    const set = vi.fn(() => Promise.resolve(false))
    installBridge({ get: () => Promise.resolve(true), set })
    const { result } = renderHook(() => useLocalGateway())
    await waitFor(() => expect(result.current.localGatewayEnabled).toBe(true))

    await act(async () => { result.current.setLocalGatewayEnabled(false) })
    expect(set).toHaveBeenCalledWith(false)
    await waitFor(() => expect(result.current.localGatewayEnabled).toBe(false))
  })

  it('the stored value wins over the requested one', async () => {
    // main.js is the authority on what was written, so a refused or coerced
    // write must not leave the switch showing a state the app is not in.
    installBridge({ get: () => Promise.resolve(true), set: () => Promise.resolve(true) })
    const { result } = renderHook(() => useLocalGateway())
    await waitFor(() => expect(result.current.localGatewayEnabled).toBe(true))

    await act(async () => { result.current.setLocalGatewayEnabled(false) })
    await waitFor(() => expect(result.current.localGatewayEnabled).toBe(true))
  })

  it('survives a rejecting bridge without losing its current value', async () => {
    installBridge({
      get: () => Promise.reject(new Error('ipc gone')),
      set: () => Promise.reject(new Error('ipc gone')),
    })
    const { result } = renderHook(() => useLocalGateway())
    await act(async () => { result.current.setLocalGatewayEnabled(false) })
    expect(result.current.localGatewayEnabled).toBe(false)
  })
})
