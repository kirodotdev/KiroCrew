import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { shiftCompensationAllowed } from '../hooks/virtualizer/useVirtualChat'

/** Who may write the scroll position when rows are inserted above the reader.
 *
 *  The compensation adds the inserted block's height to `scrollTop` so the reader
 *  stays visually still. It stands down for exactly one other owner --
 *  follow-the-tail, which is pinning to the bottom on its own schedule.
 *
 *  It does NOT stand down for "a restore is in flight", and that is the load-
 *  bearing part of this file. A restore genuinely supersedes the capture taken
 *  BEFORE it: it places the reader at an absolute offset priced against the
 *  transcript that already contains those rows, so consuming the capture
 *  afterwards counts the block twice -- measured on a phone as
 *
 *    WRITE restore  3021->965
 *    WRITE reprice2  965->20211      (+19,246px)
 *
 *  The first attempt at that fix made this predicate answer false for the whole
 *  restore window, and it caused a WORSE defect: a prepend arriving inside that
 *  window has its own fresh baseline and genuinely needs compensating, so
 *  blinding the predicate left the reader walked up a page per load, which pulled
 *  the top sentinel into view and reopened the older-history door -- reported as
 *  history loading itself again, one page at a time, on a session that had been
 *  holding 200 messages and went back to 6,362.
 *
 *  So the supersede is handled where it belongs: `restoreAnchor` CLEARS the
 *  capture it supersedes. Ownership is expressed by removing the stale input, not
 *  by silencing the mechanism. */

describe('shiftCompensationAllowed', () => {
  it('allows compensation in the ordinary case: a prepend under a still reader', () => {
    expect(shiftCompensationAllowed({ stick: false, settleMeasuring: false })).toBe(true)
  })

  it('stands down for follow-the-tail, which owns the position itself', () => {
    expect(shiftCompensationAllowed({ stick: true, settleMeasuring: false })).toBe(false)
  })

  it('stands down while the settle is ACTIVELY correcting', () => {
    // Both hold a row where it was, from different reference points, so they
    // fight: measured as five writes in one decisecond (abovefold / settle /
    // abovefold / resize / growth) netting +261px of drift after the landing.
    expect(shiftCompensationAllowed({ stick: false, settleMeasuring: true })).toBe(false)
  })

  it('does NOT stand down for a settle that is merely in charge but blind', () => {
    // The distinction this file exists for. A settle that cannot see its anchor
    // row corrects nothing while still holding its gate; blanking these too left
    // every prepend in that window uncompensated, which walked the reader up a
    // page per landing and reopened the older-history door. Exactly one mechanism
    // corrects at a time -- when the settle goes blind, these take over.
    expect(shiftCompensationAllowed({ stick: false, settleMeasuring: false })).toBe(true)
  })
})

/** Source-level guard, for the half of the rule jsdom cannot reach.
 *
 *  The compensation's consume path runs inside layout effects driven by real DOM
 *  geometry, and jsdom's `getBoundingClientRect` is degenerate there -- so the
 *  double-count can only be reproduced on a device. What IS checkable here is the
 *  structural half: `restoreAnchor` must DROP the capture it supersedes, which is
 *  what lets the predicate above stay blind to restores and keeps a later prepend
 *  compensated. Written against the source for the same reason
 *  FollowController.test.ts reads its own source: an invariant with no runtime
 *  surface is still worth failing a build over. */
describe('restoreAnchor drops the capture it supersedes', () => {
  const src = readFileSync(join(process.cwd(), 'src/hooks/virtualizer/useVirtualChat.ts'), 'utf8')
  const body = (() => {
    const at = src.indexOf('const restoreAnchor = useCallback')
    expect(at).toBeGreaterThan(-1)
    // Up to the settle loop's own scheduling, i.e. the positioning half.
    const end = src.indexOf('const startedAt', at)
    expect(end).toBeGreaterThan(at)
    return src.slice(at, end)
  })()

  for (const clear of [
    'shiftAnchorRef.current = null',
    'shiftStageRef.current = null',
    'shiftInsertedRef.current = 0',
    'prependPreScrollTopRef.current = -1',
  ]) {
    it(`clears ${clear.split('.')[0]}`, () => {
      expect(body).toContain(clear)
    })
  }

  it('clears before computing its own target, not after', () => {
    // Clearing after the write would leave the capture consumable by the very
    // next commit, which is the ordering the double-count came from.
    expect(body.indexOf('shiftAnchorRef.current = null')).toBeLessThan(body.indexOf('writeScrollTop('))
  })
})

/** The bottom pin must re-check ownership at APPLY time, not only when decided.
 *
 *  `scrollToBottom` defers its write to a rAF, while its caller's gate
 *  (`autoFollowAllowed`) asks whether the reader is within a viewport of the
 *  bottom -- true while the OUTGOING session's scrollTop is still in place. So the
 *  decision and the write happen in different states, and a restore lands between
 *  them: captured as `RESTORE.OK idx=5 n=40` followed by a burst of `WRITE
 *  bottom`, putting a reader who left 24,600px from the end at `to-end 0px`.
 *
 *  A single check at entry would not close it, because the gate can go up in that
 *  same frame. Source-level for the reason the sibling guard above is: the race
 *  needs a rAF and real geometry, neither of which jsdom provides. */
describe('scrollToBottom re-checks ownership when it applies', () => {
  const src = readFileSync(join(process.cwd(), 'src/hooks/virtualizer/useVirtualChat.ts'), 'utf8')
  const body = (() => {
    const at = src.indexOf('const scrollToBottom = useCallback')
    expect(at).toBeGreaterThan(-1)
    const end = src.indexOf('// Ensure `index` is mounted', at)
    expect(end).toBeGreaterThan(at)
    return src.slice(at, end)
  })()

  it('refuses at entry, before arming follow', () => {
    const guard = body.indexOf('restoreOwnsPosition()')
    const arm = body.indexOf('stickRef.current = followOutput')
    expect(guard).toBeGreaterThan(-1)
    expect(guard).toBeLessThan(arm)
  })

  it('refuses again inside the deferred write, not only at entry', () => {
    // Two occurrences: the entry guard and the apply-time one.
    const hits = body.split('restoreOwnsPosition()').length - 1
    expect(hits).toBeGreaterThanOrEqual(2)
  })

  it('places the apply-time check inside pinToBottom, above its own write', () => {
    const fn = body.indexOf('const pinToBottom')
    expect(fn).toBeGreaterThan(-1)
    const guardInFn = body.indexOf('restoreOwnsPosition()', fn)
    const writeInFn = body.indexOf('writeScrollTop(', fn)
    expect(guardInFn).toBeGreaterThan(-1)
    expect(guardInFn).toBeLessThan(writeInFn)
  })
})
