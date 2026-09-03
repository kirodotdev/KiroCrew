import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, act } from '@testing-library/react'
import { DEFAULT_SHORTCUTS, formatShortcut, SHORTCUTS_ENABLED_KEY, SHORTCUTS_ENABLED_EVENT, useKeyboardShortcuts, sessionCycleStep, wrapIndex, isAgentMonitorChord, RESERVED_PANEL_CODES, orderSlotsBySidebar, useDigitModifierHeld, jumpLetters, jumpLabelFor, jumpIndexForCode } from '../hooks/useKeyboardShortcuts'
import { PANEL_TOGGLE_SHORTCUTS_KEY } from '../lib/panelToggleShortcuts'
import { renderHookWithProviders, createTestStore, renderWithProviders } from './helpers'
import { consumeComposerRelease } from '../pages/chat/composerFocus'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import ShortcutsModal from '../components/ShortcutsModal'
import type { RootState } from '../store'

beforeEach(() => localStorage.clear())

describe('formatShortcut', () => {
  const setPlatform = (val: string) => Object.defineProperty(navigator, 'platform', { value: val, configurable: true })

  describe('on macOS', () => {
    beforeEach(() => setPlatform('MacIntel'))
    it('uses Option symbol', () => {
      expect(formatShortcut({ id: 't', key: 'k', alt: true, label: '', group: 'Actions' })).toBe('\u2325K')
    })
    it('uses Shift symbol', () => {
      expect(formatShortcut({ id: 't', key: 'n', alt: true, shift: true, label: '', group: 'Actions' })).toBe('\u2325\u21e7N')
    })
    it('uses Return symbol', () => {
      expect(formatShortcut({ id: 't', key: 'Enter', alt: true, label: '', group: 'Actions' })).toBe('\u2325\u23ce')
    })
  })

  describe('on non-Mac', () => {
    beforeEach(() => setPlatform('Win32'))
    it('formats Alt + key', () => {
      expect(formatShortcut({ id: 't', key: 'k', alt: true, label: '', group: 'Actions' })).toBe('Alt + K')
    })
    it('formats Alt + Shift + key', () => {
      expect(formatShortcut({ id: 't', key: 'n', alt: true, shift: true, label: '', group: 'Actions' })).toBe('Alt + Shift + N')
    })
    it('formats arrow keys', () => {
      expect(formatShortcut({ id: 't', key: 'ArrowLeft', alt: true, label: '', group: 'chat-navigation' })).toBe('Alt + \u2190')
    })
  })
})

describe('DEFAULT_SHORTCUTS', () => {
  it('has unique IDs', () => {
    const ids = DEFAULT_SHORTCUTS.map(s => s.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
  it('has all required groups', () => {
    const groups = new Set(DEFAULT_SHORTCUTS.map(s => s.group))
    expect(groups).toContain('chat-navigation')
    expect(groups).toContain('panel-navigation')
    expect(groups).toContain('actions')
  })
})

describe('useKeyboardShortcuts — toggle behavior', () => {
  const onToggleShortcutsModal = vi.fn()
  const onNewChat = vi.fn()

  function setup(opts: { enabled?: boolean; disabled?: boolean } = {}) {
    if (opts.enabled === false) localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const store = createTestStore({
      dashboard: { slots: [{ key: 'slot-1', title: 'Chat 1', messages: 1, running: false }] } as unknown as RootState['dashboard'],
      chat: { activeSlot: null, slotHistory: [] } as unknown as RootState['chat'],
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal, onNewChat, disabled: opts.disabled }),
      { store },
    )
    return store
  }

  beforeEach(() => { onToggleShortcutsModal.mockClear(); onNewChat.mockClear() })

  it('Alt+K fires when shortcuts are enabled', () => {
    setup()
    fireEvent.keyDown(document, { code: 'KeyK', altKey: true })
    expect(onToggleShortcutsModal).toHaveBeenCalledTimes(1)
  })

  it('Alt+K fires even when shortcuts are disabled', () => {
    setup({ enabled: false })
    fireEvent.keyDown(document, { code: 'KeyK', altKey: true })
    expect(onToggleShortcutsModal).toHaveBeenCalledTimes(1)
  })

  it('Alt+Shift+N fires new chat when enabled', () => {
    setup()
    fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true })
    expect(onNewChat).toHaveBeenCalledTimes(1)
  })

  it('Alt+Shift+N is suppressed when disabled', () => {
    setup({ enabled: false })
    fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true })
    expect(onNewChat).not.toHaveBeenCalled()
  })

  it('Alt+Shift+W closes the active session (handler matches, preventDefault called)', () => {
    const store = createTestStore({
      dashboard: { slots: [{ key: 'slot-1', title: 'Chat 1', messages: 1, running: false }] } as unknown as RootState['dashboard'],
      chat: { activeSlot: 'slot-1', slotHistory: [] } as unknown as RootState['chat'],
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal, onNewChat }),
      { store },
    )
    const event = new KeyboardEvent('keydown', { code: 'KeyW', altKey: true, shiftKey: true, cancelable: true, bubbles: true })
    const prevented = !document.dispatchEvent(event)
    expect(prevented).toBe(true)
  })

  it('Alt+Shift+W is suppressed when shortcuts are disabled', () => {
    const store = createTestStore({
      dashboard: { slots: [{ key: 'slot-1', title: 'Chat 1', messages: 1, running: false }] } as unknown as RootState['dashboard'],
      chat: { activeSlot: 'slot-1', slotHistory: [] } as unknown as RootState['chat'],
    })
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal, onNewChat }),
      { store },
    )
    const event = new KeyboardEvent('keydown', { code: 'KeyW', altKey: true, shiftKey: true, cancelable: true, bubbles: true })
    const prevented = !document.dispatchEvent(event)
    expect(prevented).toBe(false)
  })

  it('Ctrl+number does NOT switch chats when IS_MAC is false (non-Mac env)', () => {
    // Verifies that the Ctrl+digit handler is gated by IS_MAC/ctrlDigits.
    // In jsdom IS_MAC=false, so Ctrl+digit should be ignored.
    const store = createTestStore({
      dashboard: { slots: [{ key: 'slot-1', title: 'Chat 1', messages: 1, running: false }, { key: 'slot-2', title: 'Chat 2', messages: 0, running: false }, { key: 'slot-3', title: 'Chat 3', messages: 0, running: false }] } as unknown as RootState['dashboard'],
      chat: { activeSlot: 'slot-1', slotHistory: [] } as unknown as RootState['chat'],
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal, onNewChat }),
      { store },
    )
    fireEvent.keyDown(document, { code: 'Digit3', ctrlKey: true })
    expect(store.getState().chat.activeSlot).toBe('slot-1')
  })

  it('Alt+number dispatches chat switch on Windows/Linux', () => {
    const store = createTestStore({
      dashboard: { slots: [{ key: 'slot-1', title: 'Chat 1', messages: 1, running: false }, { key: 'slot-2', title: 'Chat 2', messages: 0, running: false }, { key: 'slot-3', title: 'Chat 3', messages: 0, running: false }] } as unknown as RootState['dashboard'],
      chat: { activeSlot: 'slot-1', slotHistory: [] } as unknown as RootState['chat'],
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal, onNewChat }),
      { store },
    )
    // Alt+3 should be handled (preventDefault called) on non-Mac
    const event = new KeyboardEvent('keydown', { code: 'Digit3', altKey: true, cancelable: true, bubbles: true })
    const prevented = !document.dispatchEvent(event)
    expect(prevented).toBe(true)
  })

  it('Alt+` arms a one-shot beforeinput guard that cancels the macOS dead-key char', () => {
    const store = createTestStore({
      dashboard: { slots: [{ key: 'slot-1', title: 'Chat 1', messages: 0, running: false }, { key: 'slot-2', title: 'Chat 2', messages: 0, running: false }] } as unknown as RootState['dashboard'],
      chat: { activeSlot: 'slot-2', slotHistory: ['slot-1'] } as unknown as RootState['chat'],
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal, onNewChat }),
      { store },
    )
    // Alt+` (MRU toggle) fires while a text field is focused. On macOS Option+`
    // is a dead key whose grave-accent char still arrives via beforeinput, which
    // keydown.preventDefault() cannot cancel — the handler arms a one-shot guard.
    document.dispatchEvent(new KeyboardEvent('keydown', { code: 'Backquote', altKey: true, cancelable: true, bubbles: true }))
    // The stray composed character arrives via beforeinput → must be cancelled.
    expect(!document.dispatchEvent(new Event('beforeinput', { cancelable: true, bubbles: true }))).toBe(true)
    // One-shot: the next beforeinput is NOT cancelled.
    expect(!document.dispatchEvent(new Event('beforeinput', { cancelable: true, bubbles: true }))).toBe(false)
  })

  it('responds to SHORTCUTS_ENABLED_EVENT to re-enable', () => {
    setup({ enabled: false })
    // Re-enable via event (wrapped in act since it triggers state update)
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '1')
    act(() => { window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT)) })
    fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true })
    expect(onNewChat).toHaveBeenCalledTimes(1)
  })

  it('Alt+Enter focuses the composer even on a touch device (#4088)', () => {
    // The Alt+Enter site is deliberately UNGUARDED: a pressed keyboard
    // shortcut proves a keyboard exists, so focusComposer()'s touch-device
    // skip must not apply. This exercises the failure path directly — a
    // future "consistency" swap to the guarded helper turns this red.
    const composer = document.createElement('textarea')
    composer.setAttribute('data-composer-input', '')
    document.body.appendChild(composer)
    const matchMedia = window.matchMedia
    // Make isTouchDevice() genuinely return true: coarse pointer + no hover.
    window.matchMedia = ((q: string) => ({
      matches: q === '(pointer: coarse)' || q === '(hover: none)',
      media: q, addEventListener: () => {}, removeEventListener: () => {},
      addListener: () => {}, removeListener: () => {}, onchange: null,
      dispatchEvent: () => false,
    })) as typeof window.matchMedia
    try {
      setup()
      fireEvent.keyDown(document, { code: 'Enter', altKey: true })
      expect(document.activeElement).toBe(composer)
    } finally {
      window.matchMedia = matchMedia
      composer.remove()
    }
  })
})

