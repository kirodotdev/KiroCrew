import { folderColorStroke, folderColorWash } from './folderColorPaint'

describe('folderColorPaint', () => {
  it('folderColorStroke pulls the color toward text-strong', () => {
    expect(folderColorStroke('#abcdef')).toBe(
      'color-mix(in srgb, #abcdef 75%, var(--text-strong))',
    )
  })

  it('folderColorWash tints the elevated surface with the color', () => {
    expect(folderColorWash('#abcdef')).toBe(
      'color-mix(in srgb, #abcdef 18%, var(--bg-elevated))',
    )
  })

  it('both paints interpolate a CSS variable input unchanged', () => {
    expect(folderColorStroke('var(--zzq)')).toContain('var(--zzq) 75%')
    expect(folderColorWash('var(--zzq)')).toContain('var(--zzq) 18%')
  })

  it('the wash is a lighter mix than the stroke for the same color', () => {
    // Guards the two ratios against being swapped: the glyph fill must stay a
    // tint, the linework must stay dominated by the folder color.
    const strokePct = Number(/ (\d+)%/.exec(folderColorStroke('#000'))![1])
    const washPct = Number(/ (\d+)%/.exec(folderColorWash('#000'))![1])
    expect(washPct).toBeLessThan(strokePct)
  })
})
