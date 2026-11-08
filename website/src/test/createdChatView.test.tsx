/**
 * A create the user moved away from leaves the user where they are.
 *
 *   - "New session with token" stages its token only in the new session's
 *     composer. A user who opened session B while the create ran keeps B's
 *     composer as it was.
 *   - Alt+Shift+N brings the user to the new chat from another page, but not
 *     after they went to another page while the create ran, and not by an
 *     extra `/chat` push when they are already on the chat page.
 *   - A popout or embed chat rewrites its own slug for the new session, so a
 *     create started there is still shown: it stages its token and focuses the
 *     composer, and nothing navigates the frame to `/chat`.
 *   - Two separate Alt+Shift+N presses during one create make two sessions.
 *   - A page that starts holding a draft while the create runs is asked before
 *     the delayed navigate. Refused, the created chat is kept, nothing
 *     navigates, and no token is staged.
 *   - An embed Shift+click create the user switched tabs away from adds its tab
 *     behind the one the user picked, or fills the Sessions tab the user
 *     opened. A drag reorder of the user's own tab is not a switch.
 *
 * The create runs through the real `createSlot` thunk with the API held
 * pending, so the switched-away decision is the reducer's own.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import type { ChatSlot } from '../types'

const navigateSpy = vi.hoisted(() => vi.fn())
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useNavigate: () => navigateSpy,
}))

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

// The shell hook's unrelated collaborators: agent lists, instance panes and
// the palette trigger read the network or the provider context.
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: '' }) }))
vi.mock('../hooks/useInstanceShortcuts', () => ({ useInstanceShortcuts: () => {} }))
vi.mock('../hooks/useAutoConnectInstances', () => ({ useAutoConnectInstances: () => {} }))
vi.mock('../hooks/useCommandPalette', () => ({ useCommandPalette: () => ({ open: false, setOpen: () => {} }) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({}) }))
// Alt+Shift+N's composer focus, which it asks for only when the create is shown.
const focusSpy = vi.hoisted(() => vi.fn())
vi.mock('../pages/chat/composerFocus', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  queryComposerOrExpand: focusSpy,
}))

import { setActiveSlot } from '../store/chatSlice'
import { usePaletteActions } from '../components/commandPalette/paletteActions'
import { useShellKeyboard } from '../shell/shortcuts/shellKeyboard'
import { setAgentSwitchNotice } from '../store/chatSlice'
import EmbedTabStrip from '../components/EmbedTabStrip'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'

const ORIGIN = 'chat-a'
const OTHER = 'chat-b'
const NEW = 'chat-new'

function deferred<T>() {
  let resolve: (v: T) => void = () => {}
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

function makeStore(activeSlot: string | null = ORIGIN) {
  const defaults = createTestStore().getState()
  const slots = [
    { key: ORIGIN, title: 'A', messages: 1, running: false },
    { key: OTHER, title: 'B', messages: 1, running: false },
  ] as ChatSlot[]
  return createTestStore({
    dashboard: { ...defaults.dashboard, slots, slotsLoaded: true },
    chat: { ...defaults.chat, activeSlot },
  })
}

function wrapper(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <MemoryRouter>{children}</MemoryRouter>
      </Provider>
    </QueryClientProvider>
  )
}

/** Put the address bar on `path`, which `createChatForView` reads. */
const goTo = (path: string) => window.history.pushState(null, '', path)

let held: ReturnType<typeof deferred<unknown>>

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  navigateSpy.mockClear()
  focusSpy.mockClear()
  held = deferred<unknown>()
  mocks.createChatSlot.mockReset().mockReturnValue(held.promise)
})

afterEach(() => {
  goTo('/')
})

