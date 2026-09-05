import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * REGRESSION GUARD — an automatic older-history fetch is authorized by a REAL
 * gesture, and that authorization ages out instead of latching.
 *
 * Reported from a phone as "还是有自动 load previous 的问题，然后导致弹跳" — a page
 * of history landing on its own every few seconds, each landing displacing the
 * reader. Frame analysis of a 60fps recording measured one landing as a 108px
 * downward shift of the whole transcript: older rows prepend ABOVE, so a reader
 * sitting at `scrollTop === max` finds max has grown and is suddenly that far
 * from the bottom.
 *
 * What made it self-sustaining: a one-way input latch with a
 * single write and no reset, so one touch of the transcript — which reading a
 * long chat requires — unlocked the automatic doors permanently. A landing's own
 * compensation writes scrollTop, that write fires a `scroll` event, and a quiet
 * timer counts it as activity: land → quiet → land, forever. The authorization
 * is therefore a WINDOW refreshed only by `wheel`/`touchmove`, which our own
 * writes never produce, so it ages out instead of latching.
 *
 * Source-scanned because these gates live in interval bodies and callbacks with
 * no exported seam.
 */
const SRC = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf8')

describe('real-gesture authorization', () => {
  it('refreshes the gesture stamp from gestures only, not from scroll', () => {
    // Our own compensation write fires `scroll`. If that refreshed the stamp the
    // window would be self-renewing and the latch would be back under a new name.
    const i = SRC.indexOf('lastRealInputAtRef.current = Date.now()')
    expect(i).toBeGreaterThan(-1)
    const setter = SRC.slice(SRC.lastIndexOf('const noteInput', i), SRC.indexOf('addEventListener', i) + 1600)
    expect(setter).toMatch(/'wheel', noteInput/)
    expect(setter).toMatch(/'touchmove', noteInput/)
    // A pointer and a wheel are not the only human ways to reach the top. Gating on
    // wheel/touchmove ALONE silenced automatic older history for keyboard readers
    // (PgUp/Home/space) and scrollbar-drag readers, so the vocabulary covers them
    // too. This does not loosen the window's premise -- writing `scrollTop` fires
    // none of these four, so an automatic scroll still cannot authorize itself.
    expect(setter).toMatch(/'keydown', noteInput/)
    expect(setter).toMatch(/'pointerdown', noteInput/)
    expect(setter).not.toMatch(/'scroll', noteInput/)
  })

  it('binds the keyboard vocabulary to the SCROLLER, never the document', () => {
    // Scoping matters as much as the vocabulary: a document-level keydown would let
    // typing in the composer authorize a history fetch, which is the same category
    // of mistake as reading our own scroll write as consent.
    expect(SRC).not.toMatch(/(?:document|window)\.addEventListener\(\s*'keydown', noteInput/)
    expect(SRC).toMatch(/el\?\.addEventListener\('keydown', noteInput/)
  })
})

describe('top sentinel (handleTopReached)', () => {
  /** The sentinel handler body. */
  function sentinelBody(): string {
    const i = SRC.indexOf('const handleTopReached = useCallback(')
    expect(i).toBeGreaterThan(-1)
    // The handler's own closing line, whatever its dependency list holds. Anchoring
    // on one literal dep array silently over-slices the moment a dep is added, and
    // then these assertions can pass on code from a LATER function.
    const end = SRC.indexOf('\n  }, [', i)
    expect(end).toBeGreaterThan(i)
    return SRC.slice(i, end)
  }

  it('refuses to page on unsettled geometry', () => {
    // shouldAutoFillOlder's "too short to scroll" branch fires on GEOMETRY, so it
    // fires on a geometry TRANSIENT too — and the composer's text is ChatPage
    // state, so every keystroke re-renders this tree and offers the virtualizer
    // another chance to be caught mid-measurement. Reported from a phone as
    // history loading while TYPING. The walk poll requires every row measured
    // before paging; this door is the one the sentinel comes through and it had
    // no such gate.
    expect(sentinelBody()).toMatch(/vFarmIsMeasuredRef\.current\?\.\(i\)/)
  })

  it('separates "not loaded yet" from "too short to scroll"', () => {
    // shouldAutoFillOlder's geometry branch returns before it ever reads
    // `sawInput`, so no authorization requirement can close it — and an EMPTY
    // transcript satisfies it just as a genuinely short one does. A switch
    // installs an empty list, restores cursor ownership, and leaves the earlier
    // bar in view with nothing above it, which together read as "reader parked at
    // the top asking for history" while the reader has done nothing. Reported from
    // a phone as load-previous on every session switch.
    // The measured-rows loop cannot stand in for this: over zero rows it checks
    // nothing and falls straight through.
    const body = sentinelBody()
    expect(body).toMatch(/if \(displayItemsRef\.current\.length === 0\) return/)
    // Anchor on the CALL, not the bare name: this handler's comments discuss
    // shouldAutoFillOlder by name, so a bare-name indexOf compares prose order.
    expect(body.indexOf('length === 0')).toBeLessThan(body.indexOf('!shouldAutoFillOlder({'))
  })

  it('never inherits authorization from the session just left', () => {
    // Each of these has exactly one write site (`noteInput`, on a real
    // wheel/touchmove), and that listener's effect is not keyed on the slot — so
    // without a per-slot clear, a gesture in the session you left authorizes the
    // doors in the one you opened. A one-way "has this session ever seen input"
    // latch used to sit alongside these; it is gone rather than reset, because an
    // authorization that can only turn ON is not one -- both automatic doors now
    // read the same EXPIRING window, so leaving a slot lets it age out.
    const i = SRC.indexOf('useEffect(() => {\n    lastRealInputAtRef.current = 0')
    expect(i).toBeGreaterThan(-1)
    const body = SRC.slice(i, SRC.indexOf('}, [', i) + 20)
    expect(body).toMatch(/lastRealInputAtRef\.current = 0/)
    expect(body).toMatch(/sentinelPagesSinceInputRef\.current = 0/)
    // The walk poll's own pair. These were effect-LOCAL `let`s, which the effect
    // reissued whole every time it re-created -- a budget a re-render can hand out
    // again is not a budget -- so they are refs now and belong to this clear too.
    expect(body).toMatch(/walkPagesSinceInputRef\.current = 0/)
    expect(body).toMatch(/walkLastInputAtRef\.current = Number\.NEGATIVE_INFINITY/)
    // Keyed on the slot, so entering ANY session starts from no authorization.
    expect(body).toMatch(/\}, \[activeSlot\]\)/)
  })

  it('authorizes on a RECENT gesture — not on the latch, and not on follow', () => {
    const body = sentinelBody()
    // Neither obvious signal can key this. A one-way input latch (one write, no
    // reset) left one touch of a scrollable transcript authorizing the automatic
    // doors for the rest of the mount; it no longer exists anywhere in the file.
    expect(SRC).not.toMatch(/sawRealInputRef/)
    // And `!follow` is the design shouldAutoFillOlder's own contract names as
    // falsified — follow is released with no reader input by an anchor restore and
    // at slot entry, where `lastWriteTop` resets to -1 so the idle branch's
    // self-check cannot rescue it. The replica probe measured one page per ~8s
    // through that door with zero input events.
    expect(body).not.toMatch(/sawInput: !vGetFollowRef\.current\(\)/)
    expect(body).toMatch(/sawInput: Date\.now\(\) - lastRealInputAtRef\.current <= REAL_GESTURE_AUTH_MS/)
  })

  it('bounds the pages one gesture buys', () => {
    // Authorization alone is not enough: the window is 20s and a landing does not
    // close it, so an unbounded door let one flick chain prepends until history ran
    // out and left the reader at the very start of the transcript. The walk poll
    // bounds itself the same way; this door must too.
    const body = sentinelBody()
    expect(body).toMatch(/sentinelPagesSinceInputRef\.current >= OLDER_WALK_MAX_PAGES_PER_INPUT/)
    // The short-transcript fill stays exempt — it bounds itself, since every page
    // makes the transcript taller until the geometry branch stops admitting.
    expect(body).toMatch(/el\.scrollHeight > el\.clientHeight \+ OLDER_FILL_SLACK_PX/)
  })
})
