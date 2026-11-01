// AppPopoutFrame is the window shell for a popped-out app: it mounts the app the
// way the dashboard does, registers as the live pop-out for its route, mirrors
// the app's name into the OS window title, carries a title bar with the app's
// name and the Return control, and
// hands any navigation that LEAVES the app to the main window instead of
// mounting it here. The window's router is mounted at /popout/app, so the
// routes in these tests are the app's own paths, as App.tsx registers them.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useEffect, useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { Link, MemoryRouter, Routes, Route, useNavigate, useLocation, useParams } from 'react-router-dom'
import { createTestStore, renderWithProviders } from './helpers'
import ErrorBoundary from '../components/ErrorBoundary'
import { ThemeProvider } from '../hooks/useTheme'
import { useAppDispatch } from '../store'
import { setActiveSlot, setPendingInput, switchSlot } from '../store/chatSlice'
import { ERROR_HANDOFF_KEY, consumeChatHandoff, handoffToChat } from '../utils/errorReport'

const popout = vi.hoisted(() => {
  const unregister = vi.fn()
  return {
    unregister,
    registerPopout: vi.fn(() => unregister),
    forwardToMain: vi.fn(),
    returnSelfToMain: vi.fn(),
    subscribe: () => () => {},
    // One stable instance: useSyncExternalStore re-renders on every snapshot
    // identity change, so a fresh Set per call would loop the control forever.
    getSnapshot: ((empty: ReadonlySet<string>) => () => empty)(new Set<string>()),
  }
})
vi.mock('../utils/appPopout', () => ({
  registerPopout: popout.registerPopout,
  forwardToMain: popout.forwardToMain,
  returnSelfToMain: popout.returnSelfToMain,
  subscribe: popout.subscribe,
  getSnapshot: popout.getSnapshot,
  openPopout: vi.fn(),
  focusPopout: vi.fn(),
}))
/** A builtin page that throws during render, for the crashed-app cases. */
const CRASH_MESSAGE = 'crashy page blew up'
function CrashyBuiltin(): never {
  throw new Error(CRASH_MESSAGE)
}
/** A route whose registry lookup itself throws, so the FRAME crashes rather than the app. */
const FRAME_CRASH_MESSAGE = 'registry lookup blew up'
vi.mock('../apps/builtinRegistry', () => ({
  hasBuiltinComponent: (route: string) => {
    if (route === '/boom') throw new Error(FRAME_CRASH_MESSAGE)
    return route === '/dev-fleet' || route === '/crashy'
  },
  getBuiltinApp: (path: string) => (path === '/crashy' ? { component: CrashyBuiltin, appId: 'crashy' } : undefined),
  builtinRefusalReason: () => undefined,
}))
const INSTALLED = [
  { name: 'dev-fleet', displayName: 'Dev Fleet', enabled: true, origin: 'builtin', manifest: { ui: { pages: [{ route: '/dev-fleet', label: 'Dev Fleet' }] } } },
  // A native builtin that is switched off: AppPage still redirects it to its page route.
  { name: 'aws-control', displayName: 'AWS Control', enabled: false, origin: 'builtin', manifest: { ui: { pages: [{ route: '/aws-control', label: 'AWS Control' }] } } },
  // A builtin that ships a UI bundle has no native surface: AppHost renders it, like an installed app.
  { name: 'bundled-tool', displayName: 'Bundled Tool', enabled: true, origin: 'builtin', manifest: { ui: { entry: 'dist/index.mjs', pages: [{ route: '/apps/bundled-tool', label: 'Bundled Tool' }] } } },
  { name: 'my-tool', displayName: 'My Tool', enabled: true, origin: 'external', manifest: { ui: { entry: 'dist/index.mjs', pages: [{ route: '/apps/my-tool', label: 'My Tool' }] } } },
  { name: 'sleepy-tool', displayName: 'Sleepy Tool', enabled: false, origin: 'external', manifest: { ui: { entry: 'dist/index.mjs', pages: [{ route: '/apps/sleepy-tool', label: 'Sleepy Tool' }] } } },
]
/** What the App Store's list answers; a case sets `isPending` / `isError` to stand in for a slow or failed list. */
const appsList = vi.hoisted(() => ({
  refetch: vi.fn(),
  state: { apps: [] as unknown[], error: null as unknown, isError: false, isLoadingError: false, isPending: false },
}))
vi.mock('../hooks/panelTabRegistry', () => ({
  useInstalledApps: () => ({ ...appsList.state, refetch: appsList.refetch }),
}))

