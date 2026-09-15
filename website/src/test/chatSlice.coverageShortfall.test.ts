import { describe, it, expect } from 'vitest'
import { slotCoverageShortfall } from '../store/chatSlice'

/**
 * The coverage question a slot switch has to answer: does the bounded window it just
 * read reach every row this tab already holds?
 *
 * It used to be answered from two totals, which cannot tell "I only ever loaded one
 * page of a long transcript" apart from "the server grew past my cache" -- both are
 * a small `cached` against a large `serverTotal`. With no earlier total to subtract
 * it had to assume the worst, so every FIRST visit to a slot assumed a hole and
 * closed it by reading everything: measured on a phone as 110 loaded messages
 * becoming 2,645 against a server total of 2,644, on a slot whose window covered its
 * cache exactly.
 *
 * The rows answer it directly, compared as epoch milliseconds. These pin both.
 */
describe('slotCoverageShortfall', () => {
  /** A valid UTC ISO instant `i` seconds after the base -- built from epoch rather
   *  than string-formatted, because a hand-padded seconds field (`00:00:010`) is not
   *  legal ISO and `Date.parse` declines it, which would make every row unplaceable
   *  and every assertion here pass or fail for the wrong reason. */
  const BASE = Date.parse('2026-01-01T00:00:00Z')
  const iso = (i: number) => new Date(BASE + i * 1000).toISOString()
  /** Rows carrying only what coverage reads. `content` differs per row so two rows at
   *  the same instant are distinguishable, which is the tie case below. */
  const row = (ts: string | undefined, content = String(ts)) => ({ role: 'user', content, ts })
  const rows = (...ts: (string | undefined)[]) => ts.map(t => row(t))

  it('reports nothing to fetch when the window reaches the cache\'s oldest row', () => {
    // The reported case: the cache IS the newest page, and the window is that same
    // page again. Overlap is total, so a refetch buys nothing.
    expect(slotCoverageShortfall({
      cached: rows(iso(10), iso(11), iso(12)),
      window: rows(iso(10), iso(11), iso(12)),
    })).toBe(0)
  })

  it('reports nothing to fetch when the window reaches further back than the cache', () => {
    expect(slotCoverageShortfall({
      cached: rows(iso(10), iso(11)),
      window: rows(iso(4), iso(10), iso(11)),
    })).toBe(0)
  })

  it('counts exactly the cached rows the window sits clear of', () => {
    expect(slotCoverageShortfall({
      cached: rows(iso(1), iso(2), iso(3), iso(20), iso(21)),
      window: rows(iso(20), iso(21)),
    })).toBe(3)
  })

  it('does not depend on the window\'s order', () => {
    // The handler's ordering is not this function's business, and membership does not
    // consult it at all. Fed descending, the answer must not change.
    expect(slotCoverageShortfall({
      cached: rows(iso(1), iso(20)),
      window: rows(iso(21), iso(20)),
    })).toBe(1)
  })

  it('compares INSTANTS, not strings, across the seconds-or-ISO union', () => {
    // A transcript `ts` is seconds-or-ISO. As strings a numeric-seconds row sorts
    // before every ISO row whatever the instant it names, so a cache of epoch
    // seconds against an ISO window would report a hole that is not there --
    // and the reverse direction silently claims coverage it does not have.
    const secs = (isoStr: string) => String(Math.floor(Date.parse(isoStr) / 1000))
    // The SAME two rows, spelled in the two units -- same content, so identity does
    // not turn one row into two.
    expect(slotCoverageShortfall({
      cached: [row(secs(iso(10)), 'a'), row(secs(iso(11)), 'b')],
      window: [row(iso(10), 'a'), row(iso(11), 'b')],
    })).toBe(0)
    // Cached rows genuinely older than the window, expressed in the other unit.
    expect(slotCoverageShortfall({
      cached: rows(secs(iso(1)), secs(iso(2)), iso(20)),
      window: rows(iso(20)),
    })).toBe(2)
  })

  it('places an ISO row by its instant even when the UTC offset differs', () => {
    // 09:00+09:00 IS 00:00Z -- older than 02:00Z, though its digits read later.
    // A string compare calls it newer, reports full coverage, and lets the bounded
    // response drop it.
    expect(slotCoverageShortfall({
      cached: rows('2026-01-01T09:00:00+09:00'),
      window: rows('2026-01-01T02:00:00Z'),
    })).toBe(1)
  })

  it('skips a row whose timestamp cannot be read, on either side', () => {
    // An unreadable `ts` is a LIVE row the server has not stamped -- a turn in
    // progress carries exactly one, spelled `x` in the wire fixtures. It sits at the
    // tail, which a newest-N window reaches by construction, so it is not evidence of
    // older history going missing. Counting it as outside was measured to refetch
    // UNBOUNDED on every switch into a streaming slot.
    expect(slotCoverageShortfall({
      cached: rows(iso(30), 'x'),
      window: rows(iso(30), 'x'),
    })).toBe(0)
    expect(slotCoverageShortfall({
      cached: rows(undefined, iso(30)),
      window: rows(iso(30)),
    })).toBe(0)
  })

  it('still counts a placeable cached row the window sits clear of, live row or not', () => {
    // Skipping the unstamped row must not blind the check to a real hole beside it.
    expect(slotCoverageShortfall({
      cached: rows(iso(1), iso(2), 'x'),
      window: rows(iso(20), 'x'),
    })).toBe(2)
  })

  it('counts a cached row TIED with the floor that the window does not contain', () => {
    // Two rows can share an instant -- a numeric-seconds `ts` has one-second
    // granularity -- and the server's slice can cut between them. A comparison alone
    // calls the tie covered and the row is dropped on replacement.
    expect(slotCoverageShortfall({
      cached: [row(iso(20), 'left-behind'), row(iso(20), 'in-window')],
      window: [row(iso(20), 'in-window'), row(iso(21), 'newer')],
    })).toBe(1)
  })

  it('does NOT count the tied row the window does contain', () => {
    // The commonest switch there is: the tab holds exactly one page, so the cache's
    // oldest row IS the window's floor. Treating every tie as outside would refetch
    // unbounded here, which is the defect this path exists to remove.
    expect(slotCoverageShortfall({
      cached: [row(iso(20), 'floor'), row(iso(21), 'newer')],
      window: [row(iso(20), 'floor'), row(iso(21), 'newer')],
    })).toBe(0)
  })

  it('matches by server mid alone, across every field that mutates', () => {
    // A row's mid is the only stable thing on it. The `ts` is overwritten from the
    // optimistic client value to the server's (sseChatMessage stashes the old one as
    // meta.clientTs precisely because it changes), the role flips streaming ->
    // assistant on finalization, and the content grows partial -> final. Pairing any of
    // them with the mid makes one row read as two, and the false shortfall reloads the
    // whole transcript after nothing more exotic than a send plus a slot switch.
    expect(slotCoverageShortfall({
      cached: [{ role: 'streaming', content: 'partial', ts: iso(20), meta: { mid: 'm-1' } }],
      window: [{ role: 'assistant', content: 'final text', ts: iso(31), meta: { mid: 'm-1' } }],
    })).toBe(0)
  })

  it('still counts two rows carrying one mid when the window holds it once', () => {
    // Dropping role and ts from the mid key cannot mask a hole, because coverage
    // COUNTS: a crafted or duplicated mid is answered by multiplicity, which is why
    // the discrimination `deduplicateByMid` needs costs coverage nothing to drop.
    const m = (content: string) => ({ role: 'user', content, ts: iso(20), meta: { mid: 'm-dup' } })
    expect(slotCoverageShortfall({ cached: [m('one'), m('two')], window: [m('one')] })).toBe(1)
  })

  it('counts duplicates: two identical id-less rows are not collapsed into one', () => {
    // Same role, same instant, same text, no mid on either -- genuinely
    // indistinguishable. A set-based test covers both with the window's single row and
    // the bounded replacement drops one message; each window row may cover only one.
    const dup = () => ({ role: 'user', content: 'ok', ts: iso(20) })
    expect(slotCoverageShortfall({ cached: [dup(), dup()], window: [dup()] })).toBe(1)
    // And two in the window cover two in the cache.
    expect(slotCoverageShortfall({ cached: [dup(), dup()], window: [dup(), dup()] })).toBe(0)
  })

  it('declines when the window holds NO placeable row at all', () => {
    // No floor to compare against, and an unplaceable window replacing a populated
    // cache is the shrink this guard is for.
    expect(slotCoverageShortfall({
      cached: rows(iso(1), iso(2)),
      window: rows('x', undefined),
    })).toBe(2)
  })

  it('reports the whole cache when the window came back empty', () => {
    expect(slotCoverageShortfall({
      cached: rows(iso(1), iso(2)),
      window: rows(),
    })).toBe(2)
  })

  it('reports nothing when the tab holds nothing', () => {
    expect(slotCoverageShortfall({ cached: [], window: [] })).toBe(0)
  })

  /**
   * The rows the server never writes. A bounded window cannot contain one however wide
   * it is asked to be, so counting one as missing is a shortfall that never closes --
   * every switch into the slot would refetch the whole transcript. These carry readable
   * timestamps on purpose: that is exactly what the earlier `ts`-only skip let through.
   */
  describe('rows the server does not keep', () => {
    const clientOnly = (role: string, ts: string) => ({ role, content: role, ts })

    it.each(['queued', 'streaming', 'thinking', 'permission'])(
      'does not count a cached %s row the window cannot hold',
      role => {
        expect(slotCoverageShortfall({
          cached: [row(iso(1)), clientOnly(role, iso(2))],
          window: [row(iso(1))],
        })).toBe(0)
      },
    )

    it('still counts a DURABLE cached row beside a client-only one', () => {
      // Skipping the client-only row must not blind the check to a real hole next to it.
      expect(slotCoverageShortfall({
        cached: [row(iso(1)), clientOnly('queued', iso(2)), row(iso(3))],
        window: [row(iso(3))],
      })).toBe(1)
    })

    it('reports nothing when the cache holds ONLY rows the server never had', () => {
      // A brand-new slot the reader has typed into: an empty window is not a shrink,
      // because there is nothing here the server could have sent back.
      expect(slotCoverageShortfall({
        cached: [clientOnly('queued', iso(1)), clientOnly('permission', iso(2))],
        window: [],
      })).toBe(0)
    })

    it('counts only the DURABLE rows at risk when the window comes back empty', () => {
      // The decline path has to be measured over the comparable cache. Counting the
      // whole of it bills the reader for rows the server was never holding -- and this
      // is the only shape that reaches the branch, since a cache with no durable row
      // at all returns before it.
      expect(slotCoverageShortfall({
        cached: [row(iso(1)), clientOnly('queued', iso(2)), clientOnly('thinking', iso(3))],
        window: [],
      })).toBe(1)
    })

    it('treats a row carrying no role at all as durable', () => {
      // The direction that keeps a genuine hole observable rather than hiding it.
      expect(slotCoverageShortfall({
        cached: [{ content: 'a', ts: iso(1) }],
        window: [],
      })).toBe(1)
    })
  })
})
