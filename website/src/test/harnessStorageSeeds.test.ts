import { describe, it, expect } from 'vitest'
import { readdirSync } from 'node:fs'
import { join, relative } from 'node:path'
import ts from 'typescript'
import { readSource } from './readSource'

/**
 * `stubDashboardApi` opens its own init script with `localStorage.clear()`, and
 * Playwright does not define the order of separately registered init scripts.
 * A harness that seeds a stubbed page from its own `addInitScript` therefore
 * races that clear. A seed made only of `localStorage.setItem` calls is exactly
 * what the stub's `localStorageEntries` expresses, and the stub applies those
 * after its clear inside the same script, so that is where such a seed belongs.
 *
 * The page is matched by its declaration (the type checker's symbol), not its
 * name: a file may also drive a page that never went through the stub.
 */

const SCRIPTS = join(__dirname, '..', '..', 'scripts')
/** `stubSplitPanes` (lib/split-pane-fixture.mjs) forwards its page to the stub. */
const STUBS = ['stubDashboardApi', 'stubSplitPanes']

function harnessFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return harnessFiles(path)
    if (!entry.name.endsWith('.mjs')) return []
    const text = readSource(path)
    return STUBS.some(stub => text.includes(`${stub}(`)) && text.includes('.addInitScript(') ? [path] : []
  })
}

function isStorageCall(node: ts.Node, method: string): boolean {
  return ts.isCallExpression(node)
    && ts.isPropertyAccessExpression(node.expression)
    && node.expression.name.text === method
    && ts.isIdentifier(node.expression.expression)
    && node.expression.expression.text === 'localStorage'
}

const isSetItem = (node: ts.Node): boolean => isStorageCall(node, 'setItem')

function onlySetsItems(fn: ts.Node | undefined): boolean {
  if (!fn || !(ts.isArrowFunction(fn) || ts.isFunctionExpression(fn))) return false
  if (!ts.isBlock(fn.body)) return isSetItem(fn.body)
  return fn.body.statements.length > 0
    && fn.body.statements.every(s => ts.isExpressionStatement(s) && isSetItem(s.expression))
}

/** A `removeItem` or `clear` anywhere in the body: the stub's own clear already empties storage. */
function removesItems(fn: ts.Node | undefined): boolean {
  if (!fn || !(ts.isArrowFunction(fn) || ts.isFunctionExpression(fn))) return false
  const visit = (node: ts.Node): boolean =>
    isStorageCall(node, 'removeItem') || isStorageCall(node, 'clear') || ts.forEachChild(node, visit) === true
  return visit(fn.body)
}

function racingSeeds(files: string[], shape: (fn: ts.Node | undefined) => boolean): string[] {
  const program = ts.createProgram(files, { allowJs: true, noLib: true, noResolve: true, types: [] })
  const checker = program.getTypeChecker()
  const found: string[] = []
  for (const file of files) {
    const source = program.getSourceFile(file)!
    const stubbed = new Set<ts.Symbol>()
    const seeds: ts.Identifier[] = []
    const visit = (node: ts.Node): void => {
      if (ts.isCallExpression(node)) {
        const [first] = node.arguments
        if (ts.isIdentifier(node.expression) && STUBS.includes(node.expression.text)
          && first && ts.isIdentifier(first)) {
          const symbol = checker.getSymbolAtLocation(first)
          if (symbol) stubbed.add(symbol)
        }
        if (ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'addInitScript'
          && ts.isIdentifier(node.expression.expression) && shape(first)) {
          seeds.push(node.expression.expression)
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(source)
    for (const page of seeds) {
      const symbol = checker.getSymbolAtLocation(page)
      if (!symbol || !stubbed.has(symbol)) continue
      const { line } = source.getLineAndCharacterOfPosition(page.getStart(source))
      found.push(`${relative(SCRIPTS, file).replaceAll('\\', '/')}:${line + 1}`)
    }
  }
  return found
}

describe('harness localStorage seeds', () => {
  it('seeds a stubbed page through localStorageEntries, not its own init script', () => {
    expect(racingSeeds(harnessFiles(SCRIPTS), onlySetsItems)).toEqual([])
  })

  // A scenario that needs another seed opens a fresh page and stubs it once.
  it('leaves removing keys to the stub\'s clear', () => {
    expect(racingSeeds(harnessFiles(SCRIPTS), removesItems)).toEqual([])
  })
})
