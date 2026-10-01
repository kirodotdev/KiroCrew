#!/usr/bin/env node
/**
 * One-shot, re-runnable codemod that routes every same-dashboard root-literal
 * `fetch('/…')` through the relocatable dashboard runtime seam
 * (`src/lib/dashboardRuntime.ts`).
 *
 * It is the deterministic lever behind the repository guard
 * (`scripts/check-dashboard-runtime.mjs`): the guard FAILS a bypass, this
 * MIGRATES one. This codemod wraps the guard's REWRITABLE SUBSET — a `fetch(`
 * call whose first argument PROVABLY resolves to a root-absolute, same-dashboard
 * path AND exposes a single first-arg span it can wrap verbatim (see
 * `sameDashboardArg`): a root-absolute string/template literal (`'/…'`, `` `/…` ``),
 * a `` `${API}/…` `` template or a bare identifier where `API` is a module-scope
 * root-const (`const API = '/api/apps/x'`), or an `API + path` concatenation off
 * such a const. The guard additionally FAILS a superset (its `argEscapesToRoot`
 * detector) — scope-threaded locals (`const url = BASE + path`), root-defaulted
 * helper parameters, and `||`/`??` fallbacks — which this codemod deliberately
 * does NOT auto-wrap, because there is no single span to rewrite without risking
 * behavior change; those stay a hand-relocation. Protocol-relative (`//host`),
 * scheme (`https://…`), bare parameters (`fetch(url)` — not provably
 * same-dashboard), and already-migrated forms (`fetch(relocateRequestUrl(…))`,
 * `dashboardFetch(…)`) are left untouched: a genuinely external route stays
 * explicit, and a same-dashboard helper is relocated at its own `fetch(param)`
 * boundary instead.
 *
 * The transform is a behavior-preserving wrap: `fetch(ARG, init?)` becomes
 * `fetch(relocateRequestUrl(ARG), init?)`. In `direct` mode `relocateRequestUrl`
 * is the identity, so the emitted request URL is byte-for-byte what it was
 * before; in `relayed-pane` mode the same call stays under the capability
 * prefix. `init` and every other argument are untouched.
 *
 * AST-based (via the `typescript` compiler API) so the first-argument span is
 * found exactly, regardless of the concatenations, template literals, or nested
 * `new URLSearchParams({…})` inside it — a regex cannot find that boundary
 * safely. Idempotent: a migrated call no longer matches, so re-running is a
 * no-op, which is the determinism check.
 *
 * Usage:
 *   node scripts/migrate-fetch-to-runtime.mjs            # dry run: list every planned edit
 *   node scripts/migrate-fetch-to-runtime.mjs --apply    # write the edits
 *
 * Scope mirrors the guard's `isExcluded`: `src/**\/*.{ts,tsx}` minus tests,
 * stories, and the standalone `src/apps/mochi/src/**` Electron app.
 */
