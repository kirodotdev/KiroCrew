/**
 * folderTree.orderFoldersWithPaths is the shared tree-ordering used by the
 * folder pickers (move-to-folder submenu, new-chat-in-folder). The
 * ordering/path/depth logic is unit-testable on its own — independent of the
 * Radix submenu it feeds.
 */
import { describe, it, expect } from 'vitest'
import { orderFoldersWithPaths, bySidebarOrder, FOLDER_PATH_SEP } from '../utils/folderTree'
import type { ChatFolder } from '../types'

const nested: ChatFolder[] = [
  { id: 'p1', name: 'Work', order: 0 },
  { id: 'p2', name: 'Personal', order: 1 },
  { id: 'c1', name: 'Drafts', order: 0, parent_id: 'p1' },
  { id: 'c2', name: 'Drafts', order: 0, parent_id: 'p2' },
]

describe('orderFoldersWithPaths', () => {
  it('uses U+203A as the breadcrumb separator (matches server folder_breadcrumb)', () => {
    expect(FOLDER_PATH_SEP).toBe(' › ')
  })

  it('orders children directly under their parent (pre-order tree)', () => {
    const paths = orderFoldersWithPaths(nested).map(o => o.path)
    expect(paths).toEqual(['Work', 'Work › Drafts', 'Personal', 'Personal › Drafts'])
  })

  it('computes depth (0 for roots, +1 per level) and ancestor names', () => {
    const byId = new Map(orderFoldersWithPaths(nested).map(o => [o.folder.id, o]))
    expect(byId.get('p1')!.depth).toBe(0)
    expect(byId.get('p1')!.ancestors).toEqual([])
    expect(byId.get('c1')!.depth).toBe(1)
    expect(byId.get('c1')!.ancestors).toEqual(['Work'])
  })

  it('keeps the full ancestry path so same-named subfolders stay unambiguous', () => {
    const byId = new Map(orderFoldersWithPaths(nested).map(o => [o.folder.id, o]))
    // Both subfolders are named "Drafts"; their paths disambiguate them.
    expect(byId.get('c1')!.path).toBe('Work › Drafts')
    expect(byId.get('c2')!.path).toBe('Personal › Drafts')
    // Root folders keep their bare name as the path.
    expect(byId.get('p1')!.path).toBe('Work')
  })

  it('sorts siblings by order then name', () => {
    const unordered: ChatFolder[] = [
      { id: 'b', name: 'Bravo', order: 1 },
      { id: 'a', name: 'Alpha', order: 0 },
      { id: 'c', name: 'Charlie', order: 1 }, // same order as Bravo → tiebreak by name
    ]
    expect(orderFoldersWithPaths(unordered).map(o => o.folder.name)).toEqual(['Alpha', 'Bravo', 'Charlie'])
  })

  it('treats an orphan parent_id (missing parent) as a root', () => {
    const orphan: ChatFolder[] = [{ id: 'x', name: 'Orphan', order: 0, parent_id: 'ghost' }]
    const out = orderFoldersWithPaths(orphan)
    expect(out).toHaveLength(1)
    expect(out[0].depth).toBe(0)
    expect(out[0].path).toBe('Orphan')
  })

  it('survives a parent↔child cycle without infinite recursion', () => {
    const cyclic: ChatFolder[] = [
      { id: 'a', name: 'A', order: 0, parent_id: 'b' },
      { id: 'b', name: 'B', order: 0, parent_id: 'a' },
    ]
    const out = orderFoldersWithPaths(cyclic)
    // Both surface (cycle guard + safety-net), none duplicated.
    expect(new Set(out.map(o => o.folder.id))).toEqual(new Set(['a', 'b']))
  })
})

// ── collectFolderSubtreeIds: the acyclicity guard for folder re-parenting ──
// Both the "Move folder to" submenu (excludes self+descendants from targets)
// and drag re-parenting (excludes them from drop collision candidates) rely on
// this returning exactly the folder's own subtree.
import { collectFolderSubtreeIds } from '../utils/folderTree'
import goldenFixture from '../../../test/fixtures/chat_folder_sibling_order.json'

