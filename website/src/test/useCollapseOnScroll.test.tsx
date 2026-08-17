/**
 * The chrome hides on the way down and comes back on the way up.
 *
 * The settle-window assertion is the load-bearing one. Collapsing the chrome
 * GROWS the gallery scroller's clientHeight, which lowers its maximum
 * scrollTop; near the bottom the browser then clamps scrollTop downward to fit.
 * That clamp arrives as a scroll event with a negative delta — identical in
 * shape to the reader swiping up — so without the window it re-expands the
 * chrome, which shrinks the scroller, which lets scrollTop grow again. The
 * result is a header that flickers exactly when the list is longest.
 *
 * `performance.now` is stubbed rather than advanced with fake timers because the
 * window is compared against it directly; controlling it makes the boundary
 * exact instead of dependent on how a timer mock rounds.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { useCollapseOnScroll, CHROME_ATTR, COLLAPSE_MS } from '../hooks/useCollapseOnScroll'

let host: HTMLDivElement
let scroller: HTMLDivElement
let clock = 0

/** Drive the scroller the way the browser does: move scrollTop, then dispatch
 *  a scroll event from the scroller itself (scroll does not bubble — the hook
 *  listens in the capture phase, which is what this exercises). */
function scrollTo(y: number) {
  act(() => {
    Object.defineProperty(scroller, 'scrollTop', { value: y, configurable: true })
    scroller.dispatchEvent(new Event('scroll', { bubbles: false }))
  })
}

const advance = (ms: number) => { clock += ms }
/** Comfortably past the settle window, which is derived from COLLAPSE_MS.
 *  A hardcoded wait here silently falls INSIDE the window as soon as the
 *  animation is slowed, and the failure reads as "the hook stopped
 *  expanding" rather than "the test's assumption went stale". */
const PAST_WINDOW = COLLAPSE_MS + 200

beforeEach(() => {
  clock = 1_000
  vi.spyOn(performance, 'now').mockImplementation(() => clock)
  host = document.createElement('div')
  scroller = document.createElement('div')
  host.appendChild(scroller)
  document.body.appendChild(host)
})

afterEach(() => {
  vi.restoreAllMocks()
  host.remove()
})

const mount = (enabled = true) => {
  // A STABLE ref object, as `useRef` gives a real caller. A fresh object per
  // render would re-run the hook's effect and reset its travel accumulator,
  // which looks exactly like the hook failing to expand.
  const hostRef = { current: host }
  return renderHook(() => useCollapseOnScroll(hostRef, enabled))
}

