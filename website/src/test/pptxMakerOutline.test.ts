import { describe, expect, it } from 'vitest'
import { hasTbd, parseOutline } from '../apps/pptx-maker/outline'

const OUTLINE = `# Coffee brewing basics

## Methods

- [pour-over] Pour-over gives the cleanest cup
  - body: slow pour through a paper filter
  - visual: a three-step flow with an icon per step
  - evidence: brief Sources, SCA guide
- [french-press] French press is the most forgiving
  - body: [TBD]

## Wrap-up
A closing note.
- [pick-one] Pick one and brew it this week
`

describe('parseOutline', () => {
  it('lifts the leading # line out as the deck title', () => {
    expect(parseOutline(OUTLINE).title).toBe('Coffee brewing basics')
  })

  it('groups slides under their chapters in document order', () => {
    const outline = parseOutline(OUTLINE)
    expect(outline.slideCount).toBe(3)
    expect(outline.chapters.map((c) => c.section?.title)).toEqual(['Methods', 'Wrap-up'])
    expect(outline.chapters[0].slides.map((s) => s.slug)).toEqual(['pour-over', 'french-press'])
  })

  it('reads the three fixed sub-items onto the slide', () => {
    const slide = parseOutline(OUTLINE).chapters[0].slides[0]
    expect(slide.message).toBe('Pour-over gives the cleanest cup')
    expect(slide.body).toBe('slow pour through a paper filter')
    expect(slide.visual).toBe('a three-step flow with an icon per step')
    expect(slide.evidence).toBe('brief Sources, SCA guide')
  })

  it('keeps any other line as prose instead of dropping it', () => {
    expect(parseOutline(OUTLINE).chapters[1].prose.map((p) => p.text)).toEqual(['A closing note.'])
  })

  it('reads a crafted title line in linear time and trims its tail', () => {
    // `# a` + spaces + `x`: a lazy group before `\s*$` was quadratic here.
    const crafted = (n: number) => '# a' + ' '.repeat(n) + 'x   \n- [s] S'
    // Growth, not a duration: quadrupling the line costs a linear scan ~4x and
    // the quadratic pattern ~16x, so the 10x bound sits between the two. Each
    // size is the cheapest of several samples, since runner noise only adds time.
    // Measured before the long line below, which a regression would never finish.
    const bestOf = (n: number) => {
      const text = crafted(n)
      let best = Infinity
      for (let sample = 0; sample < 5; sample++) {
        const t0 = performance.now()
        for (let i = 0; i < 20; i++) parseOutline(text)
        best = Math.min(best, performance.now() - t0)
      }
      return best
    }
    bestOf(4096) // warm-up: let the JIT settle before either measurement
    const small = bestOf(4096)
    const big = bestOf(16384)
    expect(big).toBeLessThan(Math.max(small, 1) * 10)
    const title = parseOutline(crafted(200_000)).title
    expect(title?.startsWith('a')).toBe(true)
    expect(title?.endsWith('x')).toBe(true)
  })

  it('has no title when the first line is not a level-1 heading', () => {
    expect(parseOutline('## Only a chapter\n- [a] A').title).toBeNull()
  })

  it('flags [TBD] placeholders', () => {
    expect(hasTbd(parseOutline(OUTLINE).chapters[0].slides[1].body)).toBe(true)
  })
})
