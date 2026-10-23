import { act, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SessionSwitcher, {
  projectFolderName,
  SWITCHER_SHOW_DELAY_MS,
} from '../components/SessionSwitcher'
import {
  SHORTCUTS_ENABLED_EVENT,
  useKeyboardShortcuts,
} from '../hooks/useKeyboardShortcuts'
import {
  cancelSessionSwitcher,
  getSessionSwitcher,
  heldModifiers,
  openSessionSwitcher,
  sessionSwitcherOrder,
} from '../lib/sessionSwitcher'
import type { ChatFolder, ChatSlot } from '../types'
import { createTestStore, renderHookWithProviders, renderWithProviders } from './helpers'

function dispatchKeyDown(init: KeyboardEventInit): boolean {
  const event = new KeyboardEvent('keydown', {
    bubbles: true,
    cancelable: true,
    ...init,
  })
  return !document.dispatchEvent(event)
}

function dispatchKeyUp(key: string): boolean {
  const event = new KeyboardEvent('keyup', { key, bubbles: true, cancelable: true })
  act(() => {
    document.dispatchEvent(event)
  })
  return event.defaultPrevented
}

function openSwitcher(
  keys = ['a', 'b', 'c', 'd'],
  options: { backwards?: boolean } = {},
) {
  const onCommit = vi.fn()
  let opened = false
  act(() => {
    opened = openSessionSwitcher({
      keys,
      holdKeys: ['Alt'],
      stepKey: '`',
      onCommit,
      ...options,
    })
  })
  return { onCommit, opened }
}

function setPlatform(platform: string): void {
  Object.defineProperty(navigator, 'platform', { value: platform, configurable: true })
}

afterEach(() => {
  act(() => cancelSessionSwitcher())
})

describe('sessionSwitcherOrder', () => {
  it('orders the active session, history newest-first, then remaining sidebar rows', () => {
    expect(sessionSwitcherOrder(['a', 'b', 'c', 'd', 'e'], 'c', ['a', 'd', 'b']))
      .toEqual(['c', 'b', 'd', 'a', 'e'])
  })

  it('drops closed and duplicate history entries', () => {
    expect(sessionSwitcherOrder(['a', 'b'], 'a', ['gone', 'b', 'a', 'b']))
      .toEqual(['a', 'b'])
  })

  it('caps the card grid at five sessions', () => {
    const keys = Array.from({ length: 9 }, (_, index) => `s${index}`)
    expect(sessionSwitcherOrder(keys, 's8', keys.slice(0, 8))).toEqual([
      's8', 's7', 's6', 's5', 's4',
    ])
  })
})

describe('session switcher gesture', () => {
  it('opens on the previous session and reverses from the far end', () => {
    expect(openSwitcher().opened).toBe(true)
    expect(getSessionSwitcher()?.index).toBe(1)
    act(() => cancelSessionSwitcher())
    openSwitcher(undefined, { backwards: true })
    expect(getSessionSwitcher()?.index).toBe(3)
  })

  it('does not open when only one session is available', () => {
    expect(openSwitcher(['only']).opened).toBe(false)
    expect(getSessionSwitcher()).toBeNull()
  })

  it('steps in both directions and wraps at each end', () => {
    openSwitcher()
    expect(dispatchKeyDown({ key: '`', code: 'Backquote', altKey: true })).toBe(true)
    expect(getSessionSwitcher()?.index).toBe(2)
    act(() => {
      dispatchKeyDown({ key: 'Tab', code: 'Tab', altKey: true })
      dispatchKeyDown({ key: 'Tab', code: 'Tab', altKey: true })
    })
    expect(getSessionSwitcher()?.index).toBe(0)
    act(() => {
      dispatchKeyDown({ key: 'Tab', code: 'Tab', altKey: true, shiftKey: true })
    })
    expect(getSessionSwitcher()?.index).toBe(3)
  })

  it('moves with every arrow direction', () => {
    openSwitcher()
    act(() => {
      dispatchKeyDown({ key: 'ArrowDown', code: 'ArrowDown', altKey: true })
      dispatchKeyDown({ key: 'ArrowRight', code: 'ArrowRight', altKey: true })
    })
    expect(getSessionSwitcher()?.index).toBe(3)
    act(() => {
      dispatchKeyDown({ key: 'ArrowUp', code: 'ArrowUp', altKey: true })
      dispatchKeyDown({ key: 'ArrowLeft', code: 'ArrowLeft', altKey: true })
    })
    expect(getSessionSwitcher()?.index).toBe(1)
  })

  it('commits on modifier release and normalizes AltGraph', () => {
    const first = openSwitcher()
    act(() => {
      dispatchKeyDown({ key: '`', code: 'Backquote', altKey: true })
    })
    expect(dispatchKeyUp('Shift')).toBe(false)
    expect(dispatchKeyUp('Alt')).toBe(true)
    expect(first.onCommit).toHaveBeenCalledWith('c')

    const second = openSwitcher()
    expect(dispatchKeyUp('AltGraph')).toBe(true)
    expect(second.onCommit).toHaveBeenCalledWith('b')
  })

  it('commits on Enter and cancels on Escape', () => {
    const enter = openSwitcher()
    expect(dispatchKeyDown({ key: 'Enter', code: 'Enter', altKey: true })).toBe(true)
    expect(enter.onCommit).toHaveBeenCalledWith('b')

    const escape = openSwitcher()
    expect(dispatchKeyDown({ key: 'Escape', code: 'Escape', altKey: true })).toBe(true)
    expect(getSessionSwitcher()).toBeNull()
    dispatchKeyUp('Alt')
    expect(escape.onCommit).not.toHaveBeenCalled()
  })

  it('cancels on another key, window blur, or tab visibility change', () => {
    const other = openSwitcher()
    expect(dispatchKeyDown({ key: '3', code: 'Digit3', altKey: true })).toBe(false)
    expect(other.onCommit).not.toHaveBeenCalled()

    const blurred = openSwitcher()
    act(() => window.dispatchEvent(new Event('blur')))
    expect(blurred.onCommit).not.toHaveBeenCalled()

    const hidden = openSwitcher()
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    expect(hidden.onCommit).not.toHaveBeenCalled()
    expect(getSessionSwitcher()).toBeNull()
  })

  it('ignores a dead-key/IME keystroke (no usable token) instead of cancelling', () => {
    openSwitcher()
    const indexBefore = getSessionSwitcher()?.index
    // code '' + key '' is what a bare compose/dead keystroke reports: no token.
    expect(dispatchKeyDown({ key: '', code: '', altKey: true })).toBe(false)
    expect(getSessionSwitcher()).not.toBeNull()
    expect(getSessionSwitcher()?.index).toBe(indexBefore)
  })

  it('names held modifiers as their keyup events report them', () => {
    expect(heldModifiers({ altKey: true, ctrlKey: false, metaKey: true }))
      .toEqual(['Alt', 'Meta'])
  })
})

describe('native shortcut integration', () => {
  const slots: ChatSlot[] = [
    { key: 's1', title: 'Alpha', messages: 1, running: false },
    { key: 's2', title: 'Beta', messages: 1, running: false },
    { key: 's3', title: 'Gamma', messages: 1, running: false },
  ]

  function setup() {
    const initial = createTestStore().getState()
    const store = createTestStore({
      dashboard: { ...initial.dashboard, slots, sidebarOrder: ['s1', 's2', 's3'] },
      chat: { ...initial.chat, activeSlot: 's3', slotHistory: ['s1', 's2'] },
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() }),
      { store },
    )
    return store
  }

  beforeEach(() => {
    localStorage.clear()
    setPlatform('Win32')
  })

  it('reuses the existing chat-mru chord — no separate chat-switcher entry', async () => {
    const { shortcutEntry } = await import('../lib/shortcutRegistry')
    expect(shortcutEntry('chat-switcher')).toBeUndefined()
    const mru = shortcutEntry('chat-mru')
    expect(mru?.dispatch).toBe('code')
    expect(mru?.defaults.other).toEqual({ key: '`', alt: true })
  })

  it('opens the overlay in MRU order on Alt+Backquote and switches on release', () => {
    const store = setup()
    act(() => {
      dispatchKeyDown({ key: '`', code: 'Backquote', altKey: true })
    })
    expect(getSessionSwitcher()?.keys).toEqual(['s3', 's2', 's1'])
    // Opens with the last-visited card selected (index 1), so a bare release
    // commits it — reproducing the instant toggle.
    dispatchKeyUp('Alt')
    expect(store.getState().chat.activeSlot).toBe('s2')
  })

  it('opens with the macOS chord (Ctrl+Opt+Backquote), including from the composer', () => {
    setPlatform('MacIntel')
    setup()
    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    textarea.focus()
    act(() => {
      textarea.dispatchEvent(new KeyboardEvent('keydown', {
        key: '`', code: 'Backquote', altKey: true, ctrlKey: true, bubbles: true, cancelable: true,
      }))
    })
    expect(getSessionSwitcher()?.keys).toEqual(['s3', 's2', 's1'])
    dispatchKeyUp('Alt')
    textarea.remove()
  })

  it('stays closed while shortcuts are disabled or a modal is open', () => {
    setup()
    localStorage.setItem('mc-keyboard-shortcuts', '0')
    act(() => window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT)))
    act(() => {
      dispatchKeyDown({ key: '`', code: 'Backquote', altKey: true })
    })
    expect(getSessionSwitcher()).toBeNull()
    localStorage.removeItem('mc-keyboard-shortcuts')
    act(() => window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT)))
  })

  it('opens with two sessions and commits the last visited on release', () => {
    const initial = createTestStore().getState()
    const store = createTestStore({
      dashboard: { ...initial.dashboard, slots: slots.slice(0, 2), sidebarOrder: ['s1', 's2'] },
      chat: { ...initial.chat, activeSlot: 's2', slotHistory: ['s1'] },
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() }),
      { store },
    )
    act(() => {
      dispatchKeyDown({ key: '`', code: 'Backquote', altKey: true })
    })
    expect(getSessionSwitcher()?.keys).toEqual(['s2', 's1'])
    dispatchKeyUp('Alt')
    expect(store.getState().chat.activeSlot).toBe('s1')
  })
})

