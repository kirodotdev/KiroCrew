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
 * ## Why the trigger follows imports, and only through hook-free modules
 *
 * Language dependence is transitive. `utils/timeAgo.ts` imports the seam and
 * re-exports its output as a plain function, so a memo boundary that renders
 * `timeAgo(t)` is exactly as stale as one rendering `fmtRelative(t)` directly --
 * with no `i18nT(` and no `i18n/format` import for either narrower branch to
 * see. That is the same "added one innocent import at a time" route the
 * direct-import branch closed, one module further out (#5922).
 *
 * So a module counts as a LANGUAGE WRAPPER when it contains no React hook call
 * AND it either imports the seam directly or imports another wrapper. The
 * hook-free half is the load-bearing one, and it is a statement about who CAN
 * fix the staleness:
 *
 *  - A hook-free module cannot subscribe to anything. It is a plain function
 *    (or a hookless presentational component) reading the active language at
 *    call time, so the only site where a subscription can exist is the memo
 *    boundary that calls it. Flagging that caller points at the one fix.
 *  - A module that already calls hooks owns its own freshness: it can add
 *    `useLanguageGeneration()` itself, and if it has a memo boundary the two
 *    branches above already require it. Propagating the trigger to ITS callers
 *    would red a parent for a child's omission and nudge the fix into the wrong
 *    file, so the trigger stops at the first hooked module.
 *
 * The wrapper set is therefore a CLOSURE over hook-free modules, not a fixed
 * depth: a hook-free wrapper of a wrapper cannot subscribe either, so capping
 * the walk at one hop would recreate the same blind spot one module further out
 * and contradict the rule's own premise. The closure is what makes it
 * self-consistent, and it costs one loop -- measured at 31 modules reached in 2
 * rounds, with zero offenders.
 *
 * A direct `i18next.language` read is the small sibling of the same gap (e.g.
 * `components/commandPalette/providers/recentsProvider.ts`): the file reads the
 * active language with no seam import at all, so it triggers on its own. It is
 * NOT a wrapper seed -- propagating from it would drag in `LanguageProvider` and
 * with it most of the tree.
 *
 * Both import spellings count. `@/utils/timeAgo` is the same module as
 * `../utils/timeAgo` (the `@/*` -> `./src/*` alias in `tsconfig.app.json` and
 * `vite.config.ts`), so resolving only relative specifiers would leave the alias
 * as a silent bypass of exactly the shape this branch exists to close.
 *
 * The rule is counted per boundary, not per file: a file with three memo
 * components and one subscription would still leave two subtrees stale, so the
 * number of `useLanguageGeneration()` calls must be at least the number of
 * `memo()` boundaries in any file that also calls `i18nT(`.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, posix } from 'node:path'

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

/** The locale-formatting seam itself, as a path relative to `src/`. */
const LANGUAGE_SEAM = 'i18n/format.ts'

/**
 * A direct read of the active language off the i18next instance -- the same
 * call-time language dependence as a formatter, with no seam import to see.
 */
const I18NEXT_LANGUAGE = /\bi18next\.language\b/

/**
 * Any React hook CALL. Used to decide whether a module can subscribe to a
 * language switch on its own; `use` followed by a capital rules out plain
 * helpers like `useful(` while catching `useState(`, `useLanguage(` and every
 * custom hook.
 */
