/**
 * Which i18n module an entry point boots through.
 *
 * `src/i18n/index.ts` imports the English catalog only; `src/i18n/all.ts` adds the
 * other twelve. Both export `initI18n` with the same signature, so a page entry can
 * boot through either one and `tsc` is happy either way — but through the
 * English-only module the dashboard renders English for a user who picked Japanese.
 * Nothing suspends, nothing throws, no key renders raw: i18next simply falls back.
 * No test goes red and the diff shows one plausible import path, which is why this
 * guard is a source-level scan rather than a behavioural one.
 *
 * Both directions of the split are pinned, and each is anchored on a PROPERTY of the
 * module graph rather than on a module name: which catalogs the entry's transitive
 * static imports reach. Renaming `all.ts`, folding `catalogs.ts` back into it, or
 * adding a thirteenth language all keep this green; pointing an entry at the
 * English-only module is what fails it.
 */
import { describe, it, expect } from 'vitest'
import { existsSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import ts from 'typescript'

import { SUPPORTED_CODES } from '../i18n/languages'
import { readSource } from './readSource'

const SRC = resolve(__dirname, '..')
const WEBSITE = resolve(SRC, '..')
const LOCALES = join(SRC, 'i18n', 'locales')

/**
 * Trees under `website/` that hold no page entry, so the scan skips them.
 *
 * This is a DENYLIST on purpose, because the two failure modes are not symmetric. A
 * missing denylist entry costs a red test somebody fixes in a minute. A missing
 * ALLOWLIST root costs nothing until a user sees the wrong language — and the roots
 * that matter most are the least obvious: `capture/` holds ~40 page entries, eleven
 * of them running a non-English language, one driven by a script that waits on a
 * Chinese string and would hang for its full timeout. So a new tree of pages is
 * governed by default, and opting one out is a visible edit here.
 *
 * `integration/` is listed because the vitest setup file SHOULD reach the
 * English-only entry — that is the property the second describe block asserts.
 */
const NON_ENTRY_TREES = new Set([
  'node_modules', 'dist', 'build', 'coverage', 'public', 'docs', 'scripts',
  'integration', 'playwright', 'electron', 'eslint-rules', 'temp-screenshots',
])

/**
 * Trees the call-site scan does not descend into.
 *
 * The test-support ones are deliberate, not an oversight: a helper or a setup file
 * SHOULD reach for the English-only entry, which is the whole point of the split.
 */
const SKIP_DIRS = new Set(['node_modules', 'locales', 'test', 'tests', '__tests__', '__mocks__'])

/** Extensions a bare specifier can resolve to, in the order Vite tries them. */
const RESOLVE_SUFFIXES = ['', '.ts', '.tsx', '.json', '/index.ts', '/index.tsx']

const rel = (file: string) => relative(WEBSITE, file).split('\\').join('/')

/**
 * The language a catalog file carries.
 *
 * A code may be spread over several files (`en.json` + `en.manual.json`), so the
 * qualifier after the code is dropped: what matters for registration is the set of
 * LANGUAGES reached, not the file count. `en-XA` keeps its hyphenated code — the
 * pseudolocale is a separate language, not a variant of the English catalog.
 */
function languageCodeOf(basename: string): string {
  return basename.split('.')[0]
}

/**
 * The languages a production entry must reach — the DECLARED set, not the disk.
 *
 * `SUPPORTED_CODES` is the list the picker offers and `resolveLanguage` accepts, so
 * it is the set a user can actually ask for. Reading `locales/` instead would make a
 * stray unregistered `nl.json` demand that every entry reach it, which is not the
 * rule this file states. `catalogParity.test.ts` pins the declared list against the
 * catalog map in both directions, so the two cannot drift apart unnoticed.
 */
const ALL_LANGUAGES = new Set<string>(SUPPORTED_CODES)

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name) && !NON_ENTRY_TREES.has(entry.name)) walk(full, out)
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      out.push(full)
    }
  }
  return out
}

function parse(file: string, source: string): ts.SourceFile {
  return ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
}

/**
 * Resolve a module specifier to a file, or null when it leaves the repo.
 *
 * Only relative and `@/`-aliased specifiers are followed; a bare specifier is a
 * package, and a `.css` import resolves to none of the candidate suffixes. Both
 * are dead ends for a catalog, so returning null skips them.
 */
function resolveModule(fromFile: string, specifier: string): string | null {
  let base: string
  if (specifier.startsWith('.')) base = resolve(dirname(fromFile), specifier)
  else if (specifier.startsWith('@/')) base = join(SRC, specifier.slice(2))
  else return null

  for (const suffix of RESOLVE_SUFFIXES) {
    const candidate = base + suffix
    if (existsSync(candidate) && statSync(candidate).isFile()) return candidate
  }
  return null
}

