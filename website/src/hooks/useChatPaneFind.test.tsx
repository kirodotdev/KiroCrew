/**
 * Find in chat in a Bot's chat (#39585). The Bot chat's transcript is
 * virtualized, so the browser's own find never reaches a message scrolled out
 * of view; ⌘F / Ctrl+F must open the Sessions chat's find over this pane, find
 * every loaded message, and bring an unmounted match row into the window.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from './useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'
import { api } from '../api/client'
import ChatPane from '../components/ChatPane'
import ChatMessageList, { type VirtualTranscriptHandle } from '../app-sdk/ChatMessageList'
import type { MessageRenderer } from '../app-sdk/messageRenderers'
import SearchHighlightContext, { MessageSearchScope, useCurrentOcc, useSearchHighlight } from './SearchHighlightContext'
import { claimScrollOwnership } from '../utils/searchScroll'
import type { DisplayItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'
import { useChatPaneFind } from './useChatPaneFind'
import { useNavToDisplayIndex } from '../pages/chat/useNavToDisplayIndex'

// The real hook, spied on: the pane's find must jump through the Sessions
// chat's own jump, not a copy of it.
vi.mock('../pages/chat/useNavToDisplayIndex', async (importOriginal) => {
  const real = await importOriginal<typeof import('../pages/chat/useNavToDisplayIndex')>()
  return { ...real, useNavToDisplayIndex: vi.fn(real.useNavToDisplayIndex) }
})

const { focusComposer } = vi.hoisted(() => ({ focusComposer: vi.fn() }))
vi.mock('../pages/chat/composerFocus', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../pages/chat/composerFocus')>()),
  focusComposer,
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('./useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('./useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
vi.mock('./useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('./useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

afterEach(() => { cleanup(); vi.useRealTimers(); focusComposer.mockClear(); vi.mocked(useNavToDisplayIndex).mockClear() })

/** A 40-turn chat: 80 messages, the word "needle" only in the first prompt. */
const longChat: ChatMessage[] = Array.from({ length: 80 }, (_, i) => ({
  role: i % 2 === 0 ? 'user' : 'assistant',
  content: i === 0 ? 'the first prompt has a needle in it' : `message ${i}`,
  cls: '',
  ts: `2026-11-20T10:00:${String(i).padStart(2, '0')}Z`,
}))
const rows: DisplayItem[] = longChat.map((msg, idx) => ({ kind: 'single', msg, idx }))

const press = (init: KeyboardEventInit): KeyboardEvent => {
  const event = new KeyboardEvent('keydown', { key: 'f', cancelable: true, ...init })
  act(() => { document.dispatchEvent(event) })
  return event
}

function setup(enabled: boolean) {
  vi.useFakeTimers()
  const list = {
    mountIndex: vi.fn(() => true),
    estimateRowTop: vi.fn(() => 4000),
  } as unknown as VirtualTranscriptHandle
  // The window is at the bottom of the chat: no row of the first turn is mounted.
  const scroller = document.createElement('div')
  Object.defineProperty(scroller, 'clientHeight', { value: 600 })
  Object.defineProperty(scroller, 'scrollHeight', { value: 10000 })
  const scrollerRef = { current: scroller }
  const hook = renderHook(({ on }) => useChatPaneFind({
    enabled: on,
    messages: longChat,
    slotKey: 'member-bot',
    displayItemsRef: { current: rows },
    listRef: { current: list },
    scrollerRef,
  }), { initialProps: { on: enabled } })
  return { ...hook, list, scroller, scrollerRef }
}