const HOOK_CALL = /(?<![\w$])use[A-Z]\w*\s*\(/

/**
 * Import specifiers that can name a file in this tree: relative (`./x`, `../x`)
 * and alias (`@/x`, the `@/*` -> `./src/*` mapping in `tsconfig.app.json` and
 * `vite.config.ts`). Covers `from '...'`, bare `import '...'` and `import('...')`.
 */
const MODULE_IMPORT = /(?:\bfrom|\bimport)\s*\(?\s*['"]((?:\.|@\/)[^'"]*)['"]/g

/** Specifiers imported by one file that could name a file in this tree. */
export function importedSpecifiers(src: string): string[] {
  return [...stripComments(src).matchAll(MODULE_IMPORT)].map(m => m[1])
}

/**
 * Resolve one specifier to a path in `known` (all paths relative to `src/`),
 * trying the extensions and index files a bundler would. `@/` resolves from the
 * tree root, everything else from the importing file's directory. Returns null
 * for anything outside the scanned tree (assets, generated files, packages).
 */
export function resolveSpecifier(fromRel: string, spec: string, known: Set<string>): string | null {
  const base = spec.startsWith('@/')
    ? posix.normalize(spec.slice(2))
    : posix.normalize(posix.join(posix.dirname(fromRel), spec))
  for (const cand of [base, `${base}.ts`, `${base}.tsx`, `${base}/index.ts`, `${base}/index.tsx`]) {
    if (known.has(cand)) return cand
  }
  return null
}

/**
 * Modules that carry the seam's language-dependent output and cannot subscribe
 * on their own: no hook call, and either a direct `i18n/format` import or an
 * import of another such module. Closed to a fixpoint, because a hook-free
 * wrapper of a wrapper cannot subscribe either -- see the header for why the
 * walk stops at the first hooked module rather than at a fixed depth.
 */
export function languageWrapperModules(sources: Map<string, string>): Set<string> {
  const known = new Set(sources.keys())
  const code = new Map([...sources].map(([rel, src]) => [rel, stripComments(src)]))
  const candidates = [...code].filter(([rel, src]) => rel !== LANGUAGE_SEAM && !HOOK_CALL.test(src))
  const wrappers = new Set(candidates.filter(([, src]) => FORMAT_IMPORT.test(src)).map(([rel]) => rel))
  for (;;) {
    const grew = candidates.filter(([rel, src]) =>
      !wrappers.has(rel) &&
      importedSpecifiers(src).some(spec => {
        const dep = resolveSpecifier(rel, spec, known)
        return dep !== null && wrappers.has(dep)
      }),
    )
    if (grew.length === 0) return wrappers
    for (const [rel] of grew) wrappers.add(rel)
  }
}

/** Count memo boundaries and subscriptions in one file's source. */
export function scanFile(src: string): {
  boundaries: number
  subscriptions: number
  usesI18nT: boolean
  importsFormat: boolean
  readsI18nextLanguage: boolean
  languageContextReads: number
} {
  const code = stripComments(src)
  const boundaries = [...code.matchAll(MEMO_BOUNDARY)].length
  const subscriptions = [...code.matchAll(/\buseLanguageGeneration\(\)/g)].length
  const usesI18nT = /\bi18nT\(/.test(code)
  const importsFormat = FORMAT_IMPORT.test(code)
  const readsI18nextLanguage = I18NEXT_LANGUAGE.test(code)
  const languageContextReads = [...code.matchAll(/\buseLanguage\(\)/g)].length
  return { boundaries, subscriptions, usesI18nT, importsFormat, readsI18nextLanguage, languageContextReads }
}

function sourceFiles(): string[] {
  return readdirSync(SRC, { recursive: true, encoding: 'utf8' })
    .map(p => p.split('\\').join('/'))
    .filter(p => /\.tsx?$/.test(p))
    .filter(p => !p.startsWith('test/') && !p.includes('__tests__') && !/\.test\.tsx?$/.test(p))
}

/** Every scanned file's source, read once so the import scan stays one pass. */
function sourceMap(): Map<string, string> {
  return new Map(sourceFiles().map(rel => [rel, readFileSync(join(SRC, rel), 'utf8')]))
}

describe('memo() + i18nT() subscription ratchet', () => {
  it('every memo() boundary in an i18nT()-using file calls useLanguageGeneration()', () => {
    const offenders: string[] = []
    for (const [rel, src] of sourceMap()) {
      if (EXEMPT.has(rel)) continue
      const { boundaries, subscriptions, usesI18nT } = scanFile(src)
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

  it('every memo() boundary in a language-formatting consumer subscribes or reads the language context', () => {
    const sources = sourceMap()
    const known = new Set(sources.keys())
    const wrappers = languageWrapperModules(sources)
    const offenders: string[] = []
    for (const [rel, src] of sources) {
      if (EXEMPT.has(rel)) continue
      const { boundaries, subscriptions, importsFormat, readsI18nextLanguage, languageContextReads } = scanFile(src)
      if (boundaries === 0) continue
      // One hop out: a wrapper's output is the seam's output, and the wrapper
      // cannot subscribe on its own, so this boundary owns the freshness.
      const viaWrappers = importedSpecifiers(src)
        .map(spec => resolveSpecifier(rel, spec, known))
        .filter((dep): dep is string => dep !== null && wrappers.has(dep))
      if (!importsFormat && !readsI18nextLanguage && viaWrappers.length === 0) continue
      // useLanguage() consumers re-render THROUGH memo on a switch (context
      // bypasses the props bailout) and format.ts reads the language at call
      // time, so either subscription shape keeps the boundary fresh.
      if (subscriptions + languageContextReads < boundaries) {
        const via = importsFormat
          ? 'i18n/format'
          : viaWrappers.length > 0
            ? `wrapper ${viaWrappers.join(', ')}`
            : 'i18next.language'
        offenders.push(
          `${rel}: ${boundaries} memo() boundaries, ${subscriptions} useLanguageGeneration() + ` +
          `${languageContextReads} useLanguage() calls (language-dependent via ${via})`,
        )
      }
    }
    expect(
      offenders,
      'src/i18n/format.ts formatters read the active language at call time, so a memoized component ' +
      'rendering their output goes stale after a language switch exactly like an i18nT() one -- whether ' +
      'it calls them directly, through a hook-free wrapper module one import hop away, or reads ' +
      'i18next.language itself. Each such memo() boundary must call useLanguageGeneration() or consume ' +
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
      readsI18nextLanguage: false,
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

  it('flags a direct i18next.language read with no seam import', () => {
    const r = scanFile(`import i18next from 'i18next'\nexport default memo(() => <a>{i18next.language}</a>)`)
    expect(r.readsI18nextLanguage).toBe(true)
    expect(r.importsFormat).toBe(false)
    expect(r.usesI18nT).toBe(false)
    expect(r.boundaries).toBe(1)
  })

  it('does not count an i18next.language mention in a comment', () => {
    expect(scanFile(`// reads i18next.language at call time\nconst X = memo(() => null)`).readsI18nextLanguage).toBe(false)
  })
})

describe('wrapper closure and import resolution', () => {
  it('collects relative and alias specifiers from every import form, and ignores packages', () => {
    const specs = importedSpecifiers(
      `import a from './a'\nimport { b } from '../b'\nimport '../side'\n` +
      `const c = await import('./c')\nimport d from '@/utils/d'\nimport react from 'react'\n` +
      `// import x from './commented'`,
    )
    expect(specs).toEqual(['./a', '../b', '../side', './c', '@/utils/d'])
  })

  it('resolves a specifier through .ts, .tsx and index files, and gives up outside the tree', () => {
    const known = new Set(['utils/timeAgo.ts', 'components/Row.tsx', 'apps/x/index.ts'])
    expect(resolveSpecifier('pages/ChatPage.tsx', '../utils/timeAgo', known)).toBe('utils/timeAgo.ts')
    expect(resolveSpecifier('pages/ChatPage.tsx', '../components/Row', known)).toBe('components/Row.tsx')
    expect(resolveSpecifier('pages/ChatPage.tsx', '../apps/x', known)).toBe('apps/x/index.ts')
    expect(resolveSpecifier('pages/ChatPage.tsx', '../styles/theme.css', known)).toBeNull()
  })

  it('resolves the @/ alias from the tree root, to the same file as the relative spelling', () => {
    const known = new Set(['utils/timeAgo.ts'])
    // The alias is depth-independent, so it must not be joined with the importer's dir.
    expect(resolveSpecifier('apps/issue-radar/views/deep/Card.tsx', '@/utils/timeAgo', known)).toBe('utils/timeAgo.ts')
    expect(resolveSpecifier('App.tsx', '@/utils/timeAgo', known)).toBe('utils/timeAgo.ts')
  })

  it('treats a hook-free seam importer as a wrapper', () => {
    const wrappers = languageWrapperModules(new Map([
      ['utils/timeAgo.ts', `import { fmtRelative } from '../i18n/format'\nexport const timeAgo = (t) => fmtRelative(t)`],
    ]))
    expect([...wrappers]).toEqual(['utils/timeAgo.ts'])
  })

  it('does not treat a hooked seam importer as a wrapper: it can subscribe itself', () => {
    const wrappers = languageWrapperModules(new Map([
      ['components/Row.tsx', `import { fmtDate } from '../i18n/format'\nexport const Row = () => { const [x] = useState(0); return fmtDate(x) }`],
    ]))
    expect([...wrappers]).toEqual([])
  })

  it('never treats the seam itself as a wrapper, and needs a real seam import', () => {
    const wrappers = languageWrapperModules(new Map([
      ['i18n/format.ts', `export const fmtDate = (d) => String(d)`],
      ['utils/plain.ts', `export const twice = (n) => n * 2`],
      ['utils/other.ts', `import { x } from '../i18n/formatters'\nexport const y = () => x`],
    ]))
    expect([...wrappers]).toEqual([])
  })

  it('closes over hook-free wrappers of wrappers, and stops at the first hooked module', () => {
    const wrappers = languageWrapperModules(new Map([
      ['utils/timeAgo.ts', `import { fmtRelative } from '../i18n/format'\nexport const timeAgo = (t) => fmtRelative(t)`],
      ['utils/stamp.ts', `import { timeAgo } from './timeAgo'\nexport const stamp = (t) => timeAgo(t)`],
      ['utils/label.ts', `import { stamp } from '@/utils/stamp'\nexport const label = (t) => stamp(t)`],
      ['hooks/useStamp.ts', `import { label } from '../utils/label'\nexport const useStamp = (t) => { const [v] = useState(label(t)); return v }`],
      ['components/Far.tsx', `import { useStamp } from '../hooks/useStamp'\nexport const Far = () => useStamp(1)`],
    ]))
    // Three hops of hook-free modules, reached through both spellings.
    expect([...wrappers].sort()).toEqual(['utils/label.ts', 'utils/stamp.ts', 'utils/timeAgo.ts'])
    // The hooked module owns its own freshness, so the walk does not continue past it.
    expect(wrappers.has('hooks/useStamp.ts')).toBe(false)
    expect(wrappers.has('components/Far.tsx')).toBe(false)
  })

  it('the defect shape is a memo boundary whose only language input is a wrapper', () => {
    const sources = new Map([
      ['utils/timeAgo.ts', `import { fmtRelative } from '../i18n/format'\nexport const timeAgo = (t) => fmtRelative(t)`],
      ['components/Row.tsx', `import { timeAgo } from '@/utils/timeAgo'\nexport default memo(({ t }) => <a>{timeAgo(t)}</a>)`],
    ])
    const wrappers = languageWrapperModules(sources)
    const row = sources.get('components/Row.tsx')!
    const r = scanFile(row)
    // Neither narrower branch sees this file at all.
    expect(r.usesI18nT).toBe(false)
    expect(r.importsFormat).toBe(false)
    expect(r.boundaries).toBe(1)
    expect(r.subscriptions + r.languageContextReads).toBe(0)
    const via = importedSpecifiers(row)
      .map(spec => resolveSpecifier('components/Row.tsx', spec, new Set(sources.keys())))
      .filter(dep => dep !== null && wrappers.has(dep))
    expect(via).toEqual(['utils/timeAgo.ts'])
  })

  it('the live tree still has wrapper modules for the branch to act on', () => {
    // A regex tightened against the tree could silently empty this set and turn
    // the widened branch into a no-op, so pin the population and the modules
    // #5922 enumerated. utils/cronUtils.tsx is deliberately not pinned: it is
    // the one of the five likeliest to gain a hook legitimately.
    const wrappers = languageWrapperModules(sourceMap())
    expect(wrappers.size).toBeGreaterThan(0)
    for (const rel of ['utils/timeAgo.ts', 'utils/formatCost.ts', 'utils/scheduleCadence.ts', 'apps/issue-radar/lib/format.ts']) {
      expect(wrappers.has(rel), `expected wrapper: ${rel}`).toBe(true)
    }
  })
})