describe('ShortcutsModal', () => {
  const onClose = vi.fn()
  beforeEach(() => onClose.mockClear())

  it('renders all shortcut groups', () => {
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    expect(screen.getByText('Chat Navigation')).toBeInTheDocument()
    expect(screen.getByText('Panel Navigation')).toBeInTheDocument()
    expect(screen.getByText('Actions')).toBeInTheDocument()
  })

  it('lists the panel-toggle shortcuts with their default bindings', () => {
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    expect(screen.getByText('Toggle left sidebar')).toBeInTheDocument()
    expect(screen.getByText('Toggle session panel')).toBeInTheDocument()
    expect(screen.getByText('Toggle side panel')).toBeInTheDocument()
    // jsdom is non-Mac, so the default session-panel chord renders as Ctrl + B.
    // The label is itself a block div (an i18n run boundary), so the row is its parent.
    const row = screen.getByText('Toggle session panel').parentElement!
    expect(row).toHaveTextContent('Ctrl')
    expect(row).toHaveTextContent('B')
  })

  it('shows "Not set" for a panel the user has cleared to unbound', () => {
    localStorage.setItem(PANEL_TOGGLE_SHORTCUTS_KEY, JSON.stringify({ 'session-panel': null }))
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    expect(screen.getByText('Toggle session panel').parentElement!).toHaveTextContent('Not set')
  })

  it('renders the enable toggle checked by default', () => {
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    const toggle = screen.getByRole('switch')
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })

  it('clicking toggle sets localStorage to disabled', () => {
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    fireEvent.click(screen.getByRole('switch'))
    expect(localStorage.getItem(SHORTCUTS_ENABLED_KEY)).toBe('0')
  })

  it('clicking toggle dispatches SHORTCUTS_ENABLED_EVENT', () => {
    const handler = vi.fn()
    window.addEventListener(SHORTCUTS_ENABLED_EVENT, handler)
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    fireEvent.click(screen.getByRole('switch'))
    expect(handler).toHaveBeenCalledTimes(1)
    window.removeEventListener(SHORTCUTS_ENABLED_EVENT, handler)
  })

  it('Escape key closes modal', () => {
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('clicking backdrop closes modal', () => {
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    fireEvent.click(screen.getByRole('dialog'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('sessionCycleStep', () => {
  const ev = (o: Partial<Record<'code' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey', unknown>>) =>
    ({ code: 'BracketRight', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...o }) as Parameters<typeof sessionCycleStep>[0]

  it('maps ⌘[ / ⌘] to -1 / +1 on macOS', () => {
    expect(sessionCycleStep(ev({ code: 'BracketLeft', metaKey: true }), true)).toBe(-1)
    expect(sessionCycleStep(ev({ code: 'BracketRight', metaKey: true }), true)).toBe(1)
  })

  it('maps Ctrl+[ / Ctrl+] to -1 / +1 on Windows/Linux', () => {
    expect(sessionCycleStep(ev({ code: 'BracketLeft', ctrlKey: true }), false)).toBe(-1)
    expect(sessionCycleStep(ev({ code: 'BracketRight', ctrlKey: true }), false)).toBe(1)
  })

  it('requires the platform primary modifier, not the other one', () => {
    expect(sessionCycleStep(ev({ ctrlKey: true }), true)).toBe(0)   // Ctrl+] on Mac
    expect(sessionCycleStep(ev({ metaKey: true }), false)).toBe(0)  // ⌘] on Win/Linux
  })

  it('rejects extra modifiers so it cannot fire from a near-miss chord', () => {
    expect(sessionCycleStep(ev({ metaKey: true, altKey: true }), true)).toBe(0)
    expect(sessionCycleStep(ev({ metaKey: true, shiftKey: true }), true)).toBe(0)
    expect(sessionCycleStep(ev({ metaKey: true, ctrlKey: true }), true)).toBe(0)
  })

  it('returns 0 for any other key', () => {
    expect(sessionCycleStep(ev({ code: 'KeyP', metaKey: true }), true)).toBe(0)
    expect(sessionCycleStep(ev({ code: 'Backslash', metaKey: true }), true)).toBe(0)
  })
})

describe('wrapIndex', () => {
  it('steps and wraps at both ends', () => {
    expect(wrapIndex(3, 1, 1)).toBe(2)
    expect(wrapIndex(3, 2, 1)).toBe(0)
    expect(wrapIndex(3, 1, -1)).toBe(0)
    expect(wrapIndex(3, 0, -1)).toBe(2)
  })
  it('lands on an end when nothing is selected', () => {
    expect(wrapIndex(3, -1, 1)).toBe(0)
    expect(wrapIndex(3, -1, -1)).toBe(2)
  })
  it('reports -1 for an empty list', () => {
    expect(wrapIndex(0, -1, 1)).toBe(-1)
  })
})

describe('⌘/Ctrl+[ and ⌘/Ctrl+] session cycling', () => {
  // The switchSlot thunk's pending reducer touches most of the chat slice, so
  // preload from the slice's real initial state rather than a partial cast.
  const chatState = (activeSlot: string | null) =>
    ({ ...createTestStore().getState().chat, activeSlot, slotHistory: [] }) as RootState['chat']

  const threeSlots = (active: string) => createTestStore({
    dashboard: { slots: [
      { key: 'slot-1', title: 'Chat 1', messages: 0, running: false },
      { key: 'slot-2', title: 'Chat 2', messages: 0, running: false },
      { key: 'slot-3', title: 'Chat 3', messages: 0, running: false },
    ] } as unknown as RootState['dashboard'],
    chat: chatState(active),
  })

  // jsdom reports a non-Mac platform, so the handler's primary modifier is Ctrl.
  function mount(store: ReturnType<typeof createTestStore>, disabled?: boolean) {
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), disabled }),
      { store },
    )
  }

  const press = (code: string, target: EventTarget = document) => {
    const event = new KeyboardEvent('keydown', { code, ctrlKey: true, cancelable: true, bubbles: true })
    return !target.dispatchEvent(event) // true when preventDefault was called
  }

  it('] advances to the next session', () => {
    const store = threeSlots('slot-2')
    mount(store)
    expect(press('BracketRight')).toBe(true)
    expect(store.getState().chat.activeSlot).toBe('slot-3')
  })

  it('[ goes back to the previous session', () => {
    const store = threeSlots('slot-2')
    mount(store)
    expect(press('BracketLeft')).toBe(true)
    expect(store.getState().chat.activeSlot).toBe('slot-1')
  })

  it('wraps forward past the last session', () => {
    const store = threeSlots('slot-3')
    mount(store)
    press('BracketRight')
    expect(store.getState().chat.activeSlot).toBe('slot-1')
  })

  it('wraps backward past the first session', () => {
    const store = threeSlots('slot-1')
    mount(store)
    press('BracketLeft')
    expect(store.getState().chat.activeSlot).toBe('slot-3')
  })

  it('works from inside a text field (the chord has no editing meaning)', () => {
    const store = threeSlots('slot-1')
    mount(store)
    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    expect(press('BracketRight', textarea)).toBe(true)
    expect(store.getState().chat.activeSlot).toBe('slot-2')
    textarea.remove()
  })

  it('leaves the keystroke to the PTY when it comes from a terminal', () => {
    const store = threeSlots('slot-1')
    mount(store)
    const term = document.createElement('div')
    term.className = 'xterm'
    const inner = document.createElement('textarea')
    term.appendChild(inner)
    document.body.appendChild(term)
    expect(press('BracketLeft', inner)).toBe(false) // not claimed → ESC reaches the PTY
    expect(store.getState().chat.activeSlot).toBe('slot-1')
    term.remove()
  })

  it('does not claim the keystroke when shortcuts are globally disabled', () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const store = threeSlots('slot-1')
    mount(store)
    expect(press('BracketRight')).toBe(false) // browser Forward still works
    expect(store.getState().chat.activeSlot).toBe('slot-1')
  })

  it('is suppressed while another surface owns the keyboard (disabled)', () => {
    const store = threeSlots('slot-1')
    mount(store, true)
    press('BracketRight')
    expect(store.getState().chat.activeSlot).toBe('slot-1')
  })

  it('does nothing with no sessions open', () => {
    const store = createTestStore({
      dashboard: { slots: [] } as unknown as RootState['dashboard'],
      chat: chatState(null),
    })
    mount(store)
    expect(press('BracketRight')).toBe(true) // still claimed, just nowhere to go
    expect(store.getState().chat.activeSlot).toBe(null)
  })

  it('is advertised in the shortcuts registry as a ⌘/Ctrl chord', () => {
    const prev = DEFAULT_SHORTCUTS.find(s => s.id === 'chat-prev-bracket')
    const next = DEFAULT_SHORTCUTS.find(s => s.id === 'chat-next-bracket')
    expect(prev).toMatchObject({ key: '[', meta: true, group: 'chat-navigation' })
    expect(next).toMatchObject({ key: ']', meta: true, group: 'chat-navigation' })
    expect(prev?.alt).toBeUndefined()
    expect(next?.ctrl).toBeUndefined()
  })
})

/**
 * Panel-toggle shortcuts (user-rebindable). jsdom reports a non-Mac platform, so
 * the default `mod` chords resolve to Ctrl: session panel = Ctrl+B, side panel =
 * Ctrl+\, left sidebar = Ctrl+Shift+S. Handled BEFORE the Alt gate (the defaults
 * carry no Alt) and fire even inside the composer, like the bracket chords.
 */
describe('panel-toggle shortcuts', () => {
  const cbs = () => ({
    onToggleLeftSidebar: vi.fn(),
    onToggleSessionPanel: vi.fn(),
    onToggleSidePanel: vi.fn(),
    onToggleTerminal: vi.fn(),
  })

  function setup(handlers: ReturnType<typeof cbs>, opts: { enabled?: boolean; disabled?: boolean } = {}) {
    if (opts.enabled === false) localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), disabled: opts.disabled, ...handlers }),
      { store },
    )
    return store
  }

  it('Ctrl+B toggles the session panel', () => {
    const h = cbs(); setup(h)
    fireEvent.keyDown(document, { code: 'KeyB', ctrlKey: true })
    expect(h.onToggleSessionPanel).toHaveBeenCalledTimes(1)
    expect(h.onToggleLeftSidebar).not.toHaveBeenCalled()
    expect(h.onToggleSidePanel).not.toHaveBeenCalled()
  })

  it('Ctrl+\\ toggles the side panel', () => {
    const h = cbs(); setup(h)
    fireEvent.keyDown(document, { code: 'Backslash', key: '\\', ctrlKey: true })
    expect(h.onToggleSidePanel).toHaveBeenCalledTimes(1)
  })

  it('leaves the left sidebar unbound by default (no default chord fires it)', () => {
    const h = cbs(); setup(h)
    fireEvent.keyDown(document, { code: 'KeyS', ctrlKey: true, shiftKey: true })
    expect(h.onToggleLeftSidebar).not.toHaveBeenCalled()
  })

  it('toggles the left sidebar once the user binds a chord to it', () => {
    localStorage.setItem(PANEL_TOGGLE_SHORTCUTS_KEY, JSON.stringify({ 'left-sidebar': { key: 's', mod: true, shift: true } }))
    const h = cbs(); setup(h)
    fireEvent.keyDown(document, { code: 'KeyS', ctrlKey: true, shiftKey: true })
    expect(h.onToggleLeftSidebar).toHaveBeenCalledTimes(1)
  })

  it('claims the keystroke so the browser default does not run', () => {
    const h = cbs(); setup(h)
    const event = new KeyboardEvent('keydown', { code: 'KeyB', ctrlKey: true, cancelable: true, bubbles: true })
    expect(!document.dispatchEvent(event)).toBe(true)
  })

  it('fires from inside a text field (toggle while composing)', () => {
    const h = cbs(); setup(h)
    const ta = document.createElement('textarea')
    document.body.appendChild(ta)
    fireEvent.keyDown(ta, { code: 'KeyB', ctrlKey: true })
    expect(h.onToggleSessionPanel).toHaveBeenCalledTimes(1)
    ta.remove()
  })

  it('leaves the keystroke to the PTY inside an embedded terminal', () => {
    const h = cbs(); setup(h)
    const term = document.createElement('div')
    term.className = 'xterm'
    const inner = document.createElement('textarea')
    term.appendChild(inner)
    document.body.appendChild(term)
    fireEvent.keyDown(inner, { code: 'KeyB', ctrlKey: true })
    expect(h.onToggleSessionPanel).not.toHaveBeenCalled()
    term.remove()
  })

  /* The terminal ships UNBOUND, so every test below binds it the way a user who
     wants it would — to Cmd/Ctrl+J, whose `^J` collision is precisely what the
     skip-shell seam has to survive. Binding it here rather than relying on a
     default is also the point: the behaviour under test is the seam, not the
     factory chord. */
  const bindTerminalToModJ = () =>
    localStorage.setItem(PANEL_TOGGLE_SHORTCUTS_KEY, JSON.stringify({ terminal: { key: 'j', mod: true } }))

  /* The shipped default must claim nothing: an unbound panel that preventDefault-ed
     would suppress the browser's own Ctrl+J (Downloads) in exchange for no action,
     and inside a shell would eat readline's accept-line. This is the assertion that
     makes adding a default a deliberate act — it fails the moment one appears. */
  it('claims no chord out of the box, since the terminal ships unbound', () => {
    const h = cbs(); setup(h)
    const event = new KeyboardEvent('keydown', { code: 'KeyJ', key: 'j', ctrlKey: true, cancelable: true, bubbles: true })
    expect(document.dispatchEvent(event)).toBe(true)
    expect(event.defaultPrevented).toBe(false)
    expect(h.onToggleTerminal).not.toHaveBeenCalled()
  })

  it('Ctrl+J toggles the docked terminal once the user binds it', () => {
    bindTerminalToModJ()
    const h = cbs(); setup(h)
    fireEvent.keyDown(document, { code: 'KeyJ', ctrlKey: true })
    expect(h.onToggleTerminal).toHaveBeenCalledTimes(1)
    expect(h.onToggleSessionPanel).not.toHaveBeenCalled()
    expect(h.onToggleSidePanel).not.toHaveBeenCalled()
  })

  /* The terminal toggle is the ONE id exempt from the PTY yield above: opening
     that panel focuses its shell, so yielding there would leave the chord able to
     open the panel but never close it. */
  it('still toggles the terminal from inside the embedded terminal', () => {
    bindTerminalToModJ()
    const h = cbs(); setup(h)
    const term = document.createElement('div')
    term.className = 'xterm'
    const inner = document.createElement('textarea')
    term.appendChild(inner)
    document.body.appendChild(term)
    // xterm.js consumes the keys it recognises, and on Windows/Linux this chord IS
    // one (Ctrl+J is ^J), so a target that stops propagation stands in for it —
    // only the capture-phase skip-shell listener can survive this.
    term.addEventListener('keydown', e => e.stopPropagation())
    inner.dispatchEvent(new KeyboardEvent('keydown', { code: 'KeyJ', key: 'j', ctrlKey: true, cancelable: true, bubbles: true }))
    expect(h.onToggleTerminal).toHaveBeenCalledTimes(1)
    term.remove()
  })

  it('keeps the skip-shell keystroke away from the PTY', () => {
    bindTerminalToModJ()
    const h = cbs(); setup(h)
    const term = document.createElement('div')
    term.className = 'xterm'
    document.body.appendChild(term)
    const event = new KeyboardEvent('keydown', { code: 'KeyJ', key: 'j', ctrlKey: true, cancelable: true, bubbles: true })
    expect(!term.dispatchEvent(event)).toBe(true)
    term.remove()
  })

  /* App leaves the callback undefined when dashboard.terminal.enabled=false, and in
     a popout/embed where the panel does not exist. A panel with no action is not
     ours to claim even when the user HAS bound a chord to it: preventDefault-ing
     there would suppress the browser's own Ctrl+J in exchange for nothing, so the
     chord has to fall through untouched rather than merely not throw. */
  it('leaves a bound Ctrl+J to the browser when the terminal is disabled (no callback supplied)', () => {
    bindTerminalToModJ()
    const h = cbs(); setup({ ...h, onToggleTerminal: undefined as unknown as () => void })
    const event = new KeyboardEvent('keydown', { code: 'KeyJ', key: 'j', ctrlKey: true, cancelable: true, bubbles: true })
    expect(document.dispatchEvent(event)).toBe(true)
    expect(event.defaultPrevented).toBe(false)
  })

  /* Same rule on the capture-phase seam, which would otherwise stopPropagation the
     keystroke away from the shell the user actually aimed it at. */
  it('leaves a bound Ctrl+J to the shell inside a terminal when the terminal is disabled', () => {
    bindTerminalToModJ()
    const h = cbs(); setup({ ...h, onToggleTerminal: undefined as unknown as () => void })
    const term = document.createElement('div')
    term.className = 'xterm'
    document.body.appendChild(term)
    const seen: string[] = []
    term.addEventListener('keydown', e => seen.push((e as KeyboardEvent).code))
    const event = new KeyboardEvent('keydown', { code: 'KeyJ', key: 'j', ctrlKey: true, cancelable: true, bubbles: true })
    expect(term.dispatchEvent(event)).toBe(true)
    expect(seen).toEqual(['KeyJ'])
    term.remove()
  })

  it('does not fire when shortcuts are globally disabled', () => {
    const h = cbs(); setup(h, { enabled: false })
    fireEvent.keyDown(document, { code: 'KeyB', ctrlKey: true })
    expect(h.onToggleSessionPanel).not.toHaveBeenCalled()
  })

  it('does not fire while a modal holds the shortcuts (disabled prop)', () => {
    const h = cbs(); setup(h, { disabled: true })
    fireEvent.keyDown(document, { code: 'KeyB', ctrlKey: true })
    expect(h.onToggleSessionPanel).not.toHaveBeenCalled()
  })

  it('yields when another handler already consumed the keystroke (defaultPrevented)', () => {
    // A capture-phase handler upstream (e.g. the Pierre editor's ⌘⇧S save) calls
    // preventDefault before this document-level listener sees the bubble; the
    // panel toggle must defer rather than double-fire.
    const h = cbs(); setup(h)
    const consume = (e: KeyboardEvent) => e.preventDefault()
    document.addEventListener('keydown', consume, true)
    try {
      // Ctrl+B is the bound session-panel default, so it WOULD fire without the guard.
      fireEvent.keyDown(document, { code: 'KeyB', ctrlKey: true })
      expect(h.onToggleSessionPanel).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', consume, true)
    }
  })

  it('does not fire for a panel the user has cleared to unbound', () => {
    localStorage.setItem(PANEL_TOGGLE_SHORTCUTS_KEY, JSON.stringify({ 'session-panel': null }))
    const h = cbs(); setup(h)
    fireEvent.keyDown(document, { code: 'KeyB', ctrlKey: true })
    expect(h.onToggleSessionPanel).not.toHaveBeenCalled()
  })
})