/** The App SDK's launch slot on the window, as `openChat` writes it. */
function launchWindow() {
  return window as Window & { __mc_chat_launch?: { message?: string; agent?: string; slotKey?: string; autoSend?: boolean; ts?: number } }
}

/** How many times the fake builtin has mounted; reset per case. */
const builtinMounts = { count: 0 }

/** Stand-in for a builtin page: renders where it is and offers the exits an
 *  app really issues — its own sub-path, a chat deep link with flags, a
 *  session it activates first (Projects, Issue Radar), an SDK-style launch.
 *  Counts its mounts and holds a bit of local state, so a case can tell an app
 *  that stayed mounted from one that was torn down and mounted again. */
function FakeBuiltin() {
  const navigate = useNavigate()
  const dispatch = useAppDispatch()
  const { pathname, search } = useLocation()
  const [edits, setEdits] = useState(0)
  useEffect(() => { builtinMounts.count += 1 }, [])
  return (
    <div data-testid="builtin-page" data-path={pathname + search} data-edits={edits}>
      <button onClick={() => setEdits(n => n + 1)}>edit</button>
      <button onClick={() => navigate(-1)}>back</button>
      <Link to="/chat?sid=chat-link">chat link</Link>
      <Link to="/dev-fleet/usage?tab=x">usage link</Link>
      <button onClick={() => navigate('/dev-fleet/usage')}>deeper</button>
      <button onClick={() => navigate('/chat?autoSend=1&newSession=1')}>to chat</button>
      {/* Dev Fleet's QA launcher, as one handler: stage the prompt, then leave. */}
      <button onClick={() => { dispatch(setPendingInput('QA this worktree')); navigate('/chat?autoSend=1&newSession=1') }}>qa</button>
      <button onClick={() => navigate('/chat?sid=chat-7')}>open session</button>
      <button onClick={() => navigate('/chat')}>plain chat</button>
      <button onClick={() => { handoffToChat('diag prompt'); navigate('/chat') }}>ask the agent exit</button>
      <button onClick={() => navigate('/chat?history=1')}>older sessions</button>
      <button onClick={() => navigate('/settings', { replace: true })}>settings (replace)</button>
    </div>
  )
}
// The real route (and its boundary card) for the crashing page; the fake for the rest.
vi.mock('../apps/BuiltinAppRoute', async importOriginal => {
  const actual = await importOriginal<typeof import('../apps/BuiltinAppRoute')>()
  const Real = actual.default
  return {
    default: function BuiltinAppRouteStandIn() {
      return useParams().builtinApp === 'crashy' ? <Real /> : <FakeBuiltin />
    },
  }
})
vi.mock('../pages/AppPage', () => ({ default: () => <div data-testid="app-page" /> }))

import AppPopoutFrame, { AppPopoutHardExit, __resetForTests } from '../pages/AppPopoutFrame'

/** The three arms App.tsx renders the frame under. */
function frameRoutes(route: string) {
  return (
    <Routes>
      <Route path="/apps/:name" element={<AppPopoutFrame route={route} />} />
      <Route path="/:builtinApp/*" element={<AppPopoutFrame route={route} />} />
      <Route path="*" element={<AppPopoutFrame route={route} />} />
    </Routes>
  )
}

function renderFrame(route: string, at: string, store = createTestStore()) {
  return renderWithProviders(frameRoutes(route), { route: at, store })
}

/** The frame as App.tsx mounts it: the hard-exit sink beside the routes, each
 *  arm inside a route-level `ErrorBoundary`, plus a probe on the window's router. */
