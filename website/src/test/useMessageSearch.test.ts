import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useMessageSearch } from '../hooks/useMessageSearch'

describe('useMessageSearch', () => {
  it('initial state', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    expect(result.current.isOpen).toBe(false)
    expect(result.current.focusNonce).toBe(0)
  })

  it('open() sets isOpen=true', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    act(() => result.current.open())
    expect(result.current.isOpen).toBe(true)
    // A button or menu open focuses the field as the shortcut does.
    expect(result.current.focusNonce).toBe(1)
  })

  it('close() closes it', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    act(() => result.current.open())
    act(() => result.current.close())
    expect(result.current.isOpen).toBe(false)
  })

  it('Ctrl/Cmd+F opens the search and bumps focusNonce (select-all trigger)', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    const before = result.current.focusNonce
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', ctrlKey: true })) })
    expect(result.current.isOpen).toBe(true)
    expect(result.current.focusNonce).toBe(before + 1)
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true })) })
    expect(result.current.focusNonce).toBe(before + 2)
  })

  it('Ctrl/Cmd+Shift+F is left to Search all chats, even with a lower-case key', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    const shifted = (init: KeyboardEventInit) => new KeyboardEvent('keydown', { key: 'f', shiftKey: true, cancelable: true, ...init })
    const ctrl = shifted({ ctrlKey: true })
    const meta = shifted({ metaKey: true })
    act(() => { document.dispatchEvent(ctrl); document.dispatchEvent(meta) })
    expect(result.current.isOpen).toBe(false)
    expect(ctrl.defaultPrevented || meta.defaultPrevented).toBe(false)
  })

  it('resets on activeSlot change', () => {
    const { result, rerender } = renderHook(
      ({ slot }) => useMessageSearch(slot),
      { initialProps: { slot: 'slot-1' as string | null } },
    )
    act(() => result.current.open())
    expect(result.current.isOpen).toBe(true)
    rerender({ slot: 'slot-2' })
    expect(result.current.isOpen).toBe(false)
  })
})

describe('useMessageSearch when the host does not offer it', () => {
  it('leaves the key to the browser while the host does not offer the search', () => {
    const { result } = renderHook(() => useMessageSearch('member-bot', false))
    const event = new KeyboardEvent('keydown', { key: 'f', ctrlKey: true, cancelable: true })
    act(() => { document.dispatchEvent(event) })
    expect(result.current.isOpen).toBe(false)
    expect(event.defaultPrevented).toBe(false)
  })

  it('closes an open search when the host stops offering it, leaving focus where the user moved it', async () => {
    // An earlier case's close() hands focus over one frame later; let that
    // frame pass before a composer exists to receive it.
    await new Promise<void>(r => requestAnimationFrame(() => r()))
    const other = document.createElement('input')
    const composer = document.createElement('textarea')
    composer.setAttribute('data-composer-input', '')
    document.body.append(other, composer)
    try {
      const { result, rerender } = renderHook(({ on }) => useMessageSearch('member-bot', on), { initialProps: { on: true } })
      act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', ctrlKey: true })) })
      expect(result.current.isOpen).toBe(true)
      other.focus()
      rerender({ on: false })
      await new Promise<void>(r => requestAnimationFrame(() => r()))
      expect(result.current.isOpen).toBe(false)
      // The user just moved into a side panel or drawer: pulling the caret back
      // into the composer would take their focus away.
      expect(document.activeElement).toBe(other)
    } finally {
      other.remove()
      composer.remove()
    }
  })
})