describe('Alt+Shift+A agent cycling', () => {
  it('calls onCycleAgent on Alt+Shift+A', () => {
    const onCycleAgent = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCycleAgent }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyA', altKey: true, shiftKey: true })
    expect(onCycleAgent).toHaveBeenCalledTimes(1)
  })

  it('does not fire when disabled', () => {
    const onCycleAgent = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCycleAgent, disabled: true }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyA', altKey: true, shiftKey: true })
    expect(onCycleAgent).not.toHaveBeenCalled()
  })

  it('does not fire when shortcuts are globally disabled', () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT))
    const onCycleAgent = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCycleAgent }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyA', altKey: true, shiftKey: true })
    expect(onCycleAgent).not.toHaveBeenCalled()
  })
})



describe('Alt+Shift+D reasoning effort cycling', () => {
  it('calls onCycleReasoningEffort on Alt+Shift+D', () => {
    const onCycleReasoningEffort = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCycleReasoningEffort }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyD', altKey: true, shiftKey: true })
    expect(onCycleReasoningEffort).toHaveBeenCalledTimes(1)
  })
})

describe('Alt+Shift+Z previous agent', () => {
  it('calls onCyclePrevAgent on Alt+Shift+Z', () => {
    const onCyclePrevAgent = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCyclePrevAgent }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyZ', altKey: true, shiftKey: true })
    expect(onCyclePrevAgent).toHaveBeenCalledTimes(1)
  })
})



