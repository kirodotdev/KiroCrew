/** The crew group's spaces: the peer's folder tree, read-only, built from rows. */
import { describe, it, expect } from 'vitest'
import { crewSpaceChain, crewSpaceId, crewSpaces } from '../pages/chat-sidebar/CrewGroups'
import { peerFoldersOf, type PeerFolder } from '../hooks/useInstanceSessions'

const F = (id: string, order: number, parent_id?: string, name = id): PeerFolder =>
  ({ id, name, order, ...(parent_id ? { parent_id } : {}) })
const row = (key: string, peer_folder_id?: string) => ({ key, ...(peer_folder_id ? { peer_folder_id } : {}) })

describe('crewSpaces', () => {
  it('keeps only folders with a chat below them, sorted by the peer order', () => {
    const t = crewSpaces([F('b', 2), F('a', 1), F('empty', 0), F('kid', 0, 'b')], [row('1', 'kid'), row('2', 'a')])
    expect(t.children.get('')?.map(f => f.id)).toEqual(['a', 'b'])
    expect(t.children.get('b')?.map(f => f.id)).toEqual(['kid'])
    expect(t.total.get('b')).toBe(1)
    expect(t.unfiled).toEqual([])
  })

  it('files a row whose folder is unknown as unfiled', () => {
    const t = crewSpaces([F('a', 0)], [row('1', 'nope'), row('2')])
    expect(t.unfiled.map(r => r.key)).toEqual(['1', '2'])
    expect(t.children.size).toBe(0)
  })

  it('puts a folder whose parent was not listed at the top', () => {
    const t = crewSpaces([F('kid', 0, 'missing')], [row('1', 'kid')])
    expect(t.children.get('')?.map(f => f.id)).toEqual(['kid'])
  })

  it('places every folder of a parent cycle once, reachable from the top', () => {
    const t = crewSpaces([F('a', 0, 'b'), F('b', 1, 'a')], [row('1', 'a')])
    expect(t.total.get('a')).toBe(1)
    expect(t.total.get('b')).toBe(1)
    // Walk the tree the group renders: it ends, and it reaches the filled folder.
    const reached: string[] = []
    const walk = (id: string, depth: number) => {
      expect(depth).toBeLessThan(5)
      for (const f of t.children.get(id) ?? []) { reached.push(f.id); walk(f.id, depth + 1) }
    }
    walk('', 0)
    expect(reached.sort()).toEqual(['a', 'b'])
    expect(crewSpaceChain('c', [F('a', 0, 'b'), F('b', 0, 'a')], 'a')).toEqual([crewSpaceId('c', 'a'), crewSpaceId('c', 'b')])
  })
})

describe('crewSpaces depth', () => {
  it('bounds the nesting of a very deep chain and keeps its chat', () => {
    const N = 20_000
    const chain = Array.from({ length: N }, (_, i) => F(`f${i}`, 0, i ? `f${i - 1}` : undefined))
    const t = crewSpaces(chain, [row('leaf', `f${N - 1}`)])
    // Follow the chain down from its root, f0.
    let depth = 0
    for (let id = 'f0'; t.children.has(id); id = t.children.get(id)![0].id) depth += 1
    // `orderFoldersWithPaths` stops at 20 ancestors; every deeper folder, the
    // leaf's own with them, is surfaced at the top instead.
    expect(depth).toBe(20)
    expect(t.children.get('')?.map(f => f.id)).toContain(`f${N - 1}`)
    expect(t.rowsIn.get(`f${N - 1}`)?.map(r => r.key)).toEqual(['leaf'])
  })

  it('keeps a stored-order tie in the sidebar order, not the browser locale', () => {
    const t = crewSpaces([F('b', 0, undefined, 'beta'), F('a', 0, undefined, 'Alpha')], [row('1', 'a'), row('2', 'b')])
    expect(t.children.get('')?.map(f => f.name)).toEqual(['Alpha', 'beta'])
  })
})

describe('crewSpaceChain', () => {
  it('names the space and every space above it', () => {
    expect(crewSpaceChain('c', [F('top', 0), F('mid', 0, 'top'), F('leaf', 0, 'mid')], 'leaf'))
      .toEqual([crewSpaceId('c', 'leaf'), crewSpaceId('c', 'mid'), crewSpaceId('c', 'top')])
    expect(crewSpaceChain('c', [F('top', 0)], undefined)).toEqual([])
  })
})

describe('peerFoldersOf', () => {
  it('keeps only well-formed folders from a peer reply', () => {
    expect(peerFoldersOf({ not: 'a list' })).toEqual([])
    expect(peerFoldersOf([
      { id: 'a', name: 'A', order: 3, parent_id: 'p', project_dir: '/x' },
      { id: 'b', name: { evil: true } },
      { id: '', name: 'blank' },
      null,
      { id: 'c', name: 'C', order: 'x', parent_id: 5 },
      // A repeated id keeps its first row: a second one could close a cycle.
      { id: 'a', name: 'A again', order: 0, parent_id: 'c' },
    ])).toEqual([
      { id: 'a', name: 'A', order: 3, parent_id: 'p' },
      { id: 'c', name: 'C', order: 0 },
    ])
  })
})
