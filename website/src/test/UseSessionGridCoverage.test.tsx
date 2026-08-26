import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSessionGrid } from '../hooks/useSessionGrid'
import type { GridLeaf, GridNode, GridSplit } from '../hooks/useSessionGrid'

/**
 * Coverage for useSessionGrid — the recursive split-tree state behind Kiro Crew's
 * native "terminal split" chat mode.
 *
 * The hook owns four things worth pinning down: restore-from-persistence on entry,
 * the immutable tree transforms (seed / split / close / fill / resize / prune),
 * the derived selectors (leaves, occupiedSlots, paneCount, focus healing), and the
 * three persistence paths (300ms debounced write, synchronous dissolve, synchronous
 * unmount flush). All of it is plain state + localStorage, so it exercises directly
 * through renderHook with no Redux store or query client.
 */

const STORE_KEY = 'mc-split-layouts'

const asSplit = (node: GridNode | null): GridSplit => {
  if (!node || node.type !== 'split') throw new Error('expected a split node')
  return node
}

const asLeaf = (node: GridNode | null): GridLeaf => {
  if (!node || node.type !== 'leaf') throw new Error('expected a leaf node')
  return node
}

/** A persisted, real (>= 2 sessions) split anchored at slot `a`. */
const twoSessionSplit = (): GridSplit => ({
  type: 'split',
  id: 'split-root',
  dir: 'col',
  children: [
    { type: 'leaf', id: 'leaf-a', kind: 'session', slot: 'a' },
    { type: 'leaf', id: 'leaf-b', kind: 'session', slot: 'b' },
  ],
  sizes: [0.5, 0.5],
})

const seedStore = (map: Record<string, GridNode>) => {
  localStorage.setItem(STORE_KEY, JSON.stringify(map))
}

const readStore = (): Record<string, GridNode> => {
  const raw = localStorage.getItem(STORE_KEY)
  return raw ? (JSON.parse(raw) as Record<string, GridNode>) : {}
}

beforeEach(() => {
  localStorage.clear()
})

describe('useSessionGrid — restore and empty state', () => {
  it('starts empty when no anchor slot is supplied', () => {
    const { result } = renderHook(() => useSessionGrid(null))

    expect(result.current.tree).toBeNull()
    expect(result.current.isEmpty).toBe(true)
    expect(result.current.leaves).toEqual([])
    expect(result.current.occupiedSlots).toEqual([])
    expect(result.current.paneCount).toBe(0)
    expect(result.current.focusedId).toBeNull()
  })

  it('starts empty for an anchor that owns no persisted layout', () => {
    seedStore({ a: twoSessionSplit() })

    const { result } = renderHook(() => useSessionGrid('unrelated-slot'))

    expect(result.current.tree).toBeNull()
    expect(result.current.isEmpty).toBe(true)
  })

  it('restores the layout persisted under its anchor slot', () => {
    seedStore({ a: twoSessionSplit() })

    const { result } = renderHook(() => useSessionGrid('a'))

    const root = asSplit(result.current.tree)
    expect(root.dir).toBe('col')
    expect(root.children).toHaveLength(2)
    expect(result.current.isEmpty).toBe(false)
    expect(result.current.leaves.map((l) => l.id)).toEqual(['leaf-a', 'leaf-b'])
    expect(result.current.occupiedSlots).toEqual(['a', 'b'])
    expect(result.current.paneCount).toBe(2)
  })

  it('heals focus onto the first restored leaf', () => {
    seedStore({ a: twoSessionSplit() })

    const { result } = renderHook(() => useSessionGrid('a'))

    expect(result.current.focusedId).toBe('leaf-a')
  })
})

