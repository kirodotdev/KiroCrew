import { describe, it, expect } from 'vitest'
import type { ChatFolder } from '../types'
import { computeSiblingMove } from '../utils/reorderFolders'
import { bySidebarOrder } from '../utils/folderTree'

const legacy = (id: string, name: string, order: number, parent_id = ''): ChatFolder =>
  ({ id, name, order, collapsed: false, parent_id }) as ChatFolder

/** Apply a move's optimistic ranks, then read the container as the sidebar draws it. */
const drawn = (folders: ChatFolder[], move: ReturnType<typeof computeSiblingMove>, parent = ''): string[] =>
  folders
    .map(f => (move?.ranks.has(f.id) ? { ...f, rank: move.ranks.get(f.id) } : f))
    .filter(f => (f.parent_id || '') === parent)
    .sort(bySidebarOrder)
    .map(f => f.id)

describe('computeSiblingMove', () => {
  // Two containers with DIFFERENT ids in each, so a change leaking across them
  // is visible rather than hidden behind a coincidence of indices.
  const tree = [
    legacy('root-a', 'Alpha', 0),
    legacy('root-b', 'Bravo', 1),
    legacy('root-c', 'Charlie', 2),
    legacy('kid-x', 'Xray', 0, 'root-a'),
    legacy('kid-y', 'Yankee', 1, 'root-a'),
    legacy('kid-z', 'Zulu', 2, 'root-a'),
  ]

  it('dragging down lands AFTER the target, dragging up lands BEFORE it', () => {
    expect(computeSiblingMove(tree, 'root-a', 'root-c')?.anchor).toEqual({ after: 'root-c' })
    expect(computeSiblingMove(tree, 'root-c', 'root-a')?.anchor).toEqual({ before: 'root-a' })
  })

  it('draws the move the gateway will write, spreading a legacy section once', () => {
    const move = computeSiblingMove(tree, 'kid-z', 'kid-x')
    expect(drawn(tree, move, 'root-a')).toEqual(['kid-z', 'kid-x', 'kid-y'])
    // Every sibling in the legacy section gets a rank; nothing outside it does.
    expect([...(move?.ranks.keys() ?? [])].sort()).toEqual(['kid-x', 'kid-y', 'kid-z'])
  })

  it('in a ranked section re-ranks only the moved folder', () => {
    const ranked = [
      { ...legacy('a', 'A', 0), rank: 'F' },
      { ...legacy('b', 'B', 1), rank: 'V' },
      { ...legacy('c', 'C', 2), rank: 'k' },
    ]
    const move = computeSiblingMove(ranked, 'c', 'b')
    expect(move?.anchor).toEqual({ before: 'b' })
    expect([...(move?.ranks.keys() ?? [])]).toEqual(['c'])
    expect(drawn(ranked, move)).toEqual(['a', 'c', 'b'])
  })

  it('moves nothing when the target sits in another container', () => {
    // A cross-container drop is a RE-PARENT, and the collision layer routes it
    // as one.
    expect(computeSiblingMove(tree, 'kid-z', 'root-b')).toBeNull()
    expect(computeSiblingMove(tree, 'root-b', 'kid-z')).toBeNull()
  })

  it('returns null for a no-op drag or an unknown folder', () => {
    expect(computeSiblingMove(tree, 'kid-x', 'kid-x')).toBeNull()
    expect(computeSiblingMove(tree, 'ghost', 'kid-x')).toBeNull()
    expect(computeSiblingMove(tree, 'kid-x', 'ghost')).toBeNull()
  })

  it('scopes an orphan to the root lane, where the sidebar draws it', () => {
    const orphaned = [
      legacy('root-a', 'Alpha', 0),
      legacy('root-b', 'Bravo', 1),
      legacy('lost', 'Lost', 2, 'folder-deleted'),
    ]
    const move = computeSiblingMove(orphaned, 'lost', 'root-a')
    expect(move?.anchor).toEqual({ before: 'root-a' })
  })

  it('takes its baseline from the order the sidebar draws, tie-break included', () => {
    // Zulu and Alpha share order 0 and arrive Zulu-first; the sidebar draws
    // Alpha, Zulu, Mike. Dragging Mike onto Alpha must land it first.
    const tied = [legacy('zulu', 'Zulu', 0), legacy('alpha', 'Alpha', 0), legacy('mike', 'Mike', 1)]
    const move = computeSiblingMove(tied, 'mike', 'alpha')
    expect(move?.anchor).toEqual({ before: 'alpha' })
    expect(drawn(tied, move)).toEqual(['mike', 'alpha', 'zulu'])
  })
})
