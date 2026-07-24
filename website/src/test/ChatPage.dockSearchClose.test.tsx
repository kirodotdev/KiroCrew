/**
 * Regression test: opening a right-dock panel closes the find pane.
 *
 * The bug this pins: the right-hand dock is a single slot. The find/search
 * pane renders on `search.isOpen` and takes precedence — every other dock
 * panel (file viewer via `panel`, diff via `diffPanel`) is render-gated
 * behind `!search.isOpen`. The open handlers (`handleFileOpen`,
 * `handleOpenDiff`) set their panel state but originally did NOT close the
 * find pane, so the requested panel opened *underneath* the find pane and
 * only became visible once the user manually closed find ("layered").
 *
 * The fix: both open handlers call `search.close()` directly (the
 * `useMessageSearch` hook is hoisted above the handlers) so the find pane
 * closes and the requested panel renders immediately. This test drives the
 * real ChatPage handlers (via a stub AssistantMessage that calls the passed
 * `onOpenDiff` / `onFileOpen` props) with the find pane open and asserts the
 * find input disappears and the target panel appears.
 *
 * Uses the REAL usePanelState / useDiffPanel / useMessageSearch hooks so the
 * single-dock precedence + close-on-open wiring is exercised end to end.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// --- Stub the chat message components: AssistantMessage exposes the real
// handler props as clickable buttons so we can fire the actual ChatPage
// handlers without rendering the full markdown/tool tree.
vi.mock('../pages/chat', async () => {
  const React = await import('react')
  return {
    ChatFooter: () => null,
    McpInfoButton: () => null,
    UserMessage: () => null,
    AssistantMessage: (props: { onOpenDiff?: (f: string, m: string, o: string) => void; onFileOpen?: (f: string) => void }) =>
      React.createElement('div', null,
        React.createElement('button', { 'data-testid': 'open-diff', onClick: () => props.onOpenDiff?.('/f.txt', 'new', 'old') }, 'diff'),
        React.createElement('button', { 'data-testid': 'open-file', onClick: () => props.onFileOpen?.('/f.txt') }, 'file'),
      ),
  }
})

// Identifiable stubs for the two dock panels we assert on.
vi.mock('../components/MarkdownPanel', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'md-panel' }) }
})
vi.mock('../components/DiffPanel', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'diff-panel' }) }
})

// Everything else ChatPage pulls in that is irrelevant to dock/search wiring.
// NOTE: usePanelState / useDiffPanel / useMessageSearch are intentionally NOT
// mocked — the test exercises the real hooks. DetailPanel + SearchBar are also
// real so the find pane and diff pane actually mount/unmount.
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
// The real message list uses a custom virtualizer (useVirtualChat) driven by
// IntersectionObserver + height measurement, neither of which works in jsdom,
// so it mounts zero items. Mock it to mount every display item directly so the
// seeded assistant message (and its onOpenDiff/onFileOpen buttons) render.
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
      scrollToBottom: vi.fn(),
      scrollToIndexSmooth: vi.fn(),
      mountIndex: vi.fn(),
      measureRef: () => () => {},
      topSentinelRef: { current: null },
      bottomSentinelRef: { current: null },
      offsetBefore: 0,
      offsetAfter: 0,
    }
  },
}))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
// SearchResultsList is left REAL: SearchBar imports SEARCH_LISTBOX_ID /
// searchOptionId from it, and the list body only renders when there are
// matches (empty term → placeholder), so it stays out of the way.
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {}
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
  // handleFileOpen builds a file-read URL via this helper.
  fileReadUrl: (p: string) => `/api/file?path=${encodeURIComponent(p)}`,
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
  ok: true, status: 200,
  text: () => Promise.resolve('file content'),
  json: () => Promise.resolve({}),
}) as never

import ChatPage, { virtualKeyFor, turnLeadKey } from '../pages/ChatPage'
import type { DisplayItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

const ASSISTANT_MSG = {
  role: 'assistant',
  content: 'hello',
  ts: '2026-06-23T20:00:00Z',
  meta: { file_changes: [{ path: '/f.txt', status: 'modified' }] },
}

const renderChatPage = () => {
  const slot = { key: 'chat-1', title: 'chat-1', messages: 1, running: false, mode: '', created: '', last_ts: '' }
  apiMocks.chatSlots = vi.fn().mockResolvedValue([slot])
  // On mount ChatPage loads the active slot's detail; return the seeded
  // message so the post-mount reconcile keeps it (an empty list would wipe it).
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: [ASSISTANT_MSG], has_more: false, total: 1 })
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: false,
      slots: [slot], approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      activeSlot: 'chat-1',
      messages: [ASSISTANT_MSG], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
    } as never,
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
  return store
}

// Force the message list to render the seeded assistant message. Injecting via
// a post-mount dispatch (rather than only preloadedState) survives the mount
// effects; connected:false keeps switchSlot from firing and wiping it.
const seedMessage = (store: ReturnType<typeof createTestStore>) => {
  act(() => {
    store.dispatch({ type: 'chat/replaceMessages', payload: [ASSISTANT_MSG] })
  })
}

const FIND_PLACEHOLDER = 'Find in chat…'
const openFind = () => {
  act(() => {
    fireEvent.keyDown(document, { key: 'f', ctrlKey: true })
  })
}

describe('ChatPage – opening a dock panel closes the find pane', () => {
  beforeEach(() => {
    Object.keys(apiMocks).forEach(k => delete apiMocks[k])
  })

  it('Ctrl+F opens the find pane', async () => {
    renderChatPage()
    expect(screen.queryByPlaceholderText(FIND_PLACEHOLDER)).toBeNull()
    openFind()
    expect(await screen.findByPlaceholderText(FIND_PLACEHOLDER)).toBeTruthy()
  })

  it('opening the diff pane (diff pill) closes the find pane and shows the diff', async () => {
    const store = renderChatPage()
    seedMessage(store)
    openFind()
    expect(await screen.findByPlaceholderText(FIND_PLACEHOLDER)).toBeTruthy()

    const diffBtn = await screen.findByTestId('open-diff')
    act(() => {
      fireEvent.click(diffBtn)
    })

    // find pane gone, diff pane visible
    await waitFor(() => {
      expect(screen.queryByPlaceholderText(FIND_PLACEHOLDER)).toBeNull()
      expect(screen.getByTestId('diff-panel')).toBeTruthy()
    })
  })

  it('opening the file viewer (file chip) closes the find pane and shows the file', async () => {
    const store = renderChatPage()
    seedMessage(store)
    openFind()
    expect(await screen.findByPlaceholderText(FIND_PLACEHOLDER)).toBeTruthy()

    const fileBtn = await screen.findByTestId('open-file')
    act(() => {
      fireEvent.click(fileBtn)
    })

    // handleFileOpen is async (awaits file read) — wait for the panel.
    await waitFor(() => {
      expect(screen.getByTestId('md-panel')).toBeTruthy()
      expect(screen.queryByPlaceholderText(FIND_PLACEHOLDER)).toBeNull()
    })
  })
})


// The per-message identity resolver ChatPage feeds virtualKeyFor: prefer the
// optimistic clientTs (the #253 steer-bubble stability fix), then ts, then a
// stable minted id for ts-less messages. Mirrors the component's stableMsgKey
// so these unit tests exercise the real key derivation.
const makeMsgKey = () => {
  const ids = new WeakMap<ChatMessage, string>()
  let seq = 0
  return (m: ChatMessage): string => {
    const explicit = (m.meta?.clientTs as string | undefined) || m.ts
    if (explicit) return explicit
    let id = ids.get(m)
    if (!id) { id = `mid-${seq++}`; ids.set(m, id) }
    return id
  }
}

const single = (m: ChatMessage, idx: number): DisplayItem => ({ kind: 'single', msg: m, idx })
const turnOf = (items: DisplayItem[]): DisplayItem =>
  ({ kind: 'turn', items: items as never, complete: false })

describe('virtualKeyFor — #253 stability extended to the virtualizer/HeightCache key', () => {
  it('does NOT change when a steer_push echo overwrites ts with the server ts', () => {
    const msgKey = makeMsgKey()
    // Optimistic append: client ts, stashed as meta.clientTs by the reconcile.
    const optimistic: ChatMessage = { role: 'user', content: 'hi', cls: '', ts: 'client-1', meta: { clientTs: 'client-1', steer: true } }
    const before = virtualKeyFor(single(optimistic, 3), 3, msgKey)
    // Echo reconcile swaps ts → server ts but keeps meta.clientTs.
    const reconciled: ChatMessage = { ...optimistic, ts: 'server-2' }
    const after = virtualKeyFor(single(reconciled, 3), 3, msgKey)
    expect(after).toBe(before) // HeightCache entry NOT orphaned, no viewport lurch
  })

  it('is UNCHANGED when a single promotes into a grouped turn (mid-stream regroup)', () => {
    const msgKey = makeMsgKey()
    const lead: ChatMessage = { role: 'assistant', content: 'working…', cls: '', ts: 'a1' }
    // Before: the assistant renders as a standalone single.
    const asSingle = virtualKeyFor(single(lead, 5), 5, msgKey)
    // After: working steps accumulate and collapse into a turn led by the same
    // assistant message.
    const tool: ChatMessage = { role: 'tool', content: '🔧 grep', cls: '', ts: 't1' }
    const tool2: ChatMessage = { role: 'tool', content: '🔧 cat', cls: '', ts: 't2' }
    const asTurn = virtualKeyFor(turnOf([single(lead, 5), single(tool, 6), single(tool2, 7)]), 5, msgKey)
    expect(asTurn).toBe(asSingle) // same row identity → no remount / re-measure
  })

  it('a turn led by a group inherits the group key (stable across regroup)', () => {
    const msgKey = makeMsgKey()
    const grp: DisplayItem = { kind: 'group', msgs: [{ role: 'tool', content: '🔧 a', cls: '' }], startIdx: 9 } as never
    const asGroup = virtualKeyFor(grp, 9, msgKey)
    const asTurn = virtualKeyFor(turnOf([grp]), 9, msgKey)
    expect(asTurn).toBe(asGroup)
    expect(asGroup).toBe('grp-9')
  })

  it('a ts-less message keys on a stable minted id, NOT the array index', () => {
    const msgKey = makeMsgKey()
    // e.g. an error appended on the send-failure path — no ts, no clientTs.
    const errless: ChatMessage = { role: 'error', content: 'boom', cls: '' }
    // Same message object at two different positions (later rows truncated →
    // its display index shifted) must yield the SAME key so the following rows
    // don't mass-remount.
    const atFive = virtualKeyFor(single(errless, 5), 5, msgKey)
    const atNinety = virtualKeyFor(single(errless, 90), 90, msgKey)
    expect(atNinety).toBe(atFive)
    expect(atFive.startsWith('row-mid-')).toBe(true)
  })

  it('turnLeadKey unifies a single and its promoting turn on the same identity', () => {
    const msgKey = makeMsgKey()
    const lead: ChatMessage = { role: 'assistant', content: 'x', cls: '', ts: 'z9' }
    expect(turnLeadKey({ kind: 'single', msg: lead, idx: 0 }, msgKey)).toBe('row-z9')
  })
})