describe('useSessionGrid — seedFromSession', () => {
  it('seeds a lone placeholder when there is no current session', () => {
    const { result } = renderHook(() => useSessionGrid(null))

    act(() => result.current.seedFromSession(null))

    const leaf = asLeaf(result.current.tree)
    expect(leaf.kind).toBe('placeholder')
    expect(leaf.slot).toBeUndefined()
    expect(result.current.focusedId).toBe(leaf.id)
    expect(result.current.paneCount).toBe(0)
  })

  it('splits the current session in place, focusing the fresh placeholder', () => {
    const { result } = renderHook(() => useSessionGrid(null))

    act(() => result.current.seedFromSession('slot-1'))

    const root = asSplit(result.current.tree)
    expect(root.dir).toBe('col')
    expect(root.sizes).toEqual([0.5, 0.5])
    const [current, blank] = root.children.map(asLeaf)
    expect(current.kind).toBe('session')
    expect(current.slot).toBe('slot-1')
    expect(blank.kind).toBe('placeholder')
    expect(result.current.focusedId).toBe(blank.id)
    expect(result.current.occupiedSlots).toEqual(['slot-1'])
    expect(result.current.paneCount).toBe(1)
  })

  it('generates ids without crypto.randomUUID', () => {
    vi.stubGlobal('crypto', {})
    try {
      const { result } = renderHook(() => useSessionGrid(null))

      act(() => result.current.seedFromSession('slot-1'))

      const root = asSplit(result.current.tree)
      expect(root.id).toMatch(/^[a-z0-9]+$/)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('generates ids when crypto is absent entirely', () => {
    vi.stubGlobal('crypto', undefined)
    try {
      const { result } = renderHook(() => useSessionGrid(null))

      act(() => result.current.seedFromSession(null))

      expect(asLeaf(result.current.tree).id).toMatch(/^[a-z0-9]+$/)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('useSessionGrid — splitLeaf', () => {
  it('wraps a root leaf in a col split when splitting right', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession(null))
    const rootLeafId = asLeaf(result.current.tree).id

    act(() => result.current.splitLeaf(rootLeafId, 'right'))

    const root = asSplit(result.current.tree)
    expect(root.dir).toBe('col')
    expect(root.sizes).toEqual([0.5, 0.5])
    expect(asLeaf(root.children[0]).id).toBe(rootLeafId)
    expect(result.current.focusedId).toBe(asLeaf(root.children[1]).id)
  })

  it('wraps a root leaf in a row split when splitting down', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession(null))
    const rootLeafId = asLeaf(result.current.tree).id

    act(() => result.current.splitLeaf(rootLeafId, 'down'))

    expect(asSplit(result.current.tree).dir).toBe('row')
  })

  it('inserts a flattened sibling when the parent already runs on that axis', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))
    const firstId = asLeaf(asSplit(result.current.tree).children[0]).id

    act(() => result.current.splitLeaf(firstId, 'right'))

    const root = asSplit(result.current.tree)
    expect(root.id).toBe(asSplit(result.current.tree).id)
    expect(root.children).toHaveLength(3)
    expect(root.children.every((c) => c.type === 'leaf')).toBe(true)
    expect(root.sizes).toEqual([0.25, 0.25, 0.5])
    expect(asLeaf(root.children[0]).id).toBe(firstId)
    expect(result.current.focusedId).toBe(asLeaf(root.children[1]).id)
  })

  it('nests a new split when the requested axis differs from the parent', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))
    const blankId = asLeaf(asSplit(result.current.tree).children[1]).id

    act(() => result.current.splitLeaf(blankId, 'down'))

    const root = asSplit(result.current.tree)
    expect(root.dir).toBe('col')
    expect(root.children).toHaveLength(2)
    const nested = asSplit(root.children[1])
    expect(nested.dir).toBe('row')
    expect(asLeaf(nested.children[0]).id).toBe(blankId)
    expect(result.current.leaves).toHaveLength(3)
  })

  it('falls back to an even share when the persisted split has no sizes', () => {
    seedStore({
      a: {
        type: 'split',
        id: 'split-root',
        dir: 'col',
        children: [
          { type: 'leaf', id: 'leaf-a', kind: 'session', slot: 'a' },
          { type: 'leaf', id: 'leaf-b', kind: 'session', slot: 'b' },
        ],
        sizes: [],
      },
    })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.splitLeaf('leaf-a', 'right'))

    const root = asSplit(result.current.tree)
    expect(root.children).toHaveLength(3)
    expect(root.sizes.slice(0, 2)).toEqual([0.25, 0.25])
  })

  it('is a no-op on an empty tree', () => {
    const { result } = renderHook(() => useSessionGrid(null))

    act(() => result.current.splitLeaf('nope', 'right'))

    expect(result.current.tree).toBeNull()
  })

  it('leaves the tree intact for an unknown leaf id', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))

    act(() => result.current.splitLeaf('does-not-exist', 'right'))

    expect(result.current.leaves).toHaveLength(2)
  })
})

