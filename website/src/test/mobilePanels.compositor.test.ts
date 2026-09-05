/**
 * The OTHER two mobile panels ride the same compositor machinery as the
 * sessions drawer — pinned as SOURCE contracts, the way App.test.tsx pins the
 * drawer's own wrapper: these are pairings ("compositor slide" ⟷ "no framer
 * transform / no projection under it"), and one half regressing alone
 * reintroduces a bug the other half was built around.
 *
 *  LEFT (App.tsx mobile nav drawer):
 *   - the panel is a plain <nav> whose slide runs via animateDrawer — framer
 *     must not own a competing transform on it;
 *   - NavItem drops `layout` on mobile: a projection node under a
 *     compositor-driven ancestor compounds a corrective offset (the ChatSidebar
 *     rows measured >4,000px of it);
 *   - the scrim's opacity is animated in lockstep by animateDrawer, not by a
 *     framer fade of its own.
 *
 *  RIGHT (ChatPage inline side panel on mobile):
 *   - the mobile branch must NEVER animate `width` — a layout animation the
 *     compositor cannot take, which re-laid-out the squeezed chat pane every
 *     frame (the original 400ms width reveal);
 *   - it slides as a fixed overlay via sideOverlayX / animateDrawer;
 *   - keep-alive survives: a closed-but-alive panel stays mounted display:none
 *     so a live app tab's iframe is not torn down by the overlay conversion.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const app = readFileSync(resolve(__dirname, '../App.tsx'), 'utf8')
// The page composition layer owns state and gestures; the extracted view owns
// the DOM bindings. Keep the source contract spanning both rather than making
// either module pretend to own the whole mobile-panel interaction.
const chatController = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')
const chatView = readFileSync(resolve(__dirname, '../pages/chat/ChatPageView.tsx'), 'utf8')
const chat = `${chatController}\n${chatView}`
const hook = readFileSync(resolve(__dirname, '../hooks/useDrawerSwipe.ts'), 'utf8')

describe('mobile nav drawer (left) — compositor pairing', () => {
  const drawer = app.slice(app.indexOf('key="mobile-nav-drawer"'), app.indexOf('key="mobile-nav-drawer"') + 900)

  it('slides a plain <nav>, not a framer-owned motion.nav', () => {
    expect(app.indexOf('key="mobile-nav-drawer"')).toBeGreaterThan(0)
    // The element framer animates is the element the compositor cannot have:
    // two writers to one transform is the judder the sessions drawer already
    // debugged. The ref is what animateDrawer drives.
    expect(drawer).toContain('ref={mobileNavPanelRef}')
    expect(drawer).not.toContain('<motion.nav')
    expect(drawer).not.toContain('animate={{')
  })

  it('registers panel+scrim with the drawer runtime', () => {    expect(app).toContain('registerDrawerTargets(mobileNavX')
    expect(app).toMatch(/panel: \(\) => mobileNavPanelRef\.current/)
    expect(app).toMatch(/scrim: \(\) => mobileNavScrimRef\.current/)
  })

  it('NavItem drops layout projection on mobile', () => {
    expect(app).toContain("layout={isMobileRow ? undefined : 'position'}")
  })

  it('the scrim has no framer fade of its own (animateDrawer owns it)', () => {
    const scrim = app.slice(app.indexOf('data-testid="nav-backdrop"') - 400, app.indexOf('data-testid="nav-backdrop"') + 400)
    expect(scrim).toContain('ref={mobileNavScrimRef}')
    expect(scrim).not.toContain('initial={{ opacity')
  })

  it('scrim is decorative (aria-hidden) and Escape is the keyboard dismissal', () => {
    // The scrim's click-to-dismiss is a pointer convenience. It must stay out
    // of the tab order and the accessibility tree (a focusable full-screen
    // scrim is a giant tab stop), so the keyboard path is an Escape handler
    // gated on the drawer being open — both halves pinned here.
    const scrim = app.slice(app.indexOf('data-testid="nav-backdrop"') - 400, app.indexOf('data-testid="nav-backdrop"') + 400)
    expect(scrim).toContain('aria-hidden="true"')
    const esc = app.slice(app.indexOf("mobileNavPhase !== 'open'") - 200, app.indexOf("mobileNavPhase !== 'open'") + 400)
    expect(esc).toContain("e.key === 'Escape'")
    expect(esc).toContain('closeMobileNavDrawer()')
  })
})

describe('inline side panel (right) — mobile overlay pairing', () => {
  const start = chat.indexOf('key="side-panel-inline"')
  // The mount predicate (with the keep-alive arm) sits a few hundred chars ABOVE the key.
  const block = chat.slice(start - 1400, start + 2400)

  it('mobile branch never animates width (layout animation)', () => {
    expect(start).toBeGreaterThan(0)
    // The width tween survives ONLY behind the desktop/embed ternary arm.
    expect(block).toContain('initial={isMobile ? false : { width: 0 }}')
    expect(block).toContain('animate={isMobile ? undefined : {')
  })

  it('mobile branch is a fixed overlay driven by the drawer runtime', () => {
    expect(block).toContain('ref={isMobile ? sideOverlayPanelRef : undefined}')
    expect(chat).toContain('registerDrawerTargets(sideOverlayX')
    // BOUND, not a one-shot `.get()` read: this panel has a drag gesture, and a
    // drag writes the MotionValue directly, so only a live binding paints those
    // frames. The compositor settle still runs through the registered element —
    // `takeOverDrawer` / `publishArrival` are written for a framer-bound panel
    // (it is the sessions drawer's own shape).
    expect(block).toContain('{ x: sideOverlayX }')
    expect(block).not.toContain('translate3d(${sideOverlayX.get()}px')
  })

  it('keep-alive: a closed-but-alive panel stays mounted display:none', () => {
    // The mount predicate keeps a hidden live-app panel in the tree…
    expect(block).toContain('|| (shouldMountSidePanel(')
    // …and the closed phase renders it invisible rather than unmounting it.
    expect(block).toMatch(/sideOverlayPhase === 'closed'\s*\n?\s*\? \{ display: 'none' \}/)
  })
})

describe('both mobile chat panels — one element, two gestures', () => {
  /** Each `useDrawerSwipe(chatContainerRef, …)` call and its options. */
  const bindings = [...chat.matchAll(/useDrawerSwipe\(chatContainerRef, \{([\s\S]*?)\n {2}\}\)/g)]
    .map(m => m[1])

  it('binds one gesture per panel, and names the right one for the right side', () => {
    expect(bindings).toHaveLength(2)
    // Left is the default and stays unspelled; right must say so, or it would
    // run the left panel's signs against the right panel's offset.
    const right = bindings.filter(b => b.includes("side: 'right'"))
    expect(right).toHaveLength(1)
    expect(right[0]).toContain('x: sideOverlayX')
    expect(bindings.find(b => b.includes('x: drawerX'))).not.toContain("side: 'right'")
  })

  it('each gesture is gated on the OTHER panel not being OPEN', () => {
    // Direction separates the two while both are closed, but not once one is
    // open: that panel's closing drag is the other's opening drag. Losing
    // either half of this cross-gate makes a dismissal open the opposite panel
    // — see drawerSwipeTwoPanels.test.ts for the behaviour itself.
    //
    // The predicate is `!== 'open'` rather than `=== 'closed'` on purpose. The
    // hazard lasts exactly as long as the sibling is open; a gate that waited
    // for `'closed'` also stayed shut through the whole slide out, so a swipe
    // dismissing one panel could not be followed straight away by a swipe
    // revealing the other. `'closing'` must therefore pass.
    const left = bindings.find(b => b.includes('x: drawerX')) as string
    const right = bindings.find(b => b.includes('x: sideOverlayX')) as string
    expect(left).toMatch(/enabled:.*sideOverlayPhase !== 'open'/)
    expect(right).toMatch(/enabled:.*drawerPhase !== 'open'/)
  })

  it('each gesture reports its release at COMMIT time, not only on arrival', () => {
    // `onSettle` fires from the settle animation's completion callback, so it
    // cannot open the sibling's gate any earlier than the animation ends. The
    // handoff above depends on `onCommit` carrying the decision immediately and
    // parking the phase at 'closing' — which keeps the panel mounted for the
    // rest of its travel, unlike 'closed'.
    //
    // Matched against the binding with COMMENTS STRIPPED, and against the setter
    // call rather than a bare quoted word: the prose in these options mentions
    // both phase names, so a looser pattern is satisfied by the explanation
    // instead of the code it describes.
    const code = (b: string) => b.replace(/\/\/[^\n]*/g, '')
    for (const b of bindings) {
      expect(code(b)).toMatch(/onCommit:/)
      expect(code(b)).toMatch(/set\w*Phase\('closing'\)/)
    }
  })

  it('the right gesture stays out of surfaces that would undo it', () => {
    const right = bindings.find(b => b.includes('x: sideOverlayX')) as string
    // The actbar column owns the panel on desktop, and the find pane holds the
    // dock exclusively — in both the store's mount predicate refuses to keep
    // the panel open, so a committed drag would be reverted on the next render.
    expect(right).toMatch(/enabled:.*!activitySlot/)
    expect(right).toMatch(/enabled:.*!search\.isOpen/)
  })
})

