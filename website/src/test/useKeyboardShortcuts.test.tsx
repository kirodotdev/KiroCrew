import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, act } from '@testing-library/react'
import { DEFAULT_SHORTCUTS, formatShortcut, SHORTCUTS_ENABLED_KEY, SHORTCUTS_ENABLED_EVENT, useKeyboardShortcuts, sessionCycleStep, wrapIndex } from '../hooks/useKeyboardShortcuts'
import { renderHookWithProviders, createTestStore, renderWithProviders } from './helpers'
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
      expect(formatShortcut({ id: 't', key: 'ArrowLeft', alt: true, label: '', group: 'Chat Navigation' })).toBe('Alt + \u2190')
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
    expect(groups).toContain('Chat Navigation')
    expect(groups).toContain('Panel Navigation')
    expect(groups).toContain('Actions')
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
    expect(prev).toMatchObject({ key: '[', meta: true, group: 'Chat Navigation' })
    expect(next).toMatchObject({ key: ']', meta: true, group: 'Chat Navigation' })
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
