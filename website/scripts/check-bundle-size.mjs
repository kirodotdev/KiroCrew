// Per-chunk bundle-size regression gate.
//
// Usage:
//   vite build --mode analyze && node scripts/check-bundle-size.mjs
//   node scripts/check-bundle-size.mjs [path/to/bundle-report.json]
//
// The global `chunkSizeWarningLimit` in vite.config.ts is one ceiling for every
// chunk, sized to the largest known-large chunk -- so it cannot tell
// "known-large" from "newly oversized": any NEW chunk up to that ceiling is
// admitted silently. This gate closes that gap with explicit per-chunk budgets:
// the chunks that are irreducibly large today are allowlisted at ceilings just
// above their measured size, and every other chunk gets a 500 KB default. A
// chunk over its budget fails the build with one actionable line per breach.
//
// Reads the `dist/bundle-report.json` that the `kirocrew-bundle-report` plugin
// emits in analyze mode (see vite.config.ts), so a normal `npm run build` stays
// byte-for-byte unaffected -- CI runs the analyze build and then this script.
import path from 'path'
import { pathToFileURL } from 'url'
import { checkChunkBudgets, formatBytes, loadBundleSummary } from './lib/bundleReport.mjs'

const KB = 1024

/** Budget for any chunk without an explicit allowlist entry below. */
export const DEFAULT_BUDGET_BYTES = 500 * KB

/**
 * Explicit ceilings for the chunks that are already known-large, keyed by
 * LOGICAL chunk name (the emitted file name minus the `assets/` prefix and the
 * content hash -- see `logicalChunkName`), never by a hashed file name.
 *
 * Every entry documents WHY the chunk is exempt from the default budget. Each
 * ceiling is the size measured by an analyze build, plus roughly 5% headroom so
 * routine churn (a new string, a dependency patch release) does not fail
 * unrelated PRs, while a real regression -- a new library landing in the chunk
 * -- still trips it. Lower a ceiling the moment its chunk shrinks; raising one
 * is a bundle-size regression and needs to be justified in the PR that does it.
 */
