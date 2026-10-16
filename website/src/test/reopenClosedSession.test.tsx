import './mockApiClient'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { Provider } from 'react-redux'
import { api } from '../api/client'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { ShortcutDefRow, groupShortcuts } from '../components/ShortcutsModal'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import { useSessionActions } from '../hooks/useSessionActions'
import {
  MAX_RECENTLY_CLOSED, hasRecentlyClosedSession, isClosedSessionOpenIn, rememberClosedSession, resetRecentlyClosedSessions,
  takeRecentlyClosedSession,
} from '../lib/recentlyClosedSessions'
import { matchShortcutEvent, resolveShortcuts, shortcutEntry } from '../lib/shortcutRegistry'
import { renderHookWithProviders, createTestStore } from './helpers'
import { store as appStore } from '../store'
import dashboardReducer, { addSlotOptimistic } from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import type { RootState } from '../store'

const chatInitialState = chatReducer(undefined, { type: '@@INIT' })

/**
 * ⌘⇧T / Ctrl+Shift+T reopens the session closed most recently (#18969).
 *
 * Three things are pinned: the stack's order and filters (what is offered for
 * reopening), that only a close that LANDED is recorded (a refused close puts
 * the row back, so there is nothing to reopen), and that the chord reopens
 * through the History resume (`api.resumeChatSlot`), the path that restores
 * the transcript.
 */

const neverOpen = () => false

beforeEach(() => {
  localStorage.clear()
  resetRecentlyClosedSessions()
  vi.mocked(api.deleteChatSlot).mockReset()
  vi.mocked(api.resumeChatSlot).mockReset()
})

describe('recentlyClosedSessions', () => {
  it('reopens newest first, then walks further back', () => {
    rememberClosedSession({ key: 'a', history_key: 'dashboard_a', title: 'A' })
    rememberClosedSession({ key: 'b', history_key: 'dashboard_b', title: 'B' })
    expect(takeRecentlyClosedSession(neverOpen)).toEqual({ key: 'b', historyKey: 'dashboard_b', title: 'B' })
    expect(takeRecentlyClosedSession(neverOpen)).toEqual({ key: 'a', historyKey: 'dashboard_a', title: 'A' })
    expect(takeRecentlyClosedSession(neverOpen)).toBeNull()
  })

  it('skips and drops a session that is open again by its bare slot key', () => {
    rememberClosedSession({ key: 'a', history_key: 'dashboard_a', title: 'A' })
    rememberClosedSession({ key: 'b', history_key: 'dashboard_b', title: 'B' })
    const isOpen = vi.fn((ref: { key: string }) => ref.key === 'b')
    expect(takeRecentlyClosedSession(isOpen)).toEqual({ key: 'a', historyKey: 'dashboard_a', title: 'A' })
    expect(isOpen).toHaveBeenCalledWith({ key: 'b', historyKey: 'dashboard_b', title: 'B' })
    expect(takeRecentlyClosedSession(neverOpen)).toBeNull()
  })

  it('treats a session as open when a slot under ANOTHER key carries its history_key', () => {
    // A channel-linked `chat-2` reopened from History comes back published under
    // the folded transcript stem (`_normalize_slot_key(history_key)`), not under
    // `chat-2`. By bare key it would look closed and be resumed a second time.
    rememberClosedSession({ key: 'older', history_key: 'dashboard_older', title: 'Older' })
    rememberClosedSession({ key: 'chat-2', history_key: 'slack_C0ABC_1785370133.085469', title: 'Thread' })
    const isOpen = isClosedSessionOpenIn([
      { key: 'chat-1', history_key: 'dashboard_chat-1' },
      { key: 'slack_C0ABC_1785370133_085469', history_key: 'slack_C0ABC_1785370133.085469' },
    ])
    expect(hasRecentlyClosedSession(isOpen)).toBe(true)
    expect(takeRecentlyClosedSession(isOpen)).toEqual({ key: 'older', historyKey: 'dashboard_older', title: 'Older' })
    expect(takeRecentlyClosedSession(neverOpen)).toBeNull()
  })

  it('isClosedSessionOpenIn matches by slot key as well, and nothing on an unrelated tab', () => {
    const isOpen = isClosedSessionOpenIn([{ key: 'chat-1', history_key: 'dashboard_chat-1' }])
    expect(isOpen({ key: 'chat-1', historyKey: 'dashboard_chat-1', title: '' })).toBe(true)
    expect(isOpen({ key: 'chat-2', historyKey: 'dashboard_chat-2', title: '' })).toBe(false)
    // A slot the backend sent without history_key never matches an entry by stem.
    expect(isClosedSessionOpenIn([{ key: 'x' }])({ key: 'y', historyKey: 'dashboard_y', title: '' })).toBe(false)
  })

  it('moves a re-closed session to the top instead of listing it twice', () => {
    rememberClosedSession({ key: 'a', history_key: 'dashboard_a', title: 'A' })
    rememberClosedSession({ key: 'b', history_key: 'dashboard_b', title: 'B' })
    rememberClosedSession({ key: 'a', history_key: 'dashboard_a', title: 'A' })
    expect(takeRecentlyClosedSession(neverOpen)?.key).toBe('a')
    expect(takeRecentlyClosedSession(neverOpen)?.key).toBe('b')
    expect(takeRecentlyClosedSession(neverOpen)).toBeNull()
  })

  it('keeps only the newest MAX_RECENTLY_CLOSED closes', () => {
    for (let i = 0; i < MAX_RECENTLY_CLOSED + 5; i++) rememberClosedSession({ key: `s${i}`, history_key: `dashboard_s${i}`, title: `S${i}` })
    let n = 0
    while (takeRecentlyClosedSession(neverOpen)) n++
    expect(n).toBe(MAX_RECENTLY_CLOSED)
  })

  it('falls back to the key when the session has no title', () => {
    rememberClosedSession({ key: 'a', history_key: 'dashboard_a' })
    expect(takeRecentlyClosedSession(neverOpen)).toEqual({ key: 'a', historyKey: 'dashboard_a', title: 'a' })
  })

  it('does not record a slot without history_key, so no transcript is guessed', () => {
    rememberClosedSession({ key: 'chat-9', title: 'No stem' })
    expect(takeRecentlyClosedSession(neverOpen)).toBeNull()
  })

  it("resumes the backend's history_key verbatim, which covers an unbound channel slot", () => {
    // An unbound channel-born slot is not dashboard_-prefixed; only the
    // backend's slot_history_key knows its transcript.
    rememberClosedSession({ key: 'slack_1785370133.085469', history_key: 'slack_1785370133.085469', title: 'Unbound thread' })
    expect(takeRecentlyClosedSession(neverOpen)).toEqual({
      key: 'slack_1785370133.085469',
      historyKey: 'slack_1785370133.085469',
      title: 'Unbound thread',
    })
  })

  it('records incognito and temporary sessions, whose transcript History keeps', () => {
    rememberClosedSession({ key: 'i', history_key: 'dashboard_i', memory_mode: 'incognito' })
    rememberClosedSession({ key: 't', history_key: 'dashboard_t', memory_mode: 'temporary' })
    expect(takeRecentlyClosedSession(neverOpen)?.key).toBe('t')
    expect(takeRecentlyClosedSession(neverOpen)?.key).toBe('i')
  })

  it('never records a session the chat page cannot display', () => {
    rememberClosedSession({ key: 'x', history_key: 'dashboard_x', surface: 'some-app-surface' })
    rememberClosedSession({ key: 'legacy', history_key: 'dashboard_legacy', mode: 'orchestrator' })
    expect(takeRecentlyClosedSession(neverOpen)?.key).toBe('legacy')
    expect(takeRecentlyClosedSession(neverOpen)).toBeNull()
  })
})

