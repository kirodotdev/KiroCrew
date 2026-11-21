import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, renderHookWithProviders, createTestStore } from './helpers'
import RelayCrewPaneHost from '../components/RelayCrewPaneHost'
import { useTheme } from '../hooks/useTheme'
import { RELAY_PANE_WARM_CAP, relayPaneBridge, relayPaneRect, useRelayTabs, type RelayPaneEvent } from '../lib/relayCrewPanes'
import { openRelayCrewTab } from '../hooks/useRelayViewerCrews'
import { NavItem } from '../shell/nav/navRow'
import { localCovered, relayPaneOpened, setActiveId } from '../store/instancesSlice'

const RECT = { x: 236, y: 42, width: 700, height: 500 }

function installBridge(openAnswer: { ok: boolean; error?: string; window?: boolean } = { ok: true }, paneLoadEvents = true) {
  const listeners: Array<(e: RelayPaneEvent) => void> = []
  const api = {
    paneLoadEvents,
    open: vi.fn(async () => ({ ok: true })),
    closeAll: vi.fn(async () => undefined),
    openPane: vi.fn(async () => openAnswer),
    setPaneBounds: vi.fn(async () => undefined),
    setPaneOverlay: vi.fn(async () => undefined),
    setPaneInactive: vi.fn(async () => undefined),
    closePane: vi.fn(async () => undefined),
    onPaneEvent: vi.fn((cb: (e: RelayPaneEvent) => void) => {
      listeners.push(cb)
      return () => { listeners.splice(listeners.indexOf(cb), 1) }
    }),
  }
  ;(window as unknown as { relayCrewAPI?: unknown }).relayCrewAPI = api
  const emit = (e: RelayPaneEvent) => act(() => { for (const cb of [...listeners]) cb(e) })
  return { api, emit }
}

function mountMain() {
  const main = document.createElement('main')
  main.id = 'main-content'
  main.getBoundingClientRect = () => RECT as DOMRect
  document.body.appendChild(main)
  return main
}

function storeWith(ids: string[], active: string | null) {
  const store = createTestStore()
  for (const id of ids) store.dispatch(relayPaneOpened({ id, name: id.toUpperCase() }))
  if (active) store.dispatch(relayPaneOpened({ id: active, name: active.toUpperCase() }))
  return store
}

beforeEach(() => {
  class RO { observe() {} disconnect() {} }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = RO
})

afterEach(() => {
  delete (window as unknown as { relayCrewAPI?: unknown }).relayCrewAPI
  document.getElementById('main-content')?.remove()
  document.querySelectorAll('[data-test-overlay]').forEach(el => el.remove())
  vi.restoreAllMocks()
})