describe('useSessionGrid — closeLeaf', () => {
  it('collapses a single-child split into that child', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))
    const blankId = asLeaf(asSplit(result.current.tree).children[1]).id

    act(() => result.current.closeLeaf(blankId))

    const leaf = asLeaf(result.current.tree)
    expect(leaf.kind).toBe('session')
    expect(leaf.slot).toBe('slot-1')
    expect(result.current.paneCount).toBe(1)
  })

  it('re-homes focus onto a surviving leaf when the focused pane closes', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))
    const root = asSplit(result.current.tree)
    const keptId = asLeaf(root.children[0]).id
    const blankId = asLeaf(root.children[1]).id
    expect(result.current.focusedId).toBe(blankId)

    act(() => result.current.closeLeaf(blankId))

    expect(result.current.focusedId).toBe(keptId)
  })

  it('keeps focus when an unfocused pane closes', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))
    const root = asSplit(result.current.tree)
    const sessionId = asLeaf(root.children[0]).id
    const blankId = asLeaf(root.children[1]).id

    act(() => result.current.closeLeaf(sessionId))

    expect(asLeaf(result.current.tree).id).toBe(blankId)
    expect(result.current.focusedId).toBe(blankId)
  })

  it('empties the tree when the last pane closes', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession(null))
    const only = asLeaf(result.current.tree).id

    act(() => result.current.closeLeaf(only))

    expect(result.current.tree).toBeNull()
    expect(result.current.isEmpty).toBe(true)
    expect(result.current.focusedId).toBeNull()
  })

  it('renormalizes surviving sibling sizes', () => {
    seedStore({
      a: {
        type: 'split',
        id: 'split-root',
        dir: 'col',
        children: [
          { type: 'leaf', id: 'leaf-a', kind: 'session', slot: 'a' },
          { type: 'leaf', id: 'leaf-b', kind: 'session', slot: 'b' },
          { type: 'leaf', id: 'leaf-c', kind: 'session', slot: 'c' },
        ],
        sizes: [0.2, 0.4, 0.4],
      },
    })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.closeLeaf('leaf-a'))

    const root = asSplit(result.current.tree)
    expect(root.children).toHaveLength(2)
    expect(root.sizes[0]).toBeCloseTo(0.5, 6)
    expect(root.sizes[1]).toBeCloseTo(0.5, 6)
  })

  it('falls back to an even share for children missing a persisted size', () => {
    seedStore({
      a: {
        type: 'split',
        id: 'split-root',
        dir: 'col',
        children: [
          { type: 'leaf', id: 'leaf-a', kind: 'session', slot: 'a' },
          { type: 'leaf', id: 'leaf-b', kind: 'session', slot: 'b' },
          { type: 'leaf', id: 'leaf-c', kind: 'session', slot: 'c' },
        ],
        sizes: [0.4],
      },
    })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.closeLeaf('leaf-a'))

    const root = asSplit(result.current.tree)
    expect(root.sizes).toHaveLength(2)
    root.sizes.forEach((s) => expect(s).toBeCloseTo(0.5, 6))
  })

  it('survives a persisted split whose sizes all sum to zero', () => {
    seedStore({
      a: {
        type: 'split',
        id: 'split-root',
        dir: 'col',
        children: [
          { type: 'leaf', id: 'leaf-a', kind: 'session', slot: 'a' },
          { type: 'leaf', id: 'leaf-b', kind: 'session', slot: 'b' },
          { type: 'leaf', id: 'leaf-c', kind: 'session', slot: 'c' },
        ],
        sizes: [0, 0, 0],
      },
    })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.closeLeaf('leaf-c'))

    expect(asSplit(result.current.tree).sizes).toEqual([0, 0])
  })

  it('prunes a degenerate one-child split out of the tree entirely', () => {
    // A legacy-default persisted layout can hold a split with a single child
    // (a shape the live transforms collapse but the store never rewrites).
    // Closing that child must remove the empty split, not leave a childless node.
    seedStore({
      a: {
        type: 'split',
        id: 'split-root',
        dir: 'col',
        children: [
          { type: 'leaf', id: 'leaf-a', kind: 'session', slot: 'a' },
          { type: 'split', id: 'split-inner', dir: 'row', children: [{ type: 'leaf', id: 'leaf-b', kind: 'session', slot: 'b' }], sizes: [1] },
        ],
        sizes: [0.5, 0.5],
      },
    })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.closeLeaf('leaf-b'))

    const leaf = asLeaf(result.current.tree)
    expect(leaf.id).toBe('leaf-a')
    expect(result.current.occupiedSlots).toEqual(['a'])
  })

  it('is a no-op on an empty tree', () => {
    const { result } = renderHook(() => useSessionGrid(null))

    act(() => result.current.closeLeaf('nope'))

    expect(result.current.tree).toBeNull()
  })
})

