/**
 * The System tab's card layouts must actually narrow.
 *
 * ServicesTab set `style={{ columns: 3 }}` inline next to
 * `max-[900px]:columns-2 max-[600px]:columns-1`. An inline style outranks any
 * stylesheet rule, so both responsive classes were dead. Measured in a browser
 * at a 390px viewport: the container still reported `column-count: 3` and each
 * card got **119px**, versus 390px once the declaration is a class. That 119px
 * is what squeezed the value column to roughly 24px, which is the width at
 * which a metric reading visibly splits mid-number.
 *
 * PerformanceTab's stats grid was `grid-cols-2` with no narrow override, so it
 * held two columns at 390px.
 *
 * happy-dom does no layout, so these pin the declaration form rather than the
 * resulting geometry: the defect here IS the form (inline vs class), and that is
 * exactly what a source assertion can see.
 */
import { describe, it, expect } from 'vitest'

async function read(mod: string): Promise<string> {
  return (await import(mod)).default as string
}

describe('ServicesTab card columns', () => {
  it('declares the column count as a class so the responsive overrides can win', async () => {
    const src = await read('../pages/system/ServicesTab.tsx?raw')
    const el = src.match(/className="columns-3[^"]*"/)
    expect(el, 'expected a columns-3 class on the card container').not.toBeNull()
    expect(el![0]).toContain('max-[600px]:columns-1')
    expect(el![0]).toContain('max-[900px]:columns-2')
  })

  it('does not set the column count inline, which would outrank those classes', async () => {
    const src = await read('../pages/system/ServicesTab.tsx?raw')
    // Strip JSX comments first: the comment explaining this rule necessarily
    // quotes the very form it forbids, and matching that is a false positive.
    const code = src.replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
    expect(code).not.toMatch(/style=\{\{\s*columns:/)
  })
})

describe('PerformanceTab stats grid', () => {
  it('starts at one column and widens, rather than starting at two', async () => {
    const src = await read('../pages/system/PerformanceTab.tsx?raw')
    const el = src.match(/className="grid grid-cols-1[^"]*"/)
    expect(el, 'expected the stats grid to start at grid-cols-1').not.toBeNull()
    expect(el![0]).toContain('sm:grid-cols-2')
    expect(el![0]).toContain('lg:grid-cols-3')
    // An unqualified two-column stats grid is the defect: it applies at every
    // width. Matched by the stats grid's own `gap-x-6` rather than by a bare
    // `grid grid-cols-2`, because the resource rail below is legitimately 2x2
    // while narrow and would otherwise trip this.
    expect(src).not.toMatch(/className="grid grid-cols-2 gap-x-6/)
  })
})

describe('PerformanceTab resource rail', () => {
  it('applies the fixed 196px rail only from sm up, as a class', async () => {
    const src = await read('../pages/system/PerformanceTab.tsx?raw')
    const el = src.match(/className="grid gap-4 sm:grid-cols-\[196px_minmax\(0,1fr\)\]"/)
    expect(el, 'expected the pane to be one column until sm').not.toBeNull()
  })

  it('does not set the pane columns inline, which would apply at every width', async () => {
    const src = await read('../pages/system/PerformanceTab.tsx?raw')
    // Same false-positive guard as above: the comment quotes what it forbids.
    const code = src.replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
    expect(code).not.toMatch(/gridTemplateColumns/)
    // The 196px literal must not reappear as an unconditional inline value.
    expect(code).not.toMatch(/style=\{\{[^}]*196px/)
  })

  it('lays the four tiles out 2x2 while narrow instead of stacking them', async () => {
    const src = await read('../pages/system/PerformanceTab.tsx?raw')
    const nav = src.match(/className="grid grid-cols-2 gap-1\.5 sm:flex sm:flex-col"/)
    expect(nav, 'expected the rail to be 2x2 below sm and a column above').not.toBeNull()
    // Stacking all four sparkline tiles costs 362px of rail before the graph;
    // 2x2 is 178px. An unconditional flex-col is that regression.
    expect(src).not.toMatch(/className="flex flex-col gap-1\.5"/)
  })
})