export const CHUNK_BUDGETS = {
  // Eager i18n catalogs for all shipped languages, reached through
  // `src/i18n/all.ts` — Rolldown names the chunk after that entry. Grows a
  // little with every translated string, which is expected and fine; what this
  // ceiling catches is a NEW library or surface landing in the catalog chunk.
  // The built-in App Store guidance adds one use-case and one configuration
  // string for each of 23 apps across all 12 shipped catalogs. The Dev Fleet
  // closed-PR prune group and the expanded Disconnect guidance are the largest
  // recent catalog increments included in this measurement; Dev Fleet's
  // per-pod system readout then adds its own strings across the same 12
  // catalogs on top of that baseline. The Code Review Sage guided Review
  // Fix surface (~130 keys) also ships translated in all 12 catalogs, and
  // the catalogs are eager by design (see `src/i18n/index.ts`'s
  // lazy-loading seam), so the growth lands here in full.
  all: 10570 * KB, // measured 10066 KB with the Sage Review Fix strings on main's 9985 KB (~5% headroom)

  // The i18n RUNTIME — the i18next singleton, `initI18n`, the English catalog —
  // named after `src/i18n/t.ts`. Held separately from `all` above because
  // `src/i18n/index.ts` imports English alone, so the ~600 components that call
  // `t()` no longer pull the other twelve catalogs in behind them. Sized for the
  // English catalog plus headroom; a jump here means a non-English catalog, or a
  // library, reached the runtime module.
  // Re-measured 2026-09-04 at 740 KB, and the 702 KB note above was ~38 KB
  // stale, which is the same recurrence it describes: main drifted to EXACTLY
  // 740.00 KB (757,764 B, 4 B over its own ceiling), so the gate began failing
  // on the merge ref of every open PR rather than on the new library or surface
  // it exists to catch. Attribution was measured, not assumed -- the branch that
  // tripped it first builds a BYTE-IDENTICAL `t` chunk to its own base
  // (`t-BLZeayKy.js`, 755,868 B on both), and main's tip alone, with none of that
  // branch's code, reproduces the 4-byte failure with the same content hash. So
  // the growth is main's accumulated English strings, and headroom is what was
  // actually missing. 5% headroom, matching the `all` entry's convention above,
  // so the next English string does not re-trip this for the third time.
  t: 777 * KB, // measured 740 KB on main @ 1cd64b8c9 (~5% headroom)

  // Pierre editor implementation (PR #4072 replaced Monaco, whose
  // 'editor.api2' chunk this entry set used to carry) -- the code-editor
  // engine, code-split from the app core and not usefully splittable further.
  PierreImpl: 570 * KB, // measured 540 KB

  // Textmate grammar bundles shipped with the pierre editor's syntax
  // highlighting (PR #4072). Each is a prebuilt upstream grammar artifact,
  // lazy-loaded per language; size is fixed by the grammar, not our code.
  'emacs-lisp': 810 * KB, // measured 772 KB
  cpp: 806 * KB, // measured 767 KB

  // The oniguruma regex engine WASM payload backing those grammars
  // (PR #4072); a single prebuilt binary, loaded on demand.
  wasm: 640 * KB, // measured 608 KB

  // The app-core chunk: the dashboard shell plus everything eagerly imported
  // from it. The vendor split in vite.config.ts already extracts the heaviest
  // libraries; what remains is first-party code with no clean lazy boundary.
  // Re-measured 2026-09-04: main drifted to 3201 KB (3,277,346 B, 546 B over
  // the previous 3200 KB ceiling), so the gate began failing on the merge ref
  // of every open PR rather than on a new library or surface — the same
  // recurrence the `t` entry above documents. Attribution was measured, not
  // assumed: main's tip alone, with no PR code, reproduces the failure.
  // 5% headroom, matching the `all` and `t` entries' convention, so ordinary
  // first-party growth does not re-trip this within days.
  App: 3360 * KB, // measured 3201 KB on main @ 701f8f981 (~5% headroom)

  // Markdown/math/syntax rendering stack (katex, highlight.js, remark/rehype)
  // -- one deliberate `manualChunks` bucket, see vite.config.ts.
  'vendor-markdown': 712 * KB, // measured 678 KB

  // Mermaid's own prebuilt internal chunk; the name comes from mermaid's build,
  // so it is stable for the pinned mermaid version but changes on upgrade. When
  // an upgrade renames it, the renamed chunk fails against the default budget
  // -- re-measure and replace this entry (and remove this stale one, which the
  // gate reports as unused).
  'chunk-KEIR6QF5': 680 * KB, // measured 647 KB (mermaid 11.16.1)

  // Excalidraw whiteboard (@excalidraw/excalidraw 0.18.1), reached ONLY through
  // SketchDialog's lazy `import()` when the composer's sketch pad opens — none
  // of these three chunks is statically imported or modulepreloaded (the entry
  // graph is unchanged; verified by grepping the built App chunk and
  // dist/index.html). Their sizes are the vendor's, not ours, and change only
  // with an Excalidraw upgrade — re-measure and rename these entries then, the
  // same maintenance contract as the mermaid entry above.
  //
  // `prod` is Excalidraw's main module (named after its dist/prod/index.js);
  // the two hash-named chunks are its font-subsetting payload for PNG/SVG
  // export (the large one is embedded font data) plus internals shared with
  // the subsetting worker. Canvas DISPLAY fonts are separate emitted assets
  // (dist/vendor/excalidraw/fonts/**, ~14MB, self-hosted by vite.config's
  // excalidrawFontsPlugin with EXCALIDRAW_ASSET_PATH pointed at them) — they
  // are not JS chunks, so this gate never sees them; without that plugin the
  // library fetches them from a third-party CDN at text-tool time.
  //
  // UPGRADE RITUAL — an Excalidraw bump moves THREE things in lockstep, and a
  // partial move fails at runtime, not build time: (1) the exact version in
  // package.json dependencies, (2) the scoped Radix/nanoid overrides beside it
  // (stale pins re-split the layer stack — the #6358 guard in
  // AgentSelector.dialog.test.tsx goes red), and (3) these hash-named chunk
  // entries (re-measure with an analyze build; stale names fail this gate's
  // matched-no-chunk warning).
  prod: 560 * KB, // measured 534 KB (@excalidraw/excalidraw 0.18.1)
  'chunk-EIO257PC': 1830 * KB, // measured 1744 KB (excalidraw 0.18.1 embedded font data, worker-loaded)
  'chunk-K2UTITRG': 550 * KB, // measured 522 KB (excalidraw 0.18.1 font-subsetting internals)

  // Graph/network visualization stack (vis-network, sigma, graphology,
  // cytoscape) -- one deliberate `manualChunks` bucket, see vite.config.ts.
  'vendor-graph': 606 * KB, // measured 577 KB

  // The SPA entry chunk: router, providers, and the eager page skeleton.
  main: 594 * KB, // measured 566 KB
}

