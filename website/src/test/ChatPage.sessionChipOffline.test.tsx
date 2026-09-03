/**
 * Regression test: a session chip click on an offline gateway must not blank the
 * transcript.
 *
 * `switchSlot` is rejected while disconnected, which empties `messages` and lets
 * WelcomeView replace the painted conversation — losing what the user was
 * reading. `handleSessionOpen` therefore returns before dispatching, the same
 * guard the URL-driven and sidebar switch sites already take.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

/** Props the stubbed transcript last received, for asserting what ChatPage passes. */
const lastAssistantProps: { sessions?: ReadonlyMap<string, string> } = {}
import { render, screen, fireEvent, act } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Stub AssistantMessage: expose onOpenDiff as clickable buttons.
// Button fires with original === modified (the no-op case).
vi.mock('../pages/chat', async () => {
  const React = await import('react')
  return {
    ChatFooter: () => null,
    McpInfoButton: () => null,
    UserMessage: () => null,
    AssistantMessage: (props: {
      onSessionOpen?: (key: string) => void
      sessions?: ReadonlyMap<string, string>
      content?: string
    }) => (
      lastAssistantProps.sessions = props.sessions,
      React.createElement('div', null,
        React.createElement('span', { 'data-testid': 'transcript' }, props.content ?? ''),
        React.createElement('button', {
          'data-testid': 'chip',
          onClick: () => props.onSessionOpen?.('chat-2'),
        }, 'chip'),
      )),
  }
})

vi.mock('../components/MarkdownPanel', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'md-panel' }) }
})
vi.mock('../components/DiffPanel', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'diff-panel' }) }
})

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: (opts: { items?: unknown[]; getKey?: (it: unknown, i: number) => string }) => {
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
vi.mock('../components/WelcomeView', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'welcome' }) }
})
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
  ok: true, status: 200,
  text: () => Promise.resolve('file content'),
  json: () => Promise.resolve({}),
}) as never

import ChatPage from '../pages/ChatPage'

const MSG = { role: 'assistant', content: 'painted transcript', ts: '2026-06-23T20:00:00Z' }

const SLOT_A = { key: 'chat-1', title: 'one', messages: 1, running: false, mode: '', created: '', last_ts: '' }
const SLOT_B = { key: 'chat-2', title: 'two', messages: 1, running: false, mode: '', created: '', last_ts: '' }
/** A surface ChatPage does not render, so it must never reach the chip roster. */
const SLOT_DC = { key: 'chat-99-1700000000', title: 'dc-run', messages: 1, running: false, mode: 'design-critique', created: '', last_ts: '' }
/** `crew` IS a chat surface, so it pins the filter against over-narrowing. */
const SLOT_CREW = { key: 'chat-77-1700000001', title: 'crew run', messages: 1, running: false, mode: 'crew', created: '', last_ts: '' }

const renderChatPage = (connected: boolean, extraSlots: typeof SLOT_A[] = []) => {
  const allSlots = [SLOT_A, SLOT_B, ...extraSlots]
  apiMocks.chatSlots = vi.fn().mockResolvedValue(allSlots)
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: [MSG], has_more: false, total: 1 })
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected,
      slots: allSlots, approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      activeSlot: 'chat-1',
      messages: [MSG], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
    } as never,
  })
  // Wrapped BEFORE render: `useDispatch` captures `store.dispatch` by reference
  // during render, so a wrapper installed afterwards is simply bypassed.
  const seen: string[] = []
  const real = store.dispatch
  store.dispatch = ((action: unknown) => {
    // A thunk dispatches its own `pending` through the middleware's internal
    // dispatch, so the thunk itself is the observable the guard actually gates.
    seen.push(typeof action === 'function' ? 'thunk' : ((action as { type?: string } | null)?.type ?? 'unknown'))
    return (real as (a: unknown) => unknown)(action)
  }) as typeof store.dispatch
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
  return { store, seen }
}

const seedMessage = (store: ReturnType<typeof createTestStore>) => {
  act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: [MSG] }) })
}

