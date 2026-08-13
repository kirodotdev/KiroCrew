#!/usr/bin/env node
/**
 * Catalog-key reference gate — does every key the source asks for actually EXIST?
 *
 * ## The defect this gates (observed, not hypothetical)
 *
 * i18next returns a missing key as its own fallback rather than throwing, so a
 * reference to a key that is not in the catalog renders the dotted path itself into
 * the UI — `pages.chat.activityViewer.show_all` where a button label should be. Every
 * other i18n gate in this repo asks a different question and is blind to it:
 *
 *   catalogParity      compares the 9 target catalogs AGAINST en. A key absent from
 *                      en is absent from all of them, so parity is perfect.
 *   deadKeys           the other direction — catalog keys nothing references.
 *   dynamicKeys        forbids ASSEMBLING a key; says nothing about whether a
 *                      perfectly literal key exists.
 *   check-i18n-strings the string is translated; it never asks if the key resolves.
 *   gen-pseudolocale   derived from en, so it inherits en's gaps silently.
 *
 * Phase 3 item 1a proved the hole live: it collapsed a shared count-badge fragment
 * into `show_all_count` and updated one of the key's TWO render sites. The catalogs
 * were internally consistent, `catalogParity`, `deadKeys` and `qa.test.ts` all passed,
 * and the only thing that noticed was an unrelated component test that happened to
 * assert the rendered text. Phase 3 renames and deletes catalog keys at scale, so that
 * near-miss is the normal case, not the exception.
 *
 * ## Why the TypeScript compiler API and not a regex
 *
 * The same reason `i18n-plural-codemod.mjs` was rewritten as an AST check: a regex
 * over `i18nT\('([^']+)'\)` hard-codes the quote style, the function name, the absence
 * of a line break inside the call, and — fatally here — it cannot tell a key in CALL
 * position from the same dotted string in a doc comment, a test fixture or an
 * `expect()` assertion. Resolution has to follow `const` bindings and `as const` maps
 * to be worth anything (see the four resolvable shapes below), and that is a scope
 * question, not a text question.
 *
 * ## What "resolvable" means — four shapes beyond the plain literal
 *
 * A call site is STATIC if a finite set of possible key strings can be computed from
 * syntax alone. That is deliberately more than `i18nT('a.b.c')`, because the repo's
 * own recommended fix for a dynamic key (`dynamicKeys.test.ts`) is an `as const` map,
 * and a gate that could not see through the recommended fix would punish it:
 *
 *   literal          i18nT('pages.x.y')
 *   const binding    const K = 'pages.x.y'          → i18nT(K)
 *   map access       UPDATE_ERROR_KEYS.offline      → i18nT(UPDATE_ERROR_KEYS.offline)
 *                    STATUS_LABEL_KEY[st]           → union of ALL the map's values
 *   finite choice    cond ? 'pages.x.a' : 'pages.x.b'   (and `??` / `||`)
 *
 * Every member of the resolved set must exist. `STATUS_LABEL_KEY[st]` therefore
 * validates three keys from one call site, which is exactly the coverage a manifest
 * would have had to declare by hand.
 *
 * ## Why not i18next's typed keys, which would make tsc do this
 *
 * The industry-standard alternative is `CustomTypeOptions` with a generated key union,
 * turning `i18nT` into `i18nT(key: <union of every key>)` so a dangling reference is a
 * compile error with the type checker's own scope resolution instead of this file's
 * approximation of one. It is the better end state and is already on the plan as Phase 6
 * item 6. It is not the right thing to land here, for three reasons:
 *
 *  1. **It cannot report a coverage number.** A computed key widens to `string` and the
 *     union check silently stops applying. That is precisely the population this gate
 *     exists to *count* and ratchet — a typed union would make it invisible again.
 *  2. **It cannot land before the keys are stable.** Phase 3 renames and deletes keys at
 *     scale; a generated union regenerated mid-phase turns every in-flight branch into a
 *     merge conflict on one enormous type. The gate that protects those renames has to
 *     precede them, and this one needs no codegen artifact.
 *  3. **`strictKeyChecks` needs one catalog.** Keys live in `en.json` (codemod-generated)
 *     and `en.manual.json` (hand-written) and the union would have to be their merge —
 *     which is also why this script asserts the two never shadow each other. Settling
 *     that shape is Phase 6 work.
 *
 * So: this is a stopgap with a defined end, not permanent infrastructure. When Phase 6
 * item 6 lands the typed union, the resolver here should be deleted and only the
 * dynamic-site ratchet kept.
 *
 * ## Dynamic keys are counted, not waved through
 *
 * A key genuinely computed at runtime cannot be resolved by any tool — `i18next-cli`'s
 * own docs concede this and fall back to `preservePatterns` globs. The honest response
 * is not an allowlist file (which rots, and which `dynamicKeys.test.ts` already made
 * unnecessary by banning assembly outright) but a COUNT: `dynamic-keys-baseline.json`
 * records the unresolvable call sites per file, bidirectionally. That number IS the
 * gate's coverage statement — an unstated dynamic population is a silent blind spot —
 * and the downward pressure is what turns `i18nT(s.labelKey)` into a map access
 * eventually instead of leaving it there forever. This gets the stale-exemption
 * detection `ember-intl-analyzer` exposes as a dedicated option out of the ratchet
 * itself, rather than from a second file.
 *
 * ## Dispositions, and why they differ
 *
 *   dangling references  HARD ZERO. Not a ratchet: each one is a live bug rendering a
 *                        raw key to a user. "Acceptable debt" is not a coherent
 *                        position for broken output, and unlike an untranslated string
 *                        there is no burn-down to schedule — the fix is one edit.
 *   dynamic call sites   PER-FILE BIDIRECTIONAL RATCHET, the `check-i18n-strings.mjs`
 *                        idiom, for the reasons given there: per file so one file's
 *                        fix cannot pay for another's regression, bidirectional so a
 *                        gain cannot be given back.
 *   catalog shadowing    HARD ZERO. `index.ts` deep-merges `en.json` under
 *                        `en.manual.json`, so a key in both silently takes the manual
 *                        value while the codemod keeps regenerating the other one.
 *                        Nothing else checks this: `catalogParity` compares the MERGED
 *                        en against the targets, so it sees one key and is satisfied.
 *
 * ## What this deliberately does NOT do
 *
 * It does not gate the reverse direction. `deadKeys.test.ts` owns "catalog key nothing
 * references" with a baseline of 19, and its reference scan is a quoted-string search
 * over all of `src` — which is STRICTLY WIDER than this file's AST view: it sees
 * `labelKey: 'nav.sessions'` in a data table, a key named in a doc comment, and a key
 * used only by a test. Gating on the narrower AST view would report keys as dead that
 * `deadKeys` correctly calls live, i.e. two gates in the same repo contradicting each
 * other on the same question. `--report` prints the AST-only view as a DIAGNOSTIC so
 * the difference is visible, and never fails on it.
 *
 * Usage:
 *   node scripts/check-i18n-keys.mjs             # gate
 *   node scripts/check-i18n-keys.mjs --report     # gate + coverage / dead-key diagnostics
 *   node scripts/check-i18n-keys.mjs --update     # rewrite the dynamic-site baseline
 */

