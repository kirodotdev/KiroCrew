/**
 * The automatic older-history trigger fires on DISTANCE, not on stillness.
 *
 * The affordance-in-view rule carries a lead so the page arrives before the
 * reader does. A lower bound on stillness defeated that entirely: the page could
 * only start once the reader had already stopped, and a continuous climb never
 * stops — so the lead never had a window and history always landed after they had
 * reached the top. Speed is not evidence against the intent; a fast climb wants
 * the history sooner. Runaway paging is prevented elsewhere and structurally: the
 * in-flight `loadingOlder` gate, the thunk's own `condition`, and a per-gesture
 * page budget.
 *
 * Source-scanned because the gate chain lives inside a ChatPage effect with no
 * exported seam — the same convention `ChatPage.idlePrefetchAuth.test.ts` uses.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { earlierAffordanceInView, shouldContinueOlderWalk } from '../pages/chat/pagination'

const SRC = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf8')

/** The walk poll's gate chain, from its authorization call to its dispatch. */
function walkBody(): string {
  const i = SRC.indexOf('sawRealInput: Date.now() - lastRealInputAtRef.current')
  expect(i).toBeGreaterThan(-1)
  const j = SRC.indexOf('loadOlderMessages()', i)
  expect(j).toBeGreaterThan(i)
  return SRC.slice(i, j)
}

describe('older-history trigger distance', () => {
  it('authorizes on an ABSOLUTE pixel distance, not a viewport multiple', () => {
    // The number is a tuning knob the device decides; what must not regress is
    // the unit. Both terms this lead pays for are absolute — a fetch plus a
    // measured landing is a few hundred ms whatever the screen — so scaling the
    // lead with the viewport grows it where the cost did not, handing a desktop
    // far more than it needs while a phone's is set by whatever the browser
    // chrome left over.
    expect(walkBody()).toMatch(/nearTop:\s*el2\.scrollTop <= OLDER_WALK_TRIGGER_PX/)
    const m = SRC.match(/const OLDER_WALK_TRIGGER_PX = (\d+)/)
    expect(m).toBeTruthy()
    // Larger than one phone viewport (~600-800px): below that the page can only
    // start once the reader is already inside the screen they are about to leave.
    expect(Number(m![1])).toBeGreaterThan(1000)
  })

  it('requires the affordance to be genuinely ON SCREEN, with no lead', () => {
    // Reversed deliberately, and the report that reversed it is the whole reason:
    // spending the walk's trigger distance here let the bar count as "in view"
    // while still two screens above the viewport, so history loaded before the
    // reader could see that it was going to. A zero lead makes the visible
    // affordance itself the permission.
    //
    // It cannot deadlock the walk: the reader reaches the bar by scrolling, and
    // once it is on screen the trigger's own near-top test is satisfied too.
    const i = SRC.indexOf('const earlierBarInView = useCallback(')
    const body = SRC.slice(i, SRC.indexOf('}, [])', i))
    expect(body).not.toMatch(/OLDER_WALK_TRIGGER_PX/)
    expect(body).toMatch(/\n\s*0,\n\s*\)/)
    expect(body).not.toMatch(/clientHeight/)
  })

  it('fires on the SCROLL EVENT, not only on the poll tick', () => {
    // The distance is the smaller half of the lead. One OLDER_TOP_POLL_MS of
    // delay is worth more travel than the whole budget (700ms at fling speed is
    // ~3500px against a 2000px lead), so a trigger that only ever ran on the tick
    // could not satisfy the invariant however far out it was set.
    //
    // Pinned as ONE body with TWO callers: a second copy of the gate chain for the
    // scroll path is how one copy drifts from the other.
    expect(SRC).toMatch(/const attemptOlderWalk = \(\) => \{/)
    expect(SRC).toMatch(/setInterval\(attemptOlderWalk, OLDER_TOP_POLL_MS\)/)
    const i = SRC.indexOf('const noteScroll = () => {')
    const body = SRC.slice(i, SRC.indexOf('\n    }', i))
    expect(body).toContain('attemptOlderWalk()')
    // Coalesced per FRAME: a fling delivers many events per frame and the chain
    // ends in an O(rows) measurement sweep, so per-event would spend the frame
    // budget of the gesture this exists to keep smooth.
    expect(body).toMatch(/requestAnimationFrame/)
    // Cancel-and-reschedule, never latch-on-pending: a dropped frame handle must
    // not wedge the trigger for the rest of the mount.
    expect(body).toMatch(/cancelAnimationFrame/)
  })
})

