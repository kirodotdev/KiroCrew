/**
 * Repository guard for the relocatable dashboard runtime (R1-A).
 *
 * NOTE: no `#!` shebang — this module's pure detectors (`scanSource`,
 * `computeViolations`) are imported by `src/test/dashboardRuntimeGuard.test.ts`,
 * and a src-imported script that opens with a shebang breaks the Windows Vite
 * transform (see `src/test/scriptsShebang.test.ts`). It is invoked as
 * `node scripts/check-dashboard-runtime.mjs`, so the shebang was decorative.
 *
 * The dashboard must aim every request at ITS OWN gateway through one typed
 * seam — `src/lib/dashboardRuntime.ts` — so the same bundle runs both at the
 * origin root (`direct`) and under a capability prefix (`relayed-pane`). A
 * fetch/EventSource whose target is a root-absolute same-dashboard path, or a
 * raw `new WebSocket(…)`, bypasses that seam and would escape the prefix in
 * relay mode. This check fails such a bypass in CI.
 *
 * It is AST-based (via the `typescript` compiler API) and shares its classifier
 * with the migration codemod through `scripts/gatewayFetchClassifier.mjs`. The
 * guard FAILS a SUPERSET of the set the codemod WRAPS: it scans with the
 * scope-aware `argEscapesToRoot` detector, while the codemod auto-wraps only the
 * narrower `sameDashboardArg` shapes it can rewrite safely. Beyond a plain root
 * literal it therefore catches the shipped variable/template bypasses a regex
 * missed — `` fetch(`${API}/x`) ``, `fetch(API_BASE)`, `fetch(BASE + path)` —
 * where the leading token is a module-scope root-const
 * (`const API = '/api/apps/x'`), AND the scope-threaded shapes the codemod
 * leaves for hand-relocation (`const url = BASE + path`, a root-defaulted helper
 * parameter, a `provider || '/api/…'` fallback).
 *
 * ZERO TOLERANCE, three gateway-connection families. Every same-dashboard call
 * goes through the runtime seam:
 *
 *  - A same-dashboard `fetch(…)` → `fetch(relocateRequestUrl(…))` (or
 *    `dashboardFetch(gatewayPath)` for a new caller). All pre-seam sites were
 *    migrated by `scripts/migrate-fetch-to-runtime.mjs`, so the allowed count is
 *    0 everywhere and any reintroduced bypass fails.
 *  - Raw `new WebSocket(`        → `dashboardWebSocket(gatewayPath)`. The only
 *    allowed constructor is the one inside the runtime module itself.
 *  - A same-dashboard `new EventSource(…)` → `new EventSource(relocateRequestUrl(
 *    …))`. SSE resolves against the document origin like fetch, so a root target
 *    escapes the prefix in relay mode.
 *
 * There is no baseline: the migration is complete, so the ratchet that once
 * tracked the retained sites is gone. A new bypass in any file fails the gate.
 *
 * Usage:
 *   node scripts/check-dashboard-runtime.mjs           # gate (exit 1 on any bypass)
 *
 * NOT matched — a genuinely external or non-statically-resolvable target stays
 * explicit: external URLs (`https://…`, protocol-relative `//…`, `blob:`/`data:`),
 * and a bare parameter (`fetch(url)`) whose value cannot be proven same-dashboard
 * — those same-dashboard helpers are relocated at their own `fetch(param)`
 * boundary, which then reads as an already-relocated call here.
 */
