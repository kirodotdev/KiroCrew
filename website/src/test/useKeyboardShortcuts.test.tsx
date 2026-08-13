import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, act } from '@testing-library/react'
import { DEFAULT_SHORTCUTS, formatShortcut, SHORTCUTS_ENABLED_KEY, SHORTCUTS_ENABLED_EVENT, useKeyboardShortcuts, sessionCycleStep, wrapIndex, isAgentMonitorChord, RESERVED_PANEL_CODES, orderSlotsBySidebar, useDigitModifierHeld } from '../hooks/useKeyboardShortcuts'
import { renderHookWithProviders, createTestStore, renderWithProviders } from './helpers'
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