describe('useCollapseOnScroll', () => {
  it('collapses after a deliberate downward swipe past the arming offset', () => {
    const { result } = mount()
    expect(result.current).toBe(false)
    scrollTo(0)
    scrollTo(200)
    expect(result.current).toBe(true)
  })

  it('does not collapse while the first rows are still on screen', () => {
    const { result } = mount()
    scrollTo(0)
    // Past the old 24px trigger but short of the arming offset.
    scrollTo(30)
    expect(result.current).toBe(false)
  })

  it('ignores a small reading scroll — the flick has to be deliberate', () => {
    // The reported feel problem: a normal scroll of a few tens of pixels flipped
    // the chrome. Well past the arming offset, so what this pins is the travel
    // budget itself, not the offset.
    const { result } = mount()
    scrollTo(0)
    scrollTo(200)
    expect(result.current).toBe(true)
    advance(PAST_WINDOW)
    // A 40px nudge back up must NOT bring the chrome back.
    scrollTo(160)
    expect(result.current).toBe(true)
  })

  it('expands again on an upward swipe', () => {
    const { result } = mount()
    scrollTo(0)
    scrollTo(400)
    expect(result.current).toBe(true)
    advance(PAST_WINDOW) // derived, so dialling COLLAPSE_MS cannot stale it
    scrollTo(300)
    expect(result.current).toBe(false)
  })

  it('ignores the clamp that its own collapse causes', () => {
    const { result } = mount()
    scrollTo(0)
    scrollTo(600)
    expect(result.current).toBe(true)
    // The browser clamps scrollTop downward because the scroller just got
    // taller. Same shape as a swipe up, and it lands inside the window.
    advance(20)
    scrollTo(340)
    expect(result.current).toBe(true)
  })

  it('opens at the top even inside the settle window', () => {
    // The reader flicks hard back to the start right after a collapse. Travel
    // alone would also expand here, so what this pins is the ORDERING: the
    // top rule has to be read before the settle window swallows the event,
    // otherwise the filters stay hidden with the list already at row 1.
    const { result } = mount()
    scrollTo(0)
    scrollTo(600)
    expect(result.current).toBe(true)
    advance(20) // still inside the window
    scrollTo(0)
    expect(result.current).toBe(false)
  })

  it('ignores a scroller that lives inside the chrome', () => {
    // The expanded "From your chats" list is inside the collapsing region and
    // has its own scroller. Capture-phase delivery reaches it too, so without
    // the opt-out, scrolling that list hides the list — it slides out from under
    // the finger mid-gesture.
    const nested = document.createElement('div')
    nested.setAttribute(CHROME_ATTR, '')
    const sub = document.createElement('div')
    nested.appendChild(sub)
    host.appendChild(nested)

    const { result } = mount()
    act(() => {
      Object.defineProperty(sub, 'scrollTop', { value: 0, configurable: true })
      sub.dispatchEvent(new Event('scroll'))
      Object.defineProperty(sub, 'scrollTop', { value: 600, configurable: true })
      sub.dispatchEvent(new Event('scroll'))
    })
    expect(result.current).toBe(false)

    // The gallery's own scroller still drives it.
    scrollTo(0)
    scrollTo(600)
    expect(result.current).toBe(true)
  })

  it('keeps the settle window derived from the collapse it must outlast', () => {
    // A second literal here is how the window silently becomes shorter than the
    // animation, at which point the reflow the animation causes lands back in
    // the accumulator and re-triggers.
    const src = readFileSync(join(__dirname, '..', 'hooks', 'useCollapseOnScroll.ts'), 'utf8')
    expect(src).toMatch(/const SETTLE_MS = COLLAPSE_MS \+ \d+/)
  })

  it('registers no scroll listener at all while disabled', () => {
    // The return value being false is not evidence: with no listener the state
    // never leaves its initial value either, so a passing "returns false" test
    // would pass with the guard deleted too.
    const spy = vi.spyOn(host, 'addEventListener')
    const { result } = mount(false)
    expect(spy).not.toHaveBeenCalled()
    scrollTo(900)
    expect(result.current).toBe(false)
  })
})

describe('ArtifactsPage wires the collapse to the chrome, narrow only', () => {
  const src = readFileSync(join(__dirname, '..', 'pages', 'ArtifactsPage.tsx'), 'utf8')

  it('gates the collapse on a phone AND on the gallery owning the axis', () => {
    // Ungated it would move the header on desktop, where the chrome is a small
    // fraction of a tall column; without `galleryOwnsScroll` it would arm on the
    // content-sized gallery, whose scroller is the page column itself.
    expect(src).toMatch(/useCollapseOnScroll\(chromeHostRef,\s*isMobile && galleryOwnsScroll\)/)
  })

  it('wraps every pinned block — header, filter rows, and the chats section', () => {
    // Any one left out stays pinned, which is the 317px defect partly fixed.
    // Each stays in its own parent (the header is a sibling of the scrolling
    // column, the chats section lives inside the dnd column) so the expanded
    // layout is unchanged.
    expect(src.match(/<CollapsibleChrome collapsed=\{chromeCollapsed\}>/g)).toHaveLength(3)
  })

  it('listens on the element that CONTAINS the scroller, not the scroller', () => {
    // The virtualization library owns its scroller and exposes no ref, so the
    // host is the page column and the hook relies on capture-phase delivery.
    expect(src).toMatch(/ref=\{chromeHostRef\}/)
  })
})