describe('collectFolderSubtreeIds', () => {
  const tree: ChatFolder[] = [
    { id: 'a', name: 'A', order: 0 },
    { id: 'b', name: 'B', order: 0, parent_id: 'a' },
    { id: 'c', name: 'C', order: 0, parent_id: 'b' },
    { id: 'x', name: 'X', order: 1 },
    { id: 'y', name: 'Y', order: 0, parent_id: 'x' },
  ]

  it('returns the folder itself plus all descendants, transitively', () => {
    expect([...collectFolderSubtreeIds(tree, 'a')].sort()).toEqual(['a', 'b', 'c'])
  })

  it('returns only the folder itself for a leaf', () => {
    expect([...collectFolderSubtreeIds(tree, 'c')]).toEqual(['c'])
  })

  it('does not leak unrelated branches', () => {
    const ids = collectFolderSubtreeIds(tree, 'a')
    expect(ids.has('x')).toBe(false)
    expect(ids.has('y')).toBe(false)
  })

  it('terminates on a corrupt parent_id cycle', () => {
    const cyclic: ChatFolder[] = [
      { id: 'p', name: 'P', order: 0, parent_id: 'q' },
      { id: 'q', name: 'Q', order: 0, parent_id: 'p' },
    ]
    expect([...collectFolderSubtreeIds(cyclic, 'p')].sort()).toEqual(['p', 'q'])
  })
})

describe('bySidebarOrder', () => {  /**
   * The single definition every surface that draws siblings must use — the tree
   * walk here, and both of ChatSidebar's nested renders. A render path with its
   * own comparator (or none) shows a sequence the person never chose, which is
   * what the nested subfolder render did before `chat_folder_move` could set a
   * position at all.
   */
  const f = (id: string, name: string, order: number) => ({ id, name, order })

  it('sorts by stored order', () => {
    const rows = [f('c', 'C', 2), f('a', 'A', 0), f('b', 'B', 1)]
    expect([...rows].sort(bySidebarOrder).map(r => r.id)).toEqual(['a', 'b', 'c'])
  })

  it('breaks a tie on name, since the store permits duplicate order values', () => {
    const rows = [f('z', 'Zulu', 5), f('a', 'Alpha', 5)]
    expect([...rows].sort(bySidebarOrder).map(r => r.id)).toEqual(['a', 'z'])
  })

  it('is the comparator the tree walk itself applies', () => {
    const rows = [f('late', 'Aaa', 9), f('early', 'Zzz', 1)]
    const walked = orderFoldersWithPaths(rows).map(o => o.folder.id)
    expect(walked).toEqual([...rows].sort(bySidebarOrder).map(r => r.id))
  })

  it('treats a row with no order key as 0, the way the Python reader does', () => {
    // A folder written before the field existed carries no `order`, and the
    // folders endpoint returns rows verbatim. Without `?? 0` the subtraction is
    // NaN -- which is falsy, so the whole comparison would fall through to the
    // name tie-break and order the numbered siblings by name instead.
    const legacy = { id: 'legacy', name: 'Zulu' } as unknown as Parameters<typeof bySidebarOrder>[0]
    const rows = [f('five', 'Alpha', 5), legacy, f('one', 'Bravo', 1)]
    expect([...rows].sort(bySidebarOrder).map(r => r.id)).toEqual(['legacy', 'one', 'five'])
  })

  it('does not let a missing order silently reorder numbered siblings', () => {
    const legacy = { id: 'legacy', name: 'Mike' } as unknown as Parameters<typeof bySidebarOrder>[0]
    // Name-only ordering would put Alpha(9) before Mike before Zulu(1); the
    // numbers must win, with the unnumbered row sorting as 0.
    const rows = [f('nine', 'Alpha', 9), legacy, f('one', 'Zulu', 1)]
    expect([...rows].sort(bySidebarOrder).map(r => r.id)).toEqual(['legacy', 'one', 'nine'])
  })

  it('survives a folder row with no name, instead of taking the sidebar down', () => {
    // A folder row is persisted JSON: `name` can be absent or non-string, and an
    // exception thrown inside a comparator kills the whole render, not one row.
    const nameless = { id: 'nameless', order: 0 } as unknown as Parameters<typeof bySidebarOrder>[0]
    const numeric = { id: 'numeric', name: 7, order: 0 } as unknown as Parameters<
      typeof bySidebarOrder
    >[0]
    const rows = [f('named', 'Alpha', 0), nameless, numeric]
    expect(() => [...rows].sort(bySidebarOrder)).not.toThrow()
    expect([...rows].sort(bySidebarOrder).map(r => r.id)).toHaveLength(3)
  })

  it('compares names the way the Python sort key does, so equal orders agree', () => {
    // Neither side folds case, so this pair is decided by code unit alone
    // (s=0x73 before U+00DF) — identical here and in `_chat_folder_name_key`,
    // and dependent on no Unicode table on either side.
    const rows = [f('sharp', 'straße', 0), f('ss', 'strasse', 0)]
    expect([...rows].sort(bySidebarOrder).map(r => r.id)).toEqual(['ss', 'sharp'])
  })
})

