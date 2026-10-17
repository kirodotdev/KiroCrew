import { describe, it, expect } from 'vitest'
import { computeReorderedFolders, computeSiblingReorder } from '../utils/reorderFolders'

const folders = [
  { id: 'f-1', name: 'Alpha', order: 0, collapsed: false, parent_id: '' },
  { id: 'f-2', name: 'Beta', order: 1, collapsed: false, parent_id: '' },
  { id: 'f-3', name: 'Gamma', order: 2, collapsed: false, parent_id: '' },
]

describe('computeReorderedFolders', () => {
  it('returns empty array for no-op drag (same id)', () => {
    expect(computeReorderedFolders(folders, 'f-1', 'f-1')).toEqual([])
  })

  it('returns empty array for unknown active id', () => {
    expect(computeReorderedFolders(folders, 'f-99', 'f-1')).toEqual([])
  })

  it('returns empty array for unknown over id', () => {
    // This refusal is what makes a cross-container drop safe for
    // computeSiblingReorder: the target of a re-parent gesture is absent from the
    // sibling list it renumbers, so no row is renumbered for a move that is not a
    // reorder.
    expect(computeReorderedFolders(folders, 'f-1', 'f-99')).toEqual([])
  })

  it('computes correct order when moving first to last', () => {
    const result = computeReorderedFolders(folders, 'f-1', 'f-3')
    expect(result).toContainEqual({ id: 'f-1', order: 2 })
    expect(result).toContainEqual({ id: 'f-2', order: 0 })
    expect(result).toContainEqual({ id: 'f-3', order: 1 })
  })

  it('computes correct order when moving last to first', () => {
    const result = computeReorderedFolders(folders, 'f-3', 'f-1')
    expect(result).toContainEqual({ id: 'f-3', order: 0 })
    expect(result).toContainEqual({ id: 'f-1', order: 1 })
    expect(result).toContainEqual({ id: 'f-2', order: 2 })
  })

  it('computes correct order for adjacent swap', () => {
    const result = computeReorderedFolders(folders, 'f-1', 'f-2')
    expect(result).toContainEqual({ id: 'f-1', order: 1 })
    expect(result).toContainEqual({ id: 'f-2', order: 0 })
    // f-3 unchanged
    expect(result.find(r => r.id === 'f-3')).toBeUndefined()
  })

  it('handles unsorted input folders', () => {
    const unsorted = [
      { id: 'f-3', name: 'Gamma', order: 2, collapsed: false, parent_id: '' },
      { id: 'f-1', name: 'Alpha', order: 0, collapsed: false, parent_id: '' },
      { id: 'f-2', name: 'Beta', order: 1, collapsed: false, parent_id: '' },
    ]
    const result = computeReorderedFolders(unsorted, 'f-3', 'f-1')
    expect(result).toContainEqual({ id: 'f-3', order: 0 })
  })

  it('takes its baseline from the order the sidebar draws, tie-break included', () => {
    // The baseline decides which index each folder moves FROM. An order-only sort
    // leaves a duplicate-order pair in cache position while the sidebar draws it by
    // name, so the two disagree and the drag computes a move the person did not
    // make. Here Zulu and Alpha share order 0 and arrive Zulu-first.
    const tied = [
      { id: 'zulu', name: 'Zulu', order: 0, collapsed: false, parent_id: '' },
      { id: 'alpha', name: 'Alpha', order: 0, collapsed: false, parent_id: '' },
      { id: 'last', name: 'Mike', order: 1, collapsed: false, parent_id: '' },
    ]
    // Rendered order is Alpha, Zulu, Mike. Dragging Mike onto Alpha's slot must
    // land it first, which is only true if the baseline agreed with the render.
    const result = computeReorderedFolders(tied, 'last', 'alpha')
    expect(result).toContainEqual({ id: 'last', order: 0 })
    expect(result).toContainEqual({ id: 'alpha', order: 1 })
    expect(result).toContainEqual({ id: 'zulu', order: 2 })
  })
})

/**
 * Sibling-scoped reorder — the half that makes a NESTED subfolder draggable.
 *
 * Before this, the sidebar renumbered over `folders.filter(f => !f.parent_id)`,
 * so a nested row had no reorder to compute even if a drag reached the call: its
 * id is absent from that list, and `computeReorderedFolders` returns `[]` for an
 * unknown active id. An agent could position it with `chat_folder_move`'s
 * `before` / `after` and a person could not.
 */
