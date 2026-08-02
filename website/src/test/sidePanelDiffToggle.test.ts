/**
 * The side panel's diff view controls: the split-view button's ACTIVE styling
 * must track the state it toggles.
 *
 * The button was styled active (`text-accent bg-accent-subtle`) on
 * `!diffSideBySide`, so the highlight read backwards: it lit up in unified
 * mode and went dim in split mode — the opposite of the line-numbers button
 * sitting next to it, which is gated on the plain state.
 *
 * This is a class-string inversion, so neither tsc nor a render assertion on
 * the toggle's behaviour would catch a regression. Assert on the source.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { join } from 'path'

const SIDE_PANEL = join(__dirname, '..', 'pages', 'chat', 'SidePanel.tsx')
const ACTIVE = "'text-accent bg-accent-subtle'"

/** The single line declaring the button that calls the given setter. */
function buttonLine(src: string, setter: string): string {
  const line = src.split('\n').find(l => l.includes(`onClick={() => ${setter}(`) && l.includes('<button'))
  if (!line) throw new Error(`no <button> line calling ${setter} found in SidePanel.tsx`)
  return line
}

describe('SidePanel diff view controls', () => {
  const src = readFileSync(SIDE_PANEL, 'utf8')

  it('lights the split button up in split mode, not unified mode', () => {
    const line = buttonLine(src, 'setDiffSideBySide')
    expect(line).toContain(`\${diffSideBySide ? ${ACTIVE}`)
    expect(line).not.toContain(`\${!diffSideBySide ? ${ACTIVE}`)
  })

  it('keeps the line-numbers button gated the same way (no inversion)', () => {
    const line = buttonLine(src, 'setDiffLineNumbers')
    expect(line).toContain(`\${diffLineNumbers ? ${ACTIVE}`)
    expect(line).not.toContain(`\${!diffLineNumbers ? ${ACTIVE}`)
  })
})
