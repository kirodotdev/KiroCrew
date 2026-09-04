import { describe, it, expect } from 'vitest'
import {
  childFolders,
  isDescendantFolder,
  subtreeFolderIds,
  folderSubtreeStats,
  folderBreadcrumb,
} from '../utils/artifactFolderTree'
import type { ArtifactFolder } from '../types'

// Tree used across cases:
//   a (2 artifacts)
//   ├── b (1 artifact)
//   │   └── c (3 artifacts)
//   └── d (0 artifacts)
//   e (root sibling, 5 artifacts)
const F = (id: string, name: string, order: number, parent = '', count = 0): ArtifactFolder =>
  ({ id, name, order, parent_id: parent, item_count: count })

const tree: ArtifactFolder[] = [
  F('a', 'Alpha', 0, '', 2),
  F('b', 'Beta', 0, 'a', 1),
  F('c', 'Gamma', 0, 'b', 3),
  F('d', 'Delta', 1, 'a', 0),
  F('e', 'Echo', 1, '', 5),
]

describe('childFolders', () => {
  it('returns direct children of root sorted alphabetically', () => {
    expect(childFolders(tree, '').map(f => f.id)).toEqual(['a', 'e'])
  })

  it('returns direct children of a nested folder', () => {
    expect(childFolders(tree, 'a').map(f => f.id)).toEqual(['b', 'd'])
  })

  it('sorts alphabetically regardless of the order field', () => {
    // Persistent containers sort by name (predictable placement), NOT by the
    // legacy order field — even when order says otherwise.
    const t = [F('x', 'Zed', 0), F('y', 'apple', 5)]
    expect(childFolders(t, '').map(f => f.id)).toEqual(['y', 'x'])
  })

  it('is case-insensitive', () => {
    const t = [F('x', 'banana', 0), F('y', 'Apple', 1), F('z', 'Cherry', 2)]
    expect(childFolders(t, '').map(f => f.id)).toEqual(['y', 'x', 'z'])
  })

  it('treats orphans (dangling parent_id) as roots', () => {
    const t = [F('x', 'X', 0, 'missing'), F('y', 'Y', 1)]
    expect(childFolders(t, '').map(f => f.id)).toEqual(['x', 'y'])
  })
})

describe('isDescendantFolder', () => {
  it('is true for the folder itself', () => {
    expect(isDescendantFolder(tree, 'a', 'a')).toBe(true)
  })

  it('is true for a direct child and a deep descendant', () => {
    expect(isDescendantFolder(tree, 'a', 'b')).toBe(true)
    expect(isDescendantFolder(tree, 'a', 'c')).toBe(true)
  })

  it('is false for siblings, ancestors, and unrelated folders', () => {
    expect(isDescendantFolder(tree, 'b', 'a')).toBe(false)
    expect(isDescendantFolder(tree, 'a', 'e')).toBe(false)
    expect(isDescendantFolder(tree, 'd', 'c')).toBe(false)
  })

  it('is false for empty ids (root cannot be a descendant)', () => {
    expect(isDescendantFolder(tree, 'a', '')).toBe(false)
    expect(isDescendantFolder(tree, '', 'a')).toBe(false)
  })

  it('terminates on a parent_id cycle', () => {
    const cyclic = [F('x', 'X', 0, 'y'), F('y', 'Y', 0, 'x')]
    expect(isDescendantFolder(cyclic, 'z', 'x')).toBe(false)
  })
})

describe('subtreeFolderIds', () => {
  it('returns the whole subtree inclusive, pre-order', () => {
    expect(subtreeFolderIds(tree, 'a')).toEqual(['a', 'b', 'c', 'd'])
  })

  it('returns just the folder for a leaf', () => {
    expect(subtreeFolderIds(tree, 'c')).toEqual(['c'])
  })
})

describe('folderSubtreeStats', () => {
  it('sums artifact counts across the subtree and counts subfolders', () => {
    expect(folderSubtreeStats(tree, 'a')).toEqual({ artifactCount: 6, subfolderCount: 3 })
  })

  it('is the direct count with zero subfolders for a leaf', () => {
    expect(folderSubtreeStats(tree, 'e')).toEqual({ artifactCount: 5, subfolderCount: 0 })
  })

  it('tolerates missing item_count', () => {
    const t = [{ id: 'x', name: 'X', order: 0 } as ArtifactFolder]
    expect(folderSubtreeStats(t, 'x')).toEqual({ artifactCount: 0, subfolderCount: 0 })
  })
})

describe('folderBreadcrumb', () => {
  it('returns the root→leaf chain inclusive', () => {
    expect(folderBreadcrumb(tree, 'c').map(f => f.id)).toEqual(['a', 'b', 'c'])
  })

  it('returns a single-element chain for a root folder', () => {
    expect(folderBreadcrumb(tree, 'e').map(f => f.id)).toEqual(['e'])
  })

  it('returns empty for an unknown id', () => {
    expect(folderBreadcrumb(tree, 'nope')).toEqual([])
  })

  it('truncates at an orphaned ancestor instead of looping', () => {
    const t = [F('x', 'X', 0, 'missing'), F('y', 'Y', 0, 'x')]
    expect(folderBreadcrumb(t, 'y').map(f => f.id)).toEqual(['x', 'y'])
  })

  it('terminates on a parent_id cycle', () => {
    const cyclic = [F('x', 'X', 0, 'y'), F('y', 'Y', 0, 'x')]
    const chain = folderBreadcrumb(cyclic, 'x')
    expect(chain.length).toBeLessThanOrEqual(2)
    expect(chain.map(f => f.id)).toContain('x')
  })
})
