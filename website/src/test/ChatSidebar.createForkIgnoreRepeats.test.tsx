/**
 * A repeated click on a sidebar create control makes one session, the same
 * rule the header New button keeps with `disabled={creatingSlot}`.
 *
 *   - Folder "+": a second or third plain click while the first create is in
 *     flight is dropped. Once it settles, the next click creates again. The
 *     tab gestures are exempt, since each one opens its own tab.
 *   - Session row "Fork chat": a second click while the fork of that session
 *     is in flight is dropped. A fork of another session still goes through.
 *
 * Both clicks of a double click land before React re-renders, so these fire
 * them back to back with the API held pending, the way a real double click
 * reaches the handlers.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, renderHook, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot, ChatFolder } from '../types'

// Render framer-motion elements as plain DOM because jsdom cannot run projection.
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

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn(), forkChatSlot: vi.fn(), chatFolders: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
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
import { useFolderChatCreate } from '../pages/chat-sidebar/create'

const ORIGIN = 's1'
const OTHER = 's2'
const FOLDER_ID = 'folder-alpha'
const SLOTS = [
  { key: ORIGIN, title: 'Session 1', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' },
  { key: OTHER, title: 'Session 2', messages: 1, running: false, mode: '' },
] as ChatSlot[]
const FOLDER_B = 'folder-beta'
const FOLDERS: ChatFolder[] = [
  { id: FOLDER_ID, name: 'Alpha', order: 0, collapsed: false },
  { id: FOLDER_B, name: 'Beta', order: 1, collapsed: false },
]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'linux' }, connected: true, slots: SLOTS, slotsLoaded: true,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: ORIGIN, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0, pendingInput: null, slotContextPct: {},
      voicePlaying: false, voiceAudio: null, subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
      slotActivity: {}, slotHistory: [], slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], FOLDERS)
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={ORIGIN} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="kirocrew" installedAgents={[]}
              onOpenSlotInNewTab={vi.fn()}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

/** A promise the test settles by hand, so a request stays in flight. */
function deferred<T>() {
  let resolve: (v: T) => void = () => {}
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

const plusButton = () => screen.getByTestId(`folder-new-chat-${FOLDER_ID}`)
function forkButton(slotKey: string): HTMLElement {
  const row = document.querySelector(`[data-session-row="${slotKey}"]`) as HTMLElement | null
  if (!row) throw new Error(`no row for ${slotKey}; rows: ${Array.from(document.querySelectorAll('[data-session-row]')).map(r => r.getAttribute('data-session-row')).join(',')}`)
  return Array.from(row.querySelectorAll('button')).find(b => b.getAttribute('aria-label') === 'Fork chat')!
}

beforeEach(() => {
  localStorage.clear()
  mocks.createChatSlot.mockReset()
  mocks.forkChatSlot.mockReset()
  // The folder query refetches on mount; answer with the same folder so the
  // "+" stays rendered for the later clicks.
  mocks.chatFolders.mockReset().mockResolvedValue(FOLDERS)
})

describe('folder "+" ignores repeat clicks while its create is in flight', () => {
  it('a double click creates one session', async () => {
    const pending = deferred<unknown>()
    mocks.createChatSlot.mockReturnValue(pending.promise)
    renderSidebar()
    fireEvent.click(plusButton())
    fireEvent.click(plusButton())
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    await act(async () => { pending.resolve({ key: 'chat-new-1', folder_id: FOLDER_ID }) })
    expect(mocks.createChatSlot).toHaveBeenCalledTimes(1)
  })

  it('a triple click creates one session, and a click after it settles creates again', async () => {
    const first = deferred<unknown>()
    mocks.createChatSlot.mockReturnValueOnce(first.promise).mockResolvedValue({ key: 'chat-new-2', folder_id: FOLDER_ID })
    const store = renderSidebar()
    fireEvent.click(plusButton())
    fireEvent.click(plusButton())
    fireEvent.click(plusButton())
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    await act(async () => { first.resolve({ key: 'chat-new-1', folder_id: FOLDER_ID }) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-new-1'))
    fireEvent.click(plusButton())
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(2))
  })

  it('a click after a failed create creates again', async () => {
    mocks.createChatSlot.mockRejectedValueOnce(new Error('no capacity')).mockResolvedValue({ key: 'chat-new-1', folder_id: FOLDER_ID })
    renderSidebar()
    fireEvent.click(plusButton())
    await screen.findByText(/no capacity/)
    fireEvent.click(plusButton())
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(2))
  })

  it('a click during a pending header New still creates', async () => {
    // Header New sets the global `creatingSlot`, as background creates do.
    // The folder "+" guard must not read it: this click is a new request,
    // not a repeat of the "+" create.
    const header = deferred<unknown>()
    mocks.createChatSlot.mockReturnValueOnce(header.promise).mockReturnValue(deferred<unknown>().promise)
    const store = renderSidebar()
    const headerNew = document.querySelector('[data-create-menu] > button') as HTMLButtonElement
    fireEvent.click(headerNew)
    await waitFor(() => expect(store.getState().chat.creatingSlot).toBe(true))
    expect(mocks.createChatSlot).toHaveBeenCalledTimes(1)
    fireEvent.click(plusButton())
    fireEvent.click(plusButton())
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(2))
    // folder_id is createChatSlot's 8th positional argument.
    expect(mocks.createChatSlot.mock.calls[0][7]).toBeUndefined()
    expect(mocks.createChatSlot.mock.calls[1][7]).toBe(FOLDER_ID)
  })

  it('a click on another folder\'s "+" while the first create is in flight still creates', async () => {
    mocks.createChatSlot.mockReturnValue(deferred<unknown>().promise)
    renderSidebar()
    fireEvent.click(plusButton())
    fireEvent.click(screen.getByTestId(`folder-new-chat-${FOLDER_B}`))
    fireEvent.click(screen.getByTestId(`folder-new-chat-${FOLDER_B}`))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(2))
    // folder_id is createChatSlot's 8th positional argument.
    expect(mocks.createChatSlot.mock.calls.map(c => c[7])).toEqual([FOLDER_ID, FOLDER_B])
  })

  it('New incognito and New temporary chat on the same folder still create while its "+" create is in flight', async () => {
    // The folder menu rows call the same sink with a memoryMode. Driven at
    // the hook, because the Radix folder menu does not render its items in
    // this file's sidebar harness.
    mocks.createChatSlot.mockReturnValue(deferred<unknown>().promise)
    const store = createTestStore({ dashboard: { connected: true } as unknown as RootState['dashboard'], chat: { activeSlot: ORIGIN } as unknown as RootState['chat'] })
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}><Provider store={store}>{children}</Provider></QueryClientProvider>
    )
    const { result } = renderHook(() => useFolderChatCreate({
      folders: FOLDERS, mode: undefined, dispatch: store.dispatch, connected: true, setFolderOfflineNotice: vi.fn(),
      dropSlotMutation: { mutate: vi.fn() } as never, updateFolderMutation: { mutate: vi.fn() } as never,
      onOpenSlotInNewTab: undefined, onSelectSlot: vi.fn(), clearBoardCollapse: vi.fn(),
    }), { wrapper })
    act(() => { result.current.createChatInFolder(FOLDER_ID) })
    act(() => { result.current.createChatInFolder(FOLDER_ID, { memoryMode: 'incognito' }) })
    act(() => { result.current.createChatInFolder(FOLDER_ID, { memoryMode: 'temporary' }) })
    // Repeats of the plain and the incognito create are still dropped.
    act(() => { result.current.createChatInFolder(FOLDER_ID) })
    act(() => { result.current.createChatInFolder(FOLDER_ID, { memoryMode: 'incognito' }) })
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(3))
    // memory_mode is createChatSlot's 5th and folder_id its 8th positional argument.
    // The plain create sends "persistent" and may reach the API after the
    // other two, so compare without order.
    expect(mocks.createChatSlot.mock.calls.map(c => `${c[4]}@${c[7]}`).sort()).toEqual([
      `incognito@${FOLDER_ID}`, `persistent@${FOLDER_ID}`, `temporary@${FOLDER_ID}`,
    ])
  })

  it('Ctrl-clicks are tab gestures and each one still creates', async () => {
    const pending = deferred<unknown>()
    mocks.createChatSlot.mockReturnValue(pending.promise)
    renderSidebar()
    fireEvent.click(plusButton(), { ctrlKey: true })
    fireEvent.click(plusButton(), { ctrlKey: true })
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(2))
  })
})