describe('useChatPaneFind', () => {
  it('starts row navigation before a mounted child scrolls to the current occurrence', () => {
    vi.useFakeTimers()
    const order: string[] = []
    const messages: ChatMessage[] = [{ role: 'assistant', content: 'needle then needle', cls: '', ts: 'reply' }]
    const displayItemsRef = { current: messages.map((msg, idx): DisplayItem => ({ kind: 'single', msg, idx })) }
    const listRef = { current: { mountIndex: vi.fn(() => false) } as unknown as VirtualTranscriptHandle }
    const scrollerRef = { current: document.createElement('div') }
    const realHook = vi.mocked(useNavToDisplayIndex).getMockImplementation()!

    function Child() {
      const currentOcc = useCurrentOcc()
      useEffect(() => { if (currentOcc >= 0) order.push('child') }, [currentOcc])
      return null
    }
    function Parent() {
      const { search, highlight } = useChatPaneFind({
        enabled: true, messages, slotKey: 'member-bot-order', displayItemsRef, listRef, scrollerRef,
      })
      return (
        <SearchHighlightContext.Provider value={highlight}>
          <button onClick={() => search.setTerm('needle')}>Find</button>
          <button onClick={search.next}>Next</button>
          <MessageSearchScope messageIdx={0}><Child /></MessageSearchScope>
        </SearchHighlightContext.Provider>
      )
    }

    vi.mocked(useNavToDisplayIndex).withImplementation((options) => {
      const navigation = realHook(options)
      return {
        ...navigation,
        navToMessage: (...args) => { order.push('nav'); return navigation.navToMessage(...args) },
      }
    }, () => {
      render(<Parent />)
      act(() => { screen.getByRole('button', { name: 'Find' }).click() })
      act(() => { vi.advanceTimersByTime(50) })
      expect(order).toEqual(['nav', 'child'])

      order.length = 0
      act(() => { screen.getByRole('button', { name: 'Next' }).click() })
      expect(order).toEqual(['nav', 'child'])
    })
  })

  it('opens on Ctrl+F, finds a message far above the window and brings its row in', () => {
    const { result, list, scroller, scrollerRef } = setup(true)
    expect(useNavToDisplayIndex).toHaveBeenCalledWith(expect.objectContaining({ scrollerRef }))
    const event = press({ ctrlKey: true })
    expect(result.current.search.isOpen).toBe(true)
    expect(event.defaultPrevented).toBe(true)

    act(() => { result.current.search.setTerm('needle') })
    act(() => { vi.advanceTimersByTime(60) })
    expect(result.current.search.matches).toEqual([{ msgIdx: 0, occ: 0 }])
    expect(result.current.highlight).toMatchObject({ term: 'needle', currentMessageIdx: 0, currentOccurrenceIdx: 0 })
    expect(list.mountIndex).toHaveBeenCalledWith(0, undefined)

    // A far row takes several frames to commit: nothing scrolls while it is
    // missing (no jump to an estimate, no jump to the top)...
    act(() => { vi.advanceTimersByTime(100) })
    expect(scroller.scrollTop).toBe(0)
    // ...and once it mounts, the jump centres it.
    const row = document.createElement('div')
    row.setAttribute('data-display-index', '0')
    row.getBoundingClientRect = () => ({ top: 4000, height: 80, bottom: 4080, left: 0, right: 0, width: 0, x: 0, y: 4000, toJSON: () => ({}) })
    scroller.appendChild(row)
    act(() => { vi.advanceTimersByTime(20) })
    expect(scroller.scrollTop).toBe(4000 - 300)
  })

  it('navigates again when a new term lands in the same unmounted row, but not when messages stream', () => {
    vi.useFakeTimers()
    const messages: ChatMessage[] = [{ role: 'user', content: 'alpha beta', cls: '', ts: 'a' }]
    const list = { mountIndex: vi.fn(() => true) } as unknown as VirtualTranscriptHandle
    const scroller = document.createElement('div')
    const displayItemsRef = { current: messages.map((msg, idx): DisplayItem => ({ kind: 'single', msg, idx })) }
    const listRef = { current: list }
    const scrollerRef = { current: scroller }
    // Each navigation announces itself once; the row never mounts here, so the
    // jump's poll keeps asking for its mount, and the mount count says nothing.
    let jumps = 0
    const onJump = () => { jumps++ }
    window.addEventListener('mc-chat-scroll-jump', onJump)
    const { result, rerender } = renderHook(({ messages }) => useChatPaneFind({
      enabled: true, messages, slotKey: 'member-bot-term', displayItemsRef, listRef, scrollerRef,
    }), { initialProps: { messages } })
    expect(scroller.querySelector('[data-display-index="0"]')).toBeNull()

    act(() => { result.current.search.setTerm('alpha') })
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.search.matches).toEqual([{ msgIdx: 0, occ: 0 }])
    expect(jumps).toBe(1)

    act(() => { result.current.search.setTerm('beta') })
    // The old term's matches remain until the debounce finishes.
    expect(jumps).toBe(1)
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.search.matches).toEqual([{ msgIdx: 0, occ: 0 }])
    expect(jumps).toBe(2)
    expect(list.mountIndex.mock.calls.at(-1)?.[0]).toBe(0)

    const previousMatches = result.current.search.matches
    rerender({ messages: [...messages, { role: 'assistant', content: 'streamed reply', cls: '', ts: 'b' }] })
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.search.matches).not.toBe(previousMatches)
    expect(result.current.search.matches).toEqual([{ msgIdx: 0, occ: 0 }])
    expect(jumps).toBe(2)

    act(() => { result.current.search.setTerm('alpha') })
    expect(jumps).toBe(2)
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.search.matches).toEqual([{ msgIdx: 0, occ: 0 }])
    expect(jumps).toBe(3)
    window.removeEventListener('mc-chat-scroll-jump', onJump)
  })

  it('leaves a narrowed term in a mounted row to the message, which keeps scroll ownership', () => {
    vi.useFakeTimers()
    const supersededFor: string[] = []
    const messages: ChatMessage[] = [{ role: 'assistant', content: 'a long reply that says alphab once', cls: '', ts: 'reply' }]
    const displayItemsRef = { current: messages.map((msg, idx): DisplayItem => ({ kind: 'single', msg, idx })) }
    const list = { mountIndex: vi.fn(() => false) } as unknown as VirtualTranscriptHandle
    const listRef = { current: list }
    // The match's row is mounted and taller than the viewport: centring the
    // row instead of the word can leave the word off screen.
    const scroller = document.createElement('div')
    Object.defineProperty(scroller, 'clientHeight', { value: 600 })
    const row = document.createElement('div')
    row.setAttribute('data-display-index', '0')
    row.getBoundingClientRect = () => ({ top: 0, height: 3000, bottom: 3000, left: 0, right: 0, width: 0, x: 0, y: 0, toJSON: () => ({}) })
    scroller.appendChild(row)
    const scrollerRef = { current: scroller }

    // Stands in for the message's occurrence poll (scrollCurrentMatchIntoView):
    // it claims scroll ownership whenever the term or the current occurrence changes.
    function Occurrence() {
      const { term } = useSearchHighlight()
      const currentOcc = useCurrentOcc()
      useEffect(() => {
        if (currentOcc < 0) return
        return claimScrollOwnership(() => { supersededFor.push(term) })
      }, [term, currentOcc])
      return null
    }
    function Parent() {
      const { search, highlight } = useChatPaneFind({
        enabled: true, messages, slotKey: 'member-bot-narrow', displayItemsRef, listRef, scrollerRef,
      })
      return (
        <SearchHighlightContext.Provider value={highlight}>
          <input aria-label="term" value={search.term} onChange={(e) => search.setTerm(e.target.value)} />
          <MessageSearchScope messageIdx={0}><Occurrence /></MessageSearchScope>
        </SearchHighlightContext.Provider>
      )
    }

    render(<Parent />)
    const input = screen.getByRole('textbox', { name: 'term' })
    act(() => { fireEvent.change(input, { target: { value: 'alpha' } }) })
    act(() => { vi.advanceTimersByTime(50) })
    expect(list.mountIndex).toHaveBeenCalledTimes(1)

    act(() => { fireEvent.change(input, { target: { value: 'alphab' } }) })
    act(() => { vi.advanceTimersByTime(50) })
    // Same message, same match index: no row jump takes the scroll away from
    // the poll centring "alphab".
    expect(supersededFor).toEqual([])
    expect(list.mountIndex).toHaveBeenCalledTimes(1)
  })

  it('leaves the key to the browser while the pane does not offer the find', () => {
    const { result } = setup(false)
    const event = press({ ctrlKey: true })
    expect(result.current.search.isOpen).toBe(false)
    expect(event.defaultPrevented).toBe(false)
  })

  it('closes an open bar when the pane stops offering the find, leaving focus where the user moved it', () => {
    const { result, rerender } = setup(true)
    press({ ctrlKey: true })
    act(() => { result.current.search.setTerm('needle') })
    expect(result.current.search.isOpen).toBe(true)
    rerender({ on: false })
    expect(result.current.search.isOpen).toBe(false)
    expect(result.current.search.term).toBe('')
    // The user just moved into a side panel or drawer: pulling the caret back
    // into the composer would take their focus away.
    expect(focusComposer).not.toHaveBeenCalled()
  })

  it('still hands typing back to the composer when the user closes the bar with Escape', () => {
    const { result } = setup(true)
    press({ ctrlKey: true })
    press({ key: 'Escape' })
    expect(result.current.search.isOpen).toBe(false)
    expect(focusComposer).toHaveBeenCalledTimes(1)
  })
})

