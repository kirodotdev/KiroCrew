/**
 * The sent bubble must SHRINK-WRAP, and a steered bubble must land on the same
 * edge as a normal one.
 *
 * Measured on a pod with three images of mixed aspect ratio and a short
 * caption: before, both bubbles were 550px wide (their cap) with a 393px empty
 * band, and the steered one sat 16px short of the normal bubble's end edge;
 * after, both are 272px (widest image + padding) and both land at gap 0.
 *
 * Both parts are load-bearing: shrink-wrapping alone still leaves the two
 * bubbles on different edges, and equalising the edges alone still leaves the
 * empty band.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const src = readFileSync(join(__dirname, '..', 'pages', 'chat', 'UserMessage.tsx'), 'utf8')

describe('sent bubble sizing', () => {
  it('the bubble shrink-wraps instead of only capping its width', () => {
    expect(/message-bubble[^`]*\bw-fit\b/.test(src)).toBe(true)
  })

  it('the steer wrapper shrink-wraps too, so both bubbles share one end edge', () => {
    // The wrapper must carry the SAME cap as the bubble, not just max-w-full:
    // during intrinsic sizing a percentage max-width is treated as none, so a
    // wrapper capped only at 100% inflates to the full column and the capped
    // bubble inside lands at its left edge. min(550px,100%) resolves to 100%
    // below 550px, so the mobile-overflow cap is preserved.
    expect(/className="relative w-fit max-w-\[min\(550px,100%\)\]"/.test(src)).toBe(true)
  })
})