describe('the shared golden fixture (test/fixtures/chat_folder_sibling_order.json)', () => {
  // ONE fixture, both suites. The pytest side asserts the same rows through
  // `_chat_folder_siblings`, so a coercion that agrees here and diverges there
  // (or the reverse) fails in one of the two runs instead of shipping as an
  // anchor that names a different gap than the tool reported.
  for (const c of goldenFixture.cases) {
    it(c.name, () => {
      const rows = c.rows.map((r) => {
        const units = (r as { name_code_units?: number[] }).name_code_units
        // An unpaired surrogate cannot travel as a JSON string (strict parsers
        // reject the escape), so that one name arrives as UTF-16 code units.
        const name = units ? String.fromCharCode(...units) : (r as { name?: unknown }).name
        return { ...r, name, parent_id: '' }
      }) as unknown as ChatFolder[]
      const got = [...rows].sort(bySidebarOrder).map((r) => r.id)
      expect(got).toEqual(c.expected)
    })
  }
})

describe('bySidebarOrder is never NaN', () => {
  // The fixture cannot cover this: a comparator returning NaN leaves the order
  // UNSPECIFIED rather than deterministically wrong, so a golden sequence can
  // pass by luck. Assert the comparator's own contract instead.
  const junk = ['abc', null, undefined, {}, [], '', NaN, Infinity, -Infinity, true, '3']
  for (const v of junk) {
    it(`order=${JSON.stringify(v) ?? String(v)} compares as a number`, () => {
      const a = { id: 'a', name: 'a', parent_id: '', order: v } as unknown as ChatFolder
      const b = { id: 'b', name: 'b', parent_id: '', order: 1 } as unknown as ChatFolder
      expect(Number.isNaN(bySidebarOrder(a, b))).toBe(false)
      expect(Number.isNaN(bySidebarOrder(b, a))).toBe(false)
    })
  }
})

describe('the tie-break folds A-Z and nothing else', () => {
  // Mirror of the pytest side. Anything outside A-Z has a case mapping that can
  // differ between this runtime and the interpreter's, so it must pass through.
  const f = (id: string, name: string, order: number) =>
    ({ id, name, order, parent_id: '' }) as unknown as ChatFolder
  it('folds an ASCII capital so ordering stays alphabetical', () => {
    const rows = [f('apr', 'apricot', 0), f('app', 'Apple', 0), f('ban', 'banana', 0)]
    expect([...rows].sort(bySidebarOrder).map(r => r.id)).toEqual(['app', 'apr', 'ban'])
  })

  it('leaves a non-ASCII capital alone', () => {
    // U+0130 is past every ASCII letter, so it sorts after `zebra` on both sides.
    const rows = [f('dotted', '\u0130stanbul', 0), f('z', 'zebra', 0)]
    expect([...rows].sort(bySidebarOrder).map(r => r.id)).toEqual(['z', 'dotted'])
  })

  it('leaves the sharp s and an accented capital alone', () => {
    const rows = [f('sharp', 'stra\u00dfe', 0), f('ss', 'strasse', 0)]
    expect([...rows].sort(bySidebarOrder).map(r => r.id)).toEqual(['ss', 'sharp'])
  })
})
