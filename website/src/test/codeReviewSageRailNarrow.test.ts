/**
 * Code Review Sage's rail must stop competing with the report on a phone.
 *
 * The rail is a resizable column with `MIN_RAIL_WIDTH = 280`, set through an
 * inline `style={{ width }}` on a `flex-shrink-0` box — so at a 390px viewport it
 * held 280-360px and left the report pane roughly 100px. `useColumnResize`
 * already supports a narrow-viewport strip, but it is OPT-IN via the collapse
 * config's `whenNarrow`, and this shell never passed one.
 *
 * Two halves, and the second is the load-bearing one. `ProjectsPage` states the
 * trap directly: "A page that only got the strip would hand the user an expand
 * button that leads straight back into the squeeze." So the strip alone is not
 * the fix — the EXPANDED state has to take the whole viewport, with the report
 * stepping aside, which is what `WebhooksPage` and `ProjectsPage` both do.
 *
 * Asserted over the source: the defect is which declaration each element carries
 * (an inline px width vs `100%`, `flex` vs `hidden`), and jsdom does no layout,
 * so a render could not measure the difference anyway.
 */
import { describe, it, expect } from 'vitest'

import {
  COLLAPSED_RAIL_WIDTH, MIN_RAIL_WIDTH, RAIL_COLLAPSED_KEY,
} from '../apps/code-review-sage/lib/layout'

async function shell(): Promise<string> {
  return (await import('../apps/code-review-sage/Workspace.tsx?raw')).default as string
}

/** Any other file in the app, read as source for the same reason as `shell`.
 * Spelled out per file rather than interpolated, so the bundler resolves each
 * `?raw` import statically. */
async function railSource(): Promise<string> {
  return (await import('../apps/code-review-sage/components/LeftRail.tsx?raw')).default as string
}

async function headerSource(): Promise<string> {
  return (await import('../apps/code-review-sage/components/RailHeader.tsx?raw')).default as string
}

