import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useMessageSearch } from '../hooks/useMessageSearch'
import type { ChatMessage } from '../types'

const msg = (role: string, content: string): ChatMessage => ({ role, content, cls: '' })

const messages: ChatMessage[] = [
  msg('user', 'hello world'),           // 0 — 1 occurrence of 'world'
  msg('assistant', 'hi there world'),   // 1 — 1 occurrence, but followed by tool → skipped (reasoning)
  msg('tool', 'tool output with world'),// 2 — tool, always skipped
  msg('user', 'another message'),       // 3 — matches 'another'
  msg('assistant', 'world world again'),// 4 — 2 occurrences of 'world'
  msg('thinking', 'thinking about world'), // 5 — thinking, always skipped
]

describe('useMessageSearch', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('initial state', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    expect(result.current.isOpen).toBe(false)
    expect(result.current.term).toBe('')
    expect(result.current.matches).toEqual([])
    expect(result.current.currentIdx).toBe(0)
  })

  it('open() sets isOpen=true', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.open())
    expect(result.current.isOpen).toBe(true)
  })

  it('close() clears everything', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => { result.current.open(); result.current.setTerm('world') })
    act(() => { vi.advanceTimersByTime(50) })
    act(() => result.current.close())
    expect(result.current.isOpen).toBe(false)
    expect(result.current.term).toBe('')
    expect(result.current.matches).toEqual([])
  })

  it('setTerm updates term immediately', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.setTerm('hello'))
    expect(result.current.term).toBe('hello')
  })

  it('matches computed after 50ms debounce', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.setTerm('world'))
    expect(result.current.matches).toEqual([]) // not yet
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches.length).toBeGreaterThan(0)
  })

  it('matches are per-occurrence, not per-message', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.setTerm('world'))
    act(() => { vi.advanceTimersByTime(50) })
    // msg 0: 1 occurrence, msg 1: skipped (reasoning), msg 4: 2 occurrences = 3 total
    expect(result.current.matches).toEqual([
      { msgIdx: 0, occ: 0 },
      { msgIdx: 4, occ: 0 },
      { msgIdx: 4, occ: 1 },
    ])
  })

  it('excludes assistant reasoning segments (followed by tool)', () => {
    const msgs = [
      msg('user', 'search term'),
      msg('assistant', 'reasoning with search term'),  // followed by tool → skipped
      msg('tool', '🔧 bash'),
      msg('assistant', 'conclusion with search term'),  // not followed by tool → included
    ]
    const { result } = renderHook(() => useMessageSearch(msgs, 'slot-1'))
    act(() => result.current.setTerm('search term'))
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches).toEqual([
      { msgIdx: 0, occ: 0 },
      { msgIdx: 3, occ: 0 },
    ])
  })

  it('case-insensitive by default', () => {
    const msgs = [msg('user', 'Hello HELLO hello')]
    const { result } = renderHook(() => useMessageSearch(msgs, 'slot-1'))
    act(() => result.current.setTerm('hello'))
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches).toEqual([
      { msgIdx: 0, occ: 0 },
      { msgIdx: 0, occ: 1 },
      { msgIdx: 0, occ: 2 },
    ])
  })

  it('toggleCaseSensitive flips and recomputes', () => {
    const msgs = [msg('user', 'Hello hello')]
    const { result } = renderHook(() => useMessageSearch(msgs, 'slot-1'))
    act(() => result.current.setTerm('hello'))
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches.length).toBe(2) // case-insensitive
    act(() => result.current.toggleCaseSensitive())
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches).toEqual([{ msgIdx: 0, occ: 0 }]) // case-sensitive: only lowercase
  })

  it('next() wraps around', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.setTerm('world'))
    act(() => { vi.advanceTimersByTime(50) })
    // 3 matches total
    expect(result.current.currentIdx).toBe(0)
    act(() => result.current.next())
    expect(result.current.currentIdx).toBe(1)
    act(() => result.current.next())
    expect(result.current.currentIdx).toBe(2)
    act(() => result.current.next())
    expect(result.current.currentIdx).toBe(0) // wrapped
  })

  it('prev() wraps around', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.setTerm('world'))
    act(() => { vi.advanceTimersByTime(50) })
    // 3 matches total
    expect(result.current.currentIdx).toBe(0)
    act(() => result.current.prev())
    expect(result.current.currentIdx).toBe(2) // wrapped to end
  })

  it('goTo() jumps to an arbitrary match', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.setTerm('world'))
    act(() => { vi.advanceTimersByTime(50) })
    // 3 matches total (indices 0,1,2)
    act(() => result.current.goTo(2))
    expect(result.current.currentIdx).toBe(2)
    act(() => result.current.goTo(1))
    expect(result.current.currentIdx).toBe(1)
  })

  it('goTo() clamps out-of-range indices', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.setTerm('world'))
    act(() => { vi.advanceTimersByTime(50) })
    act(() => result.current.goTo(99))
    expect(result.current.currentIdx).toBe(2) // clamped to last of 3 matches
    act(() => result.current.goTo(-5))
    expect(result.current.currentIdx).toBe(0) // clamped to first
  })

  it('goTo() is a no-op with zero matches', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.goTo(3))
    expect(result.current.currentIdx).toBe(0)
  })

  it('excludes trailing [OPTIONS:] block from search (rendered as buttons)', () => {
    const msgs: ChatMessage[] = [
      msg('assistant', 'Body has widget once.\n\n[OPTIONS: rebuild widget | another widget]'),
    ]
    const { result } = renderHook(() => useMessageSearch(msgs, 'slot-x'))
    act(() => result.current.setTerm('widget'))
    act(() => { vi.advanceTimersByTime(50) })
    // Only the body occurrence counts; the two inside OPTIONS are stripped.
    expect(result.current.matches).toHaveLength(1)
  })

  it('Ctrl/Cmd+F opens the bar and bumps focusNonce (select-all trigger)', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    const before = result.current.focusNonce
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', ctrlKey: true })) })
    expect(result.current.isOpen).toBe(true)
    expect(result.current.focusNonce).toBe(before + 1)
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true })) })
    expect(result.current.focusNonce).toBe(before + 2)
  })

  it('currentIdx clamped when matches shrink', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.setTerm('world'))
    act(() => { vi.advanceTimersByTime(50) })
    act(() => result.current.next())
    act(() => result.current.next())
    expect(result.current.currentIdx).toBe(2)
    act(() => result.current.setTerm('another'))
    act(() => { vi.advanceTimersByTime(50) })
    // only 1 match now, currentIdx should clamp
    expect(result.current.currentIdx).toBe(0)
  })

  it('currentMessageIdx and currentOccurrenceIdx track current match', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    expect(result.current.currentMessageIdx).toBe(-1)
    expect(result.current.currentOccurrenceIdx).toBe(-1)
    act(() => result.current.setTerm('world'))
    act(() => { vi.advanceTimersByTime(50) })
    // match 0: { msgIdx: 0, occ: 0 }
    expect(result.current.currentMessageIdx).toBe(0)
    expect(result.current.currentOccurrenceIdx).toBe(0)
    act(() => result.current.next())
    // match 1: { msgIdx: 4, occ: 0 }
    expect(result.current.currentMessageIdx).toBe(4)
    expect(result.current.currentOccurrenceIdx).toBe(0)
    act(() => result.current.next())
    // match 2: { msgIdx: 4, occ: 1 }
    expect(result.current.currentMessageIdx).toBe(4)
    expect(result.current.currentOccurrenceIdx).toBe(1)
  })

  it('resets on activeSlot change', () => {
    const { result, rerender } = renderHook(
      ({ slot }) => useMessageSearch(messages, slot),
      { initialProps: { slot: 'slot-1' as string | null } },
    )
    act(() => { result.current.open(); result.current.setTerm('world') })
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.isOpen).toBe(true)
    rerender({ slot: 'slot-2' })
    expect(result.current.isOpen).toBe(false)
    expect(result.current.term).toBe('')
    expect(result.current.matches).toEqual([])
  })

  it('empty term produces empty matches immediately', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.setTerm(''))
    expect(result.current.matches).toEqual([])
  })

  it('next() and prev() are no-ops when matches is empty', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.setTerm('xyznonexistent'))
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches).toEqual([])
    act(() => result.current.next())
    expect(result.current.currentIdx).toBe(0)
    act(() => result.current.prev())
    expect(result.current.currentIdx).toBe(0)
  })

  it('navigates through multiple occurrences within the same message', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.setTerm('world'))
    act(() => { vi.advanceTimersByTime(50) })
    // matches: [{ msgIdx: 0, occ: 0 }, { msgIdx: 4, occ: 0 }, { msgIdx: 4, occ: 1 }]
    act(() => result.current.next()) // → index 1: msgIdx 4, occ 0
    expect(result.current.currentMessageIdx).toBe(4)
    expect(result.current.currentOccurrenceIdx).toBe(0)
    act(() => result.current.next()) // → index 2: msgIdx 4, occ 1
    expect(result.current.currentMessageIdx).toBe(4)
    expect(result.current.currentOccurrenceIdx).toBe(1)
  })

  it('last assistant message at end of array is not skipped', () => {
    const msgs = [
      msg('user', 'hello'),
      msg('assistant', 'world at the end'),  // last message, no next — should NOT be skipped
    ]
    const { result } = renderHook(() => useMessageSearch(msgs, 'slot-1'))
    act(() => result.current.setTerm('world'))
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches).toEqual([{ msgIdx: 1, occ: 0 }])
  })
})