/** Every static `import`/`export … from` specifier in a parsed module. */
function staticSpecifiers(sf: ts.SourceFile): string[] {
  const out: string[] = []
  for (const statement of sf.statements) {
    if (
      (ts.isImportDeclaration(statement) || ts.isExportDeclaration(statement))
      && statement.moduleSpecifier
      && ts.isStringLiteral(statement.moduleSpecifier)
    ) {
      out.push(statement.moduleSpecifier.text)
    }
  }
  return out
}

/**
 * The languages an entry module registers, via its transitive static imports.
 *
 * Static reachability is the right measure precisely because `t()` is synchronous:
 * a catalog that is not statically imported is not registered before first render,
 * so nothing renders it. `export … from` is followed as well as `import` — the
 * all-languages entry re-exports the runtime API that way.
 */
function languagesReachedFrom(entry: string): Set<string> {
  const reached = new Set<string>()
  const seen = new Set<string>([entry])
  const queue = [entry]

  while (queue.length) {
    const file = queue.pop()!
    if (file.startsWith(LOCALES) && file.endsWith('.json')) {
      reached.add(languageCodeOf(file.slice(LOCALES.length + 1)))
      continue
    }
    if (!/\.tsx?$/.test(file)) continue
    for (const specifier of staticSpecifiers(parse(file, readSource(file)))) {
      const next = resolveModule(file, specifier)
      if (next && !seen.has(next)) {
        seen.add(next)
        queue.push(next)
      }
    }
  }
  return reached
}

/** A non-test module that calls `initI18n`, and where it imports the call from. */
interface CallSite {
  file: string
  /** Specifiers supplying `initI18n`; more than one would be a genuine oddity. */
  specifiers: string[]
  /**
   * Every `initI18n` call here passes the literal `'en'`, so English is all it can
   * ever render and the English-only entry is the correct import.
   *
   * A bare `initI18n()` is NOT exempt: with no argument it resolves the language from
   * `readStoredLanguage()`, so it can land on any of the twelve.
   */
  englishOnly: boolean
}

/**
 * Find the call sites by AST, not by text match.
 *
 * Comments are not AST nodes, so a module that only mentions `initI18n()` in prose
 * is skipped for free, and so is the declaration in `i18n/index.ts` — it exports the
 * function without calling it. A module that both declares and calls its own
 * `initI18n` is a redeclaration, not a boot path, so it is skipped explicitly.
 */
function findCallSites(): CallSite[] {
  const sites: CallSite[] = []

  for (const file of walk(WEBSITE)) {
    const source = readSource(file)
    if (!source.includes('initI18n')) continue

    const sf = parse(rel(file), source)
    const specifiers = new Set<string>()
    let calls = false
    let declares = false
    let nonEnglishCall = false

    const visit = (node: ts.Node): void => {
      if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
        const bindings = node.importClause?.namedBindings
        const supplies = bindings
          && ((ts.isNamedImports(bindings)
            && bindings.elements.some((e) => (e.propertyName ?? e.name).text === 'initI18n'))
            // A namespace import reaches the same function as `ns.initI18n()`.
            || ts.isNamespaceImport(bindings))
        if (supplies) specifiers.add(node.moduleSpecifier.text)
      }
      if (ts.isCallExpression(node)) {
        const callee = node.expression
        if (
          (ts.isIdentifier(callee) && callee.text === 'initI18n')
          || (ts.isPropertyAccessExpression(callee) && callee.name.text === 'initI18n')
        ) {
          calls = true
          const [arg] = node.arguments
          // Anything that is not the literal 'en' — including no argument at all,
          // which falls through to the stored language — can render another language.
          if (!(arg && ts.isStringLiteral(arg) && arg.text === 'en')) nonEnglishCall = true
        }
      }
      // Switching after init reaches another catalog just as much as booting into
      // one, and neither `changeLanguage` nor a mounted `<LanguageProvider>` shows up
      // in an `initI18n` argument. Without this, an entry pinned to `initI18n('en')`
      // that renders the language picker would be exempt and broken.
      if (
        (ts.isIdentifier(node) || ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node))
        && /^(changeLanguage|LanguageProvider)$/.test(
          ts.isIdentifier(node) ? node.text : node.tagName.getText(sf),
        )
      ) {
        nonEnglishCall = true
      }
      if (
        (ts.isFunctionDeclaration(node) || ts.isVariableDeclaration(node))
        && node.name
        && ts.isIdentifier(node.name)
        && node.name.text === 'initI18n'
      ) {
        declares = true
      }
      ts.forEachChild(node, visit)
    }
    visit(sf)

    if (calls && !declares) {
      sites.push({ file, specifiers: [...specifiers], englishOnly: !nonEnglishCall })
    }
  }

  return sites
}

