/**
 * The React Compiler opt-in list (react-compiler.config.mjs) holds only files
 * the compiler compiles completely. The build runs the compiler with its
 * default panicThreshold, which emits a bailed function as written, so without
 * this test a listed component could stop being compiled and nothing would say
 * so. Each listed file is compiled here with the build's options and fails on
 * any function the compiler reports as bailed or skipped.
 *
 * The control cases pin the detector itself. The clean control carries a
 * destructuring default, which Babel 8 with compiler 1.0 reports as a false
 * error, so a Babel major bump that breaks the scan fails here first.
 */
import { describe, expect, it } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { transformSync } from '@babel/core'
import reactCompiler from 'babel-plugin-react-compiler'
import {
  REACT_COMPILER_FILES,
  REACT_COMPILER_OPTIONS,
  reactCompilerIdFilter,
} from '../../react-compiler.config.mjs'

const WEBSITE = path.resolve(__dirname, '../..')

interface CompileReport {
  compiled: string[]
  problems: string[]
}

function compile(code: string, filename: string): CompileReport {
  const report: CompileReport = { compiled: [], problems: [] }
  const logger = {
    logEvent(_file: string | null, e: {
      kind: string
      fnName?: string | null
      fnLoc?: { start: { line: number } } | null
      reason?: string
      detail?: { reason?: string; options?: { reason?: string } }
      data?: unknown
    }) {
      const line = e.fnLoc ? `line ${e.fnLoc.start.line}` : 'file'
      if (e.kind === 'CompileSuccess') report.compiled.push(e.fnName ?? line)
      else if (e.kind === 'CompileError') report.problems.push(`${line}: ${e.detail?.options?.reason ?? e.detail?.reason ?? 'CompileError'}`)
      else if (e.kind === 'CompileSkip') report.problems.push(`${line}: skipped (${e.reason ?? 'no reason'})`)
      else if (e.kind === 'PipelineError') report.problems.push(`${line}: pipeline error ${String(e.data).slice(0, 200)}`)
    },
  }
  transformSync(code, {
    filename,
    babelrc: false,
    configFile: false,
    code: false,
    parserOpts: { plugins: ['typescript', ...(filename.endsWith('x') ? ['jsx' as const] : [])] },
    plugins: [[reactCompiler, { ...REACT_COMPILER_OPTIONS, panicThreshold: 'none', logger }]],
  })
  return report
}

const IMPORTS_TRANSLATOR = /from\s+'[^']*\bi18n\/(t|format)'/
const SUBSCRIBES = /\b(useLanguageGeneration|useTranslation)\(\)/

/** Resolve an import specifier of `fromRel` to a website-relative source path. */
function resolveModule(fromRel: string, spec: string): string | null {
  const base = spec.startsWith('@/')
    ? path.join(WEBSITE, 'src', spec.slice(2))
    : path.resolve(path.dirname(path.join(WEBSITE, fromRel)), spec)
  for (const cand of [base, `${base}.tsx`, `${base}.ts`, path.join(base, 'index.tsx'), path.join(base, 'index.ts')]) {
    if (/\.tsx?$/.test(cand) && existsSync(cand)) return path.relative(WEBSITE, cand).replace(/\\/g, '/')
  }
  return null
}

const SPECIFIER = /(?:\bfrom|\bimport\()\s*'((?:\.{1,2}|@)\/[^']+)'/g
const REEXPORT = /\bexport\s+(?:\*|\{[^}]*\}|\*\s+as\s+\w+)\s+from\s+'((?:\.{1,2}|@)\/[^']+)'/g

/**
 * The `.tsx` modules a listed file can render: its static and lazy imports, plus
 * whatever those modules re-export, so a barrel or a re-exporting wrapper does
 * not hide the module that translates.
 */
function childModules(rel: string): Set<string> {
  const out = new Set<string>()
  const src = readFileSync(path.join(WEBSITE, rel), 'utf8')
  for (const m of src.matchAll(SPECIFIER)) {
    // Only `.tsx` modules render; a `.ts` helper the file calls is covered by
    // the generation-argument rule above.
    const child = resolveModule(rel, m[1])
    if (!child || !child.endsWith('.tsx') || out.has(child)) continue
    out.add(child)
    const childSrc = readFileSync(path.join(WEBSITE, child), 'utf8')
    for (const r of childSrc.matchAll(REEXPORT)) {
      const target = resolveModule(child, r[1])
      if (target?.endsWith('.tsx')) out.add(target)
    }
  }
  return out
}