describe('the sessions drawer travels its own width', () => {  // The drawer is narrower than the screen on purpose. Its width and its travel
  // therefore have to come from ONE number: spelling the width as a
  // `max-w-[calc(100vw-2.5rem)]` class while the travel read `innerWidth` is how
  // they drifted 40px apart, which put the panel fully offscreen at ~90% of the
  // slide and spent the settle's whole deceleration on an invisible panel.

  it('derives the panel width from the uncovered-strip constant', () => {
    expect(chat).toMatch(/const DRAWER_UNCOVERED_PX = \d+/)
    // Scoped to the call site, not the whole file: the constant's own comment
    // names the class it replaced, and a file-wide match would read that prose
    // as live code.
    const call = chat.slice(chat.indexOf('<OverlayDrawer open='), chat.indexOf('<OverlayDrawer open=') + 900)
    expect(call).toContain('width={isMobile ? Math.max(0, winW - DRAWER_UNCOVERED_PX)')
    // The class that used to state the same width a second way is gone.
    expect(call).not.toContain('max-w-[calc(')
  })

  it('derives the travel from that same constant', () => {
    expect(chat).toMatch(/const drawerTravel = useCallback\(\s*\(\) => Math\.max\(0, \(window\.innerWidth \|\| 0\) - DRAWER_UNCOVERED_PX \+ safeAreaLeft\(\)\)/)
  })

  it('counts the safe-area inset the panel starts at', () => {
    // `left-safe` puts the panel an inset in, so clearing the screen costs its
    // width PLUS that inset. Leaving it out parks the panel with an inset-wide
    // strip still visible — the same defect as undershooting the width, but only
    // reachable on a notched phone in landscape.
    expect(chat).toContain('DRAWER_UNCOVERED_PX + safeAreaLeft()')
  })

  it('measures the inset from the same expression the panels are pinned by', () => {
    // Pinned HERE rather than behaviourally: jsdom does not implement `env()`, so a
    // probe reading `env(safe-area-inset-left)` and one reading `0` both measure 0
    // and no assertion in a jsdom test can tell them apart. The value being the
    // panels' OWN inset is the whole point, so it is checked against the source.
    expect(hook).toContain("s.left = 'env(safe-area-inset-left)'")
    // …and against what the panels actually use, so the two cannot drift apart:
    // a Tailwind `left-safe` emits `left: env(safe-area-inset-left)`.
    expect(app).toContain('left-safe')
  })

  it('hands that one travel to every consumer of the offset', () => {
    // Five of them, and a miss in any one desynchronises the panel's edge, the
    // scrim's zero, the drag's clamp or the commit point.
    const start = chat.indexOf('registerDrawerTargets(drawerX')
    expect(chat.slice(start, start + 320)).toContain('travel: drawerTravel')
    expect(chat).toContain('x / Math.max(1, drawerTravel())')
    expect(chat).toContain('drawerX.set(-drawerTravel())')
    expect(chat).toContain('animateDrawer(drawerX, -drawerTravel()')
    const left = [...chat.matchAll(/useDrawerSwipe\(chatContainerRef, \{([\s\S]*?)\n {2}\}\)/g)]
      .map(m => m[1]).find(b => b.includes('x: drawerX')) as string
    expect(left).toContain('travel: drawerTravel')
  })
})

describe('the nav drawer travels its own width too', () => {
  // Same defect class as the sessions drawer, at a smaller scale: its travel was
  // 240 against ~231 of real clearance, so it sat fully offscreen at 96% of the
  // slide and 211ms of a 450ms dismissal moved nothing.

  it('derives the travel from the width and inset it actually has', () => {
    expect(app).toMatch(/const MOBILE_NAV_WIDTH = \d+/)
    expect(app).toMatch(/const MOBILE_NAV_INSET = \d+/)
    expect(app).toMatch(/const mobileNavTravel = \(\) =>\s*MOBILE_NAV_WIDTH \+ MOBILE_NAV_INSET \+ \d+ \+ safeAreaLeft\(\)/)
    // The rendered width comes from the same constant, so the two cannot drift.
    expect(app).toContain('style={{ width: MOBILE_NAV_WIDTH,')
    // Live, not a module constant: the safe-area inset is only knowable at
    // runtime and changes with orientation, so every consumer calls it.
    for (const site of [
      'travel: mobileNavTravel,',
      'mobileNavX.set(-mobileNavTravel())',
      'animateDrawer(mobileNavX, -mobileNavTravel()',
    ]) expect(app).toContain(site)
  })

  it('clears the screen with only a hair of slack', () => {
    const num = (re: RegExp) => Number(re.exec(app)![1])
    const width = num(/const MOBILE_NAV_WIDTH = (\d+)/)
    const inset = num(/const MOBILE_NAV_INSET = (\d+)/)
    const slack = num(/MOBILE_NAV_WIDTH \+ MOBILE_NAV_INSET \+ (\d+) \+ safeAreaLeft\(\)/)
    // Enough to cover the 1px border and the shadow's spread…
    expect(slack).toBeGreaterThan(0)
    // …and not the 9px that made the tail invisible. Stated against the travel so
    // it scales if the panel is ever resized.
    expect(slack / (width + inset)).toBeLessThan(0.03)
  })
})

describe('a panel with a gesture is bound LIVE to its offset', () => {  // The defect this pins, observed on a real device: the nav panel read
  // `mobileNavX.get()` into an inline transform at render time. A MotionValue
  // deliberately does not re-render React, so a drag wrote the value every frame
  // while the DOM moved only on the single re-render the gesture's own
  // setDragging causes — the panel came out "a little" and then completed only
  // on release, when the settle took over. Correct while the tap was its only
  // mover; wrong the moment it gained a gesture.
  it('binds the nav panel and its scrim to the MotionValue, not a snapshot', () => {
    expect(app).toMatch(/<motion\.nav[\s\S]{0,200}?style=\{\{ width: MOBILE_NAV_WIDTH, x: mobileNavX \}\}/)
    expect(app).toMatch(/<motion\.div[\s\S]{0,200}?ref=\{mobileNavScrimRef\}[\s\S]{0,200}?style=\{\{ opacity: mobileNavScrim \}\}/)
    // A render-time read of the value is the bug, on either element.
    expect(app).not.toContain('mobileNavX.get()}px')
    expect(app).not.toMatch(/ref=\{mobileNavScrimRef\}[\s\S]{0,200}?style=\{\{ opacity: 0 \}\}/)
  })

  it('derives the scrim from the same travel the panel uses', () => {
    // Divided by the drawer's OWN travel, so the dim reaches 0 exactly as the
    // panel clears the edge rather than at some fraction of the viewport.
    expect(app).toMatch(/useTransform\(mobileNavX, x =>\s*\n?\s*Math\.max\(0, Math\.min\(1, 1 \+ x \/ Math\.max\(1, mobileNavTravel\(\)\)\)\)\)/)
  })

  it('keeps every gesture-driven panel on the same rule', () => {
    // The sessions drawer and the right overlay were already live-bound; the
    // rule is now uniform, so a future panel copying any of them gets it right.
    expect(chat).toMatch(/slideX=\{isMobile \? drawerX : undefined\}/)
    // The right overlay binds through a ternary (it is display:none while closed,
    // to keep an iframe alive), so the live binding is the branch, not the whole
    // style prop.
    expect(chat).toMatch(/\{ x: sideOverlayX \}\)/)
  })

  it('makes every in-shell gesture consumer declare its claim', () => {
    // The invariant, not a headcount: any consumer that binds its OWN gesture
    // inside the shell has to claim its sides, or it and the app-wide instance
    // arm on one touch. App.tsx is the shell instance itself, so it declares
    // nothing — everyone else must. A new page that adds a drawer gesture and
    // forgets the attribute trips this rather than shipping a fight.
    const consumers = { 'App.tsx': app, 'ChatPage.tsx': chat }
    for (const [name, src] of Object.entries(consumers)) {
      if (!/useDrawerSwipe\(/.test(src)) continue
      const isShellInstance = /useDrawerSwipe\(shellRef, \{/.test(src)
      if (isShellInstance) continue
      expect(src, `${name} binds a drawer gesture without data-owns-swipe`)
        .toContain('data-owns-swipe')
    }
  })

  it('withholds the claim exactly where the page binds nothing', () => {
    // An embedded ChatPage renders INSIDE the shell (artifact companion, Papyrus
    // co-author, app SDK panel) at full width on mobile, and both of its own
    // instances are gated on `!embedded`. A claim there suppressed the nav swipe
    // while owning nothing — a dead gesture on the whole screen, and a direct
    // defeat of the fail-open default, since the one page that declares was
    // declaring past its own ownership.
    expect(chat).toContain("data-owns-swipe={embedded ? undefined : 'left right'}")
    expect(chat).not.toContain('data-owns-swipe="left right"')
    // The gates the claim now mirrors.
    expect((chat.match(/enabled: isMobile && !embedded/g) ?? []).length).toBe(2)
  })

  it('documents the override, so a page author can find it', () => {
    // A cross-component DOM contract that lives only in code comments is one a
    // new page never learns about. page-layout.md is where the sibling mobile
    // and gesture rules already live.
    const doc = readFileSync(resolve(__dirname, '../../docs/page-layout.md'), 'utf8')
    expect(doc).toContain('data-owns-swipe')
    // The two halves that are silent when wrong: which element carries it, and
    // that a panel gaining a gesture must stop serializing its offset.
    expect(doc).toMatch(/not including/)
    expect(doc).toMatch(/bound LIVE|bind it instead|Bind it instead/i)
    // …and the widget half, which is the one no attribute expresses.
    expect(doc).toContain('touch-action: none')
  })
})

describe('the nav drawer is swipeable on every page', () => {
  it('binds the gesture on the shell, which the scrim and panel are inside too', () => {
    // NOT <main>: the panel and scrim are `fixed` siblings outside it, so a
    // gesture rooted there can open the drawer but never sees the touch that
    // should close it.
    expect(app).toMatch(/ref=\{shellRef\}\s*\n\s*data-testid="dashboard-shell"/)
    expect(app).toMatch(/useDrawerSwipe\(shellRef, \{/)
    expect(app).not.toMatch(/<main ref=/)
    // Mounted for a gesture WITHOUT the tap animation, or the settle would race
    // the finger for the same value.
    expect(app).toContain('onGestureOpen: beginMobileNavDrag')
    expect(app).not.toMatch(/useDrawerSwipe\(shellRef, \{[^}]*onGestureOpen: openMobileNav/)
  })

  it('lets the chat page keep both of its own sides', () => {
    // The claim has to sit on the SAME element the page binds its own gestures
    // to: one line lower and the page suppresses itself, one line higher and the
    // app-wide instance never sees it. Pinned as adjacency because no runtime
    // assertion can observe which element an attribute was authored on.
    expect(chat).toMatch(/ref=\{chatContainerRef\}[\s\S]{0,1600}?data-owns-swipe=/)
    for (const bound of ['useDrawerSwipe(chatContainerRef, {']) expect(chat).toContain(bound)
    // Both instances still name chatContainerRef, so the claimed sides and the
    // bound sides cannot drift apart.
    expect((chat.match(/useDrawerSwipe\(chatContainerRef, \{/g) ?? []).length).toBe(2)
  })
})
