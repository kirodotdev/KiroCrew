/**
 * Regression test: paging older history shows a visible loading indicator.
 *
 * `loadingOlder` was already tracked in the store and already read by ChatPage,
 * but only as a re-entrancy guard — nothing rendered it. A fetch in flight was
 * therefore indistinguishable from nothing happening, so a stalled paging
 * trigger looked exactly like a session that simply had no more history.
 *
 * The three cases below are the whole contract: absent when idle (so the
 * assertion is not passing on a permanently-mounted node), present while the
 * fetch is pending, and gone again when it settles — asserted on the rejected
 * path, which is the one a user hits when the request fails and the spinner
 * would otherwise be left spinning forever.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { i18nT } from '../i18n/t'

vi.mock('../pages/chat', () => ({
  ChatFooter: () => null,
  McpInfoButton: () => null,
  UserMessage: () => null,
  AssistantMessage: () => null,
}))

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
  ok: true, status: 200,
  text: () => Promise.resolve(''),
  json: () => Promise.resolve({}),
}) as never

import ChatPage from '../pages/ChatPage'

const MSG = { role: 'assistant', content: 'newest', ts: '2026-06-23T20:00:00Z' }
const INDICATOR = 'older-messages-loading'
const BAR = 'load-earlier-messages'

const renderChatPage = () => {
  const slot = { key: 'chat-1', title: 'chat-1', messages: 1, running: false, mode: '', created: '', last_ts: '' }
  apiMocks.chatSlots = vi.fn().mockResolvedValue([slot])
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: [MSG], has_more: true, total: 50 })
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
      messages: [MSG], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: true, slotOldestIndex: 49, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      // switchSlot.pending caches the outgoing transcript here; the real initial state
      // has it, so a fixture without it models a store that never exists.
      slotMessages: {},
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

// Driven at the reducer boundary: the thunk dispatches `pending` before its
// creator reads `loadingOlder` as a guard, so it returns null and never fetches.
const pending = { type: 'chat/loadOlder/pending', meta: { arg: 'chat-1', requestId: 'r1', requestStatus: 'pending' } }
// The real producer of a has-more cursor: its reducer runs setPagingCursor, which
// writes has-more, the offset and the cursor key as one unit.
const olderPageLanded = { type: 'chat/loadOlder/fulfilled', payload: { slot: 'chat-1', nextBefore: 20, messages: [], hasMore: true, total: 50 }, meta: { requestId: 'r2', requestStatus: 'fulfilled' } }
// The last page: has-more false, which is what removes the bar's mount condition.
const finalPageLanded = { type: 'chat/loadOlder/fulfilled', payload: { slot: 'chat-1', nextBefore: 0, messages: [], hasMore: false, total: 50 }, meta: { requestId: 'r3', requestStatus: 'fulfilled' } }
const sameKeySwitchPending = { type: 'chat/switchSlot/pending', meta: { arg: 'chat-1', requestId: 's1', requestStatus: 'pending' } }
const rejected = { type: 'chat/loadOlder/rejected', meta: { arg: 'chat-1', requestId: 'r1', requestStatus: 'rejected' }, error: { message: 'offline' } }

// The initial fetch replaces the store's messages after mount, and an empty
// list renders the welcome hero instead of the scroller. Re-seed once mounted.
const seed = (store: ReturnType<typeof createTestStore>) =>
  screen.findByLabelText(i18nT('pages.chatPage.session_options')).then(() => {
    act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: [MSG] }) })
    return screen.findByLabelText(i18nT('pages.chatPage.chat_messages'))
  })

describe('ChatPage – older-messages loading indicator', () => {
  beforeEach(() => {
    Object.keys(apiMocks).forEach(k => delete apiMocks[k])
  })

  it('renders nothing while no older page is in flight', async () => {
    const store = renderChatPage()
    await seed(store)
    expect(screen.queryByTestId(INDICATOR)).toBeNull()
  })

  it('shows a labelled status region while an older page is in flight', async () => {
    const store = renderChatPage()
    await seed(store)

    act(() => { store.dispatch(pending) })

    const el = await screen.findByTestId(INDICATOR)
    // A bare spinner is an unnamed live region: screen readers announce the
    // region with no content, since the icon contributes no text.
    expect(el.getAttribute('role')).toBe('status')
    expect(el.getAttribute('aria-label')).toBe(i18nT('pages.chatPage.loading_earlier_messages'))
    // The label must say WHAT is loading: a bare "Loading…" in a live region
    // tells a screen-reader user nothing about which region moved.
    expect(el.getAttribute('aria-label')).not.toBe(i18nT('pages.chatPage.loading'))
    // Not the browser's scroll anchor: it must not shift the list as it mounts.
    expect(el.style.overflowAnchor).toBe('none')
    // Pinned, not parked at the list top: the only trigger fires from the pins
    // panel, so an unpinned indicator renders off-screen in a long session.
    // ABSOLUTE overlay with zero layout footprint: a sticky element still
    // owned flow space, so each loadingOlder flip inserted/removed ~32px
    // above the content -- a per-landing twitch for a reader parked at the
    // top (measured on the momentum rig).
    expect(el.className).toContain('absolute')
    expect(el.className).not.toContain('sticky')
    expect(el.className).toContain('top-16')
    // The container is a transparent zero-footprint overlay; opacity lives
    // on the BADGE child, or the messages scrolling beneath show through.
    const badge = el.querySelector('span') as HTMLElement
    expect(badge).not.toBeNull()
    expect(badge.style.background).not.toBe('')
  })

  it('clears when the older page fails, so it cannot spin forever', async () => {
    const store = renderChatPage()
    await seed(store)

    act(() => { store.dispatch(pending) })
    await screen.findByTestId(INDICATOR)

    act(() => { store.dispatch(rejected) })
    await waitFor(() => {
      expect(screen.queryByTestId(INDICATOR)).toBeNull()
    })
  })

  // Control for the case below: with has-more reported and the cursor keyed, it mounts.
  it('mounts the earlier-messages bar once a page lands reporting more history', async () => {
    const store = renderChatPage()
    await seed(store)

    act(() => { store.dispatch(olderPageLanded) })

    await screen.findByTestId(BAR)
    expect(store.getState().chat.slotCursorKey).toBe('chat-1')
  })

  it('unmounts the bar mid-switch, when the cursor still describes the outgoing chat', async () => {
    const store = renderChatPage()
    await seed(store)
    act(() => { store.dispatch(olderPageLanded) })
    await screen.findByTestId(BAR)

    // A SAME-key switch: nulls the cursor without touching activeSlot or messages,
    // so the cursor key is the only variable that moves.
    act(() => { store.dispatch(sameKeySwitchPending) })

    await waitFor(() => {
      expect(screen.queryByTestId(BAR)).toBeNull()
    })
    // The suppression must come from the cursor, not from has-more flipping:
    // without these the test would pass for the wrong reason.
    expect(store.getState().chat.slotHasMore).toBe(true)
    expect(store.getState().chat.slotCursorKey).toBeNull()
    expect(store.getState().chat.activeSlot).toBe('chat-1')
  })

  it('hands focus to the transcript when the final page unmounts the bar', async () => {
    const store = renderChatPage()
    const scroller = await seed(store)
    act(() => { store.dispatch(olderPageLanded) })
    const bar = await screen.findByTestId(BAR)
    bar.focus()
    expect(document.activeElement).toBe(bar)

    act(() => { store.dispatch(finalPageLanded) })

    await waitFor(() => {
      expect(screen.queryByTestId(BAR)).toBeNull()
    })
    // Without the hand-off this is <body> -- the stranding the bar's own
    // aria-disabled choice exists to avoid.
    expect(document.activeElement).toBe(scroller)
    // Asserted directly because jsdom's focus() ignores focusability: a real browser
    // needs the attribute, and the line above passes with or without it.
    expect(scroller).toHaveAttribute('tabindex', '-1')
    expect(store.getState().chat.slotHasMore).toBe(false)
  })
})
