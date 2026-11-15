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
import { checkChunkBudgets, failGate, firstLoadBytes, formatBytes, loadSummaryOrExit } from './lib/bundleReport.mjs'

const KB = 1024

/** Budget for any chunk without an explicit allowlist entry below. */
export const DEFAULT_BUDGET_BYTES = 500 * KB

/**
 * Ceiling for the first load: the raw (minified, uncompressed) bytes of the
 * `main` entry chunk plus every chunk it reaches through static imports, which is
 * what `dist/index.html` modulepreloads before the dashboard can render. The
 * per-chunk budgets below cannot see this total: the first load can grow by
 * hundreds of chunks that each stay under 500 KB.
 *
 * Measured 2026-11-13 with an analyze build of main @ 1c17fbd22a plus the change
 * that keeps PublishHub and AboutPanel out of the first load: 344 chunks,
 * 9,454,228 B (`firstLoadBytes` over dist/bundle-report.json). Main alone, before
 * that change, measured 9,514,411 B. Ceiling = 9,454,228 B * 1.05, rounded up
 * to whole KB. Same rule as the per-chunk entries: lower it when the first load
 * shrinks; raising it is a regression the PR that does it must justify.
 *
 * Re-measured on main @ c275f70106: 347 chunks, 9,464,931 B. Bundling the first
 * load into 11 chunks (the `firstLoadGroups(...)` call in vite.config.ts) takes
 * it to 9,416,379 B, so the ceiling is lowered to 9,416,379 B * 1.05.
 */
export const FIRST_LOAD_BUDGET_BYTES = 9656 * KB

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
 *
 * The lazy i18n catalog chunks depart from the 5% rule on purpose; their note
 * below carries the reasoning. Every other entry, and the default budget, follow it.
 */
/** Ceiling shared by the lazy per-language catalog chunks; see their entry below. */
const CATALOG_CHUNK_BUDGET = 3 * 1024 * KB

/** The non-English catalogs `src/i18n/lazy.ts` emits as their own chunks. */
const CATALOG_CHUNK_BUDGETS = Object.fromEntries(
  ['bn', 'de', 'es', 'fr', 'hi', 'it', 'ja', 'ko', 'pt', 'ru', 'zh-CN'].map((code) => [code, CATALOG_CHUNK_BUDGET])
)

export const CHUNK_BUDGETS = {
  // One lazy chunk per shipped non-English catalog, named after its locale
  // file and fetched by `src/i18n/lazy.ts` only for the language a user picks,
  // so none of them is on the first load. Catalog copy grows with every
  // translated feature (~200 KB a week across the eleven, measured in
  // September 2026), which is why these share one ceiling well above the
  // largest (bn, 1.71 MB) instead of a 5% one per file: a 5% ceiling here was
  // spent in about three days and then failed every open PR. A new language
  // needs its code added here; until then its chunk meets the 500 KB default.
  ...CATALOG_CHUNK_BUDGETS,

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

  // Markdown/math/syntax rendering stack (katex, highlight.js, remark/rehype)
  // -- one deliberate `codeSplitting` group, see vite.config.ts.
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
  // cytoscape) -- one deliberate `codeSplitting` group, see vite.config.ts.
  'vendor-graph': 606 * KB, // measured 577 KB

  // The first load, bundled by the `firstLoadGroups(...)` call in vite.config.ts
  // (cut rules: scripts/lib/firstLoadGroup.mjs). They replace the App, `t` and
  // ModelEffortDropdown chunks and ~340 small ones. Measured on an analyze build
  // of main @ c275f70106 with this change; each ceiling is ~5% over, rounded up
  // to whole KB.
  //
  // `first-load-shared` holds the first-load modules an app window also reaches,
  // the English catalog among them (English is the always-loaded fallback), so no
  // lazy `import()` can move most of it out.
  'first-load-shared': 3216 * KB, // measured 3,135,505 B
  // The dashboard's own first-load code, cut by import depth into four parts of
  // near-equal size; `main` is the top part, holding the entry. Because the cut
  // is by size, growth anywhere in it raises all four by about a quarter each.
  'first-load-0': 1003 * KB, // measured 977,976 B
  'first-load-1': 1164 * KB, // measured 1,134,732 B
  'first-load-2': 1179 * KB, // measured 1,149,207 B
  main: 1341 * KB, // measured 1,307,743 B

  // `heic-to/csp`: libheif and its HEVC decoder compiled to plain JavaScript,
  // which is why one decoder is this large. `utils/heicImage.ts`
  // imports it lazily, only when a HEIC/HEIF photo is attached in a browser with
  // no native HEIC decoder, so it is never on the first load. The size is the
  // vendor's decoder; no app code reaches this chunk. Re-measure on an upgrade.
  'heic-to': 3231 * KB, // measured 3,150,907 B (heic-to 1.6.6)
}

const REPORT_PATH = path.resolve('dist', 'bundle-report.json')

// This gate's own exit code, beyond the 2 (missing) / 3 (malformed) that
// loadSummaryOrExit owns: 4 = report valid but lists no chunks. That one is
// checked here rather than there because an empty report is legitimate for
// bundle-report.mjs, which simply has nothing to render.

export function main(argv = process.argv.slice(2)) {
  const reportPath = argv[0] ? path.resolve(argv[0]) : REPORT_PATH
  const summary = loadSummaryOrExit(reportPath)
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
    failGate(
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

  // 5 = no `main` entry chunk, so the first-load total measured nothing.
  const firstLoad = firstLoadBytes(summary)
  if (!firstLoad) {
    failGate(`no 'main' entry chunk in ${reportPath} -- the first-load total measured nothing.`, 5)
  }
  const firstLoadOver = firstLoad.bytes > FIRST_LOAD_BUDGET_BYTES
  const firstLoadLine =
    `first load: ${firstLoad.chunkCount} chunks, ${formatBytes(firstLoad.bytes)} ` +
    `(budget ${formatBytes(FIRST_LOAD_BUDGET_BYTES)})`

  if (breaches.length === 0 && !firstLoadOver) {
    process.stdout.write(
      `bundle-size gate: ${checkedCount} chunks within budget ` +
        `(default ${formatBytes(DEFAULT_BUDGET_BYTES)}, ${Object.keys(CHUNK_BUDGETS).length} allowlisted); ` +
        `${firstLoadLine}.\n`
    )
    return
  }

  if (firstLoadOver) {
    process.stderr.write(
      `FAIL ${firstLoadLine}: over by ${formatBytes(firstLoad.bytes - FIRST_LOAD_BUDGET_BYTES)}. ` +
        'Something new is statically imported from the entry; load it with a lazy import() ' +
        'or justify a new FIRST_LOAD_BUDGET_BYTES in scripts/check-bundle-size.mjs.\n'
    )
    if (breaches.length === 0) failGate('the first load is over budget.')
  }

  // One actionable line per breach: a developer must be able to act on the
  // failure without re-running anything locally.
  for (const b of breaches) {
    process.stderr.write(
      `FAIL ${b.fileName}: ${formatBytes(b.size)} exceeds its ${formatBytes(b.budget)} budget ` +
        `by ${formatBytes(b.overage)} (chunk '${b.logicalName}')\n`
    )
  }
  failGate(
    `${breaches.length} chunk(s) over budget. Either shrink the chunk (prefer a lazy ` +
      'import() boundary or a codeSplitting group -- see website/vite.config.ts), or, if the ' +
      'growth is genuinely irreducible, add/adjust its entry in CHUNK_BUDGETS in ' +
      'scripts/check-bundle-size.mjs with a comment saying why, and justify it in the PR.'
  )
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main()
}