describe('relay Crew panes in the main window', () => {
  it('a shell without the pane calls has no pane bridge, and Open still opens a window', async () => {
    const api = { open: vi.fn(async () => ({ ok: true })), closeAll: vi.fn(async () => undefined) }
    ;(window as unknown as { relayCrewAPI?: unknown }).relayCrewAPI = api
    expect(relayPaneBridge()).toBeNull()
    const store = createTestStore()
    expect(await openRelayCrewTab('r1', 'One', store.dispatch)).toEqual({ ok: true })
    expect(api.open).toHaveBeenCalledWith('r1')
    expect(store.getState().instances.relayTabs).toBeUndefined()
  })

  it('with the pane calls, Open puts the pane over <main> and makes its tab active; a refusal adds no tab', async () => {
    mountMain()
    const { api } = installBridge()
    const store = createTestStore()
    expect(await openRelayCrewTab('r1', 'One', store.dispatch)).toEqual({ ok: true })
    expect(api.openPane).toHaveBeenCalledWith('r1', RECT, { width: window.innerWidth, height: window.innerHeight })
    expect(api.open).not.toHaveBeenCalled()
    expect(store.getState().instances.relayTabs).toEqual([{ id: 'r1', name: 'One' }])
    expect(store.getState().instances.relayActive).toBe('r1')
    api.openPane.mockResolvedValueOnce({ ok: false, error: 'CREW_OFFLINE' })
    expect(await openRelayCrewTab('r2', 'Two', store.dispatch)).toEqual({ ok: false, error: 'CREW_OFFLINE' })
    expect(store.getState().instances.relayTabs).toEqual([{ id: 'r1', name: 'One' }])
  })

  it('the host shows the active pane at <main>, hides the others, and hides it under a menu outside <main>', async () => {
    const main = mountMain()
    const { api } = installBridge()
    renderWithProviders(<RelayCrewPaneHost />, { store: storeWith(['r1', 'r2'], 'r2') })
    await waitFor(() => expect(api.setPaneInactive).toHaveBeenCalledWith('r2', false))
    expect(api.setPaneInactive).toHaveBeenCalledWith('r1', true)
    expect(api.setPaneBounds).toHaveBeenCalledWith('r2', RECT, { width: window.innerWidth, height: window.innerHeight })
    expect(api.setPaneOverlay).toHaveBeenLastCalledWith('r2', false)
    const list = document.createElement('div')
    list.setAttribute('role', 'listbox')
    main.appendChild(list)
    await new Promise(r => setTimeout(r, 0))
    expect(api.setPaneOverlay).toHaveBeenCalledTimes(1)
    const menu = document.createElement('div')
    menu.setAttribute('data-test-overlay', '')
    menu.setAttribute('role', 'menu')
    document.body.appendChild(menu)
    await waitFor(() => expect(api.setPaneOverlay).toHaveBeenLastCalledWith('r2', true))
    menu.remove()
    await waitFor(() => expect(api.setPaneOverlay).toHaveBeenLastCalledWith('r2', false))
  })

  it('a relay-only overlay outside <main> (the open notification sheet) hides the pane; an empty banner region does not', async () => {
    mountMain()
    const { api } = installBridge()
    renderWithProviders(<RelayCrewPaneHost />, { store: storeWith(['r1'], 'r1') })
    await waitFor(() => expect(api.setPaneOverlay).toHaveBeenLastCalledWith('r1', false))
    const region = document.createElement('div')
    region.setAttribute('data-test-overlay', '')
    region.setAttribute('role', 'status')
    region.setAttribute('data-testid', 'notification-banner-region')
    document.body.appendChild(region)
    await new Promise(r => setTimeout(r, 0))
    expect(api.setPaneOverlay).toHaveBeenCalledTimes(1)
    const sheet = document.createElement('div')
    sheet.setAttribute('data-test-overlay', '')
    document.body.appendChild(sheet)
    sheet.setAttribute('data-relay-pane-overlay', '')
    await waitFor(() => expect(api.setPaneOverlay).toHaveBeenLastCalledWith('r1', true))
    sheet.remove()
    await waitFor(() => expect(api.setPaneOverlay).toHaveBeenLastCalledWith('r1', false))
  })

  it('the local top bar takes the mode of the relay Crew in front, unsaved, and drops it on Local (row 69)', async () => {
    const { emit } = installBridge()
    const setTitleBarOverlayTheme = vi.fn()
    const setThemeAccent = vi.fn()
    ;(window as unknown as { electronAPI?: unknown }).electronAPI = { setTitleBarOverlayTheme, setThemeAccent }
    document.documentElement.style.setProperty('--accent', '#123456')
    onTestFinished(() => {
      delete (window as unknown as { electronAPI?: unknown }).electronAPI
      document.documentElement.style.removeProperty('--accent')
    })
    mountMain()
    const store = storeWith(['r1'], 'r1')
    const Probe = () => <span data-testid="theme-probe">{useTheme().theme}</span>
    renderWithProviders(<><RelayCrewPaneHost /><Probe /></>, { store })
    const own = document.documentElement.dataset.mode
    const flip = own === 'dark' ? false : true
    // Before its page has drawn, the pane's colour is the boot page's: ignored.
    emit({ type: 'theme', endpointId: 'r1', index: -1, dark: flip })
    expect(document.documentElement.dataset.mode).toBe(own)
    await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) })
    setThemeAccent.mockClear()
    emit({ type: 'loaded', endpointId: 'r1', index: -1 })
    await waitFor(() => expect(document.documentElement.dataset.mode).toBe(flip ? 'dark' : 'light'))
    // Every reader of the context's mode (top-bar art) follows too, the Windows caption buttons included.
    await waitFor(() => expect(setTitleBarOverlayTheme).toHaveBeenLastCalledWith(flip ? 'dark' : 'light'))
    expect(screen.getByTestId('theme-probe').textContent).toBe(flip ? 'dark' : 'light')
    // The shell saves the accent for the next launch's splash: not the borrowed mode's.
    await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) })
    expect(setThemeAccent).not.toHaveBeenCalled()
    expect(localStorage.getItem('mc-theme')).toBeNull()
    act(() => { store.dispatch(setActiveId(null)) })
    await waitFor(() => expect(document.documentElement.dataset.mode).toBe(own))
    expect(setTitleBarOverlayTheme).toHaveBeenLastCalledWith(own)
    act(() => { store.dispatch(relayPaneOpened({ id: 'r1', name: 'R1' })) })
    await waitFor(() => expect(document.documentElement.dataset.mode).toBe(flip ? 'dark' : 'light'))
    emit({ type: 'closed', endpointId: 'r1', index: -1 })
    await waitFor(() => expect(document.documentElement.dataset.mode).toBe(own))
  })

  it('with a Scheduled preference the shell is never handed the borrowed mode (row 69)', async () => {
    localStorage.setItem('mc-theme', 'schedule')
    onTestFinished(() => { localStorage.removeItem('mc-theme') })
    const setThemeMode = vi.fn()
    ;(window as unknown as { electronAPI?: unknown }).electronAPI = { setThemeMode }
    onTestFinished(() => { delete (window as unknown as { electronAPI?: unknown }).electronAPI })
    const { emit } = installBridge()
    mountMain()
    const store = storeWith(['r1'], 'r1')
    renderWithProviders(<RelayCrewPaneHost />, { store })
    await waitFor(() => expect(document.documentElement.dataset.modePref).toBe('schedule'))
    const own = document.documentElement.dataset.mode as 'dark' | 'light'
    const borrowed = own === 'dark' ? 'light' : 'dark'
    emit({ type: 'loaded', endpointId: 'r1', index: -1 })
    emit({ type: 'theme', endpointId: 'r1', index: -1, dark: borrowed === 'dark' })
    await waitFor(() => expect(document.documentElement.dataset.mode).toBe(borrowed))
    // The shell reads data-mode only under a Scheduled preference; it is told the own mode instead.
    expect(document.documentElement.dataset.modePref).toBe(own)
    expect(setThemeMode).not.toHaveBeenCalledWith(borrowed)
    act(() => { store.dispatch(setActiveId(null)) })
    await waitFor(() => expect(document.documentElement.dataset.modePref).toBe('schedule'))
  })

  it('shows the Crew name and Connecting… from the click until its page loads, and nothing after a refusal (row 73)', async () => {
    let answer: (r: { ok: boolean; error?: string }) => void = () => {}
    const { api, emit } = installBridge()
    api.openPane.mockImplementation(() => new Promise(r => { answer = r }))
    mountMain()
    const store = createTestStore()
    renderWithProviders(<RelayCrewPaneHost />, { store })
    expect(screen.queryByTestId('relay-crew-connecting')).toBeNull()
    let opened: Promise<unknown> = Promise.resolve()
    act(() => { opened = openRelayCrewTab('r1', 'Team Lab', store.dispatch) })
    const cover = await screen.findByTestId('relay-crew-connecting')
    expect(cover.textContent).toContain('Team Lab')
    // It fills its parent, which App.tsx makes exactly <main>'s box (not the docked terminal's).
    expect(cover.className).toContain('absolute inset-0')
    expect(cover.textContent).toContain('Connecting…')
    await act(async () => { answer({ ok: true }); await opened })
    expect(store.getState().instances.relayActive).toBe('r1')
    expect(screen.getByTestId('relay-crew-connecting').textContent).toContain('Team Lab')
    emit({ type: 'loaded', endpointId: 'r1', index: -1 })
    await waitFor(() => expect(screen.queryByTestId('relay-crew-connecting')).toBeNull())
    act(() => { opened = openRelayCrewTab('r2', 'Other', store.dispatch) })
    expect((await screen.findByTestId('relay-crew-connecting')).textContent).toContain('Other')
    await act(async () => { answer({ ok: false, error: 'CREW_OFFLINE' }); await opened })
    expect(screen.queryByTestId('relay-crew-connecting')).toBeNull()
    expect(store.getState().instances.relayActive).toBe('r1')
  })

  it('with a shell that sends no load event, the cover goes with the answer, not with a load that never comes', async () => {
    installBridge({ ok: true }, false)
    mountMain()
    const store = createTestStore()
    renderWithProviders(<RelayCrewPaneHost />, { store })
    await act(async () => { await openRelayCrewTab('r1', 'Team Lab', store.dispatch) })
    expect(store.getState().instances.relayActive).toBe('r1')
    expect(screen.queryByTestId('relay-crew-connecting')).toBeNull()
  })

  it('past the warm cap the least recent hidden pane closes and its tab goes with it, so no tab lacks a pane', async () => {
    mountMain()
    const { api, emit } = installBridge()
    const ids = Array.from({ length: RELAY_PANE_WARM_CAP + 1 }, (_, i) => `r${i + 1}`)
    const store = storeWith(ids, 'r1')
    renderWithProviders(<RelayCrewPaneHost />, { store })
    // Opened in order, then r1 again: r2 is the least recent hidden pane.
    await waitFor(() => expect(api.closePane).toHaveBeenCalledWith('r2'))
    expect(api.closePane).toHaveBeenCalledTimes(1)
    emit({ type: 'closed', endpointId: 'r2', index: -1 })
    const { relayTabs, relayLive, relayActive } = store.getState().instances
    expect(relayTabs?.map(t => t.id)).toEqual(ids.filter(id => id !== 'r2'))
    expect([...(relayTabs ?? [])].map(t => t.id).sort()).toEqual([...(relayLive ?? [])].sort())
    expect(relayActive).toBe('r1')
    expect(api.closePane).toHaveBeenCalledTimes(1)
  })
})

