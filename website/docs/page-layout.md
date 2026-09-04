# Page Layout Guide

Every dashboard page follows one layout pattern. Copy the skeleton below rather
than inventing a custom layout: a page that diverges costs a reader the
orientation cues (title block position, scroll container, section rhythm) that
every other page gives them for free.

Components come from [`src/components/ui.tsx`](../src/components/ui.tsx); the
conventions around them (a11y, data fetching, typography) live in
[frontend-conventions](frontend-conventions.md).

## Page skeleton

```tsx
<>
  <PageHeader title="PageName" subtitle="Short description" />
  <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
    {/* optional StatCard row, then Cards with tables/forms */}
  </div>
</>
```

`PageHeader` owns its own `px-4 md:px-6 pt-2 pb-3` — the SAME horizontal gutter as the
content container below it, so the title shares a left edge with the cards and rows it
labels. Keep them equal: a header that drifts off the container's gutter insets the
title from its own content, and the top bar above is a separate layer that does not
have to match (see "The title belongs to the content column" below).
`overflow-y-auto flex-1 min-h-0` is what makes the content the scrolling region while
the header stays put: the shell is height-locked, so without `min-h-0` the flex child
refuses to shrink and the whole page scrolls instead.

`PageHeader` also takes an `actions` node, rendered right-aligned on the title
row. Put page-level buttons there rather than in the first `Card`.

### Read every class here narrow-first

**The unprefixed value is the PHONE's. `md:` adds the desktop.** `px-4 md:px-6` is a
gutter that starts at 16px and widens; `p-5 max-md:px-4` is the same intent written
backwards. Both render identically, and only the first one is maintainable.

This is not a style preference. Tailwind is built narrow-first, so a rule whose
unprefixed value is the desktop one forces every narrow fix to claw the width back
with `max-md:` and, usually, a negative margin hand-pinned to a number owned in
another file. That pairing is invisible when it breaks: the pane slides past the
screen edge, and on a script that breaks between characters nothing overflows, so no
scroll assertion sees it. Writing the phone value unprefixed removes the second
number instead of documenting how to keep it in sync.

So the sections below are not exceptions to a desktop standard. They are the
baseline, and the desktop is what `md:` adds to it.

### The narrow-viewport inset budget

These are **recommendations for the narrow branch only** — nothing here changes a
page from `md` up, and `AUTOSDE.yaml` does not enforce a gutter value. A page that
keeps one gutter at every width is conformant.

**Recommended below `md`: a 16px page gutter (`px-4`) and an 8px `Card` horizontal
inset (`px-2 … md:px-5`)**, serving a budget of **no more than ~25px of stacked inset
before body text** (16 + a 1px border + 8). 16px is the screen margin Material, Apple
HIG, Fluent, Carbon, Polaris, Primer and Atlassian all converge on, and two surfaces
here already ran it before this was written down (`SidePanelLayout`, the Knowledge
page), so the default is the number the rest of the industry and this app had already
picked rather than a new one.

Padding stacks, and the eye reads the SUM. The gutter is 24px from `md` up, where
it is comfortable. At 390px the same 24px plus a 20px card inset put card text
44px from the screen — 88px of a 390px screen, **22.6%**, spent on nothing, and
the line that pays is the longest content in the card.

Which layer yields is not arbitrary:

- **Both layers yield, the one against a DRAWN border yields less.** `Card` goes to
  8px horizontally — narrowed, never flushed. Its inset is often the only gutter its
  own rows have, so flushing a card to 0 is still wrong (see below). The VERTICAL
  inset stays 20px: horizontal is the axis a phone cannot spare, and changing the
  vertical would move every card's height.
- **The layer against the SCREEN EDGE yields more.** A phone bezel is not a drawn
  line, so a gutter narrower than the 24px desktop one reads as intentional rather
  than cramped. 16px is where that stops being true in the other direction: this
  was tried at 8px and then at 12px, and 8px read as content pressed against the
  bezel rather than as a deliberately dense page. 16px keeps most of the width the
  narrow gutter buys while leaving the page visibly inset.

This MATCHES the 16px screen margin that Material, Apple HIG, Fluent, Carbon,
Polaris, Primer and Atlassian all converge on. Their 16px is content-to-edge for
content that is **not already inside a bordered container**: a Material list item at
a 16px margin puts its text at 16px, not at 36px. So this app hits that number
exactly for content ON the gutter -- a heading, a row, a tab strip -- and a `Card`
then charges its own 8px on top, putting card text at 25px. The chat transcript runs
the SAME 16px -- its message row and its composer are both `px-4` -- so a page's
uncontained content and the agent's own text sit on one vertical line across the whole
app, and only a `Card` steps inside it. That a card cannot also land on 16px under one
gutter is arithmetic, not an oversight: the card inset absorbs the difference, which is
one more reason to reach for a `Card` less often on a phone (see below).

At 390px the card goes from 342px wide (the 24px gutter) to 358px and its text line
from 300px to 340px, **+13.3%**; at 320px the card goes from 272px to 288px and the
text line from 230px to 270px, **+17.4%**. Nothing changes from `md` up.

That is the comparison against the DESKTOP gutter. Against the 8px narrow gutter and
10px card inset this replaced, the same card is 16px narrower and its text line 12px
shorter (390px: 374 -> 358 wide, 352 -> 340 of text). Measured, not derived. The 12px
buys a 16px screen margin on every uncontained surface and one shared left edge; a
page that would rather have the width should drop the `Card`, not re-cut the gutter.
Both sets of figures follow
from the box arithmetic -- viewport minus two gutters, then minus two 1px borders and
two card insets -- and the text-line figures subtract the borders because a border is
opaque to text the same way padding is.

The one exception is `OnboardingChapterShell`, a full-page surface with its own
`sm:px-10` scale rather than a `PageHeader` + container page.

