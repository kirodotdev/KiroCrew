/**
 * Coverage-directed tests for the ChatPage handlers that are only reachable
 * through a transcript row's own affordances or through a window event — the two
 * entry points none of the ~35 existing ChatPage suites drive.
 *
 * Three cold areas, all through a real `render(<ChatPage />)`:
 *
 *  1. The row callbacks ChatPage hands to AssistantMessage: `handleFork` (all
 *     three outcomes plus the cold-config refetch), `handlePlanFromHere`,
 *     `handleQuote`, `handleAsk`, `handleRegenerate` (including the snapshot
 *     rollback on a failed request), `handleSpeak` (both voice states) and
 *     `handleApplyPlan`'s failure path. AssistantMessage is stubbed as a prop
 *     recorder so the callbacks can be invoked directly — the card's own
 *     rendering is covered by AssistantMessage.test.tsx.
 *
 *  2. The window-event listeners: `mc-config-changed` (chat-settings reload),
 *     `toggle-pin-chat-sidebar`, `kirocrew-tool-call` (a shell browse must NOT
 *     auto-open the panel), and `mc:run-in-terminal` (both the non-string guard and the
 *     PTY-never-connects timeout that reports failure back to the code block).
 *
 *  3. The welcome-state "Continue a previous chat?" suggestion list and
 *     `handleResumeSession`, reached by pre-filling the composer through the
 *     widget bridge and letting the 300 ms history-query debounce fire.
 *
 * happy-dom has no layout, so the virtualizer is stubbed to mount every item
 * (the technique ChatPageCoverage.test.tsx uses). Nothing else about the page is
 * faked: grouping, the render dispatch and the handlers run for real.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent, within, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { RUN_IN_TERMINAL_READY_DEADLINE_MS, RUN_IN_TERMINAL_OPENING_GRACE_MS } from '../utils/fenceShell'
import { useBottomTerminal, __resetBottomTerminal, removeTab } from '../hooks/useBottomTerminal'
import { registerTerminalWs, unregisterTerminalWs } from '../utils/terminalRegistry'

// The run-in-terminal rollback consults the popout probe to avoid tearing a
// session out of a popped-out panel; the flag lets each test pick the state.
let mockTerminalPopoutOpen = false
vi.mock('../utils/terminalPopout', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/terminalPopout')>()),
  isPopoutOpen: () => mockTerminalPopoutOpen,
}))
const disposeTerminalSessionSpy = vi.hoisted(() => vi.fn())
vi.mock('../components/CliPanel', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/CliPanel')>()
  return {
    ...actual,
    disposeTerminalSession: (sessionId: string) => {
      disposeTerminalSessionSpy(sessionId)
      actual.disposeTerminalSession(sessionId)
    },
  }
})
import { ThemeProvider } from '../hooks/useTheme'
import { store as appStore } from '../store'
import { setVoicePlaying, switchSlot } from '../store/chatSlice'
import { sseDisconnected } from '../store/dashboardSlice'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

// --- Prop recorders ---------------------------------------------------------

interface AssistantProps {
  content: string
  timestamp?: string
  onFork?: (visibleIndex: number) => void | Promise<void>
  onPlanFromHere?: (visibleIndex: number) => void | Promise<void>
  onQuote?: (text: string, rect: DOMRect) => void
  onAsk?: (text: string) => void
  onSpeak?: (content: string) => void
  onRegenerate?: () => void
  onApplyPlan?: (steps: never[]) => Promise<boolean>
  forkIndex?: number
}
let assistantProps: AssistantProps | null = null

interface InputProps { value: string; onChange: (v: string) => void; onScreenshot?: () => void }
let inputProps: InputProps | null = null

vi.mock('../pages/chat', async () => {
  const React = await import('react')
  return {
    ChatFooter: () => null,
    McpInfoButton: () => null,
    PinnedPrompt: () => null,
    UserMessage: ({ content }: { content: string }) =>
      React.createElement('div', { 'data-testid': 'user-msg' }, content),
    AssistantMessage: (props: AssistantProps) => {
      assistantProps = props
      return React.createElement('div', { 'data-testid': 'assistant-msg' }, props.content)
    },
  }
})

// A minimal controlled composer. The real one is covered by ChatInput's own
// suite, but its textarea has to EXIST because `handleQuote` and the widget
// bridge look it up by aria-label to reveal the pre-filled text. The module's
// named exports are kept: `effortLabel` is imported from here by the model and
// reasoning-effort dropdowns that ChatPage also renders.
vi.mock('../components/ChatInput', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/ChatInput')>()
  const React = await import('react')
  return {
    ...actual,
    default: (props: InputProps) => {
      inputProps = props
      return React.createElement('textarea', {
        'aria-label': 'Message input',
        value: props.value,
        onChange: (e: { target: { value: string } }) => props.onChange(e.target.value),
      })
    },
  }
})

vi.mock('../components/FlyingQuote', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'flying-quote' }) }
})

// The split grid is stubbed to a prop recorder: what matters here is the
// `openSideChat` capability ChatPage hands its panes (and when it withholds
// it), not the grid's own layout, which SessionGridView's suite covers.
interface GridProps { openSideChat?: (slot: string) => boolean | void | Promise<boolean | void> }
let gridProps: GridProps | null = null
vi.mock('../components/SessionGridView', async () => {
  const React = await import('react')
  return {
    default: (props: GridProps) => {
      gridProps = props
      return React.createElement('div', { 'data-testid': 'session-grid' })
    },
  }
})

interface ProjectPickerProps { onSelect: (path: string) => void }
let projectPickerProps: ProjectPickerProps | null = null
vi.mock('../components/ProjectPicker', () => ({
  default: (props: ProjectPickerProps) => { projectPickerProps = props; return null },
}))

// --- Child components stubbed to keep the render tree small ------------------
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content?: string }) => content ?? null,
}))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null, DefaultAgentRow: () => null, ManageAgentsFooter: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
interface WelcomeProps {
  onSwitchMode?: (mode: 'persistent' | 'incognito' | 'temporary') => void | Promise<void>
}
let welcomeProps: WelcomeProps | null = null
vi.mock('../components/WelcomeView', async () => {
  const React = await import('react')
  return {
    default: (props: WelcomeProps) => {
      welcomeProps = props
      return React.createElement('div', { 'data-testid': 'welcome' })
    },
  }
})
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))

// Mutable so the `mc-config-changed` test can flip a setting and prove the
// listener re-reads it (the reload is dedupe-guarded on a JSON compare, so the
// value has to actually change).
let chatSettings: Record<string, unknown> = { contentWidth: 'compact' }
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ ...chatSettings }),
  CONTENT_WIDTH: {
    compact: { messages: '900px', input: '916px' },
    comfortable: { messages: '84%', input: '85%' },
    full: { messages: '92%', input: '93%' },
  },
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({
  useFilteredDropdown: () => ({
    filtered: [], query: '', setQuery: vi.fn(),
    selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn(),
  }),
}))
vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }),
  voiceInputSupported: false,
}))

// Mounts every display item so ChatPage's own row renderer runs for real.
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: (opts: { items?: unknown[]; getKey?: (it: unknown, i: number) => string }) => {
    const items = opts.items ?? []
    return {
      virtualItems: items.map((data, index) => ({
        key: opts.getKey ? opts.getKey(data, index) : String(index),
        index,
        mounted: true,
        data,
      })),
      isAtBottom: true,
      getFollow: () => true,
      scrollToBottom: vi.fn(),
      mountIndex: vi.fn(() => false),
      farmIsMeasured: () => true,
      farmRecord: vi.fn(() => true),
      measureRef: () => () => {},
      topSentinelRef: { current: null },
      bottomSentinelRef: { current: null },
      offsetBefore: 0,
      offsetAfter: 0,
      totalHeight: 0,
    }
  },
}))

vi.mock('../api/pins', () => ({
  PIN_PREVIEW_INPUT_MAX_CHARS: 4096,
  pinsApi: {
    list: vi.fn().mockResolvedValue({ pins: [] }),
    create: vi.fn().mockResolvedValue({}),
    remove: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {}
/** Seed (or fetch) the mock for one api method so a test can assert it was
 *  never called — reading it off `apiMocks` lazily would report `undefined`. */