function renderAsAppTsx(route: string) {
  const arm = <ErrorBoundary><AppPopoutFrame route={route} /></ErrorBoundary>
  return renderWithProviders(
    <>
      <LocationProbe />
      <AppPopoutHardExit />
      <Routes>
        <Route path="/apps/:name" element={arm} />
        <Route path="/:builtinApp/*" element={arm} />
        <Route path="*" element={arm} />
      </Routes>
    </>,
    { route },
  )
}

/** The frame for /dev-fleet under a router mounted where the pop-out window
 *  mounts it (`/popout/app`); the helper's router has no basename, so this
 *  builds the same providers around one that does. */
function renderAtPopoutBasename(store = createTestStore()) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter basename="/popout/app" initialEntries={['/popout/app/dev-fleet']}>
            {frameRoutes('/dev-fleet')}
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** Stand-in for the shell's soft navigator: a navigate from OUTSIDE the frame,
 *  which the frame's pinned navigator never sees. */
function ShellNavigator() {
  const navigate = useNavigate()
  return <button onClick={() => navigate('/chat')}>shell soft navigate</button>
}

/** Where the window's router is, and which history entry: a step back or a
 *  replace changes the key even when the path is the same. */
function LocationProbe() {
  const { pathname, key } = useLocation()
  return <div data-testid="location" data-path={pathname} data-key={key} />
}

beforeEach(() => {
  popout.registerPopout.mockClear()
  popout.unregister.mockClear()
  popout.forwardToMain.mockClear()
  popout.returnSelfToMain.mockClear()
  appsList.state = { apps: INSTALLED, error: null, isError: false, isLoadingError: false, isPending: false }
  appsList.refetch.mockClear()
  __resetForTests()
  builtinMounts.count = 0
  sessionStorage.clear()
  delete launchWindow().__mc_chat_launch
  document.title = ''
})

afterEach(() => {
  vi.useRealTimers()
})

/** The one hand-off the frame made, with the fields a case does not name left undefined. */
function handedOff() {
  expect(popout.forwardToMain).toHaveBeenCalledTimes(1)
  return popout.forwardToMain.mock.calls[0][0]
}

