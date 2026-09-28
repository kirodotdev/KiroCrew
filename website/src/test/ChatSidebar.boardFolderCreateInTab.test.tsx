/**
 * ChatSidebar -> board-view folder create entries' "open as a tab" gestures (#10575).
 *
 * Board-view twins of ChatSidebar.folderCreateInTab.test: the column folder
 * header "+" (`col-<col>-folder-<id>-new-chat`) and the column empty-folder row
 * (`col-<col>-folder-<id>-empty-new-chat`) honour the same contract as the
 * header New button -- plain click creates and activates; Ctrl/Cmd-click and
 * middle-click create with `activate: false` and open a background tab.
 *
 * Board-specific invariant also pinned here: the column drop
 * (`dropSlotToColumn`) still runs for the tab gesture -- column membership is
 * independent of which slot has focus.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot, ChatTag, TagColumn, ChatFolder } from '../types'

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
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false, defaultAutopilot: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  createChatSlot: vi.fn(),
  dropSlotToColumn: vi.fn(),
  updateChatFolder: vi.fn(),
}))
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

const ORIGIN = 's1'
const NEW_KEY = 'chat-new-1'
const FOLDER_ID = 'folder-board'
const BLOCKED = '11111111-1111-1111-1111-111111111111'
const COL_A = 'col-aaaa'
const SLOTS = [{ key: ORIGIN, title: 'Session 1', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' } as ChatSlot]
const tags: ChatTag[] = [{ id: BLOCKED, name: 'Blocked', color: '#e11', order: 0, status: true }]
const columns: TagColumn[] = [{ id: COL_A, name: 'Planned/Blocked', tag_ids: [BLOCKED], mode: 'any', order: 0 }]
// Expanded and empty: the column "+" AND the column empty-folder row both render.
const folders: ChatFolder[] = [{ id: FOLDER_ID, name: 'Board', order: 0, collapsed: false }]

function renderSidebar(onOpenSlotInNewTab?: (key: string, opts?: { background?: boolean }) => void) {
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
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folders)
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={ORIGIN} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="kirocrew" installedAgents={[]}
              onOpenSlotInNewTab={onOpenSlotInNewTab}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store }
}

function colPlus(container: HTMLElement): HTMLElement {
  return container.querySelector(`[data-testid="col-${COL_A}-folder-${FOLDER_ID}-new-chat"]`) as HTMLElement
}

function colEmptyRow(container: HTMLElement): HTMLElement {
  return container.querySelector(`[data-testid="col-${COL_A}-folder-${FOLDER_ID}-empty-new-chat"]`) as HTMLElement
}

/** A middle press produces `mousedown` (the autoscroll cancellation point --
 *  the onMouseDown/onMouseDownCapture handlers must preventDefault it) and
 *  then `auxclick`. Returns whether the mousedown WAS prevented, so gesture
 *  tests can pin the autoscroll fix rather than only the auxclick create. */
function middleClick(el: HTMLElement): boolean {
  const notPrevented = fireEvent(el, new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 1 }))
  fireEvent(el, new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 }))
  return !notPrevented
}

async function created() {
  await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
}

describe('ChatSidebar - board-view folder create open-as-tab gestures', () => {
  beforeEach(() => {
    localStorage.clear()
    mocks.createChatSlot.mockReset().mockResolvedValue({ key: NEW_KEY, folder_id: FOLDER_ID })
    mocks.dropSlotToColumn.mockReset().mockResolvedValue({ ok: true })
    mocks.updateChatFolder.mockReset().mockResolvedValue({ ok: true })
  })
  afterEach(() => vi.clearAllMocks())

  it('plain click on the column folder "+" creates, ACTIVATES and opens no tab', async () => {
    const onOpen = vi.fn()
    const { container, store } = renderSidebar(onOpen)
    expect(colPlus(container)).toBeTruthy()
    fireEvent.click(colPlus(container))
    await created()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('Ctrl-click on the column folder "+" creates WITHOUT activating, opens a background tab, and still drops into the column', async () => {
    const onOpen = vi.fn()
    const { container, store } = renderSidebar(onOpen)
    fireEvent.click(colPlus(container), { ctrlKey: true })
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    // The whole point: the user is still on the session they were reading.
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
    // Column membership is independent of focus: the drop still runs.
    await waitFor(() => expect(mocks.dropSlotToColumn).toHaveBeenCalledWith(NEW_KEY, COL_A))
  })

  it('middle-click on the column folder "+" creates WITHOUT activating and opens a background tab', async () => {
    const onOpen = vi.fn()
    const { container, store } = renderSidebar(onOpen)
    // The mousedown must be prevented too -- the middle-press autoscroll
    // cancellation, a separate handler from the auxclick create.
    expect(middleClick(colPlus(container))).toBe(true)
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('middle-click on the column empty-folder row creates WITHOUT activating and opens a background tab', async () => {
    const onOpen = vi.fn()
    const { container, store } = renderSidebar(onOpen)
    expect(middleClick(colEmptyRow(container))).toBe(true)
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('Ctrl-click on the column empty-folder row opens a background tab', async () => {
    const onOpen = vi.fn()
    const { container, store } = renderSidebar(onOpen)
    expect(colEmptyRow(container)).toBeTruthy()
    fireEvent.click(colEmptyRow(container), { ctrlKey: true })
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('plain click on the column empty-folder row creates and ACTIVATES', async () => {
    const onOpen = vi.fn()
    const { container, store } = renderSidebar(onOpen)
    fireEvent.click(colEmptyRow(container))
    await created()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('without a tab strip (no onOpenSlotInNewTab) a Ctrl-click on the column "+" is an ordinary activating create', async () => {
    const { container, store } = renderSidebar(undefined)
    fireEvent.click(colPlus(container), { ctrlKey: true })
    await created()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
  })
})