import { readFileSync, globSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import {
  ts,
  parseSource,
  augmentedConstNames,
  makeModuleLoader,
  argEscapesToRoot,
  isFetchCallee,
  isRelocated,
} from './gatewayFetchClassifier.mjs'

const WEBSITE_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

/** The runtime module owns the one blessed `new WebSocket(` and needs no fetch. */
const RUNTIME_MODULE = 'src/lib/dashboardRuntime.ts'

/**
 * Files the guard does not scan:
 *  - tests and stories are development-only, not shipped gateway callers;
 *  - `src/apps/mochi/src/**` is the standalone Electron mochi app — a separate
 *    runtime with its own backend, not the dashboard SPA.
 */
function isExcluded(rel) {
  if (/\.(test|spec)\.[tj]sx?$/.test(rel)) return true
  if (/\.stories\.[tj]sx?$/.test(rel)) return true
  if (rel.startsWith('src/apps/mochi/src/')) return true
  return false
}

/**
 * Pure detector — AST-based, counted per file, and unit-tested directly.
 *
 * `fetchLiterals` counts every `fetch(arg)` / `<obj>.fetch(arg)` whose first
 * argument PROVABLY resolves to a root-absolute, same-dashboard path and is NOT
 * already relocated — the shared classifier's scope-aware `argEscapesToRoot`
 * decides (the guard's superset detector), so this
 * catches a root literal AND the template/const/concat bypasses a regex missed
 * (`` fetch(`${API}/x`) ``, `fetch(API_BASE)`, `fetch(BASE + path)`). The name is
 * kept for the guard's stable count API; it means "same-dashboard fetch bypass".
 * `eventSourceLiterals` is the same for `new EventSource(arg)` (SSE resolves
 * against the document origin too). `rawWebSockets` counts every
 * `new WebSocket(` — the runtime module owns the one blessed constructor;
 * `dashboardWebSocket(...)` is the only sanctioned caller elsewhere.
 *
 * A `rel` (filename) picks TS vs TSX parsing; it defaults to a `.tsx` snippet so
 * the unit test can call `scanSource(text)` with a bare expression.
 *
 * `loadModule` (optional) resolves relative named imports so an IMPORTED
 * same-dashboard path constant (`import { CONFIG_PATH } from './constants'`,
 * then `fetch(CONFIG_PATH)`) is classified through its declaring module —
 * closing the blind spot where an imported constant bypassed the guard
 * silently. Omitted in unit tests, where same-file consts are enough.
 *
 * `argEscapesToRoot` is SCOPE-AWARE: beyond a root literal / module-scope const,
 * it resolves a function-local derived root path (`const url = BASE + path`) and
 * a root-defaulted gateway helper parameter through the enclosing scopes, so
 * neither can be pinned safe. A bare, unresolvable parameter is still not
 * flagged — that stays the helper's own relocate-at-boundary responsibility.
 */
export function scanSource(text, rel = 'snippet.tsx', loadModule) {
  const sf = parseSource(rel, text)
  const consts = augmentedConstNames(sf, rel, loadModule)
  let fetchLiterals = 0
  let rawWebSockets = 0
  let eventSourceLiterals = 0
  const visit = (node) => {
    if (ts.isCallExpression(node) && isFetchCallee(node.expression)) {
      const arg = node.arguments[0]
      if (argEscapesToRoot(arg, sf, consts) && !isRelocated(arg)) fetchLiterals++
    } else if (ts.isNewExpression(node) && ts.isIdentifier(node.expression)) {
      if (node.expression.text === 'WebSocket') {
        rawWebSockets++
      } else if (node.expression.text === 'EventSource') {
        const arg = node.arguments && node.arguments[0]
        if (argEscapesToRoot(arg, sf, consts) && !isRelocated(arg)) eventSourceLiterals++
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return { fetchLiterals, rawWebSockets, eventSourceLiterals }
}

function listSourceFiles() {
  return globSync('src/**/*.{ts,tsx}', { cwd: WEBSITE_DIR })
    .map((p) => p.split(path.sep).join('/'))
    .filter((rel) => !isExcluded(rel))
    .sort()
}

function collectCounts() {
  // One memoized resolver for the whole tree, so an imported same-dashboard path
  // constant is classified through the module that declares it (no allowlist).
  const loadModule = makeModuleLoader(WEBSITE_DIR)
  const counts = {}
  for (const rel of listSourceFiles()) {
    const text = readFileSync(path.join(WEBSITE_DIR, rel), 'utf8')
    counts[rel] = scanSource(text, rel, loadModule)
  }
  return counts
}

/**
 * Pure gate logic over already-collected per-file counts. Zero tolerance: any
 * root-literal fetch (outside the runtime module, which carries none) or any
 * raw WebSocket constructor (outside the runtime module, which owns the one
 * blessed constructor) is a violation. Exported so the unit test can prove a
 * new bypass fails without walking the tree.
 */
export function computeViolations(counts) {
  const violations = []
  for (const [rel, { fetchLiterals, rawWebSockets, eventSourceLiterals = 0 }] of Object.entries(counts)) {
    if (rawWebSockets > 0 && rel !== RUNTIME_MODULE) {
      violations.push(
        `${rel}: ${rawWebSockets} raw \`new WebSocket(\` — use dashboardWebSocket() from lib/dashboardRuntime`,
      )
    }
    if (fetchLiterals > 0 && rel !== RUNTIME_MODULE) {
      violations.push(
        `${rel}: ${fetchLiterals} same-dashboard fetch bypass — a root-literal fetch('/…'), ` +
          "a `${API}/…` template, a root-const identifier, or an `API + path` concat — " +
          `route it through dashboardFetch(gatewayPath) / relocateRequestUrl`,
      )
    }
    if (eventSourceLiterals > 0 && rel !== RUNTIME_MODULE) {
      violations.push(
        `${rel}: ${eventSourceLiterals} same-dashboard new EventSource bypass — ` +
          `wrap the URL in relocateRequestUrl() so the SSE stream stays under the prefix`,
      )
    }
  }
  return violations
}

/** Compute violations over the whole tree. */
export function findViolations() {
  return computeViolations(collectCounts())
}

function main() {
  const violations = findViolations()
  if (violations.length > 0) {
    console.error('Dashboard runtime guard failed — gateway URL(s) bypass lib/dashboardRuntime:\n')
    for (const v of violations) console.error('  - ' + v)
    console.error(
      '\nRoute same-dashboard HTTP through dashboardFetch(gatewayPath(...)) or relocateRequestUrl(),\n' +
        'and same-dashboard sockets through dashboardWebSocket(...). scripts/migrate-fetch-to-runtime.mjs\n' +
        '(run with --apply) auto-wraps the statically-simple fetch shapes, but this guard also fails a\n' +
        'superset it cannot rewrite — a scope-threaded local (const url = BASE + path), a root-defaulted\n' +
        "helper parameter, or a `provider || '/api/…'` fallback — which you relocate by hand.",
    )
    process.exit(1)
  }
  console.log('Dashboard runtime guard passed.')
}

// Only run as a CLI, not when imported by the unit test.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main()
}
