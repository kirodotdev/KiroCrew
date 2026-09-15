import { describe, it, expect } from 'vitest'
import { allocateSeq, nextSeq, nextSeqIn, splitDuplicateMarkers, formatToken, expandAll, type PasteBlock } from '../utils/pasteTokens'

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

  it('backs only the first occurrence and leaves a later duplicate marker literal', () => {
    const a = blk('a', 1, 3, 'C')
    const marker = formatToken(a)
    const text = `original ${marker} copy ${marker}`
    const blocks = [a]
    const out = splitDuplicateMarkers(text, blocks)
    expect(out.blocks).toHaveLength(blocks.length)
    expect(out.blocks[0]).toBe(a)
    expect(out.blocks[0].seq).toBe(a.seq)
    expect(out.text).toBe(text)
    expect(out.text.slice(out.text.lastIndexOf(marker))).toBe(marker)
    expect(expandAll(out.text, out.blocks)).toBe(`original C copy ${marker}`)
    expect(expandAll(out.text, out.blocks).split(a.content)).toHaveLength(2)
  })

  it('backs the first occurrence when a duplicate appears before the original and leaves the later marker literal', () => {
    const a = blk('a', 1, 3, 'C')
    const marker = formatToken(a)
    const text = `copy ${marker} original ${marker}`
    const blocks = [a]
    const out = splitDuplicateMarkers(text, blocks)
    expect(out.blocks).toHaveLength(blocks.length)
    expect(out.blocks).toEqual([a])
    expect(out.text).toBe(text)
    expect(expandAll(out.text, out.blocks)).toBe(`copy C original ${marker}`)
    expect(expandAll(out.text, out.blocks).split(a.content)).toHaveLength(2)
  })

  it('leaves a marker for a seq with no record inert', () => {
    const a = blk('a', 1, 1, 'A')
    const literal = '[ Paste #2 · 1 lines ]'
    const text = `${formatToken(a)} ${literal}`
    const blocks = [a]
    const out = splitDuplicateMarkers(text, blocks)
    expect(out.text).toBe(text)
    expect(out.blocks).toBe(blocks)
    expect(expandAll(out.text, out.blocks)).toBe(`A ${literal}`)
  })

  it('allocates past the highest seq of ANY block, including ones without a marker', () => {
    const a = blk('a', 1, 1, 'A')
    const twin = blk('b', 1, 1, 'B')
    const orphan = blk('o', 7, 1, 'O')
    const text = `${formatToken(a)} ${formatToken(twin)}`
    const out = splitDuplicateMarkers(text, [a, twin, orphan])
    expect(out.blocks.map(b => b.seq)).toEqual([1, 8, 7])
  })

  it('never mints a seq that a marker ALREADY IN THE TEXT carries, even one no record claims (round-10 finding)', () => {
    // Marker text with no record behind it is inert: `expandAll` leaves it as
    // the literal characters the user typed (or that a small paste carried in).
    // The fresh seq for a twin must not be that number, or the literal marker
    // suddenly names the twin's block and the SEND replaces the user's own text
    // with an unrelated paste's content.
    const a = blk('a', 1, 5, 'A1\nA2\nA3\nA4\nA5')
    const twin = blk('b', 1, 1, 'B')
    const literal = '[ Paste #2 · 9 lines ]'
    const text = `${formatToken(a)} x ${formatToken(twin)} ${literal}`
    expect(expandAll(text, [a])).toContain(literal) // inert before the split…
    const out = splitDuplicateMarkers(text, [a, twin])
    expect(out.blocks[1].seq).not.toBe(2)
    expect(out.blocks.map(b => b.seq)).toEqual([1, 3])
    // …and still inert after it: the paired record lands where its marker is,
    // and the literal stays the literal.
    expect(expandAll(out.text, out.blocks)).toBe(`${a.content} x ${twin.content} ${literal}`)
  })

  it('fresh seqs stay distinct when a literal marker sits at the safe-integer limit (round-11 finding)', () => {
    // `++max` past 2^53 - 1 stops moving (2^53 + 1 === 2^53 in a double), so two
    // twins would share a seq and `findTokenRanges` would send one paste's
    // content into both markers. Above the limit the allocator falls back to the
    // first free small integer instead.
    const a = blk('a', 1, 1, 'A')
    const b = blk('b', 1, 1, 'B')
    const c = blk('c', 1, 1, 'C')
    const literal = '[ Paste #9007199254740991 · 1 lines ]'
    const text = `${formatToken(a)} ${formatToken(b)} ${formatToken(c)} ${literal}`
    const out = splitDuplicateMarkers(text, [a, b, c])
    const seqs = out.blocks.map(block => block.seq)
    expect(new Set(seqs).size).toBe(3)
    for (const seq of seqs) expect(Number.isSafeInteger(seq)).toBe(true)
    expect(seqs).toEqual([1, 2, 3])
    expect(expandAll(out.text, out.blocks)).toBe(`A B C ${literal}`)
    // A digit run too long for a double is Infinity: still inert, still no collision.
    const huge = `[ Paste #${'9'.repeat(40)} · 1 lines ]`
    const out2 = splitDuplicateMarkers(`${formatToken(a)} ${formatToken(b)} ${huge}`, [a, b])
    expect(out2.blocks.map(block => block.seq)).toEqual([1, 2])
    expect(expandAll(out2.text, out2.blocks)).toBe(`A B ${huge}`)
  })

  it('leaves every occurrence beyond the available records as literal text', () => {
    const a = blk('a', 1, 1, 'A')
    const marker = formatToken(a)
    const text = `${marker}-${marker}-${marker}`
    const out = splitDuplicateMarkers(text, [a])
    expect(out.blocks).toEqual([a])
    expect(out.text).toBe(text)
    expect(expandAll(out.text, out.blocks)).toBe(`A-${marker}-${marker}`)
  })

  it('keeps independently backed same-seq records editable after canonicalisation', () => {
    const a = blk('a', 1, 2, 'orig\ninal')
    const twin = blk('b', 1, 2, 'second\ncopy')
    const { text, blocks } = splitDuplicateMarkers(`x${formatToken(a)}y${formatToken(twin)}z`, [a, twin])
    const edited = { ...blocks[1], lines: 1, content: 'edited' }
    const editedText = text.replace(formatToken(blocks[1]), formatToken(edited))
    expect(expandAll(editedText, [blocks[0], edited])).toBe('xorig\ninalyeditedz')
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
    })

    it('re-sequences a same-seq record no occurrence claims instead of dropping it', () => {
      const one = `x${formatToken(orig)}z`
      const out = splitDuplicateMarkers(one, [orig, edited])
      expect(out.text).toBe(one)
      expect(out.blocks).toEqual([orig, { ...edited, seq: 2 }])
      expect(expandAll(out.text, out.blocks)).toBe('xorig\ninalz')
    })

    it('leaves an occurrence with no record left literal instead of copying the last record', () => {
      const marker = formatToken(edited)
      const three = `${formatToken(orig)}-${marker}-${marker}`
      const out = splitDuplicateMarkers(three, [orig, edited])
      expect(out.blocks.map(b => b.seq)).toEqual([1, 2])
      expect(out.blocks[1]).toEqual({ ...edited, seq: 2 })
      expect(out.text).toBe(`${formatToken(orig)}-${formatToken(out.blocks[1])}-${marker}`)
      expect(expandAll(out.text, out.blocks)).toBe(`orig\ninal-edited-${marker}`)
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

describe('allocateSeq', () => {
  it('steps past max while that is an exact integer and not already used', () => {
    expect(allocateSeq(0, new Set())).toBe(1)
    expect(allocateSeq(7, new Set([1, 7]))).toBe(8)
  })

  it('falls back to the first free positive integer past the safe-integer limit, or when max is Infinity', () => {
    const used = new Set([1, 2, Number.MAX_SAFE_INTEGER])
    expect(allocateSeq(Number.MAX_SAFE_INTEGER, used)).toBe(3)
    expect(allocateSeq(Infinity, new Set([Infinity, 1]))).toBe(2)
    // Two consecutive allocations from the same limit never coincide once the
    // caller records each result in `used`.
    const a = allocateSeq(Number.MAX_SAFE_INTEGER, used); used.add(a)
    const b = allocateSeq(Number.MAX_SAFE_INTEGER, used); used.add(b)
    expect(a).not.toBe(b)
  })

  it('nextSeq uses it: a stored block with an unsafe seq does not make the next paste collide', () => {
    const blocks: PasteBlock[] = [
      { id: 'x', seq: 1, lines: 1, content: 'x' },
      { id: 'y', seq: Number.MAX_SAFE_INTEGER, lines: 1, content: 'y' },
    ]
    const s1 = nextSeq(blocks)
    expect(Number.isSafeInteger(s1)).toBe(true)
    expect(blocks.some(b => b.seq === s1)).toBe(false)
    expect(nextSeq([{ id: 'x', seq: 1, lines: 1, content: 'x' }])).toBe(2)
  })
})

describe('nextSeqIn', () => {
  it('reserves every marker already in the text, claimed or not, alongside the blocks', () => {
    expect(nextSeqIn('plain text', [])).toBe(1)
    expect(nextSeqIn('[ Paste #1 · 1 lines ] typed by hand', [])).toBe(2)
    const a: PasteBlock = { id: 'a', seq: 1, lines: 1, content: 'A' }
    expect(nextSeqIn(`${formatToken(a)} and a literal [ Paste #3 · 2 lines ]`, [a])).toBe(4)
    // Unsafe or Infinity literals fall back to the first free small integer.
    expect(nextSeqIn('[ Paste #9007199254740991 · 1 lines ]', [a])).toBe(2)
  })
})
