/**
 * Shared classifier for same-dashboard gateway URLs — the SINGLE source of truth
 * behind BOTH the repository guard (`check-dashboard-runtime.mjs`, which FAILS a
 * bypass) and the migration codemod (`migrate-fetch-to-runtime.mjs`, which WRAPS
 * one). Both draw their same-dashboard classification from here rather than from
 * two drifting regexes, and both resolve constants through the SAME module-scope
 * and imported-const machinery (`rootConstNames` / `augmentedConstNames`).
 *
 * They deliberately consume DIFFERENT detectors over that shared foundation, and
 * the guard's is a strict SUPERSET of the codemod's:
 *
 *  - the codemod wraps `sameDashboardArg` — the statically-simple shapes it can
 *    rewrite by wrapping the first-arg span verbatim: a root-absolute
 *    string/template literal, a `` `${API}/…` `` template or bare identifier off
 *    a root-const, or an `API + path` concat off one;
 *  - the guard fails `argEscapesToRoot` — that set PLUS the scope-aware dataflow
 *    shapes (`const url = BASE + path`, a root-defaulted helper parameter, a
 *    `provider || '/api/…'` fallback) that resolve to a root path but expose no
 *    single first-arg span a codemod could safely wrap.
 *
 * The asymmetry is intentional: the guard catches every provable bypass, and the
 * extra shapes it flags are relocated BY HAND (a codemod cannot rewrite a
 * threaded local without risking behavior change), so the guard is never
 * weakened down to the codemod's rewritable subset. Both detectors share the
 * same NON-matches: protocol-relative (`//host`), scheme (`https://…`), and a
 * bare, unresolvable parameter (`fetch(url)`) stay explicit — a genuinely
 * external route stays as written, and a same-dashboard helper is relocated at
 * its own `fetch(param)` boundary instead.
 */
import ts from 'typescript'
import { readFileSync, existsSync } from 'node:fs'
import path from 'node:path'

/**
 * Module-scope root-absolute string constants in a file — the names a
 * `fetch`/`EventSource` arg may resolve THROUGH.
 *
 * Not just plain `const NAME = '/…'`: a name is root-absolute when its
 * initializer PROVABLY resolves to a root-absolute, same-dashboard path by the
 * SAME rule `sameDashboardArg` applies to a call arg — a root literal, a
 * `` `${OTHER}/…` `` template or bare identifier off another root-const, or an
 * `OTHER + path` concat. That is computed to a fixpoint so a chain
 * (`const API = '/api/x'; const R = `${API}/reminders`;`) fully resolves: `R` is
 * root-absolute because `API` is. This is what closes the blind spot where a
 * template-derived path constant (the shape `crew-companion/constants.ts` uses
 * for every route) looked like a non-root const and let `fetch(R)` slip past.
 */
export function rootConstNames(sf) {
  const decls = []
  for (const st of sf.statements) {
    if (!ts.isVariableStatement(st)) continue
    for (const d of st.declarationList.declarations) {
      if (ts.isIdentifier(d.name) && d.initializer) decls.push([d.name.text, d.initializer])
    }
  }
  const names = new Set()
  let changed = true
  while (changed) {
    changed = false
    for (const [name, init] of decls) {
      if (names.has(name)) continue
      // `sameDashboardArg` reads the CURRENT set, so a const referencing an
      // already-classified root-const resolves on the next pass.
      if (sameDashboardArg(init, sf, names)) {
        names.add(name)
        changed = true
      }
    }
  }
  return names
}

/**
 * Named imports from RELATIVE modules: `[{ importPath, names }]`. Only relative
 * specifiers (`./`, `../`) are returned — a package import cannot name a
 * repo-local same-dashboard path constant — so the cross-file resolver below
 * looks no further than the sibling/relative module that declares the constant.
 */
export function relativeNamedImports(sf) {
  const out = []
  for (const st of sf.statements) {
    if (!ts.isImportDeclaration(st)) continue
    const spec = st.moduleSpecifier
    if (!ts.isStringLiteral(spec)) continue
    const p = spec.text
    if (!p.startsWith('./') && !p.startsWith('../')) continue
    const clause = st.importClause
    const named = clause && clause.namedBindings
    if (!named || !ts.isNamedImports(named)) continue
    const names = named.elements.map((el) => el.name.text)
    if (names.length) out.push({ importPath: p, names })
  }
  return out
}

/**
 * The set of names a file's `fetch`/`EventSource` args may resolve through:
 * its OWN root-consts PLUS any root-const it imports by name from a relative
 * module. `loadModule(fromRel, importPath)` returns `{ rel, sf }` for the
 * imported source (or `null`); when omitted (unit tests calling `scanSource`
 * with text only), only same-file consts are used, so the pure detector stays
 * pure. Resolution is ONE level deep — to the module that DECLARES the constant,
 * which is where same-dashboard path constants live by convention (a dedicated
 * `constants.ts`) — not a recursive re-export walk.
 */
