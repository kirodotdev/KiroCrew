import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
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
// ChatPage composes owners under pages/chat/page/ (and its useChatPage* siblings), so
// the pins read the page and those owners.
const PAGES_DIR = resolve(__dirname, '../pages')
const OWNER_DIR = resolve(PAGES_DIR, 'chat/page')
const CHAT_PAGE = [
  readFileSync(resolve(PAGES_DIR, 'ChatPage.tsx'), 'utf8'),
  ...readdirSync(OWNER_DIR).filter(f => /\.tsx?$/.test(f) && !/\.test\./.test(f)).sort()
    .map(f => readFileSync(resolve(OWNER_DIR, f), 'utf8')),
  ...readdirSync(PAGES_DIR).filter(f => /^useChatPage.*\.tsx?$/.test(f) && !/\.test\./.test(f)).sort()
    .map(f => readFileSync(resolve(PAGES_DIR, f), 'utf8')),
].join('\n')
// The dock's measurement and clearance live in one module shared with the
// pane; the page mounts the box.
const DOCK = readFileSync(resolve(__dirname, '../pages/chat/composerDockMetrics.ts'), 'utf8')
// ChatPane (split panes, a crewmate's chat) floats its composer the same way
// (#18279): same hook, same clearance, same dock root.
const CHAT_PANE = readFileSync(resolve(__dirname, '../components/ChatPane.tsx'), 'utf8')
// The pane's code also lives in its owner modules; the "never" pins read them too.
const CHAT_PANE_WITH_OWNERS = [CHAT_PANE, ...[
  'ChatPanePickerMenus.tsx',
  'ChatPaneProps.ts',
  'ChatPaneTranscript.tsx',
  'useChatPaneDraft.ts',
  'useChatPanePickers.ts',
  'useChatPaneSend.ts',
  'useChatPaneTurnControl.ts',
  'useChatPaneUploads.ts',
].map(f => readFileSync(resolve(__dirname, '../components', f), 'utf8'))].join('\n')
const WELCOME_VIEW = readFileSync(resolve(__dirname, '../components/WelcomeView.tsx'), 'utf8')

// The page and pane files on their own, for source-order pins.
const CHAT_PAGE_FILE = readFileSync(resolve(PAGES_DIR, 'ChatPage.tsx'), 'utf8')

const num = (re: RegExp, src: string): number => {
  const m = re.exec(src)
  expect(m, `pattern not found: ${re}`).not.toBeNull()
  return Number(m![1])
}

/** The dock root is ONE box in both layouts:
 *  `className={topDown ? '<top>' : '<bottom>'}` and
 *  `style={topDown ? { <topStyle> } : { <bottomStyle> }}`. */
const dockRoot = (src: string) => {
  const m = /<div ref=\{dockRef\} className=\{topDown \? '([^']*)' : '([^']*)'\} style=\{topDown \? \{ ([^}]*) \} : \{ ([^}]*) \}\} data-testid="composer-dock-root"[^>]*>/.exec(src)
  expect(m, 'the dock root keeps one box with a top-down and a bottom branch').not.toBeNull()
  return { top: m![1], bottom: m![2], topStyle: m![3], bottomStyle: m![4] }
}

