/**
 * Catalog QA guards — the checks a translation management system would run, for a
 * project that keeps its catalogs in git and has no TMS.
 *
 * `catalogParity.test.ts` proves the catalogs are structurally *aligned* with each
 * other: same keys, same placeholders, right plural categories. It says nothing
 * about whether an individual value is well formed. This file covers that half.
 *
 * ## Why these specific checks
 *
 * They are the intersection of what every major TMS ships as a stock check
 * (Lokalise, Weblate, Crowdin, POEditor all include bracket balance, edge
 * whitespace and doubled spaces) and what a JSON catalog can decide on its own.
 * They matter more here than in a TMS-backed project, not less: translations are
 * machine-generated and then edited piecemeal by contributors who cannot read the
 * other nine languages, so CI is the only reviewer these strings get.
 *
 * ## Why a per-check CEILING, and why that is TEMPORARY
 *
 * The existing violations are a codemod artifact, not a translation problem — the
 * extractor split sentences at JSX boundaries, so `'Skills ('` and `')'` became
 * separate keys. Fixing those is a large, separate piece of work. Until then this
 * suite is a **ratchet**: it fails when a check gets worse and tolerates the frozen
 * debt.
 *
 * The enforced unit is a **count per check**, and only an INCREASE fails; a decrease
 * needs no edit anywhere. A frozen set of `lang:key` sites (a per-site allowlist) is
 * the strictest useful shape and the one to return to once the backfill is done, but
 * it is unworkable while the backfill is in flight, for two reasons:
 *
 * 1. **A staleness guard fails on improvement.** Fixing a string invalidates its
 *    entry, forcing a regenerate of the whole sorted list, so two changes fixing
 *    *disjoint* strings collide textually in that one file.
 * 2. **A key rename reads as a brand-new violation.** Phase 3 collapses fragment
 *    keys, so a still-unbalanced string arrives under a new key and would trip a
 *    "no NEW violations" assertion even though nothing got worse.
 *
 * A count sidesteps both: a decrease is silent, so disjoint fixes never contend.
 *
 * **The ultimate goal is zero for every one of these numbers.** That is what makes the
 * relaxation converge instead of rot: a ceiling of 0 has nothing left to decrease, so
 * "only an increase fails" IS the strict gate — identical to `expect(...).toEqual([])`.
 * Every backfill that lowers a ceiling moves it toward being unremovable, and the last
 * one deletes the ceiling entirely. Restoring per-site identity is tracked in issue
 * #1004; reaching zero makes restoring it unnecessary.
 *
 * **The blind spot in the meantime, stated plainly:** a count cannot tell "one fixed"
 * from "one fixed and one broken". That hole is closed from the other side rather than
 * left open — `scripts/check-source-strings.mjs` runs these same predicates at ZERO
 * tolerance over every catalog value this branch adds or changes, using the shared
 * definitions in `scripts/lib/qa-checks.mjs`. Frozen debt is what the ceiling covers;
 * anything you touch has no ceiling at all.
 *
 * ## Reproducing the worklist
 *
 *     I18N_QA_REPORT=1 npx vitest run src/i18n/qa.test.ts
 *
 * prints every live violation site per check — the worklist for the cleanup, kept
 * available without a committed file that could go stale. The check predicates live
 * in `scripts/lib/qa-checks.mjs` and nowhere else, so this report and the diff-scoped
 * gate cannot drift apart.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS as RUNTIME_CATALOGS } from './index'
import { DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES } from './languages'
// One definition of the checks, shared with `check-source-strings.mjs`, which runs the
// same predicates at ZERO tolerance over the values a branch changed. Two copies would
// let the ceiling here and the diff gate there disagree about what a violation is.
import { CHECKS, flatten, site } from '../../scripts/lib/qa-checks.mjs'

/**
 * Generated catalogs are excluded. The pseudolocale is a mechanical transform of
 * `en.json`, so every defect it has is inherited — checking it would report the same
 * English violation twice and let a generator change silently move the baseline.
 */
const GENERATED = new Set(SUPPORTED_LANGUAGES.filter(l => l.devOnly).map(l => l.code))

/**
 * Frozen debt per check. Lower a number when a backfill lands if you want the tighter
 * gate immediately — but you are never *obliged* to, and that is the whole point: a
 * decrease is silent, so two changes fixing disjoint strings do not collide here.
 *
 * Raising a number is the reviewable act. It means new malformed copy shipped.
 */
const CEILINGS: Record<string, number> = {
  // 168, not 160, because a twelfth catalog inherits the English fragments that
  // are unbalanced AT SOURCE (`'Findings ('` + N + `')'`). Measured: all 14
  // unbalanced Korean values carry exactly the English value's own delta and none
  // introduce an imbalance of their own, so the +8 is arithmetic on a defect
  // Phase 3 repairs by de-fragmenting the key, not new bad copy.
  'unbalanced-delimiter': 168,
  'odd-quote-count': 27,
  'edge-whitespace': 15,
  'doubled-space': 10,
  'bare-connector': 66,
  'fullwidth-alphanumeric': 0,
}

/** `I18N_QA_REPORT=1` prints the live sites — the worklist the allowlist used to be. */
const REPORT = process.env.I18N_QA_REPORT === '1'

