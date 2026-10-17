import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const html = () => readFile(join(__dirname, '..', '..', 'index.html'), 'utf8')

// Without this, the keyboard shrinks only the VISUAL viewport: the layout viewport
// keeps its full height, so `vh` / `dvh` / `fixed inset-0` lay out against a window
// whose bottom the user cannot see. A focused full-screen overlay then strands its
// own content under the keyboard with no gesture to reach it -- measured on the
// command palette, whose panel sits 101-692px of an 844px layout viewport while the
// keyboard covers roughly the bottom 350px.
describe('mobile software keyboard', () => {
  it('resizes the layout viewport, not just the visual one', async () => {
    const s = await html()
    const m = s.match(/<meta name="viewport" content="([^"]*)"/)
    expect(m, 'expected a viewport meta').not.toBeNull()
    expect(m![1]).toContain('interactive-widget=resizes-content')
  })

  it('keeps the rest of the viewport contract intact', async () => {
    const s = await html()
    const m = s.match(/<meta name="viewport" content="([^"]*)"/)
    expect(m![1]).toContain('width=device-width')
    expect(m![1]).toContain('initial-scale=1')
    // `resizes-visual` is the default this replaces; it must not be re-stated.
    expect(m![1]).not.toContain('resizes-visual')
  })
})
