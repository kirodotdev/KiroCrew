// Fail the build when the emitted JS chunks statically import each other in a
// cycle.
//
// Usage:
//   vite build --mode analyze && node scripts/check-chunk-cycles.mjs
//   node scripts/check-chunk-cycles.mjs [path/to/bundle-report.json]
//
// What this catches is a blank page, not a size regression. Two chunks that
// statically import each other have no valid initialization order: one body runs
// while a binding it reads is still uninitialized. In this app that lands on
// `new QueryClient(...)`, which throws before React mounts, so the user gets the
// shell's dark skeleton and nothing else -- indistinguishable from the mobile
// black screen PRs #9518 and #9540 were about.
//
// No gate that MEASURES the bundle can see it: check-bundle-size.mjs compares
// bytes per chunk, tsc and eslint never look at the built output, and the unit
// suite does not load one. The e2e job is the one that could -- it runs
// `npm run build` and stages `website/dist` so the specs drive the real bundled
// dashboard (docs/ci/e2e-gate.md) -- but only indirectly and only sometimes: a
// cycle surfaces there as a spec failing for an unrelated-looking reason, and only
// when it happens to break a surface some spec drives. This check is the
// deterministic version. It reads the graph itself, fails before any spec runs, and
// names the chunks in the cycle instead of leaving someone to find them. The
// failure is also reachable from a config edit alone: rolldown's
// `includeDependenciesRecursively: false` produced two cycles on a tree that had
// none -- a 71-chunk one spanning App, client, vendor-react and vendor-icons, and
// a 3-chunk one across the graph chunks -- with every other gate green.
//
// Reads the same `dist/bundle-report.json` the size gate does, so it costs one
// extra script run on the analyze build CI already performs, and no second build.
//
// AN ACYCLIC CHUNK GRAPH IS A DELIBERATE INVARIANT, and this gate has no
// allowlist -- deliberately unlike its sibling's CHUNK_BUDGETS, where a named
// ceiling is a measured tradeoff a human can re-measure. A cycle is not that kind
// of tradeoff. Whether one is fatal depends on whether a binding is READ during
// evaluation, which the report cannot tell you: the same cycle is inert until an
// import order changes and then blanks the page. So an allowlisted cycle would be
// precisely the failure this gate exists to stop, recorded as approved. The base
// tree has 0 cycles across ~2058 static edges, so the invariant is the status quo
// rather than a new burden, and the fix for a red is to change the chunking (see
// the `codeSplitting` comment in website/vite.config.ts) rather than to waive it.
import path from 'path'
import { pathToFileURL } from 'url'
import { failGate, findChunkCycles, loadSummaryOrExit } from './lib/bundleReport.mjs'

const REPORT_PATH = path.resolve('dist', 'bundle-report.json')

/** Most chunks in a cycle to name before truncating, per cycle. */
const MAX_LISTED = 12

// This gate's own exit codes, beyond the 2 (missing) / 3 (malformed) that
// loadSummaryOrExit owns: 4 = report valid but lists no chunks, 5 = chunks carry
// no `imports` so no edge was measured. 4 and 5 are separate because they need
// different fixes: 4 means the build emitted nothing, 5 means the report predates
// the `imports` field and the analyze build must be re-run.

export function main(argv = process.argv.slice(2)) {
  const reportPath = argv[0] ? path.resolve(argv[0]) : REPORT_PATH
  const summary = loadSummaryOrExit(reportPath)
  const { cycles, edgeCount, checkedCount, missingImports } = findChunkCycles(summary)

  // Same fail-closed posture as the size gate's empty-report check: a gate that
  // measured nothing must not certify anything.
  if (checkedCount === 0) {
    failGate(
      `no chunks in ${reportPath} -- the gate measured nothing, so it cannot ` +
        'certify an acyclic graph. Re-run `vite build --mode analyze` and check ' +
        'it emitted a bundle.',
      4
    )
  }

  if (missingImports) {
    failGate(
      `no chunk in ${reportPath} carries an 'imports' list, so no edge was ` +
        'measured and "no cycles" would be vacuous. The field is written by the ' +
        'kirocrew-bundle-report plugin in website/scripts/lib/bundleReport.mjs; ' +
        're-run `vite build --mode analyze` with a build that includes it.',
      5
    )
  }

  if (cycles.length === 0) {
    process.stdout.write(
      `chunk-cycle gate: ${checkedCount} chunks, ${edgeCount} static edges, no cycles.\n`
    )
    return
  }

  const lines = []
  for (const cycle of cycles) {
    lines.push(`  cycle of ${cycle.length}:`)
    for (const fileName of cycle.slice(0, MAX_LISTED)) lines.push(`    ${fileName}`)
    if (cycle.length > MAX_LISTED) lines.push(`    ... and ${cycle.length - MAX_LISTED} more`)
  }
  failGate(
    `${cycles.length} static import cycle(s) between emitted chunks:\n` +
      `${lines.join('\n')}\n` +
      'A cycle has no valid initialization order, so a chunk body runs against an ' +
      'uninitialized binding and the page blanks before React mounts. This is ' +
      'usually chunking configuration rather than application code: check the ' +
      '`codeSplitting` groups in website/vite.config.ts, and in particular do not ' +
      'set `includeDependenciesRecursively: false` -- that is what produced the ' +
      'cycles this gate was written for.'
  )
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main()
}
