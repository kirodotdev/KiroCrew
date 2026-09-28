/**
 * The edit-model grid geometry + recursive by-id tree ops (RFC §5.4). All pure:
 * these decide where an item may land and transform the tree by id, and the
 * editor (PR 2) leans on them for placement, cross-canvas move and
 * cycle-prevention — so they are exercised here directly, not only through the
 * conversion in editModel.
 */
import { describe, it, expect } from 'vitest'
import {
  clamp,
  overlaps,
  inBounds,
  canPlace,
  firstFreeCell,
  findItem,
  updateItems,
  removeItemById,
  subtreeHas,
  canNest,
  type GridItem,
} from '../grid'

const cell = (id: string, x: number, y: number, w = 1, h = 1): GridItem => ({
  id,
  element: 'chat',
  x,
  y,
  w,
  h,
})

describe('grid geometry', () => {
  it('clamps to the inclusive range', () => {
    expect(clamp(5, 0, 10)).toBe(5)
    expect(clamp(-3, 0, 10)).toBe(0)
    expect(clamp(99, 0, 10)).toBe(10)
  })

  it('detects overlap vs adjacency', () => {
    const a = { x: 0, y: 0, w: 2, h: 2 }
    expect(overlaps(a, { x: 1, y: 1, w: 1, h: 1 })).toBe(true) // shares a cell
    expect(overlaps(a, { x: 2, y: 0, w: 1, h: 1 })).toBe(false) // touches edge, no shared cell
    expect(overlaps(a, { x: 0, y: 2, w: 1, h: 1 })).toBe(false) // directly below
  })

  it('bounds-checks a rect against the grid', () => {
    expect(inBounds({ x: 0, y: 0, w: 2, h: 2 }, 2, 2)).toBe(true)
    expect(inBounds({ x: 1, y: 0, w: 2, h: 1 }, 2, 2)).toBe(false) // spills past cols
    expect(inBounds({ x: -1, y: 0, w: 1, h: 1 }, 2, 2)).toBe(false) // negative origin
  })

  it('canPlace rejects out-of-bounds and overlap, allows the ignored item', () => {
    const items = [cell('a', 0, 0), cell('b', 1, 0)]
    expect(canPlace(items, { x: 0, y: 1, w: 1, h: 1 }, 2, 2)).toBe(true) // free cell
    expect(canPlace(items, { x: 1, y: 0, w: 1, h: 1 }, 2, 2)).toBe(false) // over 'b'
    expect(canPlace(items, { x: 5, y: 5, w: 1, h: 1 }, 2, 2)).toBe(false) // out of bounds
    // moving 'b' onto its own cell is allowed via ignoreId
    expect(canPlace(items, { x: 1, y: 0, w: 1, h: 1 }, 2, 2, 'b')).toBe(true)
  })

  it('firstFreeCell walks row-major and returns null when full', () => {
    expect(firstFreeCell([cell('a', 0, 0)], 2, 1)).toEqual({ x: 1, y: 0 })
    expect(firstFreeCell([cell('a', 0, 0), cell('b', 1, 0)], 2, 1)).toBeNull()
    expect(firstFreeCell([cell('a', 0, 0)], 2, 2)).toEqual({ x: 1, y: 0 }) // row 0 before row 1
  })

  it('canNest is true only for a group container', () => {
    expect(canNest('group')).toBe(true)
    expect(canNest('tabs')).toBe(false)
    expect(canNest('chat')).toBe(false)
  })
})

