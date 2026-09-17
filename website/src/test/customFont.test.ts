import { describe, it, expect } from 'vitest'
import { resolveCustomFontFamily } from '../utils/customFont'

describe('resolveCustomFontFamily', () => {
  it('returns empty string for empty / whitespace input (useZoom then falls back)', () => {
    expect(resolveCustomFontFamily('')).toBe('')
    expect(resolveCustomFontFamily('   ')).toBe('')
  })

  it('quotes a multi-word family and appends a proportional (sans-serif) fallback', () => {
    expect(resolveCustomFontFamily('Comic Sans MS')).toBe("'Comic Sans MS', sans-serif")
  })

  it('leaves a single-token family unquoted and still adds the fallback', () => {
    expect(resolveCustomFontFamily('Inter')).toBe('Inter, sans-serif')
  })

  it('does not append a generic when one is already present', () => {
    expect(resolveCustomFontFamily('Georgia, serif')).toBe('Georgia, serif')
    expect(resolveCustomFontFamily('Fira Code, monospace')).toBe("'Fira Code', monospace")
  })

  it('preserves already-quoted tokens across a comma list', () => {
    expect(resolveCustomFontFamily("'Source Serif 4', Georgia"))
      .toBe("'Source Serif 4', Georgia, sans-serif")
  })
})
