/**
 * ChatSidebar → a plain click on the open chat's row shows or hides the side
 * panel, through the `toggle-activity-panel` window event the `side-panel`
 * shortcut dispatches. The listener below stands in for ChatPage's
 * (pages/chat/page/messageActions.ts), which dispatches `toggleActivity()`.
 *
 * Each case pairs the toggle with what must NOT happen: a click on another row
 * still only switches, and a double-click on the title still only renames.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { clearSwitchSlotGone, toggleActivity } from '../store/chatSlice'
import { RECLICK_PANEL_DELAY_MS, cancelReclickPanelToggle } from '../lib/reclickPanelToggle'

const { switchSlotMock } = vi.hoisted(() => ({
  switchSlotMock: vi.fn(() => ({ type: 'chat/switchSlot/pending', meta: {} })),
}))

vi.mock('../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/chatSlice')>()
  return { ...actual, switchSlot: (...args: unknown[]) => switchSlotMock(...args) }
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

function renderSidebar(onOpenSlotInNewTab?: (key: string) => void, splitMode = false, slotOpenFailed: string | null = null) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'linux' },
      connected: true,
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
      slotMessages: {}, slotLoading: false, slotOpenFailed,
    } as unknown as RootState['chat'],
  })
  const onToggle = () => store.dispatch(toggleActivity())
  window.addEventListener('toggle-activity-panel', onToggle)
  cleanups.push(() => window.removeEventListener('toggle-activity-panel', onToggle))
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
              onOpenSlotInNewTab={onOpenSlotInNewTab}
              splitMode={splitMode}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

const cleanups: Array<() => void> = []

function row(title: string): HTMLElement {
  const wrapper = screen.getByText(title).closest('[data-slot-key]') as HTMLElement
  return wrapper.querySelector('.session-row') as HTMLElement
}

const settle = () => act(() => { vi.advanceTimersByTime(RECLICK_PANEL_DELAY_MS) })

describe('ChatSidebar – re-clicking the open chat toggles the side panel', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    switchSlotMock.mockClear()
  })
  afterEach(() => {
    while (cleanups.length) cleanups.pop()!()
    vi.useRealTimers()
  })

  it('opens the closed panel, without switching again', () => {
    const store = renderSidebar()
    fireEvent.click(row('Session 1'), { detail: 1 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('closes the open panel on the next click', () => {
    const store = renderSidebar()
    fireEvent.click(row('Session 1'), { detail: 1 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(true)
    fireEvent.click(row('Session 1'), { detail: 1 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('a click on another chat only switches', () => {
    const store = renderSidebar()
    fireEvent.click(row('Session 2'), { detail: 1 })
    settle()
    expect(switchSlotMock).toHaveBeenCalledWith({ key: 's2', announceOnMissing: true, gesture: true })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('a click on another chat cancels a toggle still waiting', () => {
    const store = renderSidebar()
    fireEvent.click(row('Session 1'), { detail: 1 })
    fireEvent.click(row('Session 2'), { detail: 1 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('a double-click on the title renames and leaves the panel alone', () => {
    const store = renderSidebar()
    const title = row('Session 1').querySelector('[data-session-title]') as HTMLElement
    fireEvent.click(title, { detail: 1 })
    fireEvent.click(title, { detail: 2 })
    fireEvent.doubleClick(title, { detail: 2 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(screen.getByDisplayValue('Session 1')).toBeInTheDocument()
  })

  it('a slow double-click on the title takes back the toggle its first click made', () => {
    const store = renderSidebar()
    const title = row('Session 1').querySelector('[data-session-title]') as HTMLElement
    fireEvent.click(title, { detail: 1 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(true)
    fireEvent.click(title, { detail: 2 })
    fireEvent.doubleClick(title, { detail: 2 })
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(screen.getByDisplayValue('Session 1')).toBeInTheDocument()
  })

  it('takes it back however slow the platform lets a double-click be (800 ms apart)', () => {
    const store = renderSidebar()
    const title = row('Session 1').querySelector('[data-session-title]') as HTMLElement
    fireEvent.click(title, { detail: 1 })
    act(() => { vi.advanceTimersByTime(800) })
    expect(store.getState().chat.activityOpen).toBe(true)
    fireEvent.click(title, { detail: 2 })
    fireEvent.doubleClick(title, { detail: 2 })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('a double-click on a row control (Fork chat) leaves the toggle alone', () => {
    const store = renderSidebar()
    fireEvent.click(row('Session 1'), { detail: 1 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(true)
    // The button stops its own clicks; its dblclick still bubbles to the row.
    const dup = row('Session 1').querySelector('[data-ui-location="sessions.row-duplicate"]') as HTMLElement
    expect(dup).not.toBeNull()
    fireEvent.doubleClick(dup, { detail: 2 })
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('a later double-click does not undo an earlier, finished toggle', () => {
    const store = renderSidebar()
    fireEvent.click(row('Session 1'), { detail: 1 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(true)
    // A fresh gesture: its first click clears the earlier sequence's record.
    const title = row('Session 1').querySelector('[data-session-title]') as HTMLElement
    fireEvent.click(title, { detail: 1 })
    fireEvent.click(title, { detail: 2 })
    fireEvent.doubleClick(title, { detail: 2 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('a click inside the rename box leaves the panel alone', () => {
    const store = renderSidebar()
    const title = row('Session 1').querySelector('[data-session-title]') as HTMLElement
    fireEvent.doubleClick(title, { detail: 2 })
    fireEvent.click(screen.getByDisplayValue('Session 1'), { detail: 1 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('the page can drop a toggle still waiting (a switch made another way)', () => {
    const store = renderSidebar()
    fireEvent.click(row('Session 1'), { detail: 1 })
    cancelReclickPanelToggle()
    settle()
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('in Split View the open chat offers no tooltip and a click does not toggle', () => {
    const store = renderSidebar(undefined, true)
    expect(row('Session 1').title).toBe('')
    fireEvent.click(row('Session 1'), { detail: 1 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('after a failed open, a click on the open chat retries it and leaves the panel alone', () => {
    const store = renderSidebar(undefined, false, 's1')
    // Dismissing the failure notice must not turn the retry back into a toggle.
    act(() => { store.dispatch(clearSwitchSlotGone()) })
    expect(row('Session 1').title).toBe('')
    fireEvent.click(row('Session 1'), { detail: 1 })
    settle()
    expect(switchSlotMock).toHaveBeenCalledWith({ key: 's1', announceOnMissing: true, gesture: true })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('a failed open of another chat does not stop the open chat toggling', () => {
    const store = renderSidebar(undefined, false, 's2')
    fireEvent.click(row('Session 1'), { detail: 1 })
    settle()
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('a modifier-click on the open chat keeps its own meaning', () => {
    const onOpenSlotInNewTab = vi.fn()
    const store = renderSidebar(onOpenSlotInNewTab)
    fireEvent.click(row('Session 1'), { detail: 1, ctrlKey: true })
    settle()
    expect(onOpenSlotInNewTab).toHaveBeenCalledWith('s1', { background: true })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('names the action and the shortcut on the open chat only', () => {
    renderSidebar()
    expect(row('Session 1').title).toBe('Click again to show or hide the side panel (Ctrl+\\)')
    expect(row('Session 2').title).toBe('')
  })
})
