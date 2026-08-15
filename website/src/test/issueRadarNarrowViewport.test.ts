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
    expect(s).toMatch(/onBack=\{listDetail\.closeDetail\}/)
    // One Back per drill-down view; the rail's nav rows switch section without
    // leaving the detail, so this is the only way back.
    expect((s.match(/\{narrowBack\(/g) ?? []).length).toBe(3)
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
    // EVERY navigating control reports, headers AND section bodies. A partial
    // wiring is the actual defect shape: the first pass covered the four section
    // headers and the crew chips and missed the Dashboards header and all four
    // section bodies, which made those taps read as dead on a phone.
    const rail = await src('components/LeftRail.tsx')
    expect((rail.match(/onNavigate\?\.\(\)/g) ?? []).length,
      'rail headers that navigate must report it').toBe(6)
    for (const f of ['DashboardsSection', 'FiltersSection', 'PrFiltersSection', 'SettingsSection']) {
      const body = await src(`components/${f}.tsx`)
      expect(body, `${f} must accept the callback`).toMatch(/onNavigate\?: \(\) => void/)
      expect(body, `${f} must report its navigation`).toMatch(/onNavigate\?\.\(\)/)
      expect(rail, `${f} must be handed the callback`).toContain(`<${f} onNavigate={onNavigate} />`)
    }
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
    const s = await shell()
    // `changeRequestTitle` is singular ("Pull Request") and reads as the item you
    // are looking at; Back returns to the list, which is the plural.
    expect(s).toContain('narrowBack(terms.changeRequestPluralTitle)')
    expect(s).not.toContain('narrowBack(terms.changeRequestTitle)')
  })

  it('keeps the drill-down state stable enough to host in a context', async () => {
    const hook = (await import('../hooks/useListDetailView.ts?raw')).default as string
    // Issue Radar puts this in its context value's dependency list. A fresh object
    // per render would recompute that memo every render, which would make the memo
    // guarding the app's largest context dead code.
    expect(hook).toMatch(/return useMemo\(\(\) => \(\{/)
    expect(hook).toMatch(/\}\), \[isMobile, detailOpen, openDetail, closeDetail\]\)/)
  })

  it('drops the pointer-only drag handles while narrow', async () => {
    const s = await shell()
    // Four render sites for three columns: the `list` handle is rendered once in
    // the issues view and again in the pulls view. All cost width a phone has
    // none of, and a drag handle does nothing on touch.
    expect((s.match(/\{!listDetail\.isMobile && \(/g) ?? []).length).toBe(4)
  })
})