### The title belongs to the content column, not to the chrome

Going down the left of a phone screen there are two layers, and they are allowed to
differ:

| | narrow | made of |
|---|---|---|
| **content column** — `PageHeader`, page rows, `Card` boxes | **16px** | the page gutter, `px-4` |
| `Card` body text | 25px | 16 + 1px border + the `Card`'s own 8 |
| top bar icon BOX (chrome) | 16px | header `pl-2` (8) + each icon button's own 8 |
| top bar hamburger INK | 16px | that 16px box, less a 2.5px optical correction, plus `Menu`'s own 2.5px of empty box |

The title shares the container's 16px so it sits directly above the left edge of the
cards and rows it labels. That is the rule, and it is what decides the number: the
title follows its CONTENT, never the chrome above it. An earlier round tried the
opposite — moving the header out to meet the top bar — and it read worse, because the
title then sat inside the very cards beneath it.

At 16px the chrome happens to land on the same line, and that is a consequence rather
than the reason. The top bar's icon BOXES are header inset plus each icon button's own
8px, so an 8px header inset puts them at 16px: the hamburger, the page title, the
chat session-list toggle and every card's left edge become one vertical line. Only the
LEFT cluster is tuned this way — `.tb-right` carries a padding/negative-margin pair
that keeps the notification badge's 4px overhang from being clipped, and re-tuning it
needs a real WebKit check rather than a local one. Two things make this line easy to
break silently: a mobile-only `px-2` on the left cluster once stacked on the header's
own inset and pushed the hamburger out past the page's own edge, and the glyph position
is never the container's `className` — measure the rendered glyph with
`getBoundingClientRect`, not the class.

**A correctly placed box does not mean a correctly placed glyph.** An icon's artwork
need not fill its own viewBox, and the eye sees the INK, not the box. `Menu` is the one
icon here that does not fill it: lucide draws its three rules from `x=4` in a 24-unit
viewBox and the round cap reaches half a stroke further, leaving 3 units — at `size={20}`
that is 3 × 20/24 = **2.5px** — empty on the left. Measured at 390px, its box sat
correctly at 16px while the visible glyph drew at 18.5px, reading as indented against a
card border directly beneath it. It carries a `-translate-x-[2.5px]` correction so the
ink lands at 16px; a transform rather than a margin, so the box, the hit target and the
hover pill all stay on the 8px grid and no sibling in the cluster shifts.

The correction is per-icon and most icons need none — the chat session-list toggle's
`MessageSquare` starts at `x=2`, i.e. 0.67px at `size={16}`, which is already on the
line. Do not generalise this into one shared offset. Note also what the correction
trades: ink now agrees with hard edges (a card border, a divider) and sits ~2px left of
the page TITLE's ink, because text carries its own left side bearing — 2px for `N` at
24px bold. Two things cannot both be true at once, and hard edges won: a border is a
crisp line the eye measures against, while a letter's bearing varies per glyph and per
platform font.

Chat is on this line too, not beside it: the transcript's message row and the composer
are `px-4` with no responsive variant. So the hamburger glyph, the page title, a page
row, a card's left edge and the agent's own text all start at 16px, and a `Card`'s body
text is the one thing that steps inside (25px). Chat is where a phone user spends most
of their time, which is why it is the surface the rest is lined up with rather than the
other way round.

`src/test/narrowFirstBaseline.test.ts` pins the header to the container gutter the
skeleton above documents, and separately pins the top bar's left cluster against the
redundant inset coming back.

### If you write a shared primitive, a breakpoint-scoped base padding is a trap

This one is for primitive authors rather than page authors, and it cost this repo a
silent desktop regression before it was written down.

`twMerge` only collapses classes that collide at the **same** breakpoint. So the
moment a primitive spells its base inset with a prefix — `md:px-5` — a caller's
plain `p-3` no longer displaces it. The two sit side by side, the caller gets its
12px on a phone, and from `md` up the primitive's 20px quietly wins. The call site
reads as 12px everywhere and is not.

Making every caller spell both halves (`p-3 md:p-3`) does close it, but it is the
wrong shape twice over: it is a permanent obligation on every future caller, and any
guard for it has to be lexical, so a computed `className={cond ? 'p-3' : ''}` or a
class list held in a module const walks straight past.

What `Card` does instead: if the incoming `className` names a padding on an axis,
the base inset for THAT axis is dropped rather than merged, decided from the final
string at render time. The caller owns the axis it asked for, at every width, and no
call site has to know the trap exists. `src/test/cardInsetYield.test.tsx` pins it by
rendering, including the computed-`className` case.

Any new primitive that pairs a `md:`-prefixed base padding with `twMerge` re-opens
the same hole, so either yield the axis the same way or keep the base unprefixed.
Stated honestly: `Card` is currently the ONLY primitive in `ui.tsx` with a
breakpoint-scoped base padding — `Btn`, `Input`, `StatCard` and `Chip` are all
unprefixed — so this note has no other instance to fix today. It is here because the
failure is silent and desktop-only, which is exactly the kind a reader will not
re-derive when they reach for `md:px-*` in a new primitive.

### Other narrow-viewport recommendations

Also recommendations, not gates. Each earned its place by breaking on a real screen,
and each carries the measurement that settled it — reach for the measurement before
arguing with the rule.

**A collapsed side rail becomes a horizontal bar across the TOP, never a thin vertical
strip.** Horizontal is the one axis a phone cannot spare; vertical it can. A 44px strip
overflows nothing, so it looks fixed while the reading column still pays for it.

**Hiding is not collapsing.** A control removed below `md` needs an entry point at that
width — an overflow menu, a drawer, a disclosure. A pane that hides the only host of the
phase-advance controls leaves the phone user unable to advance the phase at all.