describe('reopen-chat registry entry', () => {
  const ev = (code: string, mods: Partial<KeyboardEvent>) => ({
    code, key: code.replace(/^Key/, '').toLowerCase(), metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...mods,
  })

  it('matches ⌘⇧T on macOS, Ctrl+Shift+T elsewhere, and the Option/Alt+Shift+T alias on both', () => {
    const mac = resolveShortcuts({}, 'mac')
    const other = resolveShortcuts({}, 'other')
    expect(matchShortcutEvent(ev('KeyT', { metaKey: true, shiftKey: true }), mac, 'mac')).toBe('reopen-chat')
    expect(matchShortcutEvent(ev('KeyT', { altKey: true, shiftKey: true }), mac, 'mac')).toBe('reopen-chat')
    expect(matchShortcutEvent(ev('KeyT', { ctrlKey: true, shiftKey: true }), other, 'other')).toBe('reopen-chat')
    expect(matchShortcutEvent(ev('KeyT', { altKey: true, shiftKey: true }), other, 'other')).toBe('reopen-chat')
    // No shift: ⌘T / Ctrl+T is the browser's new tab, not this command.
    expect(matchShortcutEvent(ev('KeyT', { metaKey: true }), mac, 'mac')).toBeNull()
  })

  it('is an Actions command a browser never delivers, so the alias is advertised there', () => {
    const entry = shortcutEntry('reopen-chat')
    expect(entry?.group).toBe('actions')
    expect(entry?.browserReserved).toBe(true)
  })

  // jsdom lays nothing out, so the reference row's shape is pinned structurally:
  // below sm the full-width label and caps line stack; from sm up this is the
  // one row showing two full chords plus the "desktop app only" tag, and the
  // caps column wraps between combos while the label stays on one line.
  it('stacks its mobile chord line and wraps only between combos', () => {
    const def = groupShortcuts('actions', false).find(d => d.id === 'reopen-chat')!
    const { container } = render(<ShortcutDefRow def={def} />)
    const row = container.firstElementChild!
    const [label, caps] = Array.from(row.children)
    for (const c of ['flex-col', 'sm:flex-row']) expect(row.classList).toContain(c)
    expect(label.textContent).toBe('Reopen closed session')
    for (const c of ['w-full', 'sm:w-auto', 'sm:shrink-0', 'sm:max-w-[55%]']) expect(label.classList).toContain(c)
    for (const c of ['w-full', 'sm:w-auto', 'flex-wrap']) expect(caps.classList).toContain(c)
    // Two chords are shown in this (non-Electron) host: the alias leads, the
    // reserved chord follows muted with its tag.
    expect(caps.textContent).toContain('Desktop app only')
    const combos = Array.from(caps.querySelectorAll('[data-testid="shortcut-combo"]'))
    expect(combos).toHaveLength(2)
    for (const combo of combos) {
      expect(combo.classList.contains('flex-nowrap')).toBe(true)
      expect(combo.querySelectorAll('kbd').length).toBeGreaterThan(1)
    }
  })
})

