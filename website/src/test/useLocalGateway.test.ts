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
    await waitFor(() => expect(result.current.localGatewaySupported).toBe(true))
    expect(result.current.localGatewayEnabled).toBe(false)
  })

  it('keeps the switch hidden until a read succeeds', () => {
    // A connection window's read is refused; until main.js answers, nothing
    // says this window may show the switch, so it is never painted and pulled.
    installBridge({ get: () => new Promise<boolean>(() => {}), set: vi.fn() })
    const { result } = renderHook(() => useLocalGateway())
    expect(result.current.localGatewaySupported).toBe(false)
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

  it('hides the switch when main.js refuses the read, as it does for a connection window', async () => {
    const read = Promise.reject(new Error("local-gateway:get is restricted to the main window's dashboard"))
    installBridge({ get: () => read, set: vi.fn() })
    const { result } = renderHook(() => useLocalGateway())
    // Wait for the refused read itself before asserting it left the switch hidden.
    await act(async () => { await read.catch(() => {}) })
    expect(result.current.localGatewaySupported).toBe(false)
  })

  it('puts the switch back when main.js refuses the write', async () => {
    installBridge({
      get: () => Promise.resolve(true),
      set: () => Promise.reject(new Error("local-gateway:set is restricted to the main window's dashboard")),
    })
    const { result } = renderHook(() => useLocalGateway())
    await waitFor(() => expect(result.current.localGatewayEnabled).toBe(true))

    await act(async () => { result.current.setLocalGatewayEnabled(false) })
    await waitFor(() => expect(result.current.localGatewayEnabled).toBe(true))
  })
})
