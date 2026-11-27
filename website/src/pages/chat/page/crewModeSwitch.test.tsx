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
import { __resetPanelTabs, dropPanelViews, openPanelView, type ViewKind } from '../../../hooks/usePanelTabs'
import { closeActivityPanel, openActivityPanel } from '../../../store/chatSlice'

const mobile = vi.hoisted(() => ({ value: false }))
vi.mock('../../../hooks/useIsMobile', () => ({ useIsMobile: () => mobile.value }))
const askAppConfirm = vi.hoisted(() => vi.fn<(opts: { title: string; body?: unknown; confirmLabel: string; primary?: boolean }) => Promise<boolean>>())
vi.mock('../../../lib/appConfirm', () => ({ askAppConfirm }))
vi.mock('../../../hooks/usePanelTabs', async importOriginal => ({ ...await importOriginal<object>(), dropPanelViews: vi.fn() }))

const plain = (key: string, agent = 'default'): ChatSlot => ({ key, agent, agent_kind: 'template', memory_mode: 'persistent', origin: 'user' } as unknown as ChatSlot)

function setup(openView: (kind: ViewKind) => void = () => {}, { autoOpenBoard = true }: { autoOpenBoard?: boolean } = {}) {
  const pending: Array<{ resolve: (ok?: boolean) => void }> = []
  const switchAgent = vi.fn((_agent: string) => new Promise<boolean>(resolve => { pending.push({ resolve: (ok = true) => resolve(ok) }) }))
  const store = createTestStore()
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const hook = renderHook(
    ({ slot }: { slot: ChatSlot }) => useCrewModeSwitch({ currentSlot: slot, defaultAgent: 'default', hasUserMessage: false, switchAgent, openView, autoOpenBoard }),
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

describe('useCrewModeSwitch turning conductor mode off', () => {
  beforeEach(() => { setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']); vi.mocked(dropPanelViews).mockClear() })
  afterEach(() => setCrewModeCatalog(undefined, undefined))

  // Conductor mode turned on in a closed panel, so turning it off again closes the panel.
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
    // On chat B, with its panel closed, turn conductor mode on before A's switch lands.
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

describe('useCrewModeSwitch turning conductor mode on fails', () => {
  beforeEach(() => { setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']); vi.mocked(dropPanelViews).mockClear(); localStorage.clear(); __resetPanelTabs() })
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

describe('useCrewModeSwitch entering a conductor mode root', () => {
  beforeEach(() => { setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']); localStorage.clear() })
  afterEach(() => { setCrewModeCatalog(undefined, undefined); mobile.value = false })

  // `autoOpenBoard` is Auto-Open Board in Side Panel: `undefined` while the
  // dashboard config read is still in flight.
  const enter = (autoOpenBoard: boolean | undefined) => {
    const store = createTestStore()
    const openView = vi.fn()
    const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
    const hook = renderHook(
      ({ autoOpen }: { autoOpen: boolean | undefined }) => useCrewModeSwitch({ currentSlot: plain('root', 'kirocrew-conductor'), defaultAgent: 'default', hasUserMessage: true, switchAgent: vi.fn(), openView, autoOpenBoard: autoOpen }),
      { wrapper, initialProps: { autoOpen: autoOpenBoard } },
    )
    return { store, openView, hook }
  }

  it('opens the side panel on the Crew tab on a desktop', () => {
    const { store, openView } = enter(true)
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(openView).toHaveBeenCalledWith('workers')
  })

  it('leaves a phone on the chat', () => {
    mobile.value = true
    const { store, openView } = enter(true)
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(openView).not.toHaveBeenCalled()
  })

  it('keeps a panel the person closed in this chat closed on a return', () => {
    localStorage.setItem('mc-activity-open:root', 'false')
    const { store, openView } = enter(true)
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(openView).not.toHaveBeenCalled()
  })

  it('leaves the tab the person picked in front on a return', () => {
    localStorage.setItem('mc-activity-open:root', 'true')
    const { openView } = enter(true)
    expect(openView).not.toHaveBeenCalled()
  })

  it('leaves the panel closed with Auto-Open Board in Side Panel off', () => {
    const { store, openView } = enter(false)
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(openView).not.toHaveBeenCalled()
  })

  it('waits for the setting to load, then opens the side panel on the Board', () => {
    const { store, openView, hook } = enter(undefined)
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(openView).not.toHaveBeenCalled()
    hook.rerender({ autoOpen: true })
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(openView).toHaveBeenCalledWith('workers')
  })

  it('keeps the panel closed when the setting loads off', () => {
    const { store, openView, hook } = enter(undefined)
    hook.rerender({ autoOpen: false })
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(openView).not.toHaveBeenCalled()
  })

  it('opens nothing when the setting turns on during the visit', () => {
    const { store, openView, hook } = enter(false)
    hook.rerender({ autoOpen: true })
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(openView).not.toHaveBeenCalled()
  })
})

describe('useCrewModeSwitch a new chat created in conductor mode', () => {
  beforeEach(() => { setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']); vi.mocked(dropPanelViews).mockClear() })
  afterEach(() => setCrewModeCatalog(undefined, undefined))

  it('closes the panel its entry opened when it is turned off before the first message', async () => {
    const { hook, pending, store } = setup()
    expect(store.getState().chat.activityOpen).toBe(false)
    // Born on the conductor (`dashboard.new_chat_crew_mode`): entered, never pressed.
    hook.rerender({ slot: plain('a', 'kirocrew-conductor') })
    expect(store.getState().chat.activityOpen).toBe(true)
    act(() => { hook.result.current.turnOffCrewMode?.() })
    await act(async () => { pending[0].resolve(true) })
    expect(dropPanelViews).toHaveBeenCalledWith('a', ['workers', 'command-center'])
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('keeps a panel that was already open when it is turned off', async () => {
    const { hook, pending, store } = setup()
    act(() => { store.dispatch(openActivityPanel()) })
    hook.rerender({ slot: plain('a', 'kirocrew-conductor') })
    act(() => { hook.result.current.turnOffCrewMode?.() })
    await act(async () => { pending[0].resolve(true) })
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('leaves the panel closed when Auto-Open Board in Side Panel is off', () => {
    const openView = vi.fn()
    const { hook, store } = setup(openView, { autoOpenBoard: false })
    hook.rerender({ slot: plain('a', 'kirocrew-conductor') })
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(openView).not.toHaveBeenCalled()
  })
})

describe('useCrewModeSwitch a new chat on a conductor default agent', () => {
  beforeEach(() => { setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'], 'kirocrew-conductor'); localStorage.clear() })
  afterEach(() => setCrewModeCatalog(undefined, undefined))

  // The chip is not offered (there is no plain agent to go back to), so the
  // chat is entered on the conductor: the setting alone decides the panel.
  const enterNew = (autoOpenBoard: boolean) => {
    const store = createTestStore()
    const openView = vi.fn()
    const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
    const hook = renderHook(() => useCrewModeSwitch({ currentSlot: plain('new', 'kirocrew-conductor'), defaultAgent: 'kirocrew-conductor', hasUserMessage: false, switchAgent: vi.fn(), openView, autoOpenBoard }), { wrapper })
    expect(hook.result.current.crewModeOn).toBe(true)
    expect(hook.result.current.crewModeEligible).toBe(false)
    return { store, openView }
  }

  it('opens the side panel on the Board with Auto-Open Board in Side Panel on', () => {
    const { store, openView } = enterNew(true)
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(openView).toHaveBeenCalledWith('workers')
  })

  it('leaves the panel closed with Auto-Open Board in Side Panel off', () => {
    const { store, openView } = enterNew(false)
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(openView).not.toHaveBeenCalled()
  })
})

describe('useCrewModeSwitch leaving a conductor that is the default agent', () => {
  beforeEach(() => { setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']); localStorage.clear(); vi.mocked(dropPanelViews).mockClear() })
  afterEach(() => setCrewModeCatalog(undefined, undefined))

  // A new chat born on the conductor, which is the default agent too.
  const born = (hasUserMessage: boolean, slotHasMore = false, running = false, slotCursorKey = 'a') => {
    const pending: Array<(ok: boolean) => void> = []
    const switchAgent = vi.fn((_agent: string, _kind?: string) => new Promise<boolean>(resolve => { pending.push(resolve) }))
    const store = createTestStore({ chat: { ...createTestStore().getState().chat, activeSlot: 'a', slotCursorKey, slotHasMore } })
    const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
    const hook = renderHook(() => useCrewModeSwitch({ currentSlot: { ...plain('a', 'kirocrew-conductor'), running }, defaultAgent: 'kirocrew-conductor', hasUserMessage, switchAgent, openView: () => {}, autoOpenBoard: true }), { wrapper })
    return { hook, pending, switchAgent, store }
  }

  it('moves a new root onto the picked template, then drops its Crew and Dashboard tabs and the panel its entry opened', async () => {
    const { hook, pending, switchAgent, store } = born(false)
    expect(hook.result.current.turnOffCrewMode).toBeUndefined()
    expect(store.getState().chat.activityOpen).toBe(true)
    act(() => { void hook.result.current.leaveCrewModeTo?.('other-template') })
    expect(switchAgent).toHaveBeenCalledWith('other-template', 'template')
    expect(dropPanelViews).not.toHaveBeenCalled()
    await act(async () => { pending[0](true) })
    expect(dropPanelViews).toHaveBeenCalledWith('a', ['workers', 'command-center'])
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('offers no way off once the chat has a message', () => {
    expect(born(true).hook.result.current.leaveCrewModeTo).toBeUndefined()
  })

  it('offers no way off while the chat page has older rows, which the gateway counts as a message', () => {
    expect(born(false, true).hook.result.current.leaveCrewModeTo).toBeUndefined()
  })

  it('offers no way off while a turn runs, since the gateway switches no chat mid-turn', () => {
    expect(born(false, false, true).hook.result.current.leaveCrewModeTo).toBeUndefined()
  })

  it('offers no way off while the paging cursor still describes another chat', () => {
    expect(born(false, false, false, 'b').hook.result.current.leaveCrewModeTo).toBeUndefined()
  })
})

describe('useCrewModeSwitch turning conductor mode on', () => {
  beforeEach(() => setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']))
  afterEach(() => { setCrewModeCatalog(undefined, undefined); mobile.value = false })

  const turnOn = () => {
    const store = createTestStore()
    const openView = vi.fn()
    const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
    const hook = renderHook(() => useCrewModeSwitch({ currentSlot: plain('fresh'), defaultAgent: 'default', hasUserMessage: false, switchAgent: vi.fn(() => new Promise<void>(() => {})), openView, autoOpenBoard: true }), { wrapper })
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
    const hook = renderHook(() => useCrewModeSwitch({ currentSlot: plain('root', 'kirocrew-conductor'), defaultAgent: 'default', hasUserMessage: true, switchAgent: vi.fn(), openView, autoOpenBoard: true }), { wrapper })
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

describe('useCrewModeSwitch turning conductor mode on in a chat that has messages', () => {
  beforeEach(() => { setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']); askAppConfirm.mockReset(); localStorage.clear() })
  afterEach(() => setCrewModeCatalog(undefined, undefined))

  const render = (hasUserMessage: boolean) => {
    const switchAgent = vi.fn(async (_agent: string, _kind?: 'member' | 'template') => true)
    const store = createTestStore()
    const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
    const hook = renderHook(
      () => useCrewModeSwitch({ currentSlot: plain('a'), defaultAgent: 'default', hasUserMessage, switchAgent, openView: () => {}, autoOpenBoard: false }),
      { wrapper },
    )
    return { hook, switchAgent }
  }

  it('asks first in a chat that has a user message', async () => {
    askAppConfirm.mockResolvedValue(true)
    const { hook, switchAgent } = render(true)
    await act(async () => { hook.result.current.turnOnCrewMode() })
    expect(askAppConfirm).toHaveBeenCalledTimes(1)
    const opts = askAppConfirm.mock.calls[0][0]
    expect(opts.title).toBe('Turn on conductor mode for this chat?')
    expect(opts.confirmLabel).toBe('Turn on conductor mode')
    // The ordinary primary action, not a destructive one.
    expect(opts).toMatchObject({ primary: true })
    expect(switchAgent).toHaveBeenCalledWith('kirocrew-conductor', 'template')
  })

  it('leaves the chat unchanged on Cancel', async () => {
    askAppConfirm.mockResolvedValue(false)
    const { hook, switchAgent } = render(true)
    await act(async () => { hook.result.current.turnOnCrewMode() })
    expect(askAppConfirm).toHaveBeenCalledTimes(1)
    expect(switchAgent).not.toHaveBeenCalled()
    expect(hook.result.current.crewModeSwitching).toBeNull()
  })

  it('switches at once in a chat with no message yet: it can still be turned off', async () => {
    const { hook, switchAgent } = render(false)
    await act(async () => { hook.result.current.turnOnCrewMode() })
    expect(askAppConfirm).not.toHaveBeenCalled()
    expect(switchAgent).toHaveBeenCalledWith('kirocrew-conductor', 'template')
  })
})
