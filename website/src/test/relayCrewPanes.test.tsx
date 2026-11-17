import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, renderHookWithProviders, createTestStore } from './helpers'
import RelayCrewPaneHost from '../components/RelayCrewPaneHost'
import { RELAY_PANE_WARM_CAP, relayPaneBridge, useRelayTabs, type RelayPaneEvent } from '../lib/relayCrewPanes'
import { openRelayCrewTab } from '../hooks/useRelayViewerCrews'
import { NavItem } from '../shell/nav/navRow'
import { RelayReconnectingView } from '../shell/RelayReconnectingView'
import { localCovered, relayPaneOpened, setActiveId } from '../store/instancesSlice'

const RECT = { x: 236, y: 42, width: 700, height: 500 }

function installBridge(openAnswer: { ok: boolean; error?: string; window?: boolean } = { ok: true }) {
  const listeners: Array<(e: RelayPaneEvent) => void> = []
  const api = {
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

describe('relay Crew panes', () => {
  it('a shell without the pane calls has no pane bridge, and Open still opens a window', async () => {
    const open = vi.fn(async () => ({ ok: true }))
    ;(window as unknown as { relayCrewAPI?: unknown }).relayCrewAPI = { open, closeAll: vi.fn(async () => undefined) }
    expect(relayPaneBridge()).toBeNull()
    const store = createTestStore()
    expect(await openRelayCrewTab('r1', 'One', store.dispatch)).toEqual({ ok: true })
    expect(open).toHaveBeenCalledWith('r1')
    expect(store.getState().instances.relayTabs).toBeUndefined()
  })

  it('with no key set, Open puts the pane over <main> and makes its tab active; a refusal adds no tab', async () => {
    mountMain()
    const { api } = installBridge()
    expect(localStorage.getItem('mc-relay-crew-pane')).toBeNull()
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

  it('the host-offline cover, outside <main>, hides the pane so the cover shows', async () => {
    mountMain()
    const { api } = installBridge()
    renderWithProviders(<><RelayCrewPaneHost /><RelayReconnectingView cover="host" /></>, { store: storeWith(['r1'], 'r1') })
    await waitFor(() => expect(api.setPaneOverlay).toHaveBeenLastCalledWith('r1', true))
  })

  it('past the warm cap the least recent hidden pane closes and its tab goes with it, so no tab lacks a pane', async () => {
    mountMain()
    const { api, emit } = installBridge()
    const ids = ['r1', 'r2', 'r3', 'r4'].slice(0, RELAY_PANE_WARM_CAP + 1)
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

describe('relay pane windows, SSH Crews and the local Crew', () => {
  it('Open over an SSH Crew brings the local Crew forward first and opens the pane at <main>\'s real rect', async () => {
    const store = createTestStore()
    const main = mountMain()
    // <main> sits in the local pane, display:none while an SSH Crew is in front (App.tsx).
    main.getBoundingClientRect = () => (store.getState().instances.activeId ? new DOMRect(0, 0, 0, 0) : RECT as DOMRect)
    const { api } = installBridge()
    act(() => { store.dispatch(setActiveId('ssh-1')) })
    expect(await openRelayCrewTab('r1', 'One', store.dispatch)).toEqual({ ok: true })
    expect(api.openPane).toHaveBeenCalledWith('r1', RECT, { width: window.innerWidth, height: window.innerHeight })
    expect(store.getState().instances.activeId).toBeNull()
    expect(store.getState().instances.relayActive).toBe('r1')
  })

  it.each([
    ['a refusal', { ok: false, error: 'CREW_OFFLINE' }],
    ['a window answer', { ok: true, window: true }],
  ])('an Open over an SSH Crew that ends in %s puts the SSH Crew back', async (_label, answer) => {
    const store = createTestStore()
    mountMain()
    const { api } = installBridge(answer)
    act(() => { store.dispatch(setActiveId('ssh-1')) })
    expect(await openRelayCrewTab('r1', 'One', store.dispatch)).toEqual(answer)
    expect(api.openPane).toHaveBeenCalledTimes(1)
    expect(store.getState().instances.activeId).toBe('ssh-1')
    expect(store.getState().instances.relayTabs ?? []).toEqual([])
  })

  it('Open in another dashboard window asks main for the tab in the main window, and adds no tab here', async () => {
    mountMain()
    const { api } = installBridge({ ok: false, error: 'NOT_MAIN_WINDOW' })
    const openInMain = vi.fn(async () => ({ ok: true }))
    Object.assign(api, { openInMain })
    const store = createTestStore()
    expect(await openRelayCrewTab('r1', 'One', store.dispatch)).toEqual({ ok: true })
    expect(openInMain).toHaveBeenCalledWith('r1')
    expect(api.open).not.toHaveBeenCalled()
    expect(store.getState().instances.relayTabs ?? []).toEqual([])
  })

  it('a <main> with no size opens no pane', async () => {
    mountMain().getBoundingClientRect = () => new DOMRect(0, 0, 0, 0)
    const { api } = installBridge()
    const store = createTestStore()
    expect(await openRelayCrewTab('r1', 'One', store.dispatch)).toEqual({ ok: false, error: 'UNAVAILABLE' })
    expect(api.openPane).not.toHaveBeenCalled()
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
