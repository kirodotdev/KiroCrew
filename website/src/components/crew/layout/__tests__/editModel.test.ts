/**
 * The edit-model conversion: a LayoutTree survives toEditModel → toTree, and a
 * multi-cell grid with a SPANNING cell is preserved verbatim (grid dims + each
 * cell's position and size) rather than collapsed to 1×1 — the regression the
 * `grid` node fixes (RFC §5.4, decision 4, §9 lossless-geometry invariant).
 */
import { describe, it, expect } from 'vitest'
import { makeMembersFloorSeed } from '../membersFloorSeed'
import { toEditModel, toTree, newItem, addChildToTabs } from '../editModel'
import { findItem } from '../grid'
import { LAYOUT_VERSION, GROUP, TABS, type LayoutTree } from '../layoutTree'

describe('editModel round-trip', () => {
  it('preserves the seed (grid dims + cells + config)', () => {
    const seed = makeMembersFloorSeed()
    const back = toTree(toEditModel(seed))
    expect(back.root.kind).toBe('grid')
    if (back.root.kind === 'grid' && seed.root.kind === 'grid') {
      expect(back.root.cols).toBe(seed.root.cols)
      expect(back.root.rows).toBe(seed.root.rows)
      // Track weights must survive too — the seed sets colSizes [3, 2] (chat
      // wider than the panel); dropping them silently resets to equal columns.
      expect(back.root.colSizes).toEqual(seed.root.colSizes)
      expect(back.root.rowSizes).toEqual(seed.root.rowSizes)
      expect(
        back.root.children.map((c) => (c.node.kind === 'cell' ? c.node.element : c.node.kind)),
      ).toEqual(
        seed.root.children.map((c) => (c.node.kind === 'cell' ? c.node.element : c.node.kind)),
      )
      const chat = back.root.children.find((c) => c.node.kind === 'cell' && c.node.element === 'chat')
      expect(chat && chat.node.kind === 'cell' && chat.node.config?.busyMode).toBe('steer-only')
    }
  })

  it('carries colSizes/rowSizes through the round-trip, not just cols/rows', () => {
    const tree: LayoutTree = {
      version: LAYOUT_VERSION,
      root: {
        kind: 'grid',
        id: 'root',
        cols: 2,
        rows: 2,
        colSizes: [3, 1],
        rowSizes: [2, 5],
        children: [
          { x: 0, y: 0, w: 1, h: 1, node: { kind: 'cell', id: 'a', element: 'chat' } },
          { x: 1, y: 0, w: 1, h: 1, node: { kind: 'cell', id: 'b', element: 'files' } },
          { x: 0, y: 1, w: 2, h: 1, node: { kind: 'cell', id: 'c', element: 'terminal' } },
        ],
      },
    }
    // Byte-identical: the track weights are not equal, so a conversion that
    // drops them (the reviewer-found GridSpec gap) fails this outright.
    expect(toTree(toEditModel(tree))).toEqual(tree)
  })

  it('keeps a 2×3 grid with a 2×2 spanning chat cell, not collapsed to 1×1', () => {
    const tree: LayoutTree = {
      version: LAYOUT_VERSION,
      root: {
        kind: 'grid',
        id: 'root',
        cols: 3,
        rows: 2,
        children: [
          { x: 0, y: 0, w: 2, h: 2, node: { kind: 'cell', id: 'a', element: 'chat' } },
          { x: 2, y: 0, w: 1, h: 1, node: { kind: 'cell', id: 'b', element: 'sidePanel' } },
          { x: 2, y: 1, w: 1, h: 1, node: { kind: 'cell', id: 'c', element: 'files' } },
        ],
      },
    }
    const back = toTree(toEditModel(tree))
    // The whole tree must be byte-identical after the edit-model round-trip:
    // this is what proves the spanning-cell geometry survives save/reopen.
    expect(back).toEqual(tree)
    expect(back.root.kind).toBe('grid')
    if (back.root.kind === 'grid') {
      expect(back.root.cols).toBe(3)
      expect(back.root.rows).toBe(2)
      const chat = back.root.children.find((c) => c.node.kind === 'cell' && c.node.element === 'chat')!
      expect({ x: chat.x, y: chat.y, w: chat.w, h: chat.h }).toEqual({ x: 0, y: 0, w: 2, h: 2 })
    }
  })

  it('rebuilds a legacy group root into a grid without losing cells', () => {
    const legacy: LayoutTree = {
      version: LAYOUT_VERSION,
      root: {
        kind: 'group',
        id: 'root',
        dir: 'row',
        children: [
          { kind: 'cell', id: 'a', element: 'chat' },
          { kind: 'cell', id: 'b', element: 'sidePanel' },
        ],
      },
    }
    const back = toTree(toEditModel(legacy))
    expect(back.root.kind).toBe('grid')
    if (back.root.kind === 'grid') {
      expect(back.root.cols).toBe(2)
      expect(back.root.rows).toBe(1)
      expect(
        back.root.children.map((c) => (c.node.kind === 'cell' ? c.node.element : c.node.kind)),
      ).toEqual(['chat', 'sidePanel'])
    }
  })

  it('carries a legacy ROW group’s sizes onto colSizes on rebuild', () => {
    const legacy: LayoutTree = {
      version: LAYOUT_VERSION,
      root: {
        kind: 'group',
        id: 'root',
        dir: 'row',
        sizes: [3, 2], // chat wider than the panel — must not reset to equal
        children: [
          { kind: 'cell', id: 'a', element: 'chat' },
          { kind: 'cell', id: 'b', element: 'sidePanel' },
        ],
      },
    }
    const back = toTree(toEditModel(legacy))
    expect(back.root.kind).toBe('grid')
    if (back.root.kind === 'grid') {
      expect(back.root.colSizes).toEqual([3, 2]) // row group → column tracks
      expect(back.root.rowSizes).toBeUndefined()
    }
  })

  it('carries a legacy COL group’s sizes onto rowSizes on rebuild', () => {
    const legacy: LayoutTree = {
      version: LAYOUT_VERSION,
      root: {
        kind: 'group',
        id: 'root',
        dir: 'col',
        sizes: [1, 4],
        children: [
          { kind: 'cell', id: 'a', element: 'chat' },
          { kind: 'cell', id: 'b', element: 'files' },
        ],
      },
    }
    const back = toTree(toEditModel(legacy))
    expect(back.root.kind).toBe('grid')
    if (back.root.kind === 'grid') {
      expect(back.root.rowSizes).toEqual([1, 4]) // col group → row tracks
      expect(back.root.colSizes).toBeUndefined()
    }
  })
})

