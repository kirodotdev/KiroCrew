import { describe, it, expect } from 'vitest'
import { computeReorderedFolders } from '../utils/reorderFolders'

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
})