describe('the older-history walk does not wait on the idle measure farm', () => {
  it('has NO farm-measurement gate, in any scoped form', () => {
    // Structural contradiction, not a tuning miss. The measure farm runs in IDLE
    // TIME by design -- that is the whole reason it exists off-screen, so it does
    // not correct heights under the reader's finger. A gate that waits on its
    // progress can therefore only ever pass once the reader has STOPPED, which is
    // what the device reported: "it still only loads previous once I completely
    // stop scrolling". Narrowing which rows it swept did not help, because the
    // blocking row was index 0 -- freshly prepended, never mounted, so the farm is
    // the only thing that could ever measure it.
    const body = walkBody()
    expect(body).not.toMatch(/farmIsMeasured/)
    expect(body).not.toMatch(/farmRowMounted/)
  })

  it('now loads FOR a parked reader, deliberately', () => {
    // This bound used to refuse a reader who had stopped, on the reasoning that
    // pages must not land under someone no longer asking. Reversed: at rest is the
    // one state where a landing cannot be FELT, so it is the state to load in. The
    // reader who is no longer asking is now excluded by the affordance gate
    // instead -- they have to be looking at "Load earlier messages" for anything
    // automatic to happen at all.
    expect(walkBody()).toMatch(/OLDER_WALK_QUIET_MS/)
    expect(walkBody()).not.toMatch(/OLDER_WALK_ACTIVE_MS/)
  })
})

describe('a landing reports its own height against the trigger', () => {
  it('prints the height the page ADDED, in the same units as the threshold', () => {
    // A page that adds less than OLDER_WALK_TRIGGER_PX leaves the reader still
    // inside the lead, so nearTop stays true and the next tick fires again.
    // That reads as a runaway and is really a page too short to clear its own
    // trigger -- one number decides which, and three diagnoses were overturned
    // tonight for want of exactly this kind of number.
    expect(SRC).toMatch(/grew \+\$\{Math\.round\(grew\)\}px vs trigger \$\{OLDER_WALK_TRIGGER_PX\}px/)
  })

  it('measures the delta AFTER paint, not in the effect body', () => {
    // The effect runs in the same commit as the store update, before React has
    // rendered the new rows, so a synchronous scrollHeight read reports the height
    // the transcript had BEFORE the page. That printed a 100-message page as
    // adding 32px and read exactly like a real defect -- an instrument that lies
    // in the direction of alarm is worse than no instrument.
    expect(SRC).toMatch(/olderHeightAtStartRef\.current = el \? el\.scrollHeight : 0/)
    // The read must live inside the double-rAF, next to the paint close.
    const i = SRC.indexOf('devOlderLatency(olderFetchMsRef.current')
    expect(i, 'paint close not found').toBeGreaterThan(0)
    const frame = SRC.slice(i, i + 1400)
    // The element must come from the live ref inside the frame -- a mutation that
    // only swaps the declaration keeps the read expression intact and would slip
    // past an assertion that pins the expression alone.
    expect(frame).toMatch(/const el2 = vScrollerElRef\.current/)
    expect(frame).toMatch(/el2\.scrollHeight : 0\) - olderHeightAtStartRef\.current/)
    expect(frame).toMatch(/grew \+\$\{Math\.round\(grew\)\}px vs trigger \$\{OLDER_WALK_TRIGGER_PX\}px/)
  })
})