describe('session chip click and gateway connectivity', () => {
  beforeEach(() => {
    for (const k of Object.keys(apiMocks)) delete apiMocks[k]
  })

  it('does not dispatch a slot switch while disconnected', async () => {
    const { store, seen } = renderChatPage(false)
    seedMessage(store)
    const chip = await screen.findByTestId('chip')
    const before = seen.length

    await act(async () => { fireEvent.click(chip) })

    expect(seen.slice(before)).toEqual([])
    // The transcript the user was reading is still on screen, and WelcomeView --
    // which is what an emptied `messages` would surface -- never took over.
    expect(screen.getByTestId('transcript').textContent).toBe('painted transcript')
    expect(screen.queryByTestId('welcome')).toBeNull()
    expect(store.getState().chat.messages.length).toBeGreaterThan(0)
  })

  it('does dispatch the switch when connected, so the guard is not inert', async () => {
    // Positive control for the assertion above: without it, a handler that never
    // switched under any condition would pass the disconnected case vacuously.
    const { store, seen } = renderChatPage(true)
    seedMessage(store)
    const chip = await screen.findByTestId('chip')
    const before = seen.length

    await act(async () => { fireEvent.click(chip) })

    expect(seen.slice(before)).toContain('thunk')
  })
})

describe('session chip affordance while disconnected', () => {
  beforeEach(() => {
    for (const k of Object.keys(apiMocks)) delete apiMocks[k]
    delete lastAssistantProps.sessions
  })

  it('withholds the roster so the chip degrades to the copy chip', async () => {
    // No roster means the span renders as the click-to-copy chip, which acknowledges
    // the click, instead of a chip whose tooltip promises a switch that cannot happen.
    const { store } = renderChatPage(false)
    seedMessage(store)
    await screen.findByTestId('chip')

    expect(lastAssistantProps.sessions).toBeUndefined()
  })

  it('hands the roster over once connected, so the gate is not simply always-off', async () => {
    // Positive control: without this, passing `undefined` unconditionally would pass
    // the assertion above while removing the feature.
    const { store } = renderChatPage(true)
    seedMessage(store)
    await screen.findByTestId('chip')

    expect(lastAssistantProps.sessions).toBeInstanceOf(Map)
    expect(lastAssistantProps.sessions?.size).toBeGreaterThan(0)
  })
})

describe('chip roster carries only surfaces ChatPage can show', () => {
  beforeEach(() => {
    for (const k of Object.keys(apiMocks)) delete apiMocks[k]
    delete lastAssistantProps.sessions
  })

  it('omits a non-chat session, which would switch and then be cleared', async () => {
    // `design-critique` is not a chat surface, so chipping its key promises a
    // destination the page cannot render and drops the reader back out.
    const { store } = renderChatPage(true, [SLOT_DC])
    seedMessage(store)
    await screen.findByTestId('chip')

    expect(lastAssistantProps.sessions?.has('chat-1')).toBe(true)
    expect(lastAssistantProps.sessions?.has(SLOT_DC.key)).toBe(false)
  })

  it('keeps a crew session, so the filter is not simply narrowed to the default', async () => {
    // Positive control: excluding every non-empty surface would pass the test
    // above while silently dropping two surfaces the chat view does show.
    const { store } = renderChatPage(true, [SLOT_CREW])
    seedMessage(store)
    await screen.findByTestId('chip')

    expect(lastAssistantProps.sessions?.has(SLOT_CREW.key)).toBe(true)
  })
})

describe('one session-entry path', () => {
  // A SOURCE guard: the two callees were behaviourally identical, so only the
  // structure can distinguish the collapsed form from the duplicated one.
  const source = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf-8')

  it('routes the in-message chip through the shared switch callee', () => {
    expect(source).toContain('onSessionOpen={selectSessionTab}')
  })

  it('keeps no second spelling of the session-open guard', () => {
    // The duplicate would diverge the first time either side gained a side effect.
    expect(source).not.toContain('handleSessionOpen')
  })

  it('declares the shared callee exactly once', () => {
    expect(source.match(/const selectSessionTab = useCallback/g)).toHaveLength(1)
  })
})
