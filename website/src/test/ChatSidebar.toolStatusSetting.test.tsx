/**
 * Chat sidebar row status honors the `simplifiedToolNames` preference:
 * the running-session subtitle is the same tool label the inline pill shows
 * in the transcript — the agent's purpose when simplified names are on, the
 * raw tool title when they're off, so the two surfaces agree.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: any) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))

// Mutable chat config: the sidebar reads `simplifiedToolNames` through
// useSimplifiedToolNames -> loadChatConfig. tagColumnsEnabled stays off so the
// rows render as one flat lane and stay easy to query.
const cfg = vi.hoisted(() => ({
  value: { tagColumnsEnabled: false, confirmCloseSession: false, simplifiedToolNames: true } as Record<string, unknown>,
}))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => cfg.value,
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

import ChatSidebar from '../pages/ChatSidebar'

const TOOL_DETAIL = {
  kind: 'tool',
  text: 'Looking up the mic permission handler',
  toolName: 'grep --include=*.ts setPermissionCheckHandler',
  ts: 1,
}

function renderSidebar(slots: any[], chat: Record<string, unknown>) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, goalLoops: {}, ...chat } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, simplifiedToolNames: true }
})
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — tool status honors simplifiedToolNames', () => {
  const slots = [{ key: 'k', title: 'sess', running: true, messages: 5, last_message: 'stale' }]

  it('shows the agent purpose when the setting is on', () => {
    const { getByText, queryByText } = renderSidebar(slots, { slotStatusDetail: { k: TOOL_DETAIL } })
    expect(getByText(/Looking up the mic permission handler/)).toBeTruthy()
    expect(queryByText(/setPermissionCheckHandler/)).toBeNull()
  })

  it('shows the raw tool title when the setting is off', () => {
    cfg.value = { ...cfg.value, simplifiedToolNames: false }
    const { getByText, queryByText } = renderSidebar(slots, { slotStatusDetail: { k: TOOL_DETAIL } })
    expect(getByText(/setPermissionCheckHandler/)).toBeTruthy()
    expect(queryByText(/Looking up the mic permission handler/)).toBeNull()
  })

  it('still localizes the fixed thinking phase with the setting off', () => {
    // Only the `tool` phase carries two forms; thinking/streaming must keep
    // going through the catalog rather than falling into the tool branch.
    cfg.value = { ...cfg.value, simplifiedToolNames: false }
    const { getByText } = renderSidebar(slots, {
      slotStatusDetail: { k: { kind: 'thinking', text: 'Thinking…', ts: 1 } },
    })
    expect(getByText(/Thinking…/)).toBeTruthy()
  })

  it('composes the goal-loop detail from the same setting-aware label', () => {
    cfg.value = { ...cfg.value, simplifiedToolNames: false }
    const { getByText } = renderSidebar(slots, {
      goalLoops: { k: { cycle_count: 3, max_cycles: 24 } },
      slotStatusDetail: { k: TOOL_DETAIL },
    })
    expect(getByText('Loop 3/24')).toBeTruthy()
    expect(getByText(/setPermissionCheckHandler/)).toBeTruthy()
  })
})
