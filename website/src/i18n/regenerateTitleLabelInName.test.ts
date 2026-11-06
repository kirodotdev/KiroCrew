/**
 * The regenerate button's name and tooltip (`regenerate_title_with_llm`) hold its
 * visible label (`auto_title`) in every shipped locale: WCAG 2.5.3, Label in Name
 * (#10152). The generated pseudolocale pads every value, so it is skipped.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES } from './languages'

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter(l => l.devOnly).map(l => l.code))

describe('regenerate title button: its name holds its visible label', () => {
  const shipped = Object.keys(CATALOGS).filter(code => !GENERATED.has(code))

  it.each(shipped)('%s: regenerate_title_with_llm contains auto_title', code => {
    const page = (CATALOGS[code].translation.pages as Record<string, Record<string, string>>).chatPage
    expect(page.regenerate_title_with_llm).toContain(page.auto_title)
  })
})
