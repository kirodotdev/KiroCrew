import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useEffect, type ReactNode } from 'react'
import { act, render, screen, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { BrowserRouter, useLocation, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InstanceView } from '../api/client'
import { api } from '../api/client'
import { connectInstanceInto } from './connectInstance'
import { isEmbeddedPane } from './embedded'
import { paneOriginFor } from './paneCache'
import { readPaneSlot } from './paneLastSlot'
import { recentCrews } from './crewWarmSet'
import { i18nT } from '../i18n/t'
import { removeWarm, setActiveId, setPaneReady, setPendingPaneSlot, setWarm } from '../store/instancesSlice'
import { chatDeepLinkSlot } from '../utils/navIntent'
import { createTestStore, renderHookWithProviders } from '../test/helpers'
import { closeCrewWindow, currentCrewWindow, openCrewWindow } from '../pages/chat/crew-window/crewWindowStore'
import { recallActivePane, rememberActivePane } from './activePaneMemory'
import {
  PANE_OPEN_SLOT_TYPE,
  parseCrewDeepLink,
  useCrewAddressLink,
  useCrewDeepLink,
  useDeliverPendingPaneSlot,
  usePaneOpenSlot,
  withoutSpentCrewLink,
} from './crewDeepLink'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, listInstances: vi.fn() } }
})
vi.mock('./connectInstance', () => ({ connectInstanceInto: vi.fn() }))
vi.mock('./embedded', async importOriginal => {
  const mod = await importOriginal<typeof import('./embedded')>()
  return { ...mod, isEmbeddedPane: vi.fn(() => false) }
})

const listInstances = vi.mocked(api.listInstances)
const connect = vi.mocked(connectInstanceInto)

function crew(id: string, extra: Partial<InstanceView> = {}): InstanceView {
  return {
    id, name: id, ssh_host: id, remote_port: 0, local_port: 0, ttl: '', remote_bin: '',
    connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '', ssm_run_as: '',
    was_connected: true, status: { state: 'connected' } as InstanceView['status'], ...extra,
  }
}

const LINK = '/chat?crew=dev-box&crewSid=chat-3-1785870239'

function hub(store = createTestStore()) {
  const r = renderHookWithProviders(() => useCrewDeepLink(), { store, route: '/settings' })
  return { ...r, open: (path: string) => r.result.current(path) }
}

const UNKNOWN = { message: i18nT('store.chatSlice.crew_link_unknown'), error: true }
const MALFORMED = { message: i18nT('store.chatSlice.crew_link_malformed'), error: true }
const LOAD_FAILED = { message: i18nT('store.chatSlice.crew_link_load_failed'), error: true }
const OFF = { message: i18nT('store.chatSlice.crew_link_off'), error: true }
const NO_DASHBOARD = { message: i18nT('store.chatSlice.crew_link_no_dashboard'), error: true }
const OFFLINE = { message: i18nT('store.chatSlice.crew_link_not_connected'), error: true }

beforeEach(() => {
  localStorage.clear()
  listInstances.mockReset()
  connect.mockReset()
  vi.mocked(isEmbeddedPane).mockReturnValue(false)
})

describe('parseCrewDeepLink', () => {
  it('reads the resolver routes and leaves an older dashboard nothing to switch to', () => {
    expect(parseCrewDeepLink(LINK)).toEqual({ kind: 'open', crewId: 'dev-box', key: 'chat-3-1785870239' })
    expect(parseCrewDeepLink('/chat?crewError=1')).toEqual({ kind: 'error' })
    expect(parseCrewDeepLink('/chat?sid=x')).toBeNull()
    expect(parseCrewDeepLink('/settings')).toBeNull()
    // An older hub's own reader finds no session in a crew route.
    expect(chatDeepLinkSlot(LINK)).toBe('')
  })
})

