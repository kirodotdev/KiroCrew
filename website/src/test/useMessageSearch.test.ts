import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useMessageSearch } from '../hooks/useMessageSearch'
import { CONVERGE_MAX_MS } from '../utils/searchScroll'
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

  it('excludes a steering-only reply hidden by its policy-block turn', () => {
    const msgs = [
      msg('user', 'Find the visible result'),
      msg('assistant', 'The visible result is ready.'),
      msg('inject', '[Tool blocked — reason sent to the agent] policy'),
      msg('assistant', '[STEERING steer-ab12: hidden result acknowledgement]'),
    ]
    const { result } = renderHook(() => useMessageSearch(msgs, 'slot-x'))
    act(() => result.current.setTerm('result'))
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches).toEqual([
      { msgIdx: 0, occ: 0 },
      { msgIdx: 1, occ: 0 },
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

  it('Ctrl/Cmd+Shift+F is left to Search all chats, even with a lower-case key', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    const shifted = (init: KeyboardEventInit) => new KeyboardEvent('keydown', { key: 'f', shiftKey: true, cancelable: true, ...init })
    const ctrl = shifted({ ctrlKey: true })
    const meta = shifted({ metaKey: true })
    act(() => { document.dispatchEvent(ctrl); document.dispatchEvent(meta) })
    expect(result.current.isOpen).toBe(false)
    expect(ctrl.defaultPrevented || meta.defaultPrevented).toBe(false)
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
  // Escape closes the bar, and close() hands focus to the composer one frame
  // later. On fake timers that frame belongs to this suite's clock, which each
  // test restores, so it never lands in a later test that mounts a composer.
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

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

  it('Ctrl/Cmd+F is ignored when another surface already answered the chord', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    // Pierre's editor binds cmdOrCtrl+f on its own content element and calls
    // preventDefault() without stopPropagation(), so this document-level
    // handler still sees the event. Opening chat search here would stack a
    // transcript-scoped find on top of the editor's own find panel.
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
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
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

describe('useMessageSearch with no term', () => {
  // ChatPage owns this hook; a fresh [] on every `messages` change re-rendered
  // the page once per streamed chunk while the search bar was closed.
  it('keeps the same matches array while messages change and the term is empty', () => {
    let msgs = messages
    const { result, rerender } = renderHook(() => useMessageSearch(msgs, 'slot-1'))
    const first = result.current.matches
    msgs = [...messages, msg('assistant', 'streamed chunk')]
    rerender()
    msgs = [...msgs, msg('assistant', 'another chunk')]
    rerender()
    expect(result.current.matches).toBe(first)
  })
})

describe('useMessageSearch over a superseded steer', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('matches only the drawn successor row', () => {
    const rows: ChatMessage[] = [
      { role: 'user', content: 'check the logs', cls: '', meta: { mid: 'm-old', steer: true, steerState: 'superseded', supersededBy: 'm-new' } },
      { role: 'user', content: 'check the logs', cls: '', meta: { mid: 'm-new' } },
    ]
    const { result } = renderHook(() => useMessageSearch(rows, 'slot-1'))
    act(() => { result.current.open(); result.current.setTerm('logs') })
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches).toEqual([{ msgIdx: 1, occ: 0 }])
  })
})

describe('useMessageSearch over a superseded steer whose successor is cut off', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('still matches the old row, the only copy in the view', () => {
    const rows: ChatMessage[] = [
      { role: 'user', content: 'check the logs', cls: '', meta: { mid: 'm-old', steer: true, steerState: 'superseded', supersededBy: 'm-new' } },
    ]
    const { result } = renderHook(() => useMessageSearch(rows, 'slot-1'))
    act(() => { result.current.open(); result.current.setTerm('logs') })
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches).toEqual([{ msgIdx: 0, occ: 0 }])
  })
})

/* The current match is the match itself, named by its row's key: rows arriving
 * or leaving around it never turn it into another match, and only a find action
 * hands out a `reveal`, the number the hosts and rows move the reader by. */
describe('the current match while rows arrive and leave', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  // Server rows carry a `ts`, the key the transcript keys its rows by.
  const row = (minute: number, role: 'user' | 'assistant', content: string, meta?: Record<string, unknown>): ChatMessage => ({
    role, content, cls: '', ts: `2026-11-20T10:${String(minute).padStart(2, '0')}:00Z`, ...(meta ? { meta } : {}),
  })

  function searchOver(initial: ChatMessage[], term: string) {
    const hook = renderHook(({ msgs }) => useMessageSearch(msgs, 'slot-1'), { initialProps: { msgs: initial } })
    act(() => hook.result.current.setTerm(term))
    act(() => { vi.advanceTimersByTime(50) })
    return hook
  }

  it('an older page landing above keeps the same match current, from the render that draws the page, with no find action', () => {
    const newer = [row(10, 'user', 'deploy the API'), row(11, 'assistant', 'deployed at noon')]
    const older = [row(1, 'user', 'can we deploy friday?'), row(2, 'assistant', 'deploy is frozen')]
    const { result, rerender } = searchOver(newer, 'deploy')
    act(() => result.current.next())
    expect(result.current.currentMessageIdx).toBe(1)
    const reveal = result.current.reveal
    const rowKey = result.current.currentRowKey

    rerender({ msgs: [...older, ...newer] })
    // Before the search runs again, the match already names its row's new index.
    expect(result.current.currentMessageIdx).toBe(3)
    expect(result.current.currentOccurrenceIdx).toBe(0)
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches).toEqual([
      { msgIdx: 0, occ: 0 }, { msgIdx: 1, occ: 0 }, { msgIdx: 2, occ: 0 }, { msgIdx: 3, occ: 0 },
    ])
    expect(result.current.currentIdx).toBe(3)
    expect(result.current.currentMessageIdx).toBe(3)
    expect(rowKey).toBeTruthy()
    expect(result.current.currentRowKey).toBe(rowKey)
    expect(reveal).toBeGreaterThan(0)
    expect(result.current.reveal).toBe(reveal)
  })

  it('a row appended below that hides a steer above keeps the current match, and so does a plain reply', () => {
    const msgs = [
      row(1, 'user', 'check the deploy log'),
      row(2, 'assistant', 'the deploy log is clean'),
      row(3, 'user', 'deploy now', { steer: true, steerState: 'superseded', mid: 'm-steer', supersededBy: 'm-next' }),
      row(4, 'assistant', 'the deploy finished'),
    ]
    const { result, rerender } = searchOver(msgs, 'deploy')
    act(() => result.current.goTo(3))
    expect(result.current.currentMessageIdx).toBe(3)
    const reveal = result.current.reveal

    // The steer's own turn writes its row, so the steer is no longer drawn.
    const withSuccessor = [...msgs, row(5, 'user', 'deploy now', { mid: 'm-next' })]
    rerender({ msgs: withSuccessor })
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches.map(m => m.msgIdx)).toEqual([0, 1, 3, 4])
    expect(result.current.currentMessageIdx).toBe(3)
    expect(result.current.currentIdx).toBe(2)

    rerender({ msgs: [...withSuccessor, row(6, 'assistant', 'deploy is live')] })
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches).toHaveLength(5)
    expect(result.current.currentMessageIdx).toBe(3)
    expect(result.current.reveal).toBe(reveal)
  })

  it('rows that share a timestamp keep their own matches across a page landing', () => {
    // A coarse clock stamps two rows alike; their order tells them apart.
    const pair = [row(5, 'user', 'alpha'), row(5, 'assistant', 'alpha')]
    const { result, rerender } = searchOver(pair, 'alpha')
    expect(result.current.currentMessageIdx).toBe(0)
    rerender({ msgs: [row(1, 'user', 'alpha'), ...pair] })
    expect(result.current.currentMessageIdx).toBe(1)
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.matches).toEqual([{ msgIdx: 0, occ: 0 }, { msgIdx: 1, occ: 0 }, { msgIdx: 2, occ: 0 }])
    expect(result.current.currentMessageIdx).toBe(1)
    act(() => result.current.next())
    expect(result.current.currentMessageIdx).toBe(2)
  })

  it('a current match whose row leaves hands over to the next one, or to the one before when it was last, moving nobody', () => {
    const a = row(1, 'user', 'alpha')
    const b = row(2, 'assistant', 'alpha beta')
    const c = row(3, 'user', 'alpha gamma')
    const { result, rerender } = searchOver([a, b, c], 'alpha')
    act(() => result.current.goTo(1))
    rerender({ msgs: [a, c] })
    // In the render without the row, before the search runs again.
    expect(result.current.matches).toEqual([{ msgIdx: 0, occ: 0 }, { msgIdx: 1, occ: 0 }])
    expect(result.current.currentIdx).toBe(1)
    expect(result.current.currentMessageIdx).toBe(1)
    expect(result.current.reveal).toBe(0)
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.currentMessageIdx).toBe(1)
    expect(result.current.reveal).toBe(0)

    act(() => result.current.goTo(1))
    expect(result.current.reveal).toBeGreaterThan(0)
    rerender({ msgs: [a] })
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.currentIdx).toBe(0)
    expect(result.current.currentMessageIdx).toBe(0)
    expect(result.current.reveal).toBe(0)
  })

  it('a step right after a row left counts only the matches still in the list', () => {
    const a = row(1, 'user', 'alpha')
    const b = row(2, 'assistant', 'alpha beta')
    const c = row(3, 'user', 'alpha gamma')
    const { result, rerender } = searchOver([a, b, c], 'alpha')
    rerender({ msgs: [a, c] })
    act(() => result.current.next())
    expect(result.current.currentMessageIdx).toBe(1)
    expect(result.current.reveal).toBeGreaterThan(0)
  })

  it('a new term or case carries the match number and moves the reader to it', () => {
    const { result } = searchOver([row(1, 'user', 'alpha one'), row(2, 'assistant', 'alpha two'), row(3, 'user', 'alpha three')], 'alpha')
    const typed = result.current.reveal
    expect(typed).toBeGreaterThan(0)
    act(() => result.current.goTo(1))
    const stepped = result.current.reveal
    expect(stepped).toBeGreaterThan(typed)
    act(() => result.current.setTerm('alph'))
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.currentIdx).toBe(1)
    expect(result.current.reveal).toBeGreaterThan(stepped)
    act(() => result.current.setTerm('three'))
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.currentIdx).toBe(0)
    expect(result.current.currentMessageIdx).toBe(2)
    const narrowed = result.current.reveal
    act(() => result.current.toggleCaseSensitive())
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.currentMessageIdx).toBe(2)
    expect(result.current.reveal).toBeGreaterThan(narrowed)
  })

  it('the first matches arriving with new rows become current without moving the reader', () => {
    const { result, rerender } = searchOver([row(5, 'user', 'nothing here')], 'zeta')
    expect(result.current.matches).toEqual([])
    rerender({ msgs: [row(1, 'assistant', 'zeta release notes'), row(5, 'user', 'nothing here')] })
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.currentIdx).toBe(0)
    expect(result.current.currentMessageIdx).toBe(0)
    expect(result.current.reveal).toBe(0)
  })

  it('every step is a find action of its own, Enter on the only match included', () => {
    const { result } = searchOver([row(1, 'user', 'only once')], 'once')
    const seen = [result.current.reveal]
    act(() => result.current.next())
    seen.push(result.current.reveal)
    act(() => result.current.prev())
    seen.push(result.current.reveal)
    act(() => result.current.goTo(0))
    seen.push(result.current.reveal)
    expect(result.current.currentMessageIdx).toBe(0)
    expect(seen.every((n, i) => i === 0 ? n > 0 : n > seen[i - 1])).toBe(true)
  })

  it('a find action stays pending until a row has used it, and an old one never comes back', () => {
    const { result } = searchOver([row(1, 'user', 'alpha'), row(2, 'user', 'alpha')], 'alpha')
    const { revealLedger } = result.current
    const typed = result.current.reveal
    expect(revealLedger.pending(typed)).toBe(true)
    revealLedger.consume(typed)
    expect(revealLedger.pending(typed)).toBe(false)
    act(() => result.current.next())
    expect(revealLedger.pending(result.current.reveal)).toBe(true)
    expect(revealLedger.pending(0)).toBe(false)
    // A close and a new search hand out a number never handed out before.
    const last = result.current.reveal
    act(() => result.current.close())
    expect(result.current.reveal).toBe(0)
    act(() => result.current.setTerm('alpha'))
    act(() => { vi.advanceTimersByTime(50) })
    expect(result.current.reveal).toBeGreaterThan(last)
    expect(result.current.revealLedger).toBe(revealLedger)
  })

  it('a find action is pending for one converge backstop at most, and only while it is the latest', () => {
    let now = 1_000
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => now)
    try {
      const { result } = searchOver([row(1, 'user', 'alpha'), row(2, 'user', 'alpha')], 'alpha')
      const { revealLedger } = result.current
      const typed = result.current.reveal
      expect(revealLedger.pending(typed)).toBe(true)
      act(() => result.current.next())
      // A newer action takes its place, used or not.
      expect(revealLedger.pending(typed)).toBe(false)
      const stepped = result.current.reveal
      now += CONVERGE_MAX_MS - 1
      expect(revealLedger.pending(stepped)).toBe(true)
      // A row that mounts this late (it unmounted before its first scroll, or no
      // host could move to it) must not move the reader.
      now += 1
      expect(revealLedger.pending(stepped)).toBe(false)
    } finally {
      clock.mockRestore()
    }
  })
})
