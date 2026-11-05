/**
 * The Kiro Cloud Sessions feature is named exactly that, in every catalog.
 *
 * It is Kiro Agent's own feature name, kept untranslated and always plural. A
 * shortened "Kiro Cloud", a singular "Kiro Cloud Session" or a hyphenated
 * German compound ("Kiro-Cloud-Profil") would name a product that does not
 * exist, so any string that mentions Kiro Cloud must say "Kiro Cloud Sessions".
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES } from './languages'

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter((l) => l.devOnly).map((l) => l.code))

/** Any spelling of the name other than exactly "Kiro Cloud Sessions". */
const WRONG_NAME = /Kiro[ -]Cloud(?! Sessions(?![A-Za-z]))/i

function strings(obj: unknown, prefix = ''): Array<[string, string]> {
  if (obj === null || typeof obj !== 'object') return []
  return Object.entries(obj as Record<string, unknown>).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key
    return typeof value === 'string' ? [[path, value] as [string, string]] : strings(value, path)
  })
}

describe('Kiro Cloud Sessions name', () => {
  it('names the feature exactly "Kiro Cloud Sessions" in every catalog', () => {
    const wrong: string[] = []
    for (const [code, bundle] of Object.entries(CATALOGS)) {
      if (GENERATED.has(code)) continue
      for (const [key, value] of strings((bundle as { translation: unknown }).translation)) {
        if (WRONG_NAME.test(value)) wrong.push(`${code}:${key}: ${value}`)
      }
    }
    expect(wrong).toEqual([])
  })

  it('keeps the Settings heading and profile buttons on the full name', () => {
    const en = (CATALOGS.en as { translation: { sessionEnvironments: { kiroCloud: Record<string, string> } } })
      .translation.sessionEnvironments.kiroCloud
    expect(en.title).toBe('Kiro Cloud Sessions')
    expect(en.profiles).toBe('Kiro Cloud Sessions profiles')
    expect(en.add).toBe('Add Kiro Cloud Sessions profile')
    expect(en.remove).toBe('Remove Kiro Cloud Sessions profile')
  })
})
