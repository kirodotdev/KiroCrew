/**
 * Coverage-directed tests for ChatPage's render dispatch and its queued-message
 * / pinned-message handlers.
 *
 * Three areas of ChatPage.tsx that the existing ~620 ChatPage tests never
 * reach, all driven through a real `render(<ChatPage />)`:
 *
 *  1. `renderMessage`'s role dispatch. Existing suites only ever put `user`,
 *     `assistant` and `error` rows in the store, so the thinking / tool /
 *     workflow-launch / spawn-launch / file / queued / nudge / stop-event /
 *     recovery / notice / permission / mcp_oauth / workflow-completion /
 *     subagent-completion / cron-inject branches are all cold. Each row is
 *     built to satisfy the REAL predicate that selects it (extractWorkflowRunId,
 *     extractSpawnRunLaunch, parseRecoveryMessage, isWorkflowCompletionMessage,
 *     isSubagentCompletionMessage, renderMcpOAuthMessage), so a drift in any of
 *     those predicates fails these tests rather than silently skipping a branch.
 *
 *  2. The `kind: 'turn'` display item. `groupDisplayItems` only emits one when a
 *     turn has working steps AND more than two items, which no existing ChatPage
 *     fixture produces, so `renderTurnItem` and the TurnBlock wrapper are cold.
 *
 *  3. The QueueStack and PinnedMessagesPanel callbacks (cancel / interrupt /
 *     edit / reorder queued; jump-to-pin, unpin, pin-status auto-dismiss). Both
 *     components are stubbed as prop recorders so the callbacks can be invoked
 *     directly — the panels' own rendering is covered by QueueStack.test.tsx and
 *     chatPins.test.tsx.
 *
 * jsdom has no layout, so the virtualizer is stubbed to mount every item (the
 * same technique ChatPage.navFarJump.test.tsx uses) and `isAtBottom` is reported
 * false so the scroll-to-bottom affordance renders. Nothing else about the page
 * is faked: grouping, the render dispatch and the handlers run for real.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ApiError } from '../api/client'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

// --- Prop recorders for the two panels whose callbacks are under test --------

interface QueueStackProps {
  messages: ChatMessage[]
  onCancel: (queueId: string) => void
  onInterrupt: (queueId: string) => void
  onEdit: (queueId: string, content: string) => void
  onReorder: (queueId: string, direction: 'next' | 'later') => void
}
let queueProps: QueueStackProps | null = null

/** The pins contract ChatPage hands the side panel, which routes it to the
 *  Pins tab body. Captured at the SidePanel boundary because that is the seam
 *  ChatPage owns — the tab body itself is ActivityViewer's to render. */
interface PinsPanelProps {
  onJumpToPin: (messageTs: string, mid?: string) => void
  onUnpin: (id: string) => void
}
let pinsProps: PinsPanelProps | null = null

interface UserMessageProps {
  content: string
  pinned: boolean
  onTogglePin?: () => void
}
let userMsgProps: UserMessageProps | null = null

interface ChatInputProps {
  onAgentClick?: (rect: DOMRect) => void
}
let chatInputProps: ChatInputProps | null = null

interface AgentDropdownListProps {
  onSelect: (name: string) => void
}
interface DefaultAgentRowProps {
  agentName: string
  isDefault: boolean
  onSetDefault: () => void
}
let agentDropdownProps: AgentDropdownListProps | null = null
let defaultAgentRowProps: DefaultAgentRowProps | null = null

vi.mock('../components/QueueStack', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/QueueStack')>()
  return {
    ...actual,
    default: (props: QueueStackProps) => { queueProps = props; return null },
  }
})

// --- Child components stubbed to keep the render tree small ------------------
// (Same set the other ChatPage suites stub; the transcript CARDS are left real
// so their selecting predicates run for real.)
vi.mock('../pages/chat', async () => {
  const React = await import('react')
  return {
    ChatFooter: () => null,
    McpInfoButton: () => null,
    PinnedPrompt: () => null,
    UserMessage: (props: UserMessageProps) => {
      userMsgProps = props
      return React.createElement('div', { 'data-testid': 'user-msg' }, props.content)
    },
    // The two props the memoized renderMessage derives from the paging cursor, so a
    // test can see a STALE closure: both go quiet when the flag is read from one.
    AssistantMessage: ({ content, forkIndex, onLoadEarlier }: { content: string; forkIndex?: number; onLoadEarlier?: () => void }) =>
      React.createElement('div', {
        'data-testid': 'assistant-msg',
        'data-fork-index': forkIndex === undefined ? 'none' : String(forkIndex),
        'data-can-page': onLoadEarlier ? 'yes' : 'no',
      }, content),
  }
})
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content?: string }) => content ?? null,
}))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({
  default: (props: AgentDropdownListProps) => {
    agentDropdownProps = props
    return <div data-testid="agent-dropdown" />
  },
  ManageAgentsFooter: () => null,
  DefaultAgentRow: (props: DefaultAgentRowProps) => { defaultAgentRowProps = props; return null },
}))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../components/ChatInput', () => ({
  default: (props: ChatInputProps) => {
    chatInputProps = props
    return null
  },
}))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SidePanel', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../pages/chat/SidePanel')>()
  return { ...actual, default: (props: PinsPanelProps) => { pinsProps = props; return null } }
})
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: {
    compact: { messages: '900px', input: '916px' },
    comfortable: { messages: '84%', input: '85%' },
    full: { messages: '92%', input: '93%' },
  },
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({
  useAgents: () => ({ agents: [{ name: 'kirocrew' }, { name: 'reviewer' }], defaultAgent: 'kirocrew' }),
}))
vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }),
  voiceInputSupported: false,
}))

