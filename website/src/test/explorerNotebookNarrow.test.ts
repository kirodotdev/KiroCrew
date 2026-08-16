import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const app = (p: string) => readFile(join(__dirname, '..', 'apps', p), 'utf8')

// Both apps put a fixed-width navigator beside the pane that carries the primary
// text, with nothing gating it on viewport width. At 390px that left the file
// viewer 106px (inside an `overflow:hidden` split, so unreachable rather than
// merely cramped) and the note editor 130px. The fix is the same in both: the
// navigator becomes a drawer reached from a control at the TOP, so no horizontal
// space is reserved for it and the reading pane owns the full width.
describe('file-explorer at phone widths', () => {
  it('reaches the tree from a top bar instead of a side pane', async () => {
    const s = await app('file-explorer/FileExplorerPage.tsx')
    expect(s, 'expected a viewport hook').toContain("useIsMobile")
    expect(s, 'expected drawer state').toMatch(/const treeBar = isMobile && !treeOpen/)
    expect(s, 'expected a full-width expanded tree').toMatch(/const treeFull = isMobile && treeOpen/)
    expect(s, 'the bar must be a Btn primitive').toMatch(/<Btn[\s\S]{0,200}mc-fe-treebar/)
    expect(s, 'the bar must announce its disclosure state').toMatch(/aria-expanded=\{treeOpen\}/)
  })

  it('turns the split into a column and drops the pointer-only handle', async () => {
    const s = await app('file-explorer/FileExplorerPage.tsx')
    expect(s, 'expected the row to stack').toMatch(/mc-fe-split\$\{isMobile \? ' is-stacked' : ''\}/)
    // The resizer is mouse-drag-only. Rendered on touch it costs width and does nothing.
    expect(s, 'the resizer must not render while narrow').toMatch(/\{!isMobile && <div className="mc-fe-resizer"/)
    expect(s, 'the tree must take the whole width when open')
      .toMatch(/width: treeFull \? '100%' : leftWidth/)
  })

  it('closes the drawer on pick, so the full-width tree is not a one-way door', async () => {
    const s = await app('file-explorer/FileExplorerPage.tsx')
    const fn = s.match(/const openFile = useCallback\([\s\S]*?\n  \}, \[[^\]]*\]\)/)
    expect(fn, 'expected openFile').not.toBeNull()
    expect(fn![0]).toContain('if (isMobile) setTreeOpen(false)')
    expect(fn![0], 'the callback must depend on isMobile').toContain('isMobile]')
  })

  it('stacks the divider and hides panes without unmounting them', async () => {
    const css = await app('file-explorer/styles.ts')
    expect(css, 'expected a stacked modifier').toMatch(/\.mc-fe-split\.is-stacked \{ flex-direction:column; \}/)
    // A border-right would draw a stray vertical rule once the row is a column.
    expect(css).toMatch(/is-stacked > \.mc-fe-left \{ border-right:none; border-bottom:/)
    expect(css, 'panes must be hidden, not removed').toMatch(/\.mc-fe-left\.is-hidden, \.mc-fe-right\.is-hidden \{ display:none; \}/)
    // styles.ts is a template literal: a backtick in the CSS ends it early.
    const body = css.split('`')[1] ?? ''
    expect(body.includes('`'), 'no backtick may appear inside the CSS template').toBe(false)
  })
})

describe('md-notebook at phone widths', () => {
  it('drives the existing panel toggle from the viewport', async () => {
    const s = await app('md-notebook/MdNotebookPage.tsx')
    expect(s, 'expected a viewport hook').toContain('useIsMobile')
    expect(s, 'expected a derived visibility').toMatch(/const panelShown = isMobile \? \(narrowPanelOpen \|\| !activePath\) : panelOpen/)
    expect(s, 'the panel must render from the derived value').toMatch(/\{panelShown && \(/)
    expect(s, 'the toggle icon must follow it').toMatch(/\{panelShown \? <PanelLeftLight/)
    expect(s, 'the aria label must follow it').toMatch(/aria-label=\{\s*\n?\s*panelShown/)
  })

  it('never writes the narrow state into the stored desktop preference', async () => {
    const s = await app('md-notebook/MdNotebookPage.tsx')
    const fn = s.match(/const togglePanel = useCallback\([\s\S]*?\n  \}, \[[^\]]*\]\)/)
    expect(fn, 'expected togglePanel').not.toBeNull()
    // The narrow branch returns BEFORE savePref: a phone visit must not change
    // what the desktop shows on the next load.
    expect(fn![0]).toMatch(/if \(isMobile\) \{ setNarrowPanelOpen\(v => !v\); return \}/)
    const narrow = fn![0].slice(0, fn![0].indexOf('setPanelOpen'))
    expect(narrow.includes('savePref'), 'the narrow branch must not persist').toBe(false)
  })

  it('gives the open panel the full width and closes it on pick', async () => {
    const s = await app('md-notebook/MdNotebookPage.tsx')
    expect(s, 'expected a full-width panel while narrow')
      .toMatch(/width: isMobile \? '100%' : `\$\{panelW\}px`/)
    const fn = s.match(/const openNote = useCallback\([\s\S]*?\n  \}, \[[^\]]*\]\)/)
    expect(fn, 'expected openNote').not.toBeNull()
    expect(fn![0]).toContain('if (isMobile) setNarrowPanelOpen(false)')
    expect(fn![0], 'the callback must depend on isMobile').toContain('isMobile')
  })

  it('does not leave the empty state pointing at a control it just hid', async () => {
    const s = await app('md-notebook/MdNotebookPage.tsx')
    // The empty state reads "Select a note, or create one with the + button" and
    // that + lives INSIDE the drawer. Closed, it instructs an off-screen control.
    expect(s).toMatch(/const panelShown = isMobile \? \(narrowPanelOpen \|\| !activePath\) : panelOpen/)
  })

  it('drops keyboard-only advice from what is now the mobile landing view', async () => {
    const s = await app('file-explorer/FileViewer.tsx')
    expect(s, 'expected the viewport hook').toContain('useIsMobile')
    expect(s).toMatch(/subtitle=\{isMobile \? undefined : i18nT\('apps\.fileExplorer\.fileViewer\.tip_ctrl_cmd_f_to_search'\)\}/)
  })
})
