/**
 * The session chat's history search, end to end on the real ChatPage.
 *
 * On desktop the search is a Search tab of the chat's side panel
 * (`useChatHistorySearchTab`); on a phone it is that panel's full-width overlay. Opening a
 * file or a diff from the transcript ends the search and shows the requested
 * tab: the open handlers (`handleFileOpen`, `handleOpenDiff`) call
 * `search.close()` directly (the `useMessageSearch` hook is hoisted above
 * them). These tests drive the real handlers (via a stub AssistantMessage that
 * calls the passed `onOpenDiff` / `onFileOpen` props).
 *
 * Uses the REAL useMessageSearch hook so the open, close and Escape wiring is
 * exercised end to end.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, renderHook, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// The phone case flips this; every other test is a desktop window.
const viewport = vi.hoisted(() => ({ mobile: false }))
vi.mock('../hooks/useIsMobile', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../hooks/useIsMobile')>()),
  useIsMobile: () => viewport.mobile,
}))

// jsdom lays nothing out, so the side panel's on-screen check would read the
// open panel as lost and close it; the close rules below need it left alone.
vi.mock('../hooks/useSidePanelHealth', () => ({ useSidePanelHealth: () => {} }))

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
// NOTE: useMessageSearch is intentionally NOT
// mocked — the test exercises the real hook. DetailPanel + the history search
// are also real so the find pane and diff pane actually mount/unmount.
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
      farmIsMeasured: () => true,
      farmRecord: () => true,
      })),
      isAtBottom: true,
      getFollow: () => true,
      scrollToBottom: vi.fn(),
      mountIndex: vi.fn(),
      retainRange: vi.fn(),
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
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
            // The side panel opens files only once the chat is confirmed local.
            : prop === 'chatSlotEnvironment' ? { profile: null, profiles: [] }
            : prop === 'pendingQuestions' || prop === 'approvals' ? [] : {},
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
// The file-read stub. Re-installed in `beforeEach` below as well: the MSW
// server in integration/setup.ts patches `globalThis.fetch` in `beforeAll`,
// which runs AFTER this module-level assignment, so without the re-install the
// read lands on the mock server's 501 catch-all — and a failed read no longer
// opens a tab (it reports through the pane notice), so the panel never shows.
const fileReadStub = () => vi.fn().mockResolvedValue({
  ok: true, status: 200,
  // A real Response always carries headers, and the read path asks this one
  // whether the file is binary. Using the platform's own Headers keeps the mock
  // Response-shaped rather than growing a bespoke getter per field read.
  headers: new Headers(),
  text: () => Promise.resolve('file content'),
  json: () => Promise.resolve({}),
}) as never
globalThis.fetch = fileReadStub()

import ChatPage from '../pages/ChatPage'
import { closeActivityPanel, openActivityPanel } from '../store/chatSlice'
import { __resetPanelTabs, openPanelView, panelStripOf, usePanelTabs } from '../hooks/usePanelTabs'
import { CHAT_HISTORY_SEARCH_TAB_ID } from '../pages/chat/useChatHistorySearchTab'
import { virtualKeyFor, turnLeadKey } from '../pages/chat/ChatPageMessageContent'
import type { DisplayItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

const ASSISTANT_MSG = {
  role: 'assistant',
  content: 'hello',
  ts: '2026-06-23T20:00:00Z',
  meta: { file_changes: [{ path: '/f.txt', status: 'modified' }] },
}

const renderChatPage = ({ withWorker = false } = {}) => {
  const slot = { key: 'chat-1', title: 'chat-1', messages: 1, running: false, mode: '', created: '', last_ts: '' }
  // A subagent slot the active chat spawned makes the Dashboard dock
  // `relevant` (useCommandCenter scopes more than one slot to this root).
  const worker = { key: 'worker-1', title: 'worker', messages: 0, running: true, mode: '', created: '', last_ts: '', created_by: 'chat-1' }
  const slots = withWorker ? [slot, worker] : [slot]
  apiMocks.chatSlots = vi.fn().mockResolvedValue(slots)
  // On mount ChatPage loads the active slot's detail; return the seeded
  // message so the post-mount reconcile keeps it (an empty list would wipe it).
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: [ASSISTANT_MSG], has_more: false, total: 1 })
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: false,
      slots, approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
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

const FIND_PLACEHOLDER = 'Search'
// The field sits behind a lazy boundary (`ChatSidePanels`' `lazyRetry` import
// of `ChatHistorySearch`), so its first wait covers a chunk load: a cold or
// loaded runner can take longer than Testing Library's 1000ms default.
const SEARCH_FIELD_WAIT = { timeout: 10_000 }
const findField = () => screen.findByPlaceholderText(FIND_PLACEHOLDER, {}, SEARCH_FIELD_WAIT)
const openFind = () => {
  act(() => {
    fireEvent.keyDown(document, { key: 'f', ctrlKey: true })
  })
}

describe('ChatPage – the find pane and dock panels', () => {
  beforeEach(() => {
    Object.keys(apiMocks).forEach(k => delete apiMocks[k])
    globalThis.fetch = fileReadStub()
  })

  it('Ctrl+F opens the find pane', async () => {
    renderChatPage()
    expect(screen.queryByPlaceholderText(FIND_PLACEHOLDER)).toBeNull()
    openFind()
    expect(await findField()).toBeTruthy()
  })

  it("the session menu's first row is Search: it names the shortcut and opens the find pane", async () => {
    // The menu's Move rows read the folder list while it is open.
    apiMocks.chatFolders = vi.fn().mockResolvedValue([])
    renderChatPage()
    // The title row's trailing group already holds its peers, so Search is a
    // row in the session menu, not another button beside them.
    expect(screen.queryByRole('button', { name: 'Search' })).toBeNull()
    const trigger = await screen.findByRole('button', { name: 'Session options' })
    act(() => { fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' }) })
    const row = await screen.findByTestId('chat-header-search')
    expect(row).toHaveAttribute('role', 'menuitem')
    expect(row).toHaveTextContent('Search')
    expect(row).toHaveTextContent('Ctrl+F')
    expect(row.getAttribute('aria-keyshortcuts')).toBe('Control+F')
    expect(screen.getAllByRole('menuitem')[0]).toBe(row)
    expect(screen.queryByPlaceholderText(FIND_PLACEHOLDER)).toBeNull()
    act(() => { fireEvent.click(row) })
    expect(await findField()).toBeTruthy()
  })

  it("the side panel's + menu Search opens the find pane", async () => {
    const store = renderChatPage()
    act(() => { store.dispatch(openActivityPanel()) })
    const add = await screen.findByRole('button', { name: 'Open side panel tab' })
    act(() => { fireEvent.pointerDown(add, { button: 0, ctrlKey: false, pointerType: 'mouse' }) })
    act(() => { fireEvent.click(screen.getByRole('menuitem', { name: 'Search' })) })
    expect(await findField()).toBeTruthy()
  })

  // Opening a document from the chat only switches tabs: the Search tab and its
  // query stay until the person closes it, as in a bot chat.
  it.each([
    ['diff pane (diff pill)', 'open-diff', 'diff-panel'],
    ['file viewer (file chip)', 'open-file', 'md-panel'],
  ])('opening the %s keeps the Search tab and its query behind the new tab', async (_name, opener, body) => {
    const store = renderChatPage()
    seedMessage(store)
    openFind()
    const field = await findField()
    act(() => { fireEvent.change(field, { target: { value: 'plum' } }) })

    act(() => { fireEvent.click(screen.getByTestId(opener)) })

    await waitFor(() => expect(screen.getByTestId(body)).toBeTruthy())
    const searchTab = screen.getByRole('tab', { name: /Search this chat/ })
    expect(searchTab).toHaveAttribute('aria-selected', 'false')
    act(() => { fireEvent.click(searchTab) })
    await waitFor(() => expect(screen.getByPlaceholderText(FIND_PLACEHOLDER)).toHaveValue('plum'))
  })

  it('draws no status-tile HUD above the composer', async () => {
    localStorage.clear()
    renderChatPage({ withWorker: true })
    // A worker makes a team, which is exactly what used to raise the tiles.
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 50)) })
    expect(screen.queryByTestId('command-center-dock')).toBeNull()
  })
})

describe('ChatPage – the history search is an on-demand tab of the side panel', () => {
  beforeEach(() => {
    Object.keys(apiMocks).forEach(k => delete apiMocks[k])
    globalThis.fetch = fileReadStub()
    localStorage.clear()
    __resetPanelTabs()
    viewport.mobile = false
  })
  afterEach(() => { viewport.mobile = false })

  const searchChip = () => screen.queryByTestId(`side-panel-leading-tab-${CHAT_HISTORY_SEARCH_TAB_ID}`)
  const strip = () => panelStripOf('chat-1')
  const closeChip = () => act(() => { fireEvent.click(within(searchChip()!).getByRole('button', { name: 'Close tab' })) })
  const searchGone = () => waitFor(() => expect(screen.queryByPlaceholderText(FIND_PLACEHOLDER)).toBeNull())

  it('Ctrl+F opens the panel on a focused Search tab holding the search, with no separate pane', async () => {
    const store = renderChatPage()
    openFind()
    const input = await findField()
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(searchChip()).toBeTruthy()
    expect(strip().activeId).toBe(CHAT_HISTORY_SEARCH_TAB_ID)
    // The search is the Search tab's body inside the side panel itself.
    expect(input.closest('[data-testid="side-panel-leading-body"]')?.getAttribute('data-leading-id')).toBe(CHAT_HISTORY_SEARCH_TAB_ID)
    expect(input.closest('[data-testid="side-panel-inline"]')).toBeTruthy()
    expect(screen.getAllByTestId('chat-history-search')).toHaveLength(1)
    await waitFor(() => expect(document.activeElement).toBe(input))
  })

  it('a panel opened normally shows no Search tab, also after a search was left open and the panel closed', async () => {
    const store = renderChatPage()
    act(() => { store.dispatch(openActivityPanel()) })
    expect(await screen.findByTestId('side-panel-inline')).toBeTruthy()
    expect(searchChip()).toBeNull()
    openFind()
    await findField()
    // Closing the panel ends the search, so the next open has no Search tab.
    act(() => { store.dispatch(closeActivityPanel()) })
    await searchGone()
    act(() => { store.dispatch(openActivityPanel()) })
    expect(await screen.findByTestId('side-panel-inline')).toBeTruthy()
    expect(searchChip()).toBeNull()
    expect(strip().activeId).not.toBe(CHAT_HISTORY_SEARCH_TAB_ID)
  })

  it('a search that opened the panel closes the whole panel when its close button ends it', async () => {
    const store = renderChatPage()
    openFind()
    await findField()
    closeChip()
    await searchGone()
    await waitFor(() => expect(store.getState().chat.activityOpen).toBe(false))
    expect(searchChip()).toBeNull()
  })

  it('Escape on a search that opened the panel closes the panel too', async () => {
    const store = renderChatPage()
    openFind()
    await findField()
    act(() => { fireEvent.keyDown(document, { key: 'Escape' }) })
    await searchGone()
    await waitFor(() => expect(store.getState().chat.activityOpen).toBe(false))
  })

  it('a panel already open on Pins goes back to Pins and stays open', async () => {
    const store = renderChatPage()
    act(() => { store.dispatch(openActivityPanel()) })
    act(() => { openPanelView('chat-1', 'pins') })
    openFind()
    await findField()
    expect(strip().activeId).toBe(CHAT_HISTORY_SEARCH_TAB_ID)
    closeChip()
    await searchGone()
    expect(searchChip()).toBeNull()
    expect(strip().activeId).toBe('pins')
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('owns Escape only while its tab is in front, and Ctrl+F brings the tab back', async () => {
    renderChatPage()
    openFind()
    await findField()
    expect(screen.getByTestId('chat-history-search-tab')).toHaveAttribute('data-escape-owner')
    // Another tab in front: the search stays open behind it and owns nothing.
    act(() => { openPanelView('chat-1', 'pins') })
    await waitFor(() => expect(screen.getByTestId('chat-history-search-tab')).toHaveAttribute('data-history-search-off-screen'))
    expect(screen.getByTestId('chat-history-search-tab')).not.toHaveAttribute('data-escape-owner')
    act(() => { fireEvent.keyDown(document, { key: 'Escape' }) })
    expect(searchChip()).toBeTruthy()
    expect(strip().activeId).toBe('pins')
    openFind()
    await waitFor(() => expect(strip().activeId).toBe(CHAT_HISTORY_SEARCH_TAB_ID))
    expect(screen.getByTestId('chat-history-search-tab')).toHaveAttribute('data-escape-owner')
  })

  it('Ctrl+F in the focused search field ends the search; outside the field it focuses the field', async () => {
    const store = renderChatPage()
    openFind()
    const input = await findField()
    await waitFor(() => expect(document.activeElement).toBe(input))
    act(() => { (document.activeElement as HTMLElement).blur() })
    openFind()
    await waitFor(() => expect(document.activeElement).toBe(input))
    expect(searchChip()).toBeTruthy()
    act(() => { fireEvent.keyDown(input, { key: 'f', ctrlKey: true }) })
    await searchGone()
    await waitFor(() => expect(store.getState().chat.activityOpen).toBe(false))
  })

  it('a stored focus on the Search tab with no search open goes back to a real tab', async () => {
    act(() => { openPanelView('chat-1', 'pins') })
    // A remount (an in-app return) finds the strip still focused on the tab.
    const tabs = renderHook(() => usePanelTabs('chat-1', undefined, { keepFocusIds: [CHAT_HISTORY_SEARCH_TAB_ID] }))
    act(() => { tabs.result.current.setActive(CHAT_HISTORY_SEARCH_TAB_ID) })
    tabs.unmount()
    expect(strip().activeId).toBe(CHAT_HISTORY_SEARCH_TAB_ID)
    renderChatPage()
    await waitFor(() => expect(strip().activeId).not.toBe(CHAT_HISTORY_SEARCH_TAB_ID))
    expect(searchChip()).toBeNull()
  })

  it('on a phone the search is the full-screen Search tab of the overlay, with no tab strip and no sheet of its own', async () => {
    viewport.mobile = true
    const store = renderChatPage()
    openFind()
    const input = await findField()
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(input.closest('[data-testid="side-panel-inline"]')).toBeTruthy()
    expect(strip().activeId).toBe(CHAT_HISTORY_SEARCH_TAB_ID)
    // Only the search's own header (field and Cancel) shows: the strip is not drawn.
    expect(screen.getByTestId('side-panel-strip')).toHaveClass('!hidden')
    expect(screen.queryByTestId('chat-history-sheet')).toBeNull()
    // It covers the chat's top bar too (title, bell, ⋯): truly full screen.
    const overlay = screen.getByTestId('side-panel-inline')
    expect(overlay).toHaveClass('top-safe', 'z-[60]')
    expect(overlay).not.toHaveClass('top-safe-offset-[42px]')
  })

  it('on a phone a panel opened normally sits below the top bar', async () => {
    viewport.mobile = true
    const store = renderChatPage()
    act(() => { store.dispatch(openActivityPanel()) })
    act(() => { openPanelView('chat-1', 'pins') })
    const overlay = await screen.findByTestId('side-panel-inline')
    expect(overlay).toHaveClass('top-safe-offset-[42px]')
  })

  it('on a desktop the Search tab sits in the drawn tab strip', async () => {
    renderChatPage()
    openFind()
    await findField()
    expect(screen.getByTestId('side-panel-strip')).not.toHaveClass('!hidden')
    expect(searchChip()).toBeTruthy()
  })

  it('on a phone Cancel closes the overlay, even one already open on another tab', async () => {
    viewport.mobile = true
    const store = renderChatPage()
    act(() => { store.dispatch(openActivityPanel()) })
    act(() => { openPanelView('chat-1', 'pins') })
    openFind()
    await findField()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await searchGone()
    await waitFor(() => expect(store.getState().chat.activityOpen).toBe(false))
  })

  it('on a phone Quote in the preview closes the overlay and ends the search', async () => {
    viewport.mobile = true
    const store = renderChatPage()
    apiMocks.chatHistorySearch = vi.fn().mockResolvedValue({
      hits: [{ index: 0, role: 'assistant', ts: ASSISTANT_MSG.ts, mid: 'm-1', snippet: 'hello there' }], next_before: null, total: 1,
    })
    apiMocks.chatHistoryAround = vi.fn().mockResolvedValue({
      messages: [{ role: 'assistant', content: 'hello there', cls: 'msg msg-a', ts: ASSISTANT_MSG.ts, meta: { mid: 'm-1' } }],
      start: 0, end: 1, total: 1, has_earlier: false,
    })
    openFind()
    const input = await findField()
    fireEvent.change(input, { target: { value: 'hello' } })
    fireEvent.click((await screen.findAllByRole('option', {}, { timeout: 3000 }))[0])
    const target = await waitFor(() => {
      const el = screen.getByTestId('chat-history-preview').querySelector('[data-jump-target]')
      expect(el).not.toBeNull()
      return el as HTMLElement
    })
    act(() => { fireEvent.click(within(target).getByTestId('quote-message')) })
    await searchGone()
    await waitFor(() => expect(store.getState().chat.activityOpen).toBe(false))
  })
})


// The per-message identity resolver ChatPage feeds virtualKeyFor: prefer the
// optimistic clientTs (for steer-bubble stability), then ts, then a
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
    const grp: DisplayItem = { kind: 'group', msgs: [{ role: 'tool', content: '🔧 a', cls: '', ts: 'g1' }], startIdx: 9 } as never
    const asGroup = virtualKeyFor(grp, 9, msgKey)
    const asTurn = virtualKeyFor(turnOf([grp]), 9, msgKey)
    expect(asTurn).toBe(asGroup)
    // Keyed on the FIRST MESSAGE's identity, never the array index.
    expect(asGroup).toBe('grp-g1')
  })

  it('a group key is UNCHANGED by a prepend (indices renumber, identity does not)', () => {
    const msgKey = makeMsgKey()
    // DISTINCT objects for before/after, carrying equal identity fields — so
    // this pins "keyed on the first message's ts", and would fail for an
    // implementation keyed on object identity or a per-object minted id.
    const mkMsgs = (): ChatMessage[] => [
      { role: 'tool', content: '🔧 grep', cls: '', ts: 'p1' },
      { role: 'tool', content: '🔧 cat', cls: '', ts: 'p2' },
    ]
    // Before: the group starts at message index 4.
    const before: DisplayItem = { kind: 'group', msgs: mkMsgs(), startIdx: 4 } as never
    // After: a history backfill prepends 50 older messages — every index
    // shifts, and the store rebuild hands React NEW message objects with the
    // same identities.
    const after: DisplayItem = { kind: 'group', msgs: mkMsgs(), startIdx: 54 } as never
    const keyBefore = virtualKeyFor(before, 4, msgKey)
    const keyAfter = virtualKeyFor(after, 54, msgKey)
    // Same row identity → HeightCache entry, DOM node, and any group-led
    // scroll anchor survive the prepend instead of going unfindable.
    expect(keyAfter).toBe(keyBefore)
    // And a DIFFERENT group at the old position must not steal the key.
    const usurper: DisplayItem = {
      kind: 'group',
      msgs: [{ role: 'tool', content: '🔧 ls', cls: '', ts: 'q1' }],
      startIdx: 4,
    } as never
    expect(virtualKeyFor(usurper, 4, msgKey)).not.toBe(keyBefore)
  })

  it('two sibling groups whose leads share a coarse-clock ts get DISTINCT keys (mid tie-break)', () => {
    const msgKey = makeMsgKey()
    // The reducer explicitly supports distinct rows stamped in the same OS
    // tick (see isRedeliveredMessage in transcript.ts under store/chat) — row
    // identity is meta.mid. The index key this change replaces was unique by
    // construction; the mid tie-break keeps that property so sibling groups
    // never alias each other's HeightCache entry or React key.
    const a: DisplayItem = {
      kind: 'group',
      msgs: [{ role: 'tool', content: '🔧 grep', cls: '', ts: 'tick-7', meta: { mid: 'm-1' } }],
      startIdx: 2,
    } as never
    const b: DisplayItem = {
      kind: 'group',
      msgs: [{ role: 'tool', content: '🔧 cat', cls: '', ts: 'tick-7', meta: { mid: 'm-2' } }],
      startIdx: 5,
    } as never
    expect(virtualKeyFor(a, 2, msgKey)).not.toBe(virtualKeyFor(b, 5, msgKey))
    // A locally-minted row without a mid keeps the plain identity key (the
    // uniqueness it had before), and does not collide with the mid-suffixed form.
    const local: DisplayItem = {
      kind: 'group',
      msgs: [{ role: 'tool', content: '🔧 ls', cls: '', ts: 'tick-7' }],
      startIdx: 8,
    } as never
    const localKey = virtualKeyFor(local, 8, msgKey)
    expect(localKey).toBe('grp-tick-7')
    expect(localKey).not.toBe(virtualKeyFor(a, 2, msgKey))
  })

  it('turnLeadKey is total: an empty group degrades to the index instead of throwing', () => {
    const msgKey = makeMsgKey()
    // Unreachable from both producers (they emit only under `if (group.length)`)
    // but the type allows `[]` and turnLeadKey is a public export.
    const empty = { kind: 'group', msgs: [], startIdx: 3 } as never
    expect(turnLeadKey(empty, msgKey)).toBe('grp-idx-3')
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