describe('useMessageSearch keyboard shortcuts', () => {
  // Escape closes the search, and close() hands focus to the composer one frame
  // later. On fake timers that frame belongs to this suite's clock, which each
  // test restores, so it never lands in a later test that mounts a composer.
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('Cmd+F opens search', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true })) })
    expect(result.current.isOpen).toBe(true)
  })

  it('Ctrl+F opens search', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', ctrlKey: true })) })
    expect(result.current.isOpen).toBe(true)
  })

  it('Escape closes search when isOpen', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    act(() => result.current.open())
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
    expect(result.current.isOpen).toBe(false)
  })

  it('Ctrl+Cmd+F is left to the OS (macOS Toggle Full Screen), not captured', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    const e = new KeyboardEvent('keydown', { key: 'f', metaKey: true, ctrlKey: true, cancelable: true })
    act(() => { document.dispatchEvent(e) })
    // Both modifiers held is an OS chord: the search must stay shut AND the event
    // must not be consumed, or the application menu never receives it.
    expect(result.current.isOpen).toBe(false)
    expect(e.defaultPrevented).toBe(false)
  })

  it('an Escape a menu inside the search already answered leaves the search open', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    act(() => result.current.open())
    // A Radix menu closing on Escape prevents the default before this
    // document listener runs; that Escape belongs to the menu.
    const answered = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true })
    answered.preventDefault()
    act(() => { document.dispatchEvent(answered) })
    expect(result.current.isOpen).toBe(true)
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true })) })
    expect(result.current.isOpen).toBe(false)
  })

  it('Escape does nothing when not open', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
    expect(result.current.isOpen).toBe(false)
  })

  it('Ctrl/Cmd+F is ignored when the keystroke originates inside the file explorer (.mc-fe-root)', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    // Simulate an in-file search: target lives inside a .mc-fe-root subtree.
    const root = document.createElement('div')
    root.className = 'mc-fe-root'
    const inner = document.createElement('input')
    root.appendChild(inner)
    document.body.appendChild(root)
    try {
      act(() => { inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true })) })
      // Chat search must NOT open — the file explorer owns Cmd+F here.
      expect(result.current.isOpen).toBe(false)
    } finally {
      document.body.removeChild(root)
    }
  })

  it('Ctrl/Cmd+F is ignored when another surface already answered the chord', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    // Pierre's editor binds cmdOrCtrl+f on its own content element and calls
    // preventDefault() without stopPropagation(), so this document-level
    // handler still sees the event. Opening chat search here would stack a
    // transcript-scoped search on top of the editor's own find panel.
    const inner = document.createElement('div')
    document.body.appendChild(inner)
    inner.addEventListener('keydown', e => { e.preventDefault() })
    try {
      act(() => {
        inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true, cancelable: true }))
      })
      expect(result.current.isOpen).toBe(false)
    } finally {
      document.body.removeChild(inner)
    }
  })

  it('still opens chat search when no other surface claimed the chord', () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    // Control for the case above: an unclaimed chord must still reach chat
    // search, so the bail cannot be satisfied by an always-yield handler.
    const inner = document.createElement('div')
    document.body.appendChild(inner)
    try {
      act(() => {
        inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true, cancelable: true }))
      })
      expect(result.current.isOpen).toBe(true)
    } finally {
      document.body.removeChild(inner)
    }
  })
})

describe('close() hands focus back to the composer', () => {
  // Real timers here: focusComposer defers with requestAnimationFrame, driven
  // by the same flushFrame recipe as composerFocus.test.ts.
  const flushFrame = async () => {
    await Promise.resolve()
    await new Promise<void>(r => requestAnimationFrame(() => r()))
    await Promise.resolve()
  }

  let composer: HTMLTextAreaElement

  beforeEach(() => {
    composer = document.createElement('textarea')
    // The stable probe focusComposer resolves through; the aria-label is
    // translated at runtime and deliberately not part of the lookup.
    composer.setAttribute('data-composer-input', '')
    document.body.appendChild(composer)
  })
  afterEach(() => { composer.remove() })

  it('closing an open search restores focus to the composer', async () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    act(() => result.current.open())
    act(() => result.current.close())
    await flushFrame()
    expect(document.activeElement).toBe(composer)
  })

  it('Escape hands typing back to the composer', async () => {
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    act(() => result.current.open())
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
    await flushFrame()
    expect(result.current.isOpen).toBe(false)
    expect(document.activeElement).toBe(composer)
  })

  it('close() on an already-closed search does not steal focus', async () => {
    // ChatPage's file/folder-open handlers call close() unconditionally to
    // un-gate the dock panel; a close that dismissed nothing must not yank
    // the caret away from wherever the user is typing.
    const other = document.createElement('input')
    document.body.appendChild(other)
    other.focus()
    try {
      const { result } = renderHook(() => useMessageSearch('slot-1'))
      act(() => result.current.close())
      await flushFrame()
      expect(document.activeElement).toBe(other)
    } finally {
      other.remove()
    }
  })

  it('closing never throws when the composer is not mounted', async () => {
    composer.remove()
    const { result } = renderHook(() => useMessageSearch('slot-1'))
    act(() => result.current.open())
    act(() => result.current.close())
    await expect(flushFrame()).resolves.toBeUndefined()
    expect(result.current.isOpen).toBe(false)
  })

  it('session-switch reset still closes the search without focusing the composer', async () => {
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string }) => useMessageSearch(slot),
      { initialProps: { slot: 'slot-1' } },
    )
    act(() => result.current.open())
    rerender({ slot: 'slot-2' })
    await flushFrame()
    expect(result.current.isOpen).toBe(false)
    expect(document.activeElement).not.toBe(composer)
  })
})
