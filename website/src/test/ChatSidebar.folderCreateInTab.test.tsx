/**
 * ChatSidebar -> list-view folder create entries' "open as a tab" gestures (#10575).
 *
 * #10574 gave the header New button the session rows' gesture contract; these
 * pin the same contract on the list-view folder create surfaces it deferred:
 * the folder header "+" (`folder-new-chat-<id>`) and the empty-folder
 * "New chat in <name>" row (`folder-empty-new-chat-<id>`).
 *
 *   (1) a plain click still creates WITH activation and opens no tab;
 *   (2) Ctrl-click (non-mac) creates with `activate: false` -- the store's
 *       active slot must NOT move -- and hands the new key to
 *       `onOpenSlotInNewTab` in background mode;
 *   (3) middle-click does the same;
 *   (4) the platform split holds: Cmd on mac opens a tab, Ctrl on mac does not
 *       (Ctrl+click IS a right-click there);
 *   (5) without an `onOpenSlotInNewTab` (embedded hosts have no tab strip) the
 *       modifier is ignored and the click is an ordinary activating create.
 *
 * The real `createSlot` thunk runs against a mocked `api.createChatSlot`, so
 * the assertions read the thunk's ACTUAL `activate` effect off the store rather
 * than trusting the argument the sidebar passed (same as
 * ChatSidebar.newChatInTab.test).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot, ChatFolder } from '../types'

// IS_MAC is frozen at module load, so the platform is a mutable box read
// through a getter -- the same shape ChatSidebar.newChatInTab.test uses.
const platform = vi.hoisted(() => ({ mac: false }))
vi.mock('../hooks/useKeyboardShortcuts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useKeyboardShortcuts')>()
  return { ...actual, get IS_MAC() { return platform.mac } }
})

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
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn() }))
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
const FOLDER_ID = 'folder-alpha'
const SLOTS = [{ key: ORIGIN, title: 'Session 1', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' } as ChatSlot]
// Expanded and empty: the header "+" AND the empty-folder row both render.
const FOLDERS: ChatFolder[] = [{ id: FOLDER_ID, name: 'Alpha', order: 0, collapsed: false }]

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
  qc.setQueryData(['chat-folders'], FOLDERS)
  render(
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
  return store
}

function plusButton(): HTMLElement {
  return screen.getByTestId(`folder-new-chat-${FOLDER_ID}`)
}

function emptyRow(): HTMLElement {
  return screen.getByTestId(`folder-empty-new-chat-${FOLDER_ID}`)
}

/** A middle press produces `mousedown` (the autoscroll cancellation point --
 *  the onMouseDownCapture/onMouseDown handlers must preventDefault it) and
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

describe('ChatSidebar - list-view folder create open-as-tab gestures', () => {
  beforeEach(() => {
    localStorage.clear()
    platform.mac = false
    mocks.createChatSlot.mockReset().mockResolvedValue({ key: NEW_KEY, folder_id: FOLDER_ID })
  })

  it('plain click on the folder "+" creates, ACTIVATES the new session and opens no tab', async () => {
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    fireEvent.click(plusButton())
    await created()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('Ctrl-click on the folder "+" creates WITHOUT activating and opens a background tab', async () => {
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    fireEvent.click(plusButton(), { ctrlKey: true })
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    // The whole point: the user is still on the session they were reading.
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('middle-click on the folder "+" creates WITHOUT activating and opens a background tab', async () => {
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    // The mousedown must be prevented too -- that is the middle-press
    // autoscroll cancellation, a separate handler from the auxclick create.
    expect(middleClick(plusButton())).toBe(true)
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('on mac, Cmd-click on the folder "+" opens a background tab', async () => {
    platform.mac = true
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    fireEvent.click(plusButton(), { metaKey: true })
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('on mac, Ctrl-click on the folder "+" is NOT the tab gesture: plain activating create', async () => {
    platform.mac = true
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    fireEvent.click(plusButton(), { ctrlKey: true })
    await created()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('without a tab strip (no onOpenSlotInNewTab) a Ctrl-click on the "+" is an ordinary activating create', async () => {
    const store = renderSidebar(undefined)
    fireEvent.click(plusButton(), { ctrlKey: true })
    await created()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
  })

  it('Ctrl-click on the empty-folder "New chat in <name>" row opens a background tab', async () => {
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    fireEvent.click(emptyRow(), { ctrlKey: true })
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('middle-click on the empty-folder row opens a background tab', async () => {
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    expect(middleClick(emptyRow())).toBe(true)
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('plain click on the empty-folder row creates and ACTIVATES', async () => {
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    fireEvent.click(emptyRow())
    await created()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    expect(onOpen).not.toHaveBeenCalled()
  })
})
