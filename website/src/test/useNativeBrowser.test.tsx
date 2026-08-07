import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

import { useNativeBrowser } from '../hooks/useNativeBrowser'

// The hook talks to the preload bridge (window.browserAPI) and observes layout.
// jsdom has neither, so both are stubbed. These tests assert the CONTRACT with
// the main process — which rect/viewport gets reported, when the view is hidden,
// when it is released, and that everything is scoped to the right panel — not any
// rendered pixels (the native view paints outside the DOM entirely, so there is
// nothing in jsdom to assert about visually).

const PANEL = 'chat-1'

interface Call {
  panelId?: string
  rect?: unknown
  viewport?: unknown
}

function installBridge() {
  const calls = {
    setBounds: [] as Call[],
    overlay: [] as Array<{ panelId: string; active: boolean }>,
    close: [] as string[],
    inactive: [] as Array<{ panelId: string; value: boolean }>,
    open: [] as Array<{ panelId: string; url: string }>,
    agentAct: [] as Array<{ panelId: string; enabled: boolean }>,
    owner: [] as Array<{ panelId: string; owner: string }>,
  }
  const state = { open: false, visible: false, url: '', bounds: null }
  const api = {
    open: vi.fn(async (panelId: string, url: string) => {
      calls.open.push({ panelId, url })
      return { ...state, open: true, url }
    }),
    navigate: vi.fn(async (_panelId: string, url: string) => ({ ...state, open: true, url })),
    setBounds: vi.fn(async (panelId: string, rect: unknown, viewport: unknown) => {
      calls.setBounds.push({ panelId, rect, viewport })
      return { ...state }
    }),
    setOverlayActive: vi.fn(async (panelId: string, active: boolean) => {
      calls.overlay.push({ panelId, active })
      return { ...state, overlayActive: active }
    }),
    close: vi.fn(async (panelId: string) => {
      calls.close.push(panelId)
      return { ...state, open: false }
    }),
    setInactive: vi.fn(async (panelId: string, value: boolean) => {
      calls.inactive.push({ panelId, value })
      return { ...state, inactive: value }
    }),
    getState: vi.fn(async () => ({ ...state })),
    setAgentAct: vi.fn(async (panelId: string, enabled: boolean) => {
      calls.agentAct.push({ panelId, enabled })
      return { ok: true }
    }),
    setControlOwner: vi.fn(async (panelId: string, owner: string) => {
      calls.owner.push({ panelId, owner })
      return { owner, changed: true }
    }),
    onAgentOpened: vi.fn(() => () => {}),
    onDidNavigate: vi.fn(() => () => {}),
    onTitleUpdated: vi.fn(() => () => {}),
  }
  ;(window as unknown as { browserAPI?: unknown }).browserAPI = api
  return { api, calls, state }
}