describe('useKeyboardShortcuts — reopen closed session', () => {
  const slot = { key: 'chat-1', history_key: 'dashboard_chat-1', title: 'Chat 1', messages: 1, running: false }

  function setup() {
    const store = createTestStore({
      dashboard: { ...dashboardReducer(undefined, { type: '@@init' }), slots: [slot] } as RootState['dashboard'],
      chat: { ...chatInitialState, activeSlot: 'chat-1' },
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() }),
      { store },
    )
    return store
  }
  const press = (code: string) => {
    const event = new KeyboardEvent('keydown', { code, altKey: true, shiftKey: true, cancelable: true, bubbles: true })
    return !document.dispatchEvent(event)
  }

  it('reopens the session ⌘W closed, through the History resume', async () => {
    vi.mocked(api.deleteChatSlot).mockResolvedValue({ ok: true } as never)
    vi.mocked(api.resumeChatSlot).mockResolvedValue({ ok: true, key: 'chat-1', messages: [] } as never)
    setup()
    act(() => { press('KeyW') })
    await waitFor(() => expect(api.deleteChatSlot).toHaveBeenCalledWith('chat-1'))

    // Recorded only after the close settles: wait for the thunk to finish.
    await act(async () => { await Promise.resolve() })
    let claimed = false
    act(() => { claimed = press('KeyT') })
    expect(claimed).toBe(true)
    await waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_chat-1', 'Chat 1'))
  })

  it('claims the chord with nothing to reopen, says so, and resumes nothing', async () => {
    const store = setup()
    let claimed = false
    act(() => { claimed = press('KeyT') })
    expect(claimed).toBe(true)
    expect(api.resumeChatSlot).not.toHaveBeenCalled()
    // The shell's transient notice (App.tsx renders chat.agentSwitchNotice as a
    // role="status" banner), not a navigation: neutral, not the warning tone a
    // failure gets.
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).toEqual({
      message: 'No recently closed sessions.', tone: 'info',
    }))
  })

  it('does not offer a session whose close the server refused', async () => {
    vi.mocked(api.deleteChatSlot).mockRejectedValue(new Error('refused'))
    setup()
    act(() => { press('KeyW') })
    await waitFor(() => expect(api.deleteChatSlot).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    expect(takeRecentlyClosedSession(neverOpen)).toBeNull()
  })

  it('walks past a session already reopened from History under a different slot key', async () => {
    // `chat-2` was a channel-linked tab: its transcript stem is the channel's,
    // and the History resume publishes it under that stem's folded form. The
    // reopened tab therefore shares `chat-2`'s history_key but not its key.
    const stem = 'slack_C0ABC_1785370133.085469'
    const store = createTestStore({
      dashboard: {
        ...dashboardReducer(undefined, { type: '@@init' }),
        slots: [slot, { key: 'slack_C0ABC_1785370133_085469', history_key: stem, title: 'Thread', messages: 1, running: false }],
      } as RootState['dashboard'],
      chat: { ...chatInitialState, activeSlot: 'chat-1' },
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() }),
      { store },
    )
    vi.mocked(api.resumeChatSlot).mockResolvedValue({ ok: true, key: 'older', messages: [] } as never)
    rememberClosedSession({ key: 'older', history_key: 'dashboard_older', title: 'Older' })
    rememberClosedSession({ key: 'chat-2', history_key: stem, title: 'Thread' })
    act(() => { press('KeyT') })
    // The already-open thread is skipped (and dropped); the press reaches the
    // session closed before it instead of resuming the thread a second time.
    await waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_older', 'Older'))
    expect(api.resumeChatSlot).not.toHaveBeenCalledWith(stem, 'Thread')
    expect(takeRecentlyClosedSession(neverOpen)).toBeNull()
  })
})