describe('recursive by-id tree ops', () => {
  // A tree with a nested grid (inside a group) and a tabs container, so the
  // recursion into .grid and .tabs is exercised, not just the root array.
  const tree = (): GridItem[] => [
    cell('root-a', 0, 0),
    {
      id: 'grp',
      element: 'group',
      x: 1,
      y: 0,
      w: 1,
      h: 1,
      grid: { cols: 1, rows: 1, items: [cell('nested', 0, 0)] },
    },
    {
      id: 'tab',
      element: 'tabs',
      x: 0,
      y: 1,
      w: 1,
      h: 1,
      activeTab: 0,
      tabs: [cell('t0', 0, 0), cell('t1', 0, 0)],
    },
  ]

  it('findItem reaches root, nested-grid and tab children', () => {
    const arr = tree()
    expect(findItem(arr, 'root-a')?.id).toBe('root-a')
    expect(findItem(arr, 'nested')?.id).toBe('nested')
    expect(findItem(arr, 't1')?.id).toBe('t1')
    expect(findItem(arr, 'missing')).toBeNull()
  })

  it('updateItems maps by id at any depth and leaves siblings untouched', () => {
    const arr = tree()
    const next = updateItems(arr, 'nested', (i) => ({ ...i, x: 9 }))
    expect(findItem(next, 'nested')?.x).toBe(9)
    // sibling and structure preserved
    expect(findItem(next, 'root-a')?.x).toBe(0)
    expect(findItem(next, 't1')?.id).toBe('t1')
    // original array not mutated
    expect(findItem(arr, 'nested')?.x).toBe(0)
  })

  it('removeItemById deletes at any depth', () => {
    const arr = tree()
    expect(findItem(removeItemById(arr, 'nested'), 'nested')).toBeNull()
    expect(findItem(removeItemById(arr, 't0'), 't0')).toBeNull()
    // removing a tab child keeps its sibling
    expect(findItem(removeItemById(arr, 't0'), 't1')?.id).toBe('t1')
    // removing the group removes its whole subtree
    const noGrp = removeItemById(arr, 'grp')
    expect(findItem(noGrp, 'grp')).toBeNull()
    expect(findItem(noGrp, 'nested')).toBeNull()
  })

  it('subtreeHas guards a container dropping into itself', () => {
    const grp = tree()[1]
    expect(subtreeHas(grp, 'grp')).toBe(true) // itself
    expect(subtreeHas(grp, 'nested')).toBe(true) // its child
    expect(subtreeHas(grp, 'root-a')).toBe(false) // a sibling
    const tab = tree()[2]
    expect(subtreeHas(tab, 't1')).toBe(true)
  })

  // A tabs container with three tabs, so removal can hit the active tab, a tab
  // before it, and the last tab. Each case must leave activeTab valid AND
  // pointing at the same surviving tab by identity — a stale index would
  // serialize verbatim and reload unclamped (the corruption the fix closes).
  const tabsOf = (active: number): GridItem[] => [
    {
      id: 'tab',
      element: 'tabs',
      x: 0,
      y: 0,
      w: 1,
      h: 1,
      activeTab: active,
      tabs: [cell('t0', 0, 0), cell('t1', 0, 0), cell('t2', 0, 0)],
    },
  ]
  const tabsAfter = (arr: GridItem[]) => findItem(arr, 'tab') as GridItem

  it('removeItemById keeps the selected tab selected by id when an EARLIER tab goes', () => {
    // active is t2 (index 2); delete t0 → t2 shifts to index 1, activeTab must follow it
    const after = tabsAfter(removeItemById(tabsOf(2), 't0'))
    expect(after.tabs?.map((t) => t.id)).toEqual(['t1', 't2'])
    expect(after.tabs?.[after.activeTab!].id).toBe('t2') // still the same tab, new index
    expect(after.activeTab).toBe(1)
  })

  it('removeItemById clamps to a valid neighbour when the ACTIVE tab goes', () => {
    // active is t2 (last); delete it → activeTab must clamp into range, never 2
    const after = tabsAfter(removeItemById(tabsOf(2), 't2'))
    expect(after.tabs?.map((t) => t.id)).toEqual(['t0', 't1'])
    expect(after.activeTab).toBeLessThan(after.tabs!.length)
    expect(after.activeTab).toBe(1) // clamps to last surviving index
  })

  it('removeItemById clears activeTab when the last tab is removed', () => {
    let arr = removeItemById(tabsOf(0), 't0')
    arr = removeItemById(arr, 't1')
    arr = removeItemById(arr, 't2')
    const after = tabsAfter(arr)
    expect(after.tabs).toEqual([])
    expect(after.activeTab).toBeUndefined()
  })

  it('removeItemById leaves activeTab untouched when a NON-active tab after it goes', () => {
    // active is t0 (index 0); delete t2 → t0 stays index 0
    const after = tabsAfter(removeItemById(tabsOf(0), 't2'))
    expect(after.tabs?.map((t) => t.id)).toEqual(['t0', 't1'])
    expect(after.activeTab).toBe(0)
  })
})
