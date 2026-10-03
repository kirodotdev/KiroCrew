/**
 * #11820: the session row's hover action forks the whole chat. It used to be
 * labelled "Duplicate" with the clipboard `Copy` glyph, which read as "copy
 * text". It is now named "Fork chat" with the same `GitFork` glyph the
 * message-level "Fork conversation from here" uses, and still calls the
 * existing whole-session fork (api.forkChatSlot) -- no new behaviour.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

const mocks = vi.hoisted(() => ({ forkChatSlot: vi.fn() }))

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({}, {
    get: (_t, name: string) => name === 'forkChatSlot' ? mocks.forkChatSlot : vi.fn().mockResolvedValue([]),
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

const SLOT_KEY = 'chat-fork-1'

function renderSidebar() {
  const slots = [{ key: SLOT_KEY, title: 'Fork me', running: false, created: '', last_ts: '' }]
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { activeSlot: null } as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
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

afterEach(() => vi.clearAllMocks())

describe('session row "Fork chat" action', () => {
  it('is named Fork chat, draws the fork glyph, and forks the whole session', async () => {
    mocks.forkChatSlot.mockResolvedValue({ ok: true, key: 'chat-forked' })
    renderSidebar()
    const row = document.querySelector(`[data-session-row="${SLOT_KEY}"]`) as HTMLElement
    expect(row).toBeTruthy()
    const btn = Array.from(row.querySelectorAll('button')).find(b => b.getAttribute('aria-label') === 'Fork chat')
    expect(btn, 'row carries a "Fork chat" button').toBeTruthy()
    expect(btn!.getAttribute('title')).toBe('Fork chat')
    // The old clipboard-looking label and glyph are gone from the row.
    expect(screen.queryByRole('button', { name: 'Duplicate' })).toBeNull()
    expect(btn!.querySelector('svg.lucide-git-fork')).toBeTruthy()
    expect(btn!.querySelector('svg.lucide-copy')).toBeNull()
    fireEvent.click(btn!)
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledWith(SLOT_KEY))
  })
})
