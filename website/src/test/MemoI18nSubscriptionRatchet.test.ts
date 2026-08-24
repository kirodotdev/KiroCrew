/**
 * Source-level ratchet: every `memo()` boundary in a file that renders
 * standalone `i18nT()` strings must subscribe to language switches.
 *
 * The defect this pins is a SHAPE, not a bug in one component. `LanguageProvider`
 * repaints a language change with `cloneElement(children)`, which defeats React's
 * referential-equality bailout for the ROOT element only. `React.memo` compares
 * props, and `i18nT()` output is not a prop — so a memoized subtree whose props
 * did not change keeps rendering the PREVIOUS catalog until its props next move.
 * The fix is one `useLanguageGeneration()` call at the top of each memoized
 * component body (`src/i18n/useLanguageGeneration.ts` documents the mechanism).
 *
 * A behavioural test cannot catch a NEW memo()+i18nT() file nobody wrote a test
 * for — the combination is added one innocent-looking `export default memo(X)`
 * at a time — which is why this reads the tree (same shape as
 * `ImeEnterClaimRatchet.test.ts`).
 *
 * The rule is counted per boundary, not per file: a file with three memo
 * components and one subscription would still leave two subtrees stale, so the
 * number of `useLanguageGeneration()` calls must be at least the number of
 * `memo()` boundaries in any file that also calls `i18nT(`.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = join(__dirname, '..')

/**
 * Files exempt from the rule, each with the reason it is not a live boundary.
 * Keep this list short and justified — an unexplained entry is a stale subtree.
 */
const EXEMPT = new Set<string>([
  // Defines and calls its OWN `memo` — a locale-keyed formatter cache, not
  // React.memo. The locale is part of every cache key, so a language switch
  // already misses the cache; there is no React boundary here at all.
  'i18n/format.ts',
])

/** Strip // line comments and /* … *\/ blocks so prose mentioning memo() does not count. */
export function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

/**
 * React memo boundaries: bare `memo(` / `React.memo(`, or the generic forms
 * `memo<…>(` / `React.memo<…>(`. The generic parameter is NOT parsed — it can
 * contain nested `=>`/`(` (e.g. callback props) that defeat any single-line
 * regex — so `memo` followed by `<` or `(` counts. `useMemo` never matches
 * (word char before `memo`); a comparison like `x < memo` does not occur.
 */
const MEMO_BOUNDARY = /(?<![\w$])(?:React\.)?memo\s*[<(]/g

/** Count memo boundaries and subscriptions in one file's source. */
export function scanFile(src: string): { boundaries: number; subscriptions: number; usesI18nT: boolean } {
  const code = stripComments(src)
  const boundaries = [...code.matchAll(MEMO_BOUNDARY)].length
  const subscriptions = [...code.matchAll(/\buseLanguageGeneration\(\)/g)].length
  const usesI18nT = /\bi18nT\(/.test(code)
  return { boundaries, subscriptions, usesI18nT }
}

function sourceFiles(): string[] {
  return readdirSync(SRC, { recursive: true, encoding: 'utf8' })
    .map(p => p.split('\\').join('/'))
    .filter(p => /\.tsx?$/.test(p))
    .filter(p => !p.startsWith('test/') && !p.includes('__tests__') && !/\.test\.tsx?$/.test(p))
}

describe('memo() + i18nT() subscription ratchet', () => {
  it('every memo() boundary in an i18nT()-using file calls useLanguageGeneration()', () => {
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (EXEMPT.has(rel)) continue
      const { boundaries, subscriptions, usesI18nT } = scanFile(readFileSync(join(SRC, rel), 'utf8'))
      if (boundaries === 0 || !usesI18nT) continue
      if (subscriptions < boundaries) {
        offenders.push(`${rel}: ${boundaries} memo() boundaries, ${subscriptions} useLanguageGeneration() calls`)
      }
    }
    expect(
      offenders,
      'memo() bails out of the provider-level language repaint; each memoized component in a file using ' +
      'standalone i18nT() must call useLanguageGeneration() at the top of its body ' +
      '(see src/i18n/useLanguageGeneration.ts), or be exempted here with a reason.',
    ).toEqual([])
  })

  it('the exemption list holds only files that still exist', () => {
    const files = new Set(sourceFiles())
    for (const rel of EXEMPT) expect(files.has(rel), `stale exemption: ${rel}`).toBe(true)
  })
})

/*
 * Fixture suite: exercises the exact predicate the tree scan runs, so a regex
 * tightened against the live tree alone cannot silently stop matching the
 * defect shape.
 */
describe('scanFile predicate', () => {
  it('counts a bare memo(function …) boundary', () => {
    const r = scanFile(`const X = memo(function X() { return <a>{i18nT('k')}</a> })`)
    expect(r).toEqual({ boundaries: 1, subscriptions: 0, usesI18nT: true })
  })

  it('counts React.memo and generic memo<Props>() spellings', () => {
    const r = scanFile(`const A = React.memo(fn); const B = memo<Props>(fn2); i18nT('k')`)
    expect(r.boundaries).toBe(2)
  })

  it('does not count useMemo() or prose mentions in comments', () => {
    const r = scanFile(`// defeats the memo() bailout\nconst v = useMemo(() => 1, [])\n/* memo( in a block */`)
    expect(r.boundaries).toBe(0)
  })

  it('a subscribed boundary satisfies the rule', () => {
    const r = scanFile(`const X = memo(function X() { useLanguageGeneration(); return i18nT('k') })`)
    expect(r.subscriptions).toBe(1)
    expect(r.boundaries).toBe(1)
  })
})
