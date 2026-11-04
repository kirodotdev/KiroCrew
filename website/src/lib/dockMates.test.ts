import { describe, expect, it } from 'vitest'

import type { MemberRosterRow } from '../api/client'
import { dockMateOrder, starOrderWrites } from './dockMates'

const row = (name: string, starred: boolean, star_order?: number): MemberRosterRow =>
  ({ name, slug: name, slot_key: '', running: false, starred, star_order }) as MemberRosterRow

describe('dockMateOrder', () => {
  it('keeps only starred mates', () => {
    expect(dockMateOrder([row('a', true), row('b', false), row('c', true)]).map(m => m.name)).toEqual(['a', 'c'])
  })

  it('puts placed mates first, by star_order', () => {
    const out = dockMateOrder([row('a', true, 0), row('b', true, 2), row('c', true, 1)])
    expect(out.map(m => m.name)).toEqual(['c', 'b', 'a'])
  })

  it('sorts unplaced mates after placed ones, in roster order', () => {
    const out = dockMateOrder([row('new2', true), row('top', true, 1), row('new1', true, 0)])
    expect(out.map(m => m.name)).toEqual(['top', 'new2', 'new1'])
  })

  it('reads a missing star_order (older gateway) as not placed', () => {
    expect(dockMateOrder([row('a', true), row('b', true)]).map(m => m.name)).toEqual(['a', 'b'])
  })
})

describe('starOrderWrites', () => {
  const ordered = [row('a', true, 1), row('b', true, 2), row('c', true, 3)]

  it('numbers the new order and writes every row', () => {
    expect(starOrderWrites(ordered, 2, 0)).toEqual([
      { name: 'c', star_order: 1 },
      { name: 'a', star_order: 2 },
      { name: 'b', star_order: 3 },
    ])
    expect(starOrderWrites(ordered, 0, 1)).toEqual([
      { name: 'b', star_order: 1 },
      { name: 'a', star_order: 2 },
      { name: 'c', star_order: 3 },
    ])
  })

  it('still writes a row whose cached star_order already matches its new slot', () => {
    // `c` caches 3 but the server may hold another value (a stale cache).
    const stale = [row('a', true, 1), row('b', true, 2), row('c', true, 3)]
    expect(starOrderWrites(stale, 1, 0)).toContainEqual({ name: 'c', star_order: 3 })
  })

  it('places every unplaced row on the first drag', () => {
    const fresh = [row('a', true), row('b', true), row('c', true)]
    expect(starOrderWrites(fresh, 1, 0)).toEqual([
      { name: 'b', star_order: 1 },
      { name: 'a', star_order: 2 },
      { name: 'c', star_order: 3 },
    ])
  })

  it('writes nothing for a drop in place or an unknown index', () => {
    expect(starOrderWrites(ordered, 1, 1)).toEqual([])
    expect(starOrderWrites(ordered, -1, 0)).toEqual([])
    expect(starOrderWrites(ordered, 0, 3)).toEqual([])
  })
})