export function augmentedConstNames(sf, rel, loadModule) {
  const consts = rootConstNames(sf)
  if (typeof loadModule !== 'function') return consts
  for (const { importPath, names } of relativeNamedImports(sf)) {
    const mod = loadModule(rel, importPath)
    if (!mod || !mod.sf) continue
    const modConsts = rootConstNames(mod.sf)
    for (const n of names) if (modConsts.has(n)) consts.add(n)
  }
  return consts
}

/**
 * A filesystem-backed `loadModule` for the guard and codemod, rooted at
 * `websiteDir` and memoized. Resolves a relative specifier to a `.ts`/`.tsx`
 * (or `/index.*`) source under the tree; a specifier that resolves outside the
 * tree or to no file is `null` (the consumer then falls back to same-file
 * consts). Kept here beside the classifier so the guard (which FAILS a bypass)
 * and the codemod (which WRAPS one) resolve imported constants IDENTICALLY.
 */
export function makeModuleLoader(websiteDir) {
  const cache = new Map()
  return (fromRel, importPath) => {
    const fromDir = path.posix.dirname(fromRel.split(path.sep).join('/'))
    const base = path.posix.normalize(path.posix.join(fromDir, importPath))
    // A specifier that climbs above the tree root is not a same-dashboard module.
    if (base.startsWith('..')) return null
    const hasExt = base.endsWith('.ts') || base.endsWith('.tsx')
    const candidates = hasExt
      ? [base]
      : [`${base}.ts`, `${base}.tsx`, `${base}/index.ts`, `${base}/index.tsx`]
    for (const relCandidate of candidates) {
      if (cache.has(relCandidate)) {
        const cached = cache.get(relCandidate)
        if (cached) return cached
        continue
      }
      const abs = path.join(websiteDir, relCandidate)
      if (existsSync(abs)) {
        const entry = { rel: relCandidate, sf: parseSource(relCandidate, readFileSync(abs, 'utf8')) }
        cache.set(relCandidate, entry)
        return entry
      }
      cache.set(relCandidate, null)
    }
    return null
  }
}

/** True if `arg` is already routed through the runtime seam (needs no wrap). */
export function isRelocated(arg) {
  if (!ts.isCallExpression(arg)) return false
  const e = arg.expression
  return (
    ts.isIdentifier(e) &&
    (e.text === 'relocateRequestUrl' || e.text === 'relocateLoose' || e.text === 'webSocketUrl')
  )
}

/**
 * True when a `fetch`/`EventSource` first-arg PROVABLY resolves to a
 * root-absolute, same-dashboard path (see module doc). `consts` is the file's
 * `rootConstNames(sf)`.
 */