describe('useSessionGrid — fillLeaf and derived selectors', () => {
  it('turns a placeholder into a session pane', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))
    const blankId = asLeaf(asSplit(result.current.tree).children[1]).id

    act(() => result.current.fillLeaf(blankId, { kind: 'session', slot: 'slot-2' }))

    expect(result.current.occupiedSlots).toEqual(['slot-1', 'slot-2'])
    expect(result.current.paneCount).toBe(2)
  })

  it('turns a placeholder into a terminal pane without occupying a slot', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession(null))
    const leafId = asLeaf(result.current.tree).id

    act(() => result.current.fillLeaf(leafId, { kind: 'terminal', termId: 'pty-7' }))

    const leaf = asLeaf(result.current.tree)
    expect(leaf.kind).toBe('terminal')
    expect(leaf.termId).toBe('pty-7')
    expect(result.current.occupiedSlots).toEqual([])
    expect(result.current.paneCount).toBe(1)
  })

  it('is a no-op on an empty tree', () => {
    const { result } = renderHook(() => useSessionGrid(null))

    act(() => result.current.fillLeaf('nope', { kind: 'session', slot: 'x' }))

    expect(result.current.tree).toBeNull()
  })

  it('exposes setFocused for direct focus moves', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))
    const sessionId = asLeaf(asSplit(result.current.tree).children[0]).id

    act(() => result.current.setFocused(sessionId))

    expect(result.current.focusedId).toBe(sessionId)
  })
})

describe('useSessionGrid — resize', () => {
  it('shifts the requested fraction between adjacent panes', () => {
    seedStore({ a: twoSessionSplit() })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.resize('split-root', 0, 0.1))

    const sizes = asSplit(result.current.tree).sizes
    expect(sizes[0]).toBeCloseTo(0.6, 6)
    expect(sizes[1]).toBeCloseTo(0.4, 6)
  })

  it('clamps a grow past the minimum fraction of the next pane', () => {
    seedStore({ a: twoSessionSplit() })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.resize('split-root', 0, 0.9))

    const sizes = asSplit(result.current.tree).sizes
    expect(sizes[0]).toBeCloseTo(0.88, 6)
    expect(sizes[1]).toBeCloseTo(0.12, 6)
  })

  it('clamps a shrink past its own minimum fraction', () => {
    seedStore({ a: twoSessionSplit() })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.resize('split-root', 0, -0.9))

    const sizes = asSplit(result.current.tree).sizes
    expect(sizes[0]).toBeCloseTo(0.12, 6)
    expect(sizes[1]).toBeCloseTo(0.88, 6)
  })

  it('leaves other splits untouched when resizing a nested one', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))
    const blankId = asLeaf(asSplit(result.current.tree).children[1]).id
    act(() => result.current.splitLeaf(blankId, 'down'))
    const nestedId = asSplit(asSplit(result.current.tree).children[1]).id

    act(() => result.current.resize(nestedId, 0, 0.2))

    const root = asSplit(result.current.tree)
    expect(root.sizes).toEqual([0.5, 0.5])
    const nested = asSplit(root.children[1])
    expect(nested.sizes[0]).toBeCloseTo(0.7, 6)
    expect(nested.sizes[1]).toBeCloseTo(0.3, 6)
  })

  it('tolerates an index beyond the size array', () => {
    seedStore({ a: twoSessionSplit() })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.resize('split-root', 5, 0.1))

    const sizes = asSplit(result.current.tree).sizes
    expect(sizes).toHaveLength(7)
    expect(sizes[0]).toBeCloseTo(0.5, 6)
    expect(sizes[1]).toBeCloseTo(0.5, 6)
  })

  it('is a no-op on an empty tree', () => {
    const { result } = renderHook(() => useSessionGrid(null))

    act(() => result.current.resize('nope', 0, 0.1))

    expect(result.current.tree).toBeNull()
  })
})

