/**
 * Hindi style guards.
 *
 * Encodes mechanically checkable rules from `style/hi.md`.
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

const hi = bundle('hi')

const DEVANAGARI = /[\u0900-\u097f]/

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

/**
 * A value with the formal pronoun in it, once the two look-alikes are stripped:
 * `अपने-आप` / `अपने आप` is a DIFFERENT word meaning "automatically" and merely
 * contains those two letters, and `आपत्ति` ("objection") is a distinct noun that
 * happens to begin with them — and the natural Hindi word §4 asks for over a
 * loanword. A value is judged on the pronoun, not on any word starting आप.
 */
const hasFormalPronoun = (value: string) =>
  value.replace(/अपने[-\s]?आप/g, '').replace(/आपत्ति/g, '').includes('आप')

describe('hi punctuation (style/hi.md §1)', () => {
  it('uses purna viram (।) not Latin period for sentence-final', () => {
    // Values containing Devanagari that end with a Latin period should use ।
    const bad: string[] = []
    for (const [key, value] of Object.entries(hi)) {
      if (!DEVANAGARI.test(value)) continue
      // Ends with period after a Devanagari character
      if (/[\u0900-\u097f]\.$/.test(value)) {
        bad.push(`${key}: ${JSON.stringify(value.slice(-30))}`)
      }
    }
    // Baselined: existing catalog may use periods
    expect(bad.length, report(bad)).toBeLessThanOrEqual(30)
  })
})

describe('hi tone (style/hi.md §4)', () => {
  it('does not use formal आप', () => {
    // Formal आप should be तुम (informal).
    const bad = Object.entries(hi)
      .filter(([, v]) => hasFormalPronoun(v))
      .map(([k]) => k)
    // Baselined: the existing catalog uses आप extensively. Each block migrated
    // to तुम lowers this ceiling by the strings it converts, so the count can
    // only go down.
    expect(bad.length, report(bad)).toBeLessThanOrEqual(117)
  })
})

/* ── §4 register, scoped to the values THIS BRANCH wrote ── */

const REPO = join(__dirname, '..', '..', '..', '..')
const CATALOG = 'website/src/i18n/locales/hi.json'

/**
 * The आप-form imperatives whose तुम-form §4 spells out (करो, देखो, चुनो), as
 * whole Devanagari words. The pronoun check above already covers आप itself; this
 * catches the pronoun-less formal imperative ("फिर से प्रयास करें"). Every form is
 * a verb, so a plural noun in `-ें` (फ़ाइलें) is not in reach, and the boundaries
 * keep `दें` from matching inside a longer word.
 */
const FORMAL_IMPERATIVE =
  /(?<![\u0900-\u097f])(?:करें|देखें|चुनें|खोलें|सहेजें|भरें|लिखें|रखें|पढ़ें|दें|लें|जाएं|जाएँ|बनाएं|बनाएँ|हटाएं|हटाएँ|लगाएं|लगाएँ|चलाएं|चलाएँ|बताएं|बताएँ|दबाएं|दबाएँ|कीजिए|कीजिये|करिए)(?![\u0900-\u097f])/

/**
 * The hi values this branch added or edited, or null when there is nothing to
 * diff against (`I18N_BASE_REF` unset — a bare local run; CI always supplies it).
 *
 * Diff scope rather than a catalog-wide sweep, and the same reasoning as
 * `bnStyle`'s register gate: the inherited catalog is majority-आप, so a
 * repo-wide assertion could only land as the baselined COUNT above, and a count
 * cannot tell "one fixed" from "one fixed and one broken". Nothing is stored, so
 * two i18n branches have no ledger line to conflict on.
 */
function changedHiValues(): Record<string, string> | null {
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
  return Object.fromEntries(Object.entries(hi).filter(([key, value]) => base[key] !== value))
}

describe('hi register (style/hi.md §4)', () => {
  it('[changed-values] addresses the reader as तुम, never आप', () => {
    const changed = changedHiValues()
    if (changed === null) {
      // eslint-disable-next-line no-console -- stdout IS this gate's report channel: a gate that returns silently is one nobody can tell ran, and this skip is reachable on a bare local run
      console.log('[changed-values] skipped — I18N_BASE_REF is unset, so there is no branch to diff.')
      return
    }
    const bad = Object.entries(changed)
      .filter(([, value]) => hasFormalPronoun(value) || FORMAL_IMPERATIVE.test(value))
      .map(([key, value]) => `${key}: ${JSON.stringify(value.slice(0, 60))}`)
    expect(bad, `${report(bad)}\n\nThere is no ceiling to raise for these — the value is yours.`)
      .toEqual([])
  })
})