describe('edit-model constructors', () => {
  it('newItem mints a content cell with the given rect and no container fields', () => {
    const item = newItem('files', { x: 2, y: 1, w: 1, h: 1 })
    expect(item.element).toBe('files')
    expect({ x: item.x, y: item.y, w: item.w, h: item.h }).toEqual({ x: 2, y: 1, w: 1, h: 1 })
    expect(item.id).toBeTruthy()
    expect(item.grid).toBeUndefined()
    expect(item.tabs).toBeUndefined()
  })

  it('newItem seeds an empty inner grid for a group', () => {
    const grp = newItem(GROUP, { x: 0, y: 0, w: 1, h: 1 })
    expect(grp.grid).toEqual({ cols: 2, rows: 1, items: [] })
    expect(grp.tabs).toBeUndefined()
  })

  it('newItem seeds empty tabs for a tabs container', () => {
    const tabs = newItem(TABS, { x: 0, y: 0, w: 1, h: 1 })
    expect(tabs.tabs).toEqual([])
    expect(tabs.activeTab).toBe(0)
    expect(tabs.grid).toBeUndefined()
  })

  it('addChildToTabs appends the child and selects the new tab', () => {
    const target = newItem(TABS, { x: 0, y: 0, w: 1, h: 1 })
    const first = addChildToTabs(target, newItem('chat', { x: 0, y: 0, w: 1, h: 1 }))
    expect(first.tabs).toHaveLength(1)
    expect(first.activeTab).toBe(0)
    const second = addChildToTabs(first, newItem('notes', { x: 0, y: 0, w: 1, h: 1 }))
    expect(second.tabs).toHaveLength(2)
    expect(second.activeTab).toBe(1) // newest tab is selected
    // the added child is normalized to a 1×1 rect
    const added = second.tabs && second.tabs[1]
    expect(added && { x: added.x, y: added.y, w: added.w, h: added.h }).toEqual({ x: 0, y: 0, w: 1, h: 1 })
    // input target not mutated
    expect(target.tabs).toEqual([])
  })

  it('a group container round-trips its nested children through the grid', () => {
    const grp = newItem(GROUP, { x: 0, y: 0, w: 1, h: 1 })
    grp.grid = { cols: 2, rows: 1, items: [newItem('git', { x: 0, y: 0, w: 1, h: 1 })] }
    const spec = { cols: 1, rows: 1, items: [grp] }
    const back = toTree(spec)
    // the group becomes a nested grid node holding the git cell
    expect(findItem(toEditModel(back).items, grp.id)).toBeTruthy()
    const rootChild = back.root.kind === 'grid' ? back.root.children[0].node : null
    expect(rootChild && rootChild.kind).toBe('grid')
  })

  it('an EMPTY legacy group root converts to a ≥1×1 grid (no persisted 0 dimension)', () => {
    // Regression (review): a legacy `group` root with no children yielded
    // cols:0 (row) or rows:0 (col), which firstFreeCell can never satisfy and
    // gridDim silently rewrites to 1 on the next parse — a save→reopen geometry
    // change. Both dimensions must floor to ≥ 1.
    for (const dir of ['row', 'col'] as const) {
      const emptyGroup: LayoutTree = {
        version: LAYOUT_VERSION,
        root: { kind: 'group', id: 'root', dir, children: [] },
      }
      const spec = toEditModel(emptyGroup)
      expect(spec.cols).toBeGreaterThanOrEqual(1)
      expect(spec.rows).toBeGreaterThanOrEqual(1)
      const back = toTree(spec)
      if (back.root.kind === 'grid') {
        expect(back.root.cols).toBeGreaterThanOrEqual(1)
        expect(back.root.rows).toBeGreaterThanOrEqual(1)
      }
    }
  })
})