describe('Alt+Shift+C previous reasoning effort', () => {
  it('calls onCyclePrevReasoningEffort on Alt+Shift+C', () => {
    const onCyclePrevReasoningEffort = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCyclePrevReasoningEffort }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyC', altKey: true, shiftKey: true })
    expect(onCyclePrevReasoningEffort).toHaveBeenCalledTimes(1)
  })
})

describe('Alt+Shift+F approval mode cycling', () => {
  it('calls onCycleApprovalMode on Alt+Shift+F', () => {
    const onCycleApprovalMode = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCycleApprovalMode }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyF', altKey: true, shiftKey: true })
    expect(onCycleApprovalMode).toHaveBeenCalledTimes(1)
  })
})

describe('Alt+Shift+V previous approval mode', () => {
  it('calls onCyclePrevApprovalMode on Alt+Shift+V', () => {
    const onCyclePrevApprovalMode = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCyclePrevApprovalMode }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyV', altKey: true, shiftKey: true })
    expect(onCyclePrevApprovalMode).toHaveBeenCalledTimes(1)
  })
})

describe('Alt+Shift+S cycle model', () => {
  it('calls onCycleModel on Alt+Shift+S', () => {
    const onCycleModel = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCycleModel }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyS', altKey: true, shiftKey: true })
    expect(onCycleModel).toHaveBeenCalledTimes(1)
  })
})