/** Catalogs exactly as the runtime composes them, including English's two-file merge. */
const CATALOGS: Record<string, Record<string, string>> = Object.fromEntries(
  Object.entries(RUNTIME_CATALOGS)
    .filter(([code]) => !GENERATED.has(code))
    .map(([code, bundle]) => [code, flatten((bundle as { translation: unknown }).translation)]),
)


function findViolations(check: (typeof CHECKS)[number]): string[] {
  const out: string[] = []
  for (const [lang, catalog] of Object.entries(CATALOGS)) {
    for (const [key, value] of Object.entries(catalog)) {
      if (check.violates(value, lang)) out.push(site(lang, key))
    }
  }
  return out.sort()
}

const live: Record<string, string[]> = Object.fromEntries(
  CHECKS.map((c) => [c.id, findViolations(c)]),
)

if (REPORT) {
  for (const check of CHECKS) {
    console.log(`\n# ${check.id} — ${live[check.id].length} site(s)`)
    for (const s of live[check.id]) console.log(`  ${s}`)
  }
}

describe('catalog QA', () => {
  it.each(CHECKS.map((c) => [c.id, c] as const))('%s — does not get worse', (id, check) => {
    const ceiling = CEILINGS[id] ?? 0
    expect(
      live[id].length,
      `${check.describe}\n\n${live[id].length} violation(s), above the ceiling of ${ceiling}. ` +
        `Fix the copy. Raise the ceiling in this file only if the new violations are ` +
        `deliberate, and say why in the pull request.\n\n` +
        `${live[id].slice(0, 12).map((s) => `  ${s}`).join('\n')}\n\n` +
        `Full list: I18N_QA_REPORT=1 npx vitest run src/i18n/qa.test.ts`,
    ).toBeLessThanOrEqual(ceiling)
  })

  it('every check has a ceiling', () => {
    // Guards against a check being added here and silently never gated because
    // `CEILINGS` has no entry for it — the `?? 0` above would then gate it at zero
    // and fail confusingly rather than telling you the entry is missing.
    const missing = CHECKS.map((c) => c.id).filter((id) => !(id in CEILINGS))
    expect(missing, 'add a ceiling for the new check').toEqual([])
  })

  it('no ceiling outlives its check', () => {
    // The counterpart: a renamed or deleted check leaves a number that gates nothing.
    const ids = new Set(CHECKS.map((c) => c.id))
    const orphaned = Object.keys(CEILINGS).filter((id) => !ids.has(id))
    expect(orphaned, 'remove the ceiling for a check that no longer exists').toEqual([])
  })

  it('English is the reference catalog and is present', () => {
    expect(Object.keys(CATALOGS)).toContain(DEFAULT_LANGUAGE)
    expect(Object.keys(CATALOGS[DEFAULT_LANGUAGE]).length).toBeGreaterThan(3000)
  })
})

/**
 * The detectors themselves, tested on synthetic values.
 *
 * A per-file ratchet over real catalogs cannot tell "this check is correct" from "this
 * check never fires": both look green. A parity test over the SUM of both curly
 * directions passes on any even total — so `“click “here`, two opening quotes and no
 * closing one, would read as balanced. That is a false negative a catalog-driven test
 * can never surface, because the catalog does not happen to contain the case.
 */
describe('catalog QA detectors', () => {
  const check = (id: string) => {
    const found = CHECKS.find((c) => c.id === id)
    expect(found, `no check with id ${id}`).toBeDefined()
    return found!.violates
  }

  describe('odd-quote-count', () => {
    const raw = check('odd-quote-count')
    const violates = (v: string, lang = DEFAULT_LANGUAGE) => raw(v, lang)

    it('accepts correctly paired curly quotes', () => {
      expect(violates('press “Save” to continue')).toBe(false)
      expect(violates('no quotes at all')).toBe(false)
      expect(violates('“one” and “two”')).toBe(false)
    })

    it('rejects an odd curly-quote count', () => {
      expect(violates('press “Save to continue')).toBe(true)
      expect(violates('press Save” to continue')).toBe(true)
    })

    it('rejects an EVEN count whose directions do not match', () => {
      // The regression: even total, still broken. Parity over the sum passes all of these.
      expect(violates('“click “here')).toBe(true)
      expect(violates('click” here”')).toBe(true)
      expect(violates('“a” “b')).toBe(true)
    })

    it('applies the low-high pair for German rather than the English one', () => {
      // Correct German typography. An English-shaped rule counts one `“` and no `”` and
      // reports six real de-catalog strings as unbalanced.
      expect(violates('drücken Sie „Weiter“, um fortzufahren', 'de')).toBe(false)
      expect(violates('„Mehrzeilig“ und „Einzeilig“', 'de')).toBe(false)
      // Still catches genuinely unbalanced German.
      expect(violates('drücken Sie „Weiter, um fortzufahren', 'de')).toBe(true)
      // And the English pair is not silently accepted for German.
      expect(violates('drücken Sie “Weiter” hier', 'de')).toBe(true)
    })

    it('still parity-checks non-directional straight quotes', () => {
      expect(violates('press "Save" to continue')).toBe(false)
      expect(violates('press "Save to continue')).toBe(true)
    })
  })
})
