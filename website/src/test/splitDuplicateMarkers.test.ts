import { describe, it, expect } from 'vitest'
import { splitDuplicateMarkers, formatToken, expandAll, type PasteBlock } from '../utils/pasteTokens'

const blk = (id: string, seq: number, lines: number, content: string): PasteBlock =>
  ({ id, seq, lines, content })

describe('splitDuplicateMarkers', () => {
  it('returns the same references when every marker already names its own block', () => {
    const blocks = [blk('a', 1, 2, 'A\nA'), blk('b', 2, 1, 'B')]
    const text = `x ${formatToken(blocks[0])} y ${formatToken(blocks[1])} z`
    const out = splitDuplicateMarkers(text, blocks)
    expect(out.text).toBe(text)
    expect(out.blocks).toBe(blocks)
  })

  it('returns the same references for a value without markers, or with blocks that have none', () => {
    const blocks = [blk('a', 1, 2, 'A\nA')]
    expect(splitDuplicateMarkers('plain text', blocks).blocks).toBe(blocks)
    expect(splitDuplicateMarkers('', blocks).text).toBe('')
    const text = `${formatToken(blocks[0])}`
    expect(splitDuplicateMarkers(text, []).text).toBe(text)
  })

  it('gives a second occurrence of a marker its own block under a fresh seq + id, first occurrence untouched', () => {
    const a = blk('a', 1, 2, 'A\nA')
    const text = `x${formatToken(a)}y${formatToken(a)}z`
    const out = splitDuplicateMarkers(text, [a])
    expect(out.blocks).toHaveLength(2)
    expect(out.blocks[0]).toBe(a)
    const copy = out.blocks[1]
    expect(copy).toEqual(expect.objectContaining({ seq: 2, lines: 2, content: 'A\nA' }))
    expect(copy.id).not.toBe(a.id)
    expect(out.text).toBe(`x${formatToken(a)}y${formatToken(copy)}z`)
  })

  it('allocates past the highest seq of ANY block, including ones without a marker', () => {
    const a = blk('a', 1, 1, 'A')
    const orphan = blk('o', 7, 1, 'O')
    const text = `${formatToken(a)} ${formatToken(a)}`
    const out = splitDuplicateMarkers(text, [a, orphan])
    expect(out.blocks.map(b => b.seq)).toEqual([1, 7, 8])
  })

  it('three occurrences become three blocks, and each marker expands to its own block', () => {
    const a = blk('a', 1, 1, 'A')
    const text = `${formatToken(a)}-${formatToken(a)}-${formatToken(a)}`
    const out = splitDuplicateMarkers(text, [a])
    expect(out.blocks.map(b => b.seq)).toEqual([1, 2, 3])
    expect(new Set(out.blocks.map(b => b.id)).size).toBe(3)
    expect(expandAll(out.text, out.blocks)).toBe('A-A-A')
  })

  it('after the split, editing one copy no longer rewrites the other on expansion (the round-5 finding)', () => {
    const a = blk('a', 1, 2, 'orig\ninal')
    const { text, blocks } = splitDuplicateMarkers(`x${formatToken(a)}y${formatToken(a)}z`, [a])
    // The composer edits the second pill: its block changes lines + content and
    // its marker follows; the first pill is untouched.
    const edited = { ...blocks[1], lines: 1, content: 'edited' }
    const editedText = text.replace(formatToken(blocks[1]), formatToken(edited))
    expect(expandAll(editedText, [blocks[0], edited])).toBe('xorig\ninalyeditedz')
    // Without the split, both markers carried seq 1 and the LAST block won: the
    // original paste was replaced by the edit in what got sent.
    expect(expandAll(`x${formatToken(a)}y${formatToken({ ...a, lines: 1 })}z`, [a, { ...a, lines: 1, content: 'edited' }]))
      .toBe('xeditedyeditedz')
  })

  describe('same-seq block RECORDS (a draft persisted while two twins had already diverged)', () => {
    const orig = blk('o', 1, 2, 'orig\ninal')
    const edited = blk('e', 1, 1, 'edited')
    const text = `x${formatToken(orig)}y${formatToken(edited)}z` // two `#1` markers, 2 lines then 1 line

    it('pairs records with occurrences in order: the first paste keeps its content, the later record gets its own seq and keeps its id', () => {
      const out = splitDuplicateMarkers(text, [orig, edited])
      expect(out.blocks).toEqual([orig, { ...edited, seq: 2 }])
      expect(out.text).toBe(`x${formatToken(orig)}y${formatToken({ ...edited, seq: 2 })}z`)
      // The whole point: both pastes survive in what gets sent.
      expect(expandAll(out.text, out.blocks)).toBe('xorig\ninalyeditedz')
      // Before: `findTokenRanges` resolved both markers through the LAST record.
      expect(expandAll(text, [orig, edited])).toBe('xeditedyeditedz')
    })

    it('re-sequences a same-seq record no occurrence claims instead of dropping it', () => {
      const one = `x${formatToken(orig)}z`
      const out = splitDuplicateMarkers(one, [orig, edited])
      expect(out.text).toBe(one)
      expect(out.blocks).toEqual([orig, { ...edited, seq: 2 }])
      expect(expandAll(out.text, out.blocks)).toBe('xorig\ninalz')
    })

    it('an occurrence with no record left gets a copy of the last record under a fresh seq and id', () => {
      const three = `${formatToken(orig)}-${formatToken(edited)}-${formatToken(edited)}`
      const out = splitDuplicateMarkers(three, [orig, edited])
      expect(out.blocks.map(b => b.seq)).toEqual([1, 2, 3])
      expect(out.blocks[1]).toEqual({ ...edited, seq: 2 })
      expect(out.blocks[2]).toEqual(expect.objectContaining({ seq: 3, lines: 1, content: 'edited' }))
      expect(out.blocks[2].id).not.toBe(edited.id)
      expect(expandAll(out.text, out.blocks)).toBe('orig\ninal-edited-edited')
    })

    it('records with distinct seqs and no repeated marker are untouched (identity)', () => {
      const b = blk('b', 2, 1, 'B')
      const clean = `${formatToken(orig)} ${formatToken(b)}`
      const blocks = [orig, b]
      const out = splitDuplicateMarkers(clean, blocks)
      expect(out.text).toBe(clean)
      expect(out.blocks).toBe(blocks)
    })

    it('twins that share the id as well as the seq (minted from one block) come out with distinct ids', () => {
      // The textarea composer removes a pill by id, so two records under one id
      // would both vanish on one ✕.
      const twinA = blk('same', 1, 2, 'orig\ninal')
      const twinB = blk('same', 1, 1, 'edited')
      const out = splitDuplicateMarkers(`${formatToken(twinA)}|${formatToken(twinB)}`, [twinA, twinB])
      expect(out.blocks[0]).toBe(twinA)
      expect(out.blocks[1]).toEqual(expect.objectContaining({ seq: 2, lines: 1, content: 'edited' }))
      expect(out.blocks[1].id).not.toBe('same')
      expect(new Set(out.blocks.map(b => b.id)).size).toBe(2)
      expect(expandAll(out.text, out.blocks)).toBe('orig\ninal|edited')
    })
  })

  describe('duplicate ids with DISTINCT seqs (a draft re-sequenced before ids were made unique)', () => {
    it('is a trigger on its own: seqs and markers stay, the later holder gets a fresh id', () => {
      const first = blk('same', 1, 2, 'orig\ninal')
      const second = blk('same', 2, 1, 'edited')
      const text = `x${formatToken(first)}y${formatToken(second)}z`
      const out = splitDuplicateMarkers(text, [first, second])
      expect(out.text).toBe(text)
      expect(out.blocks[0]).toBe(first)
      expect(out.blocks[1]).toEqual(expect.objectContaining({ seq: 2, lines: 1, content: 'edited' }))
      expect(out.blocks[1].id).not.toBe('same')
      expect(expandAll(out.text, out.blocks)).toBe('xorig\ninalyeditedz')
    })

    it('a kept record listed AFTER a re-sequenced record cannot keep an id that record already claimed', () => {
      // [a#1, a'#1 (twin, id 'dup'), c#3 (id 'dup')]: the twin is re-sequenced to
      // seq 4 and keeps 'dup' as the first holder in list order; the kept record
      // c that also carries 'dup' must be freshened, or one ✕ still removes two.
      const a = blk('a', 1, 1, 'A')
      const twin = blk('dup', 1, 1, 'A2')
      const c = blk('dup', 3, 1, 'C')
      const text = `${formatToken(a)} ${formatToken(twin)} ${formatToken(c)}`
      const out = splitDuplicateMarkers(text, [a, twin, c])
      expect(out.blocks.map(b => b.seq)).toEqual([1, 4, 3])
      expect(new Set(out.blocks.map(b => b.id)).size).toBe(3)
      expect(expandAll(out.text, out.blocks)).toBe('A A2 C')
    })
  })
})
