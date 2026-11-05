/**
 * `useCrewModeSwitch` keeps a pending switch per chat: a press in a second chat
 * while the first chat's switch is still in flight must not drop the first
 * chat's composer hold.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'
import { setCrewModeCatalog } from '../../../lib/crewMode'
import type { ChatSlot } from '../../../types'
import { useCrewModeSwitch } from './crewModeSwitch'
import { crewModePreviewOn, turnOnCrewModePreview } from '../../../test/crewModePreview'
import { __resetPanelTabs, dropPanelViews, openPanelView, type ViewKind } from '../../../hooks/usePanelTabs'
import { closeActivityPanel, openActivityPanel } from '../../../store/chatSlice'

const mobile = vi.hoisted(() => ({ value: false }))
vi.mock('../../../hooks/useIsMobile', () => ({ useIsMobile: () => mobile.value }))
vi.mock('../../../hooks/usePanelTabs', async importOriginal => ({ ...await importOriginal<object>(), dropPanelViews: vi.fn() }))

const plain = (key: string, agent = 'default'): ChatSlot => ({ key, agent, agent_kind: 'template', memory_mode: 'persistent', origin: 'user' } as unknown as ChatSlot)

function setup(openView: (kind: ViewKind) => void = () => {}) {
  const pending: Array<{ resolve: (ok?: boolean) => void }> = []
  const switchAgent = vi.fn((_agent: string) => new Promise<boolean>(resolve => { pending.push({ resolve: (ok = true) => resolve(ok) }) }))
  const store = createTestStore()
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const hook = renderHook(
    ({ slot }: { slot: ChatSlot }) => useCrewModeSwitch({ currentSlot: slot, defaultAgent: 'default', hasUserMessage: false, switchAgent, openView }),
    { wrapper, initialProps: { slot: plain('a') } },
  )
  return { hook, pending, switchAgent, store }
}

describe('useCrewModeSwitch pending switches', () => {
  beforeEach(() => setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']))
  afterEach(() => setCrewModeCatalog(undefined, undefined))

  it("keeps chat A held when chat B turns Crew Mode on while A's switch is in flight", async () => {
    const { hook, pending } = setup()
    act(() => { hook.result.current.turnOnCrewMode() })
    expect(hook.result.current.crewModeSwitching).toBe('on')
    // Move to chat B and press there too; A's switch has not landed.
    hook.rerender({ slot: plain('b') })
    expect(hook.result.current.crewModeSwitching).toBeNull()
    act(() => { hook.result.current.turnOnCrewMode() })
    expect(hook.result.current.crewModeSwitching).toBe('on')
    // Back on A: still held, so a send cannot start the turn on the old agent.
    hook.rerender({ slot: plain('a') })
    expect(hook.result.current.crewModeSwitching).toBe('on')
    // B's switch lands: A stays held until its own lands.
    await act(async () => { pending[1].resolve() })
    expect(hook.result.current.crewModeSwitching).toBe('on')
    await act(async () => { pending[0].resolve() })
    expect(hook.result.current.crewModeSwitching).toBeNull()
  })

  it("keeps chat A's turning-off hold when chat B turns off too", async () => {
    const { hook, pending } = setup()
    hook.rerender({ slot: plain('a', 'kirocrew-conductor') })
    act(() => { hook.result.current.turnOffCrewMode?.() })
    expect(hook.result.current.crewModeSwitching).toBe('off')
    hook.rerender({ slot: plain('b', 'kirocrew-conductor') })
    act(() => { hook.result.current.turnOffCrewMode?.() })
    hook.rerender({ slot: plain('a', 'kirocrew-conductor') })
    expect(hook.result.current.crewModeSwitching).toBe('off')
    await act(async () => { pending[1].resolve() })
    expect(hook.result.current.crewModeSwitching).toBe('off')
    await act(async () => { pending[0].resolve() })
    expect(hook.result.current.crewModeSwitching).toBeNull()
  })
})

describe('useCrewModeSwitch turning Ultra Mode off', () => {
  beforeEach(() => { setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']); vi.mocked(dropPanelViews).mockClear() })
  afterEach(() => setCrewModeCatalog(undefined, undefined))

  // Ultra Mode turned on in a closed panel, so turning it off again closes the panel.
  const turnedOn = async () => {
    const s = setup(vi.fn<(kind: ViewKind) => void>())
    act(() => { s.hook.result.current.turnOnCrewMode() })
    await act(async () => { s.pending[0].resolve() })
    s.hook.rerender({ slot: plain('a', 'kirocrew-conductor') })
    expect(s.store.getState().chat.activityOpen).toBe(true)
    return s
  }

  it('keeps the Crew and Dashboard tabs and the panel when the switch back fails', async () => {
    const { hook, pending, store } = await turnedOn()
    act(() => { hook.result.current.turnOffCrewMode?.() })
    await act(async () => { pending[1].resolve(false) })
    expect(dropPanelViews).not.toHaveBeenCalled()
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(hook.result.current.crewModeSwitching).toBeNull()
  })

  it('drops both tabs and closes the panel once the switch back lands', async () => {
    const { hook, pending, store } = await turnedOn()
    act(() => { hook.result.current.turnOffCrewMode?.() })
    // Not before the switch has landed.
    expect(dropPanelViews).not.toHaveBeenCalled()
    expect(store.getState().chat.activityOpen).toBe(true)
    await act(async () => { pending[1].resolve(true) })
    expect(dropPanelViews).toHaveBeenCalledWith('a', ['workers', 'command-center'])
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('leaves the panel open when the person moved to another chat before the switch landed', async () => {
    const { hook, pending, store } = await turnedOn()
    act(() => { hook.result.current.turnOffCrewMode?.() })
    hook.rerender({ slot: plain('b') })
    await act(async () => { pending[1].resolve(true) })
    expect(dropPanelViews).toHaveBeenCalledWith('a', ['workers', 'command-center'])
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('leaves the panel open when the page unmounted before the switch landed', async () => {
    const { hook, pending, store } = await turnedOn()
    act(() => { hook.result.current.turnOffCrewMode?.() })
    hook.unmount()
    await act(async () => { pending[1].resolve(true) })
    expect(dropPanelViews).toHaveBeenCalledWith('a', ['workers', 'command-center'])
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it("keeps chat B's closed panel to restore when chat A's switch back lands meanwhile", async () => {
    const { hook, pending, store } = await turnedOn()
    act(() => { hook.result.current.turnOffCrewMode?.() })
    // On chat B, with its panel closed, turn Ultra Mode on before A's switch lands.
    hook.rerender({ slot: plain('b') })
    act(() => { store.dispatch(closeActivityPanel()) })
    act(() => { hook.result.current.turnOnCrewMode() })
    await act(async () => { pending[1].resolve(true) })
    await act(async () => { pending[2].resolve(true) })
    hook.rerender({ slot: plain('b', 'kirocrew-conductor') })
    act(() => { hook.result.current.turnOffCrewMode?.() })
    await act(async () => { pending[3].resolve(true) })
    expect(store.getState().chat.activityOpen).toBe(false)
  })
})

describe('useCrewModeSwitch turning Ultra Mode on fails', () => {
  beforeEach(() => { setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']); vi.mocked(dropPanelViews).mockClear(); localStorage.clear(); turnOnCrewModePreview(); __resetPanelTabs() })
  afterEach(() => setCrewModeCatalog(undefined, undefined))

  it('closes the panel it opened and drops the Crew and Dashboard tabs', async () => {
    const { hook, pending, store } = setup()
    expect(store.getState().chat.activityOpen).toBe(false)
    act(() => { hook.result.current.turnOnCrewMode() })
    expect(store.getState().chat.activityOpen).toBe(true)
    await act(async () => { pending[0].resolve(false) })
    expect(dropPanelViews).toHaveBeenCalledWith('a', ['workers', 'command-center'], { focus: null, over: ['workers'] })
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(hook.result.current.crewModeSwitching).toBeNull()
  })

  it('keeps a Dashboard tab and an open panel the person had before', async () => {
    const { hook, pending, store } = setup()
    openPanelView('a', 'command-center')
    act(() => { store.dispatch(openActivityPanel()) })
    act(() => { hook.result.current.turnOnCrewMode() })
    await act(async () => { pending[0].resolve(false) })
    expect(dropPanelViews).toHaveBeenCalledWith('a', ['workers'], { focus: 'command-center', over: ['workers'] })
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('leaves the panel open when the person moved to another chat before the switch failed', async () => {
    const { hook, pending, store } = setup()
    act(() => { hook.result.current.turnOnCrewMode() })
    hook.rerender({ slot: plain('b') })
    await act(async () => { pending[0].resolve(false) })
    expect(dropPanelViews).toHaveBeenCalledWith('a', ['workers', 'command-center'], { focus: null, over: ['workers'] })
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('touches nothing when the switch lands', async () => {
    const { hook, pending, store } = setup()
    act(() => { hook.result.current.turnOnCrewMode() })
    await act(async () => { pending[0].resolve(true) })
    expect(dropPanelViews).not.toHaveBeenCalled()
    expect(store.getState().chat.activityOpen).toBe(true)
  })
})

describe('useCrewModeSwitch entering an Ultra Mode root', () => {
  beforeEach(() => setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']))
  afterEach(() => { setCrewModeCatalog(undefined, undefined); mobile.value = false })

  const enter = () => {
    const store = createTestStore()
    const openView = vi.fn()
    const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
    renderHook(() => useCrewModeSwitch({ currentSlot: plain('root', 'kirocrew-conductor'), defaultAgent: 'default', hasUserMessage: true, switchAgent: vi.fn(), openView }), { wrapper })
    return { store, openView }
  }

  it('opens the side panel on the Crew tab on a desktop', () => {
    const { store, openView } = enter()
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(openView).toHaveBeenCalledWith('workers')
  })

  it('leaves a phone on the chat', () => {
    mobile.value = true
    const { store, openView } = enter()
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(openView).not.toHaveBeenCalled()
  })
})

describe('useCrewModeSwitch turning Ultra Mode on', () => {
  beforeEach(() => setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']))
  afterEach(() => { setCrewModeCatalog(undefined, undefined); mobile.value = false })

  const turnOn = () => {
    const store = createTestStore()
    const openView = vi.fn()
    const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
    const hook = renderHook(() => useCrewModeSwitch({ currentSlot: plain('fresh'), defaultAgent: 'default', hasUserMessage: false, switchAgent: vi.fn(() => new Promise<void>(() => {})), openView }), { wrapper })
    act(() => { hook.result.current.turnOnCrewMode() })
    return { store, openView }
  }

  it('opens the side panel on the Board on a desktop', () => {
    const { store, openView } = turnOn()
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(openView).toHaveBeenCalledWith('workers')
  })

  it('keeps a phone on the chat: no panel sheet opens', () => {
    mobile.value = true
    const { store, openView } = turnOn()
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(openView).not.toHaveBeenCalled()
  })
})

describe("useCrewModeSwitch the welcome page's Board button", () => {
  beforeEach(() => setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']))
  afterEach(() => { setCrewModeCatalog(undefined, undefined); mobile.value = false })

  const press = () => {
    const store = createTestStore()
    const openView = vi.fn()
    const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
    const hook = renderHook(() => useCrewModeSwitch({ currentSlot: plain('root', 'kirocrew-conductor'), defaultAgent: 'default', hasUserMessage: true, switchAgent: vi.fn(), openView }), { wrapper })
    store.dispatch(closeActivityPanel())
    openView.mockClear()
    act(() => { hook.result.current.openTaskBoard!() })
    return { store, openView }
  }

  it('opens the side panel on the Board tab, not the Dashboard', () => {
    const { store, openView } = press()
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(openView).toHaveBeenCalledTimes(1)
    expect(openView).toHaveBeenCalledWith('workers')
    expect(openView).not.toHaveBeenCalledWith('command-center')
  })

  it('opens the Board in the panel sheet on a phone too: the person pressed it', () => {
    mobile.value = true
    const { store, openView } = press()
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(openView).toHaveBeenCalledWith('workers')
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
