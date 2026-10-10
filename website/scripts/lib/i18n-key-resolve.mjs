/**
 * Static catalog-key resolution over the TypeScript AST, shared by the
 * key-reference gate (`check-i18n-keys.mjs`) and the find_ui index generator
 * (`gen-ui-index.mjs`). Both must agree on which expressions name a finite,
 * checkable set of catalog keys; one implementation keeps them from diverging.
 *
 * Pure: no filesystem walk, no process exit. `collectFileScopeConsts` reads the
 * one defining module of a named relative import, memoized per path.
 */

import * as fs from 'node:fs'
import * as path from 'node:path'

import ts from 'typescript'

/**
 * Is a bare `t` in this file actually the translate function?
 *
 * `t` is a very common local identifier in this codebase — `t.ts` records ~30
 * `TS2349 This expression is not callable` errors from the first codemod run, because
 * `.map(t => …)` over tabs/turns/themes shadowed a bare `t` import. So matching `t(…)`
 * on name alone would eventually report a domain object's method call as a dangling key.
 * Requiring the binding removes that false-positive class outright instead of guessing
 * from the argument's shape, which would have created a false-NEGATIVE class instead.
 *
 * `useTranslation()` is the legitimate source and `index.ts` calls it preferred for new
 * code inside a component body, so this must keep working — it just has to be bound.
 */
export function bindsTranslateT(sourceFile) {
  let bound = false
  const visit = (node) => {
    if (bound) return
    // `const { t } = useTranslation(…)`
    if (ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name) && node.initializer) {
      const init = unwrap(node.initializer)
      const isUseTranslation = init && ts.isCallExpression(init)
        && /(^|\.)useTranslation$/.test(init.expression.getText())
      if (isUseTranslation) {
        for (const element of node.name.elements) {
          const source = element.propertyName ?? element.name
          if (ts.isIdentifier(source) && source.text === 't') bound = true
        }
      }
    }
    // `import { t } from 'i18next'`
    if (ts.isImportSpecifier(node) && node.name.text === 't') bound = true
    ts.forEachChild(node, visit)
  }
  ts.forEachChild(sourceFile, visit)
  return bound
}

/**
 * `i18nT(…)`, `i18next.t(…)`, `i18n.t(…)` always; bare `t(…)` only where `t` is bound
 * to a translate function.
 *
 * Matched by SHAPE rather than by resolving the import, so aliasing the module or
 * re-exporting `i18nT` is not a bypass — the same reason `dynamicKeys.test.ts` matches
 * on the call and not on the import.
 */
export function isTranslateCall(node, tIsBound) {
  if (!ts.isCallExpression(node)) return false
  const callee = node.expression
  if (ts.isIdentifier(callee)) {
    if (callee.text === 'i18nT') return true
    return callee.text === 't' && tIsBound
  }
  if (ts.isPropertyAccessExpression(callee) && callee.name.text === 't') {
    return /^(i18next|i18n)$/.test(callee.expression.getText())
  }
  return false
}

/** Strip the wrappers that do not change a value: `(x)`, `x as const`, `x!`, `x satisfies T`. */
export function unwrap(node) {
  let n = node
  while (
    n && (ts.isParenthesizedExpression(n) || ts.isAsExpression(n)
      || ts.isNonNullExpression(n) || ts.isSatisfiesExpression?.(n))
  ) n = n.expression
  return n
}

/**
 * Collect every file-scope `const NAME = <initializer>` so an identifier argument can
 * be followed to its value.
 *
 * File scope only, and deliberately: a same-named local in a nested scope would make
 * the lookup wrong, so a shadowed name must fall through to "dynamic" rather than
 * resolve to the outer binding and check the wrong key. Being unresolvable is a
 * counted, visible outcome here; being resolved incorrectly is not.
 */
export function collectFileScopeConsts(sourceFile, absPath) {
  const consts = new Map()
  const shadowedNames = new Set()

  const collectNested = (node, depth) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
      if (depth > 0) shadowedNames.add(node.name.text)
      else if (node.initializer) consts.set(node.name.text, node.initializer)
    }
    if (ts.isParameter(node) && ts.isIdentifier(node.name)) shadowedNames.add(node.name.text)
    const nextDepth = ts.isFunctionLike(node) || ts.isBlock(node) ? depth + 1 : depth
    ts.forEachChild(node, (child) => collectNested(child, nextDepth))
  }
  ts.forEachChild(sourceFile, (child) => collectNested(child, 0))

  for (const name of shadowedNames) consts.delete(name)

  // Named imports of an EXPORTED const, resolved from the defining module.
  //
  // Without this, a key map shared by several components is unresolvable at every
  // consumer even though it is exactly the `as const` map shape this gate asks
  // for — `PRIORITY_LABEL_KEY` in apps/meetings/api.ts is read by three views.
  // The only way to satisfy the gate would have been to copy the map into each
  // consumer, i.e. duplicate the data to please the checker, which is worse code
  // AND worse i18n (three places to update a key).
  //
  // Narrow on purpose, so it cannot resolve the WRONG value:
  //  - only a bare named import (`import { X } from './m'`) — no default, no
  //    namespace, no aliasing to a different local name;
  //  - only a relative specifier, resolved on disk, so a bare-module import
  //    cannot be confused for a local file;
  //  - only a name the importing file does not already bind (a local wins, and a
  //    shadowed name stays deleted above);
  //  - one hop, no transitive re-export chase: a file that re-exports someone
  //    else's map stays unresolvable and therefore counted.
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement)) continue
    const clause = statement.importClause
    if (!clause?.namedBindings || !ts.isNamedImports(clause.namedBindings)) continue
    const spec = statement.moduleSpecifier
    if (!ts.isStringLiteral(spec) || !spec.text.startsWith('.')) continue

    const from = resolveRelativeModule(absPath, spec.text)
    if (!from) continue
    const exported = exportedConstsOf(from)
    if (!exported) continue

    for (const element of clause.namedBindings.elements) {
      // `import { A as B }` — skip: the local name is B, and honouring it would
      // mean tracking a rename for no benefit these maps need.
      if (element.propertyName) continue
      const name = element.name.text
      if (consts.has(name) || shadowedNames.has(name)) continue
      const init = exported.get(name)
      if (init) consts.set(name, init)
    }
  }

  return consts
}

