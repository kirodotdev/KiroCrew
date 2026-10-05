import { describe, it, expect } from 'vitest'
import { computeActiveSubtree, folderIsHidden, folderOffersHide, computePinnedSubtree, computePinnedAncestors } from '../utils/folderVisibility'
import type { ChatFolder } from '../types'

const folders: ChatFolder[] = [
  { id: 'root', name: 'Root', order: 0, parent_id: '' },
  { id: 'child', name: 'Child', order: 1, parent_id: 'root' },
  { id: 'grandchild', name: 'Grandchild', order: 2, parent_id: 'child' },
  { id: 'other', name: 'Other', order: 3, parent_id: '' },
]

describe('computeActiveSubtree', () => {
  it('returns empty set when no folders hold slots', () => {
    expect(computeActiveSubtree(folders, []).size).toBe(0)
  })

  it('includes a folder with a direct slot', () => {
    expect([...computeActiveSubtree(folders, ['other'])]).toEqual(['other'])
  })

  it('propagates active membership up the whole ancestor chain', () => {
    const active = computeActiveSubtree(folders, ['grandchild'])
    expect(active.has('grandchild')).toBe(true)
    expect(active.has('child')).toBe(true)
    expect(active.has('root')).toBe(true)
    expect(active.has('other')).toBe(false)
  })

  it('does not loop or duplicate on shared ancestors', () => {
    const active = computeActiveSubtree(folders, ['grandchild', 'child'])
    expect([...active].sort()).toEqual(['child', 'grandchild', 'root'])
  })
})

describe('folderIsHidden', () => {
  const active = computeActiveSubtree(folders, ['other'])

  it('hides a hidden folder with no active session', () => {
    expect(folderIsHidden({ ...folders[0], hidden: true }, active)).toBe(true)
  })

  it('keeps a hidden folder visible while its subtree has an active session', () => {
    expect(folderIsHidden({ ...folders[3], hidden: true }, active)).toBe(false)
  })

  it('never hides a folder that was not hidden', () => {
    expect(folderIsHidden({ ...folders[0], hidden: false }, active)).toBe(false)
    expect(folderIsHidden(folders[0], active)).toBe(false)
  })
})

describe('folderOffersHide', () => {
  const active = computeActiveSubtree(folders, ['other'])

  it('offers hide for an empty folder that still has archived sessions (A=0, H>0)', () => {
    expect(folderOffersHide({ ...folders[0], history_count: 2 }, active)).toBe(true)
  })

  it('does not offer hide while the subtree has an active session (A>0)', () => {
    // `other` is in the active set; even with archived sessions, no hide is offered.
    expect(folderOffersHide({ ...folders[3], history_count: 5 }, active)).toBe(false)
  })

  it('offers Delete only for a truly empty folder — no active, no history (A=0, H=0)', () => {
    expect(folderOffersHide({ ...folders[0], history_count: 0 }, active)).toBe(false)
    // history_count absent is treated as 0.
    expect(folderOffersHide(folders[0], active)).toBe(false)
  })
})

describe('computePinnedSubtree', () => {
  it('returns an empty set when no folder is pinned', () => {
    expect(computePinnedSubtree(folders).size).toBe(0)
  })

  it('covers the pinned folder and everything under it, not its siblings or ancestors', () => {
    const pinned = folders.map(f => (f.id === 'child' ? { ...f, pinned: true } : f))
    const covered = computePinnedSubtree(pinned)
    expect([...covered].sort()).toEqual(['child', 'grandchild'])
  })

  it('a pin on the root covers the whole tree below it', () => {
    const pinned = folders.map(f => (f.id === 'root' ? { ...f, pinned: true } : f))
    expect([...computePinnedSubtree(pinned)].sort()).toEqual(['child', 'grandchild', 'root'])
  })

  it('terminates on a parent_id cycle', () => {
    const cyclic: ChatFolder[] = [
      { id: 'a', name: 'A', order: 0, parent_id: 'b' },
      { id: 'b', name: 'B', order: 1, parent_id: 'a' },
    ]
    expect(computePinnedSubtree(cyclic).size).toBe(0)
  })
})

describe('computePinnedAncestors', () => {
  it('returns an empty set when no folder is pinned', () => {
    expect(computePinnedAncestors(folders).size).toBe(0)
  })

  it('names the chain above the pinned folder, not the folder, its children or its siblings', () => {
    const pinned = folders.map(f => (f.id === 'grandchild' ? { ...f, pinned: true } : f))
    expect([...computePinnedAncestors(pinned)].sort()).toEqual(['child', 'root'])
  })

  it('a pinned root has no ancestors', () => {
    const pinned = folders.map(f => (f.id === 'root' ? { ...f, pinned: true } : f))
    expect(computePinnedAncestors(pinned).size).toBe(0)
  })

  it('terminates on a parent_id cycle', () => {
    const cyclic: ChatFolder[] = [
      { id: 'a', name: 'A', order: 0, parent_id: 'b', pinned: true },
      { id: 'b', name: 'B', order: 1, parent_id: 'a' },
    ]
    expect([...computePinnedAncestors(cyclic)]).toEqual(['b'])
  })
})
