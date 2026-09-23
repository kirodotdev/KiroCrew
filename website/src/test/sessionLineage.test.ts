/**
 * The shared lineage fold — the rules two views both depend on.
 *
 * These are written against the minimal `LineageRow` shape rather than either page's
 * own row type, because that is the contract: the System page and the chat sidebar
 * hand this module different payloads with different key spaces, and it must place
 * both the same way.
 */
import { describe, expect, it } from 'vitest'

import {
  ancestorsOf,
  buildLineage,
  descendantsOf,
  nestsUnder,
  orphanCitation,
  type LineageRow,
} from '../lib/sessionLineage'

/** A row with no creator. */
const root = (key: string): LineageRow => ({ key })

/** A row whose creator is running and present in the payload. */
const child = (key: string, parentKey: string): LineageRow => ({
  key,
  parent: { slot: parentKey, key: parentKey },
})

/** A row citing a creator that has closed: the orphan case. */
const orphan = (key: string, citedSlot: string): LineageRow => ({
  key,
  parent: { slot: citedSlot, key: null },
})

describe('nestsUnder', () => {
  it('follows an edge to a creator that is present', () => {
    const rows = [root('a'), child('b', 'a')]
    const byKey = new Map(rows.map(r => [r.key, r] as const))
    expect(nestsUnder(rows[1], byKey)).toBe('a')
  })

  it('refuses a creator that names no row in this payload', () => {
    const rows = [child('b', 'gone')]
    const byKey = new Map(rows.map(r => [r.key, r] as const))
    expect(nestsUnder(rows[0], byKey)).toBeNull()
  })

  it('refuses a row that cites itself', () => {
    const rows: LineageRow[] = [{ key: 'a', parent: { slot: 'a', key: 'a' } }]
    const byKey = new Map(rows.map(r => [r.key, r] as const))
    expect(nestsUnder(rows[0], byKey)).toBeNull()
  })

  it('detaches every member of a cycle', () => {
    const rows = [child('a', 'b'), child('b', 'a')]
    const byKey = new Map(rows.map(r => [r.key, r] as const))
    expect(nestsUnder(rows[0], byKey)).toBeNull()
    expect(nestsUnder(rows[1], byKey)).toBeNull()
  })

  it('keeps the edge of a row that merely hangs off a cycle', () => {
    // a <-> b is a cycle; c cites b. Both cycle members detach, so b is a root and
    // c's edge to it is safe.
    const rows = [child('a', 'b'), child('b', 'a'), child('c', 'b')]
    const byKey = new Map(rows.map(r => [r.key, r] as const))
    expect(nestsUnder(rows[2], byKey)).toBe('b')
  })

  it('treats an absent parent and a null key alike', () => {
    const rows = [root('a'), orphan('b', 'closed-one')]
    const byKey = new Map(rows.map(r => [r.key, r] as const))
    expect(nestsUnder(rows[0], byKey)).toBeNull()
    expect(nestsUnder(rows[1], byKey)).toBeNull()
  })
})

describe('buildLineage', () => {
  it('places a child under its creator and leaves the creator a root', () => {
    const { roots, children, parentOf, depth } = buildLineage([root('a'), child('b', 'a')])
    expect(roots).toEqual(['a'])
    expect(children.get('a')).toEqual(['b'])
    expect(parentOf.get('b')).toBe('a')
    expect(depth.get('a')).toBe(0)
    expect(depth.get('b')).toBe(1)
  })

  it('nests to whatever depth the creating went', () => {
    const { roots, depth } = buildLineage([root('a'), child('b', 'a'), child('c', 'b')])
    expect(roots).toEqual(['a'])
    expect(depth.get('c')).toBe(2)
  })

  it('preserves the order it was handed, at every level', () => {
    // Roots in input order, siblings in input order. The lane sorted them already.
    const rows = [root('r2'), root('r1'), child('c2', 'r1'), child('c1', 'r1')]
    const { roots, children } = buildLineage(rows)
    expect(roots).toEqual(['r2', 'r1'])
    expect(children.get('r1')).toEqual(['c2', 'c1'])
  })

  it('makes an orphan a root that still carries its citation', () => {
    const rows = [orphan('b', 'closed-one')]
    const { roots, parentOf } = buildLineage(rows)
    expect(roots).toEqual(['b'])
    expect(parentOf.has('b')).toBe(false)
    expect(orphanCitation(rows[0], null)).toBe('closed-one')
  })

  it('reports no citation for a row that was placed', () => {
    expect(orphanCitation(child('b', 'a'), 'a')).toBeNull()
  })

  it('reports no citation for a row nobody created', () => {
    expect(orphanCitation(root('a'), null)).toBeNull()
  })

  it('makes both members of a cycle roots', () => {
    const { roots, children } = buildLineage([child('a', 'b'), child('b', 'a')])
    expect(roots).toEqual(['a', 'b'])
    expect(children.size).toBe(0)
  })

  it('drops a row with no key rather than keying the tree on an empty string', () => {
    const { roots } = buildLineage([{ key: '' }, root('a')])
    expect(roots).toEqual(['a'])
  })

  it('handles an empty payload', () => {
    const { roots, children, depth } = buildLineage([])
    expect(roots).toEqual([])
    expect(children.size).toBe(0)
    expect(depth.size).toBe(0)
  })
})

describe('ancestorsOf', () => {
  it('lists ancestors nearest first', () => {
    const { parentOf } = buildLineage([root('a'), child('b', 'a'), child('c', 'b')])
    expect(ancestorsOf('c', parentOf)).toEqual(['b', 'a'])
  })

  it('is empty for a root and for an unknown key', () => {
    const { parentOf } = buildLineage([root('a')])
    expect(ancestorsOf('a', parentOf)).toEqual([])
    expect(ancestorsOf('nope', parentOf)).toEqual([])
  })
})

describe('descendantsOf', () => {
  it('collects the whole subtree, excluding the row itself', () => {
    const { children } = buildLineage([
      root('a'),
      child('b', 'a'),
      child('c', 'b'),
      child('d', 'a'),
    ])
    expect(descendantsOf('a', children).sort()).toEqual(['b', 'c', 'd'])
    expect(descendantsOf('b', children)).toEqual(['c'])
    expect(descendantsOf('c', children)).toEqual([])
  })
})