/** Absolute path of a relative import, trying the extensions this repo uses. */
function resolveRelativeModule(fromFile, specifier) {
  const base = path.resolve(path.dirname(fromFile), specifier)
  for (const candidate of [
    `${base}.ts`, `${base}.tsx`,
    path.join(base, 'index.ts'), path.join(base, 'index.tsx'),
  ]) {
    if (fs.existsSync(candidate)) return candidate
  }
  return null
}

/** `export const NAME = <init>` of one module, memoized. Never recurses. */
const exportedConstsCache = new Map()
function exportedConstsOf(file) {
  if (exportedConstsCache.has(file)) return exportedConstsCache.get(file)
  let out = null
  try {
    const text = fs.readFileSync(file, 'utf-8')
    const sf = ts.createSourceFile(
      file, text, ts.ScriptTarget.Latest, /* setParentNodes */ true,
      /\.tsx$/.test(file) ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    )
    out = new Map()
    for (const statement of sf.statements) {
      if (!ts.isVariableStatement(statement)) continue
      const isExported = statement.modifiers?.some(
        (m) => m.kind === ts.SyntaxKind.ExportKeyword,
      )
      if (!isExported) continue
      for (const decl of statement.declarationList.declarations) {
        if (ts.isIdentifier(decl.name) && decl.initializer) {
          out.set(decl.name.text, decl.initializer)
        }
      }
    }
  } catch {
    out = null
  }
  exportedConstsCache.set(file, out)
  return out
}

/**
 * The finite set of strings an expression can evaluate to, or `null` for "unknowable".
 *
 * `seen` breaks reference cycles (`const A = B, B = A` is legal TypeScript that would
 * otherwise recurse forever). Any branch that cannot be resolved poisons the whole
 * result: a union with one unknown member cannot be checked, and reporting the members
 * that DID resolve would let an unchecked branch pass as covered.
 */
export function resolveStrings(node, consts, seen = new Set()) {
  const n = unwrap(node)
  if (!n) return null

  if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) return [n.text]

  if (ts.isIdentifier(n)) {
    if (seen.has(n.text)) return null
    const init = consts.get(n.text)
    if (!init) return null
    return resolveStrings(init, consts, new Set([...seen, n.text]))
  }

  if (ts.isConditionalExpression(n)) {
    const a = resolveStrings(n.whenTrue, consts, seen)
    const b = resolveStrings(n.whenFalse, consts, seen)
    return a && b ? [...a, ...b] : null
  }

  if (ts.isBinaryExpression(n)) {
    const op = n.operatorToken.kind
    if (op === ts.SyntaxKind.QuestionQuestionToken || op === ts.SyntaxKind.BarBarToken) {
      const a = resolveStrings(n.left, consts, seen)
      const b = resolveStrings(n.right, consts, seen)
      return a && b ? [...a, ...b] : null
    }
    // `+` is key ASSEMBLY, which `dynamicKeys.test.ts` bans outright. Never resolve it
    // here: a gate that quietly accepted a concatenation would undercut that rule.
    return null
  }

  if (ts.isPropertyAccessExpression(n)) {
    const obj = resolveObjectLiteral(n.expression, consts, seen)
    if (!obj) return null
    const value = obj.get(n.name.text)
    return value ? resolveStrings(value, consts, seen) : null
  }

  if (ts.isElementAccessExpression(n)) {
    const obj = resolveObjectLiteral(n.expression, consts, seen)
    if (!obj) return null
    const index = unwrap(n.argumentExpression)
    // A literal index selects one entry; anything else selects SOME entry, so the whole
    // map is the possibility set — which is the point of the `as const` map pattern.
    if (index && (ts.isStringLiteral(index) || ts.isNoSubstitutionTemplateLiteral(index))) {
      const value = obj.get(index.text)
      return value ? resolveStrings(value, consts, seen) : null
    }
    const all = []
    for (const value of obj.values()) {
      const resolved = resolveStrings(value, consts, seen)
      if (!resolved) return null
      all.push(...resolved)
    }
    return all.length > 0 ? all : null
  }

  return null
}

/** Follow an expression to an object literal and index its string-keyed properties. */
export function resolveObjectLiteral(node, consts, seen) {
  let n = unwrap(node)
  if (ts.isIdentifier(n)) {
    if (seen.has(n.text)) return null
    const init = consts.get(n.text)
    if (!init) return null
    return resolveObjectLiteral(init, consts, new Set([...seen, n.text]))
  }
  if (!n || !ts.isObjectLiteralExpression(n)) return null

  const out = new Map()
  for (const prop of n.properties) {
    // A spread makes the property set unknowable; refuse the whole object rather than
    // silently checking a subset of the keys it can produce.
    if (!ts.isPropertyAssignment(prop)) return null
    const name = prop.name
    if (ts.isIdentifier(name) || ts.isStringLiteral(name)) out.set(name.text, prop.initializer)
    else if (ts.isComputedPropertyName(name)) {
      const computed = unwrap(name.expression)
      if (computed && ts.isStringLiteral(computed)) out.set(computed.text, prop.initializer)
      else return null
    } else return null
  }
  return out
}