import * as fs from 'node:fs'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const SRC = path.join(ROOT, 'src')
const LOCALES = path.join(SRC, 'i18n/locales')
const BASELINE = path.join(SRC, 'i18n/dynamic-keys-baseline.json')

const UPDATE = process.argv.includes('--update')
const REPORT = process.argv.includes('--report')
for (const arg of process.argv.slice(2)) {
  if (!['--update', '--report'].includes(arg)) {
    console.error(`unknown flag ${arg} — refusing to run rather than silently gating nothing.`)
    process.exit(2)
  }
}

/**
 * Files whose translate calls are NOT render sites.
 *
 * `i18n/t.ts` declares `i18nT` and forwards its `key` parameter to `i18next.t` — that
 * is the signature, not a call site, and the parameter is unresolvable by definition.
 * `dynamicKeys.test.ts` excludes it for the same reason.
 *
 * Test files are excluded from the DANGLING check because several deliberately
 * reference keys that do not exist, to assert the missing-key fallback behaviour
 * (`pseudoBracket.test.tsx` builds fixture keys, `navLabels.test.tsx` composes
 * `settings.tabs.${k}.${field}`). Failing on those would make the gate punish the
 * tests that exist to prove the failure mode. They are still parsed, and still
 * reported under `--report`, so the exclusion is visible rather than assumed.
 */