describe('older-history trigger waits for stillness', () => {
  it('applies the stillness gate unconditionally, never split by a CSS property', () => {
    // A capability split on `'overflowAnchor' in style` was shipped and withdrawn.
    // It reads as sound -- with CSS scroll anchoring the compositor absorbs a
    // prepend before paint, so rest is unnecessary there -- and it inverted on the
    // one platform it existed for: WebKit has LANDED the property (bug 307734), so
    // it answers the probe on a device whose anchoring does not hold a virtualized
    // list, and the gate switched itself off exactly where it was needed.
    //
    // The device symptom did not point at this gate at all. With it off, a fling
    // near the top drives `scrollTop` negative (iOS rubber band, measured -2810),
    // `isOverscrolled` correctly refuses to read a stretch as a content position
    // and skips compensation, the reader is never advanced, the affordance stays on
    // screen, and the level-triggered poll re-fires -- reported as "it loads
    // continuously", caused by a position that was never held.
    //
    // Presence of a property is not evidence of a behaviour. A behavioural probe
    // (did a prepend land already-correct before we wrote anything?) could bring
    // the split back; a property name may not.
    expect(walkBody()).toMatch(/if \(Date\.now\(\) - lastScrollEvt < OLDER_WALK_QUIET_MS\) return/)
    expect(SRC).not.toMatch(/overflowAnchor/)
    expect(SRC).not.toMatch(/hasNativeScrollAnchoring/)
  })

  it('waits long enough to be past a fling, not merely past a frame', () => {
    // 20ms is barely one frame interval, so it lets the TAIL of a fling through --
    // events thin out there while the surface is still drifting, and that is the
    // landing the reader feels. 100ms is matrix-react-sdk's number, reached
    // independently for the same reason.
    const m = /const OLDER_WALK_QUIET_MS = (\d+)/.exec(SRC)
    expect(m).toBeTruthy()
    expect(Number(m![1])).toBeGreaterThanOrEqual(100)
  })

  it('refuses while the scroller is still moving', () => {
    // Inverted from "does not require rest". The bounce needs MOTION: a landing's
    // compensation puts the reader within a pixel of where they were -- measured on
    // a device, `off=1` read back after the write -- and the jolt is still visible,
    // because a programmatic scrollTop write during a fling perturbs the fling.
    // The reader's own report is the cleanest form of it: at `to-top = 0` loading
    // never bounces, and `to-top = 0` is precisely where they have stopped.
    expect(walkBody()).toMatch(/Date\.now\(\) - lastScrollEvt < OLDER_WALK_QUIET_MS/)
    expect(walkBody()).not.toMatch(/lastScrollEvt > OLDER_WALK_ACTIVE_MS/)
  })

  it('a scroll event ALSO cancels a page already in flight', () => {
    // Rewritten, not deleted: this asserted the opposite, and it was right for an
    // intent that has since been abandoned. Removing the abort belonged to the era
    // of loading WHILE the reader climbed, where killing a fetch on motion killed
    // every fetch. With rest as the permission the two halves are one mechanism --
    // the gate refuses to START a fetch while moving, this refuses to FINISH one.
    //
    // Both are needed because the gate is checked when the fetch begins. A reader at
    // rest who flings during the ~130ms round trip would otherwise take the landing
    // mid-gesture, which is the single moment the compensation cannot be invisible.
    const i = SRC.indexOf('const noteScroll = ')
    const body = SRC.slice(i, SRC.indexOf('}', SRC.indexOf('lastScrollEvt = Date.now()', i)))
    expect(body).toContain('abortActiveOlderFetch')
  })

  it('loads FOR a reader who has come to rest, which is the point', () => {
    // The old upper bound refused exactly this reader. Rest is now the permission
    // rather than the disqualification, so the constant naming the wait exists and
    // the one that timed out a parked reader does not.
    expect(SRC).toMatch(/const OLDER_WALK_QUIET_MS = \d+/)
    expect(SRC).not.toMatch(/const OLDER_WALK_ACTIVE_MS = \d+/)
  })
})

describe('the predicates the chain rests on are unchanged', () => {
  it('still refuses without a real gesture, and still spends a page budget', () => {
    const base = { sawRealInput: true, nearTop: true, walking: false, pagesSinceInput: 0 }
    expect(shouldContinueOlderWalk(base)).toBe(true)
    expect(shouldContinueOlderWalk({ ...base, sawRealInput: false })).toBe(false)
    expect(shouldContinueOlderWalk({ ...base, pagesSinceInput: 99 })).toBe(false)
  })

  it('a two-viewport lead accepts an affordance that far above the viewport', () => {
    const viewport = { top: 0, bottom: 595 }
    const bar = { top: -1150, bottom: -1110 }  // ~1.9 viewports above
    expect(earlierAffordanceInView(bar, viewport, 2 * 595)).toBe(true)
    // ...and one viewport of lead would have refused the same position.
    expect(earlierAffordanceInView(bar, viewport, 595)).toBe(false)
  })
})
