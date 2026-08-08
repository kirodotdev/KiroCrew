import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import ts from 'typescript'

/**
 * Guard against user-visible English sitting in a module-level metadata table.
 *
 * The band chips and the run status pill both held their labels as bare string
 * literals in a module-scope `Record<...>`, which is the one shape the i18n
 * codemod deliberately skips (it cannot know whether a module-level string is
 * user-visible). Nothing else caught them either: `moduleLevel.test.ts` guards
 * the INVERSE defect — a module-scope `i18nT()` CALL, which would freeze the
 * first locale — and a literal that was never wrapped at all trips no gate. The
 * result was a chip row and a status pill rendering English under all nine
 * translated locales while everything around them translated.
 *
 * The fix pattern, and what this test locks in: store a `labelKey` in the table
 * and resolve it with `i18nT()` at render time.
 *
 * Scope is this app's own tree. A repo-wide version of this rule belongs with the
 * i18n campaign that owns the other ~250 components, not here.
 */
const APP = join(__dirname, '..', 'apps', 'code-review-sage')

/** Property names whose value reaches the screen. */
const USER_VISIBLE = new Set([
  'label', 'title', 'placeholder', 'heading', 'text', 'description', 'tooltip',
  'summary', 'caption', 'hint', 'ariaLabel',
])

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) out.push(full)
  }
  return out
}

/** Module-scope `<userVisibleProp>: '<literal>'` assignments, as `line  text`. */
function moduleLevelLabelLiterals(file: string, source: string): string[] {
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const hits: string[] = []

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
      && ts.isPropertyAssignment(node)
      && (ts.isIdentifier(node.name) || ts.isStringLiteral(node.name))
      && USER_VISIBLE.has(node.name.text)
      && ts.isStringLiteralLike(node.initializer)
      // A key path is not user-visible text; it is what the fix looks like.
      && !node.initializer.text.includes('.')
      && /[A-Za-z]/.test(node.initializer.text)
    ) {
      const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1
      hits.push(`${file}:${line}  ${node.name.text}: '${node.initializer.text}'`)
    }
    const nowInside = insideFunction || isFunctionLike(node)
    ts.forEachChild(node, (child) => visit(child, nowInside))
  }

  visit(sf, false)
  return hits
}

describe('code-review-sage has no unwrapped module-level labels', () => {
  const files = walk(APP)

  it('finds source files to scan', () => {
    expect(files.length).toBeGreaterThan(10)
  })

  it('no user-visible string literal in a module-scope table', () => {
    const offenders: string[] = []
    for (const file of files) {
      const rel = relative(APP, file).split('\\').join('/')
      offenders.push(...moduleLevelLabelLiterals(rel, readFileSync(file, 'utf-8')))
    }
    expect(
      offenders,
      'A module-level label literal never reaches the codemod and never reaches a '
      + 'catalog, so it renders English under every locale. Hold the key instead '
      + "(`labelKey: 'apps.codeReviewSage…'`) and call i18nT() at render.",
    ).toEqual([])
  })
})
