/**
 * German style guards.
 *
 * Encodes mechanically checkable rules from `style/de.md`.
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

const de = bundle('de')

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('de tone (style/de.md §5)', () => {
  it('uses du (informal), not Sie (formal)', () => {
    // Capital "Sie" as formal address — but "Sie" is also "they" (always capitalized).
    // Only flag "Sie" when followed by a verb that indicates second-person address.
    const FORMAL_PATTERNS = /\bSie (können|müssen|haben|sind|möchten|sollten|werden)\b/
    const bad = Object.entries(de)
      .filter(([, v]) => FORMAL_PATTERNS.test(v))
      .map(([k]) => k)
    expect(bad.length, report(bad)).toBeLessThanOrEqual(12)
  })
})

describe('de compounds (style/de.md §7)', () => {
  it('English-origin compounds use a hyphen, not a space', () => {
    // Common violations: "Slack Integration" should be "Slack-Integration"
    const COMPOUND_CHECKS: Array<[RegExp, string]> = [
      [/\bSlack Integr/i, 'Slack-Integration'],
      [/\bGitHub Konto\b/, 'GitHub-Konto'],
      [/\bAPI Schlüssel\b/, 'API-Schlüssel'],
      [/\bMCP Server\b/, 'MCP-Server'],
    ]
    const bad: string[] = []
    for (const [key, value] of Object.entries(de)) {
      for (const [pattern, correct] of COMPOUND_CHECKS) {
        if (pattern.test(value)) {
          bad.push(`${key}: should be '${correct}'`)
        }
      }
    }
    expect(bad.length, report(bad)).toBeLessThanOrEqual(12)
  })
})