describe('React Compiler allowlist', () => {
  it('lists unique, sorted, existing non-test source files', () => {
    expect([...new Set(REACT_COMPILER_FILES)]).toEqual(REACT_COMPILER_FILES)
    expect([...REACT_COMPILER_FILES].sort()).toEqual(REACT_COMPILER_FILES)
    for (const f of REACT_COMPILER_FILES) {
      expect(f).toMatch(/^src\/[^\\]+\.tsx?$/)
      expect(f).not.toMatch(/\.(test|spec|stories)\.tsx?$/)
      expect(existsSync(path.join(WEBSITE, f)), f).toBe(true)
    }
  })

  // A listed file makes the language an input of what the compiler caches in
  // one of two ways. Direct strings come from `useTranslation()`'s `t`, whose
  // identity changes on a language switch (see UserMessage). Strings built by
  // helpers that call `i18nT()` or the `i18n/format` functions internally go
  // through one function that takes `useLanguageGeneration()` as an argument
  // (see ChatPaneNotices' `paneCopy`). `i18nT()` read straight in the body is
  // cached on unrelated inputs and keeps the previous language.
  it.each(REACT_COMPILER_FILES)('%s makes the language an input when it translates', (f) => {
    const src = readFileSync(path.join(WEBSITE, f), 'utf8')
    if (!IMPORTS_TRANSLATOR.test(src)) return
    // The generation has to be read into a name and passed on: a bare
    // `useLanguageGeneration()` statement re-renders the component but feeds
    // the compiler nothing, so the cached strings stay as they were.
    const decl = src.match(/\bconst (\w+) = useLanguageGeneration\(\)/)
    expect(decl, `${f} calls i18nT/format helpers without reading useLanguageGeneration() into a name`).not.toBeNull()
    const uses = src.match(new RegExp(`\\b${decl![1]}\\b`, 'g')) ?? []
    expect(uses.length, `${f} reads the language generation but never passes it on`).toBeGreaterThan(1)
  })

  // A render prop's output is built by the parent and can hold components that
  // translate without subscribing. A compiled file would reuse that output
  // while the content arguments are unchanged, so every `renderXxx(...)` call
  // must go through a helper that also takes the translator or the language
  // generation (see UserMessage's `renderWithLanguage`).
  it.each(REACT_COMPILER_FILES)('%s rebuilds render-prop output on a language switch', (f) => {
    const src = readFileSync(path.join(WEBSITE, f), 'utf8')
    const bare = [...src.matchAll(/(?<![\w.])(render[A-Z]\w*)\(/g)]
      .map(m => m[1])
      .filter(name => !new RegExp(`function ${name}\\b`).test(src))
    expect(bare, `${f} calls render props directly; pass them through a helper that takes t or the language generation`).toEqual([])
  })

  // A compiled component caches the elements it renders while their props are
  // unchanged, so a child it imports becomes a memo boundary: on a language
  // switch the child is not re-rendered unless it subscribes itself. Every
  // module a listed file imports, statically or through a lazy `import()`,
  // relative or through the `@/` alias, and every module such a file
  // re-exports, that translates must subscribe through `useLanguageGeneration()`
  // or `useTranslation()`.
  // Child modules that translate but need no subscription, with the reason.
  const LANGUAGE_EXEMPT: Record<string, string> = {
    // The listed files import `Btn` from here, which renders no catalog text;
    // the translating exports of this module are not rendered by them.
    'src/components/ui.tsx': 'Btn renders no catalog text',
  }
  // Class components cannot call the hook, so the listed file passes the
  // generation as a prop instead; every element of the component must carry it.
  const LANGUAGE_PROP: Record<string, string> = {
    'src/components/ErrorBoundary.tsx': 'ErrorBoundary',
  }

  it.each(REACT_COMPILER_FILES)('%s renders only children that follow a language switch', (f) => {
    const src = readFileSync(path.join(WEBSITE, f), 'utf8')
    const stale: string[] = []
    for (const rel of childModules(f)) {
      if (rel in LANGUAGE_EXEMPT) continue
      if (rel in LANGUAGE_PROP) {
        const tag = LANGUAGE_PROP[rel]
        const opens = src.match(new RegExp(`<${tag}\\b[^>]*>`, 'g')) ?? []
        if (opens.some(o => !/\blanguageGeneration=\{/.test(o))) stale.push(`${rel} (pass languageGeneration)`)
        continue
      }
      const childSrc = readFileSync(path.join(WEBSITE, rel), 'utf8')
      if (IMPORTS_TRANSLATOR.test(childSrc) && !SUBSCRIBES.test(childSrc)) stale.push(rel)
    }
    expect(stale, `${f} renders translating children that do not subscribe to language switches`).toEqual([])
  })

  it.each(REACT_COMPILER_FILES)('%s compiles with no bailout', (f) => {
    const report = compile(readFileSync(path.join(WEBSITE, f), 'utf8'), path.join(WEBSITE, f))
    expect(report.problems, `${f} bails; fix it or drop it from react-compiler.config.mjs`).toEqual([])
    expect(report.compiled.length, `${f} has no component or hook to compile`).toBeGreaterThan(0)
  })
})

describe('React Compiler bailout detector', () => {
  it('compiles a clean component, destructuring default included', () => {
    const report = compile(
      'export function Badge({ label = "x" }: { label?: string }) { return <span>{label}</span> }\n',
      path.join(WEBSITE, 'src/Badge.tsx'),
    )
    expect(report).toEqual({ compiled: ['Badge'], problems: [] })
  })

  it('reports a ref read during render', () => {
    const report = compile(
      'import { useRef } from "react"\n' +
        'export function Latest({ v }: { v: number }) { const r = useRef(v); return <i>{r.current}</i> }\n',
      path.join(WEBSITE, 'src/Latest.tsx'),
    )
    expect(report.compiled).toEqual([])
    expect(report.problems).toHaveLength(1)
  })

  it('reports a "use no memo" opt-out', () => {
    const report = compile(
      'export function Plain() { "use no memo"; return <b /> }\n',
      path.join(WEBSITE, 'src/Plain.tsx'),
    )
    expect(report.compiled).toEqual([])
    expect(report.problems).toHaveLength(1)
  })
})

describe('React Compiler child-module scan', () => {
  it('resolves relative and @/ imports, lazy imports and re-exports', () => {
    const kids = childModules('src/pages/chat/page/ChatPaneNotices.tsx')
    expect(kids).toContain('src/components/ErrorNotice.tsx')
    expect(kids).toContain('src/pages/chat/page/UnrestoredTabsNotice.tsx')
    expect(resolveModule('src/pages/chat/UserMessage.tsx', '@/components/InfoTip')).toBe('src/components/InfoTip.tsx')
    expect(resolveModule('src/pages/chat/UserMessage.tsx', './missing-module')).toBeNull()
  })
})

describe('reactCompilerIdFilter', () => {
  const files = ['src/components/ChatInput.tsx', 'src/pages/ChatPage.tsx']

  it('is null for an empty list', () => {
    expect(reactCompilerIdFilter('/w/website', [])).toBeNull()
  })

  it('matches listed ids with or without a query, and nothing else', () => {
    const re = reactCompilerIdFilter('/w/website/', files)!
    expect(re.test('/w/website/src/components/ChatInput.tsx')).toBe(true)
    expect(re.test('/w/website/src/pages/ChatPage.tsx?v=1')).toBe(true)
    expect(re.test('/w/website/src/components/ChatInputBar.tsx')).toBe(false)
    expect(re.test('/w/website/src/components/ChatInput.tsx.bak')).toBe(false)
    expect(re.test('/x/w/website/src/pages/ChatPage.tsx')).toBe(false)
    expect(re.test('/w/websiteXsrc/pages/ChatPage.tsx')).toBe(false)
  })

  it('normalizes a Windows root to the forward-slash ids Vite uses', () => {
    const re = reactCompilerIdFilter('C:\\w\\website', files)!
    expect(re.test('C:/w/website/src/components/ChatInput.tsx')).toBe(true)
  })
})
