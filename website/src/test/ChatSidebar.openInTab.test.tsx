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
import { render, screen, fireEvent, within, cleanup } from '@testing-library/react'
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

function renderSidebar(opts: { onOpenSlotInNewTab?: (key: string) => void; onSelectSlot?: (key: string) => void; connected?: boolean } = {}) {
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
              onSelectSlot={opts.onSelectSlot}
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
    localStorage.removeItem('mc-chat-config')
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
    expect(switchSlotMock).toHaveBeenCalledWith({ key: 's2', announceOnMissing: true, gesture: true })
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
    expect(switchSlotMock).toHaveBeenCalledWith({ key: 's2', announceOnMissing: true, gesture: true })
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
    expect(switchSlotMock).toHaveBeenCalledWith({ key: 's2', announceOnMissing: true, gesture: true })
    expect(onOpenSlotInNewTab).not.toHaveBeenCalled()
  })

  it('leaves every gesture unbound on a surface with no tab strip', () => {
    // The embed sessions list and a popped-out window pass no callback: a
    // middle-click that quietly navigated would be indistinguishable from a misfire.
    renderSidebar({})
    auxClick(row('Session 2'), 1)
    expect(switchSlotMock).not.toHaveBeenCalled()
    fireEvent.click(row('Session 2'), { ctrlKey: true })
    expect(switchSlotMock).toHaveBeenCalledWith({ key: 's2', announceOnMissing: true, gesture: true })
  })
})

/** Store the opt-in "Switch to Session Tabs on Cmd/Ctrl-Click" chat setting the way Settings does. */
function setSessionRowTabActive(value: unknown) {
  localStorage.setItem('mc-chat-config', JSON.stringify({ sessionRowTabActive: value }))
}

/** Render, fire one gesture on Session 2, and record every callback it reached. */
function outcome(fire: (el: HTMLElement) => void, opts: { mac: boolean; setting: unknown }) {
  macPlatform = opts.mac
  if (opts.setting === undefined) localStorage.removeItem('mc-chat-config')
  else setSessionRowTabActive(opts.setting)
  switchSlotMock.mockClear()
  const onOpenSlotInNewTab = vi.fn()
  const onSelectSlot = vi.fn()
  renderSidebar({ onOpenSlotInNewTab, onSelectSlot })
  fire(row('Session 2'))
  const seen = {
    open: onOpenSlotInNewTab.mock.calls,
    select: onSelectSlot.mock.calls,
    switch: switchSlotMock.mock.calls.slice(),
  }
  cleanup()
  return seen
}

/**
 * The opt-in "Switch to Session Tabs on Cmd/Ctrl-Click" chat setting (`sessionRowTabActive`).
 * Off, the default, every gesture does what main does and what the cases above
 * pin: Cmd/Ctrl-click queues a background tab, the sidebar-wide rule in
 * docs/decisions/2026-10-17-sidebar-create-controls-shift-opens-the-active-tab.md.
 * On, only the tab modifier changes: it opens the active tab and reports the
 * row, so the host leaves Split View the way a plain click does.
 */
