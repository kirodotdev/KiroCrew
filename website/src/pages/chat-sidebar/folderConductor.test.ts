/**
 * Conductor detection, stated as cases rather than inferred from a render.
 *
 * The detection rule decides whether a folder row offers a "Chat with conductor"
 * menu item, so the cases that must NOT resolve to a conductor matter as much as
 * the ones that must: a false positive puts an item on a folder where it opens a
 * session that conducts nothing, and the reader has no way to tell that apart
 * from a bug.
 *
 * Every case here is ONE folder's rows. Folder membership is the caller's filter,
 * so the detector never reads `folder_id` and these literals do not carry it.
 */
import { describe, it, expect } from 'vitest'

import { detectFolderConductor, type FolderConductorRow } from './folderConductor'

/** A row, written as the fields the detector reads. */
const row = (key: string, parent?: string | null, created?: string): FolderConductorRow => ({
  key,
  parent: parent === undefined ? null : { slot: parent ?? undefined, key: parent },
  created,
})

describe('detectFolderConductor', () => {
  it('names the row that opened the others', () => {
    expect(detectFolderConductor([row('lead'), row('w1', 'lead'), row('w2', 'lead')])).toBe('lead')
  })

  it('picks the TOP of the folder lineage, not a worker that dispatched helpers', () => {
    // The conductor is the top of the folder's own lineage, so a worker with
    // helpers of its own never outranks the session above it.
    expect(detectFolderConductor([row('lead'), row('w1', 'lead'), row('w1a', 'w1')])).toBe('lead')
  })

  it('ignores a creator that is not filed in this folder', () => {
    // The rows handed in are one folder's. A row whose creator lives elsewhere
    // is a ROOT here, so it can still conduct this folder.
    const rows = [
      { key: 'lead', parent: { slot: 'outside', key: 'outside' } },
      row('w1', 'lead'),
    ]
    expect(detectFolderConductor(rows)).toBe('lead')
  })

  it('finds no conductor when nobody opened anybody', () => {
    // The ordinary folder: hand-made sessions sitting side by side. Its row must
    // render exactly as it does today, so the detector has to say null rather
    // than pick the first row.
    expect(detectFolderConductor([row('a'), row('b'), row('c')])).toBeNull()
  })

  it('finds no conductor in a folder holding one session', () => {
    expect(detectFolderConductor([row('solo')])).toBeNull()
  })

  it('finds no conductor in an empty folder', () => {
    expect(detectFolderConductor([])).toBeNull()
  })

  it('refuses a citation whose creator is not running', () => {
    // `parent.key` is null when the creator is not in the payload. The row
    // still cites it in `parent.slot`, and that citation must not establish a
    // conductor the user cannot open.
    const rows: FolderConductorRow[] = [
      { key: 'w1', parent: { slot: 'gone', key: null } },
      { key: 'w2', parent: { slot: 'gone', key: null } },
    ]
    expect(detectFolderConductor(rows)).toBeNull()
  })

  it('refuses a row that cites itself', () => {
    expect(detectFolderConductor([row('a', 'a'), row('b')])).toBeNull()
  })

  it('finds no conductor when the whole folder is one lineage cycle', () => {
    // Our backend already refuses a cycle, so this is a payload we do not
    // produce. A view must still paint: the lineage detaches each member of a
    // cycle into a root, and neither root has a child, so the answer is null.
    expect(detectFolderConductor([row('a', 'b'), row('b', 'a')])).toBeNull()
  })

  it('answers for a cycle that has a worker hanging off it', () => {
    // Same detached-cycle rule, now with a row below it. `a` and `b` are both
    // roots and `c` keeps its edge to `a`, which is exactly how the conductor
    // lane DRAWS these rows -- so the item opens the row the reader sees on top.
    expect(detectFolderConductor([row('a', 'b'), row('b', 'a'), row('c', 'a')])).toBe('a')
  })

  it('counts a whole chain below the conductor, visiting each row once', () => {
    // The conductor's run is its whole in-folder subtree, not just its direct
    // children, and the subtree size is what breaks a tie below.
    expect(detectFolderConductor([
      row('lead'), row('x', 'lead'), row('y', 'x'), row('z', 'y'),
    ])).toBe('lead')
  })

  it('breaks a tie on subtree size, then creation, then key', () => {
    const bySize = detectFolderConductor([
      row('a', null, '2026-01-01T00:00:00Z'), row('a1', 'a'),
      row('b', null, '2026-01-02T00:00:00Z'), row('b1', 'b'), row('b2', 'b'),
    ])
    expect(bySize).toBe('b')

    const byCreated = detectFolderConductor([
      row('b', null, '2026-01-02T00:00:00Z'), row('b1', 'b'),
      row('a', null, '2026-01-01T00:00:00Z'), row('a1', 'a'),
    ])
    expect(byCreated).toBe('a')

    const byKey = detectFolderConductor([row('b'), row('b1', 'b'), row('a'), row('a1', 'a')])
    expect(byKey).toBe('a')
  })

  it('sorts a row with no creation instant last, not first', () => {
    // An absent `created` is unknown. Reading it as the epoch would let a
    // payload gap outrank a real timestamp.
    expect(detectFolderConductor([
      row('nostamp'), row('n1', 'nostamp'),
      row('stamped', null, '2026-06-01T00:00:00Z'), row('s1', 'stamped'),
    ])).toBe('stamped')
  })

  it('does not depend on the order the rows are handed in', () => {
    const rows = [row('lead'), row('w1', 'lead'), row('w2', 'lead')]
    expect(detectFolderConductor([...rows].reverse())).toBe(detectFolderConductor(rows))
  })

  it('skips a root that opened nobody', () => {
    // A hand-made session sitting beside a real run is a root too. It has no
    // subtree, so it is never a candidate and never wins the folder.
    expect(detectFolderConductor([row('loose'), row('lead'), row('w1', 'lead')])).toBe('lead')
  })
})