/** Prints what `useCurrentOcc` reads for the row it is drawn in. */
function OccProbe({ text }: { text: string }) {
  return <span data-testid={text}>{String(useCurrentOcc())}</span>
}
const probeRenderer: MessageRenderer = {
  id: 'user',
  roles: ['user', 'assistant'],
  render: (m) => <OccProbe key={m.ts} text={m.content} />,
}

function List({ searchRows }: { searchRows: boolean }) {
  const ref = useRef<VirtualTranscriptHandle | null>(null)
  const messages: ChatMessage[] = [
    { role: 'user', content: 'first', cls: '', ts: 'a' },
    { role: 'user', content: 'second', cls: '', ts: 'b' },
  ]
  return (
    <SearchHighlightContext.Provider value={{ term: 'x', caseSensitive: false, currentMessageIdx: 1, currentOccurrenceIdx: 0 }}>
      <ChatMessageList ref={ref} messages={messages} running={false} renderers={[probeRenderer]} searchRows={searchRows} />
    </SearchHighlightContext.Provider>
  )
}

describe('ChatMessageList searchRows', () => {
  it('tells each row its message index, so only the current match row is current', () => {
    render(<List searchRows />)
    expect(screen.getByTestId('first').textContent).toBe('-1')
    expect(screen.getByTestId('second').textContent).toBe('0')
  })

  it('leaves rows unscoped when the host runs no find of its own', () => {
    render(<List searchRows={false} />)
    expect(screen.getByTestId('second').textContent).toBe('-1')
  })
})