const apiSpy = (name: string) => {
  if (!(name in apiMocks)) apiMocks[name] = vi.fn().mockResolvedValue({})
  return apiMocks[name]
}
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop: string) => {
      if (!(prop in apiMocks)) {
        apiMocks[prop] = vi.fn().mockResolvedValue(
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
        )
      }
      return apiMocks[prop]
    },
  }),
  fileReadUrl: (p: string) => `/api/file?path=${encodeURIComponent(p)}`,
  SEARCH_MIN_CHARS: 2,
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({
  ok: true, status: 200, text: () => Promise.resolve(''), json: () => Promise.resolve({}),
}) as never

import ChatPage from '../pages/ChatPage'
import { readSideChatDraft } from '../chat-core/composer/sideChatDrafts'
import { ApiError } from '../api/apiError'

// --- Fixtures ---------------------------------------------------------------

const SLOT = {
  key: 'chat-1', title: 'chat-1', messages: 0, running: false,
  mode: '', created: '', last_ts: '',
}
const OTHER_SLOT = { ...SLOT, key: 'chat-2', title: 'chat-2' }
const REMOTE_SLOT = {
  ...SLOT,
  memory_mode: 'temporary',
  instance_id: 'crew-remote-1',
}

interface HistorySession { key: string; title: string; created: string; messages: number }

const msg = (role: string, content: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  role, content, cls: '', ...extra,
})

interface RenderOpts {
  /** Extra preloaded `chat` slice fields, merged over the reducer's own initial state. */
  chat?: Record<string, unknown>
  /** Past sessions `api.sessions` yields — ChatPage fetches them on mount, so a
   *  preloaded `chat.history` would be overwritten before the first paint. */
  sessions?: HistorySession[]
  /** The slot list, both preloaded and what `api.chatSlots` yields (ChatPage
   *  refetches it on mount). Default: `chat-1` alone. A switch to a key the
   *  list does not hold is undone by the page itself — its mode-guard clears
   *  the active slot and the auto-select falls back to the first known one. */
  slots?: (typeof SLOT)[]
}

function renderChatPage(messages: ChatMessage[], opts: RenderOpts = {}) {
  const { chat = {}, sessions = [], slots = [SLOT] } = opts
  apiMocks.chatSlots = vi.fn().mockResolvedValue(slots)
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({
    messages, has_more: false, total: messages.length,
  })
  apiMocks.sessions = vi.fn().mockResolvedValue({ sessions, has_more: false })
  // Spread the reducers' own initial state: RTK's preloadedState REPLACES a
  // slice rather than merging, so a hand-rolled literal drops keys the reducers
  // then mutate blindly (`activityTabRequest += 1` on an absent key).
  const base = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...base.dashboard,
      status: { platform: 'darwin' } as unknown as RootState['dashboard']['status'],
      connected: true,
      slots: slots as unknown as RootState['dashboard']['slots'],
    },
    chat: {
      ...base.chat,
      activeSlot: 'chat-1',
      ...chat,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat/chat-1']}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage mode="" />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  if (messages.length) {
    act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: messages }) })
  }
  return { store }
}

/** Renders one user + one assistant row and waits for the card to mount. */
async function renderTurn(opts: RenderOpts = {}) {
  const out = renderChatPage([
    msg('user', 'what changed?', { ts: '2026-08-12T07:00:00Z' }),
    msg('assistant', 'two files changed', { ts: '2026-08-12T07:00:05Z' }),
  ], opts)
  await waitFor(() => expect(assistantProps).not.toBeNull())
  return out
}

const makeAlertSpy = () => vi.spyOn(window, 'alert').mockImplementation(() => {})
let alertSpy: ReturnType<typeof makeAlertSpy>

beforeEach(() => {
  assistantProps = null
  inputProps = null
  projectPickerProps = null
  gridProps = null
  welcomeProps = null
  chatSettings = { contentWidth: 'compact' }
  localStorage.clear()
  sessionStorage.clear()
  for (const k of Object.keys(apiMocks)) delete apiMocks[k]
  disposeTerminalSessionSpy.mockClear()
  alertSpy = makeAlertSpy()
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  alertSpy.mockRestore()
})

