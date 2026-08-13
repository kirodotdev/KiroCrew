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