const REPORT_PATH = path.resolve('dist', 'bundle-report.json')

function fail(message, code = 1) {
  process.stderr.write(`${message}\n`)
  process.exit(code)
}

// Exit-code mapping for this gate: 2 = report missing, 3 = report malformed or
// unsupported version, 4 = report valid but lists no chunks. The contract itself
// (existence/shape/version) lives in the shared loadBundleSummary; 4 is checked
// here rather than there because an empty report is legitimate for
// bundle-report.mjs, which simply has nothing to render.
function loadSummary(file) {
  const { summary, error } = loadBundleSummary(file, {
    hint:
      'Run `vite build --mode analyze` first -- a plain `npm run build` deliberately ' +
      'does not write one, so the normal build stays unaffected.',
  })
  if (error) fail(error.message, error.code === 'missing' ? 2 : 3)
  return summary
}

export function main(argv = process.argv.slice(2)) {
  const reportPath = argv[0] ? path.resolve(argv[0]) : REPORT_PATH
  const summary = loadSummary(reportPath)
  const { breaches, unusedBudgets, checkedCount } = checkChunkBudgets(summary, {
    budgets: CHUNK_BUDGETS,
    defaultBudget: DEFAULT_BUDGET_BYTES,
  })

  // A report that lists no chunks measured NOTHING, and the summary below would
  // call that "0 chunks within budget" and exit 0 -- a green gate over an unbuilt
  // tree. The build steps that feed it can fail this way silently: an analyze
  // build whose plugin stops emitting, a config change that empties the chunk
  // list, or a report written before the bundle exists. Refuse ahead of the
  // unused-budget warnings, so the actionable line is not buried under one
  // warning per allowlist entry (11 of them today).
  if (checkedCount === 0) {
    fail(
      `no chunks in ${reportPath} -- the gate measured nothing, so it cannot ` +
        'certify anything. Re-run `vite build --mode analyze` and check it ' +
        'emitted a bundle.',
      4
    )
  }

  for (const name of unusedBudgets) {
    process.stderr.write(
      `warning: budget entry '${name}' matched no emitted chunk -- ` +
        'remove it from CHUNK_BUDGETS in scripts/check-bundle-size.mjs if the chunk is gone or renamed.\n'
    )
  }

  if (breaches.length === 0) {
    process.stdout.write(
      `bundle-size gate: ${checkedCount} chunks within budget ` +
        `(default ${formatBytes(DEFAULT_BUDGET_BYTES)}, ${Object.keys(CHUNK_BUDGETS).length} allowlisted).\n`
    )
    return
  }

  // One actionable line per breach: a developer must be able to act on the
  // failure without re-running anything locally.
  for (const b of breaches) {
    process.stderr.write(
      `FAIL ${b.fileName}: ${formatBytes(b.size)} exceeds its ${formatBytes(b.budget)} budget ` +
        `by ${formatBytes(b.overage)} (chunk '${b.logicalName}')\n`
    )
  }
  fail(
    `${breaches.length} chunk(s) over budget. Either shrink the chunk (prefer a lazy ` +
      'import() boundary or a manualChunks split -- see website/vite.config.ts), or, if the ' +
      'growth is genuinely irreducible, add/adjust its entry in CHUNK_BUDGETS in ' +
      'scripts/check-bundle-size.mjs with a comment saying why, and justify it in the PR.'
  )
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main()
}
