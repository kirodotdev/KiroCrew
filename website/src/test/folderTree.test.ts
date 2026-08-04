/**
 * folderTree.orderFoldersWithPaths is the shared tree-ordering used by the
 * folder pickers (move-to-folder submenu, new-chat-in-folder). The
 * ordering/path/depth logic is unit-testable on its own — independent of the
 * Radix submenu it feeds.
 */
import { describe, it, expect } from 'vitest'
import { orderFoldersWithPaths, FOLDER_PATH_SEP } from '../utils/folderTree'
import type { ChatFolder } from '../types'

const nested: ChatFolder[] = [
  { id: 'p1', name: 'Work', order: 0 },
  { id: 'p2', name: 'Personal', order: 1 },
  { id: 'c1', name: 'Drafts', order: 0, parent_id: 'p1' },
  { id: 'c2', name: 'Drafts', order: 0, parent_id: 'p2' },
]

describe('orderFoldersWithPaths', () => {
  it('uses U+203A as the breadcrumb separator (matches server folder_breadcrumb)', () => {
    expect(FOLDER_PATH_SEP).toBe(' › ')
  })

  it('orders children directly under their parent (pre-order tree)', () => {
    const paths = orderFoldersWithPaths(nested).map(o => o.path)
    expect(paths).toEqual(['Work', 'Work › Drafts', 'Personal', 'Personal › Drafts'])
  })

  it('computes depth (0 for roots, +1 per level) and ancestor names', () => {
    const byId = new Map(orderFoldersWithPaths(nested).map(o => [o.folder.id, o]))
    expect(byId.get('p1')!.depth).toBe(0)
    expect(byId.get('p1')!.ancestors).toEqual([])
    expect(byId.get('c1')!.depth).toBe(1)
    expect(byId.get('c1')!.ancestors).toEqual(['Work'])
  })

  it('keeps the full ancestry path so same-named subfolders stay unambiguous', () => {
    const byId = new Map(orderFoldersWithPaths(nested).map(o => [o.folder.id, o]))
    // Both subfolders are named "Drafts"; their paths disambiguate them.
    expect(byId.get('c1')!.path).toBe('Work › Drafts')
    expect(byId.get('c2')!.path).toBe('Personal › Drafts')
    // Root folders keep their bare name as the path.
    expect(byId.get('p1')!.path).toBe('Work')
  })

  it('sorts siblings by order then name', () => {
    const unordered: ChatFolder[] = [
      { id: 'b', name: 'Bravo', order: 1 },
      { id: 'a', name: 'Alpha', order: 0 },
      { id: 'c', name: 'Charlie', order: 1 }, // same order as Bravo → tiebreak by name
    ]
    expect(orderFoldersWithPaths(unordered).map(o => o.folder.name)).toEqual(['Alpha', 'Bravo', 'Charlie'])
  })

  it('treats an orphan parent_id (missing parent) as a root', () => {
    const orphan: ChatFolder[] = [{ id: 'x', name: 'Orphan', order: 0, parent_id: 'ghost' }]
    const out = orderFoldersWithPaths(orphan)
    expect(out).toHaveLength(1)
    expect(out[0].depth).toBe(0)
    expect(out[0].path).toBe('Orphan')
  })

  it('survives a parent↔child cycle without infinite recursion', () => {
    const cyclic: ChatFolder[] = [
      { id: 'a', name: 'A', order: 0, parent_id: 'b' },
      { id: 'b', name: 'B', order: 0, parent_id: 'a' },
    ]
    const out = orderFoldersWithPaths(cyclic)
    // Both surface (cycle guard + safety-net), none duplicated.
    expect(new Set(out.map(o => o.folder.id))).toEqual(new Set(['a', 'b']))
  })
})

// ── collectFolderSubtreeIds: the acyclicity guard for folder re-parenting ──
// Both the "Move folder to" submenu (excludes self+descendants from targets)
// and drag re-parenting (excludes them from drop collision candidates) rely on
// this returning exactly the folder's own subtree.
import { collectFolderSubtreeIds } from '../utils/folderTree'

describe('collectFolderSubtreeIds', () => {
  const tree: ChatFolder[] = [
    { id: 'a', name: 'A', order: 0 },
    { id: 'b', name: 'B', order: 0, parent_id: 'a' },
    { id: 'c', name: 'C', order: 0, parent_id: 'b' },
    { id: 'x', name: 'X', order: 1 },
    { id: 'y', name: 'Y', order: 0, parent_id: 'x' },
  ]

  it('returns the folder itself plus all descendants, transitively', () => {
    expect([...collectFolderSubtreeIds(tree, 'a')].sort()).toEqual(['a', 'b', 'c'])
  })

  it('returns only the folder itself for a leaf', () => {
    expect([...collectFolderSubtreeIds(tree, 'c')]).toEqual(['c'])
  })

  it('does not leak unrelated branches', () => {
    const ids = collectFolderSubtreeIds(tree, 'a')
    expect(ids.has('x')).toBe(false)
    expect(ids.has('y')).toBe(false)
  })

  it('terminates on a corrupt parent_id cycle', () => {
    const cyclic: ChatFolder[] = [
      { id: 'p', name: 'P', order: 0, parent_id: 'q' },
      { id: 'q', name: 'Q', order: 0, parent_id: 'p' },
    ]
    expect([...collectFolderSubtreeIds(cyclic, 'p')].sort()).toEqual(['p', 'q'])
  })
})