describe('Welcome recreation preserves remote execution', () => {
  it('carries instanceId through a memory mode change', async () => {
    apiSpy('dashboardConfig').mockResolvedValue({ default_memory_mode: 'persistent' })
    apiSpy('createChatSlot').mockResolvedValue({
      ...REMOTE_SLOT,
      key: 'chat-new',
      memory_mode: 'persistent',
    })
    apiSpy('deleteChatSlot').mockResolvedValue({ ok: true })
    renderChatPage([], { slots: [REMOTE_SLOT] })
    await waitFor(() => expect(welcomeProps).not.toBeNull())

    await act(async () => {
      await welcomeProps!.onSwitchMode?.('persistent')
    })

    await waitFor(() => expect(apiMocks.createChatSlot).toHaveBeenCalled())
    expect(apiMocks.createChatSlot.mock.calls.at(-1)?.[8]).toBe('crew-remote-1')
    expect(apiMocks.deleteChatSlot).toHaveBeenCalledWith('chat-1')
  })
})

describe('ChatPage row callbacks — fork', () => {
  it('forks at the row index with the head direction when the config is warm', async () => {
    apiSpy('dashboardConfig').mockResolvedValue({ tail_fork_enabled: false })
    apiSpy('forkChatSlot').mockResolvedValue({ ok: true, key: 'chat-2', title: 'fork' })
    await renderTurn()
    await act(async () => { await assistantProps!.onFork!(1) })
    await waitFor(() => expect(apiMocks.forkChatSlot).toHaveBeenCalled())
    expect(apiMocks.forkChatSlot).toHaveBeenCalledWith('chat-1', 1, undefined, undefined, 'head')
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('re-reads the config when the query never produced one, so a tail fork stays a tail fork', async () => {
    // The dashboardConfig query fails, so `forkCfg` is undefined at click time —
    // the branch that must fetch rather than silently downgrade to a head fork.
    apiSpy('dashboardConfig')
      .mockRejectedValueOnce(new Error('config unavailable'))
      .mockResolvedValue({ tail_fork_enabled: true })
    apiSpy('forkChatSlot').mockResolvedValue({ ok: true, key: 'chat-2' })
    await renderTurn()
    await act(async () => { await assistantProps!.onFork!(3) })
    await waitFor(() => expect(apiMocks.forkChatSlot).toHaveBeenCalled())
    expect(apiMocks.forkChatSlot).toHaveBeenCalledWith('chat-1', 3, undefined, undefined, 'tail')
  })

  it('reports a refused fork through the in-page ErrorNotice instead of switching sessions', async () => {
    apiSpy('forkChatSlot').mockResolvedValue({ ok: false, error: 'slot is busy' })
    await renderTurn()
    await act(async () => { await assistantProps!.onFork!(1) })
    // The surface is the shared ErrorNotice (role="alert" + agent hand-off),
    // never a native alert(): the rule `errors-use-error-notice` forbids the
    // browser dialog, which also carried no structured context to the agent.
    const notice = await screen.findByTestId('action-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice.textContent).toContain('slot is busy')
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('still reports when the fork request throws, naming the real reason', async () => {
    apiSpy('forkChatSlot').mockRejectedValue(new Error('network down'))
    await renderTurn()
    await act(async () => { await assistantProps!.onFork!(1) })
    const said = (await screen.findByTestId('action-error')).textContent ?? ''
    expect(said).toContain('Fork failed')
    // Flipped, as this assertion's previous form asked to be: it pinned the
    // reason being LOST — `unwrap()` rejects with a redux-toolkit
    // SerializedError (a PLAIN OBJECT), so the handler's `e instanceof Error`
    // test was false and the `String(e)` fallback rendered '[object Object]'.
    // The handler now reads the message through `utils/thunkError.errMessage`,
    // which knows that shape, so the notice carries the real text.
    expect(said).toContain('network down')
    expect(said).not.toContain('[object Object]')
    expect(alertSpy).not.toHaveBeenCalled()
  })
})

describe('ChatPage row callbacks — plan from here', () => {
  it('forks into an orchestrator session without a direction', async () => {
    apiSpy('forkChatSlot').mockResolvedValue({ ok: true, key: 'chat-2' })
    await renderTurn()
    await act(async () => { await assistantProps!.onPlanFromHere!(2) })
    await waitFor(() => expect(apiMocks.forkChatSlot).toHaveBeenCalled())
    expect(apiMocks.forkChatSlot).toHaveBeenCalledWith('chat-1', 2, undefined, 'orchestrator', undefined)
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('reports a refused plan-from-here with its own message, not the fork one', async () => {
    apiSpy('forkChatSlot').mockResolvedValue({ ok: false, error: 'no orchestrator agent' })
    await renderTurn()
    await act(async () => { await assistantProps!.onPlanFromHere!(2) })
    const said = (await screen.findByTestId('action-error')).textContent ?? ''
    expect(said).toContain('no orchestrator agent')
    expect(said).not.toContain('Fork failed')
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('surfaces a failed plan apply and resolves false', async () => {
    apiSpy('planFromChat').mockResolvedValue({ ok: false })
    await renderTurn()
    let applied: boolean | undefined
    await act(async () => { applied = await assistantProps!.onApplyPlan!([]) })
    expect(applied).toBe(false)
    const said = (await screen.findByTestId('action-error')).textContent ?? ''
    expect(said).toContain('Failed to apply plan')
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('resolves true and leaves the page quiet when the plan is accepted', async () => {
    apiSpy('planFromChat').mockResolvedValue({ ok: true, task_id: 'task-9' })
    await renderTurn()
    let applied: boolean | undefined
    await act(async () => { applied = await assistantProps!.onApplyPlan!([]) })
    expect(applied).toBe(true)
    expect(apiMocks.planFromChat).toHaveBeenCalled()
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('treats a thrown plan apply the same as a refusal', async () => {
    apiSpy('planFromChat').mockRejectedValue(new Error('projects service down'))
    await renderTurn()
    let applied: boolean | undefined
    await act(async () => { applied = await assistantProps!.onApplyPlan!([]) })
    expect(applied).toBe(false)
    const said = (await screen.findByTestId('action-error')).textContent ?? ''
    expect(said).toContain('Failed to apply plan')
    expect(alertSpy).not.toHaveBeenCalled()
  })
})

describe('ChatPage row callbacks — quote and ask', () => {
  it('quotes the selection into the composer and shows the transit animation', async () => {
    await renderTurn()
    const rect = { top: 10, left: 20, width: 5, height: 5 } as DOMRect
    act(() => { assistantProps!.onQuote!('first line\nsecond line', rect) })
    await waitFor(() => expect(inputProps!.value).toContain('> first line'))
    expect(inputProps!.value).toContain('> second line')
    expect(screen.getByTestId('flying-quote')).toBeInTheDocument()
  })

  it('appends a second quote below the first rather than replacing it', async () => {
    await renderTurn()
    const rect = { top: 0, left: 0, width: 1, height: 1 } as DOMRect
    act(() => { assistantProps!.onQuote!('alpha', rect) })
    await waitFor(() => expect(inputProps!.value).toContain('> alpha'))
    act(() => { assistantProps!.onQuote!('beta', rect) })
    await waitFor(() => expect(inputProps!.value).toContain('> beta'))
    expect(inputProps!.value).toContain('> alpha')
  })

  it('routes Ask to the side panel and seeds it, leaving the main composer untouched', async () => {
    const { store } = await renderTurn()
    act(() => { assistantProps!.onAsk!('why is this slow?') })
    // The seed is a store write under the active slot (the shared chat-core
    // seam), which is the slot the activity panel's Side Chat is bound to.
    expect(readSideChatDraft(store.getState().chat.activeSlot!)).toBe('> why is this slow?\n\n')
    expect(store.getState().chat.activityTab).toBe('side')
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(inputProps!.value).toBe('')
  })

  /** Enters split view through the header toggle and returns the grid's props.
   *  Two known slots, so a switch to `chat-2` is a real re-bind rather than a
   *  switch to a stranger the page would immediately undo. */
  async function renderSplit() {
    apiSpy('dashboardConfig').mockResolvedValue({ session_grid: true })
    const { store } = await renderTurn({ slots: [SLOT, OTHER_SLOT] })
    fireEvent.click(await screen.findByRole('button', { name: 'Enter split view' }))
    await waitFor(() => expect(gridProps?.openSideChat).toBeTypeOf('function'))
    return { store }
  }

  it('re-binds the activity panel to a split pane\'s slot before opening its Side Chat', async () => {
    const { store } = await renderSplit()
    let verdict: boolean | void | Promise<boolean | void>
    act(() => { verdict = gridProps!.openSideChat!('chat-2') })
    // A re-bind is a request the server can reject, so the opener reports its
    // verdict only once the switch settled — the Side tab opens on the
    // re-bound panel and the seam seeds on `true`.
    expect(verdict!).toBeInstanceOf(Promise)
    await expect(verdict!).resolves.toBe(true)
    expect(store.getState().chat.activeSlot).toBe('chat-2')
    expect(store.getState().chat.activityTab).toBe('side')
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('a re-bind the server rejects reports false and surfaces the failure — nothing is seeded', async () => {
    const { store } = await renderSplit()
    // The pane's session was deleted under it: the detail fetch 404s, so
    // `switchSlot.rejected` falls back to the slot the page was on.
    apiSpy('chatSlotDetail').mockRejectedValueOnce(new ApiError(404, 'no such slot'))
    let verdict: boolean | void | Promise<boolean | void>
    act(() => { verdict = gridProps!.openSideChat!('chat-2') })
    await expect(verdict!).resolves.toBe(false)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1'))
    expect(store.getState().chat.activityTab).not.toBe('side')
    // The failure is said out loud above the composer, not swallowed.
    await screen.findByText(/Couldn't open a Side Chat for that pane/)
  })

  it('a re-bind overtaken by a later switch reports false — the Side tab is not opened for the wrong pane', async () => {
    const { store } = await renderSplit()
    // Pane B's detail fetch hangs; the user goes back to pane A meanwhile.
    let release: (v: unknown) => void = () => {}
    apiSpy('chatSlotDetail').mockImplementationOnce(() => new Promise(res => { release = res }))
    let verdict: boolean | void | Promise<boolean | void>
    act(() => { verdict = gridProps!.openSideChat!('chat-2') })
    expect(store.getState().chat.activeSlot).toBe('chat-2')
    await act(async () => { await store.dispatch(switchSlot('chat-1')) })
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    // B's stale fulfilment lands: the slice ignores it (user switched away), and
    // so must the opener — a `true` here would seed B while A's panel opens.
    await act(async () => { release({}) })
    await expect(verdict!).resolves.toBe(false)
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    expect(store.getState().chat.activityTab).not.toBe('side')
  })

  it('withholds Ask from the panes while disconnected instead of switching slots offline', async () => {
    const { store } = await renderSplit()
    const askWhileConnected = gridProps!.openSideChat!
    act(() => { store.dispatch(sseDisconnected()) })
    // The capability is dropped, so the panes' toolbars offer Copy / Quote only…
    await waitFor(() => expect(gridProps!.openSideChat).toBeUndefined())
    // …and a call that slipped through in the frame before the re-render does
    // NOT dispatch the switch a disconnected gateway would reject — the
    // rejection clears the active pane's messages, blanking the transcript the
    // reader just selected from. Nothing moves and no Side Chat bound to the
    // wrong slot opens. The opener reports the refusal (`false`) so the
    // selection seam does not seed a quote into a Side Chat that never opened.
    let outcome: boolean | void | Promise<boolean | void>
    act(() => { outcome = askWhileConnected('chat-2') })
    expect(outcome).toBe(false)
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    expect(store.getState().chat.activityTab).not.toBe('side')
  })
})

describe('ChatPage row callbacks — regenerate and speak', () => {
  it('truncates back to the last user row and asks the server to regenerate', async () => {
    apiSpy('regenerateSlot').mockResolvedValue({ ok: true })
    const { store } = await renderTurn()
    expect(assistantProps!.onRegenerate).toBeTypeOf('function')
    await act(async () => { assistantProps!.onRegenerate!() })
    await waitFor(() => expect(apiMocks.regenerateSlot).toHaveBeenCalledWith('chat-1'))
    expect(store.getState().chat.messages).toHaveLength(1)
    expect(store.getState().chat.messages[0].role).toBe('user')
  })

  it('restores the transcript when the regenerate request fails', async () => {
    apiSpy('regenerateSlot').mockRejectedValue(new Error('runner busy'))
    const { store } = await renderTurn()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      await act(async () => { assistantProps!.onRegenerate!() })
      await waitFor(() => expect(store.getState().chat.messages).toHaveLength(2))
    } finally {
      warn.mockRestore()
    }
    expect(store.getState().chat.messages[1].role).toBe('assistant')
  })

  it('synthesizes speech for the row content when nothing is playing', async () => {
    apiSpy('voiceSynthesize').mockResolvedValue({})
    await renderTurn()
    act(() => { assistantProps!.onSpeak!('two files changed') })
    await waitFor(() => expect(apiMocks.voiceSynthesize).toHaveBeenCalledWith('chat-1', 'two files changed'))
  })

  it('stops playback instead of synthesizing again while a clip is playing', async () => {
    const synth = apiSpy('voiceSynthesize')
    await renderTurn()
    // The handler reads `voicePlaying` off the app-wide store singleton (so the
    // callback identity stays stable while a turn streams), not off the store
    // this test renders with — so the flag has to be set there.
    act(() => { appStore.dispatch(setVoicePlaying(true)) })
    let stopped = 0
    const onStop = () => { stopped += 1 }
    window.addEventListener('voice-stop', onStop)
    try {
      act(() => { assistantProps!.onSpeak!('two files changed') })
      await waitFor(() => expect(stopped).toBe(1))
    } finally {
      window.removeEventListener('voice-stop', onStop)
      act(() => { appStore.dispatch(setVoicePlaying(false)) })
    }
    expect(synth).not.toHaveBeenCalled()
  })
})

describe('ChatPage window-event listeners', () => {
  it('re-reads the chat settings when a config change is broadcast', async () => {
    await renderTurn()
    expect(assistantProps!.timestamp).toBeUndefined()
    chatSettings = { contentWidth: 'compact', showTimestamps: true }
    act(() => { window.dispatchEvent(new Event('mc-config-changed')) })
    await waitFor(() => expect(assistantProps!.timestamp).toBeTruthy())
  })

  it('toggles the sidebar pin and persists it', async () => {
    await renderTurn()
    expect(localStorage.getItem('mc-sidebar-pinned')).toBeNull()
    act(() => { window.dispatchEvent(new Event('toggle-pin-chat-sidebar')) })
    await waitFor(() => expect(localStorage.getItem('mc-sidebar-pinned')).not.toBeNull())
    const first = localStorage.getItem('mc-sidebar-pinned')
    act(() => { window.dispatchEvent(new Event('toggle-pin-chat-sidebar')) })
    await waitFor(() => expect(localStorage.getItem('mc-sidebar-pinned')).not.toBe(first))
  })

  it('leaves the activity panel alone when the agent runs a playwright-cli shell command', async () => {
    // A shell `playwright-cli` call drives an agent-owned headless Chromium that
    // the Browser panel has no way to frame: the native view is reached only by
    // `browser` MCP ops (surfaced by `browser:agent-opened`), and the gateway's
    // framed `show` server lives in its own session namespace. Opening the
    // panel here could only land on the "not running" card, so it must not.
    delete (window as unknown as { browserAPI?: unknown }).browserAPI
    const { store } = await renderTurn()
    expect(store.getState().chat.activityOpen).toBe(false)

    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-tool-call', {
        detail: {
          slot: 'chat-1',
          is_shell: true,
          input_preview: 'playwright-cli open https://example.test',
        },
      }))
    })

    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('leaves the activity panel alone on a playwright-cli shell command even when the native bridge is present', async () => {
    // Bridge presence is not evidence the shell browse landed in the native
    // view — it never does. The desktop path opens the panel from the main
    // process's `browser:agent-opened` signal, not from the tool-call preview.
    ;(window as unknown as { browserAPI?: unknown }).browserAPI = {
      trackSession: vi.fn(async () => ({ ok: true })),
      onAgentOpened: vi.fn(() => () => {}),
    }
    try {
      const { store } = await renderTurn()
      act(() => {
        window.dispatchEvent(new CustomEvent('kirocrew-tool-call', {
          detail: {
            slot: 'chat-1',
            is_shell: true,
            input_preview: 'playwright-cli snapshot',
          },
        }))
      })
      await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
      expect(store.getState().chat.activityOpen).toBe(false)
    } finally {
      delete (window as unknown as { browserAPI?: unknown }).browserAPI
    }
  })

  it('ignores a run-in-terminal request that carries no command', async () => {
    const { store } = await renderTurn()
    act(() => {
      window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail: { reqId: 'r1' } }))
    })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('ignores a run-in-terminal request whose command is an empty string', async () => {
    const { store } = await renderTurn()
    act(() => {
      window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail: { code: '', reqId: 'r3' } }))
    })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('answers a run-in-terminal request exactly once, carrying its reqId back', async () => {
    await renderTurn()
    const results: { reqId?: string; ok?: boolean }[] = []
    const onResult = (e: Event) => { results.push((e as CustomEvent).detail) }
    window.addEventListener('mc:run-in-terminal-result', onResult)
    try {
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal', {
          detail: { code: 'npm test', reqId: 'r2' },
        }))
      })
      // "Run in terminal" now routes to the app-wide dock panel
      // (useBottomTerminal), not the chat-scoped activity panel, so
      // `chat.activityOpen` is intentionally untouched. The handler races the
      // PTY against RUN_IN_TERMINAL_READY_DEADLINE_MS; either leg answers, and
      // the `settled` latch is what guarantees the code-block button is told
      // once and only once.
      await act(async () => { await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_READY_DEADLINE_MS + 1_000) })
      await waitFor(() => expect(results.length).toBe(1), { timeout: 5_000 })
    } finally {
      window.removeEventListener('mc:run-in-terminal-result', onResult)
    }
    expect(results[0].reqId).toBe('r2')
    expect(typeof results[0].ok).toBe('boolean')
  })
})

describe('ChatPage run-in-terminal dispatch rollback (#10822)', () => {
  const collect = () => {
    const results: { reqId?: string; ok?: boolean }[] = []
    const onResult = (e: Event) => { results.push((e as CustomEvent).detail) }
    window.addEventListener('mc:run-in-terminal-result', onResult)
    return { results, stop: () => window.removeEventListener('mc:run-in-terminal-result', onResult) }
  }

  beforeEach(() => { __resetBottomTerminal(); mockTerminalPopoutOpen = false })

  it('leaves a tab the user already closed alone — no second PTY delete, no store write', async () => {
    await renderTurn()
    const dock = renderHook(() => useBottomTerminal())
    const fetchSpy = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchSpy)
    const { results, stop } = collect()
    try {
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal', {
          detail: { code: 'npm test', reqId: 'rb3' },
        }))
      })
      expect(dock.result.current.tabs.length).toBe(1)
      const sessionId = dock.result.current.tabs[0].id

      // The user closes the tab before the shell ever reports ready. The
      // close path owns the teardown (including its own DELETE).
      act(() => { removeTab(sessionId) })
      expect(dock.result.current.tabs.length).toBe(0)

      await act(async () => { await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_READY_DEADLINE_MS + 1_000) })

      await waitFor(() => expect(results.length).toBe(1))
      expect(results[0]).toMatchObject({ reqId: 'rb3', ok: false })
      // The dispatch must not issue a second DELETE for a tab it no longer owns.
      expect(fetchSpy).not.toHaveBeenCalledWith(
        `/api/terminal/sessions/${sessionId}`, expect.objectContaining({ method: 'DELETE' }),
      )
    } finally {
      stop()
      vi.unstubAllGlobals()
    }
  })

  it('leaves the session alone when the panel was popped out before ready', async () => {
    await renderTurn()
    const dock = renderHook(() => useBottomTerminal())
    const fetchSpy = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchSpy)
    const { results, stop } = collect()
    try {
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal', {
          detail: { code: 'npm test', reqId: 'rb4' },
        }))
      })
      expect(dock.result.current.tabs.length).toBe(1)
      const sessionId = dock.result.current.tabs[0].id

      // The user pops the terminal panel out: the popout window now owns the
      // session's connection, and the tab stays in the shared store.
      mockTerminalPopoutOpen = true

      await act(async () => { await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_READY_DEADLINE_MS + 1_000) })

      await waitFor(() => expect(results.length).toBe(1))
      expect(results[0]).toMatchObject({ reqId: 'rb4', ok: false })
      // Deleting the PTY here would tear the live shell out of the popout.
      expect(fetchSpy).not.toHaveBeenCalledWith(
        `/api/terminal/sessions/${sessionId}`, expect.objectContaining({ method: 'DELETE' }),
      )
      expect(dock.result.current.tabs.length).toBe(1)
    } finally {
      stop()
      vi.unstubAllGlobals()
    }
  })

  it('keeps the tab when the probe reports a live shell without a ready frame', async () => {
    await renderTurn()
    const dock = renderHook(() => useBottomTerminal())
    const fetchSpy = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchSpy)
    const { results, stop } = collect()
    try {
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal', {
          detail: { code: 'npm test', reqId: 'rb-live' },
        }))
      })
      expect(dock.result.current.tabs.length).toBe(1)
      const sessionId = dock.result.current.tabs[0].id
      fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
        enabled: true,
        sessions: [{ session_id: sessionId, alive: true }],
      }), { status: 200 }))

      await act(async () => { await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_READY_DEADLINE_MS + 1_000) })

      await waitFor(() => expect(fetchSpy).toHaveBeenCalledWith('/api/terminal/sessions'))
      expect(results[0]).toMatchObject({ reqId: 'rb-live', ok: false })
      expect(dock.result.current.tabs.length).toBe(1)
      expect(fetchSpy).not.toHaveBeenCalledWith(
        `/api/terminal/sessions/${sessionId}`, expect.objectContaining({ method: 'DELETE' }),
      )
      expect(disposeTerminalSessionSpy).not.toHaveBeenCalled()
      // For a profile that replaces the readiness hook this is the routine
      // outcome, so it cannot be the quiet one: the kept tab explains itself.
      const said = (await screen.findByTestId('action-error')).textContent ?? ''
      expect(said).toContain('shell is running')
      expect(said).toContain('run the command there')
    } finally {
      stop()
      vi.unstubAllGlobals()
    }
  })

  it('rolls back the tab and PTY when the probe reports a dead shell', async () => {
    await renderTurn()
    const dock = renderHook(() => useBottomTerminal())
    const fetchSpy = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchSpy)
    const { results, stop } = collect()
    try {
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal', {
          detail: { code: 'npm test', reqId: 'rb-dead' },
        }))
      })
      expect(dock.result.current.tabs.length).toBe(1)
      const sessionId = dock.result.current.tabs[0].id
      fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
        enabled: true,
        sessions: [{ session_id: sessionId, alive: false }],
      }), { status: 200 }))

      await act(async () => { await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_READY_DEADLINE_MS + 1_000) })

      await waitFor(() => expect(dock.result.current.tabs.length).toBe(0))
      expect(results[0]).toMatchObject({ reqId: 'rb-dead', ok: false })
      expect(fetchSpy).toHaveBeenCalledWith('/api/terminal/sessions')
      expect(fetchSpy).toHaveBeenCalledWith(
        `/api/terminal/sessions/${sessionId}`, { method: 'DELETE', keepalive: true },
      )
      expect(disposeTerminalSessionSpy).toHaveBeenCalledWith(sessionId)
    } finally {
      stop()
      vi.unstubAllGlobals()
    }
  })

  it('rolls back an absent session locally without a DELETE the backend would 404', async () => {
    await renderTurn()
    const dock = renderHook(() => useBottomTerminal())
    const fetchSpy = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchSpy)
    const { results, stop } = collect()
    try {
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal', {
          detail: { code: 'npm test', reqId: 'rb-absent' },
        }))
      })
      expect(dock.result.current.tabs.length).toBe(1)
      const sessionId = dock.result.current.tabs[0].id
      // Absent on BOTH the first probe and the confirm probe: gone, not opening.
      fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
        enabled: true,
        sessions: [],
      }), { status: 200 }))
      fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
        enabled: true,
        sessions: [],
      }), { status: 200 }))

      await act(async () => {
        await vi.advanceTimersByTimeAsync(
          RUN_IN_TERMINAL_READY_DEADLINE_MS + RUN_IN_TERMINAL_OPENING_GRACE_MS + 1_000,
        )
      })

      await waitFor(() => expect(dock.result.current.tabs.length).toBe(0))
      expect(results[0]).toMatchObject({ reqId: 'rb-absent', ok: false })
      expect(fetchSpy).toHaveBeenCalledWith('/api/terminal/sessions')
      // The backend registry has no such session, so a DELETE could only 404
      // and surface a close failure for a session that needs no closing.
      expect(fetchSpy).not.toHaveBeenCalledWith(
        `/api/terminal/sessions/${sessionId}`, expect.objectContaining({ method: 'DELETE' }),
      )
      expect(disposeTerminalSessionSpy).toHaveBeenCalledWith(sessionId)
      // Closing a watched tab is the routine outcome, so it must not be silent.
      const said = (await screen.findByTestId('action-error')).textContent ?? ''
      expect(said).toContain("wasn't sent")
      expect(said).toContain('was closed')
    } finally {
      stop()
      vi.unstubAllGlobals()
    }
  })

  it('keeps a session that is merely opening: absent, then listed alive on the confirm probe', async () => {
    await renderTurn()
    const dock = renderHook(() => useBottomTerminal())
    const fetchSpy = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchSpy)
    const { results, stop } = collect()
    try {
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal', {
          detail: { code: 'npm test', reqId: 'rb-opening' },
        }))
      })
      expect(dock.result.current.tabs.length).toBe(1)
      const sessionId = dock.result.current.tabs[0].id
      // First probe: the backend still holds the placeholder `ws.prepare()`
      // reserved, which the sessions route skips -- so the session is absent.
      fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
        enabled: true,
        sessions: [],
      }), { status: 200 }))
      // Confirm probe: the shell finished spawning and is registered alive.
      fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
        enabled: true,
        sessions: [{ session_id: sessionId, alive: true }],
      }), { status: 200 }))

      await act(async () => {
        await vi.advanceTimersByTimeAsync(
          RUN_IN_TERMINAL_READY_DEADLINE_MS + RUN_IN_TERMINAL_OPENING_GRACE_MS + 1_000,
        )
      })

      await waitFor(() => expect(results.length).toBe(1))
      expect(results[0]).toMatchObject({ reqId: 'rb-opening', ok: false })
      // An opening shell keeps its tab: rolling it back would remove the tab
      // from under a shell about to come up, leaving it orphaned.
      expect(dock.result.current.tabs.length).toBe(1)
      expect(fetchSpy).not.toHaveBeenCalledWith(
        `/api/terminal/sessions/${sessionId}`, expect.objectContaining({ method: 'DELETE' }),
      )
      expect(disposeTerminalSessionSpy).not.toHaveBeenCalled()
    } finally {
      stop()
      vi.unstubAllGlobals()
    }
  })

  it('keeps the tab and local connection when the liveness probe fails', async () => {
    await renderTurn()
    const dock = renderHook(() => useBottomTerminal())
    const fetchSpy = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchSpy)
    const { results, stop } = collect()
    try {
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal', {
          detail: { code: 'npm test', reqId: 'rb-probe-failed' },
        }))
      })
      expect(dock.result.current.tabs.length).toBe(1)
      const sessionId = dock.result.current.tabs[0].id
      fetchSpy.mockRejectedValueOnce(new Error('network unavailable'))

      await act(async () => { await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_READY_DEADLINE_MS + 1_000) })

      await waitFor(() => expect(fetchSpy).toHaveBeenCalledWith('/api/terminal/sessions'))
      expect(results[0]).toMatchObject({ reqId: 'rb-probe-failed', ok: false })
      expect(dock.result.current.tabs.length).toBe(1)
      expect(fetchSpy).not.toHaveBeenCalledWith(
        `/api/terminal/sessions/${sessionId}`, expect.objectContaining({ method: 'DELETE' }),
      )
      expect(disposeTerminalSessionSpy).not.toHaveBeenCalled()
      // A kept tab whose command never ran is unexplained unless the failure
      // reaches the user through the required error surface -- while the probe's
      // own transport error stays out of copy the user never asked for.
      const said = (await screen.findByTestId('action-error')).textContent ?? ''
      expect(said).toContain('left open')
      expect(said).not.toContain('network unavailable')
    } finally {
      stop()
      vi.unstubAllGlobals()
    }
  })

  it('shares one liveness probe between dispatches that reach their deadlines together', async () => {
    await renderTurn()
    const dock = renderHook(() => useBottomTerminal())
    const fetchSpy = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchSpy)
    const { results, stop } = collect()
    try {
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal', {
          detail: { code: 'npm test', reqId: 'rb-dedupe-a' },
        }))
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal', {
          detail: { code: 'npm run lint', reqId: 'rb-dedupe-b' },
        }))
      })
      expect(dock.result.current.tabs.length).toBe(2)
      const ids = dock.result.current.tabs.map(tab => tab.id)
      fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
        enabled: true,
        sessions: ids.map(id => ({ session_id: id, alive: true })),
      }), { status: 200 }))

      await act(async () => { await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_READY_DEADLINE_MS + 1_000) })

      await waitFor(() => expect(results.length).toBe(2))
      // Both dispatches deadlined in the same tick: one shared request, not one
      // uncached request each.
      const probes = fetchSpy.mock.calls.filter(([url]) => url === '/api/terminal/sessions')
      expect(probes.length).toBe(1)
      expect(dock.result.current.tabs.length).toBe(2)
    } finally {
      stop()
      vi.unstubAllGlobals()
    }
  })

  it('keeps the tab when the shell becomes ready and the command is sent', async () => {
    await renderTurn()
    const dock = renderHook(() => useBottomTerminal())
    const fetchSpy = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchSpy)
    const { results, stop } = collect()
    const sent: Uint8Array[] = []
    let sessionId = ''
    try {
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal', {
          detail: { code: 'npm test', reqId: 'rb2' },
        }))
      })
      expect(dock.result.current.tabs.length).toBe(1)
      sessionId = dock.result.current.tabs[0].id

      // The PTY reports ready: registering the socket drains the ready
      // listener synchronously and the command goes out on it.
      const ws = { readyState: WebSocket.OPEN, send: (d: Uint8Array) => { sent.push(d) } } as unknown as WebSocket
      act(() => { registerTerminalWs(sessionId, ws) })

      await waitFor(() => expect(results.length).toBe(1))
      expect(results[0]).toMatchObject({ reqId: 'rb2', ok: true })
      expect(new TextDecoder().decode(sent[0])).toBe('npm test\n')

      // The deadline passing afterwards must not tear down a live shell.
      await act(async () => { await vi.advanceTimersByTimeAsync(RUN_IN_TERMINAL_READY_DEADLINE_MS + 1_000) })
      expect(dock.result.current.tabs.length).toBe(1)
      expect(fetchSpy).not.toHaveBeenCalledWith(
        `/api/terminal/sessions/${sessionId}`, expect.objectContaining({ method: 'DELETE' }),
      )
    } finally {
      stop()
      if (sessionId) unregisterTerminalWs(sessionId)
      vi.unstubAllGlobals()
    }
  })
})

