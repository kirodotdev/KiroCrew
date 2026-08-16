import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const src = (f: string) => readFile(join(__dirname, '..', 'pages', f), 'utf8')

describe('HooksPage matcher field at narrow pane widths', () => {
  it('gives the matcher field a basis so its siblings wrap instead of crushing it', async () => {
    const s = await src('HooksPage.tsx')
    const row = s.match(/matcher_tool_filter_e_g_fs_write_git[\s\S]{0,400}/)
    expect(row, 'expected the matcher row').not.toBeNull()
    // The shared Input is `flex-1 min-w-0` (flex-basis: 0%), so its hypothetical
    // main size is zero and flex line-breaking keeps every non-shrinking sibling
    // on line 1 -- the field absorbs the whole shortfall (45px at a 360px pane).
    const m = s.match(/<Input className="([^"]*)" placeholder=\{isToolHook/)
    expect(m, 'the matcher Input must carry a basis override').not.toBeNull()
    expect(m![1], 'expected a full line while narrow').toContain('basis-full')
    expect(m![1], 'expected intrinsic sizing above the breakpoint').toContain('sm:basis-auto')
  })

  it('leaves the timeout group and both actions non-shrinking', async () => {
    const s = await src('HooksPage.tsx')
    // The fix works by letting THESE wrap. If they became shrinkable instead,
    // the row would stop wrapping and the field would be squeezed again.
    expect(s).toMatch(/text-\[13px\] text-muted shrink-0/)
  })

  it('does not disturb the hook-name row, which was already sound', async () => {
    const s = await src('HooksPage.tsx')
    // Measured min 196px across the same 320-760px sweep -- it has one sibling,
    // not three. A basis override here would be churn with no defect behind it.
    expect(s).toMatch(/<Input placeholder=\{i18nT\('pages\.hooksPage\.hook_name'\)\}/)
  })
})
