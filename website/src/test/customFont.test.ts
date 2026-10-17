import { describe, it, expect } from 'vitest'
import { resolveCustomFontFamily } from '../utils/customFont'

describe('resolveCustomFontFamily', () => {
  it('returns empty string for empty / whitespace input (useZoom then falls back)', () => {
    expect(resolveCustomFontFamily('')).toBe('')
    expect(resolveCustomFontFamily('   ')).toBe('')
  })

  it('quotes a multi-word family, keeps script fallbacks, then a proportional generic', () => {
    expect(resolveCustomFontFamily('Comic Sans MS')).toBe(
      "'Comic Sans MS', var(--script-fallbacks), sans-serif",
    )
  })

  it('quotes a single-token family too and still adds script fallbacks + generic', () => {
    expect(resolveCustomFontFamily('Inter')).toBe(
      "'Inter', var(--script-fallbacks), sans-serif",
    )
  })

  it('quotes a digit-leading font-book name so --font-body stays valid (0xProto)', () => {
    // Unquoted `0xProto` is not a valid CSS <custom-ident>, so the whole
    // font-family declaration would be dropped, resetting body metrics app-wide.
    expect(resolveCustomFontFamily('0xProto')).toBe(
      "'0xProto', var(--script-fallbacks), sans-serif",
    )
  })

  it('inserts var(--script-fallbacks) before the generic even when a generic is given', () => {
    // A CJK/Devanagari/Bengali locale user picking a Latin-only family keeps the
    // localized-glyph aliases the html:lang(...) blocks carry in --script-fallbacks.
    expect(resolveCustomFontFamily('Georgia, serif')).toBe(
      "'Georgia', var(--script-fallbacks), serif",
    )
    expect(resolveCustomFontFamily('Fira Code, monospace')).toBe(
      "'Fira Code', var(--script-fallbacks), monospace",
    )
  })

  it('preserves already-quoted tokens across a comma list', () => {
    expect(resolveCustomFontFamily("'Source Serif 4', Georgia")).toBe(
      "'Source Serif 4', 'Georgia', var(--script-fallbacks), sans-serif",
    )
  })

  it('escapes a quote/backslash in a family name (via cssFontFamilyToken)', () => {
    expect(resolveCustomFontFamily("O'Reilly")).toBe(
      "'O\\'Reilly', var(--script-fallbacks), sans-serif",
    )
  })
})
