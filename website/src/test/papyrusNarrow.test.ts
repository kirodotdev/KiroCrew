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
    // The 50% share is gone entirely: the PDF and co-author columns now own
    // persisted pixel widths and the source column is `flex-1`, so it is never a
    // fixed fraction of a phone viewport.
    expect(s, 'no fixed percentage share may survive').not.toMatch(/SOURCE_PANE_PERCENT/)
    expect(s).toMatch(/className=\{`flex flex-col flex-1 min-h-0 min-w-0 \$\{narrowChat \? 'hidden' : ''\}`\}/)
  })

  it('reaches the file tree from a top bar instead of a 176px side pane', async () => {
    const s = await src()
    expect(s, 'the bar must be a Btn primitive with a disclosure state')
      .toMatch(/<Btn[\s\S]{0,160}aria-expanded=\{treeOpen\}/)
    expect(s, 'the bar must reuse the existing Files label')
      .toContain("i18nT('apps.papyrus.fileTree.files')")
    // Full width when open, no width at all when closed. The desktop branch is
    // now a dragged width, so what must not survive into the NARROW branch is any
    // side-pane width at all -- the tree is a drawer there, and `tree.width` is
    // applied only when `!isMobile`.
    expect(s).toMatch(/\? `w-full shrink-0 max-h-\[40vh\] overflow-y-auto \$\{treeOpen \? '' : 'hidden'\}`\s*\n?\s*: 'shrink-0 overflow-hidden'/)
    expect(s, 'no hardcoded 176px tree column may remain').not.toMatch(/'w-44 shrink-0'/)
    expect(s, 'the dragged width is desktop-only').toMatch(/width: isMobile \? undefined : tree\.width/)
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
    expect(s).toMatch(/border-t border-border \$\{narrowChat \? 'hidden' : ''\}`\s*\n?\s*: 'shrink-0 border-l border-border'/)
  })

  it('moves BOTH co-author widths, not just the inner one', async () => {
    const s = await src()
    // The motion wrapper is animated and content-sized. A percentage on the
    // child alone resolves against a box that hugs its content, so the panel
    // comes out NARROWER than the pixel width it replaced.
    //
    // Both read `chatWidth`, the width after the room-aware ceiling, rather than
    // the hook's raw `chat.width`: a wrapper on one and the ceiling on the other
    // would animate to a width the content box never takes.
    expect(s, 'the animated wrapper width must move')
      .toMatch(/animate=\{\{ width: isMobile \? '100%' : chatWidth, opacity: 1 \}\}/)
    expect(s, 'the inner fixed width must move too')
      .toMatch(/style=\{\{ width: isMobile \? '100%' : chatWidth \}\}/)
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
    const s = await readFile(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf8')
    // The floating opener is `fixed top-[42px] left-2`, i.e. anchored to the
    // VIEWPORT rather than to the host's pane, so inside Papyrus's co-author panel
    // it lands on the toolbar's back button -- two overlapping tap targets on the
    // app's primary exit. A positioned ancestor cannot contain a `fixed` child, so
    // the gate has to be on the render condition.
    expect(s).toMatch(/\{isMobile && !embedded && !sidebarOpen && !inlineSidePanelShowing/)
  })

  it('keeps the co-author open flag a desktop-only preference', async () => {
    const s = await src()
    // The panel covers the pane while narrow, so a flag stored on a desktop must
    // not reopen it on a phone, and dismissing it on a phone -- the only way back
    // to the text there -- must not rewrite the desktop layout.
    expect(s, 'the mount read must ignore the flag while narrow')
      .toMatch(/useState\(\(\) => \(isMobile \? false : loadChatOpen\(\)\)\)/)
    expect(s, 'the paper-switch read must ignore it too')
      .toMatch(/setChatOpen\(isMobileRef\.current \? false : loadChatOpen\(\)\)/)
    expect(s, 'and the write must be skipped while narrow')
      .toMatch(/if \(isMobile\) return[\s\S]{0,900}safeSetItem\(CHAT_OPEN_KEY/)
    // Widening past the breakpoint re-runs the effect while `chatOpen` still holds
    // the closed state the narrow mount forced. Writing there would erase a stored
    // preference on a plain window drag, so the transition reads it back instead.
    expect(s, 'the breakpoint side must be carried across runs')
      .toMatch(/const wasMobile = wasMobileRef\.current\s*\n\s*wasMobileRef\.current = isMobile/)
    expect(s, 'crossing back to desktop must restore, not write')
      .toMatch(/if \(wasMobile\) \{\s*\n\s*setChatOpen\(loadChatOpen\(\)\)\s*\n\s*return\s*\n\s*\}/)
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