describe('ChatPage welcome-state history suggestions', () => {
  const HISTORY: HistorySession[] = [
    { key: 'sess-a', title: 'rate limiter rollout', created: '2026-08-01T10:00:00Z', messages: 4 },
    { key: 'sess-b', title: 'unrelated design doc', created: '2026-08-02T10:00:00Z', messages: 2 },
  ]

  /** Pre-fills the composer through the widget bridge, then lets the 300 ms
   *  history-query debounce fire. */
  async function typeQuery(text: string) {
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-widget-send', { detail: { text } }))
    })
    await waitFor(() => expect(inputProps!.value).toContain(text))
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
  }

  it('offers only the matching past sessions and resumes the one clicked', async () => {
    apiSpy('resumeChatSlot').mockResolvedValue({
      ok: true, key: 'sess-a', messages: [], has_more: false, total: 0, mode: '',
    })
    const { store } = renderChatPage([], { sessions: HISTORY })
    await waitFor(() => expect(inputProps).not.toBeNull())
    await typeQuery('rate limiter')
    // #765: history is seeded lazily by the typed query, not on mount.
    await waitFor(() => expect(store.getState().chat.history).toHaveLength(2))
    const list = await screen.findByRole('listbox', { name: 'Previous chats' }, { timeout: 5_000 })
    const options = within(list).getAllByRole('option')
    expect(options).toHaveLength(1)
    await act(async () => { fireEvent.mouseDown(options[0]) })
    await waitFor(() => expect(apiMocks.resumeChatSlot).toHaveBeenCalledWith('sess-a', 'rate limiter rollout'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('sess-a'))
  })

  it('dismisses the suggestions on Escape', async () => {
    const { store } = renderChatPage([], { sessions: HISTORY })
    await waitFor(() => expect(inputProps).not.toBeNull())
    await typeQuery('rate limiter')
    // #765: history is seeded lazily by the typed query, not on mount.
    await waitFor(() => expect(store.getState().chat.history).toHaveLength(2))
    expect(await screen.findByRole('listbox', { name: 'Previous chats' }, { timeout: 5_000 })).toBeInTheDocument()
    act(() => { fireEvent.keyDown(document, { key: 'Escape' }) })
    await waitFor(() => expect(screen.queryByRole('listbox', { name: 'Previous chats' })).toBeNull())
  })

  it('offers nothing when no past session matches', async () => {
    const { store } = renderChatPage([], { sessions: HISTORY })
    await waitFor(() => expect(inputProps).not.toBeNull())
    await typeQuery('kubernetes migration')
    // #765: history is seeded lazily by the typed query, not on mount — wait
    // for the seed to land so the "no match" below is a real negative, not a
    // not-yet-loaded false pass.
    await waitFor(() => expect(store.getState().chat.history).toHaveLength(2))
    expect(screen.queryByRole('listbox', { name: 'Previous chats' })).toBeNull()
  })
})

