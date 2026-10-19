/**
 * DesignTweakPage.tsx and the owner beside it.
 *
 * The page file stays the Design Tweak route's one import path (`builtinRegistry`
 * lazy-loads it, and the capture harness and the specs import it by that id) and
 * renders the rail and the preview; its state, effects and actions live in
 * `useDesignTweakPageState`, bar `deleteReq`, whose native `confirm()` keeps it
 * in the page. These pins keep that shape honest:
 *
 *  - the page still exports what it exported as one file;
 *  - the owner never imports the page, and nothing outside the page under
 *    `src/`, `capture/` or `integration/` (bar specs) imports the owner;
 *  - the page's state and effect hooks all register inside
 *    `useDesignTweakPageState`, which the component calls as its one such hook,
 *    so the page keeps one registration order however its markup changes.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import ts from 'typescript'

import * as facade from './DesignTweakPage'
import { pageFamilyFiles } from '../../test/pageFamily'

const DIR = __dirname
const SRC = resolve(DIR, '..', '..')
const WEBSITE = resolve(SRC, '..')
const FILES = pageFamilyFiles('DesignTweakPage', DIR)
const OWNERS = FILES.slice(1)
const read = (p: string) => readFileSync(p, 'utf8')

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.|\.spec\./.test(name)) out.push(p)
  }
  return out
}

/** Every module a file imports (static, side-effect, `export … from` and
 *  dynamic), resolved to an absolute path without its extension. `@/` is the
 *  `src/` alias. */
const SPEC_RE = /(?:from\s+|import\s*\(?)\s*['"]([^'"]+)['"]/g
const stem = (p: string) => p.replace(/\.(tsx?|jsx?)$/, '').replace(/\/index$/, '')
function importsOf(file: string): string[] {
  return [...read(file).matchAll(SPEC_RE)].map((m) => m[1]).flatMap((spec) => {
    if (spec.startsWith('@/')) return [stem(join(SRC, spec.slice(2)))]
    if (spec.startsWith('.')) return [stem(resolve(dirname(file), spec))]
    return []
  })
}

/** The `use*` calls a function body makes outside its nested functions, in
 *  order. `useMemo` and `useCallback` register no effect and own no state. */
const MEMOS = new Set(['useMemo', 'useCallback'])
function bodyOf(file: string, name: string): ts.Block {
  const sf = ts.createSourceFile(file, read(file), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const fn = sf.statements.find((s): s is ts.FunctionDeclaration => ts.isFunctionDeclaration(s) && s.name?.text === name)
  if (!fn?.body) throw new Error(`${name} is not a function in ${relative(SRC, file)}`)
  return fn.body
}
function hookCalls(body: ts.Block): string[] {
  const out: string[] = []
  const visit = (n: ts.Node) => {
    if (n !== body && ts.isFunctionLike(n)) return
    ts.forEachChild(n, visit)
    if (!ts.isCallExpression(n) || !ts.isIdentifier(n.expression)) return
    const name = n.expression.text
    if (/^use[A-Z]/.test(name) && !MEMOS.has(name)) out.push(name)
  }
  visit(body)
  return out
}

describe('DesignTweakPage owners', () => {
  it('are the files the page is made of', () => {
    expect(OWNERS).toEqual(['useDesignTweakPageState.ts'])
  })

  it('include every sibling the page imports under its name', () => {
    // A part named off the family pattern would drop out of `pageFamilyFiles`,
    // and every pin that reads the page through it would stop seeing that part.
    const named = /^(DesignTweakPage|designTweakPage|useDesignTweakPage)[A-Z.]/
    const family = new Set(FILES.map((f) => stem(join(DIR, f))))
    const siblings = importsOf(join(DIR, 'DesignTweakPage.tsx'))
      .filter((t) => dirname(t) === DIR && named.test(t.slice(DIR.length + 1)))
    expect(siblings.length).toBeGreaterThan(0)
    expect(siblings.filter((t) => !family.has(t))).toEqual([])
  })

  it('leave the page exporting what it exported as one file', () => {
    expect(Object.keys(facade)).toEqual(expect.arrayContaining(['default']))
    expect(typeof facade.default).toBe('function')
  })

  it('never import the facade', () => {
    const facadePath = stem(join(DIR, 'DesignTweakPage.tsx'))
    expect(OWNERS.filter((f) => importsOf(join(DIR, f)).includes(facadePath))).toEqual([])
  })

  it('are imported only by the facade and by each other', () => {
    const family = new Set(FILES.map((f) => stem(join(DIR, f))))
    const owners = new Set(OWNERS.map((f) => stem(join(DIR, f))))
    const roots = [SRC, join(WEBSITE, 'capture'), join(WEBSITE, 'integration')]
    const offenders = roots.flatMap((root) => walk(root))
      .filter((file) => !family.has(stem(file)))
      .flatMap((file) => importsOf(file).filter((t) => owners.has(t)).map((t) => `${relative(WEBSITE, file)} -> ${relative(WEBSITE, t)}`))
    expect(offenders).toEqual([])
  })

  it('register the page hooks in one place', () => {
    // `useDesignTweakPageState` holds the page's state and effect hooks in the
    // order the page registers them; the component calls it once, so a state or
    // effect hook added to the component would interleave a second sequence.
    expect(hookCalls(bodyOf(join(DIR, 'DesignTweakPage.tsx'), 'DesignTweak'))).toEqual(['useDesignTweakPageState'])
    expect(hookCalls(bodyOf(join(DIR, 'useDesignTweakPageState.ts'), 'useDesignTweakPageState')).length).toBeGreaterThan(0)
    // One instance per page: a second caller in the page file would mount a
    // second copy of every state, effect and poll.
    expect(read(join(DIR, 'DesignTweakPage.tsx')).match(/\buseDesignTweakPageState\(/g)).toHaveLength(1)
  })

  it('reads imports the way the pins above rely on', () => {
    const probe = join(DIR, 'probe.tsx')
    const text = "import './Side'\nimport type { T } from '@/apps/Typed'\nexport { X } from './Re'\nconst L = () => import('./Lazy')\n"
    const found = [...text.matchAll(SPEC_RE)].map((m) => m[1])
    expect(found).toEqual(['./Side', '@/apps/Typed', './Re', './Lazy'])
    expect(stem(resolve(dirname(probe), './Side.tsx'))).toBe(join(DIR, 'Side'))
    expect(walk(join(WEBSITE, 'capture')).length).toBeGreaterThan(0)
  })

  it('reads hooks the way the pin above relies on', () => {
    const probe = 'function P() { const a = useA(); useEffect(() => { useNested() }); const b = useMemo(() => 1, []); return useB(a, b) }'
    const sf = ts.createSourceFile('probe.tsx', probe, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
    const fn = sf.statements[0] as ts.FunctionDeclaration
    expect(hookCalls(fn.body!)).toEqual(['useA', 'useEffect', 'useB'])
  })
})