import { readFileSync, writeFileSync, globSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import {
  ts,
  augmentedConstNames,
  makeModuleLoader,
  sameDashboardArg,
  isFetchCallee,
  isRelocated as isSeamWrapped,
} from './gatewayFetchClassifier.mjs'

const WEBSITE_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const RUNTIME_MODULE_REL = 'src/lib/dashboardRuntime'
const RUNTIME_IMPORT_NAME = 'relocateRequestUrl'

/** Same exclusions as scripts/check-dashboard-runtime.mjs. */
function isExcluded(rel) {
  if (/\.(test|spec)\.[tj]sx?$/.test(rel)) return true
  if (/\.stories\.[tj]sx?$/.test(rel)) return true
  if (rel.startsWith('src/apps/mochi/src/')) return true
  return false
}

function listSourceFiles() {
  return globSync('src/**/*.{ts,tsx}', { cwd: WEBSITE_DIR })
    .map((p) => p.split(path.sep).join('/'))
    .filter((rel) => !isExcluded(rel) && rel !== `${RUNTIME_MODULE_REL}.ts`)
    .sort()
}

// `rootConstNames` + `sameDashboardArg` live in `./gatewayFetchClassifier.mjs`,
// the one source of truth this codemod and the guard share. The codemod reads
// `sameDashboardArg` (rewritable subset); the guard reads the `argEscapesToRoot`
// superset from the same module.

/** The relative, extensionless specifier from `rel` to the runtime module. */
function specifierFor(rel) {
  const fromDir = path.posix.dirname(rel)
  let spec = path.posix.relative(fromDir, RUNTIME_MODULE_REL)
  if (!spec.startsWith('.')) spec = './' + spec
  return spec
}

/**
 * Collect the byte spans of first-arguments to wrap, and whether the file
 * already imports the runtime helper (and, if so, where to merge it).
 */
function analyzeFile(rel, text, loadModule) {
  const kind = rel.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  const sf = ts.createSourceFile(rel, text, ts.ScriptTarget.Latest, true, kind)

  const wraps = [] // { start, end }
  let importInfo = { has: false, mergeAt: -1, firstImportStart: -1 }
  // Same augmented const set the guard resolves (`augmentedConstNames`), so an
  // imported same-dashboard path constant is classified identically. The
  // detector below is `sameDashboardArg` (rewritable subset); the guard's
  // `argEscapesToRoot` superset can still FAIL shapes this codemod leaves for
  // hand-relocation.
  const consts = augmentedConstNames(sf, rel, loadModule)

  const visit = (node) => {
    if (ts.isImportDeclaration(node)) {
      const spec = node.moduleSpecifier
      if (importInfo.firstImportStart < 0) importInfo.firstImportStart = node.getStart(sf)
      if (ts.isStringLiteral(spec) && /(^|\/)lib\/dashboardRuntime$/.test(spec.text)) {
        const clause = node.importClause
        const named = clause && clause.namedBindings
        if (named && ts.isNamedImports(named)) {
          const already = named.elements.some((el) => el.name.text === RUNTIME_IMPORT_NAME)
          importInfo.has = already
          if (!already) {
            importInfo.mergeAt = named.elements.length > 0 ? named.elements[0].getStart(sf) : named.getStart(sf) + 1
          } else {
            importInfo.mergeAt = -1
          }
        }
      }
    }
    if (ts.isCallExpression(node) && isFetchCallee(node.expression) && node.arguments.length > 0) {
      const arg = node.arguments[0]
      if (sameDashboardArg(arg, sf, consts) && !isSeamWrapped(arg)) {
        wraps.push({ start: arg.getStart(sf), end: arg.getEnd() })
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return { wraps, importInfo }
}

/** Apply the wrap + import edits to `text`, returning the new text. */
function rewrite(text, rel, analysis) {
  const edits = []
  for (const w of analysis.wraps) {
    edits.push({ start: w.start, end: w.start, insert: `${RUNTIME_IMPORT_NAME}(` })
    edits.push({ start: w.end, end: w.end, insert: ')' })
  }
  if (!analysis.importInfo.has) {
    if (analysis.importInfo.mergeAt >= 0) {
      edits.push({ start: analysis.importInfo.mergeAt, end: analysis.importInfo.mergeAt, insert: `${RUNTIME_IMPORT_NAME}, ` })
    } else {
      const spec = specifierFor(rel)
      const at = analysis.importInfo.firstImportStart >= 0 ? analysis.importInfo.firstImportStart : 0
      edits.push({ start: at, end: at, insert: `import { ${RUNTIME_IMPORT_NAME} } from '${spec}'\n` })
    }
  }
  // Apply from the end so earlier offsets stay valid; ties keep insertion order
  // (the wrap's opening paren must precede a same-position import insert — but
  // wrap positions never coincide with the import position, so order is moot).
  edits.sort((a, b) => b.start - a.start)
  let out = text
  for (const e of edits) {
    out = out.slice(0, e.start) + e.insert + out.slice(e.end)
  }
  return out
}

function main() {
  const apply = process.argv.includes('--apply')
  const loadModule = makeModuleLoader(WEBSITE_DIR)
  let totalWraps = 0
  let filesChanged = 0
  const report = []
  for (const rel of listSourceFiles()) {
    const abs = path.join(WEBSITE_DIR, rel)
    const text = readFileSync(abs, 'utf8')
    const analysis = analyzeFile(rel, text, loadModule)
    if (analysis.wraps.length === 0) continue
    totalWraps += analysis.wraps.length
    filesChanged += 1
    report.push(`${rel}: ${analysis.wraps.length} fetch site(s)` + (analysis.importInfo.has ? '' : analysis.importInfo.mergeAt >= 0 ? ' [+merge import]' : ' [+new import]'))
    if (apply) {
      writeFileSync(abs, rewrite(text, rel, analysis))
    }
  }
  for (const line of report) console.log('  ' + line)
  console.log(`\n${apply ? 'Applied' : 'Planned'}: ${totalWraps} fetch wraps across ${filesChanged} files.`)
  if (!apply) console.log('Dry run — re-run with --apply to write.')
}

main()
