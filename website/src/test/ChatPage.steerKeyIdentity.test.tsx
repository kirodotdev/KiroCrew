// Feature: chat-virtualizer — row identity survives optimistic steer reconciliation.
//
// Unit-level key tests in ChatPage.dockSearchClose.test.tsx are self-fulfilling:
// they hand `virtualKeyFor` a `clientTs` resolver the TEST defines, so reverting
// ChatPage's own `stableMsgKey` leaves them green while the real virtualizer key
// changes again on reconciliation.
//
// This exercises the production path instead. ChatPage renders each row as
// `<div key={vi.key} data-display-index=...>`, where `vi.key` comes from the
// getKey ChatPage itself passes to the virtualizer. So if the key is stable
// across reconciliation, React keeps the SAME DOM node; if it changes, React
// unmounts and remounts the row — which is the actual defect (the row's measured
// height in HeightCache is orphaned and the viewport lurches). Asserting on node
// identity therefore tests the real key derivation, with no resolver supplied by
// the test.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../pages/chat', async () => {
  const React = await import('react')
  return {
    ChatFooter: () => null,
    McpInfoButton: () => null,
    // Rendered content is irrelevant; the row WRAPPER carries the key.
    UserMessage: () => React.createElement('div', { 'data-testid': 'user-msg' }),
    AssistantMessage: () => React.createElement('div', { 'data-testid': 'assistant-msg' }),
  }
})
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
// The real virtualizer needs IntersectionObserver + layout, so it mounts zero
// rows in jsdom. This stub mounts every item — but CRUCIALLY it derives each
// key with `opts.getKey`, i.e. ChatPage's REAL key function. That is what keeps
// this a test of production behaviour rather than of the stub.
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: (opts: { items?: unknown[]; getKey?: (it: unknown, i: number) => string }) => {
    // Stashed for the duplicate-key wiring test below: the REAL getKey ChatPage
    // hands the virtualizer, paired with the items of the same render.
    ;(globalThis as Record<string, unknown>).__vcOpts = opts
    const items = opts.items ?? []
    return {
      virtualItems: items.map((data, index) => ({
        key: opts.getKey ? opts.getKey(data, index) : String(index),
        index,
        mounted: true,
        data,
      farmIsMeasured: () => true,
      farmRecord: () => true,
      })),
      isAtBottom: true,
      getFollow: () => true,
      scrollToBottom: vi.fn(),
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
  ok: true, status: 200, text: () => Promise.resolve(''), json: () => Promise.resolve({}),
}) as never

import ChatPage from '../pages/ChatPage'

// An optimistic steer bubble: the client stamps its own ts and stashes it in
// meta.clientTs, because the server will later hand back a DIFFERENT ts.
const OPTIMISTIC = {
  role: 'user',
  content: 'steer me',
  cls: '',
  ts: '2026-06-23T20:00:00.000Z',
  meta: { clientTs: '2026-06-23T20:00:00.000Z', steer: true },
}
// The echo reconcile: same logical message, authoritative server ts, clientTs
// preserved. Anything keyed on `ts` alone sees a brand-new message here.
const RECONCILED = {
  ...OPTIMISTIC,
  ts: '2026-06-23T20:00:04.512Z',
  meta: { ...OPTIMISTIC.meta },
}

const renderChatPage = (initial: unknown[]) => {
  const slot = { key: 'chat-1', title: 'chat-1', messages: 1, running: false, mode: '', created: '', last_ts: '' }
  apiMocks.chatSlots = vi.fn().mockResolvedValue([slot])
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: initial, has_more: false, total: initial.length })
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
      messages: initial, slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const { container } = render(
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
  act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: initial }) })
  return { store, container }
}

const rowNode = (container: HTMLElement) =>
  container.querySelector('[data-display-index="0"]') as HTMLElement | null

describe('ChatPage — the virtualizer row survives steer reconciliation without remounting', () => {
  beforeEach(() => {
    Object.keys(apiMocks).forEach(k => delete apiMocks[k])
    delete (globalThis as Record<string, unknown>).__vcOpts
  })

  it('keeps the SAME row DOM node when the server ts replaces the optimistic one', () => {
    const { store, container } = renderChatPage([OPTIMISTIC])
    const before = rowNode(container)
    expect(before).toBeTruthy()
    // Tag the live node: a remount would create a fresh element without this.
    ;(before as HTMLElement & { __probe?: string }).__probe = 'kept'

    act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: [RECONCILED] }) })

    const after = rowNode(container)
    expect(after).toBeTruthy()
    // Identity, not equality: React reuses the node only if the key is stable.
    expect(after).toBe(before)
    expect((after as HTMLElement & { __probe?: string }).__probe).toBe('kept')
  })

  it('a genuinely different message DOES get its own row node (control)', () => {
    // Guards against the test passing for a trivial reason — e.g. if row lookup
    // always returned the same node regardless of key, the assertion above would
    // be meaningless.
    const { store, container } = renderChatPage([OPTIMISTIC])
    const before = rowNode(container)
    ;(before as HTMLElement & { __probe?: string }).__probe = 'kept'
    act(() => {
      store.dispatch({
        type: 'chat/replaceMessages',
        payload: [{ role: 'user', content: 'unrelated', cls: '', ts: '2026-06-24T09:00:00.000Z' }],
      })
    })
    const after = rowNode(container)
    expect(after).toBeTruthy()
    expect((after as HTMLElement & { __probe?: string }).__probe).toBeUndefined()
  })

  it('derives DISTINCT virtualizer keys for two rows stamped in the same tick', () => {
    // A coarse OS clock can stamp two rows appended in one tick with the same
    // `ts`; a `single` keys on `msgKey` alone, so without the list-level
    // dedupe (uniqueRowKeys) both rows reach React AND the HeightCache under
    // ONE key — a dropped sibling and a shared height slot. This asserts
    // through the REAL getKey ChatPage hands the virtualizer (captured by the
    // mock above), so it pins the WIRING, not just the helper.
    const SAME_TS = '2026-06-23T20:00:00.000Z'
    renderChatPage([
      { role: 'user', content: 'first', cls: '', ts: SAME_TS },
      { role: 'assistant', content: 'second', cls: '', ts: SAME_TS },
    ])
    const opts = (globalThis as Record<string, unknown>).__vcOpts as {
      items: unknown[]
      getKey: (it: unknown, i: number) => string
    }
    const keys = opts.items.map((it, i) => opts.getKey(it, i))
    expect(keys.length).toBeGreaterThanOrEqual(2)
    expect(new Set(keys).size).toBe(keys.length)
  })
})