describe('useRelayTabs', () => {
  it('picks a tab without a new open, and closes it through its pane', () => {
    const { api, emit } = installBridge()
    const store = storeWith(['r1', 'r2'], 'r1')
    const { result } = renderHookWithProviders(() => useRelayTabs(), { store })
    renderWithProviders(<RelayCrewPaneHost />, { store })
    act(() => result.current.select('r2'))
    expect(store.getState().instances.relayActive).toBe('r2')
    expect(api.openPane).not.toHaveBeenCalled()
    act(() => result.current.close('r1'))
    expect(api.closePane).toHaveBeenCalledWith('r1')
    expect(store.getState().instances.relayTabs?.map(t => t.id)).toEqual(['r1', 'r2'])
    emit({ type: 'closed', endpointId: 'r1', index: -1 })
    expect(store.getState().instances.relayTabs?.map(t => t.id)).toEqual(['r2'])
  })
})

/** A bridge whose openPane answers only when the test says so. */
function deferredBridge() {
  const b = installBridge()
  const waiting: Array<(a: { ok: boolean; error?: string }) => void> = []
  b.api.openPane.mockImplementation(() => new Promise(r => { waiting.push(r) }))
  return { ...b, answer: (i: number, a: { ok: boolean; error?: string } = { ok: true }) => act(async () => { waiting[i](a) }) }
}

