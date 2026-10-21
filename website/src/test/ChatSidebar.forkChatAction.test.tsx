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

function renderSidebar(onSelectSlot?: (key: string) => void) {
  const slots = [{ key: SLOT_KEY, title: 'Fork me', running: false, created: '', last_ts: '' }]
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              onSelectSlot={onSelectSlot}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store }
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

  // #21935 sibling: the fork becomes the active session, so the host must hear
  // about it the way it hears about a row click, or Split View keeps drawing the
  // old panes over it.
  it('reports the fork through onSelectSlot once it opens', async () => {
    mocks.forkChatSlot.mockResolvedValue({ ok: true, key: 'chat-forked' })
    const onSelectSlot = vi.fn()
    renderSidebar(onSelectSlot)
    const row = document.querySelector(`[data-session-row="${SLOT_KEY}"]`) as HTMLElement
    const btn = Array.from(row.querySelectorAll('button')).find(b => b.getAttribute('aria-label') === 'Fork chat')!
    fireEvent.click(btn)
    await waitFor(() => expect(onSelectSlot).toHaveBeenCalledWith('chat-forked'))
  })

  // The chat page clears an active session its slot list does not carry, then
  // auto-opens the remembered chat. The fork must therefore be listed by the
  // time it becomes active, or the user lands in an unrelated chat.
  it('lists the fork before it becomes the active session', async () => {
    mocks.forkChatSlot.mockResolvedValue({ ok: true, key: 'chat-forked', title: 'Fork of Fork me', messages: 3 })
    const { store } = renderSidebar()
    // Recorded on the first state in which the fork is active.
    let listedWhenActive: boolean | null = null
    const unsubscribe = store.subscribe(() => {
      const s = store.getState()
      if (listedWhenActive === null && s.chat.activeSlot === 'chat-forked') {
        listedWhenActive = s.dashboard.slots.some(r => r.key === 'chat-forked')
      }
    })
    const row = document.querySelector(`[data-session-row="${SLOT_KEY}"]`) as HTMLElement
    fireEvent.click(Array.from(row.querySelectorAll('button')).find(b => b.getAttribute('aria-label') === 'Fork chat')!)
    await waitFor(() => expect(listedWhenActive).not.toBeNull())
    unsubscribe()
    expect(listedWhenActive).toBe(true)
    expect(store.getState().dashboard.slots.find(r => r.key === 'chat-forked')?.title).toBe('Fork of Fork me')
  })

  it('reports nothing when the fork is refused', async () => {
    mocks.forkChatSlot.mockResolvedValue({ ok: false })
    const onSelectSlot = vi.fn()
    renderSidebar(onSelectSlot)
    const row = document.querySelector(`[data-session-row="${SLOT_KEY}"]`) as HTMLElement
    fireEvent.click(Array.from(row.querySelectorAll('button')).find(b => b.getAttribute('aria-label') === 'Fork chat')!)
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledTimes(1))
    expect(onSelectSlot).not.toHaveBeenCalled()
  })
})
