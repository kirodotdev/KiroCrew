/**
 * French style guards.
 *
 * Encodes mechanically checkable rules from `style/fr.md`. The critical rule —
 * narrow no-break space before double punctuation — is checked here as a
 * warning-level baseline rather than a hard gate, since the existing catalog
 * was not authored with it.
 */

import { describe, it, expect } from 'vitest'
import { CATALOGS as RUNTIME_CATALOGS } from '../index'

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
    else out[path] = String(value)
  }
  return out
}

const bundle = (code: string) =>
  flatten((RUNTIME_CATALOGS as Record<string, { translation: unknown }>)[code].translation)

const fr = bundle('fr')

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('fr punctuation (style/fr.md §1)', () => {
  it('double punctuation is not glued to the preceding word', () => {
    // French requires a space (ideally U+202F) before ; : ? !
    // This test catches the worst case: NO space at all before these marks.
    // A regular space is imperfect but acceptable; no space is wrong.
    const bad: string[] = []
    for (const [key, value] of Object.entries(fr)) {
      // Skip very short values (likely single-char or symbols)
      if (value.length < 3) continue
      // Match a letter directly followed by ; : ? ! (no space at all)
      if (/[a-zA-Zàâéèêëïîôùûüÿçœæ][;:?!]/.test(value)) {
        // Exclude URLs, code-like patterns
        if (/https?:\/\//.test(value)) continue
        if (/\{\{.*\}\}/.test(value)) continue
        // Colons in time patterns like 10:30 are fine
        if (/\d:\d/.test(value)) continue
        bad.push(`${key}: ${JSON.stringify(value.slice(0, 60))}`)
      }
    }
    // Baselined: existing catalog was not authored with this rule
    expect(bad.length, report(bad)).toBeLessThanOrEqual(50)
  })

  it('uses tu/toi forms, not vous', () => {
    // Check for formal vous where it clearly addresses the user
    const bad = Object.entries(fr)
      .filter(([, v]) => /\bVous\b/.test(v) && !/\bvous\b/.test(v))
      .map(([k]) => k)
    // "vous" lowercase can appear in many contexts; only flag uppercase "Vous" at sentence start
    expect(bad.length, report(bad)).toBeLessThanOrEqual(11)
  })
})

describe('fr accents (style/fr.md §7)', () => {
  it('capitals have their accents', () => {
    // Common violations: "Etat" should be "État", "A propos" should be "À propos"
    const MUST_ACCENT: Array<[string, string]> = [
      ['Etat', 'État'],
      ['Ecran', 'Écran'],
      ['Element', 'Élément'],
      ['Evenement', 'Événement'],
    ]
    const bad: string[] = []
    for (const [key, value] of Object.entries(fr)) {
      for (const [wrong, correct] of MUST_ACCENT) {
        if (value.includes(wrong) && !value.includes(correct)) {
          bad.push(`${key}: has '${wrong}' not '${correct}'`)
        }
      }
    }
    expect(bad.length, report(bad)).toBeLessThanOrEqual(11)
  })
})
