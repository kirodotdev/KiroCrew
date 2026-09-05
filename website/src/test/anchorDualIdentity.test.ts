import { describe, it, expect } from 'vitest'

import { anchorMatchesRow, anchorSettleConverged, resolveAnchorRow } from '../hooks/virtualizer/FollowController'

/** A row is named by one of its member messages, and a turn's membership changes
 *  at BOTH ends:
 *
 *   - streaming appends rename its TAIL;
 *   - an older page landing regroups messages into its HEAD, renaming its LEAD.
 *
 *  Either identity alone is therefore reliable only against the growth direction
 *  it was chosen for. A switch back into a live session does both at once -- the
 *  switch always prepends a page, and the turn is still producing output -- which
 *  is why the saved position resolved to nothing and the reader landed at the
 *  bottom instead, every time, for as long as the turn was running.
 *
 *  So both are persisted and either may match. What this file pins is that the
 *  two cover opposite failures and that a tail match is never outranked by an
 *  alt match on a different row. */

const rows = (tails: string[], leads: string[]) => ({
  count: tails.length,
  tailIdAt: (i: number) => tails[i] ?? null,
  altIdAt: (i: number) => leads[i] ?? null,
})

describe('resolveAnchorRow', () => {
  it('matches on the tail id in the ordinary case', () => {
    const r = resolveAnchorRow({ anchor: { key: 'a-t2', alt: 'l-h2' }, ...rows(['a-t1', 'a-t2'], ['l-h1', 'l-h2']) })
    expect(r).toBe(1)
  })

  it('falls back to the lead when a STREAMING append renamed the tail', () => {
    // The row is the same row; it simply gained a message at the end, so the
    // tail id the reader left behind names nothing.
    const r = resolveAnchorRow({ anchor: { key: 'a-t2', alt: 'l-h2' }, ...rows(['a-t1', 'a-t9'], ['l-h1', 'l-h2']) })
    expect(r).toBe(1)
  })

  it('still matches on the tail when a page landing renamed the lead', () => {
    // The complementary case, and the reason the tail is not simply replaced by
    // the lead: older messages were regrouped into this turn's head.
    const r = resolveAnchorRow({ anchor: { key: 'a-t2', alt: 'l-h2' }, ...rows(['a-t1', 'a-t2'], ['l-h1', 'l-h9']) })
    expect(r).toBe(1)
  })

  it('misses only when BOTH ends were renamed', () => {
    const r = resolveAnchorRow({ anchor: { key: 'a-t2', alt: 'l-h2' }, ...rows(['a-t1', 'a-t9'], ['l-h1', 'l-h9']) })
    expect(r).toBe(-1)
  })

  it('prefers a TAIL match over an alt match on a different row', () => {
    // Interleaving the comparisons per row would return row 0 here. The tail is
    // the stronger signal, so it wins as a whole pass.
    const r = resolveAnchorRow({ anchor: { key: 'a-t2', alt: 'l-h1' }, ...rows(['a-t1', 'a-t2'], ['l-h1', 'l-h2']) })
    expect(r).toBe(1)
  })

  it('resolves an anchor persisted before alt existed, by tail alone', () => {
    const r = resolveAnchorRow({ anchor: { key: 'a-t2' }, ...rows(['a-t1', 'a-t2'], ['l-h1', 'l-h2']) })
    expect(r).toBe(1)
  })

  it('does not invent a row for a legacy anchor whose tail is gone', () => {
    const r = resolveAnchorRow({ anchor: { key: 'a-t2' }, ...rows(['a-t1', 'a-t9'], ['l-h1', 'l-h2']) })
    expect(r).toBe(-1)
  })

  it('answers -1 on an empty transcript rather than 0', () => {
    expect(resolveAnchorRow({ anchor: { key: 'a-t1', alt: 'l-h1' }, ...rows([], []) })).toBe(-1)
  })

  // The row-level form of the same vocabulary. Anything that re-asks "is this the
  // anchored row" AFTER resolution has to accept both ends, or a row found through
  // `alt` is disowned one step later -- which is exactly when `alt` was used, since
  // it only matches when the tail does not.
  describe('anchorMatchesRow', () => {
    it('accepts a row the resolver found through alt', () => {
      expect(anchorMatchesRow({
        anchor: { key: 'tail-old', alt: 'l-lead' },
        tailId: 'tail-new',
        altId: 'l-lead',
      })).toBe(true)
    })

    it('accepts a row matching the tail', () => {
      expect(anchorMatchesRow({ anchor: { key: 't1', alt: 'l-x' }, tailId: 't1', altId: 'l-y' })).toBe(true)
    })

    it('rejects a row where neither end matches', () => {
      expect(anchorMatchesRow({ anchor: { key: 't1', alt: 'l-x' }, tailId: 't2', altId: 'l-y' })).toBe(false)
    })

    it('rejects on a legacy anchor with no alt when the tail is gone', () => {
      expect(anchorMatchesRow({ anchor: { key: 't1' }, tailId: 't2', altId: 'l-y' })).toBe(false)
    })

    it('rejects an unmounted row rather than matching a null id', () => {
      expect(anchorMatchesRow({ anchor: { key: 't1', alt: 'l-x' }, tailId: null, altId: null })).toBe(false)
    })
  })
})

