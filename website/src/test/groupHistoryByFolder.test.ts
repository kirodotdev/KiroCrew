import { describe, it, expect } from 'vitest'
import { groupHistoryByFolder, UNFILED_GROUP_KEY } from '../utils/groupHistoryByFolder'
import type { ChatFolder } from '../types'

const folder = (id: string, order: number, parent_id?: string): ChatFolder =>
  ({ id, name: id.toUpperCase(), order, parent_id })
type Row = { key: string; folder_id?: string }
const row = (key: string, folder_id?: string): Row => ({ key, folder_id })

describe('groupHistoryByFolder', () => {
  it('buckets by folder, orders groups by sidebar order, Unfiled last, preserves row order', () => {
    const folders = [folder('b', 1), folder('a', 0)]
    const rows = [row('r1', 'b'), row('r2'), row('r3', 'a'), row('r4', 'b')]
    const groups = groupHistoryByFolder(rows, folders)
    expect(groups.map(g => g.key)).toEqual(['a', 'b', UNFILED_GROUP_KEY])
    expect(groups[1].rows.map(r => r.key)).toEqual(['r1', 'r4'])
    expect(groups[2].folder).toBeUndefined()
    expect(groups[2].rows.map(r => r.key)).toEqual(['r2'])
  })

  it('routes a row whose folder_id points at a deleted folder to Unfiled', () => {
    const groups = groupHistoryByFolder([row('r1', 'ghost')], [folder('a', 0)])
    expect(groups).toHaveLength(1)
    expect(groups[0].key).toBe(UNFILED_GROUP_KEY)
  })

  it('orders a nested folder depth-first, immediately after its parent', () => {
    const folders = [folder('p', 0), folder('child', 0, 'p'), folder('q', 1)]
    const rows = [row('r1', 'q'), row('r2', 'child'), row('r3', 'p')]
    const groups = groupHistoryByFolder(rows, folders)
    expect(groups.map(g => g.key)).toEqual(['p', 'child', 'q'])
  })
})