describe('ChatSidebar – "Switch to Session Tabs on Cmd/Ctrl-Click" setting', () => {
  beforeEach(() => {
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    localStorage.removeItem('mc-chat-config')
    switchSlotMock.mockClear()
    macPlatform = false
  })

  const TAB_MODIFIER = [
    { name: 'Ctrl+click off macOS', mac: false, fire: (el: HTMLElement) => fireEvent.click(el, { ctrlKey: true }) },
    { name: 'Cmd+click on macOS', mac: true, fire: (el: HTMLElement) => fireEvent.click(el, { metaKey: true }) },
  ]
  const OTHER_GESTURES = [
    { name: 'Ctrl+click on macOS', mac: true, fire: (el: HTMLElement) => fireEvent.click(el, { ctrlKey: true }) },
    { name: 'Cmd+click off macOS', mac: false, fire: (el: HTMLElement) => fireEvent.click(el, { metaKey: true }) },
    { name: 'Ctrl+Shift+click', mac: false, fire: (el: HTMLElement) => fireEvent.click(el, { ctrlKey: true, shiftKey: true }) },
    { name: 'Ctrl+Alt+click', mac: false, fire: (el: HTMLElement) => fireEvent.click(el, { ctrlKey: true, altKey: true }) },
    { name: 'Cmd+Shift+click on macOS', mac: true, fire: (el: HTMLElement) => fireEvent.click(el, { metaKey: true, shiftKey: true }) },
    { name: 'middle-click', mac: false, fire: (el: HTMLElement) => auxClick(el, 1) },
    { name: 'plain click', mac: false, fire: (el: HTMLElement) => fireEvent.click(el) },
  ]

  for (const g of TAB_MODIFIER) {
    it(`off: ${g.name} queues a BACKGROUND tab, as on main`, () => {
      for (const setting of [undefined, false]) {
        const seen = outcome(g.fire, { mac: g.mac, setting })
        expect(seen.open).toEqual([['s2', { background: true }]])
        expect(seen.select).toEqual([])
        expect(seen.switch).toEqual([])
      }
    })

    it(`on: ${g.name} opens the ACTIVE tab and reports the row`, () => {
      const seen = outcome(g.fire, { mac: g.mac, setting: true })
      // No background flag, so the host switches to the new tab.
      expect(seen.open).toEqual([['s2']])
      expect(seen.select).toEqual([['s2']])
      expect(seen.switch).toEqual([])
    })
  }

  for (const g of OTHER_GESTURES) {
    it(`${g.name} does the same with the setting on as off`, () => {
      const off = outcome(g.fire, { mac: g.mac, setting: false })
      const on = outcome(g.fire, { mac: g.mac, setting: true })
      expect(on).toEqual(off)
    })
  }

  it('middle-click stays a BACKGROUND open with the setting on', () => {
    const seen = outcome(el => auxClick(el, 1), { mac: false, setting: true })
    expect(seen.open).toEqual([['s2', { background: true }]])
    expect(seen.select).toEqual([])
  })

  it('reads a stored non-boolean as off', () => {
    const seen = outcome(el => fireEvent.click(el, { ctrlKey: true }), { mac: false, setting: 'true' })
    expect(seen.open).toEqual([['s2', { background: true }]])
    expect(seen.select).toEqual([])
  })

  it('applies a change in Settings on the next click, without a reload', () => {
    const onOpenSlotInNewTab = vi.fn()
    renderSidebar({ onOpenSlotInNewTab, onSelectSlot: vi.fn() })
    fireEvent.click(row('Session 2'), { ctrlKey: true })
    setSessionRowTabActive(true)
    fireEvent.click(row('Session 2'), { ctrlKey: true })
    expect(onOpenSlotInNewTab.mock.calls).toEqual([['s2', { background: true }], ['s2']])
  })
})

/**
 * The row menu's "Open in a session tab" is a foreground open whatever the
 * setting says. It reports the row so the host leaves Split View; before, it
 * switched the active session behind the grid and nothing visibly changed.
 */
describe('ChatSidebar – row menu open-in-tab', () => {
  beforeEach(() => {
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    localStorage.removeItem('mc-chat-config')
    switchSlotMock.mockClear()
    macPlatform = false
  })

  async function pickMenuItem() {
    fireEvent.pointerDown(within(row('Session 2')).getByRole('button', { name: 'More options' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: /session tab/i }))
  }

  it('opens in front and reports the selection', async () => {
    const onOpenSlotInNewTab = vi.fn()
    const onSelectSlot = vi.fn()
    renderSidebar({ onOpenSlotInNewTab, onSelectSlot })
    await pickMenuItem()
    expect(onOpenSlotInNewTab.mock.calls).toEqual([['s2']])
    expect(onSelectSlot).toHaveBeenCalledWith('s2')
  })

  it('reports no selection while offline, where the host cannot switch', async () => {
    const onOpenSlotInNewTab = vi.fn()
    const onSelectSlot = vi.fn()
    renderSidebar({ onOpenSlotInNewTab, onSelectSlot, connected: false })
    await pickMenuItem()
    expect(onOpenSlotInNewTab.mock.calls).toEqual([['s2']])
    expect(onSelectSlot).not.toHaveBeenCalled()
  })
})