// Mounts every display item so ChatPage's own row renderer runs for real, and
// reports `isAtBottom: false` so the scroll-to-bottom affordance renders.
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
      isAtBottom: false,
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

const pinsListMock = vi.fn()
const pinsCreateMock = vi.fn()
const pinsRemoveMock = vi.fn()
vi.mock('../api/pins', () => ({
  PIN_PREVIEW_INPUT_MAX_CHARS: 4096,
  pinsApi: {
    list: (...a: unknown[]) => pinsListMock(...a),
    create: (...a: unknown[]) => pinsCreateMock(...a),
    remove: (...a: unknown[]) => pinsRemoveMock(...a),
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
  // Mirrors the real class shape so a rejection carries the same fields the
  // production error does, rather than a hand-rolled object.
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
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

const msg = (role: string, content: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  role, content, cls: '', ...extra,
})

interface RenderOpts {
  /** Extra preloaded `chat` slice fields. */
  chat?: Record<string, unknown>
  /** Responses `api.chatSlotDetail` yields, in call order (head page first). */
  detailPages?: { messages: ChatMessage[]; has_more: boolean; total: number }[]
  /** Browser URL to mount at — the token / prefill effects read it directly
   *  off `window.location`, not off the router. */
  url?: string
}

/** Renders ChatPage, then pushes `messages` into the active slot.
 *
 *  The messages are dispatched AFTER mount rather than preloaded: ChatPage's
 *  slot-activation effect fetches the slot detail on mount and replaces the
 *  transcript with the response, so a preloaded list is discarded before the
 *  first paint. */
function renderChatPage(messages: ChatMessage[], opts: RenderOpts = {}) {
  const { chat = {}, detailPages, url } = opts
  if (url) window.history.replaceState({}, '', url)
  apiMocks.chatSlots = vi.fn().mockResolvedValue([SLOT])
  if (detailPages) {
    const detail = vi.fn()
    detailPages.forEach(page => detail.mockResolvedValueOnce(page))
    detail.mockResolvedValue({ messages: [], has_more: false, total: 0 })
    apiMocks.chatSlotDetail = detail
  } else {
    apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({
      messages, has_more: false, total: messages.length,
    })
  }
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: false, slots: [SLOT],
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint',
      sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    },
    chat: {
      activeSlot: 'chat-1', messages: [],
      slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'logs',
      slotActivity: {}, slotHistory: [],
      ...chat,
    },
  } as unknown as Partial<RootState>)
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

/** All text currently on screen — ChatPage spans several sibling roots. */
const shown = () => document.body.textContent || ''
/** Mounted transcript rows (one per display item). */
const rows = () => document.body.querySelectorAll('[data-display-index]')

/** Records sessionStorage writes so a hand-off that is consumed (and deleted)
 *  in the same tick is still observable. */
const setItemSpy = vi.spyOn(Storage.prototype, 'setItem')

beforeEach(() => {
  queueProps = null
  pinsProps = null
  userMsgProps = null
  chatInputProps = null
  agentDropdownProps = null
  defaultAgentRowProps = null
  sessionStorage.clear()
  setItemSpy.mockClear()
  window.history.replaceState({}, '', '/chat')
  for (const k of Object.keys(apiMocks)) delete apiMocks[k]
  pinsListMock.mockReset().mockResolvedValue({ pins: [] })
  pinsCreateMock.mockReset().mockImplementation((p: { mid: string; message_ts: string; role: string; preview: string }) =>
    Promise.resolve({
      id: `pin-${p.mid}`, slot_key: 'chat-1', mid: p.mid, message_ts: p.message_ts,
      role: p.role, preview: p.preview, pinned_at: '2026-08-01T12:00:00Z',
    }))
  pinsRemoveMock.mockReset().mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ChatPage agent-switch failure feedback', () => {
  it('stores the failure message; the picker still closes as it always has', async () => {
    const switchRequest = apiSpy('chatSlotAgent')
    // A failure the endpoint really produces, carried in the production error
    // type, so this pins the actual plumbing rather than a shape we invented.
    switchRequest.mockRejectedValueOnce(
      new ApiError(400, 'invalid agent name', JSON.stringify({ error: 'invalid agent name' })),
    )
    const { store } = renderChatPage([])

    await waitFor(() => expect(chatInputProps?.onAgentClick).toBeTypeOf('function'))
    act(() => {
      chatInputProps!.onAgentClick!({ left: 40, top: 80 } as DOMRect)
    })
    await waitFor(() => expect(screen.getByTestId('agent-dropdown')).toBeInTheDocument())

    act(() => { agentDropdownProps!.onSelect('reviewer') })

    await waitFor(() => {
      expect(switchRequest).toHaveBeenCalledWith('chat-1', 'reviewer')
      expect(store.getState().chat.agentSwitchNotice?.message).toBe('invalid agent name')
    })
    // Unchanged from before this fix: the `onSelect` call site closes the
    // dropdown synchronously without awaiting the switch, so it has always
    // closed on failure too. Pinned so that stays a deliberate choice — the
    // notice is what tells the user the switch did not take.
    expect(screen.queryByTestId('agent-dropdown')).not.toBeInTheDocument()
  })

  it('closes the picker when the switch succeeds', async () => {
    const switchRequest = apiSpy('chatSlotAgent')
    switchRequest.mockResolvedValueOnce(undefined)
    const { store } = renderChatPage([])

    await waitFor(() => expect(chatInputProps?.onAgentClick).toBeTypeOf('function'))
    act(() => {
      chatInputProps!.onAgentClick!({ left: 40, top: 80 } as DOMRect)
    })
    await waitFor(() => expect(screen.getByTestId('agent-dropdown')).toBeInTheDocument())

    act(() => { agentDropdownProps!.onSelect('reviewer') })

    await waitFor(() => expect(screen.queryByTestId('agent-dropdown')).not.toBeInTheDocument())
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })
})
describe('ChatPage default-agent footer row', () => {
  it('points the row at the session\'s own agent and writes that one', async () => {
    const setDefault = apiSpy('setDefaultAgent')
    setDefault.mockResolvedValue(undefined)
    renderChatPage([])

    await waitFor(() => expect(chatInputProps?.onAgentClick).toBeTypeOf('function'))
    act(() => { chatInputProps!.onAgentClick!({ left: 40, top: 80 } as DOMRect) })
    await waitFor(() => expect(defaultAgentRowProps).not.toBeNull())

    // An agent-less slot resolves to the configured default agent (the
    // useAgents mock above returns defaultAgent: 'kirocrew'), not the
    // literal 'default' placeholder the pre-fix fallback rendered.
    expect(defaultAgentRowProps!.agentName).toBe('kirocrew')
    act(() => { defaultAgentRowProps!.onSetDefault() })
    await waitFor(() => expect(setDefault).toHaveBeenCalledWith('kirocrew'))
  })
})

describe('ChatPage renderMessage — role dispatch', () => {
  it('renders a thinking row as a reasoning block and drops an empty one', async () => {
    renderChatPage([
      msg('thinking', 'weighing two options', { ts: 'r1' }),
      msg('thinking', '', { ts: 'r2' }),
    ])
    // The trace is folded behind its own disclosure, so the label is what
    // reaches the transcript — the point being that it is NOT an ordinary
    // assistant bubble. A block that merely mounts is settled, so it carries
    // the finished-form label, not the in-progress one.
    await waitFor(() => expect(shown()).toContain('Thought process'))
    expect(screen.queryByTestId('assistant-msg')).not.toBeInTheDocument()
    // The empty thinking row still occupies a display slot but renders nothing.
    expect(rows()).toHaveLength(2)
  })

  it('drops a tool completion row that is not a 🔧 call', async () => {
    renderChatPage([
      msg('tool', '✅ done', { ts: 't1' }),
      msg('notice', 'session resumed', { ts: 'n1' }),
    ])
    await waitFor(() => expect(shown()).toContain('session resumed'))
    expect(shown()).not.toContain('✅ done')
  })

  it('renders a 🔧 tool row as a tool-call line', async () => {
    renderChatPage([
      msg('tool', '🔧 grep', { ts: 't1', meta: { output: 'no matches' } }),
    ])
    await waitFor(() => expect(shown()).toContain('grep'))
  })

  it('renders a workflow_run launch as its own inline card, not a tool pill', async () => {
    renderChatPage([
      msg('tool', '🔧 workflow_run', {
        ts: 't1',
        meta: {
          input: JSON.stringify({ name: 'nightly digest' }),
          output: 'Started workflow run `wf_abc123`',
        },
      }),
    ])
    await waitFor(() => expect(shown()).toContain('nightly digest'))
  })

  it('renders a spawn_run launch as a sub-agent run card', async () => {
    const output = [
      'Spawned 2 subagent(s).',
      '  aaaa1111: read the specs',
      '  bbbb2222: read the code',
    ].join('\n')
    renderChatPage([msg('tool', '🔧 spawn_run', { ts: 't1', meta: { output } })])
    // The card is keyed off the parsed launch, not the raw tool name.
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(shown()).not.toContain('Spawned 2 subagent(s).')
  })

  it('renders a file row as a file card and falls through when its JSON is broken', async () => {
    renderChatPage([
      msg('file', JSON.stringify({ filename: 'report.txt', size: 12 }), { ts: 'f1' }),
      msg('file', 'not json at all', { ts: 'f2' }),
    ])
    await waitFor(() => expect(shown()).toContain('report.txt'))
    // The unparseable row falls through to the default bubble rather than
    // throwing or rendering nothing.
    expect(shown()).toContain('not json at all')
  })

  it('renders nothing in the transcript for queued and permission rows', async () => {
    renderChatPage([
      msg('queued', 'later please', { ts: 'q1', meta: { queueId: 'q1' } }),
      msg('permission', 'may I run ls?', { ts: 'p1', meta: { approval_id: 'a1' } }),
      msg('notice', 'anchor row', { ts: 'n1' }),
    ])
    await waitFor(() => expect(shown()).toContain('anchor row'))
    expect(shown()).not.toContain('later please')
    expect(shown()).not.toContain('may I run ls?')
  })

  it('renders a nudge row as its own auto-nudge card, not an assistant bubble', async () => {
    renderChatPage([
      msg('nudge', 'Check the PR again and report only real signals.', {
        ts: 'g1', meta: { loopId: 'loop-1', cycle: 3 },
      }),
    ])
    await waitFor(() => expect(shown()).toContain('Auto-nudge'))
    expect(rows()).toHaveLength(1)
    expect(screen.queryByTestId('assistant-msg')).not.toBeInTheDocument()
    expect(screen.queryByTestId('user-msg')).not.toBeInTheDocument()
  })

  it('renders a stop_event row as a stop card', async () => {
    renderChatPage([
      msg('assistant', 'stopped early', {
        ts: 's1', kind: 'stop_event', meta: { id: 'stop-1', reason: 'user_stop' },
      }),
    ])
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(screen.queryByTestId('assistant-msg')).not.toBeInTheDocument()
  })

  it('renders an automatic-recovery inject as a recovery card', async () => {
    renderChatPage([
      msg('inject', '[Stalled turn — automatic recovery]\nContinue from where you stopped.', { ts: 'i1' }),
    ])
    await waitFor(() => expect(rows()).toHaveLength(1))
    // The card names the event instead of printing the instruction prose.
    expect(shown()).not.toContain('Continue from where you stopped.')
  })

  it('renders a cron inject as a labelled bubble with the wrapper tags stripped', async () => {
    const content = [
      '[Cron notification from "daily ship report"]',
      'Three PRs merged today.',
      '[End of cron notification]',
    ].join('\n')
    renderChatPage([
      msg('inject', content, { ts: 'i1', meta: { cronLabel: 'daily ship report' } }),
    ])
    await waitFor(() => expect(shown()).toContain('Three PRs merged today.'))
    expect(shown()).toContain('daily ship report')
    expect(shown()).not.toContain('[End of cron notification]')
  })

  it('renders a notice row as a plain centred strip', async () => {
    renderChatPage([msg('notice', 'Context compacted.', { ts: 'n1' })])
    await waitFor(() => expect(shown()).toContain('Context compacted.'))
    expect(screen.queryByTestId('assistant-msg')).not.toBeInTheDocument()
  })

  it('renders an mcp_oauth row as a banner when it carries an authorize url', async () => {
    renderChatPage([
      msg('mcp_oauth', '', {
        ts: 'o1',
        meta: { server_name: 'github', oauth_url: 'https://example.com/authorize' },
      }),
    ])
    await waitFor(() => expect(shown()).toContain('github'))
  })

  it('renders no mcp_oauth banner when the row has no url and no outcome', async () => {
    renderChatPage([
      msg('mcp_oauth', '', { ts: 'o2', meta: { server_name: 'gitlab' } }),
      msg('notice', 'anchor row', { ts: 'n1' }),
    ])
    await waitFor(() => expect(shown()).toContain('anchor row'))
    expect(shown()).not.toContain('gitlab')
  })

  it('renders a workflow completion event as a status card, not raw JSON', async () => {
    const content = [
      '[Workflow completion event]',
      'Workflow `issue triage` (wf_abc123) → **finished**',
      '',
      'Result: 4 issues triaged.',
      'Use workflow_result(wf_abc123) for the full stream.',
    ].join('\n')
    renderChatPage([msg('assistant', content, { ts: 'w1' })])
    await waitFor(() => expect(shown()).toContain('issue triage'))
    expect(shown()).not.toContain('Use workflow_result(')
    expect(screen.queryByTestId('assistant-msg')).not.toBeInTheDocument()
  })

  it('renders a sub-agent completion event as an outcome row, not a chat bubble', async () => {
    const content = [
      '[Subagent completion event]',
      'Agent `53e3e5eb` (kirocrew) completed ✅',
      'Task: Add two short UI labels to the German catalog',
      '',
      'Added both keys and ran the parity check.',
    ].join('\n')
    renderChatPage([msg('subagent', content, { ts: 'a1' })])
    await waitFor(() => expect(shown()).toContain('53e3e5eb'))
    expect(screen.queryByTestId('assistant-msg')).not.toBeInTheDocument()
  })

  it('falls back to the assistant bubble when a completion prefix does not parse', async () => {
    renderChatPage([
      msg('assistant', '[Subagent completion event]\nnothing parseable here', { ts: 'a1' }),
    ])
    await waitFor(() => expect(screen.getByTestId('assistant-msg')).toBeInTheDocument())
    expect(shown()).toContain('nothing parseable here')
  })

  it('offers the scroll-to-bottom affordance while the list is scrolled up', async () => {
    renderChatPage([msg('notice', 'anchor row', { ts: 'n1' })])
    const btn = await screen.findByLabelText('Scroll to bottom')
    fireEvent.click(btn)
    expect(btn).toBeInTheDocument()
  })
})

describe('ChatPage transcript grouping — collapsed turns', () => {
  // groupDisplayItems only emits a `turn` when the items after a user message
  // have working steps AND number more than two; anything shorter is spread as
  // loose rows. Both shapes are asserted so the boundary is pinned.
  it('folds a multi-step turn into one collapsed display row', async () => {
    renderChatPage([
      msg('user', 'find the leak', { ts: 'u1' }),
      msg('assistant', 'looking', { ts: 'a1' }),
      msg('tool', '🔧 grep', { ts: 't1', meta: { output: '' } }),
      msg('tool', '🔧 read', { ts: 't2', meta: { output: '' } }),
      msg('assistant', 'found it in the pool', { ts: 'a2' }),
    ])
    await waitFor(() => expect(shown()).toContain('find the leak'))
    // The user row stays its own display item; the four working steps collapse
    // into a single turn row after it.
    expect(rows()).toHaveLength(2)
  })

  it('keeps a two-step turn as loose rows instead of collapsing it', async () => {
    renderChatPage([
      msg('user', 'quick question', { ts: 'u1' }),
      msg('assistant', 'quick answer', { ts: 'a1' }),
    ])
    await waitFor(() => expect(shown()).toContain('quick question'))
    expect(rows()).toHaveLength(2)
    expect(shown()).toContain('quick answer')
  })

  it('skips a non-🔧 tool completion inside a collapsed turn', async () => {
    renderChatPage([
      msg('user', 'find the leak', { ts: 'u1' }),
      msg('assistant', 'looking', { ts: 'a1' }),
      msg('tool', '🔧 grep', { ts: 't1', meta: { output: '' } }),
      msg('tool', '✅ grep finished', { ts: 't2' }),
      msg('assistant', 'found it', { ts: 'a2' }),
    ])
    await waitFor(() => expect(shown()).toContain('find the leak'))
    expect(shown()).not.toContain('grep finished')
  })
})

describe('ChatPage queued-message controls', () => {
  const queued = (id: string, content: string) =>
    msg('queued', content, { ts: id, meta: { queueId: id } })

  it('cancelling a queued card restores its text and removes it optimistically', async () => {
    renderChatPage([queued('q1', 'run the migration'), queued('q2', 'then deploy')])
    await waitFor(() => expect(queueProps).not.toBeNull())
    expect(queueProps!.messages.map(m => m.content)).toEqual(['run the migration', 'then deploy'])

    act(() => queueProps!.onCancel('q1'))
    await waitFor(() => expect(apiMocks.cancelQueuedMessage).toHaveBeenCalledWith('chat-1', 'q1'))
    await waitFor(() => expect(queueProps!.messages.map(m => m.content)).toEqual(['then deploy']))
  })

  it('interrupting a queued card calls the interrupt endpoint for that slot', async () => {
    renderChatPage([queued('q1', 'run the migration')])
    await waitFor(() => expect(queueProps).not.toBeNull())

    act(() => queueProps!.onInterrupt('q1'))
    await waitFor(() => expect(apiMocks.interruptSlot).toHaveBeenCalledWith('chat-1', 'q1'))
  })

  it('editing a queued card trims the text and ignores a whitespace-only edit', async () => {
    renderChatPage([queued('q1', 'run the migration')])
    await waitFor(() => expect(queueProps).not.toBeNull())
    const editSpy = apiSpy('editQueuedMessage')

    act(() => queueProps!.onEdit('q1', '   '))
    expect(editSpy).not.toHaveBeenCalled()

    act(() => queueProps!.onEdit('q1', '  deploy instead  '))
    await waitFor(() => expect(editSpy).toHaveBeenCalledWith('chat-1', 'q1', 'deploy instead'))
    await waitFor(() => expect(queueProps!.messages[0].content).toBe('deploy instead'))
  })

  it('reordering submits the FULL queue order, including non-interactive deliveries', async () => {
    // The delivery row is filtered out of the visible cards but must still ride
    // along in the submitted order, or the backend would re-append it at the
    // tail and silently demote it.
    const delivery = msg('queued', '[Subagent completion event]\nAgent `abcd1234` completed ✅', {
      ts: 'qd', meta: { queueId: 'qd' },
    })
    renderChatPage([delivery, queued('q1', 'first'), queued('q2', 'second')])
    await waitFor(() => expect(queueProps).not.toBeNull())
    expect(queueProps!.messages.map(m => m.ts)).toEqual(['q1', 'q2'])

    act(() => queueProps!.onReorder('q2', 'next'))
    await waitFor(() =>
      expect(apiMocks.reorderQueuedMessages).toHaveBeenCalledWith('chat-1', ['qd', 'q2', 'q1']),
    )
  })

  it('refuses a reorder that would move a card past either end of the queue', async () => {
    renderChatPage([queued('q1', 'first'), queued('q2', 'second')])
    await waitFor(() => expect(queueProps).not.toBeNull())
    const reorderSpy = apiSpy('reorderQueuedMessages')

    act(() => queueProps!.onReorder('q1', 'next'))    // already first
    act(() => queueProps!.onReorder('q2', 'later'))   // already last
    act(() => queueProps!.onReorder('nope', 'next'))  // unknown id
    expect(reorderSpy).not.toHaveBeenCalled()
  })
})

describe('ChatPage pinned-messages panel', () => {
  /** Opens the side panel (which hosts the Pins tab) and returns once the pins
   *  contract ChatPage passes it has been recorded. */
  async function openPins(messages: ChatMessage[], opts: RenderOpts = {}) {
    renderChatPage(messages, opts)
    fireEvent.click(await screen.findByLabelText('Open activity panel'))
    await waitFor(() => expect(pinsProps).not.toBeNull())
  }

  const highlighted = () => document.body.querySelector('.animate-msg-highlight')
  const UNAVAILABLE = 'This pinned message is no longer available in the loaded history.'

  it('jumping to a loaded pin highlights that message and clears the highlight later', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await openPins([
      msg('user', 'the pinned one', { ts: 'u1', meta: { mid: 'm-1' } }),
      msg('assistant', 'reply', { ts: 'a1' }),
    ])

    act(() => pinsProps!.onJumpToPin('u1', 'm-1'))
    await waitFor(() => expect(highlighted()).not.toBeNull())

    // The highlight is time-boxed, not sticky.
    await act(async () => { vi.advanceTimersByTime(3100) })
    expect(highlighted()).toBeNull()
  })

  it('falls back to timestamp matching when the pin carries no message id', async () => {
    await openPins([msg('user', 'legacy pin target', { ts: 'u1' })])

    act(() => pinsProps!.onJumpToPin('u1'))
    await waitFor(() => expect(highlighted()).not.toBeNull())
  })

  it('reports an unavailable pin when the message is absent and no history remains', async () => {
    await openPins([msg('user', 'something else', { ts: 'u1' })])

    act(() => pinsProps!.onJumpToPin('missing-ts', 'm-gone'))
    expect(await screen.findByText(UNAVAILABLE)).toBeInTheDocument()
  })

  it('surfaces an unpin failure and auto-dismisses the notice', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    pinsRemoveMock.mockRejectedValue(new Error('nope'))
    await openPins([msg('user', 'pinned', { ts: 'u1', meta: { mid: 'm-1' } })])

    act(() => pinsProps!.onUnpin('pin-1'))
    expect(await screen.findByText('Could not unpin the message. Try again.')).toBeInTheDocument()

    await act(async () => { vi.advanceTimersByTime(8100) })
    await waitFor(() =>
      expect(screen.queryByText('Could not unpin the message. Try again.')).not.toBeInTheDocument(),
    )
  })
})

