import { describe, expect, it, vi, afterEach } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { installPageZoomSuppression } from '../utils/pageZoom'

// The dashboard is an application shell, not a zoomable document: pinching it
// magnifies a `position: fixed` / `h-dvh` layout that then has no scroll axis to
// reach whatever the magnification pushed off the visual viewport.
//
// No single mechanism covers every engine, which is the whole reason there are
// three and the reason each is asserted separately here:
//   • the viewport meta      — honoured by Blink and Gecko
//   • the root `touch-action` — Blink's double-tap and pinch path
//   • `gesturestart`          — WebKit, which ignores the meta for user gestures
// Delete any one of them and a platform silently regains page zoom.

const root = join(__dirname, '..', '..')
const html = () => readFile(join(root, 'index.html'), 'utf8')
const css = () => readFile(join(root, 'src', 'index.css'), 'utf8')

describe('page zoom is off on the touch shell', () => {
  it('declares the viewport as non-scalable', async () => {
    const m = (await html()).match(/<meta name="viewport" content="([^"]*)"/)
    expect(m, 'expected a viewport meta').not.toBeNull()
    expect(m![1]).toContain('user-scalable=no')
    // Both keys are needed: `user-scalable=no` is what Gecko reads, and a bare
    // `maximum-scale` is what some Blink versions clamp against.
    expect(m![1]).toContain('maximum-scale=1')
  })

  // The other keys sharing this one attribute are NOT re-asserted here: they already
  // have owners, and a second copy only diverges. `viewport-fit=cover` is pinned by
  // safeArea.guard.test.ts, `interactive-widget=resizes-content` and
  // `width=device-width` by mobileKeyboardViewport.test.ts. An edit to the zoom keys
  // that dropped one of them still fails a test — just not this one.

  it('narrows the root touch-action to the two pan axes on coarse pointers', async () => {
    const s = await css()
    // `pan-x pan-y` is the precise value: it keeps scrolling and withholds pinch
    // and double-tap zoom. `none` would take scrolling with it, and `manipulation`
    // withholds only the double-tap.
    expect(s).toMatch(/@media \(pointer: coarse\) \{\s*html \{ touch-action: pan-x pan-y; \}/)
  })

  it('does not touch gestures on pointer-fine devices', async () => {
    const s = await css()
    // A bare `html { touch-action: ... }` outside the coarse query would take
    // ctrl+wheel and the trackpad pinch away from desktop too.
    expect(s).not.toMatch(/^html \{ touch-action:/m)
  })
})

// An app-wide 16px field rule was written for this PR and withdrawn. This spec
// keeps it withdrawn, because the reasons are not visible from the stylesheet and
// the rule reads like an obvious win: CSS can only SET a size, never floor one, so
// a rule broad enough to reach every field SHRANK the artifact rename input from
// `text-2xl` (24px) on touch, while any narrower selector list misses the next
// field written with an arbitrary value, an `!important` modifier or an inline
// style. No source sweep can police either shape either -- ~120 of this app's
// fields go through the `<Input>` / `<Textarea>` components rather than a native
// tag, so a guard grepping for `<input>` cannot see them and reports green.
//
// What such a rule was for -- WebKit's focus zoom, which reads the FIELD's size and
// does not zoom back out on blur -- is a pre-existing condition rather than
// something the zoom suppression introduces, and whether it fires at all under an
// authored `maximum-scale=1` needs a real device to answer.
describe('no app-wide coarse-pointer field size rule', () => {
  it('does not set a blanket font-size on touch form fields', async () => {
    const s = await css()
    // The composer's own hook-scoped rule is expected and pre-dates this change;
    // what must stay absent is a rule reaching fields by ELEMENT.
    const blanket = s.match(/@media \(pointer: coarse\)[^}]*\b(?:input|textarea|select)\b[^{]*\{[^}]*font-size/)
    expect(
      blanket?.[0],
      'a coarse-pointer rule is setting font-size on form fields by element again -- it cannot floor without shrinking a deliberately larger field; see the note in index.css',
    ).toBeUndefined()
  })
})

describe('the axe meta-viewport rule', () => {
  const main = () => readFile(join(root, 'src', 'main.tsx'), 'utf8')

  it('is left enabled, so the unresolved WCAG trade keeps being reported', async () => {
    const s = await main()
    // A waiver for this rule was written and removed. Two reviewers arrived at the
    // same objection from opposite directions -- it removes an existing validation
    // guard, and it silences the one recurring reminder that suppressing page zoom
    // is an accessibility trade nobody has yet accepted in writing. The original
    // argument for waiving ("a permanent finding nobody can action") does not hold:
    // the finding is actionable, because it is a decision.
    expect(s, 'the meta-viewport waiver is back -- see the note in main.tsx').not.toMatch(/'meta-viewport'/)
    // axe must still be installed, and with no rule-spec argument suppressing anything.
    expect(s).toMatch(/axe\.default\(React, ReactDOM, 1000\)/)
    expect(s).toMatch(/website\/docs\/page-layout\.md/)
    expect(s).not.toMatch(/axe\.configure\(\s*\{\s*rules:\s*\[\]/)
  })
})

describe('installPageZoomSuppression', () => {
  const listeners: Array<() => void> = []
  afterEach(() => { while (listeners.length) listeners.pop()!(); vi.restoreAllMocks() })

  /** Force the pointer class the module reads at install time. */
  function pointer(coarse: boolean) {
    vi.spyOn(window, 'matchMedia').mockImplementation(((q: string) => ({
      matches: coarse && q.includes('coarse'), media: q, onchange: null,
      addListener: () => {}, removeListener: () => {},
      addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    })) as typeof window.matchMedia)
  }

  function install() {
    const teardown = installPageZoomSuppression()
    listeners.push(teardown)
    return teardown
  }

  /** Dispatch a cancelable event on document and report whether it was cancelled. */
  function fire(type: string, extra: Record<string, unknown> = {}) {
    const e = new Event(type, { bubbles: true, cancelable: true })
    Object.assign(e, extra)
    document.dispatchEvent(e)
    return e.defaultPrevented
  }

  it('cancels the WebKit pinch gesture on a touch device', () => {
    pointer(true)
    install()
    expect(fire('gesturestart')).toBe(true)
    expect(fire('gesturechange')).toBe(true)
    expect(fire('gestureend')).toBe(true)
  })

  it('leaves a pointer-fine device alone', () => {
    pointer(false)
    install()
    // Desktop Safari raises the same events for a trackpad pinch, where zooming
    // the page is a convention this has no business removing.
    expect(fire('gesturestart')).toBe(false)
  })

  it('cancels a two-finger touchmove only once WebKit has moved the scale', () => {
    pointer(true)
    install()
    // The hole this plugs: a pinch that begins as a one-finger scroll and gains a
    // second finger does not always raise `gesturestart`.
    expect(fire('touchmove', { touches: [{}, {}], scale: 1.4 })).toBe(true)
    // A plain two-finger scroll (scale still 1) must keep scrolling…
    expect(fire('touchmove', { touches: [{}, {}], scale: 1 })).toBe(false)
    // …and a one-finger drag is never in scope, whatever `scale` reads.
    expect(fire('touchmove', { touches: [{}], scale: 2 })).toBe(false)
    // Engines with no `scale` on the event fall through rather than throwing.
    expect(fire('touchmove', { touches: [{}, {}] })).toBe(false)
  })

  it('removes every listener on teardown', () => {
    pointer(true)
    const teardown = install()
    teardown()
    expect(fire('gesturestart')).toBe(false)
    expect(fire('touchmove', { touches: [{}, {}], scale: 1.4 })).toBe(false)
  })

  it('is a no-op that still returns a callable teardown when uninstalled', () => {
    pointer(false)
    expect(() => install()()).not.toThrow()
  })
})