describe('session row "Fork chat" ignores repeat clicks while its fork is in flight', () => {
  it('a double click makes one fork', async () => {
    const pending = deferred<unknown>()
    mocks.forkChatSlot.mockReturnValue(pending.promise)
    renderSidebar()
    fireEvent.click(forkButton(ORIGIN))
    fireEvent.click(forkButton(ORIGIN))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledTimes(1))
    await act(async () => { pending.resolve({ ok: true, key: 'chat-forked-1' }) })
    expect(mocks.forkChatSlot).toHaveBeenCalledTimes(1)
  })

  it('forking another session meanwhile still goes through, and each source forks again once settled', async () => {
    const a = deferred<unknown>()
    const b = deferred<unknown>()
    mocks.forkChatSlot
      .mockReturnValueOnce(a.promise)
      .mockReturnValueOnce(b.promise)
      .mockResolvedValue({ ok: false })
    renderSidebar()
    fireEvent.click(forkButton(ORIGIN))
    fireEvent.click(forkButton(OTHER))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledTimes(2))
    expect(mocks.forkChatSlot.mock.calls.map(c => c[0])).toEqual([ORIGIN, OTHER])
    // Settle the older call first: its source must be released even though a
    // newer fork was started after it.
    await act(async () => { a.resolve({ ok: false }) })
    await act(async () => { b.resolve({ ok: false }) })
    fireEvent.click(forkButton(ORIGIN))
    fireEvent.click(forkButton(OTHER))
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledTimes(4))
  })
})
