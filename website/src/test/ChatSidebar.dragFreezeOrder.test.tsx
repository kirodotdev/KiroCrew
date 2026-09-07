/**
 * Sidebar row order holds for the duration of a dnd-kit drag.
 *
 * dnd-kit measures the active node's rect once at drag start and never
 * recomputes it, so a row that reorders mid-gesture leaves the collision math
 * offset and the drop resolves to the wrong row or to nothing. The default sort
 * is date-desc, so a background session going active reproduces it.
 *
 * The pointer-drag lifecycle can't be faithfully simulated in jsdom (it needs
 * real PointerEvents plus layout measurement), so this stubs the drag context,
 * invokes the real lifecycle handlers, and drives a reorder by re-rendering
 * with a bumped timestamp. That exercises the hold rather than the gesture.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Captured lifecycle props from the sidebar's DndContext. Stubbing the context
// (children pass through) is what lets the real handlers run without a gesture.
const dnd = vi.hoisted(() => ({
  handlers: {} as Record<string, ((e: unknown) => void) | undefined>,
  overId: null as string | null,
}))

vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: { children?: unknown; onDragStart?: (e: unknown) => void; onDragEnd?: (e: unknown) => void }) => {
      dnd.handlers.onDragStart = props.onDragStart
      dnd.handlers.onDragEnd = props.onDragEnd
      return props.children as never
    },
    useDroppable: (args: Parameters<typeof actual.useDroppable>[0]) => ({
      ...actual.useDroppable(args),
      isOver: dnd.overId === String(args.id),
    }),
  }
})

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
          'renameSlot', 'forkSession',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatFolders: vi.fn().mockResolvedValue([]),
      sessionsSearch: vi.fn().mockResolvedValue({ sessions: [] }),
    },
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar from '../pages/ChatSidebar'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const TITLE_A = 'Alpha session'
const TITLE_B = 'Bravo session'
const TITLE_C = 'Charlie session'

const slot = (key: string, title: string, lastTs: string, pinned = false): ChatSlot => ({
  key, title, messages: 1, running: false, mode: '', created: lastTs, last_ts: lastTs, pinned,
} as ChatSlot)

// date-desc: A (newest), B, C (oldest).
const SLOTS_INITIAL = [
  slot('chat-a', TITLE_A, '2026-03-01T00:00:00Z'),
  slot('chat-b', TITLE_B, '2026-02-01T00:00:00Z'),
  slot('chat-c', TITLE_C, '2026-01-01T00:00:00Z'),
]

// C goes active mid-drag and becomes the newest → date-desc order is C, A, B.
const SLOTS_REORDERED = [
  SLOTS_INITIAL[0],
  SLOTS_INITIAL[1],
  slot('chat-c', TITLE_C, '2026-04-01T00:00:00Z'),
]

function renderSidebar(slots: ChatSlot[]) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: true,
      slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 'chat-a',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined,
      history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (s: ChatSlot[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={s}
              activeSlot={'chat-a'}
              unreadSlots={[]}
              history={[]}
              historyHasMore={false}
              defaultAgent={'default'}
              installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const { rerender } = render(tree(slots))
  return { rerender: (s: ChatSlot[]) => rerender(tree(s)) }
}

/** Rendered order of the three fixture rows, top to bottom. */
function renderedOrder(): string[] {
  const nodes = [TITLE_A, TITLE_B, TITLE_C].map(t => ({ t, el: screen.getByText(t) }))
  return nodes
    .sort((x, y) => (x.el.compareDocumentPosition(y.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))
    .map(n => n.t)
}

describe('ChatSidebar – sidebar row order is held during a dnd-kit drag', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    dnd.handlers = {}
    dnd.overId = null
  })

  it('holds order while a drag is live and re-derives once it ends', () => {
    const { rerender } = renderSidebar(SLOTS_INITIAL)

    // Guard: a missed capture would make the drag steps silent no-ops.
    expect(typeof dnd.handlers.onDragStart).toBe('function')
    expect(typeof dnd.handlers.onDragEnd).toBe('function')

    expect(renderedOrder()).toEqual([TITLE_A, TITLE_B, TITLE_C])

    act(() => {
      dnd.handlers.onDragStart!({ active: { id: 'chat-a', data: { current: { type: 'session', key: 'chat-a' } } } })
    })

    // C becomes the newest mid-drag. Frozen, so the order must not move — this
    // is the assertion that fails without the hold.
    rerender(SLOTS_REORDERED)
    expect(renderedOrder()).toEqual([TITLE_A, TITLE_B, TITLE_C])

    act(() => {
      dnd.handlers.onDragEnd!({ active: { id: 'chat-a', data: { current: { type: 'session', key: 'chat-a' } } }, over: null })
    })

    expect(renderedOrder()).toEqual([TITLE_C, TITLE_A, TITLE_B])
  })

  it('the fixture actually reorders under date-desc (guards fixture validity)', () => {
    const order = (slots: ChatSlot[]) => [...slots]
      .sort((a, b) => new Date(b.last_ts!).getTime() - new Date(a.last_ts!).getTime())
      .map(s => s.key)
    expect(order(SLOTS_INITIAL)).toEqual(['chat-a', 'chat-b', 'chat-c'])
    expect(order(SLOTS_REORDERED)).toEqual(['chat-c', 'chat-a', 'chat-b'])
  })

  it('reorders only pinned rows and keeps that order under Created: newest', () => {
    localStorage.setItem('mc-session-sort', 'created-desc')
    const pinnedSlots = [
      slot('chat-a', TITLE_A, '2026-03-01T00:00:00Z', true),
      slot('chat-b', TITLE_B, '2026-02-01T00:00:00Z', true),
      slot('chat-c', TITLE_C, '2026-04-01T00:00:00Z'),
    ]
    renderSidebar(pinnedSlots)

    expect(renderedOrder()).toEqual([TITLE_A, TITLE_B, TITLE_C])
    expect(screen.getAllByTestId('pinned-session-divider')).toHaveLength(1)

    act(() => {
      dnd.handlers.onDragStart!({
        active: { id: 'session:chat-a', data: { current: { type: 'session', key: 'chat-a', pinned: true, container: 'root' } } },
      })
      dnd.handlers.onDragEnd!({
        active: { id: 'session:chat-a', data: { current: { type: 'session', key: 'chat-a', pinned: true, container: 'root' } } },
        over: { id: 'pinned-session:list:chat-b', data: { current: { type: 'pinned-session', key: 'chat-b', container: 'root' } } },
      })
    })

    expect(renderedOrder()).toEqual([TITLE_B, TITLE_A, TITLE_C])
    expect(JSON.parse(localStorage.getItem('mc-pinned-session-order')!)).toEqual(['chat-b', 'chat-a'])
    expect(screen.getAllByTestId('pinned-session-divider')).toHaveLength(1)
  })

  it('shows the insertion line while a pinned row is over another pinned row', () => {
    renderSidebar([
      slot('chat-a', TITLE_A, '2026-03-01T00:00:00Z', true),
      slot('chat-b', TITLE_B, '2026-02-01T00:00:00Z', true),
      slot('chat-c', TITLE_C, '2026-01-01T00:00:00Z'),
    ])
    dnd.overId = 'pinned-session:list:chat-b'
    act(() => {
      dnd.handlers.onDragStart!({
        active: { id: 'session:chat-a', data: { current: { type: 'session', key: 'chat-a', pinned: true, container: 'root' } } },
      })
    })
    expect(screen.getByTestId('pinned-session-insertion')).toBeTruthy()
  })

  it('moves a focused pinned row with Alt+ArrowUp and leaves unpinned rows automatic', () => {
    renderSidebar([
      slot('chat-a', TITLE_A, '2026-03-01T00:00:00Z', true),
      slot('chat-b', TITLE_B, '2026-02-01T00:00:00Z', true),
      slot('chat-c', TITLE_C, '2026-04-01T00:00:00Z'),
    ])
    const row = screen.getByText(TITLE_B).closest('[data-session-row]') as HTMLElement
    expect(row.getAttribute('aria-keyshortcuts')).toBe('Alt+ArrowUp Alt+ArrowDown')
    fireEvent.keyDown(row, { key: 'ArrowUp', altKey: true })
    expect(renderedOrder()).toEqual([TITLE_B, TITLE_A, TITLE_C])
    expect(JSON.parse(localStorage.getItem('mc-pinned-session-order')!)).toEqual(['chat-b', 'chat-a'])
  })

})
