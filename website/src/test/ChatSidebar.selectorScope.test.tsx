/**
 * Verifies the sidebar's subagent selectors re-render only when counts change,
 * not on every streaming token. Uses a hoisted render counter on the component.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sseSubagentBatchChunks, sseSubagentSpawn } from '../store/chatSlice'

// Counts renders of ChatSidebar.
const { sidebarRenders } = vi.hoisted(() => ({ sidebarRenders: { n: 0 } }))

// Mock framer-motion to avoid jsdom projection issues.
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: () => vi.fn().mockResolvedValue([]),
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

// The actual sidebar, but with a render counter injected via a hook mock.
// We mock useSimplifiedToolNames since it's called unconditionally at render.
vi.mock('../hooks/useSimplifiedToolNames', () => ({
  useSimplifiedToolNames: () => {
    sidebarRenders.n++
    return false
  },
}))

import ChatSidebar from '../pages/ChatSidebar'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

function mountSidebar(slots: ChatSlot[], chat: Record<string, unknown>, activeSlotProp: string | null = null) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: chat.activeSlot ?? null,
      slotStatusDetail: {},
      subagents: {},
      slotActivity: {},
      subagentQueued: {},
      goalLoops: {},
      messages: [],
      slotMessages: {},
      ...chat,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const result = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={activeSlotProp} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, ...result }
}

/** Runs `body`, lets React flush, and returns how many times the sidebar rendered. */
async function rendersDuring(body: () => void) {
  const before = sidebarRenders.n
  await act(async () => {
    body()
    await new Promise(r => setTimeout(r, 50))
  })
  return sidebarRenders.n - before
}

describe('ChatSidebar store subscription scope', () => {
  beforeEach(() => { sidebarRenders.n = 0; localStorage.clear() })
  afterEach(() => { cleanup(); vi.clearAllMocks() })

  it('does not re-render on a subagent chunk for a DIFFERENT (background) slot', async () => {
    const slots = [
      { key: 'slot-a', title: 'A', running: false, messages: 1 },
      { key: 'slot-b', title: 'B', running: false, messages: 1 },
    ] as unknown as ChatSlot[]
    const { store } = mountSidebar(
      slots,
      {
        activeSlot: 'slot-a',
        slotActivity: {
          'slot-b': { toolLog: [], subagents: { 'sub-1': { id: 'sub-1', status: 'running', streaming: '' } } },
        },
      },
      'slot-a',
    )

    await act(async () => { await new Promise(r => setTimeout(r, 100)) })
    sidebarRenders.n = 0

    expect(await rendersDuring(() => {})).toBe(0) // Control: no dispatch → no render

    // Cross-slot: chunk for slot-b should not re-render when slot-a is active.
    const delta = await rendersDuring(() => {
      store.dispatch(sseSubagentBatchChunks({ chunks: [{ slot: 'slot-b', id: 'sub-1', text: 'token' }] }))
    })

    expect(delta).toBe(0)
  })

  it('does not re-render on a subagent chunk for the ACTIVE slot when count unchanged', async () => {
    const slots = [{ key: 'slot-a', title: 'A', running: false, messages: 1 }] as unknown as ChatSlot[]
    const { store } = mountSidebar(
      slots,
      {
        activeSlot: 'slot-a',
        subagents: { 'sub-1': { id: 'sub-1', status: 'running', streaming: '' } },
      },
      'slot-a',
    )

    await act(async () => { await new Promise(r => setTimeout(r, 100)) })
    sidebarRenders.n = 0

    // Same-slot streaming: count unchanged so derived selector should not re-render.
    const delta = await rendersDuring(() => {
      store.dispatch(sseSubagentBatchChunks({ chunks: [{ slot: 'slot-a', id: 'sub-1', text: 'token' }] }))
    })

    expect(delta).toBe(0)
  })

  it('DOES re-render when a new subagent spawns (count changes)', async () => {
    const slots = [{ key: 'slot-a', title: 'A', running: false, messages: 1 }] as unknown as ChatSlot[]
    const { store } = mountSidebar(
      slots,
      {
        activeSlot: 'slot-a',
        subagents: { 'sub-1': { id: 'sub-1', status: 'running', streaming: '' } },
      },
      'slot-a',
    )

    await act(async () => { await new Promise(r => setTimeout(r, 100)) })
    sidebarRenders.n = 0

    const delta = await rendersDuring(() => {
      store.dispatch(sseSubagentSpawn({ slot: 'slot-a', id: 'sub-2', name: 'agent2' }))
    })

    expect(delta).toBeGreaterThan(0) // Count changed: re-render expected
  })
})
