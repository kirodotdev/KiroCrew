/**
 * Number+unit literal matcher: unit tests.
 *
 * This file proves the MATCHER. The gate that runs it over the repo lives in
 * `scripts/check-unit-literals.mjs`, registered in the `i18n:check` table beside
 * the project's other repo-scale gates.
 *
 * WHY THE SCAN IS NOT HERE. It parses every in-scope file with the TypeScript
 * compiler, so its cost scales with the size of the repo rather than with what a
 * branch changed. Inside vitest that cost is multiplied by v8 coverage
 * instrumentation and by every sibling worker competing for cores, measured on one
 * machine at 2.0s standalone, 16.6s at 6 workers, 28.6s at 12 (a 14.2x blow-up), against a 15s
 * per-test budget sized for tests that `await import(...)`. The scan therefore
 * failed intermittently on wide machines and would have needed its ceiling raised
 * again as hardware widened. Out in a script it runs once, in one process,
 * uninstrumented, with no timeout to tune.
 *
 * What stays here is what a unit test is for: proof that the predicate detects the
 * shapes that actually shipped, and that it exempts CSS values and the format seam.
 * Both consumers import the same `scripts/lib/unit-literals.mjs`, so the assertions
 * below cannot drift from the gate they describe.
 */

import { join, relative } from 'node:path'

import { describe, it, expect } from 'vitest'

import { unitLiteralHits, walk, inScope, mayHoldUnitLiteral } from '../../scripts/lib/unit-literals.mjs'

const SRC = join(__dirname, '..')

describe('a number is never glued to a unit literal', () => {
  const files = walk(SRC).filter((f: string) => inScope(relative(SRC, f).split('\\').join('/')))

  it('finds source files to scan', () => {
    // Guards the population the gate walks: a broken `inScope` that filtered
    // everything would make the gate pass vacuously.
    expect(files.length).toBeGreaterThan(300)
  })

  it('detects the shapes that actually shipped, so the matcher is known to work', () => {
    // Every line here is a real defect this repo carried before Phase 4.
    const sample = [
      'const a = `${m}m ${s}s`',              // useUptime
      "const b = bytes + ' KB'",              // formatBytes
      'const c = `${(n / 1000).toFixed(1)}K`', // fmtK
      'const d = `${ms}ms`',                  // fmtDuration
      "const e = Math.round(pct) + '%'",      // percent label
      'const f = <span>{pct.toFixed(0)}%</span>', // the idiomatic JSX shape
    ].join('\n')
    expect(unitLiteralHits('sample.tsx', sample).length).toBeGreaterThanOrEqual(6)
  })

  it('does not flag CSS values, which must keep bare digits', () => {
    // A localized digit in a CSS length is dropped outright by
    // lib/cssSanitize.ts's Latin-only allowlist, a silent total value loss.
    const css = [
      'const a = <div style={{ width: `${pct}%` }} />',
      "el.style.height = Math.min(h, maxH) + 'px'",
      'const c = { transition: `height ${MS}ms ${EASE}` }',
      'const d = { gridTemplateColumns: `${railWidth}px minmax(0,1fr) auto` }',
      'const e = useMotionTemplate`calc(${f} * (100% - ${KNOB}px))`',
    ].join('\n')
    expect(unitLiteralHits('css.tsx', css)).toEqual([])
  })

  it('does not flag a value already routed through the seam', () => {
    const ok = [
      "const a = fmtDuration([[m, 'minute'], [s, 'second']])",
      "const b = fmtBytes(bytes)",
      "const c = fmtPercent(ratio)",
    ].join('\n')
    expect(unitLiteralHits('ok.tsx', ok)).toEqual([])
  })

  /**
   * The fast path decides which files the gate parses at all, so a shape it rejects
   * is a shape the gate is blind to -- the same silent-exemption failure the two
   * older gates had. Every case below is a REAL finding, and each is admitted by one
   * clause alone: drop `RAW_DIFFERS` and the escape and comment cases go dark, drop
   * `RAW_CONCAT` and the concatenations do. Asserting the matcher still reports each
   * one keeps the pairing from going vacuous.
   */
  it('the fast path never hides a finding the matcher would report', () => {
    const tricky = [
      'const a = `${m}m ${s}s`',                    // RAW_BRACE: template continuation
      'const b = <span>{pct.toFixed(0)}%</span>',   // RAW_BRACE: JSX text after {expr}
      "const c = bytes + ' KB'",                    // RAW_CONCAT: one space allowed
      "const d = Math.round(pct) + '%'",            // RAW_CONCAT
      'const e = `${x}\\u006d`',                    // RAW_DIFFERS: unit as an escape
      'const h = `${x}\\m`',                        // RAW_DIFFERS: unit as an identity escape
      "const f = n + '\\x25'",                      // RAW_DIFFERS: '%' as an escape
      "const g = n + /* keep bare */ 's'",          // RAW_DIFFERS: comment after +
    ]
    for (const src of tricky) {
      expect(mayHoldUnitLiteral(src), `fast path rejected: ${src}`).toBe(true)
      expect(unitLiteralHits('t.tsx', src).length, `matcher missed: ${src}`).toBeGreaterThan(0)
    }
  })
})