describe('useMessageSearch keyboard shortcuts', () => {
  it('Cmd+F opens search', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true })) })
    expect(result.current.isOpen).toBe(true)
  })

  it('Ctrl+F opens search', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', ctrlKey: true })) })
    expect(result.current.isOpen).toBe(true)
  })

  it('Escape closes search when isOpen', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.open())
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
    expect(result.current.isOpen).toBe(false)
  })

  it('Ctrl+Cmd+F is left to the OS (macOS Toggle Full Screen), not captured', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    const e = new KeyboardEvent('keydown', { key: 'f', metaKey: true, ctrlKey: true, cancelable: true })
    act(() => { document.dispatchEvent(e) })
    // Both modifiers held is an OS chord: the bar must stay shut AND the event
    // must not be consumed, or the application menu never receives it.
    expect(result.current.isOpen).toBe(false)
    expect(e.defaultPrevented).toBe(false)
  })

  it('Escape does nothing when not open', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
    expect(result.current.isOpen).toBe(false)
  })

  it('Ctrl/Cmd+F is ignored when the keystroke originates inside the file explorer (.mc-fe-root)', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
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
})

describe('close() hands focus back to the composer', () => {
  // Real timers here (this block never touches the debounce): focusComposer
  // defers with requestAnimationFrame, driven by the same flushFrame recipe
  // as composerFocus.test.ts.
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

  it('closing an open bar restores focus to the composer', async () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.open())
    act(() => result.current.close())
    await flushFrame()
    expect(document.activeElement).toBe(composer)
  })

  it('Escape hands typing back to the composer', async () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.open())
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
    await flushFrame()
    expect(result.current.isOpen).toBe(false)
    expect(document.activeElement).toBe(composer)
  })

  it('close() on an already-closed bar does not steal focus', async () => {
    // ChatPage's file/folder-open handlers call close() unconditionally to
    // un-gate the dock panel; a close that dismissed nothing must not yank
    // the caret away from wherever the user is typing.
    const other = document.createElement('input')
    document.body.appendChild(other)
    other.focus()
    try {
      const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
      act(() => result.current.close())
      await flushFrame()
      expect(document.activeElement).toBe(other)
    } finally {
      other.remove()
    }
  })

  it('closing never throws when the composer is not mounted', async () => {
    composer.remove()
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    act(() => result.current.open())
    act(() => result.current.close())
    await expect(flushFrame()).resolves.toBeUndefined()
    expect(result.current.isOpen).toBe(false)
  })

  it('session-switch reset still closes the bar without focusing the composer', async () => {
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string }) => useMessageSearch(messages, slot),
      { initialProps: { slot: 'slot-1' } },
    )
    act(() => result.current.open())
    rerender({ slot: 'slot-2' })
    await flushFrame()
    expect(result.current.isOpen).toBe(false)
    expect(document.activeElement).not.toBe(composer)
  })
})
