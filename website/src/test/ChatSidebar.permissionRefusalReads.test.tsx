/**
 * A caller who is not given the folder or board-column list (a teammate through
 * the Kiro relay, any non-owner refused with 403 `owner_only`) must not see a red
 * "couldn't load" banner with a Retry for it: that refusal is the answer, not a
 * failed read (#32031). A real failure on the same reads still shows its banner.
 *
 * Real sidebar; the api mock is the only stand-in.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

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
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const byTag = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!byTag.has(tag)) byTag.set(tag, make(tag))
      return byTag.get(tag)
    },
  })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  chatFolders: vi.fn(),
  tagColumns: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop in mocks) return mocks[prop as keyof typeof mocks]
      return vi.fn().mockResolvedValue([])
    },
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
import { ApiError } from '../api/apiError'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const SLOTS = [{ key: 'k-1', title: 'one', running: false, messages: 1 }] as unknown as ChatSlot[]

const refusal = (code: string, authRequired = false) =>
  new ApiError(403, 'owner authorization required', JSON.stringify({ error: 'owner authorization required', code }), authRequired)

function renderSidebar() {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { ...defaults.chat, activeSlot: null, slotStatusDetail: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: {
    queries: { retry: false, staleTime: Infinity }, mutations: { retry: false },
  } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return qc
}

afterEach(() => { vi.clearAllMocks() })

describe('sidebar folder and column reads refused to a non-owner', () => {
  it.each(['owner_only', 'relay_apps_teammate'])('shows no banner for a %s refusal', async code => {
    mocks.chatFolders.mockRejectedValue(refusal(code))
    mocks.tagColumns.mockRejectedValue(refusal(code))
    const qc = renderSidebar()
    // Both rejections have settled into the query state, so absence is the answer.
    await waitFor(() => expect(qc.getQueryState(['chat-folders'])?.status).toBe('error'))
    await waitFor(() => expect(qc.getQueryState(['tag-columns'])?.status).toBe('error'))
    expect(screen.queryByTestId('chat-folders-error')).toBeNull()
    expect(screen.queryByTestId('tag-columns-error')).toBeNull()
  })

  it('still shows both banners for a real failure', async () => {
    mocks.chatFolders.mockRejectedValue(new ApiError(500, 'boom', '{"error":"boom"}'))
    mocks.tagColumns.mockRejectedValue(new TypeError('Failed to fetch'))
    renderSidebar()
    expect(await screen.findByTestId('chat-folders-error')).toBeTruthy()
    expect(await screen.findByTestId('tag-columns-error')).toBeTruthy()
  })

  it('still shows the banner for a 403 with another code, and for a session-expiry 403', async () => {
    mocks.chatFolders.mockRejectedValue(refusal('feature_disabled'))
    mocks.tagColumns.mockRejectedValue(refusal('owner_only', true))
    renderSidebar()
    expect(await screen.findByTestId('chat-folders-error')).toBeTruthy()
    expect(await screen.findByTestId('tag-columns-error')).toBeTruthy()
  })
})
