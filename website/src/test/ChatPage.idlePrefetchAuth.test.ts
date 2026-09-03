import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * REGRESSION GUARD — the idle older-prefetch cannot feed itself, and cannot move
 * a reader who is parked at the live end.
 *
 * Reported from a phone as "还是有自动 load previous 的问题，然后导致弹跳" — a page
 * of history landing on its own every few seconds, each landing displacing the
 * reader. Frame analysis of a 60fps recording measured one landing as a 108px
 * downward shift of the whole transcript: older rows prepend ABOVE, so a reader
 * sitting at `scrollTop === max` finds max has grown and is suddenly that far
 * from the bottom.
 *
 * Two independent defects made it self-sustaining:
 *
 * 1. NO POSITION GUARD. The walk poll refuses a bottom-followed reader in as many
 *    words ("its landings are pure disturbance budget"); the prefetch did not, so
 *    it was the one door left open to someone reading the live end.
 *
 * 2. AUTHORIZATION NEVER EXPIRED. `sawRealInputRef` is a one-way latch with a
 *    single write and no reset, so one touch of the transcript — which reading a
 *    long chat requires — unlocked the prefetch permanently. A landing's own
 *    compensation writes scrollTop, that write fires a `scroll` event, and the
 *    quiet timer counts it as activity: land → quiet → land, forever. The fix is
 *    a window refreshed only by `wheel`/`touchmove`, which our own writes never
 *    produce, so it ages out instead of latching.
 *
 * Source-scanned because the prefetch is an interval inside an effect with no
 * exported seam; the arithmetic relation in the third test is the part most
 * likely to be broken silently by a later constant tweak.
 */
const SRC = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf8')

/** The idle-prefetch interval body. */
function prefetchBody(): string {
  const i = SRC.indexOf('IDLE_PREFETCH_QUIET_MS) return')
  expect(i).toBeGreaterThan(-1)
  const end = SRC.indexOf('}, IDLE_PREFETCH_TICK_MS)', i)
  expect(end).toBeGreaterThan(i)
  return SRC.slice(i, end)
}

function constValue(name: string): number {
  const m = SRC.match(new RegExp(`const ${name} = (\\d+)`))
  expect(m).not.toBeNull()
  return Number(m![1])
}

describe('idle older-prefetch', () => {
  it('refuses a bottom-followed reader, like the walk poll already does', () => {
    expect(prefetchBody()).toMatch(/if \(vGetFollowRef\.current\(\)\) return/)
  })

  it('authorizes on a RECENT gesture, never on the one-way latch', () => {
    const body = prefetchBody()
    expect(body).toMatch(/IDLE_PREFETCH_AUTH_MS/)
    expect(body).toMatch(/sawInput: recentInput/)
    // The latch must not be what authorizes this path any more.
    expect(body).not.toMatch(/sawInput: sawRealInputRef\.current/)
  })

  it('keeps the authorization window wider than the quiet window', () => {
    // The prefetch fires only after IDLE_PREFETCH_QUIET_MS of silence, so an
    // authorization window at or below that can never be open when the tick
    // arrives — the prefetch would silently become dead code rather than fail.
    expect(constValue('IDLE_PREFETCH_AUTH_MS')).toBeGreaterThan(constValue('IDLE_PREFETCH_QUIET_MS'))
  })

  it('refreshes the gesture stamp from gestures only, not from scroll', () => {
    // Our own compensation write fires `scroll`. If that refreshed the stamp the
    // window would be self-renewing and the latch would be back under a new name.
    const i = SRC.indexOf('lastRealInputAtRef.current = Date.now()')
    expect(i).toBeGreaterThan(-1)
    const setter = SRC.slice(SRC.lastIndexOf('const noteInput', i), SRC.indexOf('addEventListener', i) + 400)
    expect(setter).toMatch(/'wheel', noteInput/)
    expect(setter).toMatch(/'touchmove', noteInput/)
    expect(setter).not.toMatch(/'scroll', noteInput/)
  })
})

describe('top sentinel (handleTopReached)', () => {
  /** The sentinel handler body. */
  function sentinelBody(): string {
    const i = SRC.indexOf('const handleTopReached = useCallback(')
    expect(i).toBeGreaterThan(-1)
    const end = SRC.indexOf('}, [dispatch])', i)
    expect(end).toBeGreaterThan(i)
    return SRC.slice(i, end)
  }

  it('refuses to page on unsettled geometry', () => {
    // shouldAutoFillOlder's "too short to scroll" branch fires on GEOMETRY, so it
    // fires on a geometry TRANSIENT too — and the composer's text is ChatPage
    // state, so every keystroke re-renders this tree and offers the virtualizer
    // another chance to be caught mid-measurement. Reported from a phone as
    // history loading while TYPING. The walk poll and the idle prefetch both
    // require every row measured before paging; this door is the one the sentinel
    // comes through and it had no such gate.
    expect(sentinelBody()).toMatch(/vFarmIsMeasuredRef\.current\?\.\(i\)/)
  })

  it('authorizes on reader POSITION, not on the one-way input latch', () => {
    // The site's own comment promises "further history is reader-initiated" on a
    // scrollable transcript. `sawRealInputRef` is a latch with one write and no
    // reset, so it cannot express that; not being at the live end can.
    const body = sentinelBody()
    expect(body).toMatch(/sawInput: !vGetFollowRef\.current\(\)/)
    expect(body).not.toMatch(/sawInput: sawRealInputRef\.current/)
  })
})
