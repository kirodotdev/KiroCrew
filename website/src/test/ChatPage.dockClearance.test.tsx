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
 *  4. The welcome hero is the one box that ENDS above the dock (a margin by the
 *     same measurement, not padding under it): its cards and Refresh link are
 *     controls, and a control under the glass is an ambiguous tap (#15820's
 *     hero half, kept when the rest of #15820 was reverted in #16291). It is
 *     its own stacking context so its z-indexed cards never climb over the
 *     composer.
 *
 * There is deliberately NO opaque fade band between transcript and dock any
 * more: the material's own blur and tint are what keep the dock legible, and a
 * solid gradient would hide exactly the content the layout exists to show.
 *
 * Asserted against SOURCE TEXT: the wiring spans a constant, a hook and two JSX
 * attributes, and happy-dom has no layout for a ResizeObserver to fire against.
 */
const CHAT_PAGE = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')
// The dock's measurement lives in its owner; the page mounts the box.
const DOCK = readFileSync(resolve(__dirname, '../pages/chat/page/composerDock.tsx'), 'utf8')
const WELCOME_VIEW = readFileSync(resolve(__dirname, '../components/WelcomeView.tsx'), 'utf8')

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
    expect(CHAT_PAGE).toMatch(/<div ref=\{dockRef\} className="[^"]*\babsolute\b[^"]*\bbottom-0\b[^"]*" style=\{\{ right: dockGutter \}\} data-testid="composer-dock-root"[^>]*>/)
    // A callback ref, not a `[]` layout effect: the dock sits inside the pane's
    // conditional branch, so a mount-once effect can run before it exists and
    // never measure. The ref fires on every mount/unmount of the box.
    // The same measurement reads the scroller's reserved scrollbar gutter, so
    // the dock's `right` inset lines its column up with the transcript's and
    // leaves the thumb uncovered — hence `scrollerRef` in the deps.
    const hook = /const dockRef = useCallback\(\(el: HTMLDivElement \| null\) => \{[\s\S]*?if \(!el\) \{ setDockH\(0\); setDockGutter\(0\); return \}[\s\S]*?setDockH\(el\.offsetHeight\)[\s\S]*?setDockGutter\(sc \? Math\.max\(0, sc\.offsetWidth - sc\.clientWidth\) : 0\)[\s\S]*?new ResizeObserver\(measure\)[\s\S]*?ro\.observe\(el\)[\s\S]*?\}, \[scrollerRef\]\)/
    expect(DOCK).toMatch(hook)
    expect(CHAT_PAGE, 'the page takes dockRef from the dock owner').toMatch(/const \{ inputAreaRef, dockH, dockGutter, dockRef \} = useComposerDockMetrics\(scrollerRef\)/)
    expect(CHAT_PAGE).toMatch(/<div ref=\{dockRef\} className="[^"]*" style=\{\{ right: dockGutter \}\} data-testid="composer-dock-root"[^>]*>/)
    for (const src of [CHAT_PAGE, DOCK]) expect(src).not.toMatch(/useLayoutEffect\(\(\) => \{\s*const el = dockRef\.current/)
  })

  it('states the clearance in px, never in viewport units', () => {
    // A spacer sized in vh/dvh/svh/lvh reads as px to the arithmetic while still
    // shrinking on a phone.
    for (const src of [CHAT_PAGE, DOCK]) expect(src).not.toMatch(/height:\s*['"]?\d+(\.\d+)?(vh|dvh|svh|lvh)/)
    expect(CHAT_PAGE).toMatch(/<div style=\{\{ height: TRANSCRIPT_TAIL_SPACER_PX \}\} \/>/)
  })

  it('ends the welcome hero above the dock by the same measurement', () => {
    // A margin, not padding under the glass: the cards and the Refresh link are
    // controls, and a label blurred under the composer or the memory-mode chip
    // read as a glitch with an ambiguous tap target (maintainer ruling, #15820).
    expect(CHAT_PAGE).toMatch(/key="welcome-hero"[\s\S]{0,2000}?style=\{\{ marginBottom: dockH \}\}/)
    expect(CHAT_PAGE).not.toMatch(/key="welcome-hero"[\s\S]{0,2000}?style=\{\{ paddingBottom: dockH \}\}/)
  })

  it('isolates the welcome hero so its own z-indexed cards never climb over the dock', () => {
    // WelcomeView layers a hovered card at z-10 and the Refresh link at z-20;
    // the composer inside the (z-index: auto) dock root is z-10. Without a
    // stacking context on the hero those compared directly and the Refresh link
    // painted over the input box.
    const hero = /key="welcome-hero"[\s\S]{0,2000}?className="([^"]*)"/.exec(CHAT_PAGE)
    expect(hero).not.toBeNull()
    expect(hero![1].split(/\s+/)).toContain('isolate')
    expect(hero![1].split(/\s+/)).toContain('overflow-y-auto')
  })

  it('lets the welcome column grow so the hero scrolls inside its box, and fits a short window', () => {
    // In the column mode the column must grow to its content: shrunk by
    // `min-h-0`, its rows spilled past the box as overflow the scroller never
    // counted, so it could not scroll. The grid mode (wide AND tall) keeps
    // `min-h-0`: there the fr rows must size from the hero's height, or intrinsic
    // sizing scales every fr row from the tallest one's ratio and the grid
    // outgrows the box. A taller-than-box column needs `safe center`, or its top
    // scrolls out of reach. Under 600px tall the cards are compact rows and the
    // brand mark yields, so both rows and the Refresh link fit above the dock.
    const layout = /<div data-testid="welcome-layout" className="([^"]*)"/.exec(WELCOME_VIEW)
    expect(layout).not.toBeNull()
    expect(layout![1].split(/\s+/)).not.toContain('min-h-0')
    expect(layout![1].split(/\s+/)).toContain('[@media(min-width:640px)_and_(min-height:600px)]:min-h-0')
    expect(WELCOME_VIEW).toMatch(/className="contents \[@media\(max-height:599px\)\]:hidden">\{brandMark\}/)
    expect(WELCOME_VIEW).toMatch(/className="relative \[@media\(min-width:640px\)_and_\(min-height:600px\)\]:h-\[108px\] hover:z-10 focus-within:z-10"/)
    expect(WELCOME_VIEW).not.toMatch(/className="relative sm:h-\[108px\]/)
    // A truncated compact row names its full text: hover/focus wraps it, and
    // the native tooltip covers a pointer that only pauses.
    expect(WELCOME_VIEW).toMatch(/title=\{text\}/)
    expect(WELCOME_VIEW).toMatch(/truncate group-hover:whitespace-normal group-focus-visible:whitespace-normal/)
    const hero = /key="welcome-hero"[\s\S]{0,2000}?className="([^"]*)"/.exec(CHAT_PAGE)
    expect(hero![1].split(/\s+/)).toContain('[justify-content:safe_center]')
    expect(hero![1].split(/\s+/)).not.toContain('justify-center')
  })

  it('has no opaque fade band between the transcript and the dock', () => {
    for (const src of [CHAT_PAGE, DOCK]) {
      expect(src).not.toMatch(/bg-gradient-to-t from-bg from-\[\d+%\] to-transparent/)
      expect(src).not.toMatch(/TRANSCRIPT_MASK_ABOVE_PX|COMPOSER_MASK_OVERSHOOT_PX/)
    }
  })

  it('keeps the memory chip row transparent, so the conversation shows through the glass', () => {
    const row = /<div className="([^"]*)" data-testid="composer-memory-chip">/.exec(CHAT_PAGE)
    expect(row).not.toBeNull()
    expect(row![1]).not.toMatch(/\bbg-bg\b/)
  })
})
