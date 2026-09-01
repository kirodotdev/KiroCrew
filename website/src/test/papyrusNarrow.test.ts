import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const src = () => readFile(join(__dirname, '..', 'apps', 'papyrus', 'PapyrusPage.tsx'), 'utf8')

// Four surfaces competed for one row: a 50% source column holding a 176px `w-44`
// file tree beside the editor, a PDF column, and a 420px co-author panel that
// alone exceeds a phone viewport. At 390px the editor -- the pane carrying the
// text being written -- measured 19px.
describe('papyrus at phone widths', () => {
  it('turns both nested rows into columns', async () => {
    const s = await src()
    const rows = s.match(/flex flex-1 min-h-0 \$\{isMobile \? 'flex-col' : ''\}/g) || []
    expect(rows.length, 'both the outer and the inner row must stack').toBe(2)
  })

  it('drops the percentage width so the source column is not half a phone', async () => {
    const s = await src()
    expect(s).toMatch(/width: isMobile \? '100%' : `\$\{SOURCE_PANE_PERCENT\}%`/)
  })

  it('reaches the file tree from a top bar instead of a 176px side pane', async () => {
    const s = await src()
    expect(s, 'the bar must be a Btn primitive with a disclosure state')
      .toMatch(/<Btn[\s\S]{0,160}aria-expanded=\{treeOpen\}/)
    expect(s, 'the bar must reuse the existing Files label')
      .toContain("i18nT('apps.papyrus.fileTree.files')")
    // Full width when open, no width at all when closed -- and `w-44` must not
    // survive into the narrow branch.
    expect(s).toMatch(/\? `w-full shrink-0 max-h-\[40vh\] overflow-y-auto \$\{treeOpen \? '' : 'hidden'\}`\s*\n?\s*: 'w-44 shrink-0'/)
  })

  it('bounds the stacked panes in vh, since a percentage would not resolve', async () => {
    const s = await src()
    expect(s, 'tree bound').toMatch(/max-h-\[40vh\]/)
    expect(s, 'pdf bound').toMatch(/max-h-\[45vh\]/)
    expect(s, 'a percentage bound would be inert here').not.toMatch(/max-h-\[\d+%\]/)
  })

  it('turns the divider with the axis', async () => {
    const s = await src()
    // A left border draws a stray vertical rule once the row is a column.
    expect(s).toMatch(/border-t border-border \$\{narrowChat \? 'hidden' : ''\}`\s*\n?\s*: 'flex-1 border-l border-border'/)
  })

  it('moves BOTH co-author widths, not just the inner one', async () => {
    const s = await src()
    // The motion wrapper is animated and content-sized. A percentage on the
    // child alone resolves against a box that hugs its content, so the panel
    // comes out NARROWER than the pixel width it replaced.
    expect(s, 'the animated wrapper width must move')
      .toMatch(/animate=\{\{ width: isMobile \? '100%' : CHAT_PANEL_WIDTH, opacity: 1 \}\}/)
    expect(s, 'the inner fixed width must move too')
      .toMatch(/style=\{\{ width: isMobile \? '100%' : CHAT_PANEL_WIDTH \}\}/)
    expect(s, 'the wrapper must own the pane while narrow')
      .toMatch(/isMobile \? 'flex-1' : 'shrink-0'/)
  })

  it('lets the co-author panel own the pane by stepping the others aside', async () => {
    const s = await src()
    expect(s).toMatch(/const narrowChat = isMobile && chatOpen/)
    const hides = s.match(/\$\{narrowChat \? 'hidden' : ''\}/g) || []
    expect(hides.length, 'both the source and the PDF column must step aside').toBe(2)
  })

  it('closes the drawer on pick, so the full-width tree is not a one-way door', async () => {
    const s = await src()
    const fn = s.match(/const openFile = useCallback\(async \(path: string\) => \{[\s\S]*?\n  \}, \[[^\]]*\]\)/)
    expect(fn, 'expected openFile').not.toBeNull()
    expect(fn![0]).toContain('if (isMobile) setTreeOpen(false)')
    expect(fn![0], 'the callback must depend on isMobile').toContain('isMobile]')
  })

  it('keeps the viewport-anchored sessions opener out of an embedded host', async () => {
    const s = await readFile(join(__dirname, '..', 'pages', 'chat', 'ChatPageView.tsx'), 'utf8')
    // The floating opener is `fixed top-[42px] left-2`, i.e. anchored to the
    // VIEWPORT rather than to the host's pane, so inside Papyrus's co-author panel
    // it lands on the toolbar's back button -- two overlapping tap targets on the
    // app's primary exit. A positioned ancestor cannot contain a `fixed` child, so
    // the gate has to be on the render condition.
    expect(s).toMatch(/\{isMobile && !embedded && !sidebarOpen && !inlineSidePanelShowing/)
  })

  it('does not repeat the Files heading directly under the disclosure bar', async () => {
    const s = await readFile(join(__dirname, '..', 'apps', 'papyrus', 'FileTree.tsx'), 'utf8')
    expect(s, 'expected the viewport hook').toContain('useIsMobile')
    // Hidden from sight but kept for assistive tech, and the spacer keeps the
    // new-file button in place.
    expect(s).toMatch(/\$\{isMobile \? 'sr-only' : ''\}/)
    expect(s).toMatch(/\{isMobile && <span className="flex-1" \/>\}/)
  })
})