describe('Code Review Sage rail at narrow widths', () => {
  it('opts into the narrow-viewport strip', async () => {
    const src = await shell()
    const cfg = src.match(/const RAIL_COLLAPSE: CollapseConfig = \{[\s\S]*?\}/)
    expect(cfg, 'expected a module-level RAIL_COLLAPSE config').not.toBeNull()
    // Without `whenNarrow` the hook's narrowMode stays false and the rail keeps
    // its px width on a phone — the config alone is not enough.
    expect(cfg![0]).toContain('whenNarrow: true')
    expect(cfg![0]).toContain('COLLAPSED_RAIL_WIDTH')
  })

  it('gives the whole viewport to the rail once expanded while narrow', async () => {
    const src = await shell()
    expect(src).toMatch(/mobileRailOpen = isMobile && !rail\.collapsed/)
    // The anti-squeeze invariant: expanded-while-narrow is 100%, never rail.width.
    // Nested inside the bar-mode branch, which releases the width entirely while
    // COLLAPSED — the two narrow states are disjoint, so this still pins the
    // expanded one and still fails if the 100% is dropped.
    expect(src).toMatch(/mobileRailOpen \? '100%' : rail\.width/)
  })

  it('steps the report aside instead of pushing it off-screen', async () => {
    const src = await shell()
    // A 100%-wide rail beside a flex-1 main would push the report out of view
    // rather than replace it, leaving nothing reachable by scrolling.
    expect(src).toMatch(/mobileRailOpen \? 'hidden' : 'flex'/)
    // And no main pane may keep an unconditional `flex` class.
    expect(src).not.toMatch(/className="flex-1 min-w-0 min-h-0 flex flex-col"/)
  })

  it('leaves the collapsed strip an expand control with an accessible name', async () => {
    const src = await shell()
    expect(src).toMatch(/rail\.collapsed \?/)
    expect(src).toMatch(/onClick=\{rail\.expand\}/)
    // Reuses the app-agnostic catalog key, so this adds no untranslated string.
    expect(src).toContain("i18nT('app.expand_sidebar')")
  })

  it('collapses the rail on select, so the expanded rail is not a one-way door', async () => {
    const src = await shell()
    // The third leg of the pattern. Without it, picking a review from the
    // full-width rail changes nothing visible: the rail keeps the viewport, the
    // report stays hidden, and the drag handle that would collapse it is gone on
    // touch — leaving a reload as the only escape. `whenNarrow` requires it, and
    // both cited shells (ProjectsPage, WebhooksPage) call rail.collapse().
    expect(src).toMatch(/if \(isMobile\) rail\.collapse\(\)/)
    // Keyed on the selection, not on every mobile render, or it would fight the
    // user's own expand and the strip could never be opened at all.
    expect(src).toMatch(/\}, \[selectedPr, activeRun, mainView, isMobile\]\)/)
  })

  it('keeps the drag handle off touch, where it costs width and does nothing', async () => {
    const src = await shell()
    expect(src).toMatch(/\{!isMobile && \(\s*<Splitter/)
  })

  it('has a strip narrower than the column minimum it replaces', async () => {
    // A "collapsed" width at or above the minimum would not free any room.
    expect(COLLAPSED_RAIL_WIDTH).toBeLessThan(MIN_RAIL_WIDTH)
    expect(RAIL_COLLAPSED_KEY).toMatch(/^kc:code-review-sage:/)
  })

  it('lays the collapsed strip ACROSS THE TOP while narrow', async () => {
    const src = await shell()
    // Even a 44px strip is a tenth of the reading column at 390px, and
    // horizontal is the axis a phone cannot spend. Above the report it costs
    // height, which a phone has.
    expect(src, 'expected a railBar derived from narrow AND collapsed')
      .toMatch(/const railBar = isMobile && rail\.collapsed/)
    expect(src, 'expected the shell to stack while the bar is up')
      .toMatch(/\$\{railBar \? 'flex-col' : ''\}/)
    // The strip's own axis has to turn with it: left border and a column of
    // controls make no sense once the element is a row across the top. And the
    // width must be released, or the bar keeps reserving the strip's column.
    expect(src, 'expected the strip to turn into a row').toMatch(/flex-row items-center border-b/)
    expect(src, 'expected the vertical form to survive on a desktop')
      .toMatch(/flex-col items-center border-r/)
    expect(src, 'expected the bar to release the strip width')
      .toMatch(/width: railBar \? undefined :/)
  })

  it('keeps the section nav on the bar, not only inside the rail', async () => {
    const src = await shell()
    // The nav (Reviews / Learning / Settings) is this app's ONLY navigation. A
    // bar carrying just an expand glyph put every section behind a control that
    // does not look like navigation, so from a report there was no visible route
    // to Learning or Settings at all — the rail had to be reopened first.
    expect(src, 'expected the bar to render the rail header')
      .toMatch(/<RailHeader\s+narrow\b/)
    const header = await headerSource()
    for (const key of ['reviews', 'learning', 'settings'] as const) {
      expect(header, `expected the shared header to own the ${key} nav row`)
        .toContain(`apps.codeReviewSage.components.leftRail.${key}`)
    }
    // Shared, not copied: two spellings of the nav would drift, and the rail is
    // where it already lived.
    const rail = await railSource()
    expect(rail, 'expected the rail to render the same header component')
      .toMatch(/<RailHeader\b/)
    expect(rail, 'expected the rail to have given up its own nav rows')
      .not.toMatch(/function NavRow/)
  })

  it('does not spend the same glyph on the app mark and a nav button', async () => {
    const header = await headerSource()
    // The mark is decoration and the nav button is a control, and on the bar they
    // sit inches apart: rendering both as ScanSearch left a first-time user no
    // way to tell them apart, so the mark reads as a button and taps dead. The
    // mark keeps the app's identity glyph; the Reviews row takes the one its own
    // list already uses.
    const markGlyph = header.match(/<ScanSearch size=\{16\}[^>]*aria-hidden/)
    expect(markGlyph, 'expected the app mark to keep the identity glyph').not.toBeNull()
    const reviewsEntry = header.match(/view: 'reviews',[\s\S]*?icon: (\w+),/)
    expect(reviewsEntry, 'expected the reviews section to declare an icon').not.toBeNull()
    expect(reviewsEntry![1], 'expected the reviews nav glyph to differ from the app mark')
      .not.toBe('ScanSearch')
  })

  it('gives the bar a labelled way back to the list', async () => {
    const src = await shell()
    // The collapsed bar sits above the report, and component selection state is
    // not browser history: without an explicit control there is no way back to
    // the list a review was picked from. Reuses the shared list-detail control
    // rather than a bare chevron, so the label names the destination.
    expect(src, 'expected the shared back control').toMatch(/<ListDetailBack\b/)
    expect(src, 'expected back to reopen the rail, which is where the lists live')
      .toMatch(/onBack=\{rail\.expand\}/)
    // Settings' rail holds no list, so a "back" there would reveal an empty
    // panel — the nav rows are the way out of that section. Asked of the shared
    // section table rather than spelled here, so one place decides it.
    expect(src, 'expected the back control to be scoped to the bar and to sections with a list')
      .toMatch(/const canOpenList = railBar && sectionHasList\(mainView\)/)
  })

  it('names the pane the back control actually lands on', async () => {
    const src = await shell()
    // A control that says "Reviews" and opens the Pull requests tab, or "Learning"
    // and opens a panel headed "Namespaces", names something the user cannot see —
    // the defect class this shell removes, so it must not reappear in the control
    // pointing AT the rail. Each section answers with its rail's own heading,
    // reusing that pane's existing catalog key rather than adding a string.
    expect(src, 'expected one named resolver rather than a growing ternary chain')
      .toMatch(/function railPaneLabel\(view: MainView, listTab: ListTab\): string/)
    expect(src, 'expected reviews to follow the active list tab')
      .toMatch(/listTab === 'reviews'/)
    for (const key of [
      'components.middleColumn.reviews',
      'components.middleColumn.pull_requests',
      'components.learningRail.namespaces',
    ] as const) {
      expect(src, `expected the pane's own label key ${key}`)
        .toContain(`apps.codeReviewSage.${key}`)
    }
    // A section with no rail pane of its own still answers with the section name.
    expect(src, 'expected the section-name fallback')
      .toMatch(/return sectionLabel\(view\)/)
    expect(src, 'expected the control to render that resolved name')
      .toMatch(/<ListDetailBack label=\{listLabel\}/)
  })

  it('gives the full-width rail its own exit while narrow', async () => {
    const src = await shell()
    // Re-picking the row that is already selected changes no state, so the
    // collapse-on-select effect never fires for it: without an explicit collapse
    // control, a user who opened the rail over a report they were reading could
    // be left with no way back to it.
    expect(src, 'expected the narrow rail to receive a collapse control')
      .toMatch(/<LeftRail narrow=\{mobileRailOpen\} onCollapse=\{rail\.collapse\} \/>/)
    const rail = await railSource()
    // Gated on `narrow`, not merely on the callback being present: the desktop
    // rail closes with its drag handle and must not grow a second control.
    expect(rail, 'expected the rail to render the collapse control only while narrow')
      .toMatch(/narrow && onCollapse \?/)
    expect(rail, 'expected the app-agnostic catalog key, adding no new string')
      .toContain("i18nT('app.collapse_sidebar')")
  })

  it('makes a re-tap of the ALREADY-ACTIVE section do something', async () => {
    const header = await headerSource()
    // `setMainView(view)` on the view it is already on changes no state, so React
    // renders nothing and the tap is silently dead — on the bar's Reviews icon,
    // which is the control a user reaching for "the list" is most likely to
    // press. The shell decides what re-tapping means; the nav only routes it.
    expect(header, 'expected an active re-tap to route to the shell, not re-set the view')
      .toMatch(/mainView === s\.view \? onReselect\?\.\(\) : setMainView\(s\.view\)/)
    const src = await shell()
    expect(src, 'expected the bar to spend that re-tap on opening the rail')
      .toMatch(/onReselect=\{canOpenList \? rail\.expand : undefined\}/)
  })

  it('governs BOTH routes into the rail with one predicate', async () => {
    const src = await shell()
    // Settings' rail body is an empty spacer, so reopening the rail there hands
    // the user a viewport-filling panel holding nothing. The labelled control
    // already refused to promise that; gating only the label left the icon
    // re-tap free to open it anyway — one rule, two doors, and only one locked.
    const predicate = src.match(/const canOpenList = railBar && sectionHasList\(mainView\)/)
    expect(predicate, 'expected a single named predicate for "reopening leads somewhere"')
      .not.toBeNull()
    // Both doors read it, and neither re-derives the condition for itself.
    expect(src, 'expected the labelled route to read the predicate')
      .toMatch(/const backToList = canOpenList/)
    expect(src, 'expected the icon re-tap to read the SAME predicate')
      .toMatch(/onReselect=\{canOpenList \?/)
    expect(
      (src.match(/sectionHasList\(mainView\)/g) || []).length,
      'expected sectionHasList to be consulted exactly once, by the predicate',
    ).toBe(1)
  })

  it('gives the mobile empty state a route to the list it points at', async () => {
    const src = await shell()
    // "Select a review to see its progress" names a list that is ON SCREEN on a
    // desktop and HIDDEN behind the bar while narrow — and this is the app's
    // first-run mobile screen, where nothing is selected yet. Reuses the bar's
    // own control (so it is null on a desktop, where the list is already
    // visible) rather than adding a second, differently-worded affordance.
    expect(src, 'expected the empty state to carry the same back-to-list control')
      .toMatch(/<EmptyState[\s\S]*?>\s*\{\/\*[\s\S]*?\*\/\}\s*\{backToList\}\s*<\/EmptyState>/)
  })

  it('reads the back label from the nav\'s own section table', async () => {
    const src = await shell()
    const header = await headerSource()
    // The label and the has-a-list question are the nav's knowledge. Spelling
    // them again in the shell let the two drift the moment a section is added or
    // renamed — Design Review's finding on the first revision.
    // Both of the table's exports are consulted (order-independent: the label
    // resolver sits above the component, the predicate inside it).
    expect(src, 'expected the shell to ask the table which sections have a list')
      .toMatch(/sectionHasList\(mainView\)/)
    expect(src, 'expected the shell to ask the table for a section name')
      .toMatch(/sectionLabel\(view\)/)
    // The tab keys are the LIST's own names, not section names — the table still
    // owns every section label the shell uses.
    expect(src, 'expected the shell to hold no section label keys of its own')
      .not.toMatch(/leftRail\.(reviews|learning|settings)'/)
    for (const key of ['reviews', 'learning', 'settings'] as const) {
      expect(header, `expected the table to own the ${key} label key`)
        .toContain(`apps.codeReviewSage.components.leftRail.${key}`)
    }
    // Resolved per call, not at module scope: a label frozen at import time
    // would keep the first locale for the life of the process.
    expect(header, 'expected the label to be resolved when asked for')
      .toMatch(/export function sectionLabel\([\s\S]*?i18nT\(/)
  })
})