describe('useSessionGrid — pruneAgainst', () => {
  it('drops panes whose session no longer exists and collapses the split', () => {
    seedStore({ a: twoSessionSplit() })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.pruneAgainst(['a']))

    const leaf = asLeaf(result.current.tree)
    expect(leaf.slot).toBe('a')
    expect(result.current.occupiedSlots).toEqual(['a'])
  })

  it('empties the tree when every pinned session is gone', () => {
    seedStore({ a: twoSessionSplit() })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.pruneAgainst([]))

    expect(result.current.tree).toBeNull()
    expect(result.current.isEmpty).toBe(true)
  })

  it('keeps every pane when all sessions are still live', () => {
    seedStore({ a: twoSessionSplit() })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.pruneAgainst(['a', 'b', 'c']))

    expect(result.current.occupiedSlots).toEqual(['a', 'b'])
    expect(asSplit(result.current.tree).children).toHaveLength(2)
  })

  it('keeps placeholder and terminal panes regardless of the live list', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))

    act(() => result.current.pruneAgainst(['slot-1']))

    expect(result.current.leaves).toHaveLength(2)
  })
})

describe('useSessionGrid — persistence', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('debounces a layout write for a real split', () => {
    seedStore({ a: twoSessionSplit() })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.resize('split-root', 0, 0.1))
    expect(readStore().a).toMatchObject({ sizes: [0.5, 0.5] })

    act(() => void vi.advanceTimersByTime(400))

    expect(asSplit(readStore().a).sizes[0]).toBeCloseTo(0.6, 6)
  })

  it('coalesces a burst of resizes into a single persisted layout', () => {
    seedStore({ a: twoSessionSplit() })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => {
      result.current.resize('split-root', 0, 0.05)
    })
    act(() => void vi.advanceTimersByTime(100))
    act(() => {
      result.current.resize('split-root', 0, 0.05)
    })
    expect(asSplit(readStore().a).sizes[0]).toBeCloseTo(0.5, 6)

    act(() => void vi.advanceTimersByTime(400))

    expect(asSplit(readStore().a).sizes[0]).toBeCloseTo(0.6, 6)
  })

  it('dissolves the persisted layout synchronously when it drops below two sessions', () => {
    seedStore({ a: twoSessionSplit() })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.closeLeaf('leaf-b'))

    expect(readStore().a).toBeUndefined()
  })

  it('moves the persisted entry when the anchor pane closes out of a larger split', () => {
    seedStore({
      a: {
        type: 'split',
        id: 'split-root',
        dir: 'col',
        children: [
          { type: 'leaf', id: 'leaf-a', kind: 'session', slot: 'a' },
          { type: 'leaf', id: 'leaf-b', kind: 'session', slot: 'b' },
          { type: 'leaf', id: 'leaf-c', kind: 'session', slot: 'c' },
        ],
        sizes: [0.34, 0.33, 0.33],
      },
    })
    const { result } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.closeLeaf('leaf-a'))
    act(() => void vi.advanceTimersByTime(400))

    const store = readStore()
    expect(store.a).toBeUndefined()
    expect(asSplit(store.b).children).toHaveLength(2)
  })

  it('flushes the latest real split synchronously on unmount', () => {
    seedStore({ a: twoSessionSplit() })
    const { result, unmount } = renderHook(() => useSessionGrid('a'))

    act(() => result.current.resize('split-root', 0, 0.1))
    localStorage.removeItem(STORE_KEY)

    unmount()

    expect(asSplit(readStore().a).sizes[0]).toBeCloseTo(0.6, 6)
  })

  it('writes nothing on unmount when the tree is not a real split', () => {
    const { result, unmount } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))
    act(() => void vi.advanceTimersByTime(400))

    unmount()

    expect(readStore()).toEqual({})
  })

  it('persists a split assembled from two placeholders', () => {
    const { result } = renderHook(() => useSessionGrid(null))
    act(() => result.current.seedFromSession('slot-1'))
    const blankId = asLeaf(asSplit(result.current.tree).children[1]).id

    act(() => result.current.fillLeaf(blankId, { kind: 'session', slot: 'slot-2' }))
    act(() => void vi.advanceTimersByTime(400))

    const store = readStore()
    expect(Object.keys(store)).toEqual(['slot-1'])
    expect(asSplit(store['slot-1']).children).toHaveLength(2)
  })

  it('leaves an unrelated anchor entry alone', () => {
    seedStore({ a: twoSessionSplit() })
    renderHook(() => useSessionGrid('legacy-default'))

    act(() => void vi.advanceTimersByTime(400))

    expect(readStore().a).toBeDefined()
  })
})