describe('ChatPage per-message pin toggle', () => {
  const PIN = {
    id: 'pin-1', slot_key: 'chat-1', mid: 'm-1', message_ts: 'u1',
    role: 'user', preview: 'pin me', pinned_at: '2026-08-01T12:00:00Z',
  }

  it('offers no pin action on a row without a message id', async () => {
    renderChatPage([msg('user', 'no mid here', { ts: 'u1' })])
    await waitFor(() => expect(userMsgProps).not.toBeNull())
    expect(userMsgProps!.onTogglePin).toBeUndefined()
    expect(userMsgProps!.pinned).toBe(false)
  })

  it('pins a row that is not pinned yet', async () => {
    renderChatPage([msg('user', 'pin me', { ts: 'u1', meta: { mid: 'm-1' } })])
    await waitFor(() => expect(userMsgProps?.onTogglePin).toBeInstanceOf(Function))
    expect(userMsgProps!.pinned).toBe(false)

    await act(async () => { userMsgProps!.onTogglePin!() })
    await waitFor(() => expect(pinsCreateMock).toHaveBeenCalledWith(
      expect.objectContaining({ mid: 'm-1', message_ts: 'u1', role: 'user' }),
    ))
    expect(pinsRemoveMock).not.toHaveBeenCalled()
  })

  it('unpins a row that is already pinned', async () => {
    pinsListMock.mockResolvedValue({ pins: [PIN] })
    renderChatPage([msg('user', 'pin me', { ts: 'u1', meta: { mid: 'm-1' } })])
    await waitFor(() => expect(userMsgProps?.pinned).toBe(true))

    await act(async () => { userMsgProps!.onTogglePin!() })
    await waitFor(() => expect(pinsRemoveMock).toHaveBeenCalled())
    expect(pinsCreateMock).not.toHaveBeenCalled()
  })

  it('swallows a pin failure rather than throwing out of the click handler', async () => {
    pinsCreateMock.mockRejectedValue(new Error('nope'))
    renderChatPage([msg('user', 'pin me', { ts: 'u1', meta: { mid: 'm-1' } })])
    await waitFor(() => expect(userMsgProps?.onTogglePin).toBeInstanceOf(Function))

    await act(async () => { userMsgProps!.onTogglePin!() })
    expect(await screen.findByText('Could not pin the message. Try again.')).toBeInTheDocument()
  })
  it("a session's FIRST pin opens the panel, so the pin has a visible destination", async () => {
    // Tab CREATION is no longer asserted here: Pins is a content-managed pinned
    // view, so SidePanel's reconcile adds it from pin content (SidePanel is
    // mocked to null in this file, so it cannot run). What ChatPage still owns is
    // opening the panel once, on the first pin.
    const { store } = renderChatPage([msg('user', 'pin me', { ts: 'u1', meta: { mid: 'm-1' } })])
    await waitFor(() => expect(userMsgProps?.onTogglePin).toBeInstanceOf(Function))
    expect(store.getState().chat.activityOpen).toBe(false)

    await act(async () => { userMsgProps!.onTogglePin!() })
    await waitFor(() => expect(store.getState().chat.activityOpen).toBe(true))
  })

  it('a LATER pin does not re-open the panel', async () => {
    // Only the first pin is a reveal; re-opening a panel the user closed on every
    // subsequent pin would fight them.
    pinsListMock.mockResolvedValue({ pins: [PIN] })
    const { store } = renderChatPage([
      msg('user', 'already pinned', { ts: 'u1', meta: { mid: 'm-1' } }),
      msg('user', 'pin me too', { ts: 'u2', meta: { mid: 'm-2' } }),
    ])
    await waitFor(() => expect(userMsgProps?.onTogglePin).toBeInstanceOf(Function))

    await act(async () => { userMsgProps!.onTogglePin!() })
    await waitFor(() => expect(pinsCreateMock).toHaveBeenCalled())
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('a first pin made from an open search does not open the panel over it', async () => {
    // Pinning is not a navigation request. Someone who searched the transcript to
    // FIND the message they are pinning would otherwise lose the find pane and
    // its results on the very click that acts on a result.
    const { store } = renderChatPage([msg('user', 'pin me', { ts: 'u1', meta: { mid: 'm-1' } })])
    await waitFor(() => expect(userMsgProps?.onTogglePin).toBeInstanceOf(Function))
    // Cmd+F is the real entry point (document-level handler in useMessageSearch).
    act(() => { fireEvent.keyDown(document, { key: 'f', metaKey: true }) })

    await act(async () => { userMsgProps!.onTogglePin!() })
    await waitFor(() => expect(pinsCreateMock).toHaveBeenCalled())
    expect(store.getState().chat.activityOpen).toBe(false)
  })

})

describe('ChatPage URL prompt hand-off', () => {
  /** Base64url payload token, the shape the channel redirect issues. */
  const tokenFor = (payload: Record<string, unknown>) =>
    `${btoa(JSON.stringify(payload)).replace(/\+/g, '-').replace(/\//g, '_')}.sig`

  /** A prefill hand-off is consumed (and deleted) by the slot-restore effect as
   *  soon as it names the active slot, so the WRITE is what these tests read. */
  const prefillWrites = () =>
    setItemSpy.mock.calls
      .filter(([k]) => k === 'kirocrew_prefill')
      .map(([, v]) => JSON.parse(v as string) as { slotKey: string; prompt: string })

  it('moves a ?prefill= prompt into session storage and strips it from the URL', async () => {
    renderChatPage([], { url: '/chat?sid=chat-1&prefill=deploy%20the%20thing&keep=1' })
    await waitFor(() => expect(prefillWrites().length).toBeGreaterThan(0))
    expect(prefillWrites()[0]).toMatchObject({ slotKey: 'chat-1', prompt: 'deploy the thing' })
    // Only `prefill` is dropped; unrelated params survive.
    expect(window.location.search).not.toContain('prefill')
    expect(window.location.search).toContain('keep=1')
  })

  it('creates a session and links it back to the originating thread', async () => {
    const token = tokenFor({ prompt: 'look into this', channel: 'C123', thread_ts: '1700.5' })
    apiMocks.createChatSlot = vi.fn().mockResolvedValue({ key: 'chat-9', title: 'chat-9' })
    renderChatPage([], { url: `/chat?token=${token}` })

    await waitFor(() => expect(apiMocks.slackLink).toHaveBeenCalledWith('chat-9', 'C123', '1700.5'))
    expect(prefillWrites().at(-1)).toMatchObject({ slotKey: 'chat-9', prompt: 'look into this' })
  })

  it('ignores a token whose payload carries no prompt, but still strips it', async () => {
    const token = tokenFor({ channel: 'C123' })
    const createSpy = apiSpy('createChatSlot')
    renderChatPage([], { url: `/chat?token=${token}` })

    await waitFor(() => expect(window.location.search).toBe(''))
    expect(prefillWrites()).toHaveLength(0)
    expect(createSpy).not.toHaveBeenCalled()
  })
})

describe('ChatPage search scope disclosure', () => {
  /** Open the find pane the real way -- the Cmd+F handler in useMessageSearch --
   *  and type a term, so the count span renders. */
  const openSearchAndType = (term: string) => {
    act(() => { fireEvent.keyDown(document, { key: 'f', metaKey: true }) })
    fireEvent.change(screen.getByPlaceholderText('Find in chat…'), { target: { value: term } })
  }

  it('qualifies the scope while the paging cursor does not describe the active slot', async () => {
    // switchSlot.pending nulls the cursor key while leaving slotHasMore describing
    // the outgoing slot, so false here is not this slot's answer (chatSlice:3575).
    const { store } = renderChatPage([msg('assistant', 'hello there', { ts: 'a1' })], {
      chat: { slotMessages: {} },
    })
    await waitFor(() => expect(shown()).toContain('hello there'))
    expect(store.getState().chat.slotHasMore).toBe(false)
    act(() => { store.dispatch({ type: 'chat/switchSlot/pending', meta: { arg: 'chat-1', requestId: 'r-scope' } }) })
    expect(store.getState().chat.slotCursorKey).toBeNull()

    openSearchAndType('zzz-no-match')
    await waitFor(() => expect(shown()).toMatch(/in loaded history/i))
  })

  it('reads as complete once the cursor DOES describe the active slot', async () => {
    // Opposite direction: the qualifier must not become unconditional, or every
    // fully-loaded chat claims its search was partial.
    const { store } = renderChatPage([msg('assistant', 'hello there', { ts: 'a1' })])
    await waitFor(() => expect(shown()).toContain('hello there'))
    expect(store.getState().chat.slotCursorKey).toBe('chat-1')

    openSearchAndType('zzz-no-match')
    await waitFor(() => expect(shown()).toContain('No results'))
    expect(shown()).not.toMatch(/in loaded history/i)
  })
})

/** `renderMessage` is memoized, and a switch BACK to an already-loaded chat is the
 *  one path that restores the paging cursor while leaving every one of its deps
 *  untouched. Activating another slot leaves the URL slug behind, so ChatPage's own
 *  slug effect switches straight back: `switchSlot.pending` installs the target's
 *  CACHED array — a new reference, so the renderer is rebuilt while the cursor is
 *  still null — and `fulfilled` then restores the cursor while `sameTranscript`
 *  skips the `messages` write. With the cursor flag missing from the dep list the
 *  rebuilt renderer keeps `cursorIsForActiveSlot === false`, so Fork/Plan stay shut
 *  on a chat that is fully loaded and holds a valid cursor. */
describe('ChatPage fork affordance — cursor recovery on a switch back', () => {
  const A = () => msg('assistant', 'alpha transcript', { ts: 'a1' })
  const B = () => msg('assistant', 'bravo transcript', { ts: 'b1' })
  /** What the memoized renderer currently believes, read off the row it produced. */
  const row = () => screen.getAllByTestId('assistant-msg').slice(-1)[0]

  /** Activate another slot through the real reducers. ChatPage's slug effect then
   *  drives the switch back on its own, which is the sequence under test. */
  const activateOtherSlot = (store: { dispatch: (a: unknown) => void }) => {
    act(() => { store.dispatch({ type: 'chat/switchSlot/pending', meta: { arg: 'chat-2', requestId: 'r-to-b' } }) })
    act(() => {
      store.dispatch({
        type: 'chat/switchSlot/fulfilled',
        meta: { arg: 'chat-2', requestId: 'r-to-b' },
        payload: { key: 'chat-2', messages: [B()], running: false, hasMore: false, queue: [], nextBefore: 0, total: 1 },
      })
    })
  }

  it('re-opens Fork once the cursor lands on a switch back to an identical transcript', async () => {
    const { store } = renderChatPage([A()], { chat: { slotMessages: {} } })
    await waitFor(() => expect(shown()).toContain('alpha transcript'))
    expect(store.getState().chat.slotCursorKey).toBe('chat-1')
    expect(row().getAttribute('data-fork-index')).not.toBe('none')

    activateOtherSlot(store)

    // The slug effect's switch back has landed: cursor valid, nothing left to page.
    await waitFor(() => {
      expect(store.getState().chat.activeSlot).toBe('chat-1')
      expect(store.getState().chat.slotCursorKey).toBe('chat-1')
    })
    expect(store.getState().chat.slotHasMore).toBe(false)
    await waitFor(() => expect(shown()).toContain('alpha transcript'))

    // Fork is operable again, and the cursor-gated paging handler came back with it.
    expect(row().getAttribute('data-fork-index')).not.toBe('none')
    expect(row().getAttribute('data-can-page')).toBe('yes')
  })

  it('keeps Fork shut while the cursor genuinely still names the chat we left', async () => {
    // Opposite direction, so the fix cannot buy an operable Fork by making the trust
    // predicate unconditional: a frozen fetch holds the switch back genuinely in flight.
    const { store } = renderChatPage([A()], { chat: { slotMessages: {} } })
    await waitFor(() => expect(shown()).toContain('alpha transcript'))

    apiMocks.chatSlotDetail = vi.fn(() => new Promise(() => {}))
    activateOtherSlot(store)

    await waitFor(() => expect(store.getState().chat.slotCursorKey).toBeNull())

    expect(row().getAttribute('data-fork-index')).toBe('none')
    expect(row().getAttribute('data-can-page')).toBe('no')
  })
})