describe('ChatPage project picker', () => {
  it('writes the picked directory to the active session', async () => {
    apiSpy('chatSlotProject').mockResolvedValue({ ok: true })
    await renderTurn()
    await waitFor(() => expect(projectPickerProps).not.toBeNull())
    await act(async () => { projectPickerProps!.onSelect('/repo/service') })
    await waitFor(() => expect(apiMocks.chatSlotProject).toHaveBeenCalledWith('chat-1', '/repo/service'))
  })

  it('swallows a failed project write instead of breaking the page', async () => {
    apiSpy('chatSlotProject').mockRejectedValue(new Error('no such directory'))
    await renderTurn()
    await waitFor(() => expect(projectPickerProps).not.toBeNull())
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      await act(async () => { projectPickerProps!.onSelect('/nope') })
      await waitFor(() => expect(err).toHaveBeenCalled())
    } finally {
      err.mockRestore()
    }
    // The composer is still mounted and interactive — the rejection did not
    // escape into the render tree.
    expect(inputProps).not.toBeNull()
  })
})

describe('ChatPage widget composer bridge', () => {
  it('appends a widget action below text the user already typed', async () => {
    await renderTurn()
    const rect = { top: 0, left: 0, width: 1, height: 1 } as DOMRect
    act(() => { assistantProps!.onQuote!('context line', rect) })
    await waitFor(() => expect(inputProps!.value).toContain('> context line'))
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-widget-send', { detail: { text: 'Merge it now' } }))
    })
    await waitFor(() => expect(inputProps!.value).toContain('Merge it now'))
    expect(inputProps!.value).toContain('> context line')
  })

  it('ignores a widget action with no text', async () => {
    await renderTurn()
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-widget-send', { detail: { text: '' } }))
    })
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-widget-send', { detail: {} }))
    })
    expect(inputProps!.value).toBe('')
  })
})