describe('useCrewDeepLink (hub)', () => {
  it('holds a link that arrives before the crew list, then switches and holds the key', async () => {
    let answer!: (v: { instances: InstanceView[] }) => void
    listInstances.mockReturnValue(new Promise(res => { answer = res }) as never)
    const { store, open } = hub()
    expect(open(LINK)).toBe(true)
    expect(store.getState().instances.activeId).toBeNull()
    await act(async () => { answer({ instances: [crew('dev-box')] }) })
    await waitFor(() => expect(store.getState().instances.activeId).toBe('dev-box'))
    expect(store.getState().instances.pendingPaneSlot).toEqual({ id: 'dev-box', key: 'chat-3-1785870239' })
    // No pane yet: the last-chat hint carries the key for an older remote.
    expect(readPaneSlot('dev-box')).toBe('chat-3-1785870239')
  })

  it('the newest link wins even when an older link\'s crew list answers last', async () => {
    const answers: Array<(v: { instances: InstanceView[] }) => void> = []
    listInstances.mockImplementation(() => new Promise(res => { answers.push(res) }) as never)
    const { store, open } = hub()
    open(LINK)
    open('/chat?crew=work-box&crewSid=chat-9')
    await waitFor(() => expect(answers).toHaveLength(2))
    await act(async () => { answers[1]({ instances: [crew('dev-box'), crew('work-box')] }) })
    await waitFor(() => expect(store.getState().instances.activeId).toBe('work-box'))
    await act(async () => { answers[0]({ instances: [crew('dev-box'), crew('work-box')] }) })
    expect(store.getState().instances.activeId).toBe('work-box')
    expect(store.getState().instances.pendingPaneSlot?.key).toBe('chat-9')
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('a newer link drops the key an older link still holds, before its own read answers', () => {
    listInstances.mockReturnValue(new Promise(() => {}) as never)
    const store = createTestStore()
    store.dispatch(setPendingPaneSlot({ id: 'dev-box', key: 'old' }))
    const { open } = hub(store)
    open('/chat?crew=dev-box&crewSid=new')
    expect(store.getState().instances.pendingPaneSlot).toBeNull()
  })

  it.each([
    ['a local session link', (open: (p: string) => boolean) => open('/chat?sid=local-1')],
    ['a tab click', (_open: unknown, store: ReturnType<typeof createTestStore>) => store.dispatch(setActiveId('work-box'))],
    ['a tab click away and back', (_open: unknown, store: ReturnType<typeof createTestStore>) => {
      store.dispatch(setActiveId('work-box'))
      store.dispatch(setActiveId(null))
    }],
  ])('%s made during the read is never overridden', async (_name, act2) => {
    let answer!: (v: { instances: InstanceView[] }) => void
    listInstances.mockReturnValue(new Promise(res => { answer = res }) as never)
    const store = createTestStore()
    const { open } = hub(store)
    open(LINK)
    act2(open, store)
    const before = store.getState().instances.activeId
    await act(async () => { answer({ instances: [crew('dev-box'), crew('work-box')] }) })
    expect(store.getState().instances.activeId).toBe(before)
    expect(store.getState().instances.pendingPaneSlot).toBeNull()
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('a crew already loaded keeps its own last-chat hint', async () => {
    listInstances.mockResolvedValue({ instances: [crew('dev-box')] } as never)
    const store = createTestStore()
    store.dispatch(setWarm({ id: 'dev-box', conn: { port: 5480, token: 't' } }))
    const { open } = hub(store)
    open(LINK)
    await waitFor(() => expect(store.getState().instances.pendingPaneSlot).not.toBeNull())
    expect(readPaneSlot('dev-box')).toBeNull()
  })

  it.each([
    ['an unknown crew', { instances: [crew('other')] }, UNKNOWN],
    ['a crew turned off', { instances: [crew('dev-box', { disabled: true })] }, OFF],
    ['a crew with no dashboard', { instances: [crew('dev-box', { connection_method: 'fargate' })] }, NO_DASHBOARD],
  ])('%s raises its own shell error banner over the local crew and opens nothing', async (_name, list, notice) => {
    listInstances.mockResolvedValue(list as never)
    const store = createTestStore()
    store.dispatch(setActiveId('other'))
    const { open } = hub(store)
    open(LINK)
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).toEqual(notice))
    expect(store.getState().instances.activeId).toBeNull()
    expect(store.getState().instances.pendingPaneSlot).toBeNull()
    expect(store.getState().chat.activeSlot).toBeNull()
    expect(readPaneSlot('dev-box')).toBeNull()
  })

  it('records the crew as opened only once its connect is admitted, before the warm entry', async () => {
    localStorage.setItem('mc-auto-connect', '0')
    listInstances.mockResolvedValue({ instances: [crew('dev-box')] } as never)
    let firstBeforeWarm: string | undefined
    connect.mockImplementation(async (_d, _id, _via, opts) => {
      expect(recentCrews()).toEqual([])
      opts?.beforeWarm?.()
      firstBeforeWarm = recentCrews()[0]
      return { state: 'connected', local_port: 7781, token: 't' } as never
    })
    const { open } = hub()
    open(LINK)
    await waitFor(() => expect(firstBeforeWarm).toBe('ssh:dev-box'))
  })

  it('a crew link superseded while it connects records nothing', async () => {
    localStorage.setItem('mc-auto-connect', '0')
    listInstances.mockResolvedValue({ instances: [crew('dev-box')] } as never)
    let admit: () => void = () => {}
    connect.mockImplementation((_d, _id, _via, opts) => new Promise(r => {
      admit = () => { opts?.beforeWarm?.(); r({ state: 'connected', local_port: 7781, token: 't' } as never) }
    }))
    const { store, open } = hub()
    open(LINK)
    await waitFor(() => expect(connect).toHaveBeenCalled())
    // The person picks another pane before the connect answers.
    act(() => { store.dispatch(setActiveId('other')) })
    await act(async () => { admit() })
    expect(recentCrews()).toEqual([])
  })

  it('a refused crew link records nothing', async () => {
    localStorage.setItem('mc-auto-connect', '0')
    listInstances.mockResolvedValue({ instances: [crew('dev-box')] } as never)
    connect.mockResolvedValue({ state: 'disconnected' } as never)
    const { open } = hub()
    open(LINK)
    await waitFor(() => expect(connect).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    expect(recentCrews()).toEqual([])
  })

  it('a crew list that could not load says so, not a switch', async () => {
    listInstances.mockRejectedValue(new Error('403'))
    const { store, open } = hub()
    open(LINK)
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).toEqual(LOAD_FAILED))
  })

  it('a crew value that is not an id is refused at once, without waiting for the list', () => {
    listInstances.mockReturnValue(new Promise(() => {}) as never)
    const { store, open } = hub()
    open('/chat?crewError=1')
    expect(store.getState().chat.agentSwitchNotice).toEqual(MALFORMED)
  })

  describe('with auto-connect off', () => {
    afterEach(() => localStorage.clear())

    it('a tunnel that is down is refused and never brought up', async () => {
      localStorage.setItem('mc-auto-connect', '0')
      listInstances.mockResolvedValue({ instances: [crew('dev-box', { status: { state: 'disconnected' } as InstanceView['status'] })] } as never)
      connect.mockResolvedValue({ state: 'disconnected' } as never)
      const { store, open } = hub()
      open(LINK)
      await waitFor(() => expect(store.getState().chat.agentSwitchNotice).toEqual(OFFLINE))
      expect(connect).toHaveBeenCalledTimes(1)
      expect(connect).toHaveBeenCalledWith(expect.anything(), 'dev-box', 'link', expect.objectContaining({ onlyIfConnected: true }))
      expect(store.getState().instances.activeId).toBeNull()
    })

    it('a tunnel that is up switches without a connect of its own', async () => {
      localStorage.setItem('mc-auto-connect', '0')
      listInstances.mockResolvedValue({ instances: [crew('dev-box')] } as never)
      connect.mockResolvedValue({ state: 'connected', local_port: 5480, token: 't' } as never)
      const { store, open } = hub()
      open(LINK)
      await waitFor(() => expect(store.getState().instances.activeId).toBe('dev-box'))
      expect(connect).toHaveBeenCalledTimes(1)
      expect(store.getState().instances.pendingPaneSlot?.key).toBe('chat-3-1785870239')
      // With auto-connect off the link only asks; it writes no hint, so an older
      // remote opens its own last chat.
      expect(readPaneSlot('dev-box')).toBeNull()
    })
  })
})

describe('useDeliverPendingPaneSlot (hub)', () => {
  it('posts the key to the pane exact origin once it is ready, then forgets it', () => {
    const store = createTestStore()
    store.dispatch(setWarm({ id: 'dev-box', conn: { port: 5480, token: 't' } }))
    store.dispatch(setActiveId('dev-box'))
    store.dispatch(setPendingPaneSlot({ id: 'dev-box', key: 'k1' }))
    const postMessage = vi.fn()
    const frames = { current: new Map([['dev-box', { contentWindow: { postMessage } } as unknown as HTMLIFrameElement]]) }
    const { rerender } = renderHookWithProviders(() => {
      const s = store.getState().instances
      useDeliverPendingPaneSlot(frames, s.warm, s.ready)
    }, { store })
    expect(postMessage).not.toHaveBeenCalled()
    act(() => { store.dispatch(setPaneReady('dev-box')) })
    rerender()
    expect(postMessage).toHaveBeenCalledWith(
      { type: PANE_OPEN_SLOT_TYPE, v: 1, key: 'k1' },
      paneOriginFor(5480),
    )
    expect(store.getState().instances.pendingPaneSlot).toBeNull()
  })

  it('a pane whose tunnel died is reconnected and gets the key only after the reload', async () => {
    // The link's own read says the tunnel is down; the pane still reads ready.
    listInstances.mockResolvedValue({ instances: [crew('dev-box', { status: { state: 'error' } as InstanceView['status'] })] } as never)
    const store = createTestStore()
    store.dispatch(setWarm({ id: 'dev-box', conn: { port: 5480, token: 't' } }))
    store.dispatch(setPaneReady('dev-box'))
    const postMessage = vi.fn()
    const frames = { current: new Map([['dev-box', { contentWindow: { postMessage } } as unknown as HTMLIFrameElement]]) }
    const { open } = hub(store)
    const { rerender } = renderHookWithProviders(() => {
      const s = store.getState().instances
      useDeliverPendingPaneSlot(frames, s.warm, s.ready)
    }, { store })
    open(LINK)
    await waitFor(() => expect(store.getState().instances.pendingPaneSlot).not.toBeNull())
    expect(connect).toHaveBeenCalledWith(expect.anything(), 'dev-box')
    // The reload carries the hint, for an older remote and a second token write.
    expect(readPaneSlot('dev-box')).toBe('chat-3-1785870239')
    rerender()
    // The old document is still "ready", but for the dead tunnel's token.
    expect(postMessage).not.toHaveBeenCalled()
    act(() => { store.dispatch(setWarm({ id: 'dev-box', conn: { port: 5480, token: 't2' } })) })
    act(() => { store.dispatch(setPaneReady('dev-box')) })
    rerender()
    expect(postMessage).toHaveBeenCalledTimes(1)
  })

  it('a switch to any other pane, or that pane going away, drops the held key', () => {
    const store = createTestStore()
    store.dispatch(setActiveId('dev-box'))
    store.dispatch(setPendingPaneSlot({ id: 'dev-box', key: 'k1' }))
    store.dispatch(setActiveId(null))
    expect(store.getState().instances.pendingPaneSlot).toBeNull()
    store.dispatch(setActiveId('dev-box'))
    store.dispatch(setPendingPaneSlot({ id: 'dev-box', key: 'k1' }))
    store.dispatch(removeWarm('dev-box'))
    expect(store.getState().instances.pendingPaneSlot).toBeNull()
  })
})

describe('usePaneOpenSlot (pane)', () => {
  it('opens a key only from its own parent, from any page', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    const navigate = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(() => usePaneOpenSlot({ navigate, mayLeave: () => true }), { store, route: '/settings' })
    const send = (source: MessageEventSource | null, data: unknown) =>
      act(() => { window.dispatchEvent(new MessageEvent('message', { data, source })) })
    // In the test document `window.parent === window`; a foreign frame is not.
    send({} as MessageEventSource, { type: PANE_OPEN_SLOT_TYPE, v: 1, key: 'k1' })
    send(window.parent, { type: PANE_OPEN_SLOT_TYPE, v: 1, key: '..' })
    expect(navigate).not.toHaveBeenCalled()
    send(window.parent, { type: PANE_OPEN_SLOT_TYPE, v: 1, key: 'k1' })
    expect(navigate).toHaveBeenCalledWith('/chat')
    expect(store.getState().chat.slotSwitchTarget).toBe('k1')
  })

  it('does nothing outside a pane', () => {
    const navigate = vi.fn()
    renderHookWithProviders(() => usePaneOpenSlot({ navigate, mayLeave: () => true }))
    act(() => { window.dispatchEvent(new MessageEvent('message', { data: { type: PANE_OPEN_SLOT_TYPE, v: 1, key: 'k1' }, source: window.parent })) })
    expect(navigate).not.toHaveBeenCalled()
  })
})

