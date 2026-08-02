/**
 * Build-time asset pre-compression.
 *
 * The gateway serves the SPA over HTTP and does NOT compress responses — a fact
 * KiroCrew's own SSH-tunnel code documents as the reason `ssh -C` exists
 * (`src/kiro_crew/instances/constants.py`, `ssh_tunnel_manager.py`). A cold
 * dashboard load therefore transfers ~7.8 MB of uncompressed JS, which is felt
 * as lag whenever the dashboard is reached over a tunnel or a slow link.
 *
 * aiohttp's `FileResponse` already solves the serving half: it looks for a
 * `<file>.br` then `<file>.gz` sibling, and if the client's `Accept-Encoding`
 * allows it, serves that file with the right `Content-Encoding` and
 * `Vary: Accept-Encoding` — keeping the zero-copy sendfile path. So no server
 * code is needed; the build just has to emit the siblings.
 *
 * SCOPED TO CONTENT-HASHED ASSETS ON PURPOSE. aiohttp performs no staleness
 * check: if a sibling exists it is served blindly, even if it is older than the
 * file next to it. Vite's content-hashed `dist/assets` filenames make that safe
 * (a changed chunk gets a new name, so a stale sibling is simply never
 * requested), but stable-named files served by the gateway's other static
 * mounts — `/vendor`, `/fonts`, `/app-assets`, `index.html` — could be served
 * stale forever. Those are deliberately left uncompressed.
 */

import { constants, brotliCompressSync, gzipSync } from 'node:zlib'
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import path from 'node:path'

/** Extensions worth compressing. Media and fonts are already compressed. */
export const COMPRESSIBLE_EXTENSIONS = Object.freeze(['.js', '.css', '.svg', '.json', '.map'])

/**
 * Below this size the HTTP overhead of a second representation outweighs the
 * saving, and tiny files often grow under gzip's header.
 */
export const MIN_SIZE_BYTES = 1024

/** Suffixes we ourselves emit — never recurse into them. */
const SIBLING_SUFFIXES = Object.freeze(['.gz', '.br'])

/**
 * Whether `name` (a file name, not a path) at `size` bytes should be compressed.
 */
export function shouldCompress(name, size) {
  if (SIBLING_SUFFIXES.some(s => name.endsWith(s))) return false
  if (size < MIN_SIZE_BYTES) return false
  return COMPRESSIBLE_EXTENSIONS.includes(path.extname(name))
}

/**
 * Compress every eligible file under `dir`, writing `<file>.gz` and `<file>.br`
 * siblings. A sibling is only kept when it is actually smaller than the source:
 * an incompressible payload would otherwise be served as a LARGER compressed
 * response than the original.
 *
 * Returns `{ files, rawBytes, gzipBytes, brotliBytes }` for build logging.
 */
export function compressDir(dir) {
  const stats = { files: 0, rawBytes: 0, gzipBytes: 0, brotliBytes: 0 }
  let entries
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch (e) {
    if (e && e.code === 'ENOENT') return stats  // no assets dir (library/test build)
    throw e
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      const sub = compressDir(full)
      stats.files += sub.files
      stats.rawBytes += sub.rawBytes
      stats.gzipBytes += sub.gzipBytes
      stats.brotliBytes += sub.brotliBytes
      continue
    }
    if (!entry.isFile()) continue  // symlinks etc: skip rather than follow
    const size = statSync(full).size
    if (!shouldCompress(entry.name, size)) continue
    const source = readFileSync(full)
    const gz = gzipSync(source, { level: 9 })
    const br = brotliCompressSync(source, {
      params: {
        [constants.BROTLI_PARAM_QUALITY]: 11,
        [constants.BROTLI_PARAM_SIZE_HINT]: source.length,
      },
    })
    let counted = false
    if (gz.length < size) {
      writeFileSync(`${full}.gz`, gz)
      stats.gzipBytes += gz.length
      counted = true
    }
    if (br.length < size) {
      writeFileSync(`${full}.br`, br)
      stats.brotliBytes += br.length
      counted = true
    }
    if (counted) {
      stats.files += 1
      stats.rawBytes += size
    }
  }
  return stats
}

/**
 * Vite plugin: emit `.gz` + `.br` siblings for content-hashed assets after the
 * bundle is written. Build-only — the dev server is untouched.
 *
 * Runs in `closeBundle` (not `writeBundle`) so it fires once after the final
 * pass; vite can run several rollup passes per build, and compressing in each
 * would redo the work. Re-running is harmless anyway: siblings are overwritten,
 * and the dist-staging copy (`rm -rf` + `cp -R`) clears old ones every build,
 * which is what keeps them from going stale.
 */
export function precompressPlugin({ outDir = 'dist', subdir = 'assets', log = true } = {}) {
  return {
    name: 'kirocrew-precompress',
    apply: 'build',
    closeBundle() {
      const target = path.resolve(process.cwd(), outDir, subdir)
      const stats = compressDir(target)
      if (log && stats.files > 0) {
        const mb = n => (n / 1e6).toFixed(2)
        // eslint-disable-next-line no-console -- build-time progress output
        console.log(
          `precompress: ${stats.files} assets, ${mb(stats.rawBytes)}MB raw -> ` +
          `${mb(stats.gzipBytes)}MB gzip / ${mb(stats.brotliBytes)}MB brotli`
        )
      }
    },
  }
}