// A failed screen capture used to be discarded by a bare `catch {}` commented
// "user cancelled" -- but cancellation is NOT an error path: the route answers a
// cancelled capture with 200 `{"path": ""}`, which the caller's `if (path)`
// guard absorbs. So the only things that reached that catch were real failures
// (the 400 off macOS, the 120s capture timeout, the request never reaching the
// gateway), and the user saw nothing at all.
describe('ChatPage screen capture failures', () => {
  it('reports a failed capture instead of swallowing it', async () => {
    apiSpy('screenshot').mockRejectedValue(new Error('screenshot timed out'))
    await renderTurn()
    await waitFor(() => expect(inputProps?.onScreenshot).toBeTypeOf('function'))
    await act(async () => { inputProps!.onScreenshot!() })
    // The notice names the action, not just the transport text: a bare
    // "screenshot timed out" above the composer tells the user nothing about
    // which click failed.
    expect(await screen.findByText('Screenshot failed: screenshot timed out')).toBeInTheDocument()
  })

  it('stays silent when the user cancels, which is not a failure', async () => {
    // The cancelled shape: HTTP 200, empty path. No notice, no attachment.
    apiSpy('screenshot').mockResolvedValue({ path: '' })
    await renderTurn()
    await waitFor(() => expect(inputProps?.onScreenshot).toBeTypeOf('function'))
    await act(async () => { inputProps!.onScreenshot!() })
    expect(screen.queryByText(/unknown error/i)).not.toBeInTheDocument()
  })
})
