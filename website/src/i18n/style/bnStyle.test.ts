/**
 * Bengali style guards.
 *
 * Encodes mechanically checkable rules from `style/bn.md`.
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

const bn = bundle('bn')

const BENGALI = /[\u0980-\u09ff]/

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('bn punctuation (style/bn.md §1)', () => {
  it('uses dari (।) not Latin period for sentence-final', () => {
    const bad: string[] = []
    for (const [key, value] of Object.entries(bn)) {
      if (!BENGALI.test(value)) continue
      if (/[\u0980-\u09ff]\.$/.test(value)) {
        bad.push(`${key}: ${JSON.stringify(value.slice(-30))}`)
      }
    }
    // Baselined: existing catalog may use periods
    expect(bad.length, report(bad)).toBeLessThanOrEqual(30)
  })
})

/* ── §5 register, scoped to the values THIS BRANCH wrote ── */

const REPO = join(__dirname, '..', '..', '..', '..')
const CATALOG = 'website/src/i18n/locales/bn.json'

/** The আপনি pronoun family. Any of these addresses the reader honorifically. */
const HONORIFIC = /আপনি|আপনার|আপনাকে|আপনাদের|আপনারা/
/**
 * The আপনি-form imperatives whose তুমি-form §5 spells out (করো, দেখো, বেছে নাও).
 * The trailing lookahead is load-bearing, never a bare substring match: `দেখানো`
 * ("shown", an impersonal participle that addresses nobody) opens with `দেখান`,
 * so a match without it would flag compliant copy and the gate would be answered
 * by weakening it.
 */
const FORMAL_IMPERATIVE = /(?:করুন|দেখুন|দেখান|বেছে নিন)(?![ঀ-৿])/

/**
 * The bn values this branch added or edited, or null when there is nothing to
 * diff against (`I18N_BASE_REF` unset — a bare local run; CI always supplies it).
 *
 * Diff scope rather than a catalog-wide sweep, for the same reason
 * `changedValueQa` is diff-scoped: the inherited catalog is majority-আপনি, so a
 * repo-wide assertion could only land as another baselined COUNT, and a count
 * cannot tell "one fixed" from "one fixed and one broken". Nothing is stored
 * here, so two i18n branches have no ledger line to conflict on, and every value
 * a branch writes is held to §5 at zero tolerance.
 */
function changedBnValues(): Record<string, string> | null {
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
  // The right-hand side is the runtime catalog, which is the WORKING TREE — the
  // state a developer running this locally is asking about.
  return Object.fromEntries(Object.entries(bn).filter(([key, value]) => base[key] !== value))
}

describe('bn register (style/bn.md §5)', () => {
  it('[changed-values] addresses the reader as তুমি, never আপনি', () => {
    const changed = changedBnValues()
    if (changed === null) {
      // eslint-disable-next-line no-console -- stdout IS this gate's report channel: a gate that returns silently is one nobody can tell ran, and this skip is reachable on a bare local run
      console.log('[changed-values] skipped — I18N_BASE_REF is unset, so there is no branch to diff.')
      return
    }
    const bad = Object.entries(changed)
      .filter(([, value]) => HONORIFIC.test(value) || FORMAL_IMPERATIVE.test(value))
      .map(([key, value]) => `${key}: ${JSON.stringify(value.slice(0, 60))}`)
    expect(bad, `${report(bad)}\n\nThere is no ceiling to raise for these — the value is yours.`)
      .toEqual([])
  })
})

describe('bn numerals (style/bn.md §2)', () => {
  it('uses Western digits (0-9) not Bengali digits (০-৯)', () => {
    // Bengali digits U+09E6 to U+09EF should not appear in the catalog
    const BENGALI_DIGITS = /[\u09e6-\u09ef]/
    const bad = Object.entries(bn)
      .filter(([, v]) => BENGALI_DIGITS.test(v))
      .map(([k]) => k)
    expect(bad.length, report(bad)).toBeLessThanOrEqual(8)
  })
})