function renderPane(slot: string, content: string, props: { findInChat: boolean; topInset?: number }) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  })
  vi.mocked(api.chatSlotDetail).mockResolvedValue({
    messages: [{ role: 'user', content, cls: '', ts: '2026-11-20T10:00:00Z' }],
    running: false, has_more: false, total: 1,
  } as unknown as Awaited<ReturnType<typeof api.chatSlotDetail>>)
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slot, messages: 1, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const pane = (p: { findInChat: boolean; topInset?: number }) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slot} frameless findInChat={p.findInChat} topInset={p.topInset} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const view = render(pane(props))
  return { ...view, rerenderPane: (p: { findInChat: boolean; topInset?: number }) => view.rerender(pane(p)) }
}

describe('ChatPane rows across findInChat', () => {
  it('keeps every mounted row when the pane stops and starts answering the find', async () => {
    const view = renderPane('member-bot-rows', 'keep this row mounted', { findInChat: true })
    const row = await waitFor(() => screen.getByText('keep this row mounted'))
    // A side panel takes focus: the pane stops answering ⌘F. The row's DOM
    // node must survive; a remount would drop its state (an open menu, a
    // half-selected quote, an expanded block).
    view.rerenderPane({ findInChat: false })
    expect(screen.getByText('keep this row mounted')).toBe(row)
    view.rerenderPane({ findInChat: true })
    expect(screen.getByText('keep this row mounted')).toBe(row)
  })
})

describe('ChatPane find bar and pinned prompt', () => {
  it('moves the pinned-prompt band down under the open find bar', async () => {
    const view = renderPane('member-bot-pin', 'a prompt', { findInChat: true, topInset: 40 })
    await waitFor(() => screen.getByText('a prompt'))
    // The band is the parent of the zero-height fold sentinel the banner sticks to.
    const band = () => view.container.querySelector('div[aria-hidden].h-0')!.parentElement as HTMLElement
    expect(band().style.top).toBe('40px')
    press({ ctrlKey: true })
    expect(screen.getByRole('combobox', { name: 'Find in chat' })).toBeTruthy()
    expect(band().style.top).toBe('88px')
    press({ key: 'Escape' })
    expect(band().style.top).toBe('40px')
  })
})