describe('relay pane windows, SSH Crews and the local Crew', () => {
  const VIEWPORT = () => ({ width: window.innerWidth, height: window.innerHeight })

  it('an open from an SSH Crew changes nothing until its answer: a refusal leaves the SSH Crew in front', async () => {
    const store = createTestStore()
    // <main> sits in the local pane, display:none while an SSH Crew is in front (App.tsx).
    mountMain().getBoundingClientRect = () => (store.getState().instances.activeId ? new DOMRect(0, 0, 0, 0) : RECT as DOMRect)
    const { api, answer } = deferredBridge()
    act(() => { store.dispatch(setActiveId('ssh-1')) })
    const opening = openRelayCrewTab('r1', 'One', store.dispatch)
    // The pane starts hidden, so the window's rect is enough until the host reports <main>'s.
    expect(api.openPane).toHaveBeenCalledWith('r1', { x: 0, y: 0, ...VIEWPORT() }, VIEWPORT())
    expect(store.getState().instances.activeId).toBe('ssh-1')
    await answer(0, { ok: false, error: 'CREW_OFFLINE' })
    expect(await opening).toEqual({ ok: false, error: 'CREW_OFFLINE' })
    expect(store.getState().instances.activeId).toBe('ssh-1')
    expect(store.getState().instances.relayActive ?? null).toBeNull()
  })

  it('an open from an SSH Crew that lands makes the relay Crew active', async () => {
    const store = createTestStore()
    mountMain()
    installBridge()
    act(() => { store.dispatch(setActiveId('ssh-1')) })
    expect(await openRelayCrewTab('r1', 'One', store.dispatch)).toEqual({ ok: true })
    expect(store.getState().instances.activeId).toBeNull()
    expect(store.getState().instances.relayActive).toBe('r1')
  })

  it('Local picked while an open waits stays in front; the late answer is a hidden tab', async () => {
    const store = createTestStore()
    mountMain()
    const { answer } = deferredBridge()
    act(() => { store.dispatch(setActiveId('ssh-1')) })
    const opening = openRelayCrewTab('r1', 'One', store.dispatch)
    act(() => { store.dispatch(setActiveId(null)) })
    await answer(0)
    await opening
    const s = store.getState().instances
    expect(s.activeId).toBeNull()
    expect(s.relayActive ?? null).toBeNull()
    expect(s.relayTabs).toEqual([{ id: 'r1', name: 'One' }])
    expect(s.relayLive).toEqual(['r1'])
  })

  it('A then B: B is in front whichever answer lands first; a double click is one tab', async () => {
    const store = createTestStore()
    mountMain()
    const { answer } = deferredBridge()
    const a = openRelayCrewTab('ra', 'A', store.dispatch)
    const b = openRelayCrewTab('rb', 'B', store.dispatch)
    await answer(1)
    await answer(0)
    await Promise.all([a, b])
    expect(store.getState().instances.relayActive).toBe('rb')
    expect(store.getState().instances.relayLive).toEqual(['rb', 'ra'])
    const c1 = openRelayCrewTab('rc', 'C', store.dispatch)
    const c2 = openRelayCrewTab('rc', 'C', store.dispatch)
    await answer(2)
    await answer(3)
    await Promise.all([c1, c2])
    expect(store.getState().instances.relayActive).toBe('rc')
    expect(store.getState().instances.relayTabs?.map(t => t.id)).toEqual(['rb', 'ra', 'rc'])
  })

  it('a late answer past the warm cap is the one that closes, not a tab the person used', async () => {
    mountMain()
    const store = storeWith(Array.from({ length: RELAY_PANE_WARM_CAP }, (_, i) => `r${i + 1}`), 'r1')
    const { api, answer } = deferredBridge()
    renderWithProviders(<RelayCrewPaneHost />, { store })
    const opening = openRelayCrewTab('late', 'Late', store.dispatch)
    act(() => { store.dispatch(relayPaneOpened({ id: 'r2', name: 'R2' })) })
    await answer(0)
    await opening
    await waitFor(() => expect(api.closePane).toHaveBeenCalledWith('late'))
    expect(api.closePane).toHaveBeenCalledTimes(1)
    expect(store.getState().instances.relayActive).toBe('r2')
  })

  it('a tab picked while an open waits stays in front', async () => {
    const store = storeWith(['r1'], 'r1')
    mountMain()
    const { answer } = deferredBridge()
    act(() => { store.dispatch(setActiveId(null)) })
    const opening = openRelayCrewTab('r2', 'Two', store.dispatch)
    act(() => { store.dispatch(relayPaneOpened({ id: 'r1', name: 'R1' })) })
    await answer(0)
    await opening
    expect(store.getState().instances.relayActive).toBe('r1')
  })

  it('the pane also covers the local nav rail\'s column, so the remote Crew\'s own rail is the only one', () => {
    mountMain()
    const rail = document.createElement('nav')
    rail.className = 'focus-chrome-rail'
    rail.style.marginLeft = '8px'
    rail.getBoundingClientRect = () => ({ x: 8, y: 42, width: 220, height: 500, right: 228 }) as DOMRect
    document.body.appendChild(rail)
    try {
      expect(relayPaneRect()?.[0]).toEqual({ x: 0, y: RECT.y, width: RECT.x + RECT.width, height: RECT.height })
    } finally {
      rail.remove()
    }
    expect(relayPaneRect()?.[0]).toEqual(RECT)
  })

  it('a Crew already open in its own window comes forward there and keeps no tab', async () => {
    mountMain()
    installBridge({ ok: true, window: true })
    const store = storeWith(['r1', 'r2'], 'r1')
    expect(await openRelayCrewTab('r3', 'Three', store.dispatch)).toEqual({ ok: true, window: true })
    expect(store.getState().instances.relayTabs?.map(t => t.id)).toEqual(['r1', 'r2'])
    expect(store.getState().instances.relayActive).toBe('r1')
  })

  it('a nav rail row clicked over a relay tab brings the local Crew forward', () => {
    const store = storeWith(['r1'], 'r1')
    renderWithProviders(<NavItem path="/settings" label="Settings" icon={null} active={false} collapsed={false} />, { store, route: '/chat' })
    fireEvent.click(screen.getByRole('button', { name: /Settings/ }))
    expect(store.getState().instances.relayActive).toBeNull()
  })

  it('localCovered reads an SSH Crew or a relay tab in front, and setActiveId(null) clears both', () => {
    const store = storeWith(['r1'], 'r1')
    expect(localCovered(store.getState())).toBe(true)
    act(() => { store.dispatch(setActiveId(null)) })
    expect(localCovered(store.getState())).toBe(false)
    act(() => { store.dispatch(setActiveId('ssh-1')) })
    expect(localCovered(store.getState())).toBe(true)
  })
})
