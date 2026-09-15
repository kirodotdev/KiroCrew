/**
 * The session-tab working set (#4477) — the store-free half.
 *
 * Every case here is a rule a REVIEWER cannot check by reading the strip: which
 * tab a close lands on at each end of the strip, that a filtered-away session
 * is not silently dropped from the set, that a one-tab set is not persisted (or
 * a user who never used the feature would carry invisible state deciding which
 * session a reload restores), and the status precedence that keeps a
 * blocked-on-you session from reading as busy.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  TAB_STATUS_COLOR,
  closeSessionTab,
  loadSessionTabs,
  nextActiveAfterClose,
  openSessionTab,
  pruneSessionTabs,
  removeTabAt,
  replaceSessionTab,
  saveSessionTabs,
  sessionTabsStorageKey,
  tabStatus,
  tabStatusPulses,
  truncateTabTitle,
} from '../lib/sessionTabs'

describe('sessionTabs storage', () => {
  beforeEach(() => localStorage.clear())

  it('keys per surface mode, mirroring the active-slot key', () => {
    expect(sessionTabsStorageKey('orchestrator')).toBe('mc-session-tabs-orchestrator')
    expect(sessionTabsStorageKey(undefined)).toBe('mc-session-tabs-chat')
    expect(sessionTabsStorageKey('')).toBe('mc-session-tabs-chat')
  })

  it('restores a persisted set', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['a', 'b']))
    expect(loadSessionTabs(undefined, null)).toEqual(['a', 'b'])
  })

  it('falls back to the active session when storage is empty, corrupt, or the wrong shape', () => {
    expect(loadSessionTabs(undefined, 'live')).toEqual(['live'])
    localStorage.setItem('mc-session-tabs-chat', 'not json{')
    expect(loadSessionTabs(undefined, 'live')).toEqual(['live'])
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify({ a: 1 }))
    expect(loadSessionTabs(undefined, 'live')).toEqual(['live'])
    // No active session either: an empty set, never a phantom tab.
    localStorage.removeItem('mc-session-tabs-chat')
    expect(loadSessionTabs(undefined, null)).toEqual([])
  })

  it('drops non-string, empty and duplicate entries, and keeps every real one', () => {
    // No ceiling: an earlier revision truncated here, which silently discarded
    // tabs the user had placed to prevent a narrowing that cannot happen (tabs
    // are shrink-0 in a scroller).
    const stored = [1, '', 'a', 'a', null, 'b', ...Array.from({ length: 40 }, (_, i) => `x${i}`)]
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(stored))
    const tabs = loadSessionTabs(undefined, null)
    expect(tabs.length).toBe(42)
    expect(tabs.slice(0, 2)).toEqual(['a', 'b'])
    expect(new Set(tabs).size).toBe(tabs.length)
  })

  it('persists two or more tabs but stores a one-tab set as empty', () => {
    const write = vi.fn()
    saveSessionTabs(undefined, ['a', 'b'], write)
    expect(write).toHaveBeenCalledWith('mc-session-tabs-chat', '["a","b"]')
    write.mockClear()
    // A single tab is not a working set; writing it would leave a user who never
    // opened a second tab carrying state they cannot see.
    saveSessionTabs(undefined, ['a'], write)
    expect(write).toHaveBeenCalledWith('mc-session-tabs-chat', '[]')
    write.mockClear()
    saveSessionTabs(undefined, [], write)
    expect(write).toHaveBeenCalledWith('mc-session-tabs-chat', '[]')
  })
})

describe('sessionTabs set arithmetic', () => {
  it('opens beside the active tab, not at the end', () => {
    expect(openSessionTab(['a', 'b', 'c'], 'new', 'a')).toEqual(['a', 'new', 'b', 'c'])
  })

  it('appends when the active tab is unknown or absent', () => {
    expect(openSessionTab(['a', 'b'], 'new', null)).toEqual(['a', 'b', 'new'])
    expect(openSessionTab(['a', 'b'], 'new', 'gone')).toEqual(['a', 'b', 'new'])
    expect(openSessionTab([], 'new', null)).toEqual(['new'])
  })

  it('is a no-op for an already-open session and for an empty key', () => {
    expect(openSessionTab(['a', 'b'], 'b', 'a')).toEqual(['a', 'b'])
    expect(openSessionTab(['a'], '', 'a')).toEqual(['a'])
  })

  it('never evicts an existing tab to make room', () => {
    // The feature's whole promise is that only the user moves a tab, so opening
    // one may not silently drop another.
    const many = Array.from({ length: 30 }, (_, i) => `t${i}`)
    const next = openSessionTab(many, 'new', many[many.length - 1])
    expect(next.length).toBe(31)
    for (const k of many) expect(next).toContain(k)
    expect(next[next.length - 1]).toBe('new')
  })

  it('replaces the active tab in place, keeping its position', () => {
    expect(replaceSessionTab(['a', 'b', 'c'], 'b', 'z')).toEqual(['a', 'z', 'c'])
  })

  it('leaves the set alone when the replacement is already open', () => {
    // The caller activates the existing tab instead of duplicating it.
    expect(replaceSessionTab(['a', 'b'], 'a', 'b')).toEqual(['a', 'b'])
  })

  it('appends a replacement when no tab holds the old key', () => {
    expect(replaceSessionTab(['a'], null, 'z')).toEqual(['a', 'z'])
    expect(replaceSessionTab(['a'], 'gone', 'z')).toEqual(['a', 'z'])
  })

  it('prunes sessions that no longer exist, order preserved', () => {
    expect(pruneSessionTabs(['a', 'b', 'c'], ['c', 'a'])).toEqual(['a', 'c'])
    expect(pruneSessionTabs(['a'], new Set<string>())).toEqual([])
  })

  it('removes a tab without touching the rest', () => {
    expect(closeSessionTab(['a', 'b', 'c'], 'b')).toEqual(['a', 'c'])
    expect(closeSessionTab(['a'], 'zz')).toEqual(['a'])
  })
})

describe('nextActiveAfterClose', () => {
  it('does not move the user when the closed tab is not the active one', () => {
    expect(nextActiveAfterClose(['a', 'b', 'c'], 'c', 'a')).toBe('a')
  })

  it('lands on the right neighbour', () => {
    expect(nextActiveAfterClose(['a', 'b', 'c'], 'b', 'b')).toBe('c')
  })

  it('falls back to the left at the end of the strip', () => {
    // Not 'a': closing the last of many tabs must not teleport the user to the
    // other end of the strip.
    expect(nextActiveAfterClose(['a', 'b', 'c'], 'c', 'c')).toBe('b')
  })

  it('reports nothing left for the final tab', () => {
    expect(nextActiveAfterClose(['a'], 'a', 'a')).toBeNull()
  })

  it('leaves the active key alone for a tab that is not in the set', () => {
    expect(nextActiveAfterClose(['a', 'b'], 'gone', 'gone')).toBe('gone')
  })
})

describe('removeTabAt (index model, embed strip)', () => {
  it('shifts the selection left when a tab before it closes', () => {
    expect(removeTabAt(['a', 'b', 'c'], 0, 2)).toEqual({ tabs: ['b', 'c'], activeIndex: 1 })
  })

  it('keeps the index when a later tab closes', () => {
    expect(removeTabAt(['a', 'b', 'c'], 2, 0)).toEqual({ tabs: ['a', 'b'], activeIndex: 0 })
  })

  it('clamps to the last tab when the selected tab was last', () => {
    expect(removeTabAt(['a', 'b'], 1, 1)).toEqual({ tabs: ['a'], activeIndex: 0 })
  })

  it('reports index 0 for an emptied list so the caller can seed a placeholder', () => {
    expect(removeTabAt(['a'], 0, 0)).toEqual({ tabs: [], activeIndex: 0 })
  })

  it('ignores an out-of-range index instead of corrupting the list', () => {
    expect(removeTabAt(['a', 'b'], 5, 1)).toEqual({ tabs: ['a', 'b'], activeIndex: 1 })
    expect(removeTabAt(['a', 'b'], -1, 0)).toEqual({ tabs: ['a', 'b'], activeIndex: 0 })
  })
})

describe('tabStatus precedence', () => {
  it('reports a gate the user must clear above work in flight', () => {
    // Both are true at once whenever a tool gate opens mid-turn; a tab that read
    // "running" there is the state a user leaves alone.
    expect(tabStatus({ running: true, pending_approval: true }, [], 'k')).toBe('permission')
  })

  it('reports a parked question above work in flight', () => {
    expect(tabStatus({ running: true, needs_input: true }, [], 'k')).toBe('question')
  })

  it('reports running above unread', () => {
    expect(tabStatus({ running: true }, ['k'], 'k')).toBe('running')
  })

  it('reports unread, then idle', () => {
    expect(tabStatus({}, ['k'], 'k')).toBe('unread')
    expect(tabStatus({}, new Set(['k']), 'k')).toBe('unread')
    expect(tabStatus({}, ['other'], 'k')).toBe('idle')
  })

  it('treats a missing slot as idle', () => {
    expect(tabStatus(undefined, ['k'], 'k')).toBe('idle')
  })

  it('animates only the two states waiting on something', () => {
    expect(tabStatusPulses('running')).toBe(true)
    expect(tabStatusPulses('permission')).toBe(true)
    expect(tabStatusPulses('question')).toBe(false)
    expect(tabStatusPulses('unread')).toBe(false)
    expect(tabStatusPulses('idle')).toBe(false)
  })

  it('gives every status a theme variable, so none can render colourless', () => {
    for (const status of ['idle', 'running', 'unread', 'permission', 'question'] as const) {
      expect(TAB_STATUS_COLOR[status]).toMatch(/^var\(--[a-z]+\)$/)
    }
  })
})

describe('truncateTabTitle', () => {
  it('leaves a title that fits untouched', () => {
    expect(truncateTabTitle('short')).toBe('short')
    expect(truncateTabTitle('x'.repeat(24))).toBe('x'.repeat(24))
  })

  it('ellipsizes past the limit', () => {
    expect(truncateTabTitle('y'.repeat(30))).toBe('y'.repeat(24) + '…')
    expect(truncateTabTitle('abcdef', 3)).toBe('abc…')
  })
})