describe('Alt+Shift+X previous model', () => {
  it('calls onCyclePrevModel on Alt+Shift+X', () => {
    const onCyclePrevModel = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCyclePrevModel }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyX', altKey: true, shiftKey: true })
    expect(onCyclePrevModel).toHaveBeenCalledTimes(1)
  })
})

/**
 * Ctrl+G — agent monitor. The kiro-cli backend prints "Press ctrl+g to monitor
 * progress." into its crew-pipeline tool result, so the dashboard binds Ctrl+G
 * to honor that hint on every non-TUI surface.
 */
describe('isAgentMonitorChord', () => {
  const chord = (o: Partial<Record<'code' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey', unknown>> = {}) =>
    ({ code: 'KeyG', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...o }) as Parameters<typeof isAgentMonitorChord>[0]

  it('matches plain Ctrl+G', () => {
    expect(isAgentMonitorChord(chord({ ctrlKey: true }))).toBe(true)
  })

  it('ignores G without Ctrl', () => {
    expect(isAgentMonitorChord(chord())).toBe(false)
  })

  it('ignores Cmd+G (find-next on macOS stays with the browser)', () => {
    expect(isAgentMonitorChord(chord({ metaKey: true }))).toBe(false)
  })

  it('ignores Alt+G, so the downstream panel seam keeps the chord', () => {
    expect(isAgentMonitorChord(chord({ altKey: true }))).toBe(false)
    expect(isAgentMonitorChord(chord({ ctrlKey: true, altKey: true }))).toBe(false)
  })

  it('ignores Ctrl+Shift+G and Ctrl+Cmd+G near-misses', () => {
    expect(isAgentMonitorChord(chord({ ctrlKey: true, shiftKey: true }))).toBe(false)
    expect(isAgentMonitorChord(chord({ ctrlKey: true, metaKey: true }))).toBe(false)
  })

  it('ignores Ctrl on any other key', () => {
    expect(isAgentMonitorChord(chord({ code: 'KeyH', ctrlKey: true }))).toBe(false)
  })

  /**
   * The chord requires ctrlKey && !altKey, so it is unreachable for an Alt+G
   * keystroke and cannot shadow a downstream Alt+G panel registration.
   * Reserving 'KeyG' would over-claim the extension seam — assert it stays free
   * so a future "just add it to be safe" edit has to justify itself here.
   */
  it('does not reserve KeyG from the panel-navigation seam', () => {
    expect(RESERVED_PANEL_CODES.has('KeyG')).toBe(false)
  })
})

describe('agent monitor shortcut registration', () => {
  it('is advertised in DEFAULT_SHORTCUTS as a literal-Ctrl chord', () => {
    const def = DEFAULT_SHORTCUTS.find(s => s.id === 'agent-monitor')
    expect(def).toBeDefined()
    expect(def!.key).toBe('g')
    expect(def!.ctrl).toBe(true)
    // Not meta/alt: the backend hint says "ctrl+g" on every platform.
    expect(def!.meta).toBeUndefined()
    expect(def!.alt).toBeUndefined()
  })

  it('renders as Ctrl + G on Windows/Linux', () => {
    Object.defineProperty(navigator, 'platform', { value: 'Win32', configurable: true })
    const def = DEFAULT_SHORTCUTS.find(s => s.id === 'agent-monitor')!
    expect(formatShortcut(def)).toBe('Ctrl + G')
  })

  it('renders as the Control glyph (not Command) on macOS', () => {
    Object.defineProperty(navigator, 'platform', { value: 'MacIntel', configurable: true })
    const def = DEFAULT_SHORTCUTS.find(s => s.id === 'agent-monitor')!
    expect(formatShortcut(def)).toBe('\u2303G')
  })
})

describe('Ctrl+G opens the agent monitor', () => {
  function setup(opts: { enabled?: boolean; disabled?: boolean } = {}) {
    if (opts.enabled === false) localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const store = createTestStore({
      dashboard: { slots: [{ key: 'slot-1', title: 'Chat 1', messages: 1, running: false }] } as unknown as RootState['dashboard'],
      chat: { activeSlot: 'slot-1', slotHistory: [], activityOpen: false, activityTab: 'logs' } as unknown as RootState['chat'],
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), disabled: opts.disabled }),
      { store },
    )
    return store
  }

  it('opens the Subagents activity tab', () => {
    const store = setup()
    fireEvent.keyDown(document, { code: 'KeyG', ctrlKey: true })
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(store.getState().chat.activityTab).toBe('subagents')
  })

  it('claims the keystroke so the browser does not run find-next', () => {
    setup()
    const event = new KeyboardEvent('keydown', { code: 'KeyG', ctrlKey: true, cancelable: true, bubbles: true })
    expect(!document.dispatchEvent(event)).toBe(true)
  })

  /* Fires inside the composer on purpose: the hint is read while a crew runs and
     focus is normally in the textarea, so bailing on input targets would make the
     chord dead exactly when it is needed. */
  it('still fires when a textarea has focus', () => {
    const store = setup()
    const ta = document.createElement('textarea')
    document.body.appendChild(ta)
    ta.focus()
    fireEvent.keyDown(ta, { code: 'KeyG', ctrlKey: true })
    expect(store.getState().chat.activityTab).toBe('subagents')
    ta.remove()
  })

  /* Ctrl+G is BEL in a PTY — it belongs to the terminal, not to us. */
  it('does not fire for a keystroke inside an embedded terminal', () => {
    const store = setup()
    const term = document.createElement('div')
    term.className = 'xterm'
    const inner = document.createElement('textarea')
    term.appendChild(inner)
    document.body.appendChild(term)
    fireEvent.keyDown(inner, { code: 'KeyG', ctrlKey: true })
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(store.getState().chat.activityTab).toBe('logs')
    term.remove()
  })

  it('is suppressed when shortcuts are globally disabled', () => {
    const store = setup({ enabled: false })
    fireEvent.keyDown(document, { code: 'KeyG', ctrlKey: true })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('is suppressed while a modal holds the shortcuts (disabled prop)', () => {
    const store = setup({ disabled: true })
    fireEvent.keyDown(document, { code: 'KeyG', ctrlKey: true })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('does not open the panel on Alt+G', () => {
    const store = setup()
    fireEvent.keyDown(document, { code: 'KeyG', altKey: true })
    expect(store.getState().chat.activityOpen).toBe(false)
  })
})

describe('orderSlotsBySidebar', () => {
  const s = (key: string) => ({ key })

  it('reorders slots to the published sidebar order', () => {
    const slots = [s('a'), s('b'), s('c')]
    expect(orderSlotsBySidebar(slots, ['c', 'a', 'b']).map(x => x.key)).toEqual(['c', 'a', 'b'])
  })

  it('falls back to store order when the published order is empty or undefined', () => {
    const slots = [s('a'), s('b')]
    expect(orderSlotsBySidebar(slots, []).map(x => x.key)).toEqual(['a', 'b'])
    expect(orderSlotsBySidebar(slots, undefined).map(x => x.key)).toEqual(['a', 'b'])
  })

  it('drops stale keys and appends unpublished slots in store order', () => {
    const slots = [s('a'), s('b'), s('c'), s('d')]
    // 'gone' was closed since publish; 'c' and 'd' were created/filtered since.
    expect(orderSlotsBySidebar(slots, ['b', 'gone', 'a']).map(x => x.key)).toEqual(['b', 'a', 'c', 'd'])
  })
})

describe('chat jump + cycle follow the sidebar display order', () => {
  const slots = [
    { key: 'slot-1', title: 'Oldest', messages: 0, running: false },
    { key: 'slot-2', title: 'Middle', messages: 0, running: false },
    { key: 'slot-3', title: 'Newest', messages: 0, running: false },
  ]

  function setup(sidebarOrder?: string[], activeSlot: string | null = null) {
    // Full slice states (reducer defaults) with overrides: switchSlot.pending
    // touches slotActivity/slotMessages/messages, so a partial chat state
    // would throw inside the reducer and silently swallow the switch.
    const chatInitial = chatReducer(undefined, { type: '@@test/init' })
    const dashInitial = dashboardReducer(undefined, { type: '@@test/init' })
    const store = createTestStore({
      dashboard: { ...dashInitial, slots, ...(sidebarOrder ? { sidebarOrder } : {}) } as RootState['dashboard'],
      chat: { ...chatInitial, activeSlot, slotHistory: [] } as RootState['chat'],
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() }),
      { store },
    )
    return store
  }

  it('Alt+1 picks the TOP displayed row, not the first store element', () => {
    // Sidebar shows newest-first (recent sort): 3, 2, 1.
    const store = setup(['slot-3', 'slot-2', 'slot-1'])
    fireEvent.keyDown(document, { code: 'Digit1', altKey: true })
    expect(store.getState().chat.activeSlot).toBe('slot-3')
  })

  it('Alt+2 picks the second displayed row', () => {
    const store = setup(['slot-3', 'slot-2', 'slot-1'])
    fireEvent.keyDown(document, { code: 'Digit2', altKey: true })
    expect(store.getState().chat.activeSlot).toBe('slot-2')
  })

  it('falls back to store order when the sidebar has not published (fresh store)', () => {
    const store = setup(undefined)
    fireEvent.keyDown(document, { code: 'Digit1', altKey: true })
    expect(store.getState().chat.activeSlot).toBe('slot-1')
  })

  it('Alt+ArrowRight steps to the next session in sidebar order, wrapping', () => {
    const store = setup(['slot-3', 'slot-2', 'slot-1'], 'slot-1')
    // slot-1 is the LAST displayed row; stepping forward wraps to the top row.
    fireEvent.keyDown(document, { code: 'ArrowRight', altKey: true })
    expect(store.getState().chat.activeSlot).toBe('slot-3')
  })
})

describe('letter jumps reach sessions 10+', () => {
  const manySlots = Array.from({ length: 12 }, (_, i) => ({
    key: `slot-${i + 1}`, title: `S${i + 1}`, messages: 0, running: false,
  }))
  const order = manySlots.map(s => s.key)

  function setupMany() {
    const chatInitial = chatReducer(undefined, { type: '@@test/init' })
    const dashInitial = dashboardReducer(undefined, { type: '@@test/init' })
    const store = createTestStore({
      dashboard: { ...dashInitial, slots: manySlots, sidebarOrder: order } as RootState['dashboard'],
      chat: { ...chatInitial, activeSlot: null, slotHistory: [] } as RootState['chat'],
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() }),
      { store },
    )
    return store
  }

  it('the letter sequence skips every letter another chord owns', () => {
    const letters = jumpLetters()
    for (const reserved of ['a', 'c', 'd', 'f', 'g', 'k', 'n', 'p', 's', 't', 'w']) {
      expect(letters).not.toContain(reserved)
    }
    // First letters after the exclusions: b, e, h (a = list select-all,
    // c = panel nav, d = split pane, f = find-in-page owners).
    expect(letters.slice(0, 3)).toEqual(['b', 'e', 'h'])
    expect(jumpLabelFor(8)).toBe('9')
    expect(jumpLabelFor(9)).toBe('b')
    expect(jumpLabelFor(11)).toBe('h')
    expect(jumpLabelFor(9 + letters.length)).toBeNull()
    expect(jumpIndexForCode('KeyB')).toBe(9)
    expect(jumpIndexForCode('KeyC')).toBe(-1)
    // Ctrl/Cmd+A (list select-all) and Ctrl/Cmd+D (split pane) own their
    // letters: never jump indices, on any platform.
    expect(jumpIndexForCode('KeyA')).toBe(-1)
    expect(jumpIndexForCode('KeyD')).toBe(-1)
    // Ctrl/Cmd+F (find), Ctrl/Cmd+T/W (file-explorer tabs) own theirs too —
    // all reach hasCommandModifier via plain Ctrl on macOS (XOR modifier).
    expect(jumpIndexForCode('KeyF')).toBe(-1)
    expect(jumpIndexForCode('KeyT')).toBe(-1)
    expect(jumpIndexForCode('KeyW')).toBe(-1)
    expect(jumpIndexForCode('Digit1')).toBe(0)
  })

  it('excludes every letter a pre-panel code reserves, so a future panel chord is never shadowed', () => {
    // The pre-panel exclusions are derived from RESERVED_PANEL_CODES, not
    // restated, so a single-letter Key* code added there later drops its jump
    // letter automatically. Today: KeyC/KeyK/KeyN/KeyP/KeyS.
    const letters = jumpLetters()
    const reservedLetters = [...RESERVED_PANEL_CODES]
      .map(code => /^Key([A-Z])$/.exec(code)?.[1].toLowerCase())
      .filter((letter): letter is string => letter !== undefined)
    // Pin today's baseline so a shared regex/source bug can't pass by re-deriving
    // the same wrong answer here and in the code under test.
    expect([...reservedLetters].sort()).toEqual(['c', 'k', 'n', 'p', 's'])
    for (const letter of reservedLetters) {
      expect(letters).not.toContain(letter)
      expect(jumpIndexForCode('Key' + letter.toUpperCase())).toBe(-1)
    }
  })

  it('Alt+B picks the 10th displayed row', () => {
    const store = setupMany()
    fireEvent.keyDown(document, { code: 'KeyB', altKey: true })
    expect(store.getState().chat.activeSlot).toBe('slot-10')
  })

  it('Alt+H picks the 12th displayed row (letter sequence skips excluded letters)', () => {
    const store = setupMany()
    fireEvent.keyDown(document, { code: 'KeyH', altKey: true })
    expect(store.getState().chat.activeSlot).toBe('slot-12')
  })

  it('letter jumps fire even while focus is in an input on non-Mac (session switch autofocuses the composer — chained jumps must survive it)', () => {
    // jsdom has IS_MAC=false (same convention as the Ctrl+digit gating test
    // above): this pins the Windows/Linux behavior. On macOS legacy Option
    // mode the (!isInput || !IS_MAC) guard keeps letters input-gated, because
    // Option+letter composes characters (Option+A = å).
    const store = setupMany()
    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    try {
      textarea.focus()
      fireEvent.keyDown(textarea, { code: 'KeyB', altKey: true, bubbles: true })
      // 'b' is the first letter target (index 9) — the 10th displayed row.
      expect(store.getState().chat.activeSlot).toBe('slot-10')
    } finally {
      textarea.remove()
    }
  })

  it('letter jumps never fire from inside an embedded terminal (Alt+B/F are readline word motions on the shell line)', () => {
    const store = setupMany()
    const term = document.createElement('div')
    term.className = 'xterm'
    const helper = document.createElement('textarea')
    term.appendChild(helper)
    document.body.appendChild(term)
    try {
      helper.focus()
      fireEvent.keyDown(helper, { code: 'KeyB', altKey: true, bubbles: true })
      expect(store.getState().chat.activeSlot).toBeNull()
    } finally {
      term.remove()
    }
  })

  it('macOS: a keyboard jump releases the composer, so the NEXT chord fires without clicking the chat', () => {
    // The reported bug: switch via a chord -> the switch autofocuses the
    // composer -> on macOS the letter gate kills the next chord until the
    // user clicks the chat (manually releasing focus). A keyboard-driven
    // switch must do that release itself. kbSwitch reads isMacPlatform()
    // live, so setPlatform works here even though IS_MAC froze at load.
    const prevPlatform = navigator.platform
    Object.defineProperty(navigator, 'platform', { value: 'MacIntel', configurable: true })
    const store = setupMany()
    const composer = document.createElement('textarea')
    composer.setAttribute('data-composer-input', '')
    document.body.appendChild(composer)
    try {
      composer.focus()
      // Digit jumps fire from inside inputs on every platform.
      fireEvent.keyDown(composer, { code: 'Digit2', altKey: true, bubbles: true })
      expect(store.getState().chat.activeSlot).toBe('slot-2')
      // The switch released the composer: focus is out, and the one-shot
      // autofocus skip is armed for ChatInput's autoFocusKey effect.
      expect(document.activeElement).not.toBe(composer)
      expect(consumeComposerRelease()).toBe(true)
      // With focus released, the follow-up LETTER jump chains — the exact
      // press that used to be dead.
      fireEvent.keyDown(document.body, { code: 'KeyB', altKey: true, bubbles: true })
      expect(store.getState().chat.activeSlot).toBe('slot-10')
      expect(consumeComposerRelease()).toBe(true)
    } finally {
      composer.remove()
      Object.defineProperty(navigator, 'platform', { value: prevPlatform, configurable: true })
    }
  })

  it('macOS: a SAME-key jump (already-active session) neither blurs the composer nor arms the one-shot', () => {
    // Opus/Design finding on 5b24ef561: switchSlot(sameKey) produces no
    // autoFocusKey transition, so ChatInput's effect never consumes the
    // one-shot — an unconditional release would blur with no refocus and the
    // leaked flag would eat the NEXT pointer-driven switch's autofocus.
    const prevPlatform = navigator.platform
    Object.defineProperty(navigator, 'platform', { value: 'MacIntel', configurable: true })
    const store = setupMany()
    const composer = document.createElement('textarea')
    composer.setAttribute('data-composer-input', '')
    document.body.appendChild(composer)
    try {
      // Land on slot-2 first (real transition: arms the one-shot; consume it).
      fireEvent.keyDown(document.body, { code: 'Digit2', altKey: true, bubbles: true })
      expect(store.getState().chat.activeSlot).toBe('slot-2')
      expect(consumeComposerRelease()).toBe(true)
      // The real app autofocuses the composer after a switch — simulate.
      composer.focus()
      // Self-jump: Digit2 again while slot-2 is already active.
      fireEvent.keyDown(composer, { code: 'Digit2', altKey: true, bubbles: true })
      expect(store.getState().chat.activeSlot).toBe('slot-2')
      // No transition: focus stays put, and the one-shot is NOT armed.
      expect(document.activeElement).toBe(composer)
      expect(consumeComposerRelease()).toBe(false)
    } finally {
      composer.remove()
      Object.defineProperty(navigator, 'platform', { value: prevPlatform, configurable: true })
    }
  })

  it('non-Mac: a keyboard jump keeps composer focus (letters fire in inputs there — type-after-jump survives)', () => {
    const prevPlatform = navigator.platform
    Object.defineProperty(navigator, 'platform', { value: 'Win32', configurable: true })
    const store = setupMany()
    const composer = document.createElement('textarea')
    composer.setAttribute('data-composer-input', '')
    document.body.appendChild(composer)
    try {
      composer.focus()
      fireEvent.keyDown(composer, { code: 'Digit2', altKey: true, bubbles: true })
      expect(store.getState().chat.activeSlot).toBe('slot-2')
      expect(document.activeElement).toBe(composer)
      expect(consumeComposerRelease()).toBe(false)
    } finally {
      composer.remove()
      Object.defineProperty(navigator, 'platform', { value: prevPlatform, configurable: true })
    }
  })

  it('an excluded letter falls through to its owning chord (Alt+C = panel nav, no jump)', () => {
    const store = setupMany()
    fireEvent.keyDown(document, { code: 'KeyC', altKey: true })
    // Panel navigation handled it (navigate mock); no session switch happened.
    expect(store.getState().chat.activeSlot).toBeNull()
  })

  it('an UNMAPPED letter is not claimed — no preventDefault, so the browser keeps its own Alt+letter chords', () => {
    // Only 3 sessions: every letter (index 9+) is beyond the session list.
    const chatInitial = chatReducer(undefined, { type: '@@test/init' })
    const dashInitial = dashboardReducer(undefined, { type: '@@test/init' })
    const few = manySlots.slice(0, 3)
    const store = createTestStore({
      dashboard: { ...dashInitial, slots: few, sidebarOrder: few.map(s => s.key) } as RootState['dashboard'],
      chat: { ...chatInitial, activeSlot: null, slotHistory: [] } as RootState['chat'],
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() }),
      { store },
    )
    // Alt+E would be the 11th row — unmapped with 3 sessions. The event must
    // NOT be claimed (preventDefault not called): on Windows/Linux Alt+E
    // opens the browser menu, and swallowing it would kill a chord the
    // browser owns while jumping nowhere.
    const event = new KeyboardEvent('keydown', { code: 'KeyE', altKey: true, cancelable: true, bubbles: true })
    const claimed = !document.dispatchEvent(event)
    expect(claimed).toBe(false)
    expect(store.getState().chat.activeSlot).toBeNull()
    // A mapped digit in the same store IS still claimed (pre-existing behavior).
    const digitEvent = new KeyboardEvent('keydown', { code: 'Digit2', altKey: true, cancelable: true, bubbles: true })
    const digitClaimed = !document.dispatchEvent(digitEvent)
    expect(digitClaimed).toBe(true)
    expect(store.getState().chat.activeSlot).toBe('slot-2')
  })
})