export function sameDashboardArg(arg, sf, consts) {
  if (!arg) return false
  const text = arg.getText(sf)
  // A root-absolute string/template literal head: 'x', "x", `x`, `x${…}`.
  if (/^(['"`])\/(?!\/)/.test(text)) return true
  if (ts.isTemplateExpression(arg)) {
    if (/^\/(?!\/)/.test(arg.head.text)) return true
    const first = arg.templateSpans[0]
    if (
      arg.head.text === '' &&
      first &&
      ts.isIdentifier(first.expression) &&
      consts.has(first.expression.text)
    ) {
      return true
    }
    return false
  }
  if (ts.isIdentifier(arg) && consts.has(arg.text)) return true
  if (ts.isBinaryExpression(arg) && arg.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    let left = arg.left
    while (ts.isBinaryExpression(left) && left.operatorToken.kind === ts.SyntaxKind.PlusToken) {
      left = left.left
    }
    if (ts.isIdentifier(left) && consts.has(left.text)) return true
    if (ts.isStringLiteralLike(left) && /^\/(?!\/)/.test(left.text)) return true
  }
  return false
}

/** A function-like node that introduces a parameter scope. */
function isFunctionLike(n) {
  return (
    ts.isFunctionDeclaration(n) ||
    ts.isFunctionExpression(n) ||
    ts.isArrowFunction(n) ||
    ts.isMethodDeclaration(n) ||
    ts.isConstructorDeclaration(n) ||
    ts.isGetAccessorDeclaration(n) ||
    ts.isSetAccessorDeclaration(n)
  )
}

/**
 * The initializer (or parameter default) that binds `name` as seen from
 * `fromNode`, resolved nearest-scope-first, or `undefined` when no binding is
 * in scope. A binding with no initializer/default (a bare parameter such as a
 * gateway helper's `path`, or an uninitialized `let`) returns `null` — the
 * caller then treats it as NOT provably root, so an opaque helper parameter is
 * never pinned safe on the strength of a matching name elsewhere.
 *
 * Scope-aware by walking `node.parent` (parents are set by `parseSource`): a
 * `const url` local to one function does not leak into a sibling, so a genuinely
 * external `fetch(url)` in another scope is not mis-flagged.
 */
function findBindingInit(name, fromNode) {
  let node = fromNode.parent
  while (node) {
    if (ts.isBlock(node) || ts.isSourceFile(node) || ts.isModuleBlock(node)) {
      for (const st of node.statements) {
        if (!ts.isVariableStatement(st)) continue
        for (const d of st.declarationList.declarations) {
          if (ts.isIdentifier(d.name) && d.name.text === name) {
            return d.initializer ?? null
          }
        }
      }
    }
    if (isFunctionLike(node) && node.parameters) {
      for (const p of node.parameters) {
        if (ts.isIdentifier(p.name) && p.name.text === name) {
          // A parameter default that resolves to a root path (e.g.
          // `base: string = BASE`) binds root; a bare parameter binds nothing
          // provable and returns null.
          return p.initializer ?? null
        }
      }
    }
    node = node.parent
  }
  return undefined
}

/**
 * True when `arg` PROVABLY resolves to a root-absolute, same-dashboard path —
 * the strengthened detector the guard scan uses. It extends `sameDashboardArg`
 * with SCOPE-AWARE dataflow so a function-local derived root path and a
 * root-defaulted gateway helper parameter can no longer be pinned safe:
 *
 *  - `const url = BASE + path; fetch(url)` (a local const off a root base),
 *  - `function f(base = BASE) { const url = base + p; fetch(url) }` (a param
 *    whose default is a root const, threaded through a local),
 *  - `const endpoint = provider || '/api/deploy/deploy'; fetch(endpoint)` (a
 *    `||`/`??` fallback to a root literal).
 *
 * It never flags a bare, unresolvable parameter (`fetch(param)` with no
 * root-resolvable binding) — that stays the helper's own relocate-at-boundary
 * responsibility, so a genuinely external target is not mis-flagged. `consts`
 * is the file's `augmentedConstNames`. `seen` guards identifier cycles.
 */
export function argEscapesToRoot(arg, sf, consts, seen) {
  if (!arg) return false
  seen = seen || new Set()
  if (ts.isParenthesizedExpression(arg)) return argEscapesToRoot(arg.expression, sf, consts, seen)
  if (ts.isAsExpression(arg) || ts.isNonNullExpression(arg)) {
    return argEscapesToRoot(arg.expression, sf, consts, seen)
  }
  const text = arg.getText(sf)
  // A root-absolute string/template literal head: 'x', "x", `x`, `x${…}`.
  if (/^(['"`])\/(?!\/)/.test(text)) return true
  if (ts.isTemplateExpression(arg)) {
    if (/^\/(?!\/)/.test(arg.head.text)) return true
    const first = arg.templateSpans[0]
    if (arg.head.text === '' && first) return argEscapesToRoot(first.expression, sf, consts, seen)
    return false
  }
  if (ts.isIdentifier(arg)) {
    if (consts.has(arg.text)) return true
    if (seen.has(arg.text)) return false
    seen.add(arg.text)
    const init = findBindingInit(arg.text, arg)
    if (init == null) return false // undefined (no binding) or null (no initializer)
    return argEscapesToRoot(init, sf, consts, seen)
  }
  if (ts.isBinaryExpression(arg)) {
    const op = arg.operatorToken.kind
    if (op === ts.SyntaxKind.PlusToken) {
      // String concat: the LEFTMOST operand fixes the scheme/root.
      let left = arg.left
      while (ts.isBinaryExpression(left) && left.operatorToken.kind === ts.SyntaxKind.PlusToken) {
        left = left.left
      }
      return argEscapesToRoot(left, sf, consts, seen)
    }
    if (op === ts.SyntaxKind.BarBarToken || op === ts.SyntaxKind.QuestionQuestionToken) {
      // Either branch can be the value; a root fallback (or root primary) escapes.
      return (
        argEscapesToRoot(arg.left, sf, consts, seen) ||
        argEscapesToRoot(arg.right, sf, consts, seen)
      )
    }
  }
  if (ts.isConditionalExpression(arg)) {
    return (
      argEscapesToRoot(arg.whenTrue, sf, consts, seen) ||
      argEscapesToRoot(arg.whenFalse, sf, consts, seen)
    )
  }
  return false
}

/** True if `expr` is a `fetch` / `<obj>.fetch` callee. */
export function isFetchCallee(expr) {
  if (ts.isIdentifier(expr)) return expr.text === 'fetch'
  if (ts.isPropertyAccessExpression(expr)) return expr.name.text === 'fetch'
  return false
}

/** Parse source text into a SourceFile with the right JSX kind for `rel`. */
export function parseSource(rel, text) {
  const kind = rel.endsWith('.tsx') || rel.endsWith('.jsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  return ts.createSourceFile(rel, text, ts.ScriptTarget.Latest, true, kind)
}

export { ts }
