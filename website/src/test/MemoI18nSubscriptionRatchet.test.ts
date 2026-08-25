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
 * ## Why importing `i18n/format` also counts as language-dependent
 *
 * `i18nT()` is not the only standalone language reader: every formatter in
 * `src/i18n/format.ts` reads the ACTIVE UI language at call time (dates,
 * numbers, lists, collation). A memoized component whose translated output
 * comes solely from those formatters goes stale after a language switch by the
 * exact mechanism above, without a single `i18nT(` for the narrower trigger to
 * see. So a file counts as language-dependent when it calls `i18nT(` OR imports
 * from `i18n/format`.
 *
 * The two branches accept different subscriptions, deliberately:
 *
 *  - `i18nT(` files must call `useLanguageGeneration()` per boundary — the
 *    contract #5543 converted the whole tree to. Whether `useLanguage()` should
 *    also satisfy this branch is a separate loosening decision, out of scope
 *    here.
 *  - format-only files may instead consume the `useLanguage()` context: context
 *    propagation re-renders consumers THROUGH `memo` (the props bailout does
 *    not apply to context), and the formatters read the language at call time.
 *    Precisely: the repaint that lands AFTER the async catalog swap is the
 *    provider re-rendering on its `languageChanged` handler (`setActive`),
 *    which rebuilds the (deliberately unmemoized) inline context value object —
 *    see the provider's own ordering notes. Memoizing that value on
 *    `[language, resolved, …]` would silently break this branch's guarantee;
 *    `LanguageProvider` documents why the value must stay identity-fresh.
 *    Forcing `useLanguageGeneration()` onto such a consumer would be redundant.
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

/**
 * An import from the locale-formatting seam, any relative depth (`./i18n/format`,
 * `../../i18n/format`). Type-only imports are deliberately NOT excluded: the
 * regex is a trigger for a per-file audit, and a type-only importer with memo
 * boundaries and zero subscriptions is rare enough to exempt explicitly.
 */
const FORMAT_IMPORT = /from\s+['"][^'"]*\bi18n\/format['"]/

/** Count memo boundaries and subscriptions in one file's source. */
export function scanFile(src: string): {
  boundaries: number
  subscriptions: number
  usesI18nT: boolean
  importsFormat: boolean
  languageContextReads: number
} {
  const code = stripComments(src)
  const boundaries = [...code.matchAll(MEMO_BOUNDARY)].length
  const subscriptions = [...code.matchAll(/\buseLanguageGeneration\(\)/g)].length
  const usesI18nT = /\bi18nT\(/.test(code)
  const importsFormat = FORMAT_IMPORT.test(code)
  const languageContextReads = [...code.matchAll(/\buseLanguage\(\)/g)].length
  return { boundaries, subscriptions, usesI18nT, importsFormat, languageContextReads }
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

  it('every memo() boundary in a format-consuming file subscribes or reads the language context', () => {
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (EXEMPT.has(rel)) continue
      const { boundaries, subscriptions, importsFormat, languageContextReads } =
        scanFile(readFileSync(join(SRC, rel), 'utf8'))
      if (boundaries === 0 || !importsFormat) continue
      // useLanguage() consumers re-render THROUGH memo on a switch (context
      // bypasses the props bailout) and format.ts reads the language at call
      // time, so either subscription shape keeps the boundary fresh.
      if (subscriptions + languageContextReads < boundaries) {
        offenders.push(
          `${rel}: ${boundaries} memo() boundaries, ${subscriptions} useLanguageGeneration() + ` +
          `${languageContextReads} useLanguage() calls`,
        )
      }
    }
    expect(
      offenders,
      'src/i18n/format.ts formatters read the active language at call time, so a memoized component ' +
      'rendering their output goes stale after a language switch exactly like an i18nT() one; each ' +
      'memo() boundary in a file importing i18n/format must call useLanguageGeneration() or consume ' +
      'the useLanguage() context (which re-renders through memo), or be exempted here with a reason.',
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
    expect(r).toEqual({
      boundaries: 1,
      subscriptions: 0,
      usesI18nT: true,
      importsFormat: false,
      languageContextReads: 0,
    })
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

  it('detects an i18n/format import at any relative depth', () => {
    for (const spec of ['./i18n/format', '../i18n/format', '../../i18n/format']) {
      const r = scanFile(`import { fmtDate } from '${spec}'\nconst X = memo(() => <a>{fmtDate(d)}</a>)`)
      expect(r.importsFormat, spec).toBe(true)
      expect(r.usesI18nT, spec).toBe(false)
      expect(r.boundaries, spec).toBe(1)
    }
  })

  it('a format-consuming memo boundary with no subscription is the defect shape', () => {
    const r = scanFile(`import { fmtDate } from '../i18n/format'\nexport default memo(function X({ d }) { return <a>{fmtDate(d)}</a> })`)
    expect(r.importsFormat).toBe(true)
    expect(r.boundaries).toBe(1)
    expect(r.subscriptions + r.languageContextReads).toBe(0)
  })

  it('a useLanguage() context read satisfies the format branch without the hook', () => {
    const r = scanFile(
      `import { fmtDate } from '../i18n/format'\n` +
      `export default memo(function X({ d }) { const { language } = useLanguage(); return <a>{fmtDate(d)}</a> })`,
    )
    expect(r.languageContextReads).toBe(1)
    expect(r.subscriptions).toBe(0)
    expect(r.boundaries).toBe(1)
  })

  it('does not count a format import mentioned only in a comment', () => {
    const r = scanFile(`// import { fmtDate } from '../i18n/format'\nconst X = memo(() => null)`)
    expect(r.importsFormat).toBe(false)
  })

  it('does not mistake another module for the format seam', () => {
    const r = scanFile(`import { x } from '../i18n/formatters'\nimport { y } from './format'`)
    expect(r.importsFormat).toBe(false)
  })
})
