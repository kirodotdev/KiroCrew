// Pure helpers for the debug-only bundle weight report.
//
// Kept dependency-free and side-effect-free on purpose. Rollup already knows the
// rendered size of every module it emitted, so a bundle report needs no analyzer
// package -- adding one would put a build-time dependency (and its transitive
// tree) into a repo that is already carrying a long dependabot backlog, to
// compute numbers the bundler hands us for free.
//
// Split out from the Vite plugin so the arithmetic and formatting are testable
// without running a build.
//
// One deliberate exception to side-effect-free: `loadBundleSummary` reads the
// report file from disk. It lives here because it is the single shared
// implementation of the report's on-disk contract (existence, JSON shape,
// version) for every consumer -- it returns errors rather than exiting, so each
// caller keeps its own exit-code mapping.
import { readFileSync, existsSync } from 'fs'

/** Bytes-to-human, fixed-width friendly. */
export function formatBytes(bytes) {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

/**
 * Attribute a module path to a coarse owner bucket.
 *
 * The question this report answers is "what is making the bundle big", and the
 * useful granularity for that is the dependency or source area, not the
 * individual file. node_modules entries collapse to their package name
 * (including the scope, so `@scope/pkg` stays distinct from another `pkg`).
 */
export function ownerOf(modulePath) {
  if (typeof modulePath !== 'string' || !modulePath) return '(unknown)'
  const normalized = modulePath.replace(/\\/g, '/')
  const nm = normalized.lastIndexOf('node_modules/')
  if (nm !== -1) {
    const rest = normalized.slice(nm + 'node_modules/'.length)
    const parts = rest.split('/').filter(Boolean)
    if (parts.length === 0) return '(unknown)'
    // Scoped packages keep two segments; everything else takes one.
    return parts[0].startsWith('@') && parts.length > 1 ? `${parts[0]}/${parts[1]}` : parts[0]
  }
  // First-party code: bucket by the directory under src/ so "pages" and
  // "components" are separable without listing every file.
  const src = normalized.lastIndexOf('/src/')
  if (src !== -1) {
    const parts = normalized.slice(src + '/src/'.length).split('/').filter(Boolean)
    if (parts.length > 1) return `src/${parts[0]}`
    return 'src'
  }
  return '(other)'
}

/**
 * Reduce Rollup's `generateBundle` output to a serializable summary.
 *
 * `bundle` is the object Rollup passes to the hook: keys are output filenames,
 * values carry `type`, `code`/`source` and, for chunks, a `modules` map whose
 * entries have `renderedLength` (the bytes that module contributed AFTER
 * tree-shaking and minification, which is the number that actually matters --
 * a module's own file size overstates its cost when most of it is shaken out).
 */
export function summarizeBundle(bundle, options = {}) {
  const entries = Object.entries(bundle || {})
  const chunks = []
  const assets = []
  const owners = new Map()

  for (const [fileName, output] of entries) {
    if (!output || typeof output !== 'object') continue
    if (output.type === 'asset') {
      const source = output.source
      const size =
        typeof source === 'string'
          ? Buffer.byteLength(source)
          : source && typeof source.byteLength === 'number'
            ? source.byteLength
            : 0
      assets.push({ fileName, size })
      continue
    }
    const code = typeof output.code === 'string' ? output.code : ''
    const size = Buffer.byteLength(code)
    const modules = output.modules && typeof output.modules === 'object' ? output.modules : {}
    let moduleCount = 0
    for (const [modulePath, info] of Object.entries(modules)) {
      const rendered =
        info && typeof info.renderedLength === 'number' && Number.isFinite(info.renderedLength)
          ? info.renderedLength
          : 0
      // renderedLength 0 means fully tree-shaken; counting it as an owner would
      // pad the report with modules that cost nothing.
      if (rendered <= 0) continue
      moduleCount += 1
      const owner = ownerOf(modulePath)
      owners.set(owner, (owners.get(owner) || 0) + rendered)
    }
    chunks.push({
      fileName,
      size,
      moduleCount,
      isEntry: Boolean(output.isEntry),
      isDynamicEntry: Boolean(output.isDynamicEntry),
      // STATIC imports only, as the bundler itself resolved them -- the edge set
      // an initialization cycle can form on. `dynamicImports` is deliberately
      // excluded: a dynamic edge defers execution, so it cannot make a chunk
      // body run before something it references has initialized. Recorded as an
      // additive field on version 1: a consumer that does not know about it is
      // unaffected, and findChunkCycles refuses a report that lacks it rather
      // than reading "no imports" as "no cycles".
      imports: Array.isArray(output.imports) ? [...output.imports] : [],
    })
  }

  // Byte comparison rather than localeCompare throughout: filenames, package
  // names and source paths are machine values, and a locale-sensitive tiebreak
  // would make the report order differ between machines running the same build.
  const byName = (a, b) => (a < b ? -1 : a > b ? 1 : 0)
  chunks.sort((a, b) => b.size - a.size || byName(a.fileName, b.fileName))
  assets.sort((a, b) => b.size - a.size || byName(a.fileName, b.fileName))
  const ownerList = [...owners.entries()]
    .map(([owner, size]) => ({ owner, size }))
    .sort((a, b) => b.size - a.size || byName(a.owner, b.owner))

  return {
    version: 1,
    generatedAt: options.now ? options.now() : new Date().toISOString(),
    totals: {
      chunkBytes: chunks.reduce((a, c) => a + c.size, 0),
      assetBytes: assets.reduce((a, c) => a + c.size, 0),
      chunkCount: chunks.length,
      assetCount: assets.length,
    },
    chunks,
    assets,
    owners: ownerList,
  }
}

/** Render a summary as a fixed-width text report. */
export function renderReport(summary, options = {}) {
  const top = Number.isInteger(options.top) && options.top > 0 ? options.top : 15
  if (!summary || typeof summary !== 'object') return 'No bundle summary available.'
  const t = summary.totals || {}
  const lines = []
  lines.push(`Bundle report  (generated ${summary.generatedAt || 'unknown'})`)
  lines.push(
    `JS chunks: ${t.chunkCount ?? 0} totalling ${formatBytes(t.chunkBytes)}   ` +
      `other assets: ${t.assetCount ?? 0} totalling ${formatBytes(t.assetBytes)}`
  )

  const chunks = Array.isArray(summary.chunks) ? summary.chunks : []
  if (chunks.length) {
    lines.push('')
    lines.push(`Largest chunks (top ${Math.min(top, chunks.length)}):`)
    lines.push(`  ${'SIZE'.padStart(10)}  ${'MODULES'.padStart(7)}  KIND     FILE`)
    for (const c of chunks.slice(0, top)) {
      const kind = c.isEntry ? 'entry' : c.isDynamicEntry ? 'dynamic' : 'shared'
      lines.push(
        `  ${formatBytes(c.size).padStart(10)}  ${String(c.moduleCount ?? 0).padStart(7)}  ` +
          `${kind.padEnd(7)}  ${c.fileName}`
      )
    }
  }

  const owners = Array.isArray(summary.owners) ? summary.owners : []
  if (owners.length) {
    lines.push('')
    lines.push(`Heaviest contributors after tree-shaking (top ${Math.min(top, owners.length)}):`)
    lines.push(`  ${'SIZE'.padStart(10)}  OWNER`)
    for (const o of owners.slice(0, top)) {
      lines.push(`  ${formatBytes(o.size).padStart(10)}  ${o.owner}`)
    }
  }
  return lines.join('\n')
}

/**
 * Strip the output prefix and content hash from a chunk file name, leaving the
 * chunk's logical name.
 *
 * Budgets must be keyed by something stable across builds, and the emitted file
 * name is not: `assets/main-CZ3WY91T.js` carries a content hash that changes on
 * every edit. The logical name (`main`) is what Rollup derived from the entry,
 * the dynamic-import source, or a `codeSplitting` group name, and only changes
 * when the chunk graph itself changes.
 */
export function logicalChunkName(fileName) {
  if (typeof fileName !== 'string' || !fileName) return ''
  const base = fileName.replace(/\\/g, '/').split('/').pop() || ''
  // Vite/Rollup content hashes are 8 chars of [A-Za-z0-9_-] before the
  // extension. An unhashed build (or a name whose tail is not a hash) falls
  // through to the plain basename, so the helper never returns a surprise.
  const m = base.match(/^(.+)-[A-Za-z0-9_-]{8}\.js$/)
  if (m) return m[1]
  return base.replace(/\.js$/, '')
}

/**
 * Check every JS chunk in a summary against a per-chunk byte budget.
 *
 * `budgets` maps a LOGICAL chunk name (see `logicalChunkName`) to an explicit
 * byte ceiling; every chunk without an entry gets `defaultBudget`. Assets
 * (css, images, the report itself) are not gated: the regression class this
 * catches is "a new eager JS chunk slipped past the global warning limit", and
 * assets have different, format-specific size stories.
 *
 * Returns the verdict as data rather than printing or exiting, so the
 * arithmetic is testable without spawning a process:
 *   - `breaches`: chunks over their budget, largest overage first, each with
 *     the resolved budget and the overage in bytes.
 *   - `unusedBudgets`: allowlist entries no emitted chunk matched. Not a
 *     failure -- a renamed chunk already fails against the default budget --
 *     but reported so stale entries get cleaned up rather than accreting.
 */
export function checkChunkBudgets(summary, { budgets = {}, defaultBudget } = {}) {
  const chunks = summary && Array.isArray(summary.chunks) ? summary.chunks : []
  const seen = new Set()
  const breaches = []
  for (const chunk of chunks) {
    if (!chunk || typeof chunk.fileName !== 'string') continue
    const logicalName = logicalChunkName(chunk.fileName)
    const hasOverride = Object.prototype.hasOwnProperty.call(budgets, logicalName)
    if (hasOverride) seen.add(logicalName)
    const budget = hasOverride ? budgets[logicalName] : defaultBudget
    const size = typeof chunk.size === 'number' && Number.isFinite(chunk.size) ? chunk.size : 0
    if (size > budget) {
      breaches.push({ fileName: chunk.fileName, logicalName, size, budget, overage: size - budget })
    }
  }
  breaches.sort(
    (a, b) => b.overage - a.overage || (a.fileName < b.fileName ? -1 : a.fileName > b.fileName ? 1 : 0)
  )
  const unusedBudgets = Object.keys(budgets).filter((name) => !seen.has(name)).sort()
  return { breaches, unusedBudgets, checkedCount: chunks.length }
}

/**
 * Find static-import cycles between the emitted JS chunks.
 *
 * The failure this catches is not a size regression, it is a blank page. When
 * two chunks statically import each other, one body runs before the other has
 * finished initializing, so a binding it reads is still uninitialized -- in this
 * app that surfaces as `new QueryClient(...)` throwing before React mounts, and
 * the user sees the shell's dark skeleton and nothing else. No existing gate can
 * see it: the per-chunk budget above measures bytes, and the unit suite never
 * loads a built bundle.
 *
 * It is reachable from ordinary config edits rather than from application code.
 * Setting rolldown's `includeDependenciesRecursively: false` produced two cycles
 * on a tree that had none -- a 71-chunk one spanning App, client, vendor-react
 * and vendor-icons, and a 3-chunk one across the graph chunks -- while every
 * other gate stayed green.
 *
 * Returns the verdict as data rather than printing or exiting, matching
 * `checkChunkBudgets`: `cycles` is a list of strongly connected components with
 * more than one chunk (plus any self-loop), each sorted for stable output, and
 * `edgeCount` / `checkedCount` describe what was actually measured.
 *
 * `imports` is required. A summary whose chunks carry no `imports` key measured
 * no edges at all, and reading that as "no cycles" would be a green gate over an
 * unmeasured graph, so it is reported through `missingImports` for the caller to
 * fail on.
 */
export function findChunkCycles(summary) {
  const chunks = summary && Array.isArray(summary.chunks) ? summary.chunks : []
  const known = new Set()
  for (const chunk of chunks) {
    if (chunk && typeof chunk.fileName === 'string') known.add(chunk.fileName)
  }

  let withImports = 0
  const graph = new Map()
  for (const chunk of chunks) {
    if (!chunk || typeof chunk.fileName !== 'string') continue
    if (Array.isArray(chunk.imports)) withImports += 1
    const targets = new Set()
    for (const target of Array.isArray(chunk.imports) ? chunk.imports : []) {
      // Only edges between chunks this report describes. An import of something
      // outside the emitted set cannot participate in a cycle within it.
      if (typeof target === 'string' && known.has(target)) targets.add(target)
    }
    graph.set(chunk.fileName, targets)
  }

  const edgeCount = [...graph.values()].reduce((total, set) => total + set.size, 0)
  const missingImports = graph.size > 0 && withImports === 0

  // Tarjan, iterated rather than recursive: this graph runs to hundreds of
  // chunks and a recursive walk would risk the stack on a deep dependency path.
  const index = new Map()
  const low = new Map()
  const onStack = new Set()
  const stack = []
  const cycles = []
  let counter = 0

  for (const root of graph.keys()) {
    if (index.has(root)) continue
    index.set(root, counter)
    low.set(root, counter)
    counter += 1
    stack.push(root)
    onStack.add(root)
    const work = [{ node: root, children: [...graph.get(root)].sort()[Symbol.iterator]() }]
    while (work.length > 0) {
      const frame = work[work.length - 1]
      let descended = false
      for (const child of frame.children) {
        if (!index.has(child)) {
          index.set(child, counter)
          low.set(child, counter)
          counter += 1
          stack.push(child)
          onStack.add(child)
          work.push({ node: child, children: [...(graph.get(child) || [])].sort()[Symbol.iterator]() })
          descended = true
          break
        }
        if (onStack.has(child)) low.set(frame.node, Math.min(low.get(frame.node), index.get(child)))
      }
      if (descended) continue
      work.pop()
      const parent = work.length > 0 ? work[work.length - 1].node : null
      if (parent !== null) low.set(parent, Math.min(low.get(parent), low.get(frame.node)))
      if (low.get(frame.node) === index.get(frame.node)) {
        const component = []
        for (;;) {
          const top = stack.pop()
          onStack.delete(top)
          component.push(top)
          if (top === frame.node) break
        }
        const selfLoop = graph.get(frame.node).has(frame.node)
        if (component.length > 1 || selfLoop) cycles.push(component.sort())
      }
    }
  }

  cycles.sort((a, b) => b.length - a.length || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
  return { cycles, edgeCount, checkedCount: graph.size, missingImports }
}

/**
 * Compare two summaries. Used to answer "did my change make it bigger", which is
 * the question a report is usually opened to settle.
 */
export function diffSummaries(before, after) {
  const b = (before && before.totals) || {}
  const a = (after && after.totals) || {}
  const byOwner = new Map()
  for (const o of (before && before.owners) || []) byOwner.set(o.owner, -o.size)
  for (const o of (after && after.owners) || []) {
    byOwner.set(o.owner, (byOwner.get(o.owner) || 0) + o.size)
  }
  const changed = [...byOwner.entries()]
    .filter(([, delta]) => delta !== 0)
    .map(([owner, delta]) => ({ owner, delta }))
    .sort(
      (x, y) =>
        Math.abs(y.delta) - Math.abs(x.delta) ||
        (x.owner < y.owner ? -1 : x.owner > y.owner ? 1 : 0)
    )
  return {
    chunkBytesDelta: (a.chunkBytes || 0) - (b.chunkBytes || 0),
    assetBytesDelta: (a.assetBytes || 0) - (b.assetBytes || 0),
    owners: changed,
  }
}

/**
 * Load and validate a bundle-report.json written by the analyze-mode build.
 *
 * The single implementation of the report's on-disk contract, shared by the
 * bundle-size gate (check-bundle-size.mjs) and the report renderer
 * (bundle-report.mjs) so a report-format change (e.g. a version bump) is made
 * in exactly one place. Returns `{ summary }` on success or
 * `{ error: { code, message } }` -- it never exits or prints, so each caller
 * maps codes to its own exit behavior. Codes: 'missing' (no file at `file`),
 * 'invalid' (unparseable / not a report object / unsupported version).
 *
 * `hint` is appended to the missing-file message so each caller can name the
 * command that produces the report in ITS context (`npm run analyze` for the
 * renderer, the CI analyze build for the gate).
 */
export function loadBundleSummary(file, { hint = '' } = {}) {
  if (!existsSync(file)) {
    const suffix = hint ? `\n${hint}` : ''
    return { error: { code: 'missing', message: `No bundle report at ${file}.${suffix}` } }
  }
  let parsed
  try {
    parsed = JSON.parse(readFileSync(file, 'utf-8'))
  } catch (e) {
    return { error: { code: 'invalid', message: `${file} is not valid JSON: ${e && e.message}` } }
  }
  if (!parsed || typeof parsed !== 'object') {
    return { error: { code: 'invalid', message: `${file} does not contain a report object.` } }
  }
  if (parsed.version !== 1) {
    // Refuse rather than misread a future shape as v1.
    return {
      error: {
        code: 'invalid',
        message: `${file} has version ${JSON.stringify(parsed.version)}; this reader understands 1.`,
      },
    }
  }
  return { summary: parsed }
}

/** What produces the report in a gate's context. Module-private: the only reader
 * is loadSummaryOrExit's default below, and the renderer passes its own text. */
const ANALYZE_BUILD_HINT =
  'Run `vite build --mode analyze` first -- a plain `npm run build` deliberately ' +
  'does not write one, so the normal build stays unaffected.'

/**
 * Write a gate failure to stderr and exit with `code`.
 *
 * Lives here because all three report consumers -- both gates and the renderer --
 * had a byte-identical private copy of this, so a change to the message channel
 * or the default code silently applied to one and not the others.
 */
export function failGate(message, code = 1) {
  process.stderr.write(`${message}\n`)
  process.exit(code)
}

/**
 * Load a report or exit: 2 = missing, 3 = malformed or an unsupported version.
 *
 * That mapping and the accompanying hint were duplicated verbatim in
 * check-bundle-size.mjs and check-chunk-cycles.mjs, so the two would have drifted
 * the moment either message was reworded. The renderer passes its own `hint`
 * because it names a different command (`npm run analyze`), which is a real
 * difference rather than drift -- everything else about the contract is shared.
 *
 * Exit codes beyond 3 stay with each caller: they describe what THAT gate could
 * not measure, not what the report failed to be.
 */
export function loadSummaryOrExit(file, { hint = ANALYZE_BUILD_HINT } = {}) {
  const { summary, error } = loadBundleSummary(file, { hint })
  if (error) failGate(error.message, error.code === 'missing' ? 2 : 3)
  return summary
}