describe('computeSiblingReorder', () => {
  // Two containers with DIFFERENT ids in each, so a change leaking across them
  // is visible rather than hidden behind a coincidence of indices.
  const tree = [
    { id: 'root-a', name: 'Alpha', order: 0, collapsed: false, parent_id: '' },
    { id: 'root-b', name: 'Bravo', order: 1, collapsed: false, parent_id: '' },
    { id: 'root-c', name: 'Charlie', order: 2, collapsed: false, parent_id: '' },
    { id: 'kid-x', name: 'Xray', order: 0, collapsed: false, parent_id: 'root-a' },
    { id: 'kid-y', name: 'Yankee', order: 1, collapsed: false, parent_id: 'root-a' },
    { id: 'kid-z', name: 'Zulu', order: 2, collapsed: false, parent_id: 'root-a' },
  ]

  it('reorders a nested subfolder among its own siblings', () => {
    const result = computeSiblingReorder(tree, 'kid-z', 'kid-x')
    expect(result).toContainEqual({ id: 'kid-z', order: 0 })
    expect(result).toContainEqual({ id: 'kid-x', order: 1 })
    expect(result).toContainEqual({ id: 'kid-y', order: 2 })
  })

  it('makes every order entry a pair of positions and nothing else', () => {
    // The endpoint reorders and never reparents, and the request's SHAPE is what
    // makes that auditable: an entry naming a parent would be indistinguishable
    // from a move, and a reader would have to know the handler only reads that
    // field to tell the two apart. This PR widens which rows can be renumbered,
    // so it is exactly the change that could have widened the request too. The
    // sidebar pins the same property one level up; pinning it on the helper is
    // what keeps a later field from being folded into an entry here.
    for (const c of computeSiblingReorder(tree, 'kid-z', 'kid-x')) {
      expect(Object.keys(c).sort()).toEqual(['id', 'order'])
    }
  })

  it('leaves every folder outside the dragged row container untouched', () => {
    // The renumber is per-container, so a nested drag must not restate a root
    // row's order. A whole-list renumber would give the roots indices from an
    // interleaved sequence the sidebar never draws.
    const touched = computeSiblingReorder(tree, 'kid-z', 'kid-x').map(c => c.id)
    expect(touched).not.toContain('root-a')
    expect(touched).not.toContain('root-b')
    expect(touched).not.toContain('root-c')
  })

  it('still reorders root folders, scoped to the root lane', () => {
    const result = computeSiblingReorder(tree, 'root-a', 'root-c')
    expect(result).toContainEqual({ id: 'root-a', order: 2 })
    expect(result).toContainEqual({ id: 'root-b', order: 0 })
    expect(result).toContainEqual({ id: 'root-c', order: 1 })
    expect(result.some(c => c.id.startsWith('kid-'))).toBe(false)
  })

  it('renumbers nothing when the target sits in another container', () => {
    // A cross-container drop is a RE-PARENT, and the collision layer routes it
    // as one. Renumbering here would shuffle the dragged row's own siblings for
    // a move that never happened.
    expect(computeSiblingReorder(tree, 'kid-z', 'root-b')).toEqual([])
    expect(computeSiblingReorder(tree, 'root-b', 'kid-z')).toEqual([])
  })

  it('returns no changes for a no-op drag or an unknown active id', () => {
    expect(computeSiblingReorder(tree, 'kid-x', 'kid-x')).toEqual([])
    expect(computeSiblingReorder(tree, 'ghost', 'kid-x')).toEqual([])
  })

  it('scopes an orphan to the root lane, where the sidebar draws it', () => {
    // `parent_id` naming a folder that is not in the list: every render path
    // treats that row as a root, so its sibling ring is the root lane. Scoping it
    // to the missing id instead would leave it alone in a container nothing
    // draws, and the drag onto a visible root neighbour would compute no move.
    const orphaned = [
      { id: 'root-a', name: 'Alpha', order: 0, collapsed: false, parent_id: '' },
      { id: 'root-b', name: 'Bravo', order: 1, collapsed: false, parent_id: '' },
      { id: 'lost', name: 'Lost', order: 2, collapsed: false, parent_id: 'folder-deleted' },
    ]
    const result = computeSiblingReorder(orphaned, 'lost', 'root-a')
    expect(result).toContainEqual({ id: 'lost', order: 0 })
    expect(result).toContainEqual({ id: 'root-a', order: 1 })
    expect(result).toContainEqual({ id: 'root-b', order: 2 })
  })

  it('takes its baseline from the sidebar comparator inside the container', () => {
    // Same property the root-lane test above pins, asserted one level down: the
    // nested group is drawn with `bySidebarOrder`, so a duplicate-order pair is
    // sequenced by name and the from-index has to agree with that.
    const tied = [
      { id: 'parent', name: 'Parent', order: 0, collapsed: false, parent_id: '' },
      { id: 'kid-zulu', name: 'Zulu', order: 0, collapsed: false, parent_id: 'parent' },
      { id: 'kid-alpha', name: 'Alpha', order: 0, collapsed: false, parent_id: 'parent' },
      { id: 'kid-mike', name: 'Mike', order: 1, collapsed: false, parent_id: 'parent' },
    ]
    // Drawn Alpha, Zulu, Mike. Dragging Mike onto Alpha's slot lands it first.
    const result = computeSiblingReorder(tied, 'kid-mike', 'kid-alpha')
    expect(result).toContainEqual({ id: 'kid-mike', order: 0 })
    expect(result).toContainEqual({ id: 'kid-alpha', order: 1 })
    expect(result).toContainEqual({ id: 'kid-zulu', order: 2 })
  })
})