describe('SessionSwitcher cards', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  function renderSwitcher() {
    const initial = createTestStore().getState()
    const store = createTestStore({
      dashboard: {
        ...initial.dashboard,
        slots: [
          {
            key: 'a', title: 'Current chat', messages: 1, running: false,
            agent: 'kirocrew', folder_id: 'pipelines',
          },
          {
            key: 'b', title: 'Pipeline triage', messages: 1, running: true,
            project: '/workplace/example/PaymentsService',
          },
        ],
        unreadSlots: [],
      },
    })
    const rendered = renderWithProviders(<SessionSwitcher />, {
      store,
      queryDefaults: { staleTime: Number.POSITIVE_INFINITY },
    })
    const folders: ChatFolder[] = [
      { id: 'work', name: 'Work', order: 0 },
      { id: 'pipelines', name: 'Pipelines', order: 0, parent_id: 'work' },
    ]
    act(() => rendered.queryClient.setQueryData(['chat-folders'], folders))
    return rendered
  }

  it('keeps a quick switch hidden, then renders the approved card context on hold', () => {
    renderSwitcher()
    const quick = openSwitcher(['a', 'b'])
    dispatchKeyUp('Alt')
    expect(quick.onCommit).toHaveBeenCalledWith('b')
    expect(screen.queryByRole('listbox')).toBeNull()

    openSwitcher(['a', 'b'])
    act(() => vi.advanceTimersByTime(SWITCHER_SHOW_DELAY_MS))
    const options = screen.getAllByRole('option')
    expect(options).toHaveLength(2)
    expect(options[0]).toHaveTextContent('Current chat')
    expect(options[0]).toHaveTextContent('Work › Pipelines')
    expect(options[0]).toHaveTextContent('kirocrew')
    expect(options[0]).toHaveAttribute('aria-current', 'true')
    expect(options[1]).toHaveTextContent('PaymentsService')
    expect(options[1]).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('5 most recent')).toBeInTheDocument()
  })

  it('tracks pointer selection and opens the selected card', () => {
    renderSwitcher()
    const { onCommit } = openSwitcher(['a', 'b'])
    act(() => vi.advanceTimersByTime(SWITCHER_SHOW_DELAY_MS))
    const options = screen.getAllByRole('option')
    fireEvent.mouseMove(options[0])
    expect(options[0]).toHaveAttribute('aria-selected', 'true')
    fireEvent.mouseDown(options[1], { button: 0 })
    expect(onCommit).toHaveBeenCalledWith('b')
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('extracts project folder names across path styles', () => {
    expect(projectFolderName('/workplace/example/PaymentsService/')).toBe('PaymentsService')
    expect(projectFolderName('C:\\work\\Orders')).toBe('Orders')
    expect(projectFolderName(undefined)).toBe('')
  })
})
