/**
 * Every inline ceiling in a style test names its diff-scoped companion.
 *
 * docs/ci/i18n-gates.md, "The rule covers an inline ceiling too": a
 * `toBeLessThanOrEqual(N)` over the whole catalog cannot tell a branch's own
 * violation from one it inherited, so two PRs that are each under N cross it
 * together on main and every other open PR goes red on keys it never touched.
 * The ceiling is allowed only beside a `[changed-values]` test that holds the
 * values the branch wrote at zero. This file makes that pairing executable:
 * a new ceiling fails here until it is listed with a companion that exists.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const DIR = __dirname

/**
 * Ceiling test title -> the `[changed-values]` test that covers the same
 * defect, per file. The companion may sit in another `describe` (the formal
 * register rule is a "tone" ceiling and a "register" companion in several
 * locales).
 */
const COMPANIONS: Record<string, Record<string, string>> = {
  'bnStyle.test.ts': {
    'uses dari (।) not Latin period for sentence-final': '[changed-values] ends a Bengali sentence with dari at zero tolerance',
    'uses Western digits (0-9) not Bengali digits (০-৯)': '[changed-values] uses Western digits at zero tolerance',
  },
  'deStyle.test.ts': {
    'uses du (informal), not Sie (formal)': '[changed-values] addresses the reader as du, never Sie',
    'English-origin compounds use a hyphen, not a space': '[changed-values] hyphenates English-origin compounds at zero tolerance',
  },
  'frStyle.test.ts': {
    'double punctuation is not glued to the preceding word': '[changed-values] puts U+202F before double punctuation, never a plain space',
    'uses tu/toi forms, not vous': '[changed-values] addresses the reader as tu, never vous',
    'capitals have their accents': '[changed-values] accents capitals at zero tolerance',
  },
  'hiStyle.test.ts': {
    'uses purna viram (।) not Latin period for sentence-final': '[changed-values] ends a Devanagari sentence with purna viram at zero tolerance',
    'does not use formal आप': '[changed-values] addresses the reader as तुम, never आप',
  },
  'ruStyle.test.ts': {
    'uses guillemets « » for quotation, not straight quotes around Russian text': '[changed-values] quotes Cyrillic with guillemets at zero tolerance',
  },
}

const IT_TITLE = /^\s*it\(\s*(['"`])(.*?)\1/

/** The titles of the `it` blocks in a file, and which of them hold a ceiling. */
function scan(source: string): { titles: Set<string>; ceilings: string[] } {
  const titles = new Set<string>()
  const ceilings: string[] = []
  let current: string | null = null
  for (const line of source.split('\n')) {
    const m = IT_TITLE.exec(line)
    if (m) {
      current = m[2]
      titles.add(current)
    }
    if (line.includes('.toBeLessThanOrEqual(') && current !== null && !ceilings.includes(current)) {
      ceilings.push(current)
    }
  }
  return { titles, ceilings }
}

const files = readdirSync(DIR).filter((f) => /Style\.test\.ts$/.test(f)).sort()

describe('style ceilings (docs/ci/i18n-gates.md, the ratchet rule)', () => {
  it('finds the style test files', () => {
    expect(files.length).toBeGreaterThan(0)
  })

  for (const file of files) {
    it(`${file}: every inline ceiling has a [changed-values] companion`, () => {
      const { titles, ceilings } = scan(readFileSync(join(DIR, file), 'utf8'))
      const listed = COMPANIONS[file] ?? {}
      const problems: string[] = []
      for (const ceiling of ceilings) {
        const companion = listed[ceiling]
        if (companion === undefined) {
          problems.push(`ceiling "${ceiling}" has no companion listed in ceilingCompanions.test.ts`)
        } else if (!titles.has(companion)) {
          problems.push(`ceiling "${ceiling}": companion "${companion}" is not a test in ${file}`)
        }
      }
      for (const stale of Object.keys(listed)) {
        if (!ceilings.includes(stale)) problems.push(`listed ceiling "${stale}" is no longer a ceiling in ${file}`)
      }
      expect(
        problems,
        `${problems.join('\n')}\n\nA whole-catalog ceiling needs a diff-scoped [changed-values] test over the same defect.`,
      ).toEqual([])
    })
  }
})
