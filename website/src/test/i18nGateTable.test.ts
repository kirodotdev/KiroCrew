import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import {
  CHECKS,
  SCRIPTS,
  resolveRows,
  verdict,
} from '../../scripts/lib/i18n-gate-table.mjs'

/**
 * The i18n runner's verdict.
 *
 * The property under test is the one the first version got WRONG, and it is worth
 * naming because two independent reviewers found it and reproduced it: a script that
 * exited non-zero BEFORE printing any line a row recognised had every row it owns
 * classified as an unknown, unknowns were excluded from the failure set, and the run
 * exited 0 where the `&&` chain it replaced exited 2. Reachable three ways in this
 * repo — eslint failing to load, an unresolvable `I18N_BASE_REF`, a catalog that did
 * not parse — and each is a case whose own docstring says a gate that cannot run must
 * fail, not pass.
 *
 * It was reachable because the verdict was a chain of filters over live subprocesses
 * that nothing could test. It is a pure function now, and this is the test.
 */

const run = (status: number, text = '') => ({ status, text })

/**
 * A healthy run of every script, so each scenario overrides ONE thing and the rest is
 * realistic. Defaulting the others to empty output made them all read MISSING, which
 * drowned the variable under test — the first draft of this file did exactly that.
 */
const HEALTHY: Record<string, { status: number, text: string }> = {
  pseudo: run(0, 'OK: en-XA.json matches 5811 English keys.'),
  keys: run(0, 'OK: 6459 static key references all resolve, 1 dynamic site(s) at baseline (99.98% static coverage), no shadowing.'),
  codemod: run(0, '2 unextracted string(s) — below the baseline of 3.'),
  plural: run(0, "OK: no literal-'s' pluralization found."),
  dnt: run(0, 'OK: 19 DNT term(s) intact across 9 catalog(s) — 62207 value(s) scanned.'),
  manifest: run(0, 'OK: 16 built-in manifests, 147 strings match locales/en.json exactly.'),
  source: run(0, '[source-strings] 0 new key(s) vs origin/main, 0 finding(s).\n[changed-values] 0 catalog QA finding(s) among values changed vs origin/main.'),
  strings: run(0, '[added-lines] 0 untranslated string(s) on lines this branch wrote.\n[vs-base] 0 touched file(s) gained untranslated strings vs the base.\nOK: 1324 untranslated strings across 241 files, at or below the baseline of 1837.\nOK: 1118 untranslated string(s) inside ALL-CAPS constants across 249 files, at or below the ceiling of 1118.'),
}

const runsOf = (over: Record<string, { status: number, text: string }> = {}) => {
  const m = new Map<string, { status: number, text: string }>()
  for (const s of SCRIPTS) m.set(s.key, over[s.key] ?? HEALTHY[s.key])
  return m
}
const decide = (over = {}, base = 'origin/main') => {
  const runs = runsOf(over)
  const rows = resolveRows({ checks: CHECKS, runs, base })
  return { rows, ...verdict({ rows, runs, scripts: SCRIPTS }) }
}
const row = (rows: any[], id: string) => rows.find(r => r.id === id)

describe('INVARIANT — a non-zero child always fails the run', () => {
  it('fails when a script crashes before printing anything attributable', () => {
    // The exact regression. `check-i18n-strings.mjs` exits 2 when eslint cannot load,
    // printing a ConfigError and none of its three checks' lines.
    const { failed, unexplained, rows } = decide({
      strings: run(2, 'ConfigError: Key "i18next/no-literal-string": structuredClone is not defined'),
    })

    expect(failed).toBe(true)
    expect(unexplained.map(s => s.key)).toEqual(['strings'])
    // The rows cannot attribute it, and that is fine — they are not what decides.
    expect(row(rows, 'added-lines').state).toBe('NOT RUN')
  })

  it.each(SCRIPTS.map(s => s.key))('fails when %s exits non-zero and says nothing', key => {
    // Exhaustive over the scripts, because three of them own exactly ONE check and
    // three own several — the original bug only bit the single-check ones by a
    // different route, and a table edit could move any script between the two shapes.
    expect(decide({ [key]: run(1, '') }).failed).toBe(true)
  })

  it('fails even if every row somehow reads PASS', () => {
    // Weaker than the two above, and worth being honest about which: with every row
    // forced to PASS the OLD formula also failed, because its `unexplained` predicate
    // was satisfied. So this is not the regression pin — the two cases above are.
    //
    // What it does pin is that the verdict is not derivable from the rows alone. Given
    // today's `resolveRows`, `bad || crashed` and `bad || unexplained` happen to be
    // equivalent: a crashed script either has a FAIL/MISSING row (so `bad` catches it)
    // or has none (so `unexplained` does). The formula here does not RELY on that
    // reasoning holding, which is the whole reason it reads the child status instead —
    // the original bug was precisely a row-classification change quietly invalidating
    // an invariant the exit code depended on.
    const runs = runsOf({ pseudo: run(1, '') })
    const rows = CHECKS.map(c => ({ ...c, state: 'PASS', summary: '', note: '' }))
    const v = verdict({ rows, runs, scripts: SCRIPTS })
    expect(v.failed).toBe(true)
    expect(v.crashed.map(s => s.key)).toEqual(['pseudo'])
    expect(v.bad).toHaveLength(0) // nothing in the rows says anything is wrong
  })

  it('passes only when every child exited 0 and no row is FAIL or MISSING', () => {
    const { failed, rows } = decide()
    expect(failed).toBe(false)
    expect(rows.every(r => r.state === 'PASS')).toBe(true)
  })
})

