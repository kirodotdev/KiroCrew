/**
 * `.tex` resolves through two independent paths — a FILENAME (the Papyrus editor,
 * the Files tab, a diff header) and a markdown FENCE TAG — and they must agree.
 * Pierre's own extension table sends `.tex` to the coarse `tex` grammar, which
 * leaves section titles and `\cite`/`\ref`/`\label` keys as plain body text; the
 * override moves both paths onto `latex`.
 *
 * The fence path is the one that silently drifts: it reads the extension table
 * directly, which does NOT consult Pierre's custom-extension map, so registering
 * the override for filenames alone would leave ```tex blocks on the old grammar.
 */
import { describe, it, expect } from 'vitest'
import { PIERRE_EXTENSION_OVERRIDES } from '../pierre/config'
import { fenceLanguage } from '../pierre/PierreImpl'

describe('LaTeX grammar override', () => {
  it('maps every LaTeX source extension to the latex grammar', () => {
    for (const ext of ['tex', 'ltx', 'sty', 'cls']) {
      expect(PIERRE_EXTENSION_OVERRIDES[ext], `.${ext}`).toBe('latex')
    }
  })

  it('leaves bibtex alone — it already resolves correctly', () => {
    expect(PIERRE_EXTENSION_OVERRIDES.bib).toBeUndefined()
    expect(fenceLanguage('bib')).toBe('bibtex')
  })

  it('resolves a ```tex fence to latex, not tex', () => {
    expect(fenceLanguage('tex')).toBe('latex')
  })

  it('resolves a ```latex fence to latex', () => {
    expect(fenceLanguage('latex')).toBe('latex')
  })

  it('is case-insensitive, matching the rest of fence resolution', () => {
    expect(fenceLanguage('TeX')).toBe('latex')
  })

  it('does not disturb unrelated fence tags', () => {
    expect(fenceLanguage('python')).toBe('python')
    expect(fenceLanguage('ts')).toBe('typescript')
    expect(fenceLanguage()).toBe('text')
    expect(fenceLanguage('not-a-language')).toBe('text')
  })
})