/** `setupFiles` as vitest reads it: one string, or an array of them. */
function setupFileSpecifiers(config: string): string[] {
  const list = config.match(/setupFiles:\s*\[([^\]]*)\]/)
  if (list) return [...list[1].matchAll(/'([^']+)'/g)].map((m) => m[1])
  const single = config.match(/setupFiles:\s*'([^']+)'/)
  return single ? [single[1]] : []
}

const CALL_SITES = findCallSites()

describe('every entry point that boots i18n registers all languages', () => {
  it('has a catalog per authored language to scan for', () => {
    // Without this the main assertion is satisfiable by an empty language set, which
    // is exactly the vacuous pass a guard is written to avoid.
    expect(ALL_LANGUAGES.size).toBeGreaterThanOrEqual(12)
  })

  it('reaches every tree that holds an entry, not just src/', () => {
    expect(CALL_SITES.length).toBeGreaterThan(0)

    // Named trees must each contribute, or the walk quietly stopped descending and
    // every assertion below is vacuous for whatever it no longer sees. `capture/` is
    // listed because it is the tree a scan rooted at `src/` would silently omit.
    // `src/i18n` is deliberately absent: it DECLARES `initI18n` and never calls it.
    for (const tree of ['capture/', 'src/apps/', 'src/main.tsx']) {
      expect(
        CALL_SITES.some((site) => rel(site.file).startsWith(tree)),
        `no initI18n call site found under ${tree} — the walk is not reaching it`,
      ).toBe(true)
    }
  })

  it('imports initI18n from a module whose graph reaches every catalog', () => {
    const offenders: string[] = []

    for (const { file, specifiers, englishOnly } of CALL_SITES) {
      // A screenshot entry pinned to initI18n('en') renders English by construction,
      // so the English-only import is right and pulling in twelve more catalogs would
      // only slow it down.
      if (englishOnly) continue
      if (specifiers.length === 0) {
        offenders.push(`${rel(file)} — calls initI18n() but imports it from nowhere findable`)
        continue
      }
      for (const specifier of specifiers) {
        const entry = resolveModule(file, specifier)
        if (!entry) {
          offenders.push(`${rel(file)} — cannot resolve '${specifier}' to a file`)
          continue
        }
        const reached = languagesReachedFrom(entry)
        const missing = [...ALL_LANGUAGES].filter((code) => !reached.has(code))
        if (missing.length) {
          offenders.push(
            `${rel(file)} — '${specifier}' registers no catalog for ${missing.sort().join(', ')}`,
          )
        }
      }
    }

    expect(
      offenders,
      'A page entry point must import `initI18n` from the ALL-LANGUAGES i18n entry '
        + '(`src/i18n/all.ts`), not from `src/i18n/index.ts`. `index.ts` deliberately '
        + 'imports the English catalog only, so booting through it registers English '
        + 'and nothing else: i18next then falls back for every other language and the '
        + 'dashboard renders English to a user who picked Japanese. Nothing throws and '
        + 'no key renders raw, so this scan is the only thing that reports it. Change '
        + "the import to '<path>/i18n/all' — same exports, same synchronous `t()`.",
    ).toEqual([])
  }, 20_000)
})

describe('the vitest setup module graph stays English-only', () => {
  /**
   * The other direction, and the property the split exists for.
   *
   * A `setupFiles` module graph is re-evaluated once per test FILE — ~1400 times —
   * so every catalog it reaches is re-fetched that many times. Reaching all fourteen
   * cost more setup than the whole suite spent running tests; `docs/testing.md`
   * § "What a `setupFiles` entry costs" owns the measurements. Moving one import back into `src/i18n/index.ts`
   * while "tidying up" restores the whole cost with every test still passing, which
   * is why the ceiling is asserted rather than left to a benchmark nobody reruns.
   */
  const specifiers = setupFileSpecifiers(readSource(join(WEBSITE, 'vite.config.ts')))

  it('reads setupFiles out of vite.config.ts', () => {
    expect(specifiers.length).toBeGreaterThan(0)
  })

  for (const specifier of specifiers) {
    it(`'${specifier}' reaches no catalog beyond English`, () => {
      const entry = resolveModule(join(WEBSITE, 'vite.config.ts'), specifier)
      expect(entry, `setupFiles entry '${specifier}' does not resolve to a file`).not.toBeNull()

      const extra = [...languagesReachedFrom(entry!)].filter((code) => code !== 'en').sort()
      expect(
        extra,
        `The vitest setup graph statically imports the ${extra.join(', ')} catalog(s). `
          + 'It is re-evaluated once per test file (~1400 times), so each of those '
          + 'catalogs is re-fetched ~1400 times, which costs the suite more setup '
          + 'than it spends running tests. Keep the non-English imports '
          + 'in `src/i18n/catalogs.ts`, reached only through `src/i18n/all.ts`, and let '
          + 'a test that needs another language import that entry itself.',
      ).toEqual([])
    })
  }
})
