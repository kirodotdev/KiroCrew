import { describe, expect, it } from 'vitest'

import { refusalText } from './PortabilityTab'

describe('refusalText', () => {
  const fallback = 'Import failed.'

  it('prefers the localized fallback over a coded 5xx boilerplate message', () => {
    // What these handlers actually answer with: opaque English produced in
    // Python, which says no more than the catalog string already says.
    expect(refusalText(500, { error: 'Import failed', code: 'import_failed' }, fallback))
      .toBe(fallback)
    expect(refusalText(500, { error: 'Preview failed', code: 'preview_failed' }, fallback))
      .toBe(fallback)
  })

  it('keeps the validator detail a coded 4xx carries', () => {
    // `import_archive_invalid` reports the archive validator's own finding.
    // That prose is the whole value of the message, so it must survive.
    expect(refusalText(
      400,
      { error: 'manifest.json is missing', code: 'import_archive_invalid' },
      fallback,
    )).toBe('manifest.json is missing')
  })

  it('keeps the prose of an uncoded refusal at any status', () => {
    // No machine-readable identity means the refusal may not be from these
    // handlers at all — a proxy, an edge, a gateway — and there the message can
    // be the only detail there is.
    expect(refusalText(500, { error: 'Bad gateway' }, fallback)).toBe('Bad gateway')
    expect(refusalText(400, { error: 'nope' }, fallback)).toBe('nope')
  })

  it('falls back when the body carries no message at all', () => {
    expect(refusalText(500, {}, fallback)).toBe(fallback)
    expect(refusalText(400, { error: '' }, fallback)).toBe(fallback)
  })
})
