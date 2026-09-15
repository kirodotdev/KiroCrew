/**
 * ChatSidebar → the New button's "open as a tab" gestures.
 *
 * Session rows honour Cmd/Ctrl-click and middle-click as "open this session as
 * a BACKGROUND tab" (ChatSidebar.openInTab.test). The New button did not: it
 * read no modifier, so a Ctrl-click created and ACTIVATED the session, and the
 * tab strip's invariant then swapped the tab the user was on for the new one —
 * the opposite of what the gesture asks. These pin the fixed contract:
 *
 *   (1) a plain click still creates WITH activation and opens no tab;
 *   (2) Ctrl-click (non-mac) creates with `activate: false` — the store's
 *       active slot must NOT move — and hands the new key to
 *       `onOpenSlotInNewTab` in background mode;
 *   (3) middle-click does the same;
 *   (4) the platform split holds: Cmd on mac opens a tab, Ctrl on mac does not
 *       (Ctrl+click IS a right-click there);
 *   (5) without an `onOpenSlotInNewTab` (embedded hosts have no tab strip) the
 *       modifier is ignored and the click is an ordinary activating create.
 *
 * The real `createSlot` thunk runs against a mocked `api.createChatSlot`, so
 * the assertions read the thunk's ACTUAL `activate` effect off the store rather
 * than trusting the argument the sidebar passed.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

// IS_MAC is frozen at module load, so the platform is a mutable box read
// through a getter — the same shape ChatSidebar.openInTab.test uses.
const platform = vi.hoisted(() => ({ mac: false }))
vi.mock('../hooks/useKeyboardShortcuts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useKeyboardShortcuts')>()
  return { ...actual, get IS_MAC() { return platform.mac } }
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
const SLOTS = [{ key: ORIGIN, title: 'Session 1', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' } as ChatSlot]

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
  qc.setQueryData(['chat-folders'], [])
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

function newButton(): HTMLElement {
  return screen.getByRole('button', { name: /new chat session/i })
}

/** A middle press produces `auxclick`, not `click`. */
function middleClick(el: HTMLElement) {
  fireEvent(el, new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 }))
}

async function created() {
  await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
}

describe('ChatSidebar – New button open-as-tab gestures', () => {
  beforeEach(() => {
    localStorage.clear()
    platform.mac = false
    mocks.createChatSlot.mockReset().mockResolvedValue({ key: NEW_KEY })
  })

  it('plain click creates, ACTIVATES the new session and opens no tab', async () => {
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    fireEvent.click(newButton())
    await created()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('Ctrl-click creates WITHOUT activating and opens the new session as a background tab', async () => {
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    fireEvent.click(newButton(), { ctrlKey: true })
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    // The whole point: the user is still on the session they were reading.
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('middle-click creates WITHOUT activating and opens a background tab', async () => {
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    middleClick(newButton())
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('on mac, Cmd-click opens a background tab', async () => {
    platform.mac = true
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    fireEvent.click(newButton(), { metaKey: true })
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('on mac, Ctrl-click is NOT the tab gesture (it is a right-click there): plain activating create', async () => {
    platform.mac = true
    const onOpen = vi.fn()
    const store = renderSidebar(onOpen)
    fireEvent.click(newButton(), { ctrlKey: true })
    await created()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('without a tab strip (no onOpenSlotInNewTab) a Ctrl-click is an ordinary activating create', async () => {
    const store = renderSidebar(undefined)
    fireEvent.click(newButton(), { ctrlKey: true })
    await created()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
  })
})