describe('useKeyboardShortcuts — reopen behind the draft ask', () => {
  function Probe({ guard }: { guard: () => boolean }) {
    useRegisterNavigationLeaveGuard(guard)
    useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() })
    const loc = useLocation()
    return <div data-testid="reopen-loc">{loc.pathname}</div>
  }

  function setupOnSettings(guard: () => boolean) {
    const store = createTestStore({
      dashboard: { ...dashboardReducer(undefined, { type: '@@init' }), slots: [] } as RootState['dashboard'],
      chat: { ...chatInitialState },
    })
    render(
      <Provider store={store}>
        <NavigationLeaveGuardProvider>
          <MemoryRouter initialEntries={['/settings']}>
            <Probe guard={guard} />
          </MemoryRouter>
        </NavigationLeaveGuardProvider>
      </Provider>,
    )
    return store
  }
  const pressReopen = () => act(() => {
    document.dispatchEvent(new KeyboardEvent('keydown', { code: 'KeyT', altKey: true, shiftKey: true, cancelable: true, bubbles: true }))
  })

  it('a declined leave keeps the page and the closed session', () => {
    rememberClosedSession({ key: 'gone', history_key: 'dashboard_gone', title: 'Gone' })
    const guard = vi.fn(() => false)
    setupOnSettings(guard)
    pressReopen()
    expect(guard).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('reopen-loc').textContent).toBe('/settings')
    expect(api.resumeChatSlot).not.toHaveBeenCalled()
    expect(takeRecentlyClosedSession(neverOpen)?.key).toBe('gone')
  })

  it('an allowed leave reopens and lands on /chat', async () => {
    vi.mocked(api.resumeChatSlot).mockResolvedValue({ ok: true, key: 'gone', messages: [] } as never)
    rememberClosedSession({ key: 'gone', history_key: 'dashboard_gone', title: 'Gone' })
    setupOnSettings(() => true)
    pressReopen()
    expect(screen.getByTestId('reopen-loc').textContent).toBe('/chat')
    await waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_gone', 'Gone'))
  })

  it('asks nothing when there is nothing to reopen, only shows the notice', () => {
    const guard = vi.fn(() => false)
    const store = setupOnSettings(guard)
    pressReopen()
    expect(guard).not.toHaveBeenCalled()
    expect(api.resumeChatSlot).not.toHaveBeenCalled()
    // Stays put: the notice offers an Older Sessions link; it does not open it.
    expect(screen.getByTestId('reopen-loc').textContent).toBe('/settings')
    expect(store.getState().chat.agentSwitchNotice?.message).toBe('No recently closed sessions.')
  })

  it('a failed resume keeps the session reopenable for the next press', async () => {
    vi.mocked(api.resumeChatSlot).mockRejectedValue(new Error('gateway down'))
    rememberClosedSession({ key: 'older', history_key: 'dashboard_older', title: 'Older' })
    // A channel session: its transcript stem is not `dashboard_<key>`, so the
    // retry must keep the stem it was recorded with.
    rememberClosedSession({ key: 'slack_1785370133.085469', history_key: 'slack_1785370133.085469', title: 'Gone' })
    setupOnSettings(() => true)
    pressReopen()
    await waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalledWith('slack_1785370133.085469', 'Gone'))
    await waitFor(() => expect(takeRecentlyClosedSession(neverOpen)).toEqual({
      key: 'slack_1785370133.085469', historyKey: 'slack_1785370133.085469', title: 'Gone',
    }))
  })
})

describe('useSessionActions close — records for reopen', () => {
  it('a sidebar/menu close is offered for reopening once it lands', async () => {
    vi.mocked(api.deleteChatSlot).mockResolvedValue({ ok: true } as never)
    // useSessionActions reads the app store singleton (as its busy-close gate
    // does), so the slot is seeded there and that store is the one provided.
    appStore.dispatch(addSlotOptimistic({ key: 'slot-2', history_key: 'dashboard_slot-2', title: 'Chat 2', messages: 1, running: false }))
    const { result } = renderHookWithProviders(() => useSessionActions(), { store: appStore as unknown as ReturnType<typeof createTestStore> })
    act(() => { result.current.close('slot-2') })
    await waitFor(() => expect(takeRecentlyClosedSession(neverOpen)).toEqual({ key: 'slot-2', historyKey: 'dashboard_slot-2', title: 'Chat 2' }))
  })
})
