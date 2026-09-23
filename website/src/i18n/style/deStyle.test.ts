/**
 * German style guards.
 *
 * Encodes mechanically checkable rules from `style/de.md`.
 */

import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import { join } from 'node:path'
import { CATALOGS as RUNTIME_CATALOGS } from '../catalogs'

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

/* ── §4 register, scoped to the values THIS BRANCH wrote ── */

const REPO = join(__dirname, '..', '..', '..', '..')
const CATALOG = 'website/src/i18n/locales/de.json'

/**
 * The Sie pronoun family (Sie / Ihnen / Ihr / Ihre / Ihren / Ihrem / Ihrer / Ihres)
 * in the MIDDLE of a sentence — after a character that is neither whitespace nor
 * sentence-ending punctuation. Mid-sentence, a capitalised form can only be the
 * formal address: "sie" (they/she) and "ihr" (her/their/you-plural) are lowercase
 * there. The one place capital and lowercase collide is the sentence start, which
 * is left to the count-capped check above rather than guessed at here.
 *
 * The German formal imperative always carries the pronoun ("versuchen Sie es"),
 * so this one pattern also covers imperatives; there is no separate verb list.
 */
const FORMAL_ADDRESS = /[^\s.!?…:]\s+(?:Sie|Ihnen|Ihr|Ihre|Ihren|Ihrem|Ihrer|Ihres)\b/

/**
 * The de values this branch added or edited, or null when there is nothing to
 * diff against (`I18N_BASE_REF` unset — a bare local run; CI always supplies it).
 *
 * Diff scope rather than a catalog-wide sweep, and the same reasoning as
 * `bnStyle`'s register gate: the inherited catalog is majority-Sie, so a
 * repo-wide assertion could only land as another baselined COUNT, and a count
 * cannot tell "one fixed" from "one fixed and one broken". Nothing is stored, so
 * two i18n branches have no ledger line to conflict on.
 */
function changedDeValues(): Record<string, string> | null {
  const baseRef = process.env.I18N_BASE_REF
  if (!baseRef) return null
  const git = (args: string[]) =>
    execFileSync('git', args, { cwd: REPO, encoding: 'utf-8', maxBuffer: 64 * 1024 * 1024 })
  // A ref that IS configured but cannot be resolved throws: a gate that cannot
  // run must fail, never pass quietly.
  git(['rev-parse', '--verify', `${baseRef}^{commit}`])
  let from: string
  try {
    from = git(['merge-base', baseRef, 'HEAD']).trim()
  } catch {
    // CI checks out at depth 1 and fetches the base at depth 1 too, so there is
    // no shared history to find a merge base in; the base tip needs only the two
    // trees.
    from = baseRef
  }
  let base: Record<string, string> = {}
  try {
    base = flatten(JSON.parse(git(['show', `${from}:${CATALOG}`])))
  } catch {
    // No catalog at the base ref: every value in it is this branch's.
  }
  return Object.fromEntries(Object.entries(de).filter(([key, value]) => base[key] !== value))
}

describe('de register (style/de.md §4)', () => {
  it('[changed-values] addresses the reader as du, never Sie', () => {
    const changed = changedDeValues()
    if (changed === null) {
      // eslint-disable-next-line no-console -- stdout IS this gate's report channel: a gate that returns silently is one nobody can tell ran, and this skip is reachable on a bare local run
      console.log('[changed-values] skipped — I18N_BASE_REF is unset, so there is no branch to diff.')
      return
    }
    const bad = Object.entries(changed)
      .filter(([, value]) => FORMAL_ADDRESS.test(value))
      .map(([key, value]) => `${key}: ${JSON.stringify(value.slice(0, 60))}`)
    expect(bad, `${report(bad)}\n\nThere is no ceiling to raise for these — the value is yours.`)
      .toEqual([])
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
