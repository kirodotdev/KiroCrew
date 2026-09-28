import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * The composer dock floats over the bottom of the transcript scroller (iOS
 * toolbar layout): the scroller runs the full height of the pane and the
 * conversation scrolls UNDER the glass. That only works while the scroller pays
 * for the covered strip, so four things are pinned:
 *
 *  1. The scroller's bottom padding is the dock's MEASURED height plus a fixed
 *     px clearance — never a constant. The dock's height is whatever the status
 *     stack, the follow-up chips, the approval bar and the composer's own growth
 *     add up to, and each of those changes on its own.
 *  2. That measurement comes from a callback ref (commit-phase, so the first
 *     painted frame already carries the right padding) that attaches a
 *     ResizeObserver to the dock root, so later growth re-pads without a frame
 *     where the last line sits under the glass.
 *  3. The clearance and the tail spacer are stated in px, never viewport units.
 *     As `2vh` the clearance tracked the viewport and cut into the last line on
 *     every phone while every desktop viewport looked fine.
 *  4. The welcome hero pads by the same measurement, so it centres in the strip
 *     the dock leaves visible rather than behind it.
 *
 * There is deliberately NO opaque fade band between transcript and dock any
 * more: the material's own blur and tint are what keep the dock legible, and a
 * solid gradient would hide exactly the content the layout exists to show.
 *
 * Asserted against SOURCE TEXT: the wiring spans a constant, a hook and two JSX
 * attributes, and happy-dom has no layout for a ResizeObserver to fire against.
 */
const CHAT_PAGE = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')

const num = (re: RegExp, src: string): number => {
  const m = re.exec(src)
  expect(m, `pattern not found: ${re}`).not.toBeNull()
  return Number(m![1])
}

describe('composer dock clearance', () => {
  it('pads the scroller by the measured dock height plus the px clearance', () => {
    expect(CHAT_PAGE).toMatch(/scrollerStyle=\{\{ paddingBottom: dockH \+ DOCK_CLEARANCE_PX,/)
    expect(num(/const DOCK_CLEARANCE_PX = (\d+)/, CHAT_PAGE)).toBeGreaterThan(0)
    expect(num(/const TRANSCRIPT_TAIL_SPACER_PX = (\d+)/, CHAT_PAGE)).toBeGreaterThan(0)
  })

  it('measures the dock root from a callback ref through a ResizeObserver', () => {
    expect(CHAT_PAGE).toMatch(/<div ref=\{dockRef\} className="[^"]*\babsolute\b[^"]*\bbottom-0\b[^"]*" style=\{\{ right: dockGutter \}\} data-testid="composer-dock-root">/)
    // A callback ref, not a `[]` layout effect: the dock sits inside the pane's
    // conditional branch, so a mount-once effect can run before it exists and
    // never measure. The ref fires on every mount/unmount of the box.
    // The same measurement reads the scroller's reserved scrollbar gutter, so
    // the dock's `right` inset lines its column up with the transcript's and
    // leaves the thumb uncovered — hence `scrollerRef` in the deps.
    const hook = /const dockRef = useCallback\(\(el: HTMLDivElement \| null\) => \{[\s\S]*?if \(!el\) \{ setDockH\(0\); setDockGutter\(0\); return \}[\s\S]*?setDockH\(el\.offsetHeight\)[\s\S]*?setDockGutter\(sc \? Math\.max\(0, sc\.offsetWidth - sc\.clientWidth\) : 0\)[\s\S]*?new ResizeObserver\(measure\)[\s\S]*?ro\.observe\(el\)[\s\S]*?\}, \[scrollerRef\]\)/
    expect(CHAT_PAGE).toMatch(hook)
    expect(CHAT_PAGE).toMatch(/<div ref=\{dockRef\} className="[^"]*" style=\{\{ right: dockGutter \}\} data-testid="composer-dock-root">/)
    expect(CHAT_PAGE).not.toMatch(/useLayoutEffect\(\(\) => \{\s*const el = dockRef\.current/)
  })

  it('states the clearance in px, never in viewport units', () => {
    // A spacer sized in vh/dvh/svh/lvh reads as px to the arithmetic while still
    // shrinking on a phone.
    expect(CHAT_PAGE).not.toMatch(/height:\s*['"]?\d+(\.\d+)?(vh|dvh|svh|lvh)/)
    expect(CHAT_PAGE).toMatch(/<div style=\{\{ height: TRANSCRIPT_TAIL_SPACER_PX \}\} \/>/)
  })

  it('pads the welcome hero by the same measurement', () => {
    expect(CHAT_PAGE).toMatch(/key="welcome-hero"[\s\S]{0,600}?style=\{\{ paddingBottom: dockH \}\}/)
  })

  it('has no opaque fade band between the transcript and the dock', () => {
    expect(CHAT_PAGE).not.toMatch(/bg-gradient-to-t from-bg from-\[\d+%\] to-transparent/)
    expect(CHAT_PAGE).not.toMatch(/TRANSCRIPT_MASK_ABOVE_PX|COMPOSER_MASK_OVERSHOOT_PX/)
  })

  it('keeps the memory chip row transparent, so the conversation shows through the glass', () => {
    const row = /<div className="([^"]*)" data-testid="composer-memory-chip">/.exec(CHAT_PAGE)
    expect(row).not.toBeNull()
    expect(row![1]).not.toMatch(/\bbg-bg\b/)
  })
})