describe('attribution — rows name the check, they do not decide the run', () => {
  const keysFailed = {
    keys: run(1, '1 translation key reference(s) do not exist in the English catalogs:'),
  }

  it('attributes a multi-check script failure to the check that printed it', () => {
    const { rows, bad, unexplained } = decide(keysFailed)
    expect(row(rows, 'key-refs').state).toBe('FAIL')
    // Its sibling is genuinely unknown: the script exits before evaluating the ratchet.
    expect(row(rows, 'dynamic-keys').state).toBe('NOT RUN')
    expect(bad.map(r => r.id)).toEqual(['key-refs'])
    expect(unexplained).toHaveLength(0) // a row explained it
  })

  it('attributes a single-check script failure to its only check', () => {
    // `gen-pseudolocale.mjs` owns `pseudolocale` alone and has no failure pattern, so
    // NOT RUN would have been a lie — there is nothing else it could have been.
    const { rows, unexplained } = decide({ pseudo: run(1, 'en-XA.json is stale') })
    expect(row(rows, 'pseudolocale').state).toBe('FAIL')
    expect(unexplained).toHaveLength(0)
  })

  it('flags a check that printed nothing while its script PASSED', () => {
    // The genuine stopped-measuring shape: eleven rows must not silently become ten.
    const { rows, bad, failed } = decide({ plural: run(0, 'nothing recognisable') })
    expect(row(rows, 'plurals').state).toBe('MISSING')
    expect(bad.map(r => r.id)).toContain('plurals')
    expect(failed).toBe(true)
  })
})

describe('no base commit — the diff-scoped checks did not run, and are not broken', () => {
  // `docs/ci/i18n-gates.md` tells contributors to run this locally, where `I18N_BASE_REF`
  // is normally unset, and `resolve-i18n-base.sh` deliberately returns empty on a
  // branch-creation push. Reporting those four as MISSING failed a healthy tree while
  // accusing four working checks of having stopped measuring.
  const noBase = () => decide({
    strings: run(0, '[diff-scope] skipped — I18N_BASE_REF is unset, so there is nothing to diff.\nOK: 1324 untranslated strings across 241 files, at or below the baseline of 1837.\nOK: 1118 untranslated string(s) inside ALL-CAPS constants across 249 files, at or below the ceiling of 1118.'),
    source: run(0, 'OK: skipped — cannot read the English catalogs at origin/main (shallow clone?).'),
  }, '')

  it('marks them NOT RUN rather than MISSING, and passes', () => {
    const { rows, failed } = noBase()
    for (const id of ['added-lines', 'vs-base', 'source-strings', 'changed-values']) {
      expect(row(rows, id).state, id).toBe('NOT RUN')
    }
    expect(failed).toBe(false)
  })

  it('still measures and can still fail the whole-repo checks', () => {
    expect(row(noBase().rows, 'untranslated').state).toBe('PASS')
    expect(row(noBase().rows, 'key-refs').state).toBe('PASS')
  })
})

