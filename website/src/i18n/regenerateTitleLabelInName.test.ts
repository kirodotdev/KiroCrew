/**
 * The regenerate button's name and tooltip (`regenerate_title_with_llm`) hold its
 * visible label (`auto_title`) in every shipped locale: WCAG 2.5.3, Label in Name
 * (#10152). The Undo offer that replaces it is named by `undo_auto_title_named`,
 * which starts with its visible label (`undo_auto_title`) the same way. The
 * generated pseudolocale pads every value, so it is skipped.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES } from './languages'

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter(l => l.devOnly).map(l => l.code))

describe('session title buttons: each name holds its visible label', () => {
  const shipped = Object.keys(CATALOGS).filter(code => !GENERATED.has(code))

  it.each(shipped)('%s: regenerate_title_with_llm contains auto_title', code => {
    const page = (CATALOGS[code].translation.pages as Record<string, Record<string, string>>).chatPage
    expect(page.regenerate_title_with_llm).toContain(page.auto_title)
  })

  it.each(shipped)('%s: undo_auto_title_named starts with undo_auto_title', code => {
    const page = (CATALOGS[code].translation.pages as Record<string, Record<string, string>>).chatPage
    expect(page.undo_auto_title_named.startsWith(page.undo_auto_title)).toBe(true)
  })
})