describe('AppPopoutFrame', () => {
  it('mounts a builtin app through the builtin route, under a title bar carrying its name and Return', () => {
    renderFrame('/dev-fleet', '/dev-fleet')
    expect(screen.getByTestId('builtin-page')).toBeInTheDocument()
    const bar = screen.getByTestId('app-popout-title-bar')
    expect(bar).toHaveTextContent('Dev Fleet')
    const ret = screen.getByRole('button', { name: 'Return to main window' })
    expect(bar.contains(ret)).toBe(true)
    fireEvent.click(ret)
    expect(popout.returnSelfToMain).toHaveBeenCalledTimes(1)
  })

  it('puts the title bar above the app’s scroll container, not inside it', () => {
    renderFrame('/dev-fleet', '/dev-fleet')
    const bar = screen.getByTestId('app-popout-title-bar')
    const main = screen.getByRole('main')
    expect(main.contains(bar)).toBe(false)
    expect(bar.parentElement).toBe(main.parentElement)
    expect(bar.compareDocumentPosition(main) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(main.contains(screen.getByTestId('builtin-page'))).toBe(true)
  })

  it('mounts an installed app through the AppHost page', () => {
    renderFrame('/apps/my-tool', '/apps/my-tool')
    expect(screen.getByTestId('app-page')).toBeInTheDocument()
  })

  it('registers as the live pop-out for its route and unregisters on unmount', () => {
    const { unmount } = renderFrame('/dev-fleet', '/dev-fleet')
    expect(popout.registerPopout).toHaveBeenCalledWith('/dev-fleet')
    unmount()
    expect(popout.unregister).toHaveBeenCalled()
  })

  it('titles the window after the app, from the installed-apps list', () => {
    renderFrame('/dev-fleet', '/dev-fleet')
    expect(document.title).toBe('Dev Fleet — Kiro Crew')
  })

  it('lets the app navigate its own sub-paths in place', () => {
    renderFrame('/dev-fleet', '/dev-fleet')
    fireEvent.click(screen.getByRole('button', { name: 'deeper' }))
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet/usage')
    expect(popout.forwardToMain).not.toHaveBeenCalled()
  })

  it('steps back through the app’s own history entries with navigate(-1)', () => {
    renderFrame('/dev-fleet', '/dev-fleet')
    fireEvent.click(screen.getByRole('button', { name: 'deeper' }))
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet/usage')
    fireEvent.click(screen.getByRole('button', { name: 'back' }))
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet')
    expect(popout.forwardToMain).not.toHaveBeenCalled()
  })

  it('keeps the app mounted, with its state, across an exit', () => {
    renderFrame('/dev-fleet', '/dev-fleet')
    fireEvent.click(screen.getByRole('button', { name: 'edit' }))
    expect(builtinMounts.count).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: 'to chat' }))
    expect(handedOff()).toMatchObject({ path: '/chat', search: '?autoSend=1&newSession=1' })
    // Not torn down and mounted again: the same instance, still holding its edit.
    expect(builtinMounts.count).toBe(1)
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-edits', '1')
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet')
  })

  it('intercepts a <Link> out of the app like a navigate', () => {
    renderFrame('/dev-fleet', '/dev-fleet')
    fireEvent.click(screen.getByRole('link', { name: 'chat link' }))
    expect(handedOff()).toMatchObject({ path: '/chat', search: '?sid=chat-link', slotKey: 'chat-link' })
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet')
    expect(builtinMounts.count).toBe(1)
  })

  it('renders an exit link’s href as the main dashboard’s URL and an in-app link’s as the router’s', () => {
    renderFrame('/dev-fleet', '/dev-fleet')
    expect(screen.getByRole('link', { name: 'chat link' })).toHaveAttribute('href', '/chat?sid=chat-link')
    expect(screen.getByRole('link', { name: 'usage link' })).toHaveAttribute('href', '/dev-fleet/usage?tab=x')
  })

  it('under the window’s /popout/app basename, an exit link’s href leaves the pop-out prefix; an in-app link keeps it', () => {
    renderAtPopoutBasename()
    expect(screen.getByRole('link', { name: 'chat link' })).toHaveAttribute('href', '/chat?sid=chat-link')
    expect(screen.getByRole('link', { name: 'usage link' })).toHaveAttribute('href', '/popout/app/dev-fleet/usage?tab=x')
  })

  it('a modifier-click on an exit link is left to the browser: no hand-off, no navigation here', () => {
    renderAtPopoutBasename()
    const link = screen.getByRole('link', { name: 'chat link' })
    // Last in the bubble path, after the router's handler: records whether the
    // router left the click to the browser, then cancels it (jsdom cannot
    // follow an href).
    let leftToBrowser: boolean | null = null
    const last = (e: MouseEvent) => { leftToBrowser = !e.defaultPrevented; e.preventDefault() }
    window.addEventListener('click', last)
    try {
      fireEvent.click(link, { metaKey: true })
    } finally {
      window.removeEventListener('click', last)
    }
    expect(leftToBrowser).toBe(true)
    expect(link).toHaveAttribute('href', '/chat?sid=chat-link')
    expect(popout.forwardToMain).not.toHaveBeenCalled()
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet')
  })

  it('under the window’s /popout/app basename, judges app-space paths: sub-paths pass, exits are handed off without it', () => {
    renderAtPopoutBasename()
    fireEvent.click(screen.getByRole('button', { name: 'deeper' }))
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet/usage')
    expect(popout.forwardToMain).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'open session' }))
    expect(handedOff()).toMatchObject({ path: '/chat', search: '?sid=chat-7', slotKey: 'chat-7' })
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet/usage')
    expect(builtinMounts.count).toBe(1)
  })

  it('a committed exit from outside the frame (the shell’s soft navigator) is still handed off, and the window steps back', () => {
    const store = createTestStore()
    act(() => { store.dispatch(setPendingInput('staged')) })
    renderWithProviders(<><ShellNavigator />{frameRoutes('/dev-fleet')}</>, { route: '/dev-fleet', store })
    fireEvent.click(screen.getByRole('button', { name: 'shell soft navigate' }))
    expect(handedOff()).toEqual({
      path: '/chat',
      search: undefined,
      slotKey: undefined,
      pendingInput: 'staged',
      chatLaunch: undefined,
    })
    expect(store.getState().chat.pendingInput).toBeNull()
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet')
  })

  it('reads a prompt staged in the same handler as the exit (dispatch, then navigate)', () => {
    const store = createTestStore()
    renderFrame('/dev-fleet', '/dev-fleet', store)
    fireEvent.click(screen.getByRole('button', { name: 'qa' }))
    expect(handedOff()).toMatchObject({ path: '/chat', search: '?autoSend=1&newSession=1', pendingInput: 'QA this worktree' })
    expect(store.getState().chat.pendingInput).toBeNull()
    expect(builtinMounts.count).toBe(1)
  })

  it('hands a chat exit to the main window — path, flags and the staged input — and stays on the app', () => {
    const store = createTestStore()
    renderFrame('/dev-fleet', '/dev-fleet', store)
    act(() => { store.dispatch(setPendingInput('Open a session on this worktree')) })
    fireEvent.click(screen.getByRole('button', { name: 'to chat' }))
    expect(handedOff()).toEqual({
      path: '/chat',
      search: '?autoSend=1&newSession=1',
      slotKey: undefined,
      pendingInput: 'Open a session on this worktree',
      chatLaunch: undefined,
    })
    // Consumed here: it was staged for a page this window will not mount.
    expect(store.getState().chat.pendingInput).toBeNull()
    // Still on the app: the exit never became this window's location.
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet')
  })

  it('forwards staged error prompts to main and spends the pop-out queue', () => {
    renderFrame('/dev-fleet', '/dev-fleet')
    fireEvent.click(screen.getByRole('button', { name: 'ask the agent exit' }))
    expect(handedOff()).toEqual({
      path: '/chat',
      search: undefined,
      slotKey: undefined,
      pendingInput: undefined,
      errorHandoffs: ['diag prompt'],
      chatLaunch: undefined,
    })
    expect(consumeChatHandoff()).toBeNull()
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet')
  })

  it('names the session a chat deep link carries so main switches to it', () => {
    renderFrame('/dev-fleet', '/dev-fleet')
    fireEvent.click(screen.getByRole('button', { name: 'open session' }))
    expect(handedOff()).toEqual({
      path: '/chat',
      search: '?sid=chat-7',
      slotKey: 'chat-7',
      pendingInput: undefined,
      chatLaunch: undefined,
    })
    expect(screen.getByTestId('builtin-page')).toBeInTheDocument()
  })

  it('carries the session the app activated before a plain /chat exit (switchSlot, then navigate)', () => {
    const store = createTestStore()
    renderFrame('/dev-fleet', '/dev-fleet', store)
    // What Projects / Issue Radar do to open a session: the switch runs in THIS
    // window's store, where no chat page will ever show it.
    act(() => { store.dispatch(switchSlot.pending('req-1', 'chat-9')) })
    fireEvent.click(screen.getByRole('button', { name: 'plain chat' }))
    expect(handedOff().slotKey).toBe('chat-9')
  })

  it('carries a newly activated session before a plain /chat exit', () => {
    const store = createTestStore()
    renderFrame('/dev-fleet', '/dev-fleet', store)
    // createSlot.fulfilled reaches the frame through this same activeSlot
    // transition; the direct action keeps the fixture independent of thunk meta.
    act(() => { store.dispatch(setActiveSlot('chat-new')) })
    fireEvent.click(screen.getByRole('button', { name: 'plain chat' }))
    expect(handedOff().slotKey).toBe('chat-new')
  })

  it('spends the activated session on the exit that carries it; a later exit that activated nothing carries nothing', () => {
    const store = createTestStore()
    renderFrame('/dev-fleet', '/dev-fleet', store)
    act(() => { store.dispatch(switchSlot.pending('req-1', 'chat-9')) })
    fireEvent.click(screen.getByRole('button', { name: 'plain chat' }))
    expect(handedOff().slotKey).toBe('chat-9')
    popout.forwardToMain.mockClear()
    // Issue Radar's "older sessions": main stays on whatever it shows.
    fireEvent.click(screen.getByRole('button', { name: 'older sessions' }))
    expect(handedOff()).toMatchObject({ path: '/chat', search: '?history=1', slotKey: undefined })
  })

  it('a same-session re-open is a new switch, and is carried again', () => {
    const store = createTestStore()
    renderFrame('/dev-fleet', '/dev-fleet', store)
    act(() => { store.dispatch(switchSlot.pending('req-1', 'chat-9')) })
    fireEvent.click(screen.getByRole('button', { name: 'plain chat' }))
    popout.forwardToMain.mockClear()
    // Each dispatch mints a request id; `activeSlot` alone would not have changed.
    act(() => { store.dispatch(switchSlot.pending('req-2', 'chat-9')) })
    fireEvent.click(screen.getByRole('button', { name: 'plain chat' }))
    expect(handedOff().slotKey).toBe('chat-9')
  })

  it('an exit to another page spends the switch without carrying it', () => {
    const store = createTestStore()
    renderFrame('/dev-fleet', '/dev-fleet', store)
    act(() => { store.dispatch(switchSlot.pending('req-1', 'chat-9')) })
    fireEvent.click(screen.getByRole('button', { name: 'settings (replace)' }))
    expect(handedOff()).toMatchObject({ path: '/settings', slotKey: undefined })
    popout.forwardToMain.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'plain chat' }))
    expect(handedOff().slotKey).toBeUndefined()
  })

  it('claims an App SDK launch off this window and carries it, so main can re-stage it', () => {
    // A pinned clock: the claim keeps a launch only while it is fresh (ten seconds).
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(1_700_000_000_000)
    renderFrame('/dev-fleet', '/dev-fleet')
    // What the SDK's `openChat({ message, agent })` does in the app's window: stage
    // the intent on the window, then navigate to /chat.
    launchWindow().__mc_chat_launch = { message: 'Review this', agent: 'reviewer', ts: 1_700_000_000_000 }
    fireEvent.click(screen.getByRole('button', { name: 'plain chat' }))
    expect(handedOff()).toEqual({
      path: '/chat',
      search: undefined,
      slotKey: undefined,
      pendingInput: undefined,
      chatLaunch: { message: 'Review this', agent: 'reviewer' },
    })
    // Consumed: nothing in this window will read it, and the next exit must not re-send it.
    expect(launchWindow().__mc_chat_launch).toBeUndefined()
  })

  it('a replace-navigation out never changes this window’s location', () => {
    renderFrame('/dev-fleet', '/dev-fleet')
    fireEvent.click(screen.getByRole('button', { name: 'deeper' }))
    fireEvent.click(screen.getByRole('button', { name: 'settings (replace)' }))
    expect(handedOff()).toEqual({
      path: '/settings',
      search: undefined,
      slotKey: undefined,
      pendingInput: undefined,
      chatLaunch: undefined,
    })
    expect(screen.getByTestId('builtin-page')).toHaveAttribute('data-path', '/dev-fleet/usage')
  })

  it('refuses /apps/<name> for an app the store lists at its own route (a native builtin), instead of handing AppPage’s redirect to main', () => {
    // `/apps/dev-fleet` is where AppPage would redirect to `/dev-fleet` — an exit
    // this window would hand to main, leaving nothing on screen here.
    renderFrame('/apps/dev-fleet', '/apps/dev-fleet')
    expect(screen.queryByTestId('app-page')).toBeNull()
    expect(screen.getByText('No app at /apps/dev-fleet')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Return to main window' })).toBeInTheDocument()
    expect(popout.forwardToMain).not.toHaveBeenCalled()
  })

  it('refuses /apps/<name> for a DISABLED native builtin too: AppPage redirects it regardless', () => {
    renderFrame('/apps/aws-control', '/apps/aws-control')
    expect(screen.queryByTestId('app-page')).toBeNull()
    expect(screen.getByText('No app at /apps/aws-control')).toBeInTheDocument()
    expect(popout.forwardToMain).not.toHaveBeenCalled()
  })

  it('refuses /apps/<name> the store does not list, once the list has answered', () => {
    renderFrame('/apps/nothing-here', '/apps/nothing-here')
    expect(screen.queryByTestId('app-page')).toBeNull()
    expect(screen.getByText('No app at /apps/nothing-here')).toBeInTheDocument()
  })

  it('mounts a builtin that ships a UI bundle through the AppHost page, as the dashboard does', () => {
    renderFrame('/apps/bundled-tool', '/apps/bundled-tool')
    expect(screen.getByTestId('app-page')).toBeInTheDocument()
    expect(document.title).toBe('Bundled Tool — Kiro Crew')
  })

  it('mounts a disabled installed app: AppHost shows its disabled notice, as in the dashboard', () => {
    renderFrame('/apps/sleepy-tool', '/apps/sleepy-tool')
    expect(screen.getByTestId('app-page')).toBeInTheDocument()
  })

  it('mounts no app on the /apps arm until the list has answered, and shows the loading line', () => {
    appsList.state = { apps: [], error: null, isError: false, isLoadingError: false, isPending: true }
    renderFrame('/apps/my-tool', '/apps/my-tool')
    expect(screen.queryByTestId('app-page')).toBeNull()
    expect(screen.queryByText(/No app at/)).toBeNull()
    // The same treatment AppPage gives its own load, not a blank window.
    expect(screen.getByTestId('app-popout-list-loading')).toHaveTextContent('Loading app')
    // Return is there regardless: the window is never a dead end.
    expect(screen.getByRole('button', { name: 'Return to main window' })).toBeInTheDocument()
  })

  it('a list that failed to load gets a Retry notice on the /apps arm, never AppPage (whose redirect would be handed to main)', () => {
    appsList.state = { apps: [], error: new Error('503 from /api/apps'), isError: true, isLoadingError: true, isPending: false }
    renderFrame('/apps/my-tool', '/apps/my-tool')
    expect(screen.queryByTestId('app-page')).toBeNull()
    expect(screen.getByText('Could not load the app list')).toBeInTheDocument()
    expect(screen.getByText('503 from /api/apps')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(popout.forwardToMain).not.toHaveBeenCalled()
    // Retry asks for the list again; Return is still there.
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(appsList.refetch).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Return to main window' })).toBeInTheDocument()
  })

  it('a failed list does not touch a builtin route: the registry, not the list, judges that arm', () => {
    appsList.state = { apps: [], error: new Error('503'), isError: true, isLoadingError: true, isPending: false }
    renderFrame('/dev-fleet', '/dev-fleet')
    expect(screen.getByTestId('builtin-page')).toBeInTheDocument()
    expect(screen.queryByText('Could not load the app list')).toBeNull()
  })

  it('a failed REFRESH of a list that did load keeps the app on screen (the cached list still judges)', () => {
    // A background refetch that fails (a focus during a gateway restart) raises
    // isError while the cached apps stay served; the running app must not be
    // torn down for it.
    appsList.state = { apps: INSTALLED, error: new Error('socket hang up'), isError: true, isLoadingError: false, isPending: false }
    renderFrame('/apps/my-tool', '/apps/my-tool')
    expect(screen.getByTestId('app-page')).toBeInTheDocument()
    expect(screen.queryByText('Could not load the app list')).toBeNull()
  })

  // A link can percent-encode the app's segment. The router hands the params
  // back decoded, the route stays raw, and each window must still settle on its
  // own location: one registration, no hand-off, no navigation (a new
  // `location.key` would mean the frame stepped itself back).
  for (const route of ['/apps/a%20b', '/apps/foo%2Fbar', '/dev%2Dfleet']) {
    it(`settles on an encoded route (${route}) without handing off or navigating`, () => {
      renderWithProviders(<><LocationProbe />{frameRoutes(route)}</>, { route })
      expect(screen.getByTestId('location')).toHaveAttribute('data-path', route)
      expect(screen.getByTestId('location')).toHaveAttribute('data-key', 'default')
      expect(popout.forwardToMain).not.toHaveBeenCalled()
      expect(popout.registerPopout).toHaveBeenCalledTimes(1)
      expect(popout.registerPopout).toHaveBeenCalledWith(route)
      // The frame's own shell, not the hand-off (which renders nothing).
      expect(screen.getByRole('button', { name: 'Return to main window' })).toBeInTheDocument()
    })
  }

  it('a hard Ask-the-agent from a crashed builtin app hands the prompt to main and never navigates this window', () => {
    const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    const silenced = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      renderAsAppTsx('/crashy')
      // BuiltinAppRoute's own boundary card, under the frame's title bar.
      expect(screen.getByText(CRASH_MESSAGE)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Return to main window' })).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Ask the agent' }))
      expect(handedOff()).toMatchObject({
        path: '/chat',
        errorHandoffs: [expect.stringContaining(CRASH_MESSAGE)],
      })
      expect(handedOff().errorHandoffs).toHaveLength(1)
      // Spent here, and nothing loaded or navigated in this window.
      expect(sessionStorage.getItem(ERROR_HANDOFF_KEY)).toBeNull()
      expect(assign).not.toHaveBeenCalled()
      expect(screen.getByTestId('location')).toHaveAttribute('data-path', '/crashy')
      expect(screen.getByTestId('location')).toHaveAttribute('data-key', 'default')
    } finally {
      assign.mockRestore()
      silenced.mockRestore()
    }
  })

  it('a hard Ask-the-agent from a crashed FRAME (App.tsx’s route boundary) is handed to main the same way', () => {
    const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    const silenced = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      renderAsAppTsx('/boom')
      // The frame itself threw, so no title bar: the boundary card is all there is.
      expect(screen.queryByTestId('app-popout-title-bar')).toBeNull()
      expect(screen.getByText(FRAME_CRASH_MESSAGE)).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Ask the agent' }))
      expect(handedOff()).toMatchObject({
        path: '/chat',
        errorHandoffs: [expect.stringContaining(FRAME_CRASH_MESSAGE)],
      })
      expect(sessionStorage.getItem(ERROR_HANDOFF_KEY)).toBeNull()
      expect(assign).not.toHaveBeenCalled()
      expect(screen.getByTestId('location')).toHaveAttribute('data-key', 'default')
    } finally {
      assign.mockRestore()
      silenced.mockRestore()
    }
  })

  it('refuses a deep link that names no app, with Return still offered', () => {
    renderFrame('/chat', '/chat')
    expect(screen.queryByTestId('builtin-page')).toBeNull()
    expect(screen.getByText('No app at /chat')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Return to main window' })).toBeInTheDocument()
    // Nothing to forward: this window IS at its (bogus) route.
    expect(popout.forwardToMain).not.toHaveBeenCalled()
    expect(document.title).toBe('App — Kiro Crew')
    // The title bar names the window the same way when no app does.
    expect(screen.getByTestId('app-popout-title-bar')).toHaveTextContent('App')
  })
})

describe('the shell’s soft navigator in an app pop-out', () => {
  // Read from source, like App.topbarAndShortcuts does for the shell’s shape:
  // standing up useAppShell for a pop-out window would mean faking every
  // window-level flag it keys on. The rule is the point: an "Ask the agent"
  // control in an app pop-out must reach the router (and so this frame’s
  // hand-off) instead of the hard `location.assign` that would turn the window
  // into a second dashboard. Chat and artifact pop-outs keep the hard path.
  it('is installed for an app pop-out and skipped for the other pop-outs and embeds', () => {
    const src = readFileSync(join(__dirname, '..', 'shell', 'appShell.tsx'), 'utf8')
    const effect = src.slice(src.indexOf('installSoftNavigate(navigate') - 400, src.indexOf('installSoftNavigate(navigate'))
    expect(effect).toContain('if ((isPopout && !appPopoutRoute) || isEmbed) return')
  })
})