describe('a whole-repo total over its ceiling reports and does NOT fail', () => {
  // The steering case, and it happened for real: `main` sat at 1120 against its own
  // ALL-CAPS ceiling of 1118 within minutes of #1099 setting it, and every open PR
  // inherited the red for strings none of their diffs wrote.
  // Appended to the healthy output, because these three checks share one script: a
  // fixture that replaces it would starve the siblings and report MISSING instead.
  it.each([
    ['allcaps', '\n[allcaps] REPORT: 1120 untranslated string(s) sit inside ALL-CAPS module\nconstants, 2 above the ceiling of 1118. '],
    ['untranslated', '\n[untranslated] REPORT: 3 file(s) are over their committed ceiling:\n  a.tsx: 1 -> 4'],
  ])('%s over the ceiling is PASS, not FAIL', (id, extra) => {
    const { rows, failed } = decide({ strings: run(0, HEALTHY.strings.text + extra) })
    expect(row(rows, id).state).toBe('PASS')
    expect(failed).toBe(false)
  })

  it('reports a dynamic-keys IMPROVEMENT without demanding a re-snapshot', () => {
    // It used to fail when the number went DOWN, forcing a commit to a shared counter.
    const { rows, failed } = decide({
      keys: run(0, 'OK: 6459 static key references all resolve, 1 dynamic site(s) at baseline (99.98% static coverage), no shadowing.\n[dynamic-keys] REPORT: 2 file(s) improved'),
    })
    expect(row(rows, 'dynamic-keys').state).toBe('PASS')
    expect(failed).toBe(false)
  })

  it('still fails a whole-repo HARD ZERO, which has no ceiling to inherit', () => {
    const { rows, failed } = decide({
      keys: run(1, '1 translation key reference(s) do not exist in the English catalogs:'),
    })
    expect(row(rows, 'key-refs').state).toBe('FAIL')
    expect(failed).toBe(true)
  })

  it('reads the codemod line at EXACTLY the ceiling, not just below it', () => {
    // `i18n-codemod.mjs` prints three mutually exclusive shapes, and they differ in both
    // prefix and separator: `N unextracted string(s) — below …` under the ceiling, and
    // `OK: N unextracted string(s), at …` exactly at it. A pattern anchored on the digit
    // missed the second — and with the live count at 2 against a ceiling of 3, ONE added
    // string would have made the row MISSING and failed the step on a healthy tree.
    for (const line of [
      '\n2 unextracted string(s) — below the baseline of 3. Optional: set --baseline=2',
      '\nOK: 3 unextracted string(s), at the baseline of 3.',
    ]) {
      const { rows, failed } = decide({ codemod: run(0, line) })
      expect(row(rows, 'extractable').state, line.trim()).toBe('PASS')
      expect(failed, line.trim()).toBe(false)
    }
  })

  it('prefers the over-ceiling report to a success line printed alongside it', () => {
    // `check-i18n-keys.mjs` must print its success line unconditionally, because
    // `[key-refs]` reads that same line as ITS signal. So when `[dynamic-keys]` is over,
    // both lines are present and the generic one appears LAST. Matching `find` first
    // would leave the row reading "at baseline" while the growth sat in the collapsed
    // group — the report has to win.
    const { rows, failed } = decide({
      keys: run(0, '\n[dynamic-keys] REPORT: 2 file(s) gained call sites whose key cannot be\nresolved statically:\n  a.tsx\n\nOK: 6459 static key references all resolve, 3 dynamic site(s) at baseline (99.9% static coverage), no shadowing.'),
    })
    expect(row(rows, 'dynamic-keys').summary).toContain('gained unresolvable call sites')
    expect(row(rows, 'dynamic-keys').state).toBe('PASS')
    // ...and its sibling still resolves off the shared line.
    expect(row(rows, 'key-refs').state).toBe('PASS')
    expect(failed).toBe(false)
  })
})

describe('the table covers the chain it replaced', () => {
  it('runs exactly the eight scripts, with the flags the && chain used', () => {
    // Dropping a script from this table is a silent loss of coverage, and this is the
    // only test that would notice. `--baseline=3` is part of the contract: the codemod
    // ratchet lived in the package.json chain and moved here.
    expect(SCRIPTS.map(s => s.argv.join(' '))).toEqual([
      'gen-pseudolocale.mjs --check',
      'check-i18n-keys.mjs',
      'i18n-codemod.mjs --check --baseline=3',
      'i18n-plural-codemod.mjs --check',
      'check-source-strings.mjs',
      'check-i18n-strings.mjs',
      'check-dnt-catalogs.mjs',
      'check-app-manifest-sync.mjs',
    ])
  })

  it('is what npm run i18n:check invokes', () => {
    const pkg = JSON.parse(readFileSync(resolve(__dirname, '../../package.json'), 'utf-8'))
    expect(pkg.scripts['i18n:check']).toContain('scripts/i18n-check.mjs')
  })

  it('gives every check a scope and an enforcement kind', () => {
    // Both are printed, and both are load-bearing for the reader: scope answers "is
    // this mine to fix", enforcement answers "can this fail when it improves".
    for (const c of CHECKS) {
      expect(['diff', 'repo'], c.id).toContain(c.scope)
      expect(['zero', 'hard-zero', 'info'], c.id).toContain(c.enforce)
    }
    // Only a diff-scoped check or a whole-repo HARD ZERO may fail the step. Every other
    // whole-repo number is a stored total another branch can move without touching your
    // files, so it reports. `dynamic-keys` was the last bidirectional ratchet in the repo.
    expect(CHECKS.filter(c => c.enforce === 'info').map(c => c.id))
      .toEqual(['dynamic-keys', 'extractable', 'untranslated', 'allcaps'])
    for (const c of CHECKS.filter(x => x.scope === 'repo' && x.enforce !== 'hard-zero')) {
      expect(c.enforce, `${c.id} is whole-repo and not a hard zero, so it must be info`)
        .toBe('info')
    }
  })
})