describe('useCrewAddressLink (hub, the dashboard address)', () => {
  /** The shell's hook over a child that may write the address first, as the
   *  chat page's URL writer does: a child's effects run before its parent's. */
  function Shell({ childWrite }: { childWrite?: string }) {
    useCrewAddressLink()
    const loc = useLocation()
    return <><Writer to={childWrite} /><output data-testid="loc">{loc.pathname + loc.search}</output></>
  }
  function Writer({ to }: { to?: string }) {
    const navigate = useNavigate()
    useEffect(() => { if (to) navigate(to, { replace: true }) }, [to, navigate])
    return null
  }
  function mount(url: string, { store = createTestStore(), childWrite }: { store?: ReturnType<typeof createTestStore>; childWrite?: string } = {}) {
    window.history.replaceState(null, '', url)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrap = (ui: ReactNode) => (
      <QueryClientProvider client={queryClient}><Provider store={store}><BrowserRouter>{ui}</BrowserRouter></Provider></QueryClientProvider>
    )
    const r = render(wrap(<Shell childWrite={childWrite} />))
    return { store, loc: () => screen.getByTestId('loc').textContent, unmount: r.unmount }
  }

  beforeEach(() => {
    closeCrewWindow()
    rememberActivePane(null)
  })
  afterEach(() => {
    window.history.replaceState(null, '', '/')
  })

  it('opens the session in the crew window, connecting nothing, and drops the route from the address', async () => {
    listInstances.mockResolvedValue({ instances: [crew('dev-box', { status: { state: 'disconnected' } as InstanceView['status'] })] } as never)
    const { store, loc } = mount('/chat?crew=dev-box&crewSid=chat-3&sid=local-1')
    await waitFor(() => expect(currentCrewWindow()).toEqual({ instanceId: 'dev-box', key: 'chat-3' }))
    expect(loc()).toBe('/chat?sid=local-1')
    expect(connect).not.toHaveBeenCalled()
    expect(store.getState().instances.activeId).toBeNull()
    expect(store.getState().instances.pendingPaneSlot).toBeNull()
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('keeps what the chat page wrote to the address before it, and a reload reads nothing', async () => {
    listInstances.mockResolvedValue({ instances: [crew('dev-box')] } as never)
    const first = mount('/chat?crew=dev-box&crewSid=chat-3', { childWrite: '/chat/my-chat?crew=dev-box&crewSid=chat-3&sid=local-1' })
    await waitFor(() => expect(currentCrewWindow()).toEqual({ instanceId: 'dev-box', key: 'chat-3' }))
    expect(first.loc()).toBe('/chat/my-chat?sid=local-1')
    expect(window.location.search).toBe('?sid=local-1')
    // A reload of the entry the link left.
    const reloaded = window.location.pathname + window.location.search
    first.unmount()
    closeCrewWindow()
    mount(reloaded)
    await act(async () => {})
    expect(currentCrewWindow()).toBeNull()
    expect(listInstances).toHaveBeenCalledTimes(1)
  })

  it('opens on a later navigation onto the open chat, Back and Forward included', async () => {
    listInstances.mockResolvedValue({ instances: [crew('dev-box')] } as never)
    const { loc } = mount('/chat/my-chat?sid=local-1')
    await act(async () => {})
    expect(currentCrewWindow()).toBeNull()
    act(() => {
      window.history.pushState(null, '', '/chat/my-chat?sid=local-1&crew=dev-box&crewSid=k1')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    await waitFor(() => expect(currentCrewWindow()).toEqual({ instanceId: 'dev-box', key: 'k1' }))
    expect(loc()).toBe('/chat/my-chat?sid=local-1')
    expect(window.location.search).toBe('?sid=local-1')
  })

  it.each([
    ['an unknown id', '/chat?crew=nope&crewSid=k1', UNKNOWN],
    ['a crew turned off', '/chat?crew=dev-box&crewSid=k1', OFF],
    ['a crew with no dashboard pane', '/chat?crew=ecs-box&crewSid=k1', NO_DASHBOARD],
    ['a malformed key', '/chat?crew=dev-box&crewSid=..', MALFORMED],
    ['the resolver\'s error route', '/chat/my-chat?crewError=1', MALFORMED],
  ])('%s shows its message and opens nothing', async (_name, url, notice) => {
    listInstances.mockResolvedValue({ instances: [crew('dev-box', { disabled: true }), crew('ecs-box', { connection_method: 'fargate' })] } as never)
    const { store, loc } = mount(url)
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).toEqual(notice))
    expect(currentCrewWindow()).toBeNull()
    expect(loc()).not.toMatch(/crew/)
  })

  it('a crew list that fails to load shows its message and opens nothing', async () => {
    listInstances.mockRejectedValue(new Error('down'))
    const { store } = mount('/chat?crew=dev-box&crewSid=k1')
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).toEqual(LOAD_FAILED))
    expect(currentCrewWindow()).toBeNull()
  })

  it.each([
    ['the user picks a session', () => closeCrewWindow(), null],
    ['the user opens another crew session', () => openCrewWindow({ instanceId: 'work-box', key: 'w1' }), { instanceId: 'work-box', key: 'w1' }],
    ['a crew pane comes forward', (store: ReturnType<typeof createTestStore>) => store.dispatch(setActiveId('work-box')), null],
    ['the user leaves the chat page', () => {
      window.history.pushState(null, '', '/settings')
      window.dispatchEvent(new PopStateEvent('popstate'))
    }, null],
    ['a newer address link arrives', () => {
      window.history.pushState(null, '', '/chat?crew=work-box&crewSid=k2')
      window.dispatchEvent(new PopStateEvent('popstate'))
    }, { instanceId: 'work-box', key: 'k2' }],
  ])('a list that answers after %s meanwhile yields to it', async (_name, meanwhile, want) => {
    let answer!: (v: { instances: InstanceView[] }) => void
    listInstances.mockReturnValue(new Promise(res => { answer = res }) as never)
    const { store } = mount('/chat?crew=dev-box&crewSid=k1')
    await waitFor(() => expect(listInstances).toHaveBeenCalled())
    act(() => { meanwhile(store) })
    await act(async () => { answer({ instances: [crew('dev-box'), crew('work-box')] }) })
    expect(currentCrewWindow()).toEqual(want)
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('a list that fails after the user picked a session meanwhile shows nothing', async () => {
    let fail!: (e: Error) => void
    listInstances.mockReturnValue(new Promise((_res, rej) => { fail = rej }) as never)
    const { store } = mount('/chat?crew=dev-box&crewSid=k1')
    await waitFor(() => expect(listInstances).toHaveBeenCalled())
    act(() => { closeCrewWindow() })
    await act(async () => { fail(new Error('down')) })
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
    expect(currentCrewWindow()).toBeNull()
  })

  it('wins over a remembered pane and brings the local crew forward', async () => {
    listInstances.mockResolvedValue({ instances: [crew('dev-box'), crew('work-box')] } as never)
    rememberActivePane('work-box')
    const cold = mount('/chat?crew=dev-box&crewSid=k1')
    await waitFor(() => expect(currentCrewWindow()?.key).toBe('k1'))
    expect(recallActivePane()).toBeNull()
    cold.unmount()
    closeCrewWindow()
    const store = createTestStore()
    store.dispatch(setActiveId('work-box'))
    mount('/chat?crew=dev-box&crewSid=k2', { store })
    await waitFor(() => expect(currentCrewWindow()?.key).toBe('k2'))
    expect(store.getState().instances.activeId).toBeNull()
  })

  it('does nothing in a pane or off the chat route', async () => {
    listInstances.mockResolvedValue({ instances: [crew('dev-box')] } as never)
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    const pane = mount('/chat?crew=dev-box&crewSid=k1')
    expect(pane.loc()).toBe('/chat?crew=dev-box&crewSid=k1')
    pane.unmount()
    vi.mocked(isEmbeddedPane).mockReturnValue(false)
    const other = mount('/settings?crew=dev-box&crewSid=k1')
    await act(async () => {})
    expect(other.loc()).toBe('/settings?crew=dev-box&crewSid=k1')
    expect(currentCrewWindow()).toBeNull()
    expect(listInstances).not.toHaveBeenCalled()
  })
})

describe('withoutSpentCrewLink (a URL writer copying the router search)', () => {
  afterEach(() => {
    window.history.replaceState(null, '', '/')
  })

  it('does not write back a route the address link took off the address', () => {
    window.history.replaceState(null, '', '/chat?sid=local-1')
    const stale = new URLSearchParams('crew=dev-box&crewSid=k1&crewError=1&sid=local-1&q=x')
    expect(withoutSpentCrewLink(stale).toString()).toBe('sid=local-1&q=x')
  })

  it('keeps a route the address still carries, for the shell to read after the chat page writes', () => {
    window.history.replaceState(null, '', '/chat/my-chat?sid=local-1&crew=dev-box&crewSid=k1')
    const current = new URLSearchParams('sid=local-1&crew=dev-box&crewSid=k1')
    expect(withoutSpentCrewLink(current).toString()).toBe('sid=local-1&crew=dev-box&crewSid=k1')
  })
})
