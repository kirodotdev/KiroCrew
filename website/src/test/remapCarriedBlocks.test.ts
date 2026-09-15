import { describe, it, expect } from 'vitest'
import { remapCarriedBlocks, formatToken, expandAll, type PasteBlock } from '../utils/pasteTokens'

const blk = (id: string, seq: number, lines: number, content: string): PasteBlock =>
  ({ id, seq, lines, content })

describe('remapCarriedBlocks', () => {
  it('leaves text and blocks untouched when no seq collides', () => {
    const carried = [blk('a', 3, 5, 'AAA'), blk('b', 4, 5, 'BBB')]
    const text = `x ${formatToken(carried[0])} y ${formatToken(carried[1])}`
    const out = remapCarriedBlocks(text, carried, new Set([1, 2]))
    expect(out.text).toBe(text)
    expect(out.blocks.map(b => b.seq)).toEqual([3, 4])
  })

  it('does not fuse markers when carried blocks share a line count (cascade guard)', () => {
    // The bug: rewriting per block with split/join re-matches the marker a previous
    // iteration emitted, because the needle only carries seq + line count. Two
    // 5-line blocks then collapse onto one seq, so on expansion one blob's content
    // is duplicated and the other is silently dropped.
    const b1 = blk('b1', 1, 5, 'FIRST')
    const b2 = blk('b2', 2, 5, 'SECOND')
    const text = `${formatToken(b1)} and ${formatToken(b2)}`
    const { text: out, blocks } = remapCarriedBlocks(text, [b1, b2], new Set([1]))

    // Every carried block keeps a distinct seq...
    expect(new Set(blocks.map(b => b.seq)).size).toBe(2)
    // ...each has exactly one marker in the text...
    for (const b of blocks) {
      expect(out.split(formatToken(b)).length - 1).toBe(1)
    }
    // ...and expansion restores BOTH original contents, in order.
    const expanded = expandAll(out, blocks)
    expect(expanded).toContain('FIRST')
    expect(expanded).toContain('SECOND')
    expect(expanded.indexOf('FIRST')).toBeLessThan(expanded.indexOf('SECOND'))
  })

  it('cascades safely across three equal-length blocks', () => {
    const bs = [blk('b1', 1, 5, 'ONE'), blk('b2', 2, 5, 'TWO'), blk('b3', 3, 5, 'THREE')]
    const text = bs.map(formatToken).join(' | ')
    const { text: out, blocks } = remapCarriedBlocks(text, bs, new Set([1, 2, 3]))
    expect(new Set(blocks.map(b => b.seq)).size).toBe(3)
    const expanded = expandAll(out, blocks)
    for (const want of ['ONE', 'TWO', 'THREE']) {
      expect(expanded.split(want).length - 1).toBe(1)
    }
  })

  it('records assigned seqs in the used set so later callers cannot reuse them', () => {
    const used = new Set([1])
    const { blocks } = remapCarriedBlocks(formatToken(blk('a', 1, 2, 'X')), [blk('a', 1, 2, 'X')], used)
    expect(used.has(blocks[0].seq)).toBe(true)
    expect(blocks[0].seq).not.toBe(1)
  })

  it('never reuses a seq a kept block already holds (stale free-cursor guard)', () => {
    // Reachable via a second failed recovery: the live list has a gap (a paste chip
    // was deleted, seqs are not renumbered) and the carried list is non-ascending
    // because an earlier recovery re-sequenced it. Seeding `free` once from
    // max(used) then only incrementing hands a later block a seq that an earlier
    // KEPT block just claimed.
    const carried = [blk('c2', 2, 5, 'TWO'), blk('c1', 1, 5, 'ONE'), blk('c3', 3, 5, 'THREE')]
    const text = carried.map(formatToken).join(' | ')
    const { text: out, blocks } = remapCarriedBlocks(text, carried, new Set([1]))

    expect(new Set(blocks.map(b => b.seq)).size).toBe(3)
    const expanded = expandAll(out, blocks)
    for (const want of ['ONE', 'TWO', 'THREE']) {
      expect(expanded.split(want).length - 1).toBe(1)
    }
  })
})