/** When has a restore finished landing?
 *
 *  Two conditions, and the second decides whether this works during a live turn.
 *  The row must sit where the anchor says, AND the CAUSE of the corrections must
 *  have stopped -- otherwise "in tolerance right now" declares victory
 *  mid-measurement (observed: ok at frame 1 with d=0.5, then a further +49px at
 *  frame 3, i.e. a visible hop after the cover lifted).
 *
 *  The cause is height arriving ABOVE the anchor, which is not the same thing as
 *  the transcript growing. Testing total `scrollHeight` conflated them, and during
 *  streaming the difference is total: appends land BELOW the anchor and never move
 *  it, yet they change the total every frame -- so convergence was unreachable and
 *  every restore into a streaming session burned the whole 600ms budget with the
 *  skeleton up, however early it had really landed. */
describe('anchorSettleConverged', () => {
  const TOL = 1.5

  it('converges when the row is in place and nothing above it moved', () => {
    expect(anchorSettleConverged({ delta: 0.3, aboveDelta: 0, tolerance: TOL, hasPrevious: true })).toBe(true)
  })

  it('refuses while the row is still out of place', () => {
    expect(anchorSettleConverged({ delta: 128, aboveDelta: 0, tolerance: TOL, hasPrevious: true })).toBe(false)
  })

  it('refuses while height is still arriving ABOVE the anchor', () => {
    // In tolerance this instant, but the cause has not stopped -- the frame-3 hop.
    expect(anchorSettleConverged({ delta: 0.5, aboveDelta: 49, tolerance: TOL, hasPrevious: true })).toBe(false)
  })

  it('converges during a live turn: appends below the anchor do not delay it', () => {
    // The regression this replaces. A streaming append changes the transcript
    // height on every frame while leaving the anchor's own offset untouched, so
    // `aboveDelta` is 0 and the restore is free to finish.
    expect(anchorSettleConverged({ delta: 0.9, aboveDelta: 0, tolerance: TOL, hasPrevious: true })).toBe(true)
  })

  it('never converges on the first frame, which has nothing to compare against', () => {
    expect(anchorSettleConverged({ delta: 0, aboveDelta: 0, tolerance: TOL, hasPrevious: false })).toBe(false)
  })

  it('treats an above-move within tolerance as stopped, not as motion', () => {
    // Sub-pixel jitter on a fractional-DPR device must not hold the gate open.
    expect(anchorSettleConverged({ delta: 0.2, aboveDelta: 1.0, tolerance: TOL, hasPrevious: true })).toBe(true)
  })
})

/**
 * A source-level guard, because jsdom never reaches this frame: the settle loop runs
 * inside a rAF chain behind a mounted virtual window and a live height index, and the
 * abort under test fires on frame 0, before any measurable position exists.
 *
 * What it pins is one line: the settle's identity check must go through the shared
 * two-identity predicate. A bare `rowId !== anchor.key` there aborts every
 * alt-resolved anchor on the first frame -- `alt` matched because the tail did not --
 * and the reader is left at the estimate-based write this loop exists to correct
 * (measured at +111, -1035, -1792 and -2841px).
 */
describe('the settle abort speaks both identities (source guard)', () => {
  it('compares through anchorMatchesRow, never the bare anchor key', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const src = fs.readFileSync(
      path.resolve(__dirname, '../hooks/virtualizer/useVirtualChat.ts'),
      'utf8',
    )
    expect(src).toContain('anchorMatchesRow({ anchor, tailId: rowId')
    // The shape that disowns an alt-resolved row, in any spacing.
    expect(src).not.toMatch(/if\s*\(\s*rowId\s*!==\s*anchor\.key\s*\)/)
  })
})
