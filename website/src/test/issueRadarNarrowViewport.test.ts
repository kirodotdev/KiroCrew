/**
 * Issue Radar is a THREE-column shell: nav rail, list, detail. A phone cannot
 * hold three — the rail's minimum is 280px and the list's is 260px, so at 390px
 * the detail pane was left roughly 60px before any of this.
 *
 * Two mechanisms, and the test's job is to keep them wired to each other:
 *   1. the rail opts into its icon strip while narrow (`whenNarrow`), and
 *      opening it there takes the whole viewport instead of restoring 280px;
 *   2. list and detail drill down — exactly one is on screen, with a Back
 *      control, reusing the shell-agnostic `useListDetailView` primitive.
 *
 * Asserted over source. These are declaration-level facts (which element carries
 * `hidden`, whether a row handler also drills in, whether the state is written to
 * storage) and jsdom performs no layout, so a render could not measure the pane
 * widths this is really about.
 */
import { describe, it, expect } from 'vitest'

async function src(path: string): Promise<string> {
  return (await import(`../apps/issue-radar/${path}?raw`)).default as string
}
const shell = () => src('Workspace.tsx')

describe('Issue Radar at narrow widths', () => {
  it('opts the rail into its strip while narrow', async () => {
    const s = await shell()
    const cfg = s.match(/const RAIL_COLLAPSE: CollapseConfig = \{[\s\S]*?\n\}/)
    expect(cfg, 'expected a module-level RAIL_COLLAPSE').not.toBeNull()
    expect(cfg![0]).toContain('whenNarrow: true')
  })

  it('gives the rail the whole viewport when opened while narrow', async () => {
    const s = await shell()
    // ProjectsPage's warning: a strip whose expand button only restores the
    // column minimum hands the user back the squeeze it just escaped.
    expect(s).toMatch(/const railFull = listDetail\.isMobile && !rail\.collapsed/)
    expect(s).toMatch(/width=\{railFull \? '100%' : rail\.width\}/)
  })

  it('shows exactly one of list and detail while narrow', async () => {
    const s = await shell()
    expect(s).toMatch(/const showList = listDetail\.showList && !railFull/)
    expect(s).toMatch(/const showDetail = listDetail\.showDetail && !railFull/)
    // Three list panes (issues, pulls, crews) are gated, and none keeps a
    // hard-coded column width that would survive onto a phone.
    expect((s.match(/\{showList && \(/g) ?? []).length).toBe(3)
    expect(s).not.toMatch(/style=\{\{ width: (list|crewList)\.width \}\}/)
    expect((s.match(/showDetail \? '' : 'hidden'/g) ?? []).length).toBe(3)
  })

  it('does NOT gate the settings pane on showDetail', async () => {
    const s = await shell()
    // Settings is a single-pane view with no list beside it. Gating it the same
    // way would hide it on a phone until a row was picked, and it has no rows —
    // the view would be unreachable.
    expect(s).toMatch(/\$\{railFull \? 'hidden' : ''\}`\}>\s*\n\s*<SettingsView \/>/)
  })

  it('keeps a way back out of the detail pane', async () => {
    const s = await shell()
    expect(s).toMatch(/import ListDetailBack from/)
    // Only the crews pane still takes its Back row from the shell: the issue and
    // pull panes render theirs inside their own sticky header, so it can share a
    // row with the compact title instead of standing on its own 44px.
    expect((s.match(/\{narrowBack\(/g) ?? []).length).toBe(1)
    for (const pane of ['IssueDetail', 'PrDetail']) {
      const detail = await src(`components/${pane}.tsx`)
      expect(detail, `${pane}: must render its own way back while narrow`)
        .toMatch(/back=\{listDetail\.isMobile/)
      expect(detail).toMatch(/onBack=\{listDetail\.closeDetail\}/)
    }
  })

  it('drills in from every row handler, not from "is a row selected"', async () => {
    // Selection is PERSISTED (context restores selectedIssue/selectedPull), so a
    // selection-derived rule would open the detail on load with the list behind
    // it and Back unable to win against the restore.
    const lists = await Promise.all([
      src('components/IssueList.tsx'), src('components/PrList.tsx'), src('components/CrewList.tsx'),
    ])
    const drills = lists.reduce((n, s) => n + (s.match(/listDetail\.openDetail\(\)/g) ?? []).length, 0)
    expect(drills, 'expected all five row handlers to drill in').toBe(5)
  })

  it('never persists which pane is open', async () => {
    const ctx = await src('context.tsx')
    expect(ctx).toMatch(/const listDetail = useListDetailView\(\)/)
    // A restored open detail is the same trap as a selection-derived one.
    expect(ctx).not.toMatch(/detailOpen[^\n]*(localStorage|persist|restored)/)
  })

  it('collapses the rail on navigation, so the expanded rail is not a one-way door', async () => {
    const s = await shell()
    // The third leg of the pattern, and the one that turns the full-width rail
    // into a drill-down. Without it, tapping a section leaves the user looking at
    // the rail still covering the section it just navigated to, with the drag
    // handle (the only other collapse affordance) hidden on touch.
    expect(s).toMatch(/onNavigate=\{listDetail\.isMobile \? rail\.collapse : undefined\}/)
    // A callback, not an effect keyed on mainView: re-tapping the section you are
    // already on does not change mainView, so an effect would never fire.
    //
    // EVERY navigating control in the rail reports, and BOTH halves of that
    // sentence are derived rather than listed. A pinned count catches a control
    // that stops reporting; it cannot see a new navigating row in a new file,
    // which is the omission this defect class arrived by twice.
    const files = import.meta.glob('../apps/issue-radar/components/*.tsx', {
      query: '?raw', import: 'default', eager: true,
    }) as Record<string, string>
    const rail = await src('components/LeftRail.tsx')
    // The rail's control surface = LeftRail plus the components it renders. Derived
    // from its own imports, so a new section is in scope the moment the rail can
    // show it — while panes that merely live in the same directory (RefLink,
    // RefSheet, the detail panes) are structurally out of scope rather than
    // hand-excluded. Those navigate from pane CONTENT, by which point the rail is
    // already a 48px strip and reporting would be a no-op.
    const railParts = new Set(
      [...rail.matchAll(/^import (?:\{[^}]*\}|\w+)(?:, \{[^}]*\})? from '\.\/(\w+)'/gm)]
        .map((m) => `${m[1]}.tsx`).concat(['LeftRail.tsx']),
    )
    // The navigator NAMES come from the context's own surface, not spelled here: a
    // hardcoded alternation would re-arrive at this test's own critique one
    // navigator later.
    const ctxSource = await src('context.tsx')
    const navigators = [...new Set(
      [...ctxSource.matchAll(/^\s{2}(open[A-Z]\w*)\s*:\s*\(/gm)].map((m) => m[1]),
    )]
    expect(navigators.length, 'expected to derive the navigators from the context type')
      .toBeGreaterThanOrEqual(4)
    expect(railParts.size, 'expected to derive the rail surface from its imports')
      .toBeGreaterThan(4)
    const NAV = new RegExp(`\\b(${navigators.join('|')})\\(`)
    const unwired: string[] = []
    for (const [path, source] of Object.entries(files)) {
      const name = path.split('/').pop() as string
      if (!railParts.has(name)) continue
      source.split('\n').forEach((line, i) => {
        const code = line.trim()
        // Prose mentions the navigators too; only executable lines must report.
        if (code.startsWith('*') || code.startsWith('//') || code.startsWith('/*')) return
        if (NAV.test(line) && !line.includes('onNavigate?.()')) {
          unwired.push(`${name}:${i + 1}  ${code.slice(0, 80)}`)
        }
      })
    }
    expect(unwired, 'every rail control that navigates must collapse the narrow rail').toEqual([])
  })

  it('hides every main pane the full-width rail covers, dashboards included', async () => {
    const s = await shell()
    // The dashboards pane is the one with no list beside it, so it is easy to
    // miss: left ungated it keeps flex-1 next to a 100%-wide rail and resolves to
    // zero width, which is a tap that navigates to nothing.
    expect(s).toMatch(/overflow-y-auto scrollbar-none \$\{railFull \? 'hidden' : ''\}/)
  })

  it('gives the narrow expanded rail an explicit way to close', async () => {
    const s = await shell()
    const rail = await src('components/LeftRail.tsx')
    // Only while narrow AND open: on a desktop the drag handle already does this.
    expect(s).toMatch(/onCollapse=\{railFull \? rail\.collapse : undefined\}/)
    expect(rail).toMatch(/\{onCollapse && \(/)
    // Reuses the app-agnostic catalog key, so no locale gains a string.
    expect(rail).toContain("i18nT('app.collapse_sidebar')")
  })

  it('names the LIST in the Back control, not one item from it', async () => {
    await shell()
    // `changeRequestTitle` is singular ("Pull Request") and reads as the item you
    // are looking at; Back returns to the list, which is the plural.
    // The PR pane owns its own Back control now, so the label lives there.
    const pr = await src('components/PrDetail.tsx')
    expect(pr).toContain('label={terms.changeRequestPluralTitle}')
    expect(pr).not.toContain('label={terms.changeRequestTitle}')
  })

  it('keeps the drill-down state stable enough to host in a context', async () => {
    const hook = (await import('../hooks/useListDetailView.ts?raw')).default as string
    // Issue Radar puts this in its context value's dependency list. A fresh object
    // per render would recompute that memo every render, which would make the memo
    // guarding the app's largest context dead code.
    expect(hook).toMatch(/return useMemo\(\(\) => \(\{/)
    expect(hook).toMatch(/\}\), \[isMobile, detailOpen, openDetail, closeDetail\]\)/)
  })

  it('stacks the issue detail pane instead of dividing it', async () => {
    // Both detail panes, because they carry byte-identical declarations: fixing
    // one and leaving the other is how this defect would survive review.
    for (const pane of ['IssueDetail', 'PrDetail']) {
      const detail = await src(`components/${pane}.tsx`)
      // The shell handing these panes the whole viewport exposed their OWN
      // two-column layout: a 236px fixed sidebar beside the summary, description
      // and timeline left that column 34px at 390px, clipping text to two or
      // three characters a line. Nothing overflowed, so width — not overflow —
      // is the measure that catches it.
      expect(detail, `${pane}: the sidebar must not hold a fixed width while narrow`)
        .toMatch(/className="w-full sm:w-\[236px\]/)
      expect(detail, `${pane}: the body columns must stack while narrow`)
        .toMatch(/flex flex-col sm:flex-row gap-6 /)
      // The gutter that used to be spelled in this same string is asserted by
      // issueRadarNarrowGutter.test.ts instead. Pinning it here too made one
      // gutter decision require an edit in two test files, and made this
      // assertion fail with "the body columns must stack" when what actually
      // changed was the inset — a failure message that names the wrong cause is
      // worse than no second assertion.
      // The actions no longer compete with the title for width at all: they live
      // in the sticky bar, and the title is a full-width block above it. What
      // must hold is that the pane renders its header through the SHARED shell,
      // so the mechanism is not spelled once per pane.
      expect(detail, `${pane}: the header must come from the shared shell`)
        .toMatch(/<DetailHeader\b/)
      expect(detail, `${pane}: no per-pane header element may come back`)
        .not.toMatch(/<header className=/)
      // No unguarded fixed width may come back on either column.
      expect(detail, `${pane}: no ungated fixed sidebar width`).not.toMatch(/className="w-\[236px\]/)
    }
  })

  it('drops the pointer-only drag handles while narrow', async () => {
    const s = await shell()
    // Four render sites for three columns: the `list` handle is rendered once in
    // the issues view and again in the pulls view. All cost width a phone has
    // none of, and a drag handle does nothing on touch.
    expect((s.match(/\{!listDetail\.isMobile && \(/g) ?? []).length).toBe(4)
  })

  it('lays the collapsed rail ACROSS THE TOP while narrow, not down the side', async () => {
    const s = await shell()
    // The axis is the point. A ~48px strip is free on a desktop and a tenth of
    // the reading column on a phone, and horizontal is the one axis a phone has
    // nothing spare on — so while narrow the collapsed rail becomes a bar and
    // the panes below take the whole width.
    expect(s, 'expected a railBar derived from narrow AND collapsed')
      .toMatch(/const railBar = listDetail\.isMobile && rail\.collapsed/)
    // Direction flip on the shell is what puts the rail above the panes rather
    // than beside them; without it `horizontal` would just be a wide strip
    // sitting in a row, still stealing the width.
    expect(s, 'expected the shell to stack while the bar is up')
      .toMatch(/\$\{railBar \? 'flex-col' : ''\}/)
    expect(s, 'expected the bar mode to reach the rail').toMatch(/horizontal=\{railBar\}/)
  })

  it('keeps the bar narrow-only, so a desktop still gets the vertical strip', async () => {
    const s = await shell()
    // Gated on isMobile in the SAME expression as `collapsed`: a bar leaked onto
    // a desktop costs a whole row and buys nothing, since a strip's width is
    // free there. Measured shapes: 48x858 strip at 1280px, 390x46 bar at 390px.
    const decl = s.match(/const railBar = [^\n]*/)
    expect(decl, 'expected a railBar declaration').not.toBeNull()
    expect(decl![0]).toContain('listDetail.isMobile')
    const rail = await src('components/LeftRail.tsx')
    // The bar truncates the repo label; only the vertical card rotates it. A
    // rotated label inside the bar would read sideways in a horizontal row.
    const bar = rail.match(/if \(horizontal\) \{[\s\S]*?\n  \}/)
    expect(bar, 'expected a horizontal branch in CollapsedRail').not.toBeNull()
    expect(bar![0], 'the bar must not rotate its label').not.toContain('writingMode')
    expect(bar![0], 'the bar must truncate instead').toContain('truncate')
  })
})
