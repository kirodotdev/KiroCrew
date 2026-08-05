/**
 * The diff views' split-view buttons: each button's ACTIVE styling must track
 * the state it toggles.
 *
 * The active class (`text-accent bg-accent-subtle`) is gated on the plain state,
 * not its negation, so the button lights up in split mode and dims in unified
 * mode — matching the sibling toggles beside it. Gating on the negation would
 * invert the highlight.
 *
 * This is a class-string inversion, so neither tsc nor a render assertion on
 * the toggle's behaviour would catch a regression. Assert on the source.
 *
 * Two panels own a Monaco diff with its own split toggle — SidePanel (the Turn
 * Diff tab) and MarkdownPanel (the file viewer). Both are covered here: the
 * first fix landed in SidePanel only, and MarkdownPanel kept the inverted gate
 * for another day because nothing pinned it.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { join } from 'path'

const SIDE_PANEL = join(__dirname, '..', 'pages', 'chat', 'SidePanel.tsx')
const MARKDOWN_PANEL = join(__dirname, '..', 'components', 'MarkdownPanel.tsx')
const ACTIVE = "'text-accent bg-accent-subtle'"

/** The single line declaring the button that calls the given setter. */
function buttonLine(src: string, setter: string, file: string): string {
  const line = src.split('\n').find(l => l.includes(`onClick={() => ${setter}(`) && l.includes('<button'))
  if (!line) throw new Error(`no <button> line calling ${setter} found in ${file}`)
  return line
}

describe('SidePanel diff view controls', () => {
  const src = readFileSync(SIDE_PANEL, 'utf8')

  it('lights the split button up in split mode, not unified mode', () => {
    const line = buttonLine(src, 'setDiffSideBySide', 'SidePanel.tsx')
    expect(line).toContain(`\${diffSideBySide ? ${ACTIVE}`)
    expect(line).not.toContain(`\${!diffSideBySide ? ${ACTIVE}`)
  })

  it('keeps the line-numbers button gated the same way (no inversion)', () => {
    const line = buttonLine(src, 'setDiffLineNumbers', 'SidePanel.tsx')
    expect(line).toContain(`\${diffLineNumbers ? ${ACTIVE}`)
    expect(line).not.toContain(`\${!diffLineNumbers ? ${ACTIVE}`)
  })
})

describe('MarkdownPanel diff view controls', () => {
  const src = readFileSync(MARKDOWN_PANEL, 'utf8')

  it('lights the split button up in split mode, not unified mode', () => {
    const line = buttonLine(src, 'setDiffSplit', 'MarkdownPanel.tsx')
    // barIconBtn(on) applies the active class when `on` is true.
    expect(line).toContain('barIconBtn(diffSplit)')
    expect(line).not.toContain('barIconBtn(!diffSplit)')
  })

  it('reports the same state to assistive tech as it paints', () => {
    const line = buttonLine(src, 'setDiffSplit', 'MarkdownPanel.tsx')
    expect(line).toContain('aria-pressed={diffSplit}')
    expect(line).not.toContain('aria-pressed={!diffSplit}')
  })

  it('keeps renderSideBySide authoritative below Monaco 900px breakpoint', () => {
    // Monaco's useInlineViewWhenSpaceIsLimited defaults to true and silently
    // forces the inline view under renderSideBySideInlineBreakpoint (900px).
    // This panel is always narrower than that, so without the opt-out the
    // split toggle has no visible effect.
    //
    // Scope this to the options object, not the whole file: the prop's JSDoc
    // names the same option, so a file-wide search would still pass after the
    // option was deleted from the editor config.
    const line = src.split('\n').find(l => l.includes('renderSideBySide: sideBySide'))
    if (!line) throw new Error('no options line passing renderSideBySide found in MarkdownPanel.tsx')
    expect(line).toContain('useInlineViewWhenSpaceIsLimited: false')
  })
})