describe('composer dock clearance', () => {
  it('pads the scroller by the measured dock height plus the px clearance', () => {
    // The default (bottom) layout is the ternary's `false` branch in both
    // hosts: the dock's strip is bottom padding.
    expect(CHAT_PAGE).toMatch(/scrollerStyle=\{topDown\n\s*\? \{[^\n]*\}\n\s*: \{ paddingBottom: dockH \+ DOCK_CLEARANCE_PX,/)
    expect(CHAT_PANE).toMatch(/scrollerStyle: topDown\n\s*\? \{[^\n]*\}\n\s*: \{ paddingTop: 12 \+ topInset, paddingBottom: dockH \+ DOCK_CLEARANCE_PX,/)
    expect(num(/export const DOCK_CLEARANCE_PX = (\d+)/, DOCK)).toBeGreaterThan(0)
    // One clearance for both hosts: neither re-declares its own number.
    for (const src of [CHAT_PAGE, CHAT_PANE_WITH_OWNERS]) expect(src).not.toMatch(/const DOCK_CLEARANCE_PX =/)
    expect(num(/const TRANSCRIPT_TAIL_SPACER_PX = (\d+)/, CHAT_PAGE)).toBeGreaterThan(0)
  })

  it('measures the dock root from a callback ref through a ResizeObserver', () => {
    expect(dockRoot(CHAT_PAGE).bottom.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'bottom-0']))
    // A callback ref, not a `[]` layout effect: the dock sits inside the pane's
    // conditional branch, so a mount-once effect can run before it exists and
    // never measure. The ref fires on every mount/unmount of the box.
    // The same measurement reads the scroller's reserved scrollbar gutter, so
    // the dock's `right` inset lines its column up with the transcript's and
    // leaves the thumb uncovered — hence `scrollerRef` in the deps.
    const hook = /const dockRef = useCallback\(\(el: HTMLDivElement \| null\) => \{[\s\S]*?if \(!el\) \{ setDockH\(0\); setDockGutter\(0\); return \}[\s\S]*?setDockH\(el\.offsetHeight\)[\s\S]*?setDockGutter\(sc \? Math\.max\(0, sc\.offsetWidth - sc\.clientWidth\) : 0\)[\s\S]*?new ResizeObserver\(measure\)[\s\S]*?ro\.observe\(el\)[\s\S]*?\}, \[scrollerRef\]\)/
    expect(DOCK).toMatch(hook)
    for (const src of [CHAT_PAGE, CHAT_PANE]) {
      expect(src, 'the host takes dockRef from the shared measurement').toMatch(/const \{ inputAreaRef, dockH, dockGutter, dockRef \} = useComposerDockMetrics\(scrollerRef\)/)
      const root = dockRoot(src)
      expect(root.bottom.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'bottom-0']))
      expect(root.bottomStyle).toBe('right: dockGutter')
      // Neither branch puts `dock-inert` on the root (see below).
      for (const cls of [root.top, root.bottom]) expect(cls.split(/\s+/)).not.toContain('dock-inert')
    }
    // `dock-inert` goes on the two inner wrapper boxes, never on the root: the
    // jump pill is a direct child of the root and its wrapper is a full-width
    // pointer-inert strip that `.dock-inert>*` would turn back on, so with the
    // root inert the strip above the composer swallowed clicks and wheel over
    // the rows behind it whenever the pill was showing (#18283 review).
    for (const [src, own] of [[CHAT_PAGE, CHAT_PAGE], [CHAT_PANE, CHAT_PANE_WITH_OWNERS]]) {
      expect(own).not.toMatch(/<div ref=\{dockRef\} className="[^"]*\bdock-inert\b[^"]*"/)
      expect(src).toMatch(/<div ref=\{inputAreaRef\} className="relative z-10 dock-inert">/)
      expect(src).toMatch(/className="[^"]*\bdock-inert\b[^"]*" data-testid="composer-status-stack">/)
    }
    // No opaque bottom fade in either host: the transcript scrolls under the glass.
    for (const src of [CHAT_PAGE, CHAT_PANE_WITH_OWNERS]) expect(src).not.toMatch(/<EdgeFade side="bottom"/)
    for (const src of [CHAT_PAGE, CHAT_PANE_WITH_OWNERS, DOCK]) expect(src).not.toMatch(/useLayoutEffect\(\(\) => \{\s*const el = dockRef\.current/)
  })

  it('states the clearance in px, never in viewport units', () => {
    // A spacer sized in vh/dvh/svh/lvh reads as px to the arithmetic while still
    // shrinking on a phone.
    for (const src of [CHAT_PAGE, CHAT_PANE_WITH_OWNERS, DOCK]) expect(src).not.toMatch(/height:\s*['"]?\d+(\.\d+)?(vh|dvh|svh|lvh)/)
    expect(CHAT_PAGE).toMatch(/<div style=\{\{ height: TRANSCRIPT_TAIL_SPACER_PX \}\} \/>/)
  })

  it('ends the welcome hero above the dock by the same measurement', () => {
    // A margin, not padding under the glass: the cards and the Refresh link are
    // controls, and a label blurred under the composer or the memory-mode chip
    // read as a glitch with an ambiguous tap target (maintainer ruling, #15820).
    // Top-down, the dock floats over the top instead, so the box STARTS below
    // it (the title band plus the same measurement).
    expect(CHAT_PAGE).toMatch(/key="welcome-hero"[\s\S]{0,2500}?style=\{topDown \? \{ marginTop: dockTop \+ dockH \} : \{ marginBottom: dockH \}\}/)
    expect(CHAT_PAGE).not.toMatch(/key="welcome-hero"[\s\S]{0,2500}?style=\{[^\n]*paddingBottom: dockH/)
    expect(CHAT_PAGE).not.toMatch(/key="welcome-hero"[\s\S]{0,2500}?style=\{[^\n]*paddingTop: dockTop/)
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

/**
 * The opt-in top-down layout (Settings > Chat > `topDownLayout`) keeps the same
 * decision with the dock at the other edge: the scroller still runs the full
 * height of the pane, the dock root is still `absolute` over it, now anchored
 * to its TOP edge, and the scroller pays for the covered strip with
 * `padding-top`. Ending the scroller's box at the dock (an in-flow slot above
 * it, with the dock portalled in) is what the decision rules out.
 */
describe('composer dock clearance, top-down layout', () => {
  it('anchors the same dock root to the top edge and pays for it with top padding', () => {
    const page = dockRoot(CHAT_PAGE)
    expect(page.top.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'pointer-events-none']))
    expect(page.top.split(/\s+/)).not.toContain('bottom-0')
    expect(page.topStyle).toBe('right: dockGutter, top: dockTop')
    // The band under the overlay title row: the same condition that mounts the
    // h-16 header spacer in the bottom layout, which top-down turns off, so the
    // band is paid once, by the spacer or by the padding.
    expect(CHAT_PAGE).toMatch(/const dockTop = topDown && !\(titleInTopbar && !workerBreadcrumb\) \? 64 : 0/)
    expect(CHAT_PAGE).toMatch(/headerSpacer=\{\(!titleInTopbar \|\| workerBreadcrumb\) && !topDown\}/)
    expect(CHAT_PAGE).toMatch(/scrollerStyle=\{topDown\n\s*\? \{ paddingTop: dockTop \+ dockH \+ DOCK_CLEARANCE_PX, paddingBottom: DOCK_CLEARANCE_PX,/)
    expect(CHAT_PAGE).toMatch(/topDown=\{topDown\} dockTop=\{dockTop\}/)

    const pane = dockRoot(CHAT_PANE)
    expect(pane.top.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'pointer-events-none']))
    expect(pane.top.split(/\s+/)).not.toContain('bottom-0')
    // The pane's title row is in flow above its scroller, so the root's top is
    // the scroller's measured top in the pane, plus the host band. The scroller
    // pays that band once, inside its own box.
    expect(pane.topStyle).toBe('right: dockGutter, top: scrollerTop + topInset')
    expect(CHAT_PANE).toMatch(/const measure = \(\) => setScrollerTop\(sc\.offsetTop\)/)
    expect(CHAT_PANE).toMatch(/scrollerStyle: topDown\n\s*\? \{ paddingTop: topInset \+ dockH \+ DOCK_CLEARANCE_PX, paddingBottom: DOCK_CLEARANCE_PX, minHeight: 0 \}/)
  })

  it('reserves no in-flow slot for the dock and keeps it after the scroller in DOM order', () => {
    for (const src of [CHAT_PAGE, CHAT_PANE_WITH_OWNERS]) {
      expect(src).not.toMatch(/composer-dock-top-slot|chat-pane-top-dock-slot|topDockSlot|placeDock\(|placeComposer\(/)
    }
    for (const src of [CHAT_PAGE_FILE, CHAT_PANE]) expect(src).not.toMatch(/createPortal/)
    // After the scroller, so the dock (no z-index) paints over it in both layouts.
    expect(CHAT_PAGE_FILE.indexOf('<ChatTranscriptView')).toBeGreaterThan(-1)
    expect(CHAT_PAGE_FILE.indexOf('<ComposerDockView')).toBeGreaterThan(CHAT_PAGE_FILE.indexOf('<ChatTranscriptView'))
    expect(CHAT_PANE.indexOf('<ChatMessageList')).toBeGreaterThan(-1)
    expect(CHAT_PANE.indexOf('<div ref={dockRef}')).toBeGreaterThan(CHAT_PANE.indexOf('<ChatMessageList'))
  })

  it('keeps a crewmate pane on the bottom layout', () => {
    // useUnreadLanding (crewmate only) reads the bottom geometry, the plain
    // `pane:<slot>` anchor and chronological row indexes.
    expect(CHAT_PANE).toMatch(/const topDown = chatConfig\.topDownLayout && !isMobile && !crewmate\n/)
  })
})
