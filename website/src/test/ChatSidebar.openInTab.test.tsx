/**
 * ChatSidebar → "open this session as a tab" gestures (#4477).
 *
 * Three gestures reach one callback: middle-click, modifier-click, and the row
 * menu item. Each assertion here is paired with its NEGATIVE — the gesture must
 * open a tab INSTEAD of switching, and a plain click must still switch — because
 * a handler that did both would look correct in the UI (the session does come up)
 * while silently replacing the tab the user meant to keep.
 *
 * The platform split is the other reason this file exists: Ctrl+click IS a
 * right-click on macOS, so honouring it there would fire this gesture and the
 * context menu from one press. The two "wrong platform" cases pin that.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const { switchSlotMock } = vi.hoisted(() => ({
  switchSlotMock: vi.fn(() => ({ type: 'chat/switchSlot/pending', meta: {} })),
}))

vi.mock('../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/chatSlice')>()
  return { ...actual, switchSlot: (...args: unknown[]) => switchSlotMock(...args) }
})

// IS_MAC is frozen at module load from navigator.platform, so the platform a
// test wants has to be chosen per module instance — hence the per-case
// resetModules + doMock below rather than one shared import.
let macPlatform = false
vi.mock('../hooks/useKeyboardShortcuts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useKeyboardShortcuts')>()
  return { ...actual, get IS_MAC() { return macPlatform } }
})

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: Object.fromEntries(
      [
        'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
        'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
        'renameSlot', 'forkSession', 'chatFolders',
      ].map(k => [k, vi.fn().mockResolvedValue([])]),
    ),
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar from '../pages/ChatSidebar'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const slot = (key: string, title: string): ChatSlot => ({
  key, title, messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
} as ChatSlot)

const SLOTS = [slot('s1', 'Session 1'), slot('s2', 'Session 2')]

function renderSidebar(opts: { onOpenSlotInNewTab?: (key: string) => void; connected?: boolean } = {}) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'linux' },
      connected: opts.connected ?? true,
      slots: SLOTS,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 's1',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS}
              activeSlot="s1"
              unreadSlots={[]}
              history={[]}
              historyHasMore={false}
              defaultAgent="default"
              installedAgents={[]}
              onOpenSlotInNewTab={opts.onOpenSlotInNewTab}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

function row(title: string): HTMLElement {
  const wrapper = screen.getByText(title).closest('[data-slot-key]') as HTMLElement
  return wrapper.querySelector('.session-row') as HTMLElement
}

/**
 * A middle press produces `auxclick`, not `click`, and testing-library has no
 * shorthand for it — so dispatch the real event React's onAuxClick listens for.
 */
function auxClick(el: HTMLElement, button: number) {
  fireEvent(el, new MouseEvent('auxclick', { bubbles: true, cancelable: true, button }))
}