const DECLARATION_FILE = 'i18n/t.ts'
const isTestFile = (rel) => /\.test\.tsx?$/.test(rel) || rel.startsWith('test/')

/**
 * Property names whose string-literal value IS a catalog key.
 *
 * `surfaces/registry.ts:324` is the repo's one genuinely dynamic call —
 * `i18nT(s.labelKey)` over surface data. The key is still written out as a literal,
 * just in a data table rather than in call position, which is precisely the
 * `gettext_noop` pattern: mark the string where it IS literal, translate at access.
 * Validating the literals recovers static coverage for that whole call site.
 *
 * Overlap with `navLabels.test.ts`, stated so neither looks redundant: that test
 * resolves `labelKey` at RUNTIME, for every language, over `getBuiltinSurfaces()`
 * only. This checks English only, but statically, for every `labelKey` literal
 * anywhere in `src` — including a surface registered outside `builtins.tsx`, which
 * the runtime test never enumerates. Neither subsumes the other.
 *
 * There is no false-positive class: `surfaceLabel()` passes ANY `labelKey` it is given
 * straight to `i18nT()`, so a `labelKey` that is not a catalog key is a bug by
 * construction, not a value this gate is misreading.
 */
const KEY_BEARING_PROPERTIES = new Set(['labelKey'])

// ---------------------------------------------------------------- catalogs

function flatten(obj, prefix = '', out = {}) {
  for (const [key, value] of Object.entries(obj)) {
    const dotted = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) flatten(value, dotted, out)
    else out[dotted] = value
  }
  return out
}

function readCatalog(name) {
  const file = path.join(LOCALES, name)
  if (!fs.existsSync(file)) {
    console.error(`missing ${path.relative(ROOT, file)} — cannot check key references without English.`)
    process.exit(2)
  }
  return flatten(JSON.parse(fs.readFileSync(file, 'utf-8')))
}

const generated = readCatalog('en.json')
const manual = readCatalog('en.manual.json')
// `Object.hasOwn`, not `k in manual`: catalog keys are author-chosen, and a leaf named
// `constructor` or `toString` would test true against Object.prototype and report a
// collision that does not exist.
const shadowed = Object.keys(generated).filter((k) => Object.hasOwn(manual, k)).sort()
const english = new Set([...Object.keys(generated), ...Object.keys(manual)])

const pluralRegistry = new Set(JSON.parse(fs.readFileSync(path.join(SRC, 'i18n/pluralKeys.json'), 'utf-8')))

/**
 * Does `key` resolve for a `t()` call?
 *
 * Two ways to be present:
 *  - the exact key (the ordinary case, including `{{count}}` with no plural siblings,
 *    which i18next 26 resolves cleanly);
 *  - membership in `pluralKeys.json`, because a plural key is CALLED by its base and
 *    `Intl.PluralRules` appends the category at runtime, so the base itself never exists
 *    in any catalog. That registry is the declaration `catalogParity` builds the
 *    suffixed forms from, which makes it the authority here too.
 *
 * A `key_one` / `key_other` SUFFIX SCAN is deliberately not used, and this is not a
 * style preference: 3 catalog keys end in those words as real English text
 * (`pages.channelPage.click_new_to_create_one`, `…panel_to_add_one`,
 * `…add_column_after_this_one`), so a suffix rule would invent plural bases for them and
 * silently resolve `…click_new_to_create` — a key nothing defines. The registry has no
 * such ambiguity: measured on this tree, all 45 entries have suffixed catalog keys and
 * every genuine plural base is registered, so the scan would only ever add false
 * negatives.
 */
function resolves(key) {
  return english.has(key) || pluralRegistry.has(key)
}

