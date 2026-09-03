import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * A send that lands behind a busy turn mounts the queue stack between the
 * transcript and the composer AND appends a queued row to the message array.
 * The virtualizer's viewport branch does re-pin for the scroller shrink, but
 * the append regroups and remounts tail rows while the band's spring animates,
 * so that re-pin can land on interior heights that are still settling —
 * measured pre-fix: the scroller reports "at bottom" while the content sits a
 * card-height (~21px) low, recovering or not depending on which re-render
 * lands last. Users saw the reply's last line sliced at the clip edge directly
 * above the queue card, intermittently.
 *
 * The tip card and the survey card have the same geometry and are compensated
 * by the `[activeTip, surveyLayoutTick]` effect. The status stack CANNOT ride
 * that effect: the queue stack's height is spring-animated, so a one-shot
 * re-anchor at mount time measures a half-grown band and leaves the remainder
 * clipped. It gets a ResizeObserver on the stack wrapper instead — fired after
 * every layout step of the animation, each one re-anchoring while FOLLOW holds,
 * so the final write always follows the last height change instead of racing it.
 *
 * Asserted against SOURCE TEXT like the two neighbouring mask guards
 * (fadeClearance, statusStackAboveMask): the invariant is the wiring between
 * a JSX attribute, a callback ref, and the observer body — none of which jsdom
 * can exercise (happy-dom has no layout, so a real ResizeObserver never fires).
 */
const CHAT_PAGE = readFileSync(resolve(__dirname, '..', 'pages/ChatPage.tsx'), 'utf8')

describe('composer status stack re-anchors the transcript while it resizes', () => {
  it('the stack wrapper carries the observer ref', () => {
    // The ref must sit on the SAME element statusStackAboveMask pins as the
    // stack wrapper — observing anything narrower (one child) goes blind when
    // a different band mounts.
    expect(CHAT_PAGE).toMatch(
      /<div ref=\{composerBandRef\} className="[^"]*" data-testid="composer-status-stack">/,
    )
  })

  it('the ref attaches a ResizeObserver that re-anchors only while following', () => {
    // The dep list is matched loosely on purpose: which callbacks the closure
    // captures is incidental, and pinning it verbatim is what makes this test
    // fail on a change that strengthens the gate it actually guards.
    const cb = /const composerBandRef = useCallback\(\(el: HTMLDivElement \| null\) => \{([\s\S]*?)\n {2}\}, \[[^\]]*scrollBottom[^\]]*\]\)/.exec(CHAT_PAGE)
    expect(cb, 'composerBandRef callback not found in ChatPage.tsx').not.toBeNull()
    const body = cb![1]
    // Re-attaches across unmount/remount: the previous observer is dropped first.
    expect(body).toContain('composerBandObserverRef.current?.disconnect()')
    // The observer body is gated on FOLLOW — a reader parked above the bottom has
    // released follow, and re-anchoring would yank them — and the scroll is
    // instant ('auto' via scrollBottom(true)) so it can track an animation.
    //
    // `autoFollowAllowed()` is that gate, and it is strictly stronger than the
    // follow flag alone: the flag is `stickRef.current` and nothing else, so a
    // band arriving while a reader sits far up used to satisfy it. It now also
    // requires live geometry near the bottom.
    expect(body).toMatch(/new ResizeObserver\(\(\) => \{\s*if \(autoFollowAllowed\(\)\) scrollBottom\(true\)\s*\}\)/)
    expect(body).toContain('ro.observe(el)')
  })
})