describe('ChatSidebar – open-as-tab gestures', () => {
  beforeEach(() => {
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    switchSlotMock.mockClear()
    macPlatform = false
  })

  it('middle-click opens a BACKGROUND tab instead of switching', () => {
    // Background is the browser/editor meaning of the gesture: triaging three
    // rows in a row must not yank the user to each one.
    const onOpenSlotInNewTab = vi.fn()
    renderSidebar({ onOpenSlotInNewTab })
    auxClick(row('Session 2'), 1)
    expect(onOpenSlotInNewTab).toHaveBeenCalledWith('s2', { background: true })
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('cancels the middle-button MOUSEDOWN, where Windows autoscroll starts', () => {
    // Chrome/Edge on Windows enter autoscroll on mousedown, before auxclick
    // fires, so cancelling only in the auxclick handler leaves the pointer in
    // autoscroll mode on a scrollable sidebar.
    renderSidebar({ onOpenSlotInNewTab: vi.fn() })
    const ev = new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 1 })
    fireEvent(row('Session 2'), ev)
    expect(ev.defaultPrevented).toBe(true)
  })

  it('leaves a PRIMARY-button mousedown alone for dnd-kit', () => {
    renderSidebar({ onOpenSlotInNewTab: vi.fn() })
    const ev = new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0 })
    fireEvent(row('Session 2'), ev)
    expect(ev.defaultPrevented).toBe(false)
  })

  it('ignores a right-button aux click', () => {
    const onOpenSlotInNewTab = vi.fn()
    renderSidebar({ onOpenSlotInNewTab })
    auxClick(row('Session 2'), 2)
    expect(onOpenSlotInNewTab).not.toHaveBeenCalled()
  })

  it('does not open a tab while the gateway is offline', () => {
    // Same reasoning as the plain-click offline guard: the switch would hang and
    // the user would be left looking at the previous transcript.
    const onOpenSlotInNewTab = vi.fn()
    renderSidebar({ onOpenSlotInNewTab, connected: false })
    auxClick(row('Session 2'), 1)
    expect(onOpenSlotInNewTab).not.toHaveBeenCalled()
  })

  it('Ctrl+click opens a tab off macOS', () => {
    const onOpenSlotInNewTab = vi.fn()
    renderSidebar({ onOpenSlotInNewTab })
    fireEvent.click(row('Session 2'), { ctrlKey: true })
    expect(onOpenSlotInNewTab).toHaveBeenCalledWith('s2', { background: true })
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('IGNORES Ctrl+click on macOS, where it is a right-click', () => {
    macPlatform = true
    const onOpenSlotInNewTab = vi.fn()
    renderSidebar({ onOpenSlotInNewTab })
    fireEvent.click(row('Session 2'), { ctrlKey: true })
    expect(onOpenSlotInNewTab).not.toHaveBeenCalled()
    expect(switchSlotMock).toHaveBeenCalledWith('s2')
  })

  it('Cmd+click opens a tab on macOS only', () => {
    macPlatform = true
    const onOpenSlotInNewTab = vi.fn()
    renderSidebar({ onOpenSlotInNewTab })
    fireEvent.click(row('Session 2'), { metaKey: true })
    expect(onOpenSlotInNewTab).toHaveBeenCalledWith('s2', { background: true })
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('leaves Cmd+click as a plain switch off macOS', () => {
    const onOpenSlotInNewTab = vi.fn()
    renderSidebar({ onOpenSlotInNewTab })
    fireEvent.click(row('Session 2'), { metaKey: true })
    expect(onOpenSlotInNewTab).not.toHaveBeenCalled()
    expect(switchSlotMock).toHaveBeenCalledWith('s2')
  })

  it('does not claim a modifier-click when Shift or Alt is also held', () => {
    // Those belong to other gestures (range select, window managers); claiming
    // them would make the tab open look random.
    const onOpenSlotInNewTab = vi.fn()
    renderSidebar({ onOpenSlotInNewTab })
    fireEvent.click(row('Session 2'), { ctrlKey: true, shiftKey: true })
    fireEvent.click(row('Session 2'), { ctrlKey: true, altKey: true })
    expect(onOpenSlotInNewTab).not.toHaveBeenCalled()
  })

  it('a plain click still switches — the unchanged path', () => {
    const onOpenSlotInNewTab = vi.fn()
    renderSidebar({ onOpenSlotInNewTab })
    fireEvent.click(row('Session 2'))
    expect(switchSlotMock).toHaveBeenCalledWith('s2')
    expect(onOpenSlotInNewTab).not.toHaveBeenCalled()
  })

  it('leaves every gesture unbound on a surface with no tab strip', () => {
    // The embed sessions list and a popped-out window pass no callback: a
    // middle-click that quietly navigated would be indistinguishable from a misfire.
    renderSidebar({})
    auxClick(row('Session 2'), 1)
    expect(switchSlotMock).not.toHaveBeenCalled()
    fireEvent.click(row('Session 2'), { ctrlKey: true })
    expect(switchSlotMock).toHaveBeenCalledWith('s2')
  })
})