// ---------------------------------------------------------------- AST

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir)) {
    if (entry === 'node_modules' || entry === 'locales') continue
    const full = path.join(dir, entry)
    if (fs.statSync(full).isDirectory()) walk(full, out)
    else if (/\.tsx?$/.test(entry)) out.push(full)
  }
  return out
}

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
function bindsTranslateT(sourceFile) {
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
function isTranslateCall(node, tIsBound) {
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
function unwrap(node) {
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
function collectFileScopeConsts(sourceFile) {
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

    const from = resolveRelativeModule(sourceFile.fileName, spec.text)
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
  const base = path.resolve(path.dirname(path.resolve(SRC, fromFile)), specifier)
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
function resolveStrings(node, consts, seen = new Set()) {
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
function resolveObjectLiteral(node, consts, seen) {
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

const staticRefs = []   // { rel, line, key, source }
const dynamicSites = [] // { rel, line, text }

for (const file of walk(SRC)) {
  const rel = path.relative(SRC, file).split(path.sep).join('/')
  if (rel === DECLARATION_FILE) continue

  const text = fs.readFileSync(file, 'utf-8')
  const sourceFile = ts.createSourceFile(
    rel, text, ts.ScriptTarget.Latest, /* setParentNodes */ true,
    /\.tsx$/.test(rel) ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )
  const consts = collectFileScopeConsts(sourceFile)
  const tIsBound = bindsTranslateT(sourceFile)
  const lineOf = (node) => sourceFile.getLineAndCharacterOfPosition(node.getStart()).line + 1

  const visit = (node) => {
    if (isTranslateCall(node, tIsBound)) {
      const arg = node.arguments[0]
      const keys = arg ? resolveStrings(arg, consts) : null
      if (keys) for (const key of keys) staticRefs.push({ rel, line: lineOf(node), key, source: 'call' })
      else {
        dynamicSites.push({
          rel,
          line: lineOf(node),
          text: node.getText().slice(0, 120).replace(/\s+/g, ' '),
        })
      }
    }

    // `<Trans i18nKey="…">` — zero sites today, and that is exactly why it is here:
    // Phase 3 item 3 converts ~90 fragment blocks to `<Trans>`, and a gate that only
    // learns about a call shape after the shape arrives is a gate that was absent for
    // the commit that introduced it.
    if (ts.isJsxAttribute(node) && node.name.getText() === 'i18nKey' && node.initializer) {
      const init = ts.isJsxExpression(node.initializer) ? node.initializer.expression : node.initializer
      const keys = init ? resolveStrings(init, consts) : null
      if (keys) for (const key of keys) staticRefs.push({ rel, line: lineOf(node), key, source: 'Trans' })
      else dynamicSites.push({ rel, line: lineOf(node), text: node.getText().slice(0, 120).replace(/\s+/g, ' ') })
    }

    if (ts.isPropertyAssignment(node) && (ts.isIdentifier(node.name) || ts.isStringLiteral(node.name))
      && KEY_BEARING_PROPERTIES.has(node.name.text)) {
      const keys = resolveStrings(node.initializer, consts)
      // Unresolvable here is NOT counted as a dynamic call site: this is a data field,
      // and `labelKey` is optional by design (an app-contributed surface has none), so
      // a computed value is ordinary rather than a coverage hole.
      if (keys) for (const key of keys) staticRefs.push({ rel, line: lineOf(node), key, source: node.name.text })
    }

    ts.forEachChild(node, visit)
  }
  ts.forEachChild(sourceFile, visit)
}

// ------------------------------------------------------------- corpus plausibility

// A walker that silently matched nothing would make every check below pass while
// verifying nothing — the failure mode `deadKeys.test.ts` and `dynamicKeys.test.ts`
// both guard against explicitly.
if (staticRefs.length < 3000) {
  console.error(
    `only ${staticRefs.length} static key references found — the scan is broken, not the tree. `
    + 'Expected thousands; refusing to report a pass.',
  )
  process.exit(2)
}
if (english.size < 3000) {
  console.error(`only ${english.size} English keys loaded — catalogs did not parse as expected.`)
  process.exit(2)
}

// ---------------------------------------------------------------- 1. dangling references

const dangling = staticRefs
  .filter((ref) => !isTestFile(ref.rel) && !resolves(ref.key))
  .sort((a, b) => a.rel.localeCompare(b.rel) || a.line - b.line)

const danglingInTests = staticRefs
  .filter((ref) => isTestFile(ref.rel) && !resolves(ref.key))
  .sort((a, b) => a.rel.localeCompare(b.rel) || a.line - b.line)

// ---------------------------------------------------------------- 2. dynamic-site ratchet

const dynamicByFile = {}
for (const site of dynamicSites) {
  if (isTestFile(site.rel)) continue
  dynamicByFile[site.rel] = (dynamicByFile[site.rel] || 0) + 1
}

const staticCount = staticRefs.filter((r) => !isTestFile(r.rel)).length
const dynamicCount = Object.values(dynamicByFile).reduce((a, b) => a + b, 0)
const coverage = ((staticCount / (staticCount + dynamicCount)) * 100).toFixed(2)

const current = {
  _comment:
    'Translation call sites whose key CANNOT be resolved statically, per file. Generated — '
    + 'regenerate with `node scripts/check-i18n-keys.mjs --update`. This is the coverage '
    + 'statement for the key-reference gate: every site listed here is one the gate cannot '
    + 'check. Ratchet DOWN by rewriting the site as an `as const` map access (see '
    + '`UPDATE_ERROR_KEYS` in pages/settings/AboutPanel.tsx); never raise it.',
  _total: dynamicCount,
  // The static-reference count and coverage percentage are deliberately NOT committed.
  // The gate compares `files` only, so a committed copy of either would drift on every
  // ordinary PR that adds a resolvable key — going stale without failing anything, which
  // is worse than absent because a reader trusts it. Both are printed on every run
  // instead, where they cannot rot.
  files: Object.fromEntries(Object.entries(dynamicByFile).sort(([a], [b]) => a.localeCompare(b))),
}

if (UPDATE) {
  fs.writeFileSync(BASELINE, `${JSON.stringify(current, null, 2)}\n`)
  console.log(
    `wrote ${path.relative(ROOT, BASELINE)}: ${dynamicCount} dynamic site(s) across `
    + `${Object.keys(dynamicByFile).length} file(s); ${staticCount} static references (${coverage}% coverage)`,
  )
  process.exit(0)
}

if (!fs.existsSync(BASELINE)) {
  console.error(`missing ${path.relative(ROOT, BASELINE)} — run with --update`)
  process.exit(2)
}
const base = JSON.parse(fs.readFileSync(BASELINE, 'utf-8'))

const grew = []
const shrank = []
const seen = new Set()
for (const [file, now] of Object.entries(dynamicByFile)) {
  seen.add(file)
  const then = base.files[file] ?? 0
  if (now > then) grew.push(`  ${file}: ${then} → ${now}`)
  else if (now < then) shrank.push(`  ${file}: ${then} → ${now}`)
}
for (const [file, then] of Object.entries(base.files)) {
  if (!seen.has(file)) shrank.push(`  ${file}: ${then} → 0`)
}

// ---------------------------------------------------------------- report

if (REPORT) {
  console.log(`static key references: ${staticCount} across ${new Set(staticRefs.map((r) => r.rel)).size} file(s)`)
  console.log(`distinct keys referenced: ${new Set(staticRefs.map((r) => r.key)).size} of ${english.size} English keys`)
  console.log(`dynamic call sites:    ${dynamicCount}`)
  console.log(`static coverage:       ${coverage}%`)
  for (const site of dynamicSites.filter((s) => !isTestFile(s.rel))) {
    console.log(`  dynamic  ${site.rel}:${site.line}  ${site.text}`)
  }
  if (danglingInTests.length > 0) {
    console.log(`\ntest-file references to absent keys (not gated — several are deliberate fixtures):`)
    for (const ref of danglingInTests) console.log(`  ${ref.rel}:${ref.line}  ${ref.key}`)
  }
  // DIAGNOSTIC ONLY. `deadKeys.test.ts` owns this question with a wider reference scan
  // (quoted strings anywhere in `src`, so it sees data tables, comments and tests).
  // This narrower AST view is printed to make the difference measurable, never to gate:
  // failing on it would contradict `deadKeys`, which is the source of truth.
  const referenced = new Set(staticRefs.map((r) => r.key))
  const unreferenced = [...english].filter((key) => {
    if (referenced.has(key)) return false
    const base = key.replace(/_(zero|one|two|few|many|other)$/, '')
    return !referenced.has(base) && !pluralRegistry.has(base)
  })
  console.log(
    `\ndiagnostic: ${unreferenced.length} English key(s) have no STATIC reference. This is NOT a `
    + 'failure and NOT comparable to deadKeys.test.ts (baseline 19), whose quoted-string scan '
    + 'also sees data tables, doc comments and tests. The gap between the two numbers is the '
    + 'indirection surface.',
  )
}

// ---------------------------------------------------------------- verdict

let failed = false

if (dangling.length > 0) {
  failed = true
  console.error(
    `${dangling.length} translation key reference(s) do not exist in the English catalogs:\n`
    + `${dangling.map((r) => `  ${r.rel}:${r.line}  ${r.key}`).join('\n')}\n\n`
    + 'Each one renders the raw dotted key into the UI, because i18next returns a missing key\n'
    + 'as its own fallback instead of throwing. Either add the key to en.manual.json (and all 9\n'
    + 'target catalogs, per catalogParity) or fix the reference. This is NOT ratcheted: there is\n'
    + 'no acceptable number of call sites that render their own key name to a user.',
  )
}

if (shadowed.length > 0) {
  failed = true
  console.error(
    `\n${shadowed.length} key(s) exist in BOTH en.json and en.manual.json:\n`
    + `${shadowed.slice(0, 20).map((k) => `  ${k}`).join('\n')}\n\n`
    + '`src/i18n/index.ts` deep-merges with the manual catalog winning, so the generated value is\n'
    + 'dead while the codemod keeps regenerating it — the two drift apart with nothing to say so.\n'
    + 'Keep the key in exactly one file: en.manual.json if it has no source literal to extract,\n'
    + 'otherwise let the codemod own it and delete the manual copy.',
  )
}

// REPORT, not a gate — in EITHER direction. This was the repo's last bidirectional
// ratchet: it failed when the number went UP and also when it went DOWN, so improving a
// file broke CI until someone committed a new count to a file every branch shares. That
// made it a merge-conflict generator whose failures were, in both directions,
// unattributable to the diff in front of them. The dangling-reference check above stays
// a HARD ZERO, because a reference to a key that does not exist renders a raw dotted key
// to a user and there is no ceiling to inherit.
if (grew.length > 0) {
  console.log(
    `\n[dynamic-keys] REPORT: ${grew.length} file(s) gained call sites whose key cannot be\n`
    + `resolved statically:\n${grew.join('\n')}\n\n`
    + 'This does NOT fail the run. A key this gate cannot resolve is a key it cannot verify\n'
    + 'exists, so the site is exempt from every check above — worth fixing, not worth\n'
    + 'blocking an unrelated PR. Index an `as const` map of full literal keys instead — see\n'
    + '`UPDATE_ERROR_KEYS` in pages/settings/AboutPanel.tsx or `STATUS_LABEL_KEY` in\n'
    + 'pages/chat/McpToolsPanel.tsx, which resolve to 7 and 3 checked keys respectively.',
  )
}

if (shrank.length > 0) {
  console.log(
    `\n[dynamic-keys] REPORT: ${shrank.length} file(s) improved. Nothing to do — re-snapshot\n`
    + `with --update only if you want the record tightened:\n${shrank.join('\n')}`,
  )
}

if (failed) process.exit(1)

// Unconditional, and it must stay that way: `[key-refs]` reads this line as its own
// success signal, so suppressing it when the dynamic-site count moves would make THAT
// row MISSING and fail the step. `[dynamic-keys]` shares the line, which is why
// `resolveRows` tries an `over` pattern BEFORE this one — see i18n-gate-table.mjs.
console.log(
  `OK: ${staticCount} static key references all resolve, `
  + `${dynamicCount} dynamic site(s) at baseline (${coverage}% static coverage), `
  + 'no en.json/en.manual.json shadowing.',
)
