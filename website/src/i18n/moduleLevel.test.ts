/**
 * `i18nT()` must not be evaluated at module load.
 *
 * A call in module scope runs once, when the module is first imported — before the
 * user has picked a language, and never again. The string it returns is frozen at
 * the boot language for the life of the tab, so the nav rail or a tab table stays
 * English while everything around it translates. That is the defect `labelKey` +
 * `surfaceLabel()` exists to fix, and the codemod already skips these sites on
 * purpose (`insideFunction()` in `scripts/i18n-codemod.mjs`).
 *
 * ## Why this uses the TypeScript AST and `dynamicKeys.test.ts` uses a regex
 *
 * Because the distinction is genuinely syntactic. These are safe — the call sits
 * inside a function body and runs per invocation:
 *
 *     const label = (t: string) => i18nT('a.b')
 *     function label() { return i18nT('a.b') }
 *
 * and these are not — the call runs at import:
 *
 *     const LABEL = i18nT('a.b')
 *     const TABS = [{ label: i18nT('a.b') }]
 *
 * A regex cannot separate them reliably: an arrow function with an expression body
 * has no brace to count, so a depth counter reports `const f = () => i18nT(…)` as
 * module scope. A first pass at this check did exactly that and produced three false
 * positives in `pages/chat/ActivityViewer.tsx` alone. `typescript` is already a
 * devDependency and the codemod already walks the AST for the same question, so the
 * correct tool is available.
 */

import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import ts from 'typescript'

const SRC = join(__dirname, '..')

/** The translate function's own declaration is not a call site. */
const NOT_A_CALL_SITE = new Set(['i18n/t.ts'])

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === 'locales') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) out.push(full)
  }
  return out
}

/**
 * Line numbers of `i18nT(...)` calls that are not lexically inside any function.
 *
 * Class bodies count as module scope for a property initialiser, since those also
 * run at construction rather than at render.
 */
function moduleLevelCalls(file: string, source: string): number[] {
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const hits: number[] = []

  const isFunctionLike = (n: ts.Node) =>
    ts.isFunctionDeclaration(n)
    || ts.isFunctionExpression(n)
    || ts.isArrowFunction(n)
    || ts.isMethodDeclaration(n)
    || ts.isConstructorDeclaration(n)
    || ts.isGetAccessor(n)
    || ts.isSetAccessor(n)

  const visit = (node: ts.Node, insideFunction: boolean) => {
    if (
      !insideFunction
      && ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === 'i18nT'
    ) {
      hits.push(sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1)
    }
    const nowInside = insideFunction || isFunctionLike(node)
    ts.forEachChild(node, (child) => visit(child, nowInside))
  }

  visit(sf, false)
  return hits
}

describe('i18nT is never evaluated at module load', () => {
  const files = walk(SRC).filter(
    (f) => !NOT_A_CALL_SITE.has(relative(SRC, f).split('\\').join('/')),
  )

  it('finds source files to scan', () => {
    expect(files.length).toBeGreaterThan(300)
  })

  it('no module-scope i18nT() call', () => {
    const offenders: string[] = []
    for (const file of files) {
      const rel = relative(SRC, file).split('\\').join('/')
      const source = readFileSync(file, 'utf-8')
      if (!source.includes('i18nT(')) continue
      const lines = source.split('\n')
      for (const lineNo of moduleLevelCalls(rel, source)) {
        offenders.push(`${rel}:${lineNo}  ${(lines[lineNo - 1] ?? '').trim()}`)
      }
    }
    expect(
      offenders,
      'A module-scope `i18nT()` runs once at import, before the user has chosen a '
        + 'language, and never re-runs — so the string freezes at the boot language. Move '
        + 'the call inside the component or the render callback, or store the KEY in your '
        + 'table and translate at render (see `labelKey` + `surfaceLabel()` in '
        + '`surfaces/registry.ts`).',
    ).toEqual([])
  })
})