describe('New session with token', () => {
  function harness() {
    const store = makeStore(null)
    goTo('/settings')
    const hook = renderHook(() => usePaletteActions(), { wrapper: wrapper(store) })
    return { store, ...hook }
  }

  it('keeps the token out of a session the user opened during the create', async () => {
    const { store, result } = harness()
    act(() => result.current.newSessionWithToken('$zzq-skill'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    act(() => { store.dispatch(setActiveSlot(OTHER)) })
    await act(async () => { held.resolve({ key: NEW }) })
    expect(store.getState().chat.activeSlot).toBe(OTHER)
    expect(store.getState().chat.pendingInput).toBeNull()
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('stages the token when the chat page rewrites its own slug for the new session', async () => {
    const store = makeStore(ORIGIN)
    goTo(`/chat/slug-a?sid=${ORIGIN}`)
    const { result } = renderHook(() => usePaletteActions(), { wrapper: wrapper(store) })
    act(() => result.current.newSessionWithToken('@zzq-prompt'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    // The URL sync names the new session before the create's caller resumes.
    goTo(`/chat/new-session?sid=${NEW}`)
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(store.getState().chat.pendingInput).toBe('@zzq-prompt'))
    // Already on the chat page: no extra `/chat` entry for Back to land on.
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it.each([
    ['popout', '/popout/chat'],
    ['embed', '/embed/chat'],
  ])('stages the token in a %s chat that rewrites its own slug, without leaving it', async (_frame, page) => {
    const store = makeStore(ORIGIN)
    goTo(`${page}/slug-a?sid=${ORIGIN}`)
    const { result } = renderHook(() => usePaletteActions(), { wrapper: wrapper(store) })
    act(() => result.current.newSessionWithToken('@zzq-prompt'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    goTo(`${page}/new-session?sid=${NEW}`)
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(store.getState().chat.pendingInput).toBe('@zzq-prompt'))
    // The frame's router would redirect `/chat` out of the chat it shows.
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('stages the token when an embed Sessions tab is filled with the new session', async () => {
    const store = makeStore(ORIGIN)
    goTo('/embed/sessions')
    const { result } = renderHook(() => usePaletteActions(), { wrapper: wrapper(store) })
    act(() => result.current.newSessionWithToken('@zzq-prompt'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    // The tab strip routes the Sessions tab to the session the create opened.
    goTo(`/embed/chat/${NEW}?sid=${NEW}`)
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(store.getState().chat.pendingInput).toBe('@zzq-prompt'))
    // The embed frame's router would redirect `/chat` back to its Sessions list.
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('stages the token and opens the chat when the user stayed', async () => {
    const { store, result } = harness()
    act(() => result.current.newSessionWithToken('$zzq-skill'))
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(store.getState().chat.pendingInput).toBe('$zzq-skill'))
    expect(store.getState().chat.activeSlot).toBe(NEW)
    expect(navigateSpy).toHaveBeenCalledWith('/chat')
  })
})

describe('Alt+Shift+N', () => {
  function harness() {
    const store = makeStore(ORIGIN)
    renderHook(() => useShellKeyboard({
      toggleFocusMode: () => {}, toggleNav: () => {}, terminalEnabled: false, isPopout: false,
      isEmbed: false, terminalPoppedOut: false, activeSlotProject: undefined,
    }), { wrapper: wrapper(store) })
    return store
  }
  const press = () => fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true })

  it('leaves a user on Settings who went there during the create', async () => {
    goTo(`/chat?sid=${ORIGIN}`)
    harness()
    press()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    goTo('/settings')
    await act(async () => { held.resolve({ key: NEW }) })
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('brings a user who pressed it on Settings to the new chat', async () => {
    goTo('/settings')
    const store = harness()
    press()
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/chat'))
    expect(store.getState().chat.activeSlot).toBe(NEW)
  })

  it('adds no history entry of its own on the chat page', async () => {
    goTo(`/chat?sid=${ORIGIN}`)
    const store = harness()
    press()
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW))
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('focuses the new chat in a popout without navigating the popout', async () => {
    goTo(`/popout/chat/slug-a?sid=${ORIGIN}`)
    const store = harness()
    press()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    // The popout's URL sync names the new session.
    goTo(`/popout/chat/new-session?sid=${NEW}`)
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(focusSpy).toHaveBeenCalled())
    expect(store.getState().chat.activeSlot).toBe(NEW)
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('makes a session for each of two separate presses during one create', async () => {
    goTo(`/chat?sid=${ORIGIN}`)
    harness()
    press()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    press()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(2))
    await act(async () => { held.resolve({ key: NEW }) })
  })
})

describe('agent-switch notice expiry', () => {
  function harness() {
    const store = makeStore(ORIGIN)
    renderHook(() => useShellKeyboard({
      toggleFocusMode: () => {}, toggleNav: () => {}, terminalEnabled: false, isPopout: false,
      isEmbed: false, terminalPoppedOut: false, activeSlotProject: undefined,
    }), { wrapper: wrapper(store) })
    return store
  }
  afterEach(() => { vi.useRealTimers() })

  it('clears a plain notice after 6 s but keeps one that offers a new chat', () => {
    vi.useFakeTimers()
    const store = harness()
    act(() => { store.dispatch(setAgentSwitchNotice({ message: 'refused', error: true })) })
    act(() => { vi.advanceTimersByTime(6000) })
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
    act(() => { store.dispatch(setAgentSwitchNotice({ message: 'new chat?', newChatAgent: 'writer' })) })
    act(() => { vi.advanceTimersByTime(60000) })
    expect(store.getState().chat.agentSwitchNotice).toEqual({ message: 'new chat?', newChatAgent: 'writer' })
  })
})

describe('embed tab strip Shift+click "+"', () => {
  function mount(tabs?: string[], activeSlot: string | null = ORIGIN) {
    if (tabs) {
      sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(tabs))
      sessionStorage.setItem('kirocrew-embed-active-index', '0')
    }
    const store = makeStore(activeSlot)
    const entry = activeSlot ? `/embed/chat/${activeSlot}?sid=${activeSlot}` : '/embed/sessions'
    goTo(entry)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[entry]}>
            <EmbedTabStrip />
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    navigateSpy.mockClear()
    return store
  }
  const shiftPlus = () => fireEvent.click(screen.getByLabelText('New tab'), { shiftKey: true })

  const storedTabs = () => JSON.parse(sessionStorage.getItem('kirocrew-embed-tabs') ?? '[]') as string[]
  const storedIndex = () => Number(sessionStorage.getItem('kirocrew-embed-active-index'))

  it('opens the new tab when the user stayed on theirs', async () => {
    mount()
    shiftPlus()
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith(`/embed/chat/${NEW}?sid=${NEW}`))
    expect(storedTabs()).toEqual([ORIGIN, NEW])
    expect(storedIndex()).toBe(1)
  })

  it('treats a drag reorder of the user\'s own tab as staying on it', async () => {
    mount([ORIGIN, OTHER])
    shiftPlus()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    // jsdom lays every tab out at x=0, so a drop to the right of 0 lands the
    // dragged tab last: the user's tab moves from index 0 to index 1.
    const strip = screen.getAllByRole('tab')[0].parentElement as HTMLElement
    fireEvent.pointerDown(screen.getAllByRole('tab')[0], { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(strip, { clientX: 100, pointerId: 1 })
    fireEvent.pointerUp(strip, { clientX: 100, pointerId: 1 })
    await waitFor(() => expect(storedTabs()).toEqual([OTHER, ORIGIN]))
    expect(storedIndex()).toBe(1)
    navigateSpy.mockClear()
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith(`/embed/chat/${NEW}?sid=${NEW}`))
    expect(storedTabs()).toEqual([OTHER, ORIGIN, NEW])
    expect(storedIndex()).toBe(2)
  })

  it('fills the Sessions tab a user opened with plain "+" during the create', async () => {
    const store = mount()
    shiftPlus()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByLabelText('New tab'))
    await waitFor(() => expect(storedTabs()).toEqual([ORIGIN, '']))
    navigateSpy.mockClear()
    await act(async () => { held.resolve({ key: NEW }) })
    // No slot moved, so the create opens; the user's new Sessions tab takes it.
    expect(store.getState().chat.activeSlot).toBe(NEW)
    await waitFor(() => expect(storedTabs()).toEqual([ORIGIN, NEW]))
    expect(storedIndex()).toBe(1)
    expect(storedTabs().filter(s => s === NEW)).toHaveLength(1)
  })

  it('adds the tab behind a Sessions tab opened during the first create of a fresh embed', async () => {
    // No session was active yet, so the strip leaves the create's activation
    // to this callback: only the origin-tab verdict keeps the user's tab.
    mount([''], null)
    shiftPlus()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByLabelText('New tab'))
    await waitFor(() => expect(storedTabs()).toEqual(['', '']))
    navigateSpy.mockClear()
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(storedTabs()).toEqual(['', '', NEW]))
    expect(storedIndex()).toBe(1)
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('leaves a user who opened embedded Settings during the create on Settings', async () => {
    mount()
    shiftPlus()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    // The microphone dialog's "Open Settings" moves the route but keeps the
    // tab object and the active index.
    goTo('/embed/settings')
    navigateSpy.mockClear()
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(storedTabs()).toEqual([ORIGIN, NEW]))
    // The store opened NEW, so the strip highlights NEW's tab: Settings' Back
    // goes to NEW, and the highlight must name it. Only the navigate is held.
    expect(storedIndex()).toBe(1)
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('keeps the tab a user switched to during the create', async () => {
    const store = mount()
    shiftPlus()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    // A tab switch moves the address bar and the embedded chat with it.
    goTo(`/embed/chat/${OTHER}`)
    act(() => { store.dispatch(setActiveSlot(OTHER)) })
    navigateSpy.mockClear()
    await act(async () => { held.resolve({ key: NEW }) })
    expect(navigateSpy).not.toHaveBeenCalledWith(`/embed/chat/${NEW}?sid=${NEW}`)
    expect(store.getState().chat.activeSlot).toBe(OTHER)
    expect(storedTabs()).toContain(NEW)
  })
})

describe('a page that starts holding a draft during the create', () => {
  // The guard the page on screen registers: clean when the create starts, so
  // an ask made then would pass, and dirty by the time the create settles.
  let dirty = false
  const guard = vi.fn(() => !dirty)

  function guarded(store: ReturnType<typeof makeStore>) {
    const Inner = wrapper(store)
    return ({ children }: { children: ReactNode }) => (
      <Inner><NavigationLeaveGuardProvider>{children}</NavigationLeaveGuardProvider></Inner>
    )
  }

  beforeEach(() => {
    dirty = false
    guard.mockClear()
  })

  it('keeps a user on Settings when New session with token settles', async () => {
    const store = makeStore(null)
    goTo('/settings')
    const { result } = renderHook(() => {
      useRegisterNavigationLeaveGuard(guard)
      return usePaletteActions()
    }, { wrapper: guarded(store) })
    act(() => result.current.newSessionWithToken('$zzq-skill'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    dirty = true
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(guard).toHaveBeenCalledTimes(1))
    expect(navigateSpy).not.toHaveBeenCalled()
    expect(store.getState().chat.pendingInput).toBeNull()
    // The session itself was made and stays.
    expect(store.getState().chat.activeSlot).toBe(NEW)
  })

  it('keeps a user on Settings when Alt+Shift+N settles', async () => {
    const store = makeStore(ORIGIN)
    goTo('/settings')
    renderHook(() => {
      useRegisterNavigationLeaveGuard(guard)
      useShellKeyboard({
        toggleFocusMode: () => {}, toggleNav: () => {}, terminalEnabled: false, isPopout: false,
        isEmbed: false, terminalPoppedOut: false, activeSlotProject: undefined,
      })
    }, { wrapper: guarded(store) })
    fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true })
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    dirty = true
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(guard).toHaveBeenCalledTimes(1))
    expect(navigateSpy).not.toHaveBeenCalled()
    // No `focusSpy` check: an earlier test's composer focus is queued on an
    // animation frame and can land in this test.
    expect(store.getState().chat.activeSlot).toBe(NEW)
  })

  it('still brings the user to the chat when the page agrees to be left', async () => {
    const store = makeStore(ORIGIN)
    goTo('/settings')
    renderHook(() => {
      useRegisterNavigationLeaveGuard(guard)
      useShellKeyboard({
        toggleFocusMode: () => {}, toggleNav: () => {}, terminalEnabled: false, isPopout: false,
        isEmbed: false, terminalPoppedOut: false, activeSlotProject: undefined,
      })
    }, { wrapper: guarded(store) })
    fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true })
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/chat'))
    expect(guard).toHaveBeenCalledTimes(1)
  })
})