// A Bot's chat pages older history in while the reader is near the top, with
// the find bar open. A page landing renumbers every loaded message and row. It
// must leave the current match on its message and the reader where they are,
// and a jump that is still settling when the page lands must stay on the
// message it was for.
describe('useChatPaneFind when an older page lands', () => {
  const at = (n: number) => String(n).padStart(2, '0')
  /** The loaded messages: L00 to L19, the word only where `needle` says. */
  const loadedChat = (needle: number): ChatMessage[] => Array.from({ length: 20 }, (_, i) => ({
    role: i % 2 === 0 ? 'user' : 'assistant',
    content: i === needle ? `L${at(i)} asks about the needle` : `L${at(i)} text`,
    cls: '',
    ts: `2026-11-20T11:${at(i)}:00Z`,
  }))
  /** The older page: P0 to P4, the word in P2 when `withMatch`. */
  const olderPage = (withMatch: boolean): ChatMessage[] => Array.from({ length: 5 }, (_, i) => ({
    role: i % 2 === 0 ? 'user' : 'assistant',
    content: withMatch && i === 2 ? 'P2 also has the needle' : `P${i} text`,
    cls: '',
    ts: `2026-11-20T10:0${i}:00Z`,
  }))
  const rowsOf = (messages: ChatMessage[]): DisplayItem[] => messages.map((msg, idx) => ({ kind: 'single', msg, idx }))
  const ROW_H = 100
  const VIEW_H = 600
  /** Where the jump puts the scroller to center display row `di` (rows are ROW_H apart). */
  const centered = (di: number) => di * ROW_H - VIEW_H / 2

  function mountPane(loaded: ChatMessage[]) {
    vi.useFakeTimers()
    const scroller = document.createElement('div')
    Object.defineProperty(scroller, 'clientHeight', { value: VIEW_H })
    Object.defineProperty(scroller, 'scrollHeight', { value: 100000 })
    const scrolls: number[] = []
    scroller.scrollTo = ((o: ScrollToOptions) => { scrolls.push(o.top ?? -1) }) as typeof scroller.scrollTo
    /** The rows the list has mounted, as a commit of the list draws them. */
    const draw = (count: number) => scroller.replaceChildren(...Array.from({ length: count }, (_, i) => {
      const row = document.createElement('div')
      row.setAttribute('data-display-index', String(i))
      row.getBoundingClientRect = () => ({ top: i * ROW_H, height: ROW_H, bottom: (i + 1) * ROW_H, left: 0, right: 0, width: 0, x: 0, y: i * ROW_H, toJSON: () => ({}) })
      return row
    }))
    const list = { mountIndex: vi.fn(() => false) } as unknown as VirtualTranscriptHandle
    const displayItemsRef = { current: rowsOf(loaded) }
    draw(loaded.length)
    const hook = renderHook(({ messages }) => useChatPaneFind({
      enabled: true, messages, slotKey: 'member-bot-landing', displayItemsRef, listRef: { current: list }, scrollerRef: { current: scroller },
    }), { initialProps: { messages: loaded } })
    /** The page lands: the list draws the longer list in one commit. */
    const land = (all: ChatMessage[]) => act(() => {
      displayItemsRef.current = rowsOf(all)
      draw(all.length)
      hook.rerender({ messages: all })
    })
    return { ...hook, list, scrolls, land, displayItemsRef }
  }

  it('moves nobody to a match that no row on screen draws, and spends its find action', () => {
    const loaded = loadedChat(19)
    const pane = mountPane(loaded)
    pane.displayItemsRef.current = rowsOf(loaded.slice(0, 19))
    act(() => { pane.result.current.search.setTerm('needle') })
    act(() => { vi.advanceTimersByTime(60) })
    const { reveal, revealLedger } = pane.result.current.highlight
    expect(reveal).toBeGreaterThan(0)
    expect(pane.list.mountIndex).not.toHaveBeenCalled()
    expect(pane.scrolls).toEqual([])
    // Spent, so L19's row cannot use it when the reader's own scroll mounts it.
    expect(revealLedger.pending(reveal)).toBe(false)
  })

  it('keeps L19 the current match and moves nobody when the page brings an older one (P2)', () => {
    const loaded = loadedChat(19)
    const pane = mountPane(loaded)
    act(() => { pane.result.current.search.setTerm('needle') })
    act(() => { vi.advanceTimersByTime(60) })
    expect(pane.result.current.highlight.currentMessageIdx).toBe(19)
    expect(pane.list.mountIndex).toHaveBeenCalledTimes(1)

    pane.scrolls.length = 0
    pane.land([...olderPage(true), ...loaded])
    act(() => { vi.advanceTimersByTime(500) })
    expect(pane.result.current.search.matches).toHaveLength(2)
    // L19, at its new index, is still the match the reader is on.
    expect(pane.result.current.highlight.currentMessageIdx).toBe(5 + 19)
    expect(pane.list.mountIndex).toHaveBeenCalledTimes(1)
    // Whatever still scrolls is the jump to L19, on its new row.
    expect(pane.scrolls.every((top) => top === centered(5 + 19))).toBe(true)
  })

  it('keeps a jump that is still settling on its message when the page lands', () => {
    const loaded = loadedChat(5)
    const pane = mountPane(loaded)
    act(() => { pane.result.current.search.setTerm('needle') })
    act(() => { vi.advanceTimersByTime(60) })
    act(() => { vi.advanceTimersByTime(48) })
    expect(pane.scrolls.at(-1)).toBe(centered(5))

    pane.scrolls.length = 0
    pane.land([...olderPage(false), ...loaded])
    // Within the search's debounce: the row that now holds number 5 is L00.
    act(() => { vi.advanceTimersByTime(32) })
    expect(pane.scrolls.length).toBeGreaterThan(0)
    expect(new Set(pane.scrolls)).toEqual(new Set([centered(5 + 5)]))
  })
})