describe('useDigitModifierHeld', () => {
  it('tracks the Alt key on non-Mac and clears on blur', () => {
    const { result } = renderHookWithProviders(() => useDigitModifierHeld())
    expect(result.current).toBe(false)
    act(() => { fireEvent.keyDown(window, { altKey: true, location: 1 }) })
    expect(result.current).toBe(true)
    act(() => { fireEvent.keyUp(window, { altKey: false }) })
    expect(result.current).toBe(false)
    // Alt+Tab steals the keyup: blur must clear the held state.
    act(() => { fireEvent.keyDown(window, { altKey: true, location: 1 }) })
    expect(result.current).toBe(true)
    act(() => { fireEvent.blur(window) })
    expect(result.current).toBe(false)
  })

  it('ignores non-modifier keys', () => {
    const { result } = renderHookWithProviders(() => useDigitModifierHeld())
    act(() => { fireEvent.keyDown(window, { ctrlKey: true, location: 1 }) })
    expect(result.current).toBe(false)
  })
})

describe('useKeyboardShortcuts — stop speaking (Escape)', () => {
  const onToggleShortcutsModal = vi.fn()
  const onNewChat = vi.fn()

  function setup(opts: { voicePlaying?: boolean; enabled?: boolean; disabled?: boolean } = {}) {
    if (opts.enabled === false) localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const store = createTestStore({
      dashboard: { slots: [] } as unknown as RootState['dashboard'],
      chat: { activeSlot: null, slotHistory: [], voicePlaying: opts.voicePlaying ?? false } as unknown as RootState['chat'],
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal, onNewChat, disabled: opts.disabled }),
      { store },
    )
    return store
  }

  beforeEach(() => { onToggleShortcutsModal.mockClear(); onNewChat.mockClear() })

  // Capture-phase Escape: dispatched on document so the hook's capture listener fires.
  function pressEscape() {
    act(() => { fireEvent.keyDown(document, { key: 'Escape' }) })
  }

  it('fires voice-stop on Escape while speaking', () => {
    const spy = vi.fn()
    window.addEventListener('voice-stop', spy)
    setup({ voicePlaying: true })
    pressEscape()
    window.removeEventListener('voice-stop', spy)
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('does nothing on Escape when not speaking', () => {
    const spy = vi.fn()
    window.addEventListener('voice-stop', spy)
    setup({ voicePlaying: false })
    pressEscape()
    window.removeEventListener('voice-stop', spy)
    expect(spy).not.toHaveBeenCalled()
  })

  it('does nothing when shortcuts are globally disabled', () => {
    const spy = vi.fn()
    window.addEventListener('voice-stop', spy)
    setup({ voicePlaying: true, enabled: false })
    pressEscape()
    window.removeEventListener('voice-stop', spy)
    expect(spy).not.toHaveBeenCalled()
  })

  it('fires voice-stop even while the shortcuts modal is open (disabled=true)', () => {
    const spy = vi.fn()
    window.addEventListener('voice-stop', spy)
    setup({ voicePlaying: true, disabled: true })
    pressEscape()
    window.removeEventListener('voice-stop', spy)
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('advertises stop-speaking in DEFAULT_SHORTCUTS', () => {
    const def = DEFAULT_SHORTCUTS.find(s => s.id === 'stop-speaking')
    expect(def).toBeDefined()
    expect(def?.key).toBe('Escape')
    expect(def?.group).toBe('actions')
  })
})
