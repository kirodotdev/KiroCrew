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
        navToDisplayIndex: (...args) => { order.push('nav'); navigation.navToDisplayIndex(...args) },
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
    const { result, rerender } = renderHook(({ messages }) => useChatPaneFind({
      enabled: true, messages, slotKey: 'member-bot-term', displayItemsRef, listRef, scrollerRef,
    }), { initialProps: { messages } })
    expect(scroller.querySelector('[data-display-index="0"]')).toBeNull()

    act(() => { result.current.search.setTerm('alpha') })
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.search.matches).toEqual([{ msgIdx: 0, occ: 0 }])
    expect(list.mountIndex).toHaveBeenCalledTimes(1)

    act(() => { result.current.search.setTerm('beta') })
    // The old term's matches remain until the debounce finishes.
    expect(list.mountIndex).toHaveBeenCalledTimes(1)
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.search.matches).toEqual([{ msgIdx: 0, occ: 0 }])
    expect(list.mountIndex).toHaveBeenCalledTimes(2)
    expect(list.mountIndex).toHaveBeenLastCalledWith(0, undefined)

    const previousMatches = result.current.search.matches
    rerender({ messages: [...messages, { role: 'assistant', content: 'streamed reply', cls: '', ts: 'b' }] })
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.search.matches).not.toBe(previousMatches)
    expect(result.current.search.matches).toEqual([{ msgIdx: 0, occ: 0 }])
    expect(list.mountIndex).toHaveBeenCalledTimes(2)

    act(() => { result.current.search.setTerm('alpha') })
    expect(list.mountIndex).toHaveBeenCalledTimes(2)
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.search.matches).toEqual([{ msgIdx: 0, occ: 0 }])
    expect(list.mountIndex).toHaveBeenCalledTimes(3)
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
