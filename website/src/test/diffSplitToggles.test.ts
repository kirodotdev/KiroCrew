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
 * SidePanel (the Turn Diff tab) owns a Pierre diff with its own split BUTTON, so
 * the class-string gate below is the only thing that can catch an inversion
 * there. MarkdownPanel's equivalent moved into the ⋯ menu as a checkbox row —
 * its state is `aria-checked`, which a render assertion can read directly, so
 * that half lives in MarkdownPanelCoverage.test.tsx instead.
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

  it('no longer carries a header split button to invert', () => {
    // The control moved into the ⋯ menu as a `menuitemcheckbox` row, so its
    // active state is `aria-checked` rather than a class string — asserted
    // behaviourally in MarkdownPanelCoverage.test.tsx ("offers split/unified as
    // a menu row only once a diff is on screen"), which beats a source grep.
    // This asserts only that the button did not come back unnoticed.
    expect(src).not.toContain('barIconBtn(diffSplit)')
    expect(src).toContain('onToggleDiffSplit')
  })
})