beforeEach(() => {
  class RO {
    constructor(private cb: () => void) {}
    observe() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = RO
})

afterEach(() => {
  delete (window as unknown as { browserAPI?: unknown }).browserAPI
  vi.restoreAllMocks()
})

describe('useNativeBrowser', () => {
  it('reports unavailable when the preload bridge is absent (plain browser)', () => {
    delete (window as unknown as { browserAPI?: unknown }).browserAPI
    const { result } = renderHook(() => useNativeBrowser(PANEL, true))
    expect(result.current.available).toBe(false)
  })

  it('reports unavailable without a panel id, so nothing is sent unscoped', () => {
    installBridge()
    const { result } = renderHook(() => useNativeBrowser('', true))
    expect(result.current.available).toBe(false)
  })

  it('reports available inside the Electron shell', () => {
    installBridge()
    const { result } = renderHook(() => useNativeBrowser(PANEL, true))
    expect(result.current.available).toBe(true)
  })

  it('hides — but does NOT close — while disabled, so page state survives', async () => {
    // Regression: disabling used to call close(), destroying the WebContents.
    // Switching side-panel tabs away and back then lost unsaved form input,
    // scroll position and history.
    const { calls } = installBridge()
    renderHook(() => useNativeBrowser(PANEL, false))
    await waitFor(() => expect(calls.inactive.at(-1)).toEqual({ panelId: PANEL, value: true }))
    expect(calls.close).toHaveLength(0)
    expect(calls.setBounds).toHaveLength(0)
  })

  it('marks active again when re-enabled', async () => {
    const { calls } = installBridge()
    const { rerender } = renderHook(({ on }) => useNativeBrowser(PANEL, on), {
      initialProps: { on: false },
    })
    await waitFor(() => expect(calls.inactive.at(-1)?.value).toBe(true))
    rerender({ on: true })
    await waitFor(() => expect(calls.inactive.at(-1)?.value).toBe(false))
    expect(calls.close).toHaveLength(0)
  })

  it('closes the view on unmount, so a hidden browser does not leak', async () => {
    const { calls } = installBridge()
    const { unmount } = renderHook(() => useNativeBrowser(PANEL, true))
    expect(calls.close).toHaveLength(0)
    unmount()
    await waitFor(() => expect(calls.close).toEqual([PANEL]))
  })

  it('reports the measured rect together with the viewport size and panel id', async () => {
    const { calls } = installBridge()
    const { result } = renderHook(() => useNativeBrowser(PANEL, true))

    const host = document.createElement('div')
    document.body.appendChild(host)
    host.getBoundingClientRect = () =>
      ({ x: 656, y: 48, width: 368, height: 640 }) as DOMRect
    act(() => {
      result.current.hostRef.current = host
    })
    act(() => {
      result.current.report()
    })

    await waitFor(() => expect(calls.setBounds.length).toBeGreaterThan(0))
    const last = calls.setBounds.at(-1)!
    expect(last.panelId).toBe(PANEL)
    expect(last.rect).toEqual({ x: 656, y: 48, width: 368, height: 640 })
    // The viewport travels alongside so the main process can derive zoom scale.
    expect(last.viewport).toEqual({ width: window.innerWidth, height: window.innerHeight })
  })

  it('hides the native view while an SPA dialog is open, and restores after', async () => {
    const { calls } = installBridge()
    renderHook(() => useNativeBrowser(PANEL, true))
    await waitFor(() => expect(calls.overlay).toEqual([{ panelId: PANEL, active: false }]))

    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    await act(async () => {
      document.body.appendChild(dialog)
    })
    await waitFor(() => expect(calls.overlay.at(-1)?.active).toBe(true))

    await act(async () => {
      dialog.remove()
    })
    await waitFor(() => expect(calls.overlay.at(-1)?.active).toBe(false))
  })

  it('never leaves the view hidden when unmounting with an overlay up', async () => {
    const { calls } = installBridge()
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.appendChild(dialog)

    const { unmount } = renderHook(() => useNativeBrowser(PANEL, true))
    await waitFor(() => expect(calls.overlay.at(-1)?.active).toBe(true))
    unmount()
    await waitFor(() => expect(calls.overlay.at(-1)?.active).toBe(false))
    dialog.remove()
  })

  it('open() passes the panel id and re-reports bounds', async () => {
    const { calls } = installBridge()
    const { result } = renderHook(() => useNativeBrowser(PANEL, true))
    await act(async () => {
      result.current.open('example.com')
    })
    await waitFor(() => expect(calls.open).toEqual([{ panelId: PANEL, url: 'example.com' }]))
    await waitFor(() => expect(result.current.state?.open).toBe(true))
  })
})

describe('useNativeBrowser control handoff', () => {
  it('mirrors the agent-act authorization into the main process', async () => {
    const { calls } = installBridge()
    const { rerender } = renderHook(
      ({ act: a }) => useNativeBrowser(PANEL, true, { agentActEnabled: a }),
      { initialProps: { act: false } },
    )
    await waitFor(() => expect(calls.agentAct).toEqual([{ panelId: PANEL, enabled: false }]))
    rerender({ act: true })
    await waitFor(() => expect(calls.agentAct.at(-1)?.enabled).toBe(true))
  })

  it('acquires LIGHT (in-process) control once a native page EXISTS, regardless of the Globe', async () => {
    const { calls } = installBridge()
    const { result } = renderHook(() => useNativeBrowser(PANEL, true, { agentActEnabled: false }))
    // No page yet → no agent owner to hold.
    await waitFor(() => expect(calls.owner.at(-1)?.owner).toBe('none'))
    // A page opens → request LIGHT even with the Globe OFF: ownership is "who
    // holds the debugger", which is needed to READ the page (snapshot /
    // screenshot). What may be DONE with it is gated per op class in the main
    // process. Never PLAYWRIGHT (that owner is a separate external browser).
    act(() => result.current.open('https://example.com'))
    await waitFor(() => expect(calls.owner.at(-1)?.owner).toBe('light'))
    expect(calls.owner.some(c => c.owner === 'playwright')).toBe(false)
  })

  it('keeps LIGHT when the Globe turns off — revocation is enforced per op, not by detaching', async () => {
    // Detaching on Globe-off was the regression that forced a plain "open this
    // page" back onto the mirror transport: view-class reads need the debugger.
    // operate-class ops are refused at the main-process gate instead.
    const { calls } = installBridge()
    const { result, rerender } = renderHook(
      ({ act: a }) => useNativeBrowser(PANEL, true, { agentActEnabled: a }),
      { initialProps: { act: true } },
    )
    act(() => result.current.open('https://example.com'))
    await waitFor(() => expect(calls.owner.at(-1)?.owner).toBe('light'))
    rerender({ act: false })
    await waitFor(() => expect(calls.agentAct.at(-1)?.enabled).toBe(false))
    expect(calls.owner.at(-1)?.owner).toBe('light')
  })

  it('drives the control owner even while the panel is disabled (hidden, not destroyed)', async () => {
    // Control ownership is orthogonal to visibility: a hidden-but-alive view the
    // agent is mid-task on must keep LIGHT. So the owner effect must NOT be gated
    // on `enabled` — otherwise glancing at another side-panel tab would drop the
    // agent's hold on the page.
    const { calls } = installBridge()
    const { result } = renderHook(() => useNativeBrowser(PANEL, false, { agentActEnabled: true }))
    act(() => result.current.open('https://example.com'))
    await waitFor(() => expect(calls.owner.at(-1)?.owner).toBe('light'))
  })
})

describe('useNativeBrowser panel isolation', () => {
  it('two panels never clobber each other\u2019s agent-act authorization', async () => {
    // Regression: agent-act and the control owner used to live in ONE
    // per-window slot, so a second session with a Browser panel overwrote the
    // first session's authorization (last writer wins).
    const { calls } = installBridge()
    renderHook(() => useNativeBrowser('chat-a', true, { agentActEnabled: true }))
    renderHook(() => useNativeBrowser('chat-b', true, { agentActEnabled: false }))

    await waitFor(() => expect(calls.agentAct.length).toBeGreaterThanOrEqual(2))
    const a = calls.agentAct.filter(c => c.panelId === 'chat-a')
    const b = calls.agentAct.filter(c => c.panelId === 'chat-b')
    expect(a.at(-1)?.enabled).toBe(true)
    expect(b.at(-1)?.enabled).toBe(false)
  })

  it('each panel reports its own bounds under its own id', async () => {
    const { calls } = installBridge()
    const mk = (id: string, x: number) => {
      const { result } = renderHook(() => useNativeBrowser(id, true))
      const host = document.createElement('div')
      document.body.appendChild(host)
      host.getBoundingClientRect = () => ({ x, y: 0, width: 100, height: 100 }) as DOMRect
      act(() => { result.current.hostRef.current = host })
      act(() => { result.current.report() })
      return result
    }
    mk('chat-a', 10)
    mk('chat-b', 20)
    await waitFor(() => expect(calls.setBounds.length).toBeGreaterThanOrEqual(2))
    const a = calls.setBounds.filter(c => c.panelId === 'chat-a').at(-1)
    const b = calls.setBounds.filter(c => c.panelId === 'chat-b').at(-1)
    expect((a?.rect as { x: number }).x).toBe(10)
    expect((b?.rect as { x: number }).x).toBe(20)
  })
})
