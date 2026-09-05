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
 *     `toggle-pin-chat-sidebar`, and `mc:run-in-terminal` (both the non-string
 *     guard and the PTY-never-connects timeout that reports failure back to the
 *     code block).
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
import { render, screen, act, waitFor, fireEvent, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { store as appStore } from '../store'
import { setVoicePlaying } from '../store/chatSlice'
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

interface InputProps { value: string; onChange: (v: string) => void }
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
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
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

// --- Fixtures ---------------------------------------------------------------

const SLOT = {
  key: 'chat-1', title: 'chat-1', messages: 0, running: false,
  mode: '', created: '', last_ts: '',
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
}

function renderChatPage(messages: ChatMessage[], opts: RenderOpts = {}) {
  const { chat = {}, sessions = [] } = opts
  apiMocks.chatSlots = vi.fn().mockResolvedValue([SLOT])
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
      slots: [SLOT] as unknown as RootState['dashboard']['slots'],
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
  chatSettings = { contentWidth: 'compact' }
  localStorage.clear()
  sessionStorage.clear()
  for (const k of Object.keys(apiMocks)) delete apiMocks[k]
  alertSpy = makeAlertSpy()
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  alertSpy.mockRestore()
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

  it('reports a refused fork through the error notice instead of switching sessions', async () => {
    apiSpy('forkChatSlot').mockResolvedValue({ ok: false, error: 'slot is busy' })
    await renderTurn()
    await act(async () => { await assistantProps!.onFork!(1) })
    const notice = await waitFor(() => screen.getByTestId('fork-error'))
    expect(notice.textContent).toContain('slot is busy')
    // The surface moved off `alert` deliberately; asserting its absence is what
    // stops a regression back to a modal that discards the agent hand-off.
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('still surfaces a thrown fork request, naming the real reason', async () => {
    apiSpy('forkChatSlot').mockRejectedValue(new Error('network down'))
    await renderTurn()
    await act(async () => { await assistantProps!.onFork!(1) })
    const notice = await waitFor(() => screen.getByTestId('fork-error'))
    const said = notice.textContent ?? ''
    expect(said).toContain('Fork failed')
    // `unwrap()` rejects with a SerializedError (a PLAIN OBJECT), so an `e instanceof
    // Error` test reads false and a `String(e)` fallback renders '[object Object]'.
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
    await waitFor(() => expect(alertSpy).toHaveBeenCalled())
    const said = String(alertSpy.mock.calls[0][0])
    expect(said).toContain('no orchestrator agent')
    expect(said).not.toContain('Fork failed')
  })

  it('surfaces a failed plan apply and resolves false', async () => {
    apiSpy('planFromChat').mockResolvedValue({ ok: false })
    await renderTurn()
    let applied: boolean | undefined
    await act(async () => { applied = await assistantProps!.onApplyPlan!([]) })
    expect(applied).toBe(false)
    expect(alertSpy).toHaveBeenCalledWith(expect.stringContaining('Failed to apply plan'))
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
    expect(alertSpy).toHaveBeenCalledWith(expect.stringContaining('Failed to apply plan'))
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
    const seeds: (string | undefined)[] = []
    const onSeed = (e: Event) => { seeds.push((e as CustomEvent).detail?.text) }
    window.addEventListener('side-seed', onSeed)
    try {
      act(() => { assistantProps!.onAsk!('why is this slow?') })
      await waitFor(() => expect(seeds).toContain('why is this slow?'))
    } finally {
      window.removeEventListener('side-seed', onSeed)
    }
    expect(store.getState().chat.activityTab).toBe('side')
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(inputProps!.value).toBe('')
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
      // PTY against a ~6 s cap; either leg answers, and the `settled` latch is
      // what guarantees the code-block button is told once and only once.
      await act(async () => { await vi.advanceTimersByTimeAsync(7_000) })
      await waitFor(() => expect(results.length).toBe(1), { timeout: 5_000 })
    } finally {
      window.removeEventListener('mc:run-in-terminal-result', onResult)
    }
    expect(results[0].reqId).toBe('r2')
    expect(typeof results[0].ok).toBe('boolean')
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