**Gate on the constraint, not the viewport.** When a pane can be narrow at any viewport
(a split, a resizable rail, an embedded panel), measure the PANE with a `ResizeObserver`
rather than calling `useIsMobile()`. A 1280px window can hold a 200px pane.

**A tabbed shell's pane needs its own top inset once the header goes away — and it
must be the only one.** `SidePanelLayout` drops the desktop header block below `md` —
the block whose `pb-3` put 12px between a tab's title and its content — and replaces it
with a pill strip that ends in a drawn `border-b`. The pane kept no inset of its own, so
a tab whose first element is a `Card` or a `StatCard` rendered that element's own border
ON the divider: two lines touching, measured at a 0px gap on four of Agent Capabilities'
seven tabs and on seven of Developer's eight renderable ones at 390px. The pane carries
`pt-3` on the narrow branch only — desktop must stay at 0 or the two insets stack.

That inset is shared by all three pages built on the shell (Agent Capabilities,
Developer, Settings), which makes the second half of the rule as load-bearing as the
first: **a tab must not add a top margin to its own first element.** Doing so stacks on
the pane and lands that tab 28px down while its siblings sit at 12px — the inconsistency
reads as sloppiness precisely because the tabs are one keystroke apart. Two shapes, and
the difference is whether the heading can ever have a sibling above it:

- **A heading at the tab's root** (`SkillsTab`, `SteeringTab`) drops the margin outright.
  Do NOT reach for `first:mt-0` here: `SkillsTab` renders `PendingSkillsPanel` above the
  heading, and that panel returns `null` when nothing is pending — so the heading moves in
  and out of `:first-child` with the pending count, and a positional rule would make the
  gap depend on it. (A conditionally rendered `Modal` does NOT have this effect: it
  `createPortal`s to `document.body` and never occupies a sibling slot.)
