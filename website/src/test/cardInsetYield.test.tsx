import { render } from '@testing-library/react'
import { Card } from '../components/ui'

// `Card`'s base inset is breakpoint-scoped (`md:px-5`), and twMerge only collapses
// classes that collide at the SAME breakpoint. So a caller's bare `p-3` does NOT
// displace it: without the yield in `Card`, the two sit side by side and the card
// widens back to 20px horizontal from `md` up -- 12px on a phone, 20px on a
// desktop, from a call site that says 12px.
//
// These assertions render, rather than scanning source text, for one specific
// reason: a lexical scan cannot see `className={cond ? 'p-3' : ''}`, and two such
// `Card` call sites already exist in this repo (KiroPrerequisiteGate). The last
// case here is that shape.

const classesOf = (el: HTMLElement) => (el.className || '').split(/\s+/)

describe('Card yields its own inset on whichever axis the caller sets', () => {
  it('keeps the narrow-first base inset when the caller sets no padding', () => {
    const { container } = render(<Card>x</Card>)
    const cls = classesOf(container.firstElementChild as HTMLElement)
    expect(cls).toContain('px-2')
    expect(cls).toContain('md:px-5')
    expect(cls).toContain('py-5')
  })

  it('drops BOTH base axes for a caller `p-3`, so 12px holds at every width', () => {
    const { container } = render(<Card className="p-3 mb-0">x</Card>)
    const cls = classesOf(container.firstElementChild as HTMLElement)
    expect(cls).toContain('p-3')
    expect(cls, 'md:px-5 surviving is the 12px->20px desktop regression')
      .not.toContain('md:px-5')
    expect(cls).not.toContain('px-2')
    expect(cls).not.toContain('py-5')
  })

  it('yields only the axis the caller names', () => {
    const x = classesOf(render(<Card className="px-4">x</Card>)
      .container.firstElementChild as HTMLElement)
    expect(x).not.toContain('md:px-5')
    expect(x, 'the vertical axis was not overridden, so it keeps the base').toContain('py-5')

    const y = classesOf(render(<Card className="py-2">x</Card>)
      .container.firstElementChild as HTMLElement)
    expect(y).not.toContain('py-5')
    expect(y, 'the horizontal axis was not overridden, so it keeps the base')
      .toContain('md:px-5')
  })

  it('leaves a `md:`-prefixed override to twMerge, which handles it correctly', () => {
    // Same breakpoint as the base, so they genuinely collide and the caller wins.
    const cls = classesOf(render(<Card className="md:px-8">x</Card>)
      .container.firstElementChild as HTMLElement)
    expect(cls).toContain('md:px-8')
    expect(cls).not.toContain('md:px-5')
    expect(cls, 'the narrow value is untouched by a md: override').toContain('px-2')
  })

  it('covers a COMPUTED className, which no source-text scan can see', () => {
    const compact = true
    const cls = classesOf(render(<Card className={compact ? 'p-3' : ''}>x</Card>)
      .container.firstElementChild as HTMLElement)
    expect(cls).toContain('p-3')
    expect(cls, 'the yield is decided from the rendered string, not the source')
      .not.toContain('md:px-5')
  })

  it('is the number the capability panes cancel, across files', async () => {
    // The two numbers are one number. `SkillsTab` and `SteeringTab` pull their
    // pane out by exactly the inset `Card` puts in, so the pane reaches the card
    // edge; changing one without the other pushes the pane past the border or
    // leaves it short. This is the only place that relationship is checked.
    //
    // Card's side is read from the RENDERED class list rather than from ui.tsx's
    // source: a source regex pinned to the old literal spelling broke silently
    // the moment the inset moved into a variable, which is the same lexical
    // fragility this file's other assertions exist to avoid.
    const base = classesOf(render(<Card>x</Card>).container.firstElementChild as HTMLElement)
    const inset = base.find((c) => /^px-[\d.]+$/.test(c))
    expect(inset, 'Card should carry an unprefixed narrow horizontal inset').toBeTruthy()
    const cardInset = inset!.replace('px-', '')

    const { readFile } = await import('node:fs/promises')
    const { join } = await import('node:path')
    for (const file of ['SkillsTab.tsx', 'SteeringTab.tsx']) {
      const src = await readFile(join(__dirname, '..', 'pages', 'overview', file), 'utf8')
      const pane = src.match(/const PANE_SHELL_CLASS = 'flex gap-3 -mx-([\d.]+) md:mx-0/)
      expect(pane, `${file} should full-bleed the pane shell below md`).toBeTruthy()
      expect(pane![1], `${file}: pane -mx-${pane![1]} must match Card px-${cardInset}`)
        .toBe(cardInset)
    }
  })
})