- **A heading that repeats within one tab** (`SettingsSection`, used many times per
  Settings tab; `LocalStorageDebug`'s section headings) keeps `mt-4`, because the gap
  between two sections is real, and pairs it with `first:mt-0`. The fragment adds no DOM
  node, so every section header is a sibling in one parent and only the leading one
  matches — and when a tab renders something of its own above the first section, the
  header stops being first and correctly keeps the margin.

Measured at 390px with `website/scripts/capture-side-panel-pane-inset.mjs`, which reports
the divider→first-in-flow-box distance per tab: all 31 renderable tabs across the three
pages now read 12px. Residual differences in where the first *pixel* lands (21px on
Connections, on Developer > System, on Settings > Instances) are a control's own internal
padding — a sub-tab's or a segmented button's tap target — not stacked page padding, and
tightening those would shrink a touch target.

**An unbounded action cluster leaves the text row; it does not shrink it.** A row of
actions whose count depends on state (enabled, updatable, uninstallable) and that carries
`shrink-0` takes its natural width, and the text column gets the remainder — measured at
34px on a 390px screen, and 0px at 320px. Move the cluster to its own row below the text.

**A per-character-breaking script collapses instead of overflowing, so overflow metrics
cannot see it.** CJK text reaches `scrollWidth == clientWidth` while wrapping to one or
two characters per line. Judge a reading column by its WIDTH, not by whether anything
overflowed.

**Two coupled numbers must be pinned by a test.** A negative margin that cancels an inset
(`-mx-2 md:mx-0` against `Card`'s own `px-2`), or a pull-back sized to a tile's width plus a
gap, is ONE number written twice. Changing one alone misaligns silently — nothing
overflows, so only a test that asserts the pair catches it.

**An icon alone cannot carry a state-changing action.** `aria-label` fixes the screen
reader, not the sighted user, who is left guessing what a bare glyph does. Icon-only is
for neutral, recoverable affordances (refresh, expand), not for a write.

**Verify at 320px, not only 390px.** 320 is the floor every major design system bottoms
out at, and it is where a layout that merely looks tight at 390 actually breaks — the
Apps card measured a 34px text column at 390px and 0px at 320px.

**Build touch targets to 44px; grade them in two tiers.** 44px is the number every system
recommends. WCAG 2.2 SC 2.5.8's floor is 24x24, but it carries a **spacing** exception: an
undersized target still conforms if a 24px circle centred on it does not intersect a
neighbour's. So under 24x24 *and* crowded is a conformance failure; under 44x44 alone is a
convention miss. Reporting every sub-44 control as a violation over-reports by roughly 3x.

**`overflow: hidden` on ANY ancestor kills `position: sticky` — use `overflow: clip`.**
Same family: a `transform` on an ancestor re-anchors `position: fixed` children, and
`align-self: start` is the most common silent sticky failure in flex and grid. A sticky
element also cannot escape its own parent's box, so a bar that must outlive a scrolling
sibling has to be that sibling's SIBLING, not its child.

**`100vh` resolves against the LARGE viewport.** A `100vh` panel overflows while the URL
bar is showing and its bottom controls fall off screen. Use `svh` for app shells, since it
does not reflow as the bar animates, and `dvh` only for surfaces that must track the exact
visible area (a chat container, a modal). Safe area is **padding, not size**:
`padding-bottom: env(safe-area-inset-bottom)`, which resolves to 0 without
`viewport-fit=cover`.

**The shell is an application, not a zoomable document — page zoom is off on touch.**
Pinching magnifies a `position: fixed` / `h-dvh` layout whose scrollers are all
*inner*, so there is no axis left to reach what the magnification pushed outside the
visual viewport: topbar, composer and drawer leave at once and only a second pinch
brings them back. Three mechanisms enforce it because no single one covers every
engine — `maximum-scale=1, user-scalable=no` in `index.html` (Blink, Gecko), a root
`html { touch-action: pan-x pan-y }` under `@media (pointer: coarse)` in `index.css`
(Blink's pinch and double-tap paths), and cancelling Safari's `gesturestart` in
`utils/pageZoom.ts` (WebKit has ignored the viewport zoom keys for user gestures
since iOS 10). Pointer-fine devices are untouched: ctrl+wheel and the trackpad pinch
are a desktop convention this has no business changing.

The corollary is the part to get right. **A surface that must magnify owns its own
zoom — it does not ask for `pinch-zoom` back.** `touch-action` is intersected from
the hit-test target up to the root, so a descendant cannot re-grant a behaviour the
root withheld; declaring `touch-pinch-zoom` there buys a dead gesture, not a working
one.

**Count the surfaces this rule binds before believing it holds.** There are **three**
full-viewport magnify overlays — the image viewer (`Lightbox` in
`MarkdownRenderer.tsx`), the diagram viewer (`DiagramLightbox.tsx`), and the
screenshot viewer in `pages/AppDetailPage.tsx` — and when page zoom was first
switched off only the first owned a gesture. The second silently
became unmagnifiable by any gesture, because its content is fit-scaled vector whose
labels are smallest at exactly the state it opens in. The rule read as satisfied
because the *documented example* obeyed it; nothing had counted the instances. The
first two
now share `hooks/usePinchZoom.ts` (contact tracking, focal anchoring, pan clamping),
so a further such surface gets the gesture by using the hook rather than by
re-deriving the math — and `touch-none` on the transform target is what opts it out
of the root's `pan-x pan-y`.

**A trackpad is a third input class, not a touchscreen.** A trackpad pinch emits no
pointer events at all, so it reaches none of the contact-tracking code: Blink
reports it as a `wheel` carrying `ctrlKey`, WebKit as
`gesturestart`/`gesturechange` carrying a **cumulative** `scale`. The hook claims
both, which is what gives a laptop — and `ctrl`+scroll on a mouse — the same
magnification a touchscreen gets from two fingers. Four constraints are
load-bearing and each is easy to get wrong:

- **`gesture*` binds only under `(pointer: fine)`.** The converse of "a trackpad
  pinch emits no pointer events" does not hold: a gesture event does not imply a
  trackpad. **iOS Safari fires `gesturestart`/`gesturechange` for a two-finger
  TOUCH pinch too**, and those fingers are already driving the contact-tracking
  path — so binding both on a touch device puts two independent formulas on one
  pinch and zooms twice. The media query keeps this an *additional* input path for
  pointing devices rather than a second one for touch. `wheel` is deliberately
  **not** gated: a coarse-pointer device can still carry a mouse. Absent
  `matchMedia` counts as coarse, because failing closed costs only a trackpad path
  on a platform that has none, while failing open restores the double zoom.

- **The listeners cannot be React props.** React attaches `wheel` at the root
  *passively*, so `preventDefault()` inside an `onWheel` prop is ignored and the
  browser page-zooms anyway. They are manual `addEventListener` calls with
  `{ passive: false }`.
- **They sit on `window` and gate on containment**, not on the element. A viewer's
  element ref is null until it opens, so an effect reading the element at mount
  would bind nothing. Containment is the **overlay**, not the transform target: the
  letterbox around a small image is visually the viewer, and letting a pinch there
  fall through page-zooms the whole app behind a viewer that looks unchanged.
- **Binding is gated on the consumer being in a zoomable state**, which carries two
  distinct costs. A non-passive listener makes the compositor wait on main-thread
  dispatch for *every* wheel event, so an always-mounted consumer would tax
  scrolling app-wide while its viewer is shut. And claiming a gesture the consumer
  ignores would suppress page zoom — which, on content that is **not** fit-scaled,
  genuinely does magnify. So a no-viewBox diagram binds nothing and keeps that
  fallback.
- **Only `ctrl`+wheel is claimed.** A plain wheel belongs to whatever scroller owns
  it, which is what a no-viewBox diagram depends on to reach its edges.

And note why page zoom is not a substitute for any of this: a fit-to-viewport
surface is *invariant* under page zoom. At 200% the viewport's CSS-pixel width
halves, the `fixed inset-0` box halves with it, and the content re-fits to the
smaller box while each CSS pixel covers two device pixels — the two cancel, and the
labels come out the same apparent size.

The guard that enforces this sweeps **both** `components/**` and `pages/**`, because
a magnify overlay can live in either and a population scoped to one directory counts
instances of a set it has itself narrowed. `AppDetailPage.tsx` is carried in that
guard as a named, issue-linked exception rather than excluded by the glob: an
exception a reader can see is a debt with an owner, a glob boundary is not. Giving it
the gesture is tracked separately because its overlay also owns arrow-key navigation
between screenshots and click-to-dismiss, so a pinch there has to be reconciled with
a prev/next seam the other two do not have.

Code blocks take the other legitimate route and scroll
horizontally instead. And note what is *not* lost — the OS Display Zoom setting sits
outside the viewport contract and still magnifies anything. A browser tab's own
text-size control does too, but it is **not** a fallback in the installed app: a
standalone PWA has no Safari toolbar to reach it from, so on a home-screen install
Display Zoom is the only route. State it with that qualification everywhere the
claim appears (`website/index.html`, `docs/guides/remote-and-mobile.md`) — an
unqualified version points a low-vision user at a control that is not there.

**Any touch input below 16px zooms the viewport on focus, and WebKit does not zoom
back out.** The scale is `clampTo(16 / fontSize, minimumScale, maximumScale)` from the
FIELD's computed size, so a `text-sm` field can leave the user zoomed in — and with
page zoom off there is no pinch-out to undo it. **An app-wide floor for this was
written and withdrawn, and it should stay withdrawn** unless someone brings evidence
from a real device, because CSS cannot express a floor at all: it can only SET a
size, so the two available shapes are wrong in opposite directions. A rule broad
enough to reach every field SHRINKS the ones that are deliberately larger — measured,
not hypothetical: `input:not([type=…]):not([type=…])` is (0,2,1) and beat the artifact
rename field's `text-2xl`, snapping a 24px title to 16px on a phone. A narrower
selector list misses fields instead, because a size can arrive as a named utility, an
arbitrary value (`text-[13px]`), an `!important` modifier or an inline style, and no
list contains the next one. A guard test rescues neither shape: ~120 of this app's
fields are routed through `<Input>` / `<Textarea>` rather than a native tag, so a
source sweep for `<input>` cannot see them and reports green.

Two things make withholding the floor the safer side of that trade. The focus zoom is
**pre-existing** — it is not introduced by suppressing pinch, which removes only the
recovery gesture — and whether it can fire at all once `maximum-scale=1` is authored
depends on the same engine path that decides whether WebKit honours the viewport keys,
which is not answerable from source. Settle it on a device; if it does fire, the fix
belongs in the field components, where a real `max(16px, authored)` is expressible.

**The `meta-viewport` axe rule is left ENABLED, deliberately.** `@axe-core/react` scans
every render, so `user-scalable=no` reports a critical WCAG 1.4.4 finding on every scan.
A waiver for it was written and removed; do not re-add one. The argument for waiving was
that a permanent finding nobody can action trains contributors to ignore the console —
but the finding *is* actionable, because it is a decision, and a decision does not stop
being owed because a scanner keeps asking for it. That recurring report is currently the
only automated reminder that suppressing page zoom is an accessibility trade with no
in-app text-size control substituting for it. Revisit the waiver only once that decision
is recorded, and then record the decision rather than the silence.

**Use the line-length cap in reverse to tell "ugly" from "broken".** WCAG 1.4.8 caps a
reading measure at 80 characters, 40 for CJK. Run it backwards and a squeezed pane stops
being a matter of taste: a 50px column at 13px holds three CJK glyphs, which is a defect
you can state as a number.

**Reach for a `Card` less often on a phone.** A card buys grouping with a drawn border
plus its own inset — on a 390px screen that is 16px of width and a line the screen edge
already implies. Where a section is the only thing on the page, or where the grouping is
already obvious from a heading, prefer a heading plus content and let the page gutter do
the work. Cards earn their keep when several peer groups must be told apart on one
screen; they cost the most when they are nested, since each level charges its inset
again.

**An overflowing action row belongs in an overflow menu — not wrapped, not silently
scrolled.** This is the one place the design systems are unanimous (Primer's `ActionBar`,
Carbon's five-action cap, Apple's "define which items move to the overflow menu"), and it
is what `AUTOSDE.yaml`'s `max-two-buttons-per-row` encodes. Wrapping such a row below `md`
keeps the controls reachable, but it is an interim, not the answer.

### A horizontal drag on mobile belongs to the nav drawer unless a page claims it

The mobile nav drawer is bound app-wide: **one** `useDrawerSwipe` on the shell
(`[data-testid="dashboard-shell"]`), so a rightward drag opens it and a leftward one
closes it on every routed page, including surfaces that know nothing about it. The
root is the SHELL, not `<main>` — the drawer's panel and scrim are `fixed`
**siblings** of `<main>`, so an instance rooted there can open the drawer but never
receives the touch that should close it: the finger lands on the scrim, and the
listener is on an element the scrim is not inside.

**A page with its own horizontal drawer must claim the sides it owns**, or two
instances arm on one touch and fight for the same direction. Put
`data-owns-swipe` on the element the page binds its OWN gesture to, listing the
sides — `"left"`, `"right"`, or both space-separated:

```tsx
<div ref={chatContainerRef} data-owns-swipe="left right">
```

`side` here is the edge a panel is ANCHORED to, matching `useDrawerSwipe`'s own
option: a left-anchored drawer opens on a rightward drag.

The hook walks from the touch target up to but **not including** its own root, which
is what lets one attribute serve both instances — the claim is strictly below the
shell (so the app-wide instance stands down) and IS the page instance's own root (so
the page proceeds). Put the attribute anywhere else and one of the two breaks
silently: on a descendant, the page suppresses its own drawer; on an ancestor, the
app-wide instance never sees it.

**The mechanism fails OPEN.** No attribute means the app-wide gesture works, so a page
that forgets to declare gets a visible conflict. The inverse default would let one
missing attribute kill the gesture dashboard-wide with nothing to see.

**Which is why the claim must track what is actually BOUND, not the page.** A claim
that outlives its ownership defeats that default from the one place that declares.
The chat page binds nothing when `embedded` — and an embedded chat renders *inside*
the shell at full width on mobile (the artifact companion, the Papyrus co-author
panel, an app SDK panel), so an unconditional claim there suppressed the nav swipe
while serving nothing: a dead gesture across the whole screen, on the chat-shaped
surface where a user is most likely to try it. Gate the attribute on the same
condition as the bindings:

```tsx
data-owns-swipe={embedded ? undefined : 'left right'}
```

Two kinds of surface need no attribute, and it is worth knowing why rather than
copying:

- **Anything portaled to `document.body`** is outside the shell entirely, so the
  gesture cannot reach it. That covers `Modal`, the notification sheet, and the
  collapsed-rail tooltips. A static read of the JSX suggests otherwise — the sheet is
  written inside the topbar — so check for `createPortal` before concluding a surface
  is inside the shell.
- **Content that scrolls horizontally** already claims the gesture by being
  scrollable: the hook defers to the nearest horizontally-scrollable ancestor
  **outright**, whatever its scroll position. Wide code blocks, markdown tables and
  diagram strips need nothing declared. The deference is deliberately not the
  nested-scroll handoff you would give a scrollable PARENT — deferring only while
  the inner scroller still had somewhere to go meant a freshly rendered code block,
  which sits at `scrollLeft: 0`, handed the very first rightward drag to the drawer
  instead of scrolling the code. An element with nothing to scroll (content that
  fits) owns no axis, so the drawer is still reachable over it.

  **The search crosses shadow boundaries, via `composedPath()`.** `e.target` read
  from a listener outside a shadow root is retargeted to the HOST, so walking
  `parentElement` from it never sees a scroller inside the root — and that is not a
  corner case: a *finished* chat code block renders through `@pierre/diffs`, whose
  `diffs-container` is a web component carrying the `overflow` on an element in its
  shadow root. Read from the outside, such a block looks scroller-less and the drawer
  took every drag over it. Testing this needs a fixture that dispatches on the host
  with a real `composedPath()`; dispatching straight at the inner node leaves
  `e.target` inside the root, where a plain parent walk also finds the scroller, and
  the test cannot fail.

**A locked gesture takes the page's own handling away, and only then.** The four touch
listeners are `passive: true`, which is what keeps a touch that never becomes a gesture
on the browser's scroll fast path; the price is that a passive listener may not
`preventDefault()`, so the page kept scrolling vertically under the moving drawer and
fired a click on release. Both are suppressed from the moment the gesture LOCKS — a
non-passive `touchmove` added then governs the rest of the gesture, plus a one-shot
capture-phase `click` swallower for the release, both on `window` and both released when
the gesture ends. The click swallower is not redundant with `preventDefault`: a touch
that BEGAN on a button and then moved still fires its click. A suppression that has ENDED
is a different thing wearing the same slot — parked only to eat the release's click, with
its touchmove listener already removed — so a new gesture must release it and install a
fresh one rather than inheriting it. That window is ~350ms, which is exactly the
"swipe shut, swipe straight back open" beat, so inheriting it left the second of two
quick drags with no scroll suppression at all. It is also released as soon as a NEW touch
begins, because a fresh finger means any pending click belongs to that touch — without
which the swallower eats a genuine tap in the COMMON case rather than a rare one: a drag
over non-interactive content has its synthetic click suppressed by `preventDefault()`
already, so nothing arrives to disarm the swallower, it stays armed for the full window,
and the next real tap is the one it swallows — right on this feature's core beat, swipe
the drawer open and immediately tap something in it.

**Ownership is declined, never contested: the browser decides first.** It commits a
touch to a scroller earlier than this hook's axis lock does and by its own rule, and
once it has, nothing takes the touch back — `preventDefault()` is ignored. A diagonal
drag is where the two rules disagreed: a dy just under dx passed the "is this vertical?"
test while dy alone had already started a scroll, so the drawer arrived to find the page
moving under it. So a gesture whose vertical drift reaches `PLATFORM_SCROLL_SLOP` (8px,
deliberately below the 10px axis lock) is abandoned rather than fought for. Reading the
platform's own answer instead — an engine marks a touchmove non-cancelable once it owns
the touch — is **not** safe to act on: `cancelable` is false by default on a synthetic
event and is not guaranteed true for an ordinary touchmove delivered to a passive
listener, and a false reading abandons every gesture. A displacement threshold is
engine-independent and fails toward keeping the gesture.

**The gesture reads the state the panel is COMMITTED to, not the `open` prop.** The
consumer learns a new state from `onSettle`, which runs in the settle animation's
completion callback, so for the whole ~200-300ms of a closing slide the prop still says
open. A gesture starting in that window judged its direction against a panel that was
already leaving — a re-opening drag read as an opening drag on an open panel and was
declined — so swiping the drawer shut and immediately swiping it back open failed for as
long as the settle ran, intermittently and with the direction perfectly clean. A settle
therefore commits its own target the moment it starts, and the prop is adopted when it
CHANGES, which is the authority for a panel opened by tap rather than by gesture. Both
halves are load-bearing: without the first, re-opening is declined; without the second, a
hamburger-opened drawer cannot be dragged shut.

**A MODAL LAYER owns every touch inside it, read from its `role`.** A dialog is not
necessarily portaled out of the shell: the changelog and update-error overlays are plain
`fixed inset-0` JSX inside it (the shell element spans `App.tsx` 2635-3878, and both sit
between), so a horizontal drag across one pulled the nav drawer out BEHIND the dialog.
The hook therefore stands down for any `role="dialog"` / `role="alertdialog"` in the
chain. Read as a rule rather than a list of overlays, because `src/` declares dozens of
dialogs and a list means the next one silently fights the drawer — the same reasoning as
the `touch-action` rule below. Only those two roles count: treating any `role` as
ownership would hand away most of the page.

**A drag WIDGET needs no attribute either, because `touch-action: none` already says
so.** Sliders, resize handles, column splitters and pinch-zoom canvases are not
horizontally scrollable, so the scroller deference does not cover them — and they run
on POINTER events, whose `preventDefault` does NOT stop the touch stream from reaching
a listener on an ancestor. The hook therefore also yields to any element in the chain
whose computed `touch-action` is `none`, which is the platform's own declaration that
the element took touch handling from the browser. Only a full `none` counts: the root
sets `pan-x pan-y` under a coarse pointer to switch page zoom off, and treating that as
ownership would kill the gesture everywhere.

Reading the property is what keeps this from being a list that goes stale. There are
around a dozen such widget families in `src/` today (`ResizeHandle`, `ColumnSplitter`,
`BottomTerminalPanel`, `SessionGridLayout`, `DiagramLightbox`, the `Slider` in
`components/ui.tsx`, …); asking each to remember an attribute means the next one
silently fights the nav drawer instead.

**Count the panels before believing the rule holds.** Four are driven by
`registerDrawerTargets` today: the nav drawer, the chat page's sessions drawer and
activity panel (all three with gestures), and the notification sheet (no gesture,
portaled). Only the chat page declares a claim, because it is the only one that binds
its own gesture inside the shell.

**Two sibling instances exclude each other on INTENT, not on arrival.** The chat page
binds `useDrawerSwipe` twice on one element — sessions drawer on the left, side panel on
the right. While both are closed, DIRECTION separates them: each rejects the drag that
would open the other. Once one is open it cannot, because that panel's closing drag is
the other's opening drag, so each instance is `enabled` only while its sibling is not
open.

Spell that gate as `phase !== 'open'`, never `phase === 'closed'`, and give the consumer
the release decision through `onCommit` rather than `onSettle`. `onSettle` deliberately
waits for the settle animation so a consumer cannot unmount a panel mid-slide, which
makes it the wrong signal for a gate: keyed on arrival, the exclusion stayed shut for the
whole ~300ms slide, so a swipe that dismissed one panel could not be followed straight
away by a swipe revealing the other — the user had to wait out an animation they had
already finished driving. The hazard lasts exactly as long as the sibling is OPEN.

A committed close therefore parks the phase at `'closing'`, not `'closed'`: the panel is
still on screen and its mount predicate keys on `!== 'closed'`, so writing `'closed'`
here would cut the slide short. That also matches what a tap-driven close already did,
which is why the chrome derived from the phase does not change timing.

### A panel that gains a gesture must be bound LIVE to its offset

A panel moved only by a tap may serialize its offset at render time —
`style={{ transform: \`translate3d(${x.get()}px, 0, 0)\` }}` — because `animateDrawer`
writes the arrival into the element's own inline style. The notification sheet still
does this, correctly.

**The moment that panel gains a drag, that form is wrong**, and the failure looks
like a feel problem rather than a bug: a MotionValue deliberately does not re-render
React, so the drag writes the value every frame while the DOM moves only on whatever
re-render happens to occur — the panel comes out a little, freezes, and completes on
release when the settle takes over. Bind it instead, as all three gesture-driven
panels do:

```tsx
<motion.nav style={{ x: mobileNavX }}>
```

Framer and the compositor settle coexist on one element, because `takeOverDrawer`
adopts and cancels whatever is running before either writes. A scrim has the same
requirement in its other half: derive its opacity from the offset (over the drawer's
OWN travel, so the dim reaches 0 exactly as the panel clears the edge) rather than
holding a literal, or it cannot dim with the finger.

### Horizontal insets below the breakpoint

Padding stacks, and the eye reads the SUM. On a wide viewport a page gutter plus a card
inset plus a row inset is comfortable; at 390px it is not. The skill-budget row measured
16px (page) + 20px (`Card`) + 16px (row) = **52px** before its text, against 16px for the
same text in chat.

The page container keeps the `px-4 md:px-6 pb-8` the skeleton above prescribes -- that is what
`AUTOSDE.yaml`'s `page-layout-pattern` requires, and it is not the layer to change. The
third layer is the one to drop:

**Below `md`, prefer no horizontal padding on a row that is a DIRECT child of a `Card`.**
The page gutter and the card's own inset already supply it:

```tsx
<div className="… py-2 md:px-4">   {/* row: the card supplies the inset while narrow */}
```

Gate **every** row in that card the same way -- section header, group header, data row,
footnote. Gating only some of them leaves the data rows sitting to the left of the headers
that label them, which reads as rows escaping their own section.

**The direct-child part is the precondition, not a detail.** The rule works because the
card is what supplies the inset the row gives up. Put an unpadded bordered pane between
them and that stops being true:

```tsx
<Card>                                                   {/* 20px */}
  <div className="… border border-border rounded-md">    {/* 0px, draws a visible edge */}
    <div className="… px-4 py-2.5 border-b">             {/* row: px-4 is its ONLY gutter */}
```

Here the row's `px-4` is load-bearing -- gating it puts the text flush against the border.
The excess inset belongs to the card, but the card is NOT what yields: halve the card's inset
below `md` and pull the pane out by exactly that amount, on the shell the pane and its
loading skeleton share so the layout does not jump when data arrives. The two numbers
are ONE number -- changing the inset without the margin pushes the pane past the border:

```tsx
const PANE_SHELL_CLASS = 'flex gap-3 -mx-2 md:mx-0 …'  /* cancels `Card`'s own px-2 */
```

From the boxes at 390px on the Skills tab: the pane goes from left 25 / width 340 to
left 17 / width 356, so a row inside it starts at ~34px instead of ~42px, against 16px
for the same text in chat. (The pattern was first measured on a page that ran a 16px
gutter and a 20px card inset, where the same pull-back moved the pane from left 37 /
width 316 to left 17 / width 356.)

**Do not flush the card itself** (a `px-0` override). Its padding is also the only gutter the
toolbar above the pane has, and removing it puts the search field's rounded border
directly against the card's border -- measured as a 0px gap, and the first thing a reader
calls ugly. `Card`'s own narrow inset (`px-2`, 8px) keeps the field off the border
while giving the row back most of the width. An inset toolbar above a full-bleed list is the ordinary phone pattern; the
two do not need to share a left edge.

This does not touch the page container's `px-4 md:px-6 pb-8`, which is what `AUTOSDE.yaml`'s
`page-layout-pattern` names and is not the layer to change. For a pane that must reach the
SCREEN edge, past the page gutter, cancel the gutter itself inside the pane (`-mx-3` while
narrow) -- the same one-number-written-twice pairing, so pin it with a test.

**Status: a direction, not a description of the repo.** Two shapes are migrated --
`SkillContextBudget` (direct-child rows) and the `SkillsTab` / `SteeringTab` split panes
(card flush). A scan for `className="…px-4…py-2"` under `website/src/pages` matches ~27
rows across 15 files, but a hit is not a work item: most are toolbars, banners, sticky
bars and buttons that own the only gutter their content has, and rows inside a bordered
pane must keep theirs. There is no lint gate for this. Read the structure around a hit
before gating it, and see kirodotdev/KiroCrew#3939 for the triage of all 27.

## Stat cards

OPTIONAL summary metrics above the content. Add a row only when a number is not
already visible in the content below it: a rolled-up total, a rate, an error
count. Do NOT add one that restates `items.length` for a list rendered on the
same screen; it costs roughly 90px above the fold and carries no action. A page
with no stat card row is conformant.

```tsx
<div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
  <StatCard label="Total" value={count} accent />
  <StatCard label="Active" value={active} />
</div>
```

`StatCard` renders a pulsing skeleton when `value` is `undefined` or `null`, so
pass the query result straight through instead of branching on a loading flag.
Pass `delay` (in ms) to join the grid's stagger. Give it `onClick` only when the
card is really actionable; it then wires `role="button"`, `tabIndex` and
Enter/Space itself.

## Data sections

`Card` + `CardTitle` + `InfoTip`:

```tsx
<Card>
  <CardTitle>Section Name <InfoTip text="Explanation." /></CardTitle>
  <SearchInput placeholder="Filter…" value={filter} onChange={…} />
  {items.length === 0
    ? <EmptyState icon={<Anchor className="lucide-inline" />} title="None yet" />
    : <table className="w-full border-collapse table-striped">…</table>}
</Card>
```

Inside a **side panel**, a counted list-section header is `PanelSectionHeader`
(label + count node + hairline rule), never a hand-rolled one. Hierarchy comes
from weight and size, never from an opacity modifier, and the label is not
uppercased (`text-transform` is a no-op on CJK).

## Tables

Striped body, one header cell style:

```tsx
<th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">
```

`table-striped` shades even rows with `var(--card-hl)`.

## Forms

Inline within a `Card`, built from the shared primitives:

- `Input` for text fields.
- `SendBtn` for the primary action (accent-colored).
- `Btn` for secondary actions, `Btn danger` for destructive ones.
- `Checkbox` from `ui.tsx` for a boolean box.
- **Dropdowns: never a native `<select>`.** Its popup is drawn by the OS, so it
  ignores every theme token, cannot be styled per row, and looks nothing like
  the rest of the app. Pick by list length and purpose:
  - `SettingsSelect` (`components/settings.tsx`) on a Settings page — label +
    description + dropdown as one field. The choke point for that surface.
  - `SimpleSelect` (`components/SimpleSelect.tsx`) anywhere else, up to roughly
    fifteen options. Radix Select under the hood; takes `options` /
    `optionLabels` / `value` / `onChange(value)`, and `action` for a trailing
    "+ New…" row.
  - `SearchableSelect` (`components/SearchableSelect.tsx`) past that, or any
    list a user would want to filter (timezones, file lists). Radix Popover plus
    a filter box.
  - `DropdownMenu` (`components/ui/dropdown-menu.tsx`) for a menu of *commands*
    rather than a bound value.
  - `AgentSelector` for agent dropdowns specifically (portal-based, ARIA-wired).

  These render a `<button>`, not a `<select>`, so an external
  `<label htmlFor>` does **not** name them — pass `aria-label`.

  **The one exception is touch, and it is not yours to make.** `SimpleSelect`
  routes to `NativeSelect` (`components/ui/native-select.tsx`) on a coarse
  pointer, so the OS draws the list there. The reason above is theming, and
  theming does not reach a phone: the Radix popup's list is a `position:fixed`
  overflow scroller inside react-remove-scroll's lock, and iOS Safari does not
  reliably hand a finger drag to that shape — Settings → Voice → Language showed
  7 of its ~41 codes with the rest unreachable. A themed list nobody can scroll
  is worse than an OS-drawn list that works. Because the choice lives inside
  `SimpleSelect`, no call site makes it — and `SettingsSelect` inherits it by
  wrapping `SimpleSelect`. It goes no further: `SearchableSelect`,
  `DropdownMenu` and `AgentSelector` keep the themed popup on a coarse pointer,
  since a native `<select>` cannot host a filter box, per-option sublabels or a
  command menu. Reaching for one of those does not mean the touch case has been
  handled for you; whether that scroller is a real defect on a phone is
  unresolved in #5551. `NativeSelect` is the single file exempted from the
  `no-restricted-syntax` rule; do not add a second.
- `Toggle` for a boolean switch. It carries `role="switch"`, `aria-checked` and
  `aria-disabled` itself, so do not re-add them.

## Status indicators

- `Badge variant="ok" | "err" | "warn" | "aim" | "muted"`.
- `SourceBadge source="…"` for provenance (where an agent, app, or skill came
  from). It maps known sources to colors and falls back to a neutral pill for an
  unknown one, so pass the raw source string.

## Errors

A dismissible banner above the content:

```tsx
<div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
```

## Animations

`animate-rise` on cards and banners, `animate-scale-in` on inline reveals. Both
are Tailwind utilities defined in `tailwind.config.js`, and both use
`backwards` fill so an `animationDelay` holds the element hidden until its turn.

## Do NOT

- Wrap a page in `<div className="p-6 max-w-[960px] mx-auto">`. Use
  `PageHeader` + the `px-4 md:px-6 pb-8` container.
- Use a raw `<input>` / `<button>`. Use `Input`, `Btn`, `SendBtn`,
  `SearchInput`, `Checkbox`.
- Use a native `<select>`. There is no styled wrapper for one any more — see
  §Forms for which dropdown component to reach for, and for the one touch-only
  exception `SimpleSelect` already makes for you. Enforced by
  `no-restricted-syntax` in `eslint.config.js`.
- Use raw status text. Use `Badge` or `SourceBadge`.
- Use `text-xs`. Use `text-[13px]`.
- Add a new CSS `@keyframes`. Use Framer Motion, or an existing utility.
